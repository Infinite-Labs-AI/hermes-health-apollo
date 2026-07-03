from __future__ import annotations

import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse, request as urllib_request
import webbrowser

from . import normalize, oauth_token, store, sync_control


OURA_API_BASE_URL = "https://api.ouraring.com"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
DEFAULT_REDIRECT_URI = "http://localhost:43828/callback"
OURA_APPLICATIONS_URL = "https://cloud.ouraring.com/oauth/applications"
# These are the portal checkbox labels needed by the endpoints this plugin syncs.
REQUIRED_PORTAL_SCOPES = (
    "Daily",
    "Session",
    "SpO2",
    "Stress",
    "Heartrate",
    "Tag",
    "Workout",
    "Personal",
    "Heart Health",
    "Ring Configuration",
)
# Oura's OpenAPI 1.34 lists `spo2Daily`, while the auth docs and ecosystem
# clients use `spo2`. By default, omit the OAuth scope parameter and rely on the
# developer app being limited to REQUIRED_PORTAL_SCOPES.
DEFAULT_OAUTH_SCOPES: tuple[str, ...] = ()
DAILY_ENDPOINTS = {
    "daily_sleep": "/v2/usercollection/daily_sleep",
    "readiness": "/v2/usercollection/daily_readiness",
    "stress": "/v2/usercollection/daily_stress",
    "activity": "/v2/usercollection/daily_activity",
    "spo2": "/v2/usercollection/daily_spo2",
}
DATE_DOCUMENT_ENDPOINTS = {
    "sleep": "/v2/usercollection/sleep",
    "workouts": "/v2/usercollection/workout",
    "sessions": "/v2/usercollection/session",
    "tags": "/v2/usercollection/tag",
    "enhanced_tags": "/v2/usercollection/enhanced_tag",
    "daily_resilience": "/v2/usercollection/daily_resilience",
    "daily_cardiovascular_age": "/v2/usercollection/daily_cardiovascular_age",
    "vo2_max": "/v2/usercollection/vO2_max",
    "sleep_time": "/v2/usercollection/sleep_time",
    "rest_mode_periods": "/v2/usercollection/rest_mode_period",
}
DATETIME_ENDPOINTS = {
    "heart_rate": "/v2/usercollection/heartrate",
    "ring_battery": "/v2/usercollection/ring_battery_level",
}
UNDATED_DOCUMENT_ENDPOINTS = {
    "ring_configuration": "/v2/usercollection/ring_configuration",
}
SINGLE_ENDPOINTS = {
    "personal_info": "/v2/usercollection/personal_info",
}
MAX_DATETIME_WINDOW_DAYS = 30
SYNC_ENDPOINTS = {
    **DAILY_ENDPOINTS,
    **DATE_DOCUMENT_ENDPOINTS,
    **DATETIME_ENDPOINTS,
    **UNDATED_DOCUMENT_ENDPOINTS,
    **SINGLE_ENDPOINTS,
}


class OuraNotConnected(RuntimeError):
    pass


class OuraAPIError(RuntimeError):
    pass


