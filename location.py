from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import store, sync_control


SPOOL_SCHEMA_VERSION = "v1"
SECONDS_PER_MINUTE = 60.0
SOURCE_SLUG = "local_location"
PROVIDER = "local"
OBJECT_TYPE = "location_visit"
CONNECTION_NAME = "Local location clustering"
LOCATION_SPOOL_NAME = "location_spool.jsonl"
PRIVACY_FLAG = "precise_location_opt_in"


class LocationNotConnected(RuntimeError):
    pass


class LocationSyncError(RuntimeError):
    pass


def opt_in_enabled(*, conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT privacy_json FROM health_profile WHERE id = 'default'"
    ).fetchone()
    if row is None or row[0] is None:
        return False
    privacy = json.loads(row[0])
    return privacy.get(PRIVACY_FLAG) is True


def read_spool_rows(*, spool_path: Path) -> list[dict]:
    if not spool_path.exists():
        raise LocationNotConnected(str(spool_path))
    rows: list[dict] = []
    for line in spool_path.read_text().splitlines():
        text = line.strip()
        if not text:
            continue
        rows.append(json.loads(text))
    return rows


def write_visits(*, conn: sqlite3.Connection, rows: list[dict]) -> dict:
    visits_written = 0
    visits_skipped = 0
    for row in rows:
        if row.get("schema") != SPOOL_SCHEMA_VERSION or not row.get("departure_at"):
            visits_skipped += 1
            continue
        location_id = row["location_id"]
        arrival_at = row["arrival_at"]
        departure_at = row["departure_at"]
        conn.execute(
            """
            INSERT INTO location_clusters(
                location_id, centroid_lat_coarse, centroid_lng_coarse, radius_meters,
                visit_count, first_seen, last_seen
            )
            VALUES (?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(location_id) DO UPDATE SET
                centroid_lat_coarse = excluded.centroid_lat_coarse,
                centroid_lng_coarse = excluded.centroid_lng_coarse,
                radius_meters = excluded.radius_meters,
                first_seen = MIN(location_clusters.first_seen, excluded.first_seen),
                last_seen = MAX(location_clusters.last_seen, excluded.last_seen),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                location_id,
                row.get("centroid_lat_coarse"),
                row.get("centroid_lng_coarse"),
                row.get("radius_meters"),
                arrival_at,
                departure_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO location_visits(
                visit_id, location_id, day, arrival_at, departure_at, duration_minutes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(visit_id) DO UPDATE SET
                location_id = excluded.location_id,
                day = excluded.day,
                arrival_at = excluded.arrival_at,
                departure_at = excluded.departure_at,
                duration_minutes = excluded.duration_minutes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                f"{location_id}:{arrival_at}",
                location_id,
                arrival_at[:10],
                arrival_at,
                departure_at,
                _duration_minutes(arrival_at, departure_at),
            ),
        )
        visits_written += 1
    return {"visits_written": visits_written, "visits_skipped": visits_skipped}


def rebuild_cluster_counts(*, conn: sqlite3.Connection, location_ids: list[str]) -> dict:
    for location_id in location_ids:
        conn.execute(
            """
            UPDATE location_clusters
            SET visit_count = (
                SELECT COUNT(*) FROM location_visits WHERE location_id = ?
            ),
            updated_at = CURRENT_TIMESTAMP
            WHERE location_id = ?
            """,
            (location_id, location_id),
        )
    return {"clusters_rebuilt": len(location_ids)}


def rebuild_location_audio_daily(
    *, conn: sqlite3.Connection, pairs: list[tuple[str, str]]
) -> dict:
    written = 0
    for location_id, day in pairs:
        cursor = conn.execute(
            """
            INSERT INTO location_audio_daily(
                location_id, day, avg_noise_db, quiet_minutes, visit_minutes
            )
            SELECT
                ?,
                w.day,
                SUM(w.avg_noise_db * w.duration_seconds) / SUM(w.duration_seconds),
                SUM(w.quiet_seconds) / ?,
                SUM(w.duration_seconds) / ?
            FROM audio_windows w
            WHERE w.day = ?
              AND EXISTS (
                SELECT 1 FROM location_visits v
                WHERE v.location_id = ? AND v.day = w.day
                  AND w.window_start < v.departure_at
                  AND w.window_end > v.arrival_at
              )
            GROUP BY w.day
            ON CONFLICT(location_id, day) DO UPDATE SET
                avg_noise_db = excluded.avg_noise_db,
                quiet_minutes = excluded.quiet_minutes,
                visit_minutes = excluded.visit_minutes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (location_id, SECONDS_PER_MINUTE, SECONDS_PER_MINUTE, day, location_id),
        )
        written += cursor.rowcount if cursor.rowcount > 0 else 0
    return {"location_days_written": written}


