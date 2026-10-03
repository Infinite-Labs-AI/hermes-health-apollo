from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import store, sync_control


SUPPORTED_SPOOL_SCHEMAS = ("v1", "v2")
DEFAULT_SILENCE_THRESHOLD_DB = -50.0
SECONDS_PER_MINUTE = 60.0
SOURCE_SLUG = "local_audio"
PROVIDER = "local"
OBJECT_TYPE = "audio_window"
CONNECTION_NAME = "Local ambient audio"
AUDIO_SPOOL_NAME = "audio_spool.jsonl"
RETENTION_DAYS = 90
CURSOR_KIND = "byte_offset"


class AudioNotConnected(RuntimeError):
    pass


class AudioSyncError(RuntimeError):
    pass


def read_new_spool_rows(
    *, spool_path: Path, offset: int, fingerprint: str | None
) -> dict:
    if not spool_path.exists():
        raise AudioNotConnected(str(spool_path))
    data = spool_path.read_bytes()
    current_fingerprint = _fingerprint(data)
    start = offset
    reset = False
    if offset > len(data) or (
        fingerprint is not None and current_fingerprint != fingerprint
    ):
        start = 0
        reset = offset != 0
    chunk = data[start:]
    last_newline = chunk.rfind(b"\n")
    if last_newline == -1:
        return {
            "rows": [],
            "offset": start,
            "fingerprint": current_fingerprint,
            "reset": reset,
        }
    complete = chunk[: last_newline + 1]
    rows = [
        json.loads(line)
        for line in complete.decode("utf-8").splitlines()
        if line.strip()
    ]
    return {
        "rows": rows,
        "offset": start + len(complete),
        "fingerprint": current_fingerprint,
        "reset": reset,
    }


def _fingerprint(data: bytes) -> str:
    newline = data.find(b"\n")
    head = data if newline == -1 else data[: newline + 1]
    return hashlib.sha256(head).hexdigest()


def write_windows(
    *, conn: sqlite3.Connection, rows: list[dict], silence_threshold_db: float
) -> dict:
    windows_written = 0
    windows_skipped = 0
    for row in rows:
        if row.get("schema") not in SUPPORTED_SPOOL_SCHEMAS:
            windows_skipped += 1
            continue
        duration = _duration_seconds(row)
        if duration <= 0:
            windows_skipped += 1
            continue
        histogram = row.get("level_hist_db")
        if histogram is not None:
            quiet_seconds = _quiet_seconds_at(histogram, silence_threshold_db)
            level_hist_json = json.dumps(histogram, sort_keys=True)
        else:
            quiet_seconds = row.get("quiet_seconds", 0.0)
            level_hist_json = None
        conn.execute(
            """
            INSERT INTO audio_windows(
                window_start, window_end, day, duration_seconds,
                avg_noise_db, peak_noise_db, quiet_seconds, level_hist_db
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_start) DO UPDATE SET
                window_end = excluded.window_end,
                day = excluded.day,
                duration_seconds = excluded.duration_seconds,
                avg_noise_db = excluded.avg_noise_db,
                peak_noise_db = excluded.peak_noise_db,
                quiet_seconds = excluded.quiet_seconds,
                level_hist_db = excluded.level_hist_db,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                row["window_start"],
                row["window_end"],
                row["window_start"][:10],
                duration,
                row["avg_noise_db"],
                row["peak_noise_db"],
                quiet_seconds,
                level_hist_json,
            ),
        )
        windows_written += 1
    return {"windows_written": windows_written, "windows_skipped": windows_skipped}


def _quiet_seconds_at(level_hist_db: dict, threshold_db: float) -> float:
    return float(
        sum(
            count
            for db, count in level_hist_db.items()
            if float(db) <= threshold_db
        )
    )


def get_silence_threshold(*, conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT silence_threshold_db FROM audio_settings WHERE id = 1"
    ).fetchone()
    return float(row[0]) if row is not None else DEFAULT_SILENCE_THRESHOLD_DB


def set_silence_threshold(*, threshold_db: float) -> dict:
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO audio_settings(id, silence_threshold_db)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET
                silence_threshold_db = excluded.silence_threshold_db,
                updated_at = CURRENT_TIMESTAMP
            """,
            (threshold_db,),
        )
        days = _rethreshold_windows(conn=conn, threshold_db=threshold_db)
        rebuild_daily(conn=conn, days=days)
    return {"silence_threshold_db": threshold_db, "days_rescored": len(days)}