class OuraResponse:
    def __init__(
        self,
        status_code: int,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self.body


def sync_oura(
    *,
    request: Callable[..., Any] | None = None,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]
    | None = None,
    today: datetime | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
) -> dict:
    sync_started = time.monotonic()
    clock = today or datetime.now(UTC)
    explicit_window = any(value not in (None, "") for value in (start_date, end_date)) or lookback_days is not None
    start_date, end_date = sync_window(
        clock.date().isoformat(),
        start_date=start_date,
        end_date=end_date,
        lookback_days=lookback_days,
    )
    token = load_token()
    if token_expired(token):
        client_id, client_secret = load_oura_client_credentials()
        token = refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            http_post=http_post or http_post_form,
        )
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise OuraNotConnected("Oura access token missing; re-run `/health connect`.")

    requester = request or http_request
    with store.sync_guard("oura"):
        with store.connect() as conn:
            source_id = sync_control.ensure_source(
                conn,
                source_slug="oura",
                provider="oura",
                connection_name="Primary Oura account",
                status="connected",
                sync_mode="pull",
                metadata={"portal_scopes": list(REQUIRED_PORTAL_SCOPES)},
            )
            sync_control.ensure_scope_rows(
                conn,
                source_id=source_id,
                scopes=[
                    {"scope_key": "daily", "scope_label": "Oura daily"},
                    {"scope_key": "session", "scope_label": "Oura session"},
                    {"scope_key": "spo2", "scope_label": "Oura SpO2"},
                    {"scope_key": "stress", "scope_label": "Oura stress"},
                    {"scope_key": "heartrate", "scope_label": "Oura heartrate"},
                    {"scope_key": "tag", "scope_label": "Oura tag"},
                    {"scope_key": "workout", "scope_label": "Oura workout"},
                    {"scope_key": "personal", "scope_label": "Oura personal"},
                    {"scope_key": "heart_health", "scope_label": "Oura heart health"},
                    {
                        "scope_key": "ring_configuration",
                        "scope_label": "Oura ring configuration",
                    },
                ],
            )
            sync_run_id = sync_control.start_sync_run(
                conn,
                source_id=source_id,
                trigger_kind="backfill" if explicit_window else "manual",
                request_start=start_date,
                request_end=end_date,
            )
        rows: dict[str, list[dict[str, Any]]] = {}
        endpoint_errors: dict[str, str] = {}
        endpoint_timings_ms: dict[str, int] = {}
        successful_endpoints: set[str] = set()
        date_params = {"start_date": start_date, "end_date": end_date}

        for name, endpoint in {**DAILY_ENDPOINTS, **DATE_DOCUMENT_ENDPOINTS}.items():
            _collect_paginated(
                rows,
                endpoint_errors,
                successful_endpoints,
                name=name,
                endpoint=endpoint,
                access_token=access_token,
                request=requester,
                params=date_params,
                endpoint_timings_ms=endpoint_timings_ms,
            )
        for name, endpoint in DATETIME_ENDPOINTS.items():
            _collect_datetime_paginated(
                rows,
                endpoint_errors,
                successful_endpoints,
                name=name,
                endpoint=endpoint,
                access_token=access_token,
                request=requester,
                params=date_params,
                endpoint_timings_ms=endpoint_timings_ms,
            )
        for name, endpoint in UNDATED_DOCUMENT_ENDPOINTS.items():
            _collect_paginated(
                rows,
                endpoint_errors,
                successful_endpoints,
                name=name,
                endpoint=endpoint,
                access_token=access_token,
                request=requester,
                params={},
                endpoint_timings_ms=endpoint_timings_ms,
            )
        for name, endpoint in SINGLE_ENDPOINTS.items():
            _collect_single(
                rows,
                endpoint_errors,
                successful_endpoints,
                name=name,
                endpoint=endpoint,
                access_token=access_token,
                request=requester,
                endpoint_timings_ms=endpoint_timings_ms,
            )

        with store.connect() as conn:
            raw_map, raw_count, batch_count = _persist_oura_control_plane(
                conn,
                source_id=source_id,
                sync_run_id=sync_run_id,
                rows=rows,
                endpoint_errors=endpoint_errors,
                start_date=start_date,
                end_date=end_date,
            )

        if endpoint_errors and not successful_endpoints:
            with store.connect() as conn:
                sync_control.finish_sync_run(
                    conn,
                    sync_run_id=sync_run_id,
                    status="error",
                    records_seen=raw_count,
                    records_written=raw_count,
                    batch_count=batch_count,
                    error_count=len(endpoint_errors),
                )
                sync_control.mark_source_synced(
                    conn,
                    source_slug="oura",
                    status="partial",
                )
            first_error = next(iter(endpoint_errors.values()))
            raise OuraAPIError(f"Oura sync failed for all endpoints: {first_error}")

        daily_rows = normalize.normalize_daily_rows(
            daily_sleep=rows.get("daily_sleep", []),
            readiness=rows.get("readiness", []),
            stress=rows.get("stress", []),
            activity=rows.get("activity", []),
            spo2=rows.get("spo2", []),
        )
        sessions = normalize.normalize_sleep_sessions(rows.get("sleep", []))
        extra_rows = normalize.normalize_extra_rows(rows)
        coverage_diagnostics: dict[str, Any] = {}
        with store.connect() as conn:
            normalize.upsert_oura_rows(conn, daily_rows, sessions, extra_rows)
            _attach_oura_lineage(
                conn,
                raw_map=raw_map,
                rows=rows,
                sessions=sessions,
                extra_rows=extra_rows,
            )
            coverage_diagnostics = _oura_coverage_diagnostics(
                conn,
                start_date=start_date,
                end_date=end_date,
                rows=rows,
                sessions=sessions,
                extra_rows=extra_rows,
                endpoint_errors=endpoint_errors,
            )
            conn.execute(
                """
                UPDATE sync_state
                SET last_sync_date = CASE
                        WHEN last_sync_date IS NULL OR last_sync_date < ?
                        THEN ?
                        ELSE last_sync_date
                    END
                WHERE provider = 'oura'
                """,
                (end_date, end_date),
            )
            for object_type in {"daily", *rows.keys()}:
                sync_control.update_cursor(
                    conn,
                    source_slug="oura",
                    object_type=object_type,
                    cursor_kind="date_window_end",
                    cursor_value=end_date,
                    window_start=start_date,
                    window_end=end_date,
                )
            run_status = "partial" if endpoint_errors else "ok"
            sync_control.finish_sync_run(
                conn,
                sync_run_id=sync_run_id,
                status=run_status,
                records_seen=raw_count,
                records_written=raw_count,
                batch_count=batch_count,
                error_count=len(endpoint_errors),
            )
            sync_control.mark_source_synced(
                conn,
                source_slug="oura",
                status="partial" if endpoint_errors else "connected",
            )
    if endpoint_errors:
        mark_sync_partial("oura")
    return {
        "ok": True,
        "start_date": start_date,
        "end_date": end_date,
        "daily_rows": len(daily_rows),
        "sleep_sessions": len(sessions),
        **_extra_counts(extra_rows),
        "endpoint_errors": endpoint_errors,
        "endpoint_counts": _endpoint_counts(
            rows=rows,
            endpoint_errors=endpoint_errors,
            endpoint_timings_ms=endpoint_timings_ms,
        ),
        "coverage_diagnostics": coverage_diagnostics,
        "duration_ms": _elapsed_ms(sync_started),
    }


