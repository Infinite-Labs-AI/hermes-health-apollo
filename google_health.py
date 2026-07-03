"""Google Health normalization + persistence onto the canonical health tables.

This is the offline normalization layer: it maps an already-fetched Google Health
API response into the source-linked canonical tables (sample / interval / session /
daily) with raw lineage, reusing the existing source + raw-record + lineage helpers.

The point-field access is reconciled to the real Google Health API **v4** resource
model (verified against developers.google.com/health and the v4 discovery doc): a
DataPoint is ``{name, dataSource, <field>: {...}}`` where the populated union field
matches the data type. This layer derives the canonical ``sample_time_unix`` /
``start_time_unix`` / ``end_time_unix`` columns from the provider instants (the
format-independent ordering guard) so offline fixtures and the live connector
populate them consistently.

Out of scope here (separate follow-up PRs): Fitbit as a second source, GPS /
sleep-stage child tables, and semantic-layer projection. The live OAuth + HTTP
fetch that feeds this layer lives in ``sync_google_health`` (below) and
``google_health_auth``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import date as _date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse, request as urllib_request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import google_health_auth, oauth_token, store, sync_control


SOURCE_SLUG = "google_health"
PROVIDER = "google_health"

logger = logging.getLogger(__name__)

# Google Health API v4 ``dataType`` -> canonical mapping, one entry per temporal
# shape for the MVP. Keys are the real v4 data type ids (the kebab-case path
# segment of ``users/*/dataTypes/{dataType}``). ``field`` is the camelCase union
# sub-object present on each DataPoint and ``value_key`` is the scalar measurement
# inside it. Extend by adding rows; each shape routes to one canonical table.
#
# Verified field layout (v4 discovery doc): samples carry
# ``<field>.sampleTime.physicalTime``; intervals/sessions carry
# ``<field>.interval.{startTime,endTime}``; daily metrics carry ``<field>.date``
# ({year,month,day}). Values are JSON int64 strings/numbers, NOT the legacy Google
# Fit ``value:[{fpVal|intVal}]`` arrays.
METRIC_REGISTRY: dict[str, dict[str, Any]] = {
    "heart-rate": {
        "shape": "sample",
        "metric": "heart_rate",
        "unit": "bpm",
        "field": "heartRate",
        "value_key": "beatsPerMinute",
    },
    "steps": {
        "shape": "interval",
        "metric": "steps",
        "unit": "count",
        "field": "steps",
        "value_key": "count",
    },
    "sleep": {
        "shape": "session",
        "session_type": "sleep",
        "field": "sleep",
    },
    "daily-resting-heart-rate": {
        "shape": "daily",
        "metric": "resting_heart_rate",
        "unit": "bpm",
        "aggregation_kind": "provider_daily_summary",
        "field": "dailyRestingHeartRate",
        "value_key": "beatsPerMinute",
    },
}


def persist_google_health(
    conn: sqlite3.Connection,
    *,
    responses: list[dict[str, Any]],
    sync_batch_id: str | None = None,
) -> dict[str, int]:
    """Persist Google Health ``dataPoints``-style responses into canonical tables.

    Each response is ``{"dataType", "userId", "point": [...]}``. Unregistered data
    types and points missing required timing are skipped. Returns per-shape counts.
    """
    source_id = sync_control.ensure_default_source(conn, SOURCE_SLUG)
    tz = _resolve_profile_timezone(conn)
    counts = {"sample": 0, "interval": 0, "session": 0, "daily": 0}
    for response in responses:
        spec = METRIC_REGISTRY.get(str(response.get("dataType")))
        if spec is None:
            continue
        ctx = {
            "conn": conn,
            "source_id": source_id,
            "sync_batch_id": sync_batch_id,
            "data_type": str(response.get("dataType")),
            "user_id": response.get("userId"),
            "spec": spec,
            "tz": tz,
        }
        handler = _HANDLERS[spec["shape"]]
        for point in response.get("point") or []:
            if handler(ctx, point):
                counts[spec["shape"]] += 1
    return counts


def _persist_sample(ctx: dict[str, Any], point: dict[str, Any]) -> bool:
    spec = ctx["spec"]
    obs = _observation(point, spec)
    sample_time = _sample_time(obs)
    if not sample_time:
        return False
    name = point.get("name")
    key = _derive_key(ctx["data_type"], name, str(sample_time))
    value_number = _to_number(obs.get(spec.get("value_key")))
    _upsert(
        ctx["conn"],
        table="health_sample_observations",
        conflict_columns=("source_id", "observation_key"),
        row={
            "source_id": ctx["source_id"],
            "observation_key": key,
            "provider_user_id": ctx["user_id"],
            "provider_data_type": ctx["data_type"],
            "provider_point_name": name,
            "metric": spec["metric"],
            "sample_time": str(sample_time),
            "sample_time_unix": _unix(sample_time),
            "value_number": value_number,
            "metric_unit": spec.get("unit"),
            "provenance_json": _provenance_json(point),
        },
    )
    _link(ctx, "sample_observation", key, point, "health_sample_observations", key)
    return True


def _persist_interval(ctx: dict[str, Any], point: dict[str, Any]) -> bool:
    spec = ctx["spec"]
    obs = _observation(point, spec)
    start, end = _interval_bounds(obs)
    if not start or not end:
        return False
    start_unix = _unix(start)
    end_unix = _unix(end)
    # Skip out-of-order intervals (provider clock skew / mixed UTC offsets) rather
    # than letting CHECK(end_time >= start_time) or the *_unix ordering CHECK raise
    # and abort the rest of the batch. Guard BOTH the text and unix orderings the
    # table enforces, since a non-Z offset can make them disagree.
    if str(end) < str(start):
        return False
    if start_unix is not None and end_unix is not None and end_unix < start_unix:
        return False
    name = point.get("name")
    key = _derive_key(ctx["data_type"], name, str(start), str(end))
    value_number = _to_number(obs.get(spec.get("value_key")))
    _upsert(
        ctx["conn"],
        table="health_interval_observations",
        conflict_columns=("source_id", "observation_key"),
        row={
            "source_id": ctx["source_id"],
            "observation_key": key,
            "provider_user_id": ctx["user_id"],
            "provider_data_type": ctx["data_type"],
            "provider_point_name": name,
            "metric": spec["metric"],
            "start_time": str(start),
            "start_time_unix": start_unix,
            "end_time": str(end),
            "end_time_unix": end_unix,
            "value_number": value_number,
            "metric_unit": spec.get("unit"),
            "provenance_json": _provenance_json(point),
        },
    )
    _link(ctx, "interval_observation", key, point, "health_interval_observations", key)
    return True


def _persist_session(ctx: dict[str, Any], point: dict[str, Any]) -> bool:
    spec = ctx["spec"]
    obs = _observation(point, spec)
    start, end = _interval_bounds(obs)
    if not start:
        return False
    session_type = spec["session_type"]
    # v4 DataPoint.name is the globally-unique resource path; use it as the stable
    # session key, falling back to a derived key if a point lacks one.
    name = point.get("name")
    key = str(name) if name else _derive_key(session_type, str(start))
    _upsert(
        ctx["conn"],
        table="health_sessions",
        conflict_columns=("source_id", "session_key"),
        row={
            "source_id": ctx["source_id"],
            "session_key": key,
            "provider_user_id": ctx["user_id"],
            "provider_session_id": name,
            "session_type": session_type,
            "day": _local_day(start, ctx["tz"]),
            "start_time": str(start),
            "end_time": end,
            "duration_seconds": _duration_seconds(start, end),
            "provenance_json": _provenance_json(point),
        },
    )
    _link(ctx, "session", key, point, "health_sessions", key, sensitive=True)
    return True


def _persist_daily(ctx: dict[str, Any], point: dict[str, Any]) -> bool:
    spec = ctx["spec"]
    obs = _observation(point, spec)
    # v4 daily metrics carry a civil ``date`` ({year,month,day}); fall back to the
    # user-local day of any sample/interval instant present on the point.
    day = _civil_date(obs.get("date"))
    if not day:
        day = _local_day(_sample_time(obs) or _interval_bounds(obs)[0], ctx["tz"])
    if not day:
        return False
    metric = spec["metric"]
    # aggregation_kind is a property of the source data type (registry), not the
    # point: trusting a per-point value would let an unrecognized provider value
    # violate the CHECK (aborting the batch) and split idempotency across the PK.
    aggregation_kind = spec["aggregation_kind"]
    value_number = _to_number(obs.get(spec.get("value_key")))
    _upsert(
        ctx["conn"],
        table="daily_health_metrics",
        conflict_columns=("day", "source_id", "metric", "metric_component", "aggregation_kind"),
        row={
            "day": day,
            "source_id": ctx["source_id"],
            "metric": metric,
            "provider_data_type": ctx["data_type"],
            "aggregation_kind": aggregation_kind,
            "value_number": value_number,
            "metric_unit": spec.get("unit"),
            "provenance_json": _provenance_json(point),
        },
    )
    # daily_health_metrics has no single key column, so the lineage canonical_id is a
    # surrogate hashed from the full composite PK (day, source_id, metric,
    # metric_component='', aggregation_kind). To join record_lineage back to a daily
    # row, recompute this key from those five columns.
    canonical_id = _derive_key(day, ctx["source_id"], metric, "", aggregation_kind)
    _link(ctx, "daily_metric", canonical_id, point, "daily_health_metrics", canonical_id)
    return True


_HANDLERS = {
    "sample": _persist_sample,
    "interval": _persist_interval,
    "session": _persist_session,
    "daily": _persist_daily,
}


def _link(
    ctx: dict[str, Any],
    object_type: str,
    external_id: str,
    point: dict[str, Any],
    canonical_table: str,
    canonical_id: str,
    *,
    sensitive: bool = False,
) -> None:
    """Persist the raw point and link it to its canonical row."""
    raw_record_id = sync_control.persist_raw_record(
        ctx["conn"],
        source_id=ctx["source_id"],
        sync_batch_id=ctx["sync_batch_id"],
        provider=PROVIDER,
        object_type=object_type,
        external_id=external_id,
        payload=point,
        source_updated_at=None,
        privacy_tier="sensitive" if sensitive else "standard",
    )
    sync_control.attach_lineage(
        ctx["conn"],
        canonical_table=canonical_table,
        canonical_id=canonical_id,
        raw_record_id=raw_record_id,
    )


def _upsert(
    conn: sqlite3.Connection,
    *,
    table: str,
    conflict_columns: tuple[str, ...],
    row: dict[str, Any],
) -> None:
    columns = list(row)
    update_columns = [c for c in columns if c not in conflict_columns]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
    set_clause = f"{set_clause}, updated_at = CURRENT_TIMESTAMP" if set_clause else "updated_at = CURRENT_TIMESTAMP"
    conn.execute(
        f"""
        INSERT INTO {table}({", ".join(columns)})
        VALUES ({", ".join("?" for _ in columns)})
        ON CONFLICT({", ".join(conflict_columns)}) DO UPDATE SET
            {set_clause}
        """,
        [row[c] for c in columns],
    )


def _observation(point: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The v4 union sub-object for this data type (e.g. ``point['heartRate']``)."""
    obj = point.get(spec.get("field"))
    return obj if isinstance(obj, dict) else {}