def _apollo_sense_binary() -> str:
    return os.environ.get("APOLLO_SENSE_BIN") or "apollo-sense"


def calibrate_suggestion(*, seconds: int | None = None, binary: str | None = None) -> dict:
    command = [binary or _apollo_sense_binary(), "calibrate"]
    if seconds is not None:
        command += ["--window-seconds", str(seconds)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise AudioNotConnected(f"apollo-sense binary not found: {command[0]}") from error
    output = completed.stdout.strip()
    if not output:
        raise AudioSyncError(f"calibrate produced no output (exit {completed.returncode})")
    data = json.loads(output.splitlines()[-1])
    if data.get("all_zero") or data.get("error"):
        raise AudioSyncError(data.get("error", "calibration failed"))
    return data


def _rethreshold_windows(*, conn: sqlite3.Connection, threshold_db: float) -> list[str]:
    rows = conn.execute(
        "SELECT window_start, day, level_hist_db FROM audio_windows "
        "WHERE level_hist_db IS NOT NULL"
    ).fetchall()
    days: set[str] = set()
    for window_start, day, level_hist_json in rows:
        quiet_seconds = _quiet_seconds_at(json.loads(level_hist_json), threshold_db)
        conn.execute(
            "UPDATE audio_windows SET quiet_seconds = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE window_start = ?",
            (quiet_seconds, window_start),
        )
        days.add(day)
    return sorted(days)


def rebuild_daily(*, conn: sqlite3.Connection, days: list[str]) -> dict:
    for day in days:
        conn.execute(
            """
            INSERT INTO audio_daily(
                day, avg_noise_db, peak_noise_db, quiet_minutes, coverage_minutes
            )
            SELECT
                day,
                SUM(avg_noise_db * duration_seconds) / SUM(duration_seconds),
                MAX(peak_noise_db),
                SUM(quiet_seconds) / ?,
                SUM(duration_seconds) / ?
            FROM audio_windows
            WHERE day = ?
            GROUP BY day
            ON CONFLICT(day) DO UPDATE SET
                avg_noise_db = excluded.avg_noise_db,
                peak_noise_db = excluded.peak_noise_db,
                quiet_minutes = excluded.quiet_minutes,
                coverage_minutes = excluded.coverage_minutes,
                updated_at = CURRENT_TIMESTAMP
            """,
            (SECONDS_PER_MINUTE, SECONDS_PER_MINUTE, day),
        )
    return {"days_rebuilt": len(days)}


def prune_audio(*, conn: sqlite3.Connection, before_day: str) -> dict:
    windows_pruned = conn.execute(
        "DELETE FROM audio_windows WHERE day < ?",
        (before_day,),
    ).rowcount
    raw_records_pruned = conn.execute(
        "DELETE FROM raw_records WHERE object_type = ? AND substr(external_id, 1, 10) < ?",
        (OBJECT_TYPE, before_day),
    ).rowcount
    return {
        "windows_pruned": windows_pruned,
        "raw_records_pruned": raw_records_pruned,
    }


def _duration_seconds(window: dict) -> float:
    start = datetime.fromisoformat(window["window_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["window_end"].replace("Z", "+00:00"))
    return (end - start).total_seconds()


def _horizon_day(*, now: datetime | None = None) -> str:
    reference = now or datetime.now(UTC)
    return (reference - timedelta(days=RETENTION_DAYS)).date().isoformat()


def connect_audio(*, spool_path: Path | None = None) -> dict:
    path = spool_path or _default_spool_path()
    with store.connect() as conn:
        _ensure_audio_source(conn)
    return {
        "status": "connected",
        "source_slug": SOURCE_SLUG,
        "spool_path": str(path),
        "spool_present": path.exists(),
    }


def sync_audio(
    *,
    spool_path: Path | None = None,
    trigger_kind: str = "manual",
    now: datetime | None = None,
) -> dict:
    path = spool_path or _default_spool_path()
    if not path.exists():
        raise AudioNotConnected(str(path))
    cutoff = _horizon_day(now=now)
    with store.sync_guard(SOURCE_SLUG):
        with store.connect() as conn:
            source_id = _ensure_audio_source(conn)
            offset, fingerprint = _read_cursor(conn, source_id=source_id)
        spool = read_new_spool_rows(
            spool_path=path, offset=offset, fingerprint=fingerprint
        )
        rows = [row for row in spool["rows"] if row.get("window_start", "")[:10] >= cutoff]
        late_windows_dropped = len(spool["rows"]) - len(rows)
        with store.connect() as conn:
            sync_run_id = sync_control.start_sync_run(
                conn,
                source_id=source_id,
                trigger_kind=trigger_kind,
                request_start=rows[0]["window_start"] if rows else None,
                request_end=rows[-1]["window_end"] if rows else None,
            )
        try:
            result = _persist_audio(
                source_id=source_id,
                sync_run_id=sync_run_id,
                rows=rows,
                cutoff=cutoff,
                offset=spool["offset"],
                fingerprint=spool["fingerprint"],
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
            raise AudioSyncError(str(error)) from error
    return {
        "status": "ok",
        "windows": len(rows),
        "days": result["days"],
        "late_windows_dropped": late_windows_dropped,
        "windows_pruned": result["windows_pruned"],
        "cursor_reset": spool["reset"],
    }


def _persist_audio(
    *,
    source_id: str,
    sync_run_id: str,
    rows: list[dict],
    cutoff: str,
    offset: int,
    fingerprint: str,
) -> dict:
    batch_window_end = rows[-1]["window_end"] if rows else None
    with store.connect() as conn:
        batch_id = sync_control.start_sync_batch(
            conn,
            sync_run_id=sync_run_id,
            object_type=OBJECT_TYPE,
            window_start=rows[0]["window_start"] if rows else None,
            window_end=batch_window_end,
        )
        day_record_ids: dict[str, str] = {}
        for row in rows:
            raw_record_id = sync_control.persist_raw_record(
                conn,
                source_id=source_id,
                sync_batch_id=batch_id,
                provider=PROVIDER,
                object_type=OBJECT_TYPE,
                external_id=row["window_start"],
                payload=row,
                source_updated_at=row["window_end"],
                privacy_tier="standard",
                is_redacted=False,
            )
            day_record_ids.setdefault(row["window_start"][:10], raw_record_id)
        write_windows(
            conn=conn,
            rows=rows,
            silence_threshold_db=get_silence_threshold(conn=conn),
        )
        days = sorted(day_record_ids)
        rebuild_daily(conn=conn, days=days)
        for day, raw_record_id in day_record_ids.items():
            sync_control.attach_lineage(
                conn,
                canonical_table="audio_daily",
                canonical_id=day,
                raw_record_id=raw_record_id,
            )
        prune = prune_audio(conn=conn, before_day=cutoff)
        sync_control.finish_sync_batch(
            conn,
            sync_batch_id=batch_id,
            status="ok",
            cursor_after=str(offset),
            records_seen=len(rows),
            records_written=len(rows),
        )
        sync_control.update_cursor(
            conn,
            source_slug=SOURCE_SLUG,
            object_type=OBJECT_TYPE,
            cursor_kind=CURSOR_KIND,
            cursor_value=str(offset),
            window_start=rows[0]["window_start"] if rows else None,
            window_end=batch_window_end,
            metadata={"fingerprint": fingerprint},
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
    return {"days": days, "windows_pruned": prune["windows_pruned"]}


def _ensure_audio_source(conn: sqlite3.Connection) -> str:
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
        scopes=[{"scope_key": OBJECT_TYPE, "scope_label": "Ambient audio windows"}],
    )
    return source_id


def _read_cursor(conn: sqlite3.Connection, *, source_id: str) -> tuple[int, str | None]:
    row = conn.execute(
        """
        SELECT cursor_value, metadata_json
        FROM sync_cursors
        WHERE source_id = ? AND object_type = ? AND cursor_kind = ?
        """,
        (source_id, OBJECT_TYPE, CURSOR_KIND),
    ).fetchone()
    if row is None:
        return 0, None
    offset = int(row[0]) if row[0] is not None else 0
    metadata = json.loads(row[1] or "{}")
    return offset, metadata.get("fingerprint")


def _default_spool_path() -> Path:
    return store.hermes_home() / AUDIO_SPOOL_NAME