def connect_oura(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    code: str | None = None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    state: str | None = None,
    scopes: str | list[str] | tuple[str, ...] | None = None,
    loopback_timeout: float = 120,
    browser_open: Callable[[str], object] | None = None,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]
    | None = None,
) -> dict:
    if client_id and client_secret:
        save_oura_client_credentials(client_id, client_secret)
    else:
        client_id, client_secret = load_oura_client_credentials(required=False)

    if not client_id or not client_secret:
        return {
            "ok": False,
            "connected": False,
            **oura_registration_details(redirect_uri=redirect_uri),
            "guidance": (
                "Create an Oura developer application at registration_url. Use "
                "redirect_uri exactly, select the required_portal_scopes, save "
                "the app, copy its Client ID and Client Secret, then run "
                "`hermes health connect --client-id <id> "
                "--client-secret <secret>`. The credentials are stored in "
                "`~/.hermes/.env`; do not commit them."
            ),
        }

    manual_exchange = code is not None
    oauth_state = state if manual_exchange else (state or secrets.token_urlsafe(24))
    requested_scopes = normalize_scopes(scopes)
    auth_url = authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=oauth_state,
        scopes=requested_scopes,
    )
    if manual_exchange:
        validate_pending_state(oauth_state)
    if not code:
        if loopback_timeout > 0:
            try:
                code = run_loopback_oauth(
                    auth_url=auth_url,
                    redirect_uri=redirect_uri,
                    expected_state=oauth_state,
                    timeout=loopback_timeout,
                    browser_open=browser_open,
                )
            except OuraNotConnected as exc:
                save_pending_state(oauth_state)
                return {
                    "ok": False,
                    "connected": False,
                    "error": str(exc),
                    "authorize_url": auth_url,
                    "state": oauth_state,
                    "requested_oauth_scopes": requested_scopes,
                    **oura_registration_details(redirect_uri=redirect_uri),
                    "guidance": (
                        "Open authorize_url, approve Oura, copy the `code` query "
                        "parameter from the localhost callback URL, then re-run "
                        "`hermes health connect --code <code> --state <state>`."
                    ),
                }
        else:
            save_pending_state(oauth_state)
            return {
                "ok": False,
                "connected": False,
                "authorize_url": auth_url,
                "state": oauth_state,
                "requested_oauth_scopes": requested_scopes,
                **oura_registration_details(redirect_uri=redirect_uri),
                "guidance": (
                    "Open authorize_url, approve Oura, copy the `code` query "
                    "parameter from the localhost callback URL, then re-run "
                    "`hermes health connect --code <code> --state <state>`."
                ),
            }
    if not code:
        save_pending_state(oauth_state)
        return {
            "ok": False,
            "connected": False,
            "authorize_url": auth_url,
            "state": oauth_state,
            "requested_oauth_scopes": requested_scopes,
            **oura_registration_details(redirect_uri=redirect_uri),
            "guidance": (
                "Open authorize_url, approve Oura, copy the `code` query "
                "parameter from the localhost callback URL, then re-run "
                "`hermes health connect --code <code> --state <state>`."
            ),
        }

    response = exchange_code_for_token(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        http_post=http_post or http_post_form,
    )
    save_token(response)
    return {"ok": True, "connected": True, "token_path": str(token_path())}


