from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str):
    package_name = "hermes_plugins.health_data"
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(ROOT)]
        sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.{name}", ROOT / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.import_module(spec.name) if spec.name in sys.modules else None
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


@pytest.fixture()
def modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return {"store": load_module("store")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _index_exists(conn: sqlite3.Connection, index: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index,),
    ).fetchone()
    return row is not None


def test_schema_version_is_seven(modules):
    store = modules["store"]
    assert store.SCHEMA_VERSION == 7


def test_new_tables_are_created(modules, tmp_path: Path):
    store = modules["store"]
    store.initialize()

    with sqlite3.connect(tmp_path / "health.db") as conn:
        for table in (
            "daily_health_metrics",
            "google_health_samples",
            "google_health_sessions",
        ):
            assert _table_exists(conn, table), f"missing table {table}"


def test_new_indexes_are_created(modules, tmp_path: Path):
    store = modules["store"]
    store.initialize()

    with sqlite3.connect(tmp_path / "health.db") as conn:
        for index in (
            "idx_daily_health_metrics_day_metric",
            "idx_daily_health_metrics_source_day",
            "idx_google_health_samples_source_metric_ts",
            "idx_google_health_samples_ts",
            "idx_google_health_sessions_source_day",
            "idx_google_health_sessions_source_type_day",
        ):
            assert _index_exists(conn, index), f"missing index {index}"


def test_initialize_is_idempotent(modules, tmp_path: Path):
    store = modules["store"]
    store.initialize()
    # second run must not raise and must not duplicate the schema_version row
    store.initialize()

    with sqlite3.connect(tmp_path / "health.db") as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == store.SCHEMA_VERSION


def test_existing_oura_tables_still_present(modules, tmp_path: Path):
    """Adding the new provider-agnostic tables must not regress the
    existing Oura / calendar / email / food / sync_state tables."""

    store = modules["store"]
    store.initialize()

    with sqlite3.connect(tmp_path / "health.db") as conn:
        for table in (
            "oura_daily",
            "oura_sleep_sessions",
            "oura_heart_rate",
            "oura_workouts",
            "oura_sessions",
            "calendar_daily",
            "email_daily",
            "food_logs",
            "sync_state",
            "health_sources",
            "source_scopes",
            "sync_runs",
            "sync_batches",
            "sync_cursors",
            "sync_errors",
            "sync_schedules",
            "raw_records",
            "record_lineage",
        ):
            assert _table_exists(conn, table), f"regression: missing {table}"


def test_daily_health_metrics_accepts_writes_and_unique_constraint(
    modules, tmp_path: Path
):
    store = modules["store"]
    store.initialize()

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO daily_health_metrics(day, source, metric, value_double, metric_unit)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("2026-06-12", "google_health", "step_count", 8088, "count"),
        )
        # Second insert on the same (day, source, metric) primary key must
        # be rejected. UPSERT behaviour is owned by the connector, not
        # the schema.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO daily_health_metrics(day, source, metric, value_double, metric_unit)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("2026-06-12", "google_health", "step_count", 9999, "count"),
            )


def test_google_health_samples_and_sessions_writable(modules, tmp_path: Path):
    store = modules["store"]
    store.initialize()

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO google_health_samples(
                sample_id, source, metric, timestamp, timestamp_unix,
                value_double, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "sample-1",
                "google_health",
                "heart_rate_bpm",
                "2026-06-12T10:00:00Z",
                1749722400,
                72.0,
                '{"value": 72.0}',
            ),
        )
        conn.execute(
            """
            INSERT INTO google_health_sessions(
                session_id, source, session_type, day, start_time, end_time,
                duration_seconds, metric_payload_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "session-1",
                "google_health",
                "sleep",
                "2026-06-12",
                "2026-06-11T22:30:00Z",
                "2026-06-12T06:30:00Z",
                28800,
                '{"stages": []}',
                "{}",
            ),
        )
        samples = conn.execute(
            "SELECT COUNT(*) FROM google_health_samples"
        ).fetchone()[0]
        sessions = conn.execute(
            "SELECT COUNT(*) FROM google_health_sessions"
        ).fetchone()[0]
        assert samples == 1
        assert sessions == 1
