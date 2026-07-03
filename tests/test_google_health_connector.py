from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import parse

import pytest


ROOT = Path(__file__).resolve().parents[1]


# Mirrors the per-test-file loader convention used elsewhere in this suite
# (e.g. tests/test_health_observation_schema.py); there is no shared conftest.
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
    return {
        "store": load_module("store"),
        "sync_control": load_module("sync_control"),
        "google_health": load_module("google_health"),
        "auth": load_module("google_health_auth"),
    }


def _unix(rfc3339: str) -> int:
    return int(
        datetime.fromisoformat(rfc3339.replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        .timestamp()
    )


# --- Real Google Health API v4 DataPoint fixtures -------------------------- #
# Shape verified against developers.google.com/health (REST reference + v4
# discovery doc): each DataPoint is {name, dataSource, <unionField>: {...}}.


def _sample_response():  # heart-rate -> sample
    return {
        "dataType": "heart-rate",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/heart-rate/dataPoints/hr-1",
                "dataSource": {
                    "recordingMethod": "AUTOMATICALLY_RECORDED",
                    "platform": "FITBIT",
                    "application": {"packageName": "com.fitbit"},
                    "device": {"type": "WATCH", "uid": "dev-1"},
                },
                "heartRate": {
                    "sampleTime": {"physicalTime": "2026-06-12T10:00:00Z"},
                    "beatsPerMinute": "62",
                },
            }
        ],
    }


def _interval_response():  # steps -> interval
    return {
        "dataType": "steps",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/steps/dataPoints/st-1",
                "dataSource": {"platform": "FITBIT"},
                "steps": {
                    "interval": {
                        "startTime": "2026-06-12T10:00:00Z",
                        "endTime": "2026-06-12T10:05:00Z",
                    },
                    "count": "540",
                },
            }
        ],
    }


def _session_response():  # sleep -> session
    return {
        "dataType": "sleep",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/sleep/dataPoints/sleep-session-1",
                "dataSource": {"platform": "FITBIT"},
                "sleep": {
                    "interval": {
                        "startTime": "2026-06-12T23:10:00Z",
                        "endTime": "2026-06-13T06:40:00Z",
                    }
                },
            }
        ],
    }


def _daily_response():  # daily-resting-heart-rate -> daily
    return {
        "dataType": "daily-resting-heart-rate",
        "userId": "health-user-1",
        "point": [
            {
                "name": (
                    "users/health-user-1/dataTypes/daily-resting-heart-rate/"
                    "dataPoints/drhr-1"
                ),
                "dataSource": {"platform": "GOOGLE"},
                "dailyRestingHeartRate": {
                    "date": {"year": 2026, "month": 6, "day": 12},
                    "beatsPerMinute": "58",
                },
            }
        ],
    }


def test_sample_datapoint_persists_with_provenance_lineage_and_unix(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[_sample_response()])

        row = conn.execute(
            """
            SELECT observation_key, provider_user_id, provider_data_type,
                   metric, metric_unit, value_number, sample_time, sample_time_unix,
                   provenance_json
            FROM health_sample_observations
            """
        ).fetchall()
        assert len(row) == 1
        (
            observation_key,
            provider_user_id,
            provider_data_type,
            metric,
            metric_unit,
            value_number,
            sample_time,
            sample_time_unix,
            provenance_json,
        ) = tuple(row[0])
        assert provider_user_id == "health-user-1"
        assert provider_data_type == "heart-rate"
        assert metric == "heart_rate"
        assert metric_unit == "bpm"
        assert value_number == 62.0
        assert sample_time == "2026-06-12T10:00:00Z"
        # The live connector must populate the format-independent ordering guard.
        assert sample_time_unix == _unix("2026-06-12T10:00:00Z")
        assert json.loads(provenance_json)["platform"] == "FITBIT"

        lineage = conn.execute(
            """
            SELECT rr.object_type
            FROM record_lineage rl
            JOIN raw_records rr ON rr.raw_record_id = rl.raw_record_id
            WHERE rl.canonical_table = 'health_sample_observations'
              AND rl.canonical_id = ?
            """,
            (observation_key,),
        ).fetchall()
        assert len(lineage) == 1


