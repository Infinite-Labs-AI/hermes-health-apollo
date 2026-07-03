from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 9

DAILY_OVERVIEW_SQL = """
    CREATE VIEW daily_overview AS
        SELECT
            od.day,
            od.readiness_score,
            od.sleep_score,
            od.activity_score,
            od.stress_high_seconds,
            od.recovery_high_seconds,
            od.stress_day_summary,
            od.resting_heart_rate,
            od.hrv_balance,
            od.spo2_average,
            od.total_sleep_duration_seconds,
            od.deep_sleep_duration_seconds,
            od.primary_bedtime_start,
            od.primary_bedtime_end,
            cd.meeting_count,
            cd.meeting_minutes,
            cd.first_meeting_start,
            cd.last_meeting_end,
            ed.received_count,
            ad.avg_noise_db,
            ad.peak_noise_db,
            ad.quiet_minutes
        FROM oura_daily od
        LEFT JOIN calendar_daily cd ON cd.day = od.day
        LEFT JOIN email_daily ed ON ed.day = od.day
        LEFT JOIN audio_daily ad ON ad.day = od.day
"""


class SyncAlreadyRunning(RuntimeError):
    pass


def hermes_home() -> Path:
    try:
        from hermes_cli.utils import get_hermes_home
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return Path(get_hermes_home())


def database_path() -> Path:
    return hermes_home() / "health.db"


