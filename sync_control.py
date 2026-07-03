from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any


LEGACY_SOURCE_STATUS = {
    "ok": "connected",
    "running": "partial",
    "partial": "partial",
    "error": "partial",
    "never": "disconnected",
}

SECRET_LIKE_KEYS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization",
    "auth_code",
    "password",
    "token",
    "secret",
}

DEFAULT_SOURCE_DEFINITIONS = {
    "oura": {
        "provider": "oura",
        "connection_name": "Primary Oura account",
        "status": "disconnected",
        "sync_mode": "pull",
    },
    "google_calendar": {
        "provider": "google_workspace",
        "connection_name": "Google Workspace shared auth",
        "status": "disconnected",
        "sync_mode": "pull",
    },
    "gmail": {
        "provider": "google_workspace",
        "connection_name": "Google Workspace shared auth",
        "status": "disconnected",
        "sync_mode": "pull",
    },
    "manual_food": {
        "provider": "manual",
        "connection_name": "Manual food logging",
        "status": "manual",
        "sync_mode": "manual",
    },
    "google_health": {
        "provider": "google_health",
        "connection_name": "Google Health",
        "status": "disconnected",
        "sync_mode": "pull",
    },
}


def ensure_source(
    conn: sqlite3.Connection,
    *,
    source_slug: str,
    provider: str,
    connection_name: str,
    status: str,
    sync_mode: str,
    metadata: dict[str, Any] | None = None,
    account_external_id: str | None = None,
) -> str:
    metadata_json = _metadata_json(metadata or {})
    now = _now()
    row = conn.execute(
        "SELECT source_id FROM health_sources WHERE source_slug = ?",
        (source_slug,),
    ).fetchone()
    if row:
        conn.execute(
            """
            UPDATE health_sources
            SET provider = ?, connection_name = ?, account_external_id = ?,
                status = ?, sync_mode = ?, metadata_json = ?, updated_at = ?
            WHERE source_slug = ?
            """,
            (
                provider,
                connection_name,
                account_external_id,
                status,
                sync_mode,
                metadata_json,
                now,
                source_slug,
            ),
        )
        return str(row[0])

    source_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO health_sources(
            source_id, source_slug, provider, connection_name, account_external_id,
            status, sync_mode, connected_at, last_synced_at, metadata_json,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            source_id,
            source_slug,
            provider,
            connection_name,
            account_external_id,
            status,
            sync_mode,
            now,
            metadata_json,
            now,
            now,
        ),
    )
    return source_id


def ensure_default_source(
    conn: sqlite3.Connection,
    source_slug: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    row = conn.execute(
        "SELECT source_id FROM health_sources WHERE source_slug = ?",
        (source_slug,),
    ).fetchone()
    if row:
        return str(row[0])
    definition = DEFAULT_SOURCE_DEFINITIONS[source_slug]
    return ensure_source(
        conn,
        source_slug=source_slug,
        provider=str(definition["provider"]),
        connection_name=str(definition["connection_name"]),
        status=str(definition["status"]),
        sync_mode=str(definition["sync_mode"]),
        metadata=metadata,
    )


def ensure_scope_rows(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    scopes: list[dict[str, str]],
    granted_at: str | None = None,
) -> None:
    now = _now()
    granted = granted_at or now
    for scope in scopes:
        scope_key = scope["scope_key"]
        scope_label = scope["scope_label"]
        metadata_json = _metadata_json(scope.get("metadata", {}) if isinstance(scope, dict) else {})
        conn.execute(
            """
            INSERT INTO source_scopes(
                scope_id, source_id, scope_key, scope_label, granted_at, revoked_at,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(source_id, scope_key) DO UPDATE SET
                scope_label = excluded.scope_label,
                granted_at = COALESCE(source_scopes.granted_at, excluded.granted_at),
                revoked_at = NULL,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()),
                source_id,
                scope_key,
                scope_label,
                granted,
                metadata_json,
                now,
                now,
            ),
        )


def start_sync_run(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    trigger_kind: str,
    request_start: str | None,
    request_end: str | None,
    metadata: dict[str, Any] | None = None,
) -> str:
    sync_run_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sync_runs(
            sync_run_id, source_id, trigger_kind, request_start, request_end,
            status, started_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (
            sync_run_id,
            source_id,
            trigger_kind,
            request_start,
            request_end,
            _now(),
            _metadata_json(metadata or {}),
        ),
    )
    return sync_run_id


def finish_sync_run(
    conn: sqlite3.Connection,
    *,
    sync_run_id: str,
    status: str,
    records_seen: int,
    records_written: int,
    batch_count: int = 0,
    error_count: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE sync_runs
        SET status = ?, records_seen = ?, records_written = ?, batch_count = ?,
            error_count = ?, finished_at = ?
        WHERE sync_run_id = ?
        """,
        (
            status,
            records_seen,
            records_written,
            batch_count,
            error_count,
            _now(),
            sync_run_id,
        ),
    )


def start_sync_batch(
    conn: sqlite3.Connection,
    *,
    sync_run_id: str,
    object_type: str,
    window_start: str | None,
    window_end: str | None,
    cursor_before: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    sync_batch_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sync_batches(
            sync_batch_id, sync_run_id, object_type, status, window_start,
            window_end, cursor_before, started_at, metadata_json
        )
        VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)
        """,
        (
            sync_batch_id,
            sync_run_id,
            object_type,
            window_start,
            window_end,
            cursor_before,
            _now(),
            _metadata_json(metadata or {}),
        ),
    )
    return sync_batch_id


def finish_sync_batch(
    conn: sqlite3.Connection,
    *,
    sync_batch_id: str,
    status: str,
    cursor_after: str | None,
    records_seen: int,
    records_written: int,
) -> None:
    conn.execute(
        """
        UPDATE sync_batches
        SET status = ?, cursor_after = ?, records_seen = ?, records_written = ?,
            finished_at = ?
        WHERE sync_batch_id = ?
        """,
        (status, cursor_after, records_seen, records_written, _now(), sync_batch_id),
    )


def upsert_cursor(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    object_type: str,
    cursor_kind: str,
    cursor_value: str | None,
    window_start: str | None,
    window_end: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO sync_cursors(
            sync_cursor_id, source_id, object_type, cursor_kind, cursor_value,
            window_start, window_end, updated_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, object_type, cursor_kind) DO UPDATE SET
            cursor_value = excluded.cursor_value,
            window_start = excluded.window_start,
            window_end = excluded.window_end,
            updated_at = excluded.updated_at,
            metadata_json = excluded.metadata_json
        """,
        (
            str(uuid.uuid4()),
            source_id,
            object_type,
            cursor_kind,
            cursor_value,
            window_start,
            window_end,
            now,
            _metadata_json(metadata or {}),
        ),
    )