def test_interval_datapoint_persists_with_unix(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[_interval_response()])

        rows = conn.execute(
            """
            SELECT metric, metric_unit, value_number, start_time, end_time,
                   start_time_unix, end_time_unix
            FROM health_interval_observations
            """
        ).fetchall()
        assert len(rows) == 1
        (
            metric,
            metric_unit,
            value_number,
            start_time,
            end_time,
            start_time_unix,
            end_time_unix,
        ) = tuple(rows[0])
        assert metric == "steps"
        assert metric_unit == "count"
        assert value_number == 540
        assert start_time == "2026-06-12T10:00:00Z"
        assert end_time == "2026-06-12T10:05:00Z"
        assert start_time_unix == _unix("2026-06-12T10:00:00Z")
        assert end_time_unix == _unix("2026-06-12T10:05:00Z")
        assert end_time_unix > start_time_unix


def test_session_datapoint_persists(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[_session_response()])

        rows = conn.execute(
            """
            SELECT session_key, session_type, day, start_time, end_time, duration_seconds
            FROM health_sessions
            """
        ).fetchall()
        assert len(rows) == 1
        session_key, session_type, day, start_time, end_time, duration = tuple(rows[0])
        assert session_type == "sleep"
        assert day == "2026-06-12"
        assert start_time == "2026-06-12T23:10:00Z"
        assert end_time == "2026-06-13T06:40:00Z"
        assert duration == 7 * 3600 + 30 * 60


def test_daily_datapoint_persists_with_civil_date_and_aggregation_kind(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[_daily_response()])

        rows = conn.execute(
            """
            SELECT day, metric, value_number, aggregation_kind
            FROM daily_health_metrics
            """
        ).fetchall()
        assert len(rows) == 1
        day, metric, value_number, aggregation_kind = tuple(rows[0])
        assert day == "2026-06-12"
        assert metric == "resting_heart_rate"
        assert value_number == 58.0
        assert aggregation_kind == "provider_daily_summary"


def test_name_absent_points_get_deterministic_distinct_keys(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    # Defensive: a DataPoint without a resource `name` still persists distinctly,
    # keyed by its sample instant.
    response = {
        "dataType": "heart-rate",
        "userId": "health-user-1",
        "point": [
            {
                "heartRate": {
                    "sampleTime": {"physicalTime": "2026-06-12T10:00:00Z"},
                    "beatsPerMinute": "62",
                }
            },
            {
                "heartRate": {
                    "sampleTime": {"physicalTime": "2026-06-12T10:01:00Z"},
                    "beatsPerMinute": "64",
                }
            },
        ],
    }

    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[response])
        count = conn.execute(
            "SELECT COUNT(*) FROM health_sample_observations"
        ).fetchone()[0]
    assert count == 2


def test_reingest_is_idempotent(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    responses = [
        _sample_response(),
        _interval_response(),
        _session_response(),
        _daily_response(),
    ]
    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=responses)
        google_health.persist_google_health(conn, responses=responses)

        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "health_sample_observations",
                "health_interval_observations",
                "health_sessions",
                "daily_health_metrics",
                "raw_records",
                "record_lineage",
            )
        }
    assert counts["health_sample_observations"] == 1
    assert counts["health_interval_observations"] == 1
    assert counts["health_sessions"] == 1
    assert counts["daily_health_metrics"] == 1
    assert counts["raw_records"] == 4
    assert counts["record_lineage"] == 4


def test_unregistered_datatype_is_skipped(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    unknown = {
        "dataType": "blood-glucose",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/blood-glucose/dataPoints/bg-1",
                "bloodGlucose": {
                    "sampleTime": {"physicalTime": "2026-06-12T10:00:00Z"},
                    "level": {"value": "5.4"},
                },
            }
        ],
    }
    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[unknown])
        sample = conn.execute(
            "SELECT COUNT(*) FROM health_sample_observations"
        ).fetchone()[0]
        raw = conn.execute("SELECT COUNT(*) FROM raw_records").fetchone()[0]
    assert sample == 0
    assert raw == 0