def _persist_oura_control_plane(
    conn,
    *,
    source_id: str,
    sync_run_id: str,
    rows: dict[str, list[dict[str, Any]]],
    endpoint_errors: dict[str, str],
    start_date: str,
    end_date: str,
) -> tuple[dict[str, list[tuple[dict[str, Any], str]]], int, int]:
    raw_map: dict[str, list[tuple[dict[str, Any], str]]] = {}
    raw_count = 0
    batch_count = 0
    for object_type in SYNC_ENDPOINTS:
        batch_id = sync_control.start_sync_batch(
            conn,
            sync_run_id=sync_run_id,
            object_type=object_type,
            window_start=start_date,
            window_end=end_date,
        )
        batch_count += 1
        records = rows.get(object_type, [])
        persisted: list[tuple[dict[str, Any], str]] = []
        if object_type in endpoint_errors:
            sync_control.record_sync_error(
                conn,
                source_slug="oura",
                sync_run_id=sync_run_id,
                sync_batch_id=batch_id,
                object_type=object_type,
                error_code="oura_api_error",
                error_message=endpoint_errors[object_type],
                retryable=True,
            )
        else:
            for item in records:
                raw_record_id = sync_control.persist_raw_record(
                    conn,
                    source_id=source_id,
                    sync_batch_id=batch_id,
                    provider="oura",
                    object_type=object_type,
                    external_id=_oura_external_id(object_type, item),
                    payload=item,
                    source_updated_at=_oura_source_updated_at(item),
                    privacy_tier="sensitive" if object_type == "personal_info" else "standard",
                )
                persisted.append((item, raw_record_id))
            raw_count += len(persisted)
        raw_map[object_type] = persisted
        sync_control.finish_sync_batch(
            conn,
            sync_batch_id=batch_id,
            status="partial" if object_type in endpoint_errors else "ok",
            cursor_after=end_date,
            records_seen=len(records),
            records_written=len(persisted),
        )
    return raw_map, raw_count, batch_count


def _attach_oura_lineage(
    conn,
    *,
    raw_map: dict[str, list[tuple[dict[str, Any], str]]],
    rows: dict[str, list[dict[str, Any]]],
    sessions: list[dict[str, Any]],
    extra_rows: dict[str, list[dict]],
) -> None:
    for object_type in DAILY_ENDPOINTS:
        for item, raw_record_id in raw_map.get(object_type, []):
            day = item.get("day")
            if day:
                sync_control.attach_lineage(
                    conn,
                    canonical_table="oura_daily",
                    canonical_id=str(day),
                    raw_record_id=raw_record_id,
                )

    sleep_session_ids = {str(session["id"]) for session in sessions if session.get("id")}
    for item, raw_record_id in raw_map.get("sleep", []):
        record_id = item.get("id")
        if record_id and str(record_id) in sleep_session_ids:
            sync_control.attach_lineage(
                conn,
                canonical_table="oura_sleep_sessions",
                canonical_id=str(record_id),
                raw_record_id=raw_record_id,
            )
        day = item.get("day")
        if day:
            sync_control.attach_lineage(
                conn,
                canonical_table="oura_daily",
                canonical_id=str(day),
                raw_record_id=raw_record_id,
            )

    endpoint_to_extra_key = {
        "heart_rate": "heart_rate",
        "ring_battery": "ring_battery",
        "personal_info": "personal_info",
        "workouts": "workouts",
        "sessions": "sessions",
        "tags": "tags",
        "enhanced_tags": "enhanced_tags",
        "daily_resilience": "daily_resilience",
        "daily_cardiovascular_age": "daily_cardiovascular_age",
        "vo2_max": "vo2_max",
        "sleep_time": "sleep_time",
        "rest_mode_periods": "rest_mode_periods",
        "ring_configuration": "ring_configuration",
    }
    for endpoint, extra_key in endpoint_to_extra_key.items():
        spec = normalize.EXTRA_ROW_SPECS.get(extra_key)
        if not spec:
            continue
        table = str(spec["table"])
        primary_key = str(spec["primary_key"])
        normalized_ids = {
            str(row[primary_key]) for row in extra_rows.get(extra_key, []) if row.get(primary_key)
        }
        for item, raw_record_id in raw_map.get(endpoint, []):
            canonical_id = _oura_external_id(endpoint, item)
            if canonical_id not in normalized_ids:
                continue
            sync_control.attach_lineage(
                conn,
                canonical_table=table,
                canonical_id=canonical_id,
                raw_record_id=raw_record_id,
            )