def _sample_time(obs: dict[str, Any]) -> str | None:
    """The RFC3339 instant of a v4 sample (``<field>.sampleTime.physicalTime``)."""
    sample_time = obs.get("sampleTime")
    if isinstance(sample_time, dict):
        return sample_time.get("physicalTime")
    return None


def _interval_bounds(obs: dict[str, Any]) -> tuple[str | None, str | None]:
    """The ``(startTime, endTime)`` of a v4 interval/session (``<field>.interval``)."""
    interval = obs.get("interval")
    if isinstance(interval, dict):
        return interval.get("startTime"), interval.get("endTime")
    return None, None


def _civil_date(date: Any) -> str | None:
    """Format a v4 civil ``Date`` ({year,month,day}) as an ISO 'YYYY-MM-DD' string."""
    if not isinstance(date, dict):
        return None
    try:
        return _date(int(date["year"]), int(date["month"]), int(date["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _to_number(raw: Any) -> float | None:
    """Numeric value of a v4 measurement (JSON int64 string, int, or float).

    Returns 0.0 for a true zero, and None for a missing or non-numeric value so a
    malformed point persists with a NULL value rather than aborting the batch.
    """
    if raw is None or isinstance(raw, (dict, list, bool)):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _unix(instant: str | None) -> int | None:
    """Unix seconds for an RFC3339 instant -- the format-independent ordering guard."""
    moment = _parse_instant(instant)
    return int(moment.timestamp()) if moment is not None else None


def _provenance_json(point: dict[str, Any]) -> str:
    source = point.get("dataSource") or {}
    provenance = {
        key: source[key]
        for key in ("recordingMethod", "platform", "application", "device")
        if key in source
    }
    return json.dumps(provenance, sort_keys=True)


def _derive_key(*parts: str | None) -> str:
    """Deterministic, collision-safe, source-scoped key for the given parts.

    Google Health ``DataPoint.name`` is absent for most data types, so identity is a
    composite of data type / point name (when present) / temporal bounds. The parts
    are hashed rather than delimiter-joined so that a value containing the delimiter --
    a timezone offset like '+05:30', or a provider-supplied name -- can never make two
    distinct points collide onto the same key.
    """
    encoded = json.dumps(list(parts), separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _resolve_profile_timezone(conn: sqlite3.Connection) -> tzinfo:
    """Resolve the user's configured timezone to a tzinfo, defaulting to UTC.

    Reads the single-user ``health_profile`` ('default') row. Warns once -- rather
    than silently bucketing as UTC, which is the very off-by-one this guards against
    -- if a timezone is set but is not a known IANA zone. Single-user assumption: when
    multi-user support lands, resolve the zone per userId instead of the 'default' row.
    """
    row = conn.execute(
        "SELECT timezone FROM health_profile WHERE id = 'default'"
    ).fetchone()
    tz_name = row[0] if row else None
    if not tz_name:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown profile timezone %r; bucketing days as UTC", tz_name)
        return timezone.utc


def _parse_instant(value: str | None) -> datetime | None:
    """Parse an RFC3339 instant to an aware datetime (UTC if it carries no offset)."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _local_day(instant: str | None, tz: tzinfo) -> str | None:
    """Calendar day of an RFC3339 instant in the resolved timezone.

    Google Health returns UTC instants; bucketing on the raw UTC date would assign
    near-midnight data to the wrong local day for users far from UTC. Returns None for
    an empty/unparseable instant (the point is then skipped).
    """
    moment = _parse_instant(instant)
    if moment is None:
        return None
    return moment.astimezone(tz).date().isoformat()


def _duration_seconds(start: str | None, end: str | None) -> int | None:
    started = _parse_instant(start)
    ended = _parse_instant(end)
    if started is None or ended is None:
        return None
    return int((ended - started).total_seconds())


# =========================================================================== #
# Live Google Health API v4 fetch + sync
#
# REST surface verified against developers.google.com/health: getIdentity returns
# the caller's healthUserId; dataPoints.list returns {dataPoints[], nextPageToken}
# for a given dataType. The list response's dataPoints are the raw v4 DataPoints
# that persist_google_health consumes directly (grouped by dataType), so the fetch
# layer stays thin. (Paths are assembled from API_USER_PATH below.)
# =========================================================================== #

API_BASE_URL = "https://health.googleapis.com"
API_VERSION = "v4"
# The authenticated-user resource segment, e.g. .../v4/<API_USER_PATH>/identity.
API_USER_PATH = "users/me"
DEFAULT_LOOKBACK_DAYS = 7
# dataPoints.list caps pageSize at 10000 (the max; default 1440). With the time
# filter applied the per-run window is bounded, so a high cap mainly guards
# high-frequency streams (e.g. per-second heart rate) from truncation within a window.
PAGE_SIZE = 10000
MAX_PAGES = 100


class GoogleHealthAPIError(RuntimeError):
    pass


class GoogleHealthResponse:
    def __init__(self, status_code: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    def json(self) -> dict[str, Any]:
        return self.body


def http_request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> GoogleHealthResponse:
    """Low-level urllib GET returning a status + JSON body (injectable for tests)."""
    if params:
        url = f"{url}?{parse.urlencode(params)}"
    req = urllib_request.Request(url, method=method, headers=headers or {})
    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            body = _read_json_body(response)
            return GoogleHealthResponse(response.status, body, dict(response.headers.items()))
    except urllib_error.HTTPError as exc:
        # An HTTP status response (4xx/5xx) -- keep the real status + body so the
        # caller's `status >= 400` branch raises a per-data-type GoogleHealthAPIError.
        body = _read_json_body(exc)
        return GoogleHealthResponse(exc.code, body, dict(exc.headers.items()))
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        # Transport failure (DNS / connection refused / TLS / timeout). HTTPError is
        # a URLError subclass and is handled above; this branch is the bare-transport
        # case. Surface it as a GoogleHealthAPIError so the per-data-type and
        # best-effort-identity handlers degrade the run to partial instead of letting
        # a raw URLError abort the whole sync inside sync_guard.
        raise GoogleHealthAPIError(f"Google Health request transport error: {exc}") from exc


def _read_json_body(response: Any) -> dict[str, Any]:
    try:
        raw = response.read().decode("utf-8")
    except OSError as exc:
        raise GoogleHealthAPIError("Google Health response body could not be read.") from exc
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GoogleHealthAPIError("Google Health response body was not valid JSON.") from exc
    return parsed if isinstance(parsed, dict) else {}


def _status(response: Any) -> int:
    return int(getattr(response, "status_code", 200))


def _body(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    data = response.json()
    return data if isinstance(data, dict) else {}


def get_identity(*, access_token: str, request: Callable[..., Any]) -> dict[str, Any]:
    """``users.getIdentity`` -> the user's Google Health identity (healthUserId)."""
    url = f"{API_BASE_URL}/{API_VERSION}/{API_USER_PATH}/identity"
    response = request("GET", url, params={}, headers=_auth_headers(access_token))
    status = _status(response)
    if status >= 400:
        raise GoogleHealthAPIError(f"Google Health getIdentity failed with status {status}.")
    return _body(response)


def list_data_points(
    *,
    data_type: str,
    access_token: str,
    request: Callable[..., Any],
    time_filter: str | None = None,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """``users.dataTypes.dataPoints.list`` -> all DataPoints for one data type.

    ``time_filter`` is the v4 ``filter`` query parameter (AIP-160 time-range
    filtering; see :func:`_time_filter`). When unset the API returns the most recent
    points bounded only by ``page_size`` * ``max_pages``; callers should pass a
    bounded filter (``sync_google_health`` builds one from the requested window).
    """
    url = f"{API_BASE_URL}/{API_VERSION}/{API_USER_PATH}/dataTypes/{data_type}/dataPoints"
    headers = _auth_headers(access_token)
    points: list[dict[str, Any]] = []
    page_token: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"pageSize": page_size}
        if time_filter:
            params["filter"] = time_filter
        if page_token:
            params["pageToken"] = page_token
        response = request("GET", url, params=params, headers=headers)
        status = _status(response)
        if status >= 400:
            raise GoogleHealthAPIError(
                f"Google Health dataPoints.list({data_type}) failed with status {status}."
            )
        body = _body(response)
        page = body.get("dataPoints")
        if isinstance(page, list):
            points.extend(item for item in page if isinstance(item, dict))
        page_token = body.get("nextPageToken")
        pages += 1
        if not page_token or pages >= max_pages:
            return points


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


def _scope_rows() -> list[dict[str, str]]:
    return [
        {"scope_key": "activity_and_fitness", "scope_label": "Google Health activity & fitness (read)"},
        {"scope_key": "sleep", "scope_label": "Google Health sleep (read)"},
        {"scope_key": "health_metrics", "scope_label": "Google Health metrics & measurements (read)"},
        {"scope_key": "profile", "scope_label": "Google Health profile (read)"},
    ]


def _sync_window(
    today_iso: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[str, str]:
    end = _parse_window_date(end_date, "end_date") if end_date else _date.fromisoformat(today_iso)
    if start_date:
        start = _parse_window_date(start_date, "start_date")
    else:
        start = end - timedelta(days=max(lookback_days - 1, 0))
    if start > end:
        raise GoogleHealthAPIError("Google Health sync start_date must be on or before end_date.")
    return start.isoformat(), end.isoformat()


def _parse_window_date(value: str | None, field_name: str) -> _date:
    try:
        return _date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise GoogleHealthAPIError(
            f"Google Health sync {field_name} must be YYYY-MM-DD."
        ) from exc


def _time_filter(spec: dict[str, Any] | None, data_type: str, start_iso: str, end_iso: str) -> str | None:
    """Build the v4 ``filter`` expression bounding one data type to [start, end].

    Per developers.google.com/health (dataPoints.list filter docs) the grammar is
    AIP-160 with snake_case proto field paths and a half-open range, e.g.
    ``steps.interval.start_time >= "2023-11-24T00:00:00Z" AND steps.interval.start_time
    < "2023-11-25T00:00:00Z"``. The data-type prefix is the snake_case proto field
    (kebab id with '-' -> '_'); samples filter on ``sample_time.physical_time``,
    intervals/sessions on ``interval.start_time``, and daily metrics on ``date``
    (ISO calendar dates). ``end_iso`` is made exclusive by advancing one day.
    """
    if not spec:
        return None
    field = data_type.replace("-", "_")
    end_exclusive = (_date.fromisoformat(end_iso) + timedelta(days=1)).isoformat()
    shape = spec.get("shape")
    if shape == "sample":
        path = f"{field}.sample_time.physical_time"
        return f'{path} >= "{start_iso}T00:00:00Z" AND {path} < "{end_exclusive}T00:00:00Z"'
    if shape in ("interval", "session"):
        path = f"{field}.interval.start_time"
        return f'{path} >= "{start_iso}T00:00:00Z" AND {path} < "{end_exclusive}T00:00:00Z"'
    if shape == "daily":
        path = f"{field}.date"
        return f'{path} >= "{start_iso}" AND {path} < "{end_exclusive}"'
    return None


def sync_google_health(
    *,
    request: Callable[..., Any] | None = None,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]
    | None = None,
    today: datetime | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    data_types: list[str] | None = None,
    time_filter: str | None = None,
) -> dict[str, Any]:
    """Fetch Google Health API v4 data and persist it into the canonical tables.

    Mirrors ``oura.sync_oura``: refresh the token, fetch each registered data type
    via ``dataPoints.list`` (plus ``getIdentity`` for the user id), feed the raw v4
    DataPoints to :func:`persist_google_health`, and record the sync run / batch /
    cursor / errors. A per-data-type failure degrades the run to ``partial``; only a
    total failure (every data type errored) raises.
    """
    sync_started = time.monotonic()
    clock = today or datetime.now(timezone.utc)
    explicit_window = any(v not in (None, "") for v in (start_date, end_date))
    window_start, window_end = _sync_window(
        clock.date().isoformat(),
        start_date=start_date,
        end_date=end_date,
        lookback_days=lookback_days,
    )

    token = google_health_auth.load_token()
    if oauth_token.token_expired(token):
        client_id, client_secret = google_health_auth.load_client_credentials()
        token = google_health_auth.refresh_access_token(
            client_id=client_id,
            client_secret=client_secret,
            http_post=http_post or google_health_auth.http_post_form,
        )
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise google_health_auth.GoogleHealthNotConnected(
            "Google Health access token missing; re-run `hermes health connect-google-health`."
        )

    requester = request or http_request
    registry = data_types or list(METRIC_REGISTRY)

    with store.sync_guard("google_health"):
        with store.connect() as conn:
            source_id = sync_control.ensure_default_source(
                conn, SOURCE_SLUG, metadata={"connection_name": "Google Health"}
            )
            sync_control.ensure_scope_rows(conn, source_id=source_id, scopes=_scope_rows())
            sync_run_id = sync_control.start_sync_run(
                conn,
                source_id=source_id,
                trigger_kind="backfill" if explicit_window else "manual",
                request_start=window_start,
                request_end=window_end,
            )

        # getIdentity is best-effort: dataPoints.list works against users/me without it.
        user_id: str | None = None
        identity_error: str | None = None
        try:
            identity = get_identity(access_token=access_token, request=requester)
            user_id = identity.get("healthUserId") or identity.get("name")
        except GoogleHealthAPIError as exc:
            identity_error = str(exc)

        responses: list[dict[str, Any]] = []
        endpoint_errors: dict[str, str] = {}
        endpoint_counts: dict[str, int] = {}
        for data_type in registry:
            # An explicit time_filter override wins; otherwise bound the fetch to the
            # computed window via the documented v4 filter so the run/cursor metadata
            # reflects what was actually fetched (no silent most-recent-only pull).
            dt_filter = (
                time_filter
                if time_filter is not None
                else _time_filter(
                    METRIC_REGISTRY.get(data_type), data_type, window_start, window_end
                )
            )
            try:
                points = list_data_points(
                    data_type=data_type,
                    access_token=access_token,
                    request=requester,
                    time_filter=dt_filter,
                )
            except GoogleHealthAPIError as exc:
                endpoint_errors[data_type] = str(exc)
                continue
            responses.append({"dataType": data_type, "userId": user_id, "point": points})
            endpoint_counts[data_type] = len(points)

        records_seen = sum(endpoint_counts.values())
        successful = [dt for dt in registry if dt not in endpoint_errors]
        all_failed = not successful

        with store.connect() as conn:
            batch_id = sync_control.start_sync_batch(
                conn,
                sync_run_id=sync_run_id,
                object_type="dataPoints",
                window_start=window_start,
                window_end=window_end,
            )
            counts = persist_google_health(conn, responses=responses, sync_batch_id=batch_id)
            records_written = sum(counts.values())
            for data_type, message in endpoint_errors.items():
                sync_control.record_sync_error(
                    conn,
                    source_slug=SOURCE_SLUG,
                    sync_run_id=sync_run_id,
                    sync_batch_id=batch_id,
                    object_type=data_type,
                    error_code="google_health_api_error",
                    error_message=message,
                    retryable=True,
                )
            if identity_error:
                sync_control.record_sync_error(
                    conn,
                    source_slug=SOURCE_SLUG,
                    sync_run_id=sync_run_id,
                    sync_batch_id=batch_id,
                    object_type="identity",
                    error_code="google_health_api_error",
                    error_message=identity_error,
                    retryable=True,
                )
            sync_control.finish_sync_batch(
                conn,
                sync_batch_id=batch_id,
                status="error" if all_failed else ("partial" if endpoint_errors else "ok"),
                cursor_after=window_end,
                records_seen=records_seen,
                records_written=records_written,
            )
            error_count = len(endpoint_errors) + (1 if identity_error else 0)
            if all_failed:
                sync_control.finish_sync_run(
                    conn,
                    sync_run_id=sync_run_id,
                    status="error",
                    records_seen=records_seen,
                    records_written=records_written,
                    batch_count=1,
                    error_count=error_count,
                )
                sync_control.mark_source_synced(conn, source_slug=SOURCE_SLUG, status="partial")
            else:
                sync_control.upsert_cursor(
                    conn,
                    source_id=source_id,
                    object_type="dataPoints",
                    cursor_kind="date_window_end",
                    cursor_value=window_end,
                    window_start=window_start,
                    window_end=window_end,
                )
                sync_control.finish_sync_run(
                    conn,
                    sync_run_id=sync_run_id,
                    status="partial" if endpoint_errors else "ok",
                    records_seen=records_seen,
                    records_written=records_written,
                    batch_count=1,
                    error_count=error_count,
                )
                sync_control.mark_source_synced(
                    conn,
                    source_slug=SOURCE_SLUG,
                    status="partial" if endpoint_errors else "connected",
                )

        if all_failed:
            first_error = next(iter(endpoint_errors.values()), identity_error or "unknown error")
            raise GoogleHealthAPIError(
                f"Google Health sync failed for all data types: {first_error}"
            )

    return {
        "ok": True,
        "start_date": window_start,
        "end_date": window_end,
        "user_id": user_id,
        "counts": counts,
        "endpoint_counts": endpoint_counts,
        "endpoint_errors": endpoint_errors,
        "identity_error": identity_error,
        "duration_ms": max(0, int(round((time.monotonic() - sync_started) * 1000))),
    }