def test_derive_key_is_collision_safe(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    # Two distinct points whose data_type/name/time parts would collide under a
    # naive ':'-join. They must persist as two rows, not silently overwrite.
    response = {
        "dataType": "heart-rate",
        "userId": "health-user-1",
        "point": [
            {
                "name": "a:b",
                "heartRate": {
                    "sampleTime": {"physicalTime": "2026-06-12T11:00:00Z"},
                    "beatsPerMinute": "1",
                },
            },
            {
                "name": "a",
                "heartRate": {
                    "sampleTime": {"physicalTime": "b:2026-06-12T11:00:00Z"},
                    "beatsPerMinute": "2",
                },
            },
        ],
    }
    with store.connect() as conn:
        google_health.persist_google_health(conn, responses=[response])
        count = conn.execute(
            "SELECT COUNT(*) FROM health_sample_observations"
        ).fetchone()[0]
    assert count == 2


def test_out_of_order_interval_is_skipped_without_aborting_batch(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    bad_interval = {
        "dataType": "steps",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/steps/dataPoints/bad",
                "steps": {
                    "interval": {
                        "startTime": "2026-06-12T10:05:00Z",
                        "endTime": "2026-06-12T10:00:00Z",
                    },
                    "count": "10",
                },
            }
        ],
    }
    with store.connect() as conn:
        # The reversed interval must be skipped, and must NOT abort the good sample
        # response that follows it in the same batch.
        google_health.persist_google_health(
            conn, responses=[bad_interval, _sample_response()]
        )
        interval_count = conn.execute(
            "SELECT COUNT(*) FROM health_interval_observations"
        ).fetchone()[0]
        sample_count = conn.execute(
            "SELECT COUNT(*) FROM health_sample_observations"
        ).fetchone()[0]
    assert interval_count == 0
    assert sample_count == 1


def test_int_zero_value_is_preserved_as_true_zero(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    response = {
        "dataType": "steps",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/steps/dataPoints/zero",
                "steps": {
                    "interval": {
                        "startTime": "2026-06-12T10:00:00Z",
                        "endTime": "2026-06-12T10:05:00Z",
                    },
                    "count": "0",
                },
            }
        ],
    }
    with store.connect() as conn:
        # A recorded zero is a true zero, not missing data.
        google_health.persist_google_health(conn, responses=[response])
        value = conn.execute(
            "SELECT value_number FROM health_interval_observations"
        ).fetchone()[0]
    assert value == 0.0


def test_non_numeric_value_does_not_abort_batch(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    bad_value = {
        "dataType": "heart-rate",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/heart-rate/dataPoints/bad",
                "heartRate": {
                    "sampleTime": {"physicalTime": "2026-06-12T09:00:00Z"},
                    "beatsPerMinute": "not-a-number",
                },
            }
        ],
    }
    with store.connect() as conn:
        # A non-numeric value persists with a NULL value_number rather than raising
        # and aborting the good interval response that follows it.
        google_health.persist_google_health(
            conn, responses=[bad_value, _interval_response()]
        )
        sample = conn.execute(
            "SELECT value_number FROM health_sample_observations"
        ).fetchall()
        interval_count = conn.execute(
            "SELECT COUNT(*) FROM health_interval_observations"
        ).fetchone()[0]
    assert len(sample) == 1
    assert tuple(sample[0])[0] is None
    assert interval_count == 1


def _set_profile_timezone(conn, tz_name):
    conn.execute(
        """
        INSERT INTO health_profile(
            id, timezone, goals_json, already_uses_json, privacy_json, routine_json
        )
        VALUES ('default', ?, '[]', '[]', '{}', '{}')
        ON CONFLICT(id) DO UPDATE SET timezone = excluded.timezone
        """,
        (tz_name,),
    )