def update_cursor(
    conn: sqlite3.Connection,
    *,
    source_slug: str,
    object_type: str,
    cursor_kind: str,
    cursor_value: str | None,
    window_start: str | None,
    window_end: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    row = conn.execute(
        "SELECT source_id FROM health_sources WHERE source_slug = ?",
        (source_slug,),
    ).fetchone()
    if row is None:
        source_id = ensure_default_source(conn, source_slug)
    else:
        source_id = str(row[0])
    upsert_cursor(
        conn,
        source_id=source_id,
        object_type=object_type,
        cursor_kind=cursor_kind,
        cursor_value=cursor_value,
        window_start=window_start,
        window_end=window_end,
        metadata=metadata,
    )


def record_sync_error(
    conn: sqlite3.Connection,
    *,
    source_slug: str,
    sync_run_id: str | None,
    sync_batch_id: str | None,
    object_type: str | None,
    error_code: str | None,
    error_message: str,
    retryable: bool,
    metadata: dict[str, Any] | None = None,
) -> str:
    source_row = conn.execute(
        "SELECT source_id FROM health_sources WHERE source_slug = ?",
        (source_slug,),
    ).fetchone()
    source_id = str(source_row[0]) if source_row else ensure_default_source(conn, source_slug)
    sync_error_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO sync_errors(
            sync_error_id, source_id, sync_run_id, sync_batch_id, object_type,
            error_code, error_message, retryable, created_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sync_error_id,
            source_id,
            sync_run_id,
            sync_batch_id,
            object_type,
            error_code,
            error_message,
            1 if retryable else 0,
            _now(),
            _metadata_json(metadata or {}),
        ),
    )
    return sync_error_id


def persist_raw_record(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    sync_batch_id: str | None,
    provider: str,
    object_type: str,
    external_id: str,
    payload: dict[str, Any],
    source_updated_at: str | None,
    privacy_tier: str,
    is_redacted: bool = False,
) -> str:
    payload_json = _payload_json(payload)
    content_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        SELECT raw_record_id
        FROM raw_records
        WHERE source_id = ? AND object_type = ? AND external_id = ? AND source_record_hash = ?
        """,
        (source_id, object_type, external_id, content_hash),
    ).fetchone()
    if row:
        return str(row[0])

    raw_record_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO raw_records(
            raw_record_id, source_id, sync_batch_id, provider, object_type, external_id,
            payload_json, payload_version, source_record_hash, source_updated_at,
            extracted_at, privacy_tier, is_redacted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'v1', ?, ?, ?, ?, ?)
        """,
        (
            raw_record_id,
            source_id,
            sync_batch_id,
            provider,
            object_type,
            external_id,
            payload_json,
            content_hash,
            source_updated_at,
            _now(),
            privacy_tier,
            1 if is_redacted else 0,
        ),
    )
    return raw_record_id