def _oura_external_id(object_type: str, item: dict[str, Any]) -> str:
    for key in ("id", "day", "timestamp", "start_time", "set_up_at"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    payload = json.dumps(item, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{object_type}:{digest}"


def _oura_source_updated_at(item: dict[str, Any]) -> str | None:
    for key in ("updated_at", "timestamp", "end_datetime", "bedtime_end", "day"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def run_loopback_oauth(
    *,
    auth_url: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    expected_state: str,
    timeout: float = 120,
    browser_open: Callable[[str], object] | None = None,
) -> str:
    host, port, path = _loopback_parts(redirect_uri)
    captured: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = parse.urlparse(self.path)
            query = parse.parse_qs(parsed.query)
            if parsed.path != path:
                self._finish(404, "Unexpected callback path.")
                return
            state_values = query.get("state", [])
            if state_values != [expected_state]:
                captured["error"] = "Oura OAuth state mismatch."
                self._finish(400, "State mismatch. Return to Hermes and retry.")
                return
            error_values = query.get("error", [])
            if error_values:
                captured["error"] = f"Oura authorization failed: {error_values[0]}"
                self._finish(400, "Oura authorization failed.")
                return
            code_values = query.get("code", [])
            if not code_values:
                captured["error"] = "Oura callback did not include a code."
                self._finish(400, "Missing authorization code.")
                return
            captured["code"] = code_values[0]
            self._finish(200, "Oura connected. You can return to Hermes.")

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _finish(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer((host, port), CallbackHandler)
    server.timeout = timeout
    opener = browser_open or webbrowser.open
    opener(auth_url)
    try:
        server.handle_request()
    finally:
        server.server_close()
    if "error" in captured:
        raise OuraNotConnected(captured["error"])
    if "code" not in captured:
        raise OuraNotConnected(
            "Oura OAuth callback timed out; re-run `/health connect` or use manual code."
        )
    return captured["code"]


def _loopback_parts(redirect_uri: str) -> tuple[str, int, str]:
    parsed = parse.urlparse(redirect_uri)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost"}:
        raise OuraAPIError("Oura redirect URI must be an http loopback URL.")
    if not parsed.port:
        raise OuraAPIError("Oura redirect URI must include a loopback port.")
    return host, parsed.port, parsed.path or "/"


def sync_window(
    today_iso: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int | None = None,
) -> tuple[str, str]:
    today = datetime.fromisoformat(today_iso).date()
    explicit_end_date = end_date not in (None, "")
    end = _parse_sync_date(end_date, "end_date") if explicit_end_date else today
    if start_date not in (None, ""):
        start = _parse_sync_date(start_date, "start_date")
    elif lookback_days is not None:
        if lookback_days < 1:
            raise OuraAPIError("Oura sync lookback_days must be at least 1.")
        start = end - timedelta(days=lookback_days - 1)
    elif explicit_end_date:
        start = end - timedelta(days=2)
    else:
        start = None
    if start is not None:
        if start > end:
            raise OuraAPIError("Oura sync start_date must be on or before end_date.")
        return start.isoformat(), end.isoformat()

    last_sync = None
    with contextlib.suppress(Exception):
        store.initialize()
        with store.connect() as conn:
            row = conn.execute(
                "SELECT last_sync_date FROM sync_state WHERE provider = 'oura'"
            ).fetchone()
            last_sync = row[0] if row and row[0] else None
    if last_sync:
        start = datetime.fromisoformat(str(last_sync)).date() - timedelta(days=2)
    else:
        start = end - timedelta(days=2)
    return start.isoformat(), end.isoformat()


def _parse_sync_date(value: str | None, field_name: str):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise OuraAPIError(f"Oura sync {field_name} must be YYYY-MM-DD.") from exc


def datetime_window_params(start_date: str, end_date: str) -> dict[str, str]:
    return datetime_window_chunks(start_date, end_date)[0]


def datetime_window_chunks(start_date: str, end_date: str) -> list[dict[str, str]]:
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
    # Oura datetime endpoints treat end_datetime as a timestamp boundary, so use
    # the next midnight to cover the full requested end date.
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1)
    chunks = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=MAX_DATETIME_WINDOW_DAYS), end)
        chunks.append(
            {
                "start_datetime": cursor.isoformat(),
                "end_datetime": chunk_end.isoformat(),
            }
        )
        cursor = chunk_end
    if not chunks:
        return [
            {
                "start_datetime": start.isoformat(),
                "end_datetime": end.isoformat(),
            }
        ]
    return chunks


def _collect_datetime_paginated(
    rows: dict[str, list[dict[str, Any]]],
    endpoint_errors: dict[str, str],
    successful_endpoints: set[str],
    *,
    name: str,
    endpoint: str,
    access_token: str,
    request: Callable[..., Any],
    params: dict[str, Any],
    endpoint_timings_ms: dict[str, int],
) -> None:
    started = time.monotonic()
    records: list[dict[str, Any]] = []
    try:
        for chunk_params in datetime_window_chunks(
            str(params["start_date"]), str(params["end_date"])
        ):
            records.extend(
                fetch_paginated(
                    endpoint,
                    access_token=access_token,
                    request=request,
                    params=chunk_params,
                )
            )
    except OuraAPIError as exc:
        endpoint_errors[name] = str(exc)
    rows[name] = records
    if records or name not in endpoint_errors:
        successful_endpoints.add(name)
    endpoint_timings_ms[name] = _elapsed_ms(started)


def _collect_paginated(
    rows: dict[str, list[dict[str, Any]]],
    endpoint_errors: dict[str, str],
    successful_endpoints: set[str],
    *,
    name: str,
    endpoint: str,
    access_token: str,
    request: Callable[..., Any],
    params: dict[str, Any],
    endpoint_timings_ms: dict[str, int],
) -> None:
    started = time.monotonic()
    try:
        rows[name] = fetch_paginated(
            endpoint,
            access_token=access_token,
            request=request,
            params=params,
        )
        successful_endpoints.add(name)
    except OuraAPIError as exc:
        rows[name] = []
        endpoint_errors[name] = str(exc)
    endpoint_timings_ms[name] = _elapsed_ms(started)


def _collect_single(
    rows: dict[str, list[dict[str, Any]]],
    endpoint_errors: dict[str, str],
    successful_endpoints: set[str],
    *,
    name: str,
    endpoint: str,
    access_token: str,
    request: Callable[..., Any],
    endpoint_timings_ms: dict[str, int],
) -> None:
    started = time.monotonic()
    try:
        item = fetch_single(endpoint, access_token=access_token, request=request)
        rows[name] = [item] if item else []
        successful_endpoints.add(name)
    except OuraAPIError as exc:
        rows[name] = []
        endpoint_errors[name] = str(exc)
    endpoint_timings_ms[name] = _elapsed_ms(started)


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _endpoint_counts(
    *,
    rows: dict[str, list[dict[str, Any]]],
    endpoint_errors: dict[str, str],
    endpoint_timings_ms: dict[str, int],
) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "records": len(rows.get(name, [])),
            "status": "error" if name in endpoint_errors else "ok",
            "duration_ms": endpoint_timings_ms.get(name, 0),
        }
        for name in SYNC_ENDPOINTS
    }