def connect() -> sqlite3.Connection:
    initialize()
    conn = sqlite3.connect(database_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(path: Path | None = None) -> Path:
    db_path = path or database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _configure(conn)
        _migrate(conn)
    _chmod_private_database_files(db_path)
    return db_path


def _configure(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA foreign_keys=ON")


def _chmod_private_database_files(db_path: Path) -> None:
    if os.name == "nt":
        return
    for path in _database_file_family(db_path):
        if path.exists():
            os.chmod(path, 0o600)


def _database_file_family(db_path: Path) -> tuple[Path, ...]:
    return (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
        db_path.with_name(f"{db_path.name}-journal"),
    )


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oura_daily (
            day TEXT PRIMARY KEY,
            readiness_score INTEGER,
            sleep_score INTEGER,
            activity_score INTEGER,
            stress_high_seconds INTEGER DEFAULT 0 CHECK(stress_high_seconds >= 0),
            recovery_high_seconds INTEGER DEFAULT 0 CHECK(recovery_high_seconds >= 0),
            stress_day_summary TEXT,
            resting_heart_rate REAL,
            hrv_balance REAL,
            spo2_average REAL,
            total_sleep_duration_seconds INTEGER,
            deep_sleep_duration_seconds INTEGER,
            primary_bedtime_start TEXT,
            primary_bedtime_end TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_sleep_sessions (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            type TEXT NOT NULL,
            bedtime_start TEXT,
            bedtime_end TEXT,
            total_sleep_duration_seconds INTEGER,
            deep_sleep_duration_seconds INTEGER,
            raw_json TEXT
        );

        CREATE TABLE IF NOT EXISTS oura_heart_rate (
            timestamp TEXT PRIMARY KEY,
            timestamp_unix INTEGER,
            bpm INTEGER NOT NULL,
            source TEXT NOT NULL,
            producer_timestamp TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_ring_battery (
            timestamp TEXT PRIMARY KEY,
            timestamp_unix INTEGER,
            producer_timestamp TEXT,
            level INTEGER NOT NULL,
            charging INTEGER,
            in_charger INTEGER,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_personal_info (
            id TEXT PRIMARY KEY,
            age INTEGER,
            weight REAL,
            height REAL,
            biological_sex TEXT,
            email TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_workouts (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            activity TEXT,
            calories REAL,
            distance REAL,
            intensity TEXT,
            label TEXT,
            source TEXT,
            start_datetime TEXT,
            end_datetime TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_sessions (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            type TEXT,
            start_datetime TEXT,
            end_datetime TEXT,
            mood TEXT,
            heart_rate_json TEXT,
            heart_rate_variability_json TEXT,
            motion_count_json TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_tags (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            text TEXT,
            timestamp TEXT,
            tags_json TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_enhanced_tags (
            id TEXT PRIMARY KEY,
            start_day TEXT NOT NULL,
            end_day TEXT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            tag_type_code TEXT,
            comment TEXT,
            custom_name TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_daily_resilience (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            level TEXT,
            contributors_json TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_daily_cardiovascular_age (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            vascular_age INTEGER,
            pulse_wave_velocity REAL,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_vo2_max (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            timestamp TEXT,
            vo2_max INTEGER,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_sleep_time (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            recommendation TEXT,
            status TEXT,
            optimal_bedtime_json TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_rest_mode_periods (
            id TEXT PRIMARY KEY,
            start_day TEXT NOT NULL,
            end_day TEXT,
            start_time TEXT,
            end_time TEXT,
            episodes_json TEXT,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS oura_ring_configuration (
            id TEXT PRIMARY KEY,
            color TEXT,
            design TEXT,
            firmware_version TEXT,
            hardware_type TEXT,
            set_up_at TEXT,
            size INTEGER,
            raw_json TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS calendar_daily (
            day TEXT PRIMARY KEY,
            meeting_count INTEGER NOT NULL DEFAULT 0,
            meeting_minutes INTEGER NOT NULL DEFAULT 0,
            first_meeting_start TEXT,
            last_meeting_end TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS email_daily (
            day TEXT PRIMARY KEY,
            received_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audio_windows (
            window_start TEXT PRIMARY KEY,
            window_end TEXT NOT NULL,
            day TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            avg_noise_db REAL,
            peak_noise_db REAL,
            quiet_seconds REAL NOT NULL DEFAULT 0,
            level_hist_db TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audio_daily (
            day TEXT PRIMARY KEY,
            avg_noise_db REAL,
            peak_noise_db REAL,
            quiet_minutes REAL,
            coverage_minutes REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audio_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            silence_threshold_db REAL NOT NULL DEFAULT -50.0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS location_clusters (
            location_id TEXT PRIMARY KEY,
            centroid_lat_coarse REAL,
            centroid_lng_coarse REAL,
            radius_meters REAL,
            visit_count INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            user_label TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS location_visits (
            visit_id TEXT PRIMARY KEY,
            location_id TEXT NOT NULL REFERENCES location_clusters(location_id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            arrival_at TEXT NOT NULL,
            departure_at TEXT,
            duration_minutes REAL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS location_audio_daily (
            location_id TEXT NOT NULL REFERENCES location_clusters(location_id) ON DELETE CASCADE,
            day TEXT NOT NULL,
            avg_noise_db REAL,
            quiet_minutes REAL,
            visit_minutes REAL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (location_id, day)
        );

        CREATE TABLE IF NOT EXISTS food_logs (
            id TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            logged_at TEXT NOT NULL,
            description TEXT NOT NULL,
            items_json TEXT
        );

        CREATE TABLE IF NOT EXISTS sync_state (
            provider TEXT PRIMARY KEY,
            last_sync_date TEXT,
            last_status TEXT NOT NULL DEFAULT 'never'
                CHECK(last_status IN ('ok', 'partial', 'error', 'never', 'running')),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS health_sources (
            source_id TEXT PRIMARY KEY,
            source_slug TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            connection_name TEXT NOT NULL,
            account_external_id TEXT,
            status TEXT NOT NULL CHECK(status IN ('connected', 'partial', 'manual', 'revoked', 'disconnected')),
            sync_mode TEXT NOT NULL CHECK(sync_mode IN ('pull', 'manual')),
            connected_at TEXT NOT NULL,
            last_synced_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS source_scopes (
            scope_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES health_sources(source_id) ON DELETE CASCADE,
            scope_key TEXT NOT NULL,
            scope_label TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            revoked_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_id, scope_key)
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
            sync_run_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES health_sources(source_id) ON DELETE CASCADE,
            trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('manual', 'cron', 'question_refresh', 'backfill')),
            request_start TEXT,
            request_end TEXT,
            status TEXT NOT NULL CHECK(status IN ('running', 'ok', 'partial', 'error')),
            records_seen INTEGER NOT NULL DEFAULT 0,
            records_written INTEGER NOT NULL DEFAULT 0,
            batch_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS sync_batches (
            sync_batch_id TEXT PRIMARY KEY,
            sync_run_id TEXT NOT NULL REFERENCES sync_runs(sync_run_id) ON DELETE CASCADE,
            object_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running', 'ok', 'partial', 'error')),
            window_start TEXT,
            window_end TEXT,
            cursor_before TEXT,
            cursor_after TEXT,
            records_seen INTEGER NOT NULL DEFAULT 0,
            records_written INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS sync_cursors (
            sync_cursor_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES health_sources(source_id) ON DELETE CASCADE,
            object_type TEXT NOT NULL,
            cursor_kind TEXT NOT NULL,
            cursor_value TEXT,
            window_start TEXT,
            window_end TEXT,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source_id, object_type, cursor_kind)
        );

        CREATE TABLE IF NOT EXISTS sync_errors (
            sync_error_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES health_sources(source_id) ON DELETE CASCADE,
            sync_run_id TEXT REFERENCES sync_runs(sync_run_id) ON DELETE CASCADE,
            sync_batch_id TEXT REFERENCES sync_batches(sync_batch_id) ON DELETE CASCADE,
            object_type TEXT,
            error_code TEXT,
            error_message TEXT NOT NULL,
            retryable INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS sync_schedules (
            sync_schedule_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES health_sources(source_id) ON DELETE CASCADE,
            schedule_key TEXT NOT NULL,
            trigger_kind TEXT NOT NULL CHECK(trigger_kind IN ('cron', 'manual')),
            cadence_minutes INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_registered_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(source_id, schedule_key)
        );

        CREATE TABLE IF NOT EXISTS raw_records (
            raw_record_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES health_sources(source_id) ON DELETE CASCADE,
            sync_batch_id TEXT REFERENCES sync_batches(sync_batch_id) ON DELETE SET NULL,
            provider TEXT NOT NULL,
            object_type TEXT NOT NULL,
            external_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_version TEXT NOT NULL DEFAULT 'v1',
            source_record_hash TEXT NOT NULL,
            source_updated_at TEXT,
            extracted_at TEXT NOT NULL,
            privacy_tier TEXT NOT NULL CHECK(privacy_tier IN ('standard', 'sensitive')),
            is_redacted INTEGER NOT NULL DEFAULT 0,
            UNIQUE(source_id, object_type, external_id, source_record_hash)
        );

        CREATE TABLE IF NOT EXISTS record_lineage (
            lineage_id TEXT PRIMARY KEY,
            canonical_table TEXT NOT NULL,
            canonical_id TEXT NOT NULL,
            raw_record_id TEXT NOT NULL REFERENCES raw_records(raw_record_id) ON DELETE CASCADE,
            normalization_version TEXT NOT NULL,
            lineage_role TEXT NOT NULL DEFAULT 'source',
            created_at TEXT NOT NULL,
            UNIQUE(canonical_table, canonical_id, raw_record_id)
        );

        CREATE INDEX IF NOT EXISTS idx_oura_sleep_day_type
            ON oura_sleep_sessions(day, type);
        CREATE INDEX IF NOT EXISTS idx_oura_heart_rate_timestamp
            ON oura_heart_rate(timestamp);
        CREATE INDEX IF NOT EXISTS idx_oura_heart_rate_source
            ON oura_heart_rate(source);
        CREATE INDEX IF NOT EXISTS idx_oura_workouts_day
            ON oura_workouts(day);
        CREATE INDEX IF NOT EXISTS idx_oura_sessions_day
            ON oura_sessions(day);
        CREATE INDEX IF NOT EXISTS idx_oura_tags_day
            ON oura_tags(day);
        CREATE INDEX IF NOT EXISTS idx_oura_enhanced_tags_day
            ON oura_enhanced_tags(start_day);
        CREATE INDEX IF NOT EXISTS idx_oura_resilience_day
            ON oura_daily_resilience(day);
        CREATE INDEX IF NOT EXISTS idx_oura_cardiovascular_day
            ON oura_daily_cardiovascular_age(day);
        CREATE INDEX IF NOT EXISTS idx_oura_vo2_day
            ON oura_vo2_max(day);
        CREATE INDEX IF NOT EXISTS idx_oura_sleep_time_day
            ON oura_sleep_time(day);
        CREATE INDEX IF NOT EXISTS idx_oura_rest_mode_day
            ON oura_rest_mode_periods(start_day);
        CREATE INDEX IF NOT EXISTS idx_oura_ring_battery_timestamp
            ON oura_ring_battery(timestamp);
        CREATE INDEX IF NOT EXISTS idx_food_logs_day
            ON food_logs(day);
        CREATE INDEX IF NOT EXISTS idx_oura_daily_stress_summary
            ON oura_daily(stress_day_summary);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_health_sources_slug
            ON health_sources(source_slug);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_source_scopes_unique
            ON source_scopes(source_id, scope_key);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_cursors_unique
            ON sync_cursors(source_id, object_type, cursor_kind);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_records_unique
            ON raw_records(source_id, object_type, external_id, source_record_hash);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_record_lineage_unique
            ON record_lineage(canonical_table, canonical_id, raw_record_id);
        CREATE INDEX IF NOT EXISTS idx_sync_runs_source_started_at
            ON sync_runs(source_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_sync_batches_run_object
            ON sync_batches(sync_run_id, object_type);
        CREATE INDEX IF NOT EXISTS idx_sync_errors_run_batch
            ON sync_errors(sync_run_id, sync_batch_id);
        CREATE INDEX IF NOT EXISTS idx_raw_records_source_object
            ON raw_records(source_id, object_type, extracted_at DESC);
        CREATE INDEX IF NOT EXISTS idx_audio_windows_day
            ON audio_windows(day);
        CREATE INDEX IF NOT EXISTS idx_location_visits_day
            ON location_visits(day);
        CREATE INDEX IF NOT EXISTS idx_location_visits_location
            ON location_visits(location_id, day);
        """
    )
    _ensure_column(conn, "oura_ring_battery", "producer_timestamp", "TEXT")
    _ensure_column(conn, "audio_windows", "level_hist_db", "TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO audio_settings(id, silence_threshold_db) VALUES (1, -50.0)"
    )
    _ensure_daily_overview(conn)
    from . import semantic_layer

    semantic_layer.ensure_canonical_schema(conn)
    _ensure_onboarding_schema(conn)
    from . import sync_control

    sync_control.backfill_legacy_sources(conn)
    sync_control.ensure_default_schedules(
        conn,
        cadence_minutes=360,
        schedule_key="health-data-sync",
        sources=("oura", "google_calendar", "gmail", "manual_food"),
    )
    current = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if current == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    else:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))


def _ensure_daily_overview(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_overview)")}
    if "avg_noise_db" in columns:
        return
    with conn:
        conn.execute("DROP VIEW IF EXISTS daily_overview")
        conn.execute(DAILY_OVERVIEW_SQL)


def _ensure_onboarding_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS health_profile (
            id TEXT PRIMARY KEY,
            timezone TEXT,
            goals_json TEXT NOT NULL,
            already_uses_json TEXT NOT NULL,
            privacy_json TEXT NOT NULL,
            routine_json TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS health_setup_runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            next_action TEXT,
            goals_json TEXT NOT NULL,
            already_uses_json TEXT NOT NULL,
            privacy_json TEXT NOT NULL,
            recommendations_json TEXT NOT NULL,
            source_states_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS exercise_routines (
            id TEXT PRIMARY KEY,
            weekday INTEGER NOT NULL,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextlib.contextmanager
def sync_guard(provider: str):
    initialize()
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(database_path())
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_status FROM sync_state WHERE provider = ?",
            (provider,),
        ).fetchone()
        if row and row[0] == "running":
            conn.rollback()
            raise SyncAlreadyRunning(f"{provider} sync is already running")
        conn.execute(
            """
            INSERT INTO sync_state(provider, last_status, updated_at)
            VALUES (?, 'running', ?)
            ON CONFLICT(provider) DO UPDATE SET
                last_status = 'running',
                updated_at = excluded.updated_at
            """,
            (provider, now),
        )
        conn.commit()
        yield
        finish = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE sync_state SET last_status = 'ok', updated_at = ? WHERE provider = ?",
            (finish, provider),
        )
        conn.commit()
    except SyncAlreadyRunning:
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        error_time = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE sync_state SET last_status = 'error', updated_at = ? WHERE provider = ?",
            (error_time, provider),
        )
        conn.commit()
        raise
    finally:
        conn.close()