def connect_location(*, spool_path: Path | None = None) -> dict:
    path = spool_path or _default_spool_path()
    with store.connect() as conn:
        if not opt_in_enabled(conn=conn):
            return {"status": "skipped", "reason": f"{PRIVACY_FLAG} is False"}
        _ensure_location_source(conn)
    return {
        "status": "connected",
        "source_slug": SOURCE_SLUG,
        "spool_path": str(path),
        "spool_present": path.exists(),
    }


def sync_location(*, spool_path: Path | None = None, trigger_kind: str = "manual") -> dict:
    path = spool_path or _default_spool_path()
    with store.connect() as conn:
        if not opt_in_enabled(conn=conn):
            return {"status": "skipped", "reason": f"{PRIVACY_FLAG} is False"}
    if not path.exists():
        raise LocationNotConnected(str(path))
    rows = read_spool_rows(spool_path=path)
    with store.sync_guard(SOURCE_SLUG):
        with store.connect() as conn:
            source_id = _ensure_location_source(conn)
            sync_run_id = sync_control.start_sync_run(
                conn,
                source_id=source_id,
                trigger_kind=trigger_kind,
                request_start=rows[0]["arrival_at"] if rows else None,
                request_end=rows[-1]["departure_at"] if rows else None,
            )
        try:
            result = _persist_location(
                source_id=source_id, sync_run_id=sync_run_id, rows=rows
            )
        except Exception as error:
            with store.connect() as conn:
                sync_control.record_sync_error(
                    conn,
                    source_slug=SOURCE_SLUG,
                    sync_run_id=sync_run_id,
                    sync_batch_id=None,
                    object_type=OBJECT_TYPE,
                    error_code=type(error).__name__,
                    error_message=str(error),
                    retryable=False,
                )
                sync_control.finish_sync_run(
                    conn,
                    sync_run_id=sync_run_id,
                    status="error",
                    records_seen=len(rows),
                    records_written=0,
                    batch_count=1,
                    error_count=1,
                )
                sync_control.mark_source_synced(
                    conn, source_slug=SOURCE_SLUG, status="partial"
                )
            raise LocationSyncError(str(error)) from error
    return {
        "status": "ok",
        "visits": len(rows),
        "locations": result["locations"],
    }


def _persist_location(*, source_id: str, sync_run_id: str, rows: list[dict]) -> dict:
    with store.connect() as conn:
        batch_id = sync_control.start_sync_batch(
            conn,
            sync_run_id=sync_run_id,
            object_type=OBJECT_TYPE,
            window_start=rows[0]["arrival_at"] if rows else None,
            window_end=rows[-1]["departure_at"] if rows else None,
        )
        location_record_ids: dict[str, str] = {}
        for row in rows:
            raw_record_id = sync_control.persist_raw_record(
                conn,
                source_id=source_id,
                sync_batch_id=batch_id,
                provider=PROVIDER,
                object_type=OBJECT_TYPE,
                external_id=f"{row['location_id']}:{row['arrival_at']}",
                payload=row,
                source_updated_at=row.get("departure_at"),
                privacy_tier="standard",
                is_redacted=False,
            )
            location_record_ids.setdefault(row["location_id"], raw_record_id)
        write_visits(conn=conn, rows=rows)
        locations = sorted(location_record_ids)
        rebuild_cluster_counts(conn=conn, location_ids=locations)
        pairs = sorted({(row["location_id"], row["arrival_at"][:10]) for row in rows})
        rebuild_location_audio_daily(conn=conn, pairs=pairs)
        for location_id, raw_record_id in location_record_ids.items():
            sync_control.attach_lineage(
                conn,
                canonical_table="location_clusters",
                canonical_id=location_id,
                raw_record_id=raw_record_id,
            )
        sync_control.finish_sync_batch(
            conn,
            sync_batch_id=batch_id,
            status="ok",
            cursor_after=rows[-1]["departure_at"] if rows else None,
            records_seen=len(rows),
            records_written=len(rows),
        )
        sync_control.finish_sync_run(
            conn,
            sync_run_id=sync_run_id,
            status="ok",
            records_seen=len(rows),
            records_written=len(rows),
            batch_count=1,
        )
        sync_control.mark_source_synced(
            conn, source_slug=SOURCE_SLUG, status="connected"
        )
    return {"locations": locations}


def _ensure_location_source(conn: sqlite3.Connection) -> str:
    source_id = sync_control.ensure_source(
        conn,
        source_slug=SOURCE_SLUG,
        provider=PROVIDER,
        connection_name=CONNECTION_NAME,
        status="connected",
        sync_mode="pull",
    )
    sync_control.ensure_scope_rows(
        conn,
        source_id=source_id,
        scopes=[{"scope_key": OBJECT_TYPE, "scope_label": "Location visits"}],
    )
    return source_id


def _duration_minutes(arrival_at: str, departure_at: str) -> float:
    start = datetime.fromisoformat(arrival_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(departure_at.replace("Z", "+00:00"))
    return (end - start).total_seconds() / SECONDS_PER_MINUTE


def _default_spool_path() -> Path:
    return store.hermes_home() / LOCATION_SPOOL_NAME