def _oura_coverage_diagnostics(
    conn,
    *,
    start_date: str,
    end_date: str,
    rows: dict[str, list[dict[str, Any]]],
    sessions: list[dict[str, Any]],
    extra_rows: dict[str, list[dict]],
    endpoint_errors: dict[str, str],
) -> dict[str, dict[str, Any]]:
    daily_latest = _max_day(conn, "oura_daily")
    return {
        "daily": {
            "status": "ok" if rows.get("daily_sleep") else "empty_for_window",
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "fetched_rows": len(rows.get("daily_sleep", [])),
            "local_latest_day": daily_latest,
        },
        "sleep_sessions": _coverage_detail(
            label="sleep sessions",
            endpoint_name="sleep",
            endpoint_error=endpoint_errors.get("sleep"),
            fetched_rows=len(rows.get("sleep", [])),
            normalized_rows=len(sessions),
            supporting_rows=len(rows.get("daily_sleep", [])),
            local_latest_day=_max_day(conn, "oura_sleep_sessions"),
            start_date=start_date,
            end_date=end_date,
        ),
        "sleep_time": _coverage_detail(
            label="sleep time",
            endpoint_name="sleep_time",
            endpoint_error=endpoint_errors.get("sleep_time"),
            fetched_rows=len(rows.get("sleep_time", [])),
            normalized_rows=len(extra_rows.get("sleep_time", [])),
            supporting_rows=len(rows.get("daily_sleep", [])),
            local_latest_day=_max_day(conn, "oura_sleep_time"),
            start_date=start_date,
            end_date=end_date,
        ),
    }