def attach_lineage(
    conn: sqlite3.Connection,
    *,
    canonical_table: str,
    canonical_id: str,
    raw_record_id: str,
    lineage_role: str = "source",
) -> str:
    row = conn.execute(
        """
        SELECT lineage_id
        FROM record_lineage
        WHERE canonical_table = ? AND canonical_id = ? AND raw_record_id = ?
        """,
        (canonical_table, canonical_id, raw_record_id),
    ).fetchone()
    if row:
        return str(row[0])
    lineage_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO record_lineage(
            lineage_id, canonical_table, canonical_id, raw_record_id,
            normalization_version, lineage_role, created_at
        )
        VALUES (?, ?, ?, ?, 'v1', ?, ?)
        """,
        (
            lineage_id,
            canonical_table,
            canonical_id,
            raw_record_id,
            lineage_role,
            _now(),
        ),
    )
    return lineage_id


def ensure_default_schedules(
    conn: sqlite3.Connection,
    *,
    cadence_minutes: int,
    schedule_key: str,
    sources: tuple[str, ...],
) -> None:
    for source_slug in sources:
        source_id = ensure_default_source(
            conn,
            source_slug,
            metadata={"schedule_seeded": True},
        )
        if source_slug == "manual_food":
            _upsert_schedule(
                conn,
                source_id=source_id,
                schedule_key="manual",
                trigger_kind="manual",
                cadence_minutes=None,
                metadata={},
            )
        else:
            _upsert_schedule(
                conn,
                source_id=source_id,
                schedule_key=schedule_key,
                trigger_kind="cron",
                cadence_minutes=cadence_minutes,
                metadata={"cron_schedule": "every 6h"},
            )


def backfill_legacy_sources(conn: sqlite3.Connection) -> None:
    sync_rows = {
        provider: (last_sync_date, last_status)
        for provider, last_sync_date, last_status in conn.execute(
            "SELECT provider, last_sync_date, last_status FROM sync_state"
        )
    }

    if "oura" in sync_rows:
        last_sync_date, last_status = sync_rows["oura"]
        source_id = ensure_source(
            conn,
            source_slug="oura",
            provider="oura",
            connection_name="Primary Oura account",
            status=LEGACY_SOURCE_STATUS.get(str(last_status), "partial"),
            sync_mode="pull",
            metadata={"migrated_from_sync_state": True},
        )
        if last_sync_date:
            upsert_cursor(
                conn,
                source_id=source_id,
                object_type="daily",
                cursor_kind="date_window_end",
                cursor_value=str(last_sync_date),
                window_start=None,
                window_end=str(last_sync_date),
            )

    if "google_workspace" in sync_rows:
        last_sync_date, last_status = sync_rows["google_workspace"]
        for source_slug in ("google_calendar", "gmail"):
            source_id = ensure_source(
                conn,
                source_slug=source_slug,
                provider="google_workspace",
                connection_name="Google Workspace shared auth",
                status=LEGACY_SOURCE_STATUS.get(str(last_status), "partial"),
                sync_mode="pull",
                metadata={"migrated_from_sync_state": True, "shared_auth": True},
            )
            if last_sync_date:
                upsert_cursor(
                    conn,
                    source_id=source_id,
                    object_type="daily",
                    cursor_kind="date_window_end",
                    cursor_value=str(last_sync_date),
                    window_start=None,
                    window_end=str(last_sync_date),
                )

    food_row = conn.execute("SELECT 1 FROM food_logs LIMIT 1").fetchone()
    if food_row:
        ensure_source(
            conn,
            source_slug="manual_food",
            provider="manual",
            connection_name="Manual food logging",
            status="manual",
            sync_mode="manual",
            metadata={"migrated_from_existing_rows": True},
        )


def mark_source_synced(
    conn: sqlite3.Connection,
    *,
    source_slug: str,
    status: str,
    synced_at: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE health_sources
        SET status = ?, last_synced_at = ?, updated_at = ?
        WHERE source_slug = ?
        """,
        (status, synced_at or _now(), _now(), source_slug),
    )


def _upsert_schedule(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    schedule_key: str,
    trigger_kind: str,
    cadence_minutes: int | None,
    metadata: dict[str, Any],
) -> None:
    now = _now()
    conn.execute(
        """
        INSERT INTO sync_schedules(
            sync_schedule_id, source_id, schedule_key, trigger_kind,
            cadence_minutes, enabled, last_registered_at, metadata_json
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(source_id, schedule_key) DO UPDATE SET
            trigger_kind = excluded.trigger_kind,
            cadence_minutes = excluded.cadence_minutes,
            enabled = excluded.enabled,
            last_registered_at = excluded.last_registered_at,
            metadata_json = excluded.metadata_json
        """,
        (
            str(uuid.uuid4()),
            source_id,
            schedule_key,
            trigger_kind,
            cadence_minutes,
            now,
            _metadata_json(metadata),
        ),
    )


def _payload_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _metadata_json(metadata: dict[str, Any]) -> str:
    _reject_secret_like_keys(metadata)
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"), default=str)


def _reject_secret_like_keys(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).lower()
            if key_text in SECRET_LIKE_KEYS or key_text.endswith("_token"):
                dotted = f"{path}.{key}" if path else str(key)
                raise ValueError(f"Refusing to persist secret-like key: {dotted}")
            _reject_secret_like_keys(child, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_like_keys(child, f"{path}[{index}]")


def _now() -> str:
    return datetime.now(UTC).isoformat()