def test_session_day_uses_profile_timezone_tokyo(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    with store.connect() as conn:
        _set_profile_timezone(conn, "Asia/Tokyo")
        # 2026-06-12T23:10:00Z is 2026-06-13 08:10 in Tokyo (UTC+9): the session
        # belongs to the user's local day 06-13, not the UTC day 06-12.
        google_health.persist_google_health(conn, responses=[_session_response()])
        day = conn.execute("SELECT day FROM health_sessions").fetchone()[0]
    assert day == "2026-06-13"


def test_session_day_uses_profile_timezone_california(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    session = {
        "dataType": "sleep",
        "userId": "health-user-1",
        "point": [
            {
                "name": "users/health-user-1/dataTypes/sleep/dataPoints/sleep-ca-1",
                "dataSource": {"platform": "FITBIT"},
                "sleep": {
                    "interval": {
                        "startTime": "2026-06-13T05:00:00Z",
                        "endTime": "2026-06-13T12:30:00Z",
                    }
                },
            }
        ],
    }
    with store.connect() as conn:
        _set_profile_timezone(conn, "America/Los_Angeles")
        # 2026-06-13T05:00:00Z is 2026-06-12 22:00 PDT (UTC-7): local day 06-12.
        google_health.persist_google_health(conn, responses=[session])
        day = conn.execute("SELECT day FROM health_sessions").fetchone()[0]
    assert day == "2026-06-12"


def test_invalid_profile_timezone_falls_back_to_utc_day(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    store.initialize()

    with store.connect() as conn:
        _set_profile_timezone(conn, "Not/AZone")
        # An unknown timezone must not raise; fall back to the UTC calendar day.
        google_health.persist_google_health(conn, responses=[_session_response()])
        day = conn.execute("SELECT day FROM health_sessions").fetchone()[0]
    assert day == "2026-06-12"


# --- Live fetch + sync (fully offline, injected HTTP) ---------------------- #

_IDENTITY = {
    "name": "users/123/identity",
    "healthUserId": "123",
    "legacyUserId": "fitbit-1",
}


def _datapoints_by_type():
    return {
        "heart-rate": _sample_response()["point"],
        "steps": _interval_response()["point"],
        "sleep": _session_response()["point"],
        "daily-resting-heart-rate": _daily_response()["point"],
    }


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body


def _fake_request(*, datapoints, errors=None, calls=None, identity_status=200):
    errors = errors or {}

    def request(method, url, *, params=None, headers=None):
        if calls is not None:
            calls.append((method, parse.urlparse(url).path, params))
        path = parse.urlparse(url).path
        if path.endswith("users/me/identity"):
            if identity_status >= 400:
                return _Resp(identity_status, {"error": {"message": "identity boom"}})
            return _Resp(200, dict(_IDENTITY))
        data_type = path.split("/dataTypes/")[1].split("/")[0]
        if data_type in errors:
            return _Resp(errors[data_type], {"error": {"message": "boom"}})
        return _Resp(200, {"dataPoints": datapoints.get(data_type, [])})

    return request


def _save_fresh_token(auth):
    auth.save_token(
        {
            "access_token": "gh-access",
            "refresh_token": "gh-refresh",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )


def test_sync_persists_all_shapes_with_unix_and_advances_cursor(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    _save_fresh_token(auth)

    result = google_health.sync_google_health(
        request=_fake_request(datapoints=_datapoints_by_type()),
        today=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["user_id"] == "123"
    assert result["endpoint_errors"] == {}
    assert result["counts"] == {"sample": 1, "interval": 1, "session": 1, "daily": 1}

    with store.connect() as conn:
        sample = conn.execute(
            "SELECT provider_user_id, value_number, sample_time_unix "
            "FROM health_sample_observations"
        ).fetchone()
        interval_unix = conn.execute(
            "SELECT start_time_unix, end_time_unix FROM health_interval_observations"
        ).fetchone()
        session_count = conn.execute("SELECT COUNT(*) FROM health_sessions").fetchone()[0]
        daily_count = conn.execute(
            "SELECT COUNT(*) FROM daily_health_metrics"
        ).fetchone()[0]
        raw = conn.execute(
            "SELECT COUNT(*) FROM raw_records WHERE provider = 'google_health'"
        ).fetchone()[0]
        source = conn.execute(
            "SELECT status, last_synced_at FROM health_sources WHERE source_slug = 'google_health'"
        ).fetchone()
        run = conn.execute(
            "SELECT status, records_seen, records_written, error_count "
            "FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        cursor = conn.execute(
            "SELECT cursor_value FROM sync_cursors WHERE object_type = 'dataPoints' "
            "AND cursor_kind = 'date_window_end'"
        ).fetchone()

    assert tuple(sample) == ("123", 62.0, _unix("2026-06-12T10:00:00Z"))
    assert tuple(interval_unix) == (
        _unix("2026-06-12T10:00:00Z"),
        _unix("2026-06-12T10:05:00Z"),
    )
    assert session_count == 1
    assert daily_count == 1
    assert raw == 4
    assert tuple(source) == ("connected",) + (source[1],)
    assert source[1] is not None
    assert tuple(run) == ("ok", 4, 4, 0)
    assert tuple(cursor) == ("2026-06-14",)


def test_sync_partial_when_one_datatype_errors(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    _save_fresh_token(auth)

    result = google_health.sync_google_health(
        request=_fake_request(datapoints=_datapoints_by_type(), errors={"sleep": 403}),
        today=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert "sleep" in result["endpoint_errors"]
    assert "403" in result["endpoint_errors"]["sleep"]

    with store.connect() as conn:
        run = conn.execute(
            "SELECT status, error_count FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        error_row = conn.execute(
            "SELECT object_type, retryable FROM sync_errors ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        source = conn.execute(
            "SELECT status FROM health_sources WHERE source_slug = 'google_health'"
        ).fetchone()
        # The healthy data types still persisted.
        sample_count = conn.execute(
            "SELECT COUNT(*) FROM health_sample_observations"
        ).fetchone()[0]
        session_count = conn.execute("SELECT COUNT(*) FROM health_sessions").fetchone()[0]

    assert tuple(run) == ("partial", 1)
    assert tuple(error_row) == ("sleep", 1)
    assert tuple(source) == ("partial",)
    assert sample_count == 1
    assert session_count == 0  # the errored sleep data type wrote nothing


def test_sync_raises_and_marks_error_when_all_datatypes_fail(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    _save_fresh_token(auth)

    errors = {dt: 500 for dt in google_health.METRIC_REGISTRY}
    with pytest.raises(google_health.GoogleHealthAPIError, match="all data types"):
        google_health.sync_google_health(
            request=_fake_request(datapoints={}, errors=errors),
            today=datetime(2026, 6, 14, tzinfo=timezone.utc),
        )

    with store.connect() as conn:
        run = conn.execute(
            "SELECT status FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM sync_cursors WHERE object_type = 'dataPoints'"
        ).fetchone()[0]
    assert tuple(run) == ("error",)
    # A total failure must NOT advance the incremental cursor.
    assert cursor == 0


def test_sync_is_idempotent_across_runs(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    _save_fresh_token(auth)

    for _ in range(2):
        google_health.sync_google_health(
            request=_fake_request(datapoints=_datapoints_by_type()),
            today=datetime(2026, 6, 14, tzinfo=timezone.utc),
        )

    with store.connect() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "health_sample_observations",
                "health_interval_observations",
                "health_sessions",
                "daily_health_metrics",
                "raw_records",
            )
        }
    assert counts["health_sample_observations"] == 1
    assert counts["health_interval_observations"] == 1
    assert counts["health_sessions"] == 1
    assert counts["daily_health_metrics"] == 1
    assert counts["raw_records"] == 4


def test_sync_refreshes_an_expired_token_before_fetching(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    auth.save_client_credentials("client-id", "client-secret")
    auth.save_token(
        {
            "access_token": "stale",
            "refresh_token": "gh-refresh",
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        }
    )
    refresh_calls = []

    def http_post(url, data, headers=None):
        refresh_calls.append((url, data))
        return {"access_token": "fresh-access", "expires_in": 3600}

    result = google_health.sync_google_health(
        request=_fake_request(datapoints=_datapoints_by_type()),
        http_post=http_post,
        today=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert len(refresh_calls) == 1
    assert refresh_calls[0][0] == "https://oauth2.googleapis.com/token"
    assert refresh_calls[0][1]["grant_type"] == "refresh_token"
    # The refreshed access token is persisted (refresh token preserved, not rotated).
    assert auth.load_token()["access_token"] == "fresh-access"
    assert auth.load_token()["refresh_token"] == "gh-refresh"


def test_sync_sends_documented_time_filter_per_data_type(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    _save_fresh_token(auth)
    calls = []

    google_health.sync_google_health(
        request=_fake_request(datapoints=_datapoints_by_type(), calls=calls),
        start_date="2026-06-01",
        end_date="2026-06-03",
    )

    filters = {}
    for _method, path, params in calls:
        if "/dataTypes/" in path:
            dt = path.split("/dataTypes/")[1].split("/")[0]
            filters[dt] = (params or {}).get("filter")

    # End date is made exclusive (advanced one day): 2026-06-03 -> < 2026-06-04.
    assert filters["heart-rate"] == (
        'heart_rate.sample_time.physical_time >= "2026-06-01T00:00:00Z" '
        'AND heart_rate.sample_time.physical_time < "2026-06-04T00:00:00Z"'
    )
    assert filters["steps"] == (
        'steps.interval.start_time >= "2026-06-01T00:00:00Z" '
        'AND steps.interval.start_time < "2026-06-04T00:00:00Z"'
    )
    assert filters["sleep"] == (
        'sleep.interval.start_time >= "2026-06-01T00:00:00Z" '
        'AND sleep.interval.start_time < "2026-06-04T00:00:00Z"'
    )
    assert filters["daily-resting-heart-rate"] == (
        'daily_resting_heart_rate.date >= "2026-06-01" '
        'AND daily_resting_heart_rate.date < "2026-06-04"'
    )


def test_list_data_points_paginates_and_forwards_page_token(modules):
    google_health = modules["google_health"]
    pages = [
        {"dataPoints": [{"name": "a"}], "nextPageToken": "p2"},
        {"dataPoints": [{"name": "b"}]},
    ]
    seen = []

    def request(method, url, *, params=None, headers=None):
        seen.append(dict(params or {}))
        return _Resp(200, pages[len(seen) - 1])

    points = google_health.list_data_points(
        data_type="steps", access_token="t", request=request, page_size=500
    )
    assert [p["name"] for p in points] == ["a", "b"]
    assert "pageToken" not in seen[0]
    assert seen[0]["pageSize"] == 500
    assert seen[1]["pageToken"] == "p2"


def test_list_data_points_stops_at_max_pages(modules):
    google_health = modules["google_health"]

    def request(method, url, *, params=None, headers=None):
        # Every page advertises another token; the cap must stop the loop.
        return _Resp(200, {"dataPoints": [{"name": "x"}], "nextPageToken": "always"})

    points = google_health.list_data_points(
        data_type="steps", access_token="t", request=request, max_pages=3
    )
    assert len(points) == 3


def test_http_request_converts_transport_error_to_api_error(modules, monkeypatch):
    google_health = modules["google_health"]
    from urllib import error as urllib_error

    def boom(*_args, **_kwargs):
        raise urllib_error.URLError("connection refused")

    monkeypatch.setattr(google_health.urllib_request, "urlopen", boom)
    with pytest.raises(google_health.GoogleHealthAPIError, match="transport error"):
        google_health.http_request(
            "GET", "https://health.googleapis.com/v4/x", headers={}
        )


def test_sync_identity_failure_is_best_effort(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    _save_fresh_token(auth)

    result = google_health.sync_google_health(
        request=_fake_request(datapoints=_datapoints_by_type(), identity_status=500),
        today=datetime(2026, 6, 14, tzinfo=timezone.utc),
    )

    assert result["ok"] is True
    assert result["user_id"] is None
    assert result["identity_error"]

    with store.connect() as conn:
        provider_user_id = conn.execute(
            "SELECT provider_user_id FROM health_sample_observations"
        ).fetchone()[0]
        identity_errors = conn.execute(
            "SELECT COUNT(*) FROM sync_errors WHERE object_type = 'identity'"
        ).fetchone()[0]
        run = conn.execute(
            "SELECT status, error_count FROM sync_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()

    # Data types all succeeded; only identity errored -> run ok, rows keep NULL user.
    assert provider_user_id is None
    assert identity_errors == 1
    assert tuple(run) == ("ok", 1)


def test_sync_raises_when_token_has_no_access_token(modules):
    store = modules["store"]
    google_health = modules["google_health"]
    auth = modules["auth"]
    store.initialize()
    auth.save_token(
        {
            "access_token": "",
            "refresh_token": "r",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    called = []

    def request(*_args, **_kwargs):
        called.append(1)
        return _Resp(200, {})

    # Reference the exception class through google_health's own bound auth module so
    # the assertion matches the class actually raised, regardless of test ordering.
    with pytest.raises(google_health.google_health_auth.GoogleHealthNotConnected):
        google_health.sync_google_health(
            request=request, today=datetime(2026, 6, 14, tzinfo=timezone.utc)
        )
    assert called == []  # raised before any HTTP call