def _coverage_detail(
    *,
    label: str,
    endpoint_name: str,
    endpoint_error: str | None,
    fetched_rows: int,
    normalized_rows: int,
    supporting_rows: int,
    local_latest_day: str | None,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    if endpoint_error:
        status = "endpoint_error"
        message = f"Oura {endpoint_name} endpoint failed: {endpoint_error}"
    elif fetched_rows > 0 and normalized_rows == 0:
        status = "normalization_gap"
        message = (
            f"Oura returned {fetched_rows} {endpoint_name} records, but none had "
            "the required fields for local storage."
        )
    elif local_latest_day and local_latest_day < start_date and fetched_rows == 0:
        status = "stale_local_coverage"
        message = (
            f"Local {label} coverage is only through {local_latest_day}; Oura "
            f"returned 0 {endpoint_name} records for {start_date}..{end_date}. "
            "OpenAPI 1.34 shows this endpoint uses the same start_date/end_date "
            "window shape as daily_sleep, so this is surfaced as upstream empty "
            "coverage, access/subscription behavior, or account data availability."
        )
    elif fetched_rows == 0 and supporting_rows > 0:
        status = "empty_endpoint_response"
        message = (
            f"Oura daily_sleep returned {supporting_rows} rows, but {endpoint_name} "
            "returned 0 rows for the same date window."
        )
    elif fetched_rows == 0:
        status = "empty_for_window"
        message = f"Oura returned no {endpoint_name} records for the requested window."
    else:
        status = "ok"
        message = f"Oura {endpoint_name} coverage is current for the requested window."
    return {
        "status": status,
        "requested_start_date": start_date,
        "requested_end_date": end_date,
        "fetched_rows": fetched_rows,
        "normalized_rows": normalized_rows,
        "supporting_daily_sleep_rows": supporting_rows,
        "local_latest_day": local_latest_day,
        "message": message,
    }


def _max_day(conn, table: str) -> str | None:
    row = conn.execute(f"SELECT MAX(day) FROM {table}").fetchone()
    value = row[0] if row else None
    return str(value) if value else None


def _extra_counts(extra_rows: dict[str, list[dict]]) -> dict[str, int]:
    return {
        "heart_rate_samples": len(extra_rows["heart_rate"]),
        "ring_battery_samples": len(extra_rows["ring_battery"]),
        "personal_info_rows": len(extra_rows["personal_info"]),
        "workouts": len(extra_rows["workouts"]),
        "sessions": len(extra_rows["sessions"]),
        "tags": len(extra_rows["tags"]),
        "enhanced_tags": len(extra_rows["enhanced_tags"]),
        "daily_resilience_rows": len(extra_rows["daily_resilience"]),
        "daily_cardiovascular_age_rows": len(
            extra_rows["daily_cardiovascular_age"]
        ),
        "vo2_max_rows": len(extra_rows["vo2_max"]),
        "sleep_time_rows": len(extra_rows["sleep_time"]),
        "rest_mode_periods": len(extra_rows["rest_mode_periods"]),
        "ring_configuration_rows": len(extra_rows["ring_configuration"]),
    }


def mark_sync_partial(provider: str) -> None:
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE sync_state
            SET last_status = 'partial',
                updated_at = ?
            WHERE provider = ?
            """,
            (datetime.now(UTC).isoformat(), provider),
        )


def token_path() -> Path:
    return store.hermes_home() / "oura_token.json"


def pending_token_path(path: Path | None = None) -> Path:
    return oauth_token.pending_path(path or token_path())


def lock_path(path: Path | None = None) -> Path:
    return oauth_token.lock_path(path or token_path())


def pending_state_path() -> Path:
    return store.hermes_home() / "oura_oauth_state.json"


def load_token(path: Path | None = None) -> dict[str, Any]:
    return oauth_token.load_token(
        path or token_path(),
        missing_exc=OuraNotConnected,
        missing_message="Run `hermes health connect` before Oura sync.",
        invalid_message="Oura token file is invalid; re-run `/health connect`.",
    )


def env_path() -> Path:
    return store.hermes_home() / ".env"


def load_oura_client_credentials(*, required: bool = True) -> tuple[str | None, str | None]:
    return oauth_token.load_client_credentials(
        env_file=env_path(),
        id_key="HERMES_OURA_CLIENT_ID",
        secret_key="HERMES_OURA_CLIENT_SECRET",
        required=required,
        missing_exc=OuraNotConnected,
        missing_message="Oura client credentials missing; re-run `/health connect`.",
    )


def save_oura_client_credentials(client_id: str, client_secret: str) -> Path:
    return oauth_token.save_client_credentials(
        env_file=env_path(),
        values={
            "HERMES_OURA_CLIENT_ID": client_id,
            "HERMES_OURA_CLIENT_SECRET": client_secret,
        },
    )


def clear_oura_client_credentials() -> bool:
    return oauth_token.clear_client_credentials(
        env_file=env_path(),
        keys=["HERMES_OURA_CLIENT_ID", "HERMES_OURA_CLIENT_SECRET"],
    )


def save_pending_state(state: str) -> Path:
    path = pending_state_path()
    oauth_token.write_json_atomic(path, {"state": state}, private=True)
    return path


def validate_pending_state(state: str | None) -> None:
    if not state:
        raise OuraNotConnected(
            "Oura OAuth state is required; restart with `hermes health connect`."
        )
    path = pending_state_path()
    if not path.exists():
        raise OuraNotConnected(
            "Oura OAuth state is missing or expired; restart with `hermes health connect`."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OuraNotConnected(
            "Oura OAuth state file is invalid; restart with `hermes health connect`."
        ) from exc
    expected = payload.get("state") if isinstance(payload, dict) else None
    if state != expected:
        raise OuraNotConnected("Oura OAuth state mismatch; restart Oura connect.")
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def save_token(token: dict[str, Any], path: Path | None = None) -> Path:
    return oauth_token.save_token(token, path or token_path())


def token_lock(path: Path | None = None):
    return oauth_token.token_lock(path or token_path())


def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]],
    now: Callable[[], datetime] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    return oauth_token.refresh_access_token(
        token_file=path or token_path(),
        token_url=OURA_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        http_post=http_post,
        now=now,
        not_connected_exc=OuraNotConnected,
        api_error_exc=OuraAPIError,
        missing_token_message="Run `hermes health connect` before Oura sync.",
        invalid_token_message="Oura token file is invalid; re-run `/health connect`.",
        missing_refresh_message="Oura refresh token missing; re-run `/health connect`.",
        invalid_grant_message="Oura refresh failed; re-run `/health connect`.",
        no_access_token_message=(
            "Oura token refresh response did not include an access token."
        ),
    )


def authorize_url(
    *,
    client_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    state: str | None = None,
    scopes: str | list[str] | tuple[str, ...] | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    requested_scopes = normalize_scopes(scopes)
    if requested_scopes:
        params["scope"] = requested_scopes
    return f"{OURA_AUTH_URL}?{parse.urlencode(params)}"


def normalize_scopes(scopes: str | list[str] | tuple[str, ...] | None) -> str:
    if scopes is None:
        scopes = DEFAULT_OAUTH_SCOPES
    if isinstance(scopes, str):
        return " ".join(scopes.split())
    return " ".join(str(scope).strip() for scope in scopes if str(scope).strip())


def oura_registration_details(
    *, redirect_uri: str = DEFAULT_REDIRECT_URI
) -> dict[str, object]:
    return {
        "registration_url": OURA_APPLICATIONS_URL,
        "redirect_uri": redirect_uri,
        "required_portal_scopes": list(REQUIRED_PORTAL_SCOPES),
        "default_oauth_scope_behavior": (
            "The plugin omits the OAuth `scope` parameter by default so Oura "
            "requests the scopes enabled on your app. Keep the app limited to "
            "the required portal scopes above. Oura OpenAPI 1.34 names the "
            "SpO2 OAuth scope `spo2Daily`, while some auth examples use `spo2`."
        ),
    }


def exchange_code_for_token(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]],
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    return oauth_token.exchange_code_for_token(
        token_url=OURA_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        http_post=http_post,
        now=now,
        api_error_exc=OuraAPIError,
        error_message="Oura authorization failed.",
        missing_tokens_message="Oura token response did not include required tokens.",
    )


def token_expired(
    token: dict[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
    skew: timedelta = timedelta(minutes=5),
) -> bool:
    return oauth_token.token_expired(token, now=now, skew=skew)


def fetch_paginated(
    endpoint: str,
    *,
    access_token: str,
    request: Callable[..., Any],
    params: dict[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    base_url: str = OURA_API_BASE_URL,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    base_params = dict(params or {})
    headers = {"Authorization": f"Bearer {access_token}"}
    rows: list[dict[str, Any]] = []
    next_token: str | None = None

    while True:
        request_params = dict(base_params)
        if next_token:
            request_params["next_token"] = next_token
        retry_count = 0
        while True:
            response = request("GET", url, params=request_params, headers=headers)
            status = _response_status(response)
            if status != 429:
                break
            if retry_count >= max_retries:
                raise OuraAPIError("Oura API rate limit retry budget exhausted.")
            sleep(_retry_after(response, retry_count))
            retry_count += 1

        if status >= 400:
            raise OuraAPIError(f"Oura API request failed with status {status}.")
        body = _response_json(response)
        data = body.get("data", [])
        if not isinstance(data, list):
            raise OuraAPIError("Oura API response data was not a list.")
        rows.extend(data)
        next_value = body.get("next_token")
        if not next_value:
            return rows
        next_token = str(next_value)


def fetch_single(
    endpoint: str,
    *,
    access_token: str,
    request: Callable[..., Any],
    params: dict[str, Any] | None = None,
    base_url: str = OURA_API_BASE_URL,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    response = request(
        "GET",
        url,
        params=dict(params or {}),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    status = _response_status(response)
    if status >= 400:
        raise OuraAPIError(f"Oura API request failed with status {status}.")
    body = _response_json(response)
    data = body.get("data")
    if data is None:
        return body
    if data == []:
        return {}
    if not isinstance(data, dict):
        raise OuraAPIError("Oura API response data was not an object.")
    return data


def http_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str],
) -> OuraResponse:
    if params:
        url = f"{url}?{parse.urlencode(params)}"
    req = urllib_request.Request(url, method=method, headers=headers)
    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            body = _read_json_body(response)
            return OuraResponse(response.status, body, dict(response.headers.items()))
    except urllib_error.HTTPError as exc:
        body = _read_json_body(exc)
        return OuraResponse(exc.code, body, dict(exc.headers.items()))


def http_post_form(
    url: str,
    data: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return oauth_token.http_post_form(
        url,
        data,
        headers,
        error_exc=OuraAPIError,
        not_object_message="Oura token endpoint response JSON was not an object.",
        http_error_message="Oura token endpoint failed with status {status}.",
    )


def _read_json_body(response: Any) -> Any:
    try:
        raw = response.read().decode("utf-8")
    except OSError as exc:
        raise OuraAPIError("Oura response body could not be read.") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OuraAPIError("Oura response body was not valid JSON.") from exc


def _response_status(response: Any) -> int:
    return int(getattr(response, "status_code", 200))


def _response_json(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    data = response.json()
    if not isinstance(data, dict):
        raise OuraAPIError("Oura API response JSON was not an object.")
    return data


def _retry_after(response: Any, retry_count: int) -> float:
    headers = getattr(response, "headers", {}) or {}
    try:
        return float(headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return float(2**retry_count)
