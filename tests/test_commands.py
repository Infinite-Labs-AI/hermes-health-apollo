from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path


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
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_name}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"health_data_{name}_script", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeCron:
    create_calls: list
    jobs: list
    remove_calls: list

    def create_job(self, **kwargs):
        self.create_calls.append(kwargs)
        self.jobs.append({"id": f"job-{len(self.jobs) + 1}", "enabled": True, **kwargs})

    def list_jobs(self, include_disabled: bool = False):
        return self.jobs

    def remove_job(self, job_id: str):
        self.remove_calls.append(job_id)
        original_len = len(self.jobs)
        self.jobs = [
            job
            for job in self.jobs
            if job.get("id") != job_id and job.get("name") != job_id
        ]
        return len(self.jobs) < original_len


def install_fake_cron(monkeypatch, jobs=None):
    fake_cron = FakeCron(create_calls=[], jobs=list(jobs or []), remove_calls=[])
    hermes_cli = types.ModuleType("hermes_cli")
    cron = types.ModuleType("hermes_cli.cron")
    jobs = types.ModuleType("hermes_cli.cron.jobs")
    jobs.create_job = fake_cron.create_job
    jobs.list_jobs = fake_cron.list_jobs
    jobs.remove_job = fake_cron.remove_job
    cron.jobs = jobs
    hermes_cli.cron = cron
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.cron", cron)
    monkeypatch.setitem(sys.modules, "hermes_cli.cron.jobs", jobs)
    return fake_cron


def test_setup_installs_fail_soft_launcher_under_hermes_scripts(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    monkeypatch.setattr(commands, "_hermes_command_prefix", lambda: ["/opt/hermes/bin/hermes"])

    result = commands.setup()
    launcher = tmp_path / "scripts" / "health_sync.py"
    launcher_text = launcher.read_text(encoding="utf-8")

    assert result["launcher"] == str(launcher)
    assert launcher.exists()
    assert '"/opt/hermes/bin/hermes", "health", "sync"' in launcher_text
    assert "return result.returncode" in launcher_text


def test_health_ask_prompt_keeps_simple_sleep_questions_on_the_direct_path():
    commands = load_module("commands")

    prompt = commands._health_ask_prompt(
        "How did I sleep last night?",
        sync_result={"ran": False, "skipped": "Recent health sync is still fresh."},
        days=3,
    )

    assert "Answer using the health-coach skill and health-data tools." in prompt
    assert "For cross-domain explanations" not in prompt


def test_setup_adds_skills_external_dir_and_cron_defensively(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    config.write_text("plugins:\n  enabled:\n    - health-data\n", encoding="utf-8")
    fake_cron = install_fake_cron(monkeypatch)
    commands = load_module("commands")

    result = commands.setup()

    skills_dir = str(ROOT / "skills")
    config_text = config.read_text(encoding="utf-8")
    assert result["skills_dir"] == skills_dir
    assert "skills:" in config_text
    assert "external_dirs:" in config_text
    assert f"    - {skills_dir}" in config_text
    assert fake_cron.create_calls == [
        {
            "prompt": None,
            "schedule": "every 6h",
            "name": "health-data-sync",
            "script": "health_sync.py",
            "no_agent": True,
        }
    ]


def test_setup_skips_existing_health_sync_cron(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_cron = install_fake_cron(
        monkeypatch,
        jobs=[
            {
                "id": "existing",
                "name": "health-data-sync",
                "schedule": {"kind": "interval", "minutes": 360},
                "script": "health_sync.py",
                "no_agent": True,
                "enabled": True,
            }
        ],
    )
    commands = load_module("commands")

    result = commands.setup()

    assert result["cron_registered"] is True
    assert fake_cron.create_calls == []
    assert fake_cron.remove_calls == []


def test_setup_replaces_stale_health_sync_cron(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_cron = install_fake_cron(
        monkeypatch,
        jobs=[
            {
                "id": "stale",
                "name": "health-data-sync",
                "schedule": {"kind": "interval", "minutes": 30},
                "script": "other.py",
                "no_agent": False,
                "enabled": True,
            }
        ],
    )
    commands = load_module("commands")

    result = commands.setup()

    assert result["cron_registered"] is True
    assert fake_cron.remove_calls == ["stale"]
    assert fake_cron.create_calls == [
        {
            "prompt": None,
            "schedule": "every 6h",
            "name": "health-data-sync",
            "script": "health_sync.py",
            "no_agent": True,
        }
    ]


def test_uninstall_removes_all_health_sync_cron_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fake_cron = install_fake_cron(
        monkeypatch,
        jobs=[
            {"id": "first", "name": "health-data-sync"},
            {"id": "second", "name": "health-data-sync"},
            {"id": "other", "name": "unrelated"},
        ],
    )
    commands = load_module("commands")

    assert commands.unregister_sync_cron() is True
    assert fake_cron.remove_calls == ["first", "second"]
    assert fake_cron.jobs == [{"id": "other", "name": "unrelated"}]


def test_setup_does_not_crash_when_cron_import_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.setup()

    assert result["ok"] is True
    assert (tmp_path / "scripts" / "health_sync.py").exists()


def test_uninstall_requires_confirmation_before_purge(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    commands.setup()
    database = tmp_path / "health.db"
    database.write_text("not really sqlite", encoding="utf-8")
    for suffix in ["-wal", "-shm", "-journal"]:
        database.with_name(f"{database.name}{suffix}").write_text(
            "sidecar", encoding="utf-8"
        )
    (tmp_path / "oura_token.json").write_text("{}", encoding="utf-8")
    (tmp_path / "oura_token.json.pending").write_text("{}", encoding="utf-8")
    (tmp_path / "oura_token.json.lock").write_text("", encoding="utf-8")
    (tmp_path / "oura_oauth_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "google_token.json").write_text("{}", encoding="utf-8")
    (tmp_path / "google_client_secret.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text(
        'HERMES_OURA_CLIENT_ID="client-id"\n'
        'HERMES_OURA_CLIENT_SECRET="client-secret"\n'
        'KEEP_ME="yes"\n',
        encoding="utf-8",
    )

    result = commands.uninstall(purge=True)

    assert result["ok"] is False
    assert result["confirmation_required"] is True
    assert result["purged"] is False
    assert database.exists()
    assert (tmp_path / "google_token.json").exists()
    assert (tmp_path / "google_client_secret.json").exists()
    assert not (tmp_path / "scripts" / "health_sync.py").exists()
    assert str(ROOT / "skills") not in (tmp_path / "config.yaml").read_text(
        encoding="utf-8"
    )

    confirmed = commands.uninstall(purge=True, yes=True)

    assert confirmed["ok"] is True
    assert confirmed["purged"] is True
    assert not database.exists()
    assert not (tmp_path / "health.db-wal").exists()
    assert not (tmp_path / "health.db-shm").exists()
    assert not (tmp_path / "health.db-journal").exists()
    assert not (tmp_path / "oura_token.json").exists()
    assert not (tmp_path / "oura_token.json.pending").exists()
    assert not (tmp_path / "oura_token.json.lock").exists()
    assert not (tmp_path / "oura_oauth_state.json").exists()
    assert not (tmp_path / "google_token.json").exists()
    assert not (tmp_path / "google_client_secret.json").exists()
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HERMES_OURA" not in env_text
    assert 'KEEP_ME="yes"' in env_text


def test_uninstall_removes_launcher_skill_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    commands.setup()

    result = commands.uninstall()

    assert result["ok"] is True
    assert result["purged"] is False
    assert not (tmp_path / "scripts" / "health_sync.py").exists()
    assert str(ROOT / "skills") not in (tmp_path / "config.yaml").read_text(
        encoding="utf-8"
    )


def test_cli_health_dispatches_advertised_actions(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    setup_result = commands.cli_health("setup")
    status_result = commands.cli_health("status")
    sync_result = commands.cli_health("sync")
    connect_result = commands.cli_health("connect")
    uninstall_result = commands.cli_health("uninstall --purge")
    confirmed_uninstall = commands.cli_health("uninstall --purge --yes")

    assert setup_result["ok"] is True
    assert status_result["ok"] is True
    assert sync_result["ok"] is True
    assert "Run `hermes health connect`" in sync_result["oura"]["skipped"]
    assert connect_result["ok"] is False
    assert connect_result["registration_url"].endswith("/oauth/applications")
    assert connect_result["redirect_uri"] == "http://localhost:43828/callback"
    assert connect_result["required_portal_scopes"] == [
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
    ]
    assert "Client ID" in connect_result["guidance"]
    assert uninstall_result["confirmation_required"] is True
    assert confirmed_uninstall["purged"] is True


def test_setup_recommends_sources_from_goals_and_privacy_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.setup(
        goals=["sleep", "stress", "email/calendar correlations", "food"],
        already_uses=["oura", "google_workspace"],
        privacy_email_body=False,
        privacy_precise_location=False,
    )

    assert result["ok"] is True
    assert result["setup_run"]["status"] == "in_progress"
    assert [card["provider"] for card in result["recommendations"]] == [
        "oura",
        "google_calendar",
        "gmail",
        "manual_food",
    ]
    assert all(card["default_enabled"] is True for card in result["recommendations"])
    assert "precise_location" not in [card["provider"] for card in result["recommendations"]]
    assert result["sources"]["oura"]["state"] in {"connected", "needs_auth", "needs_setup", "needs_sync"}
    assert result["sources"]["gmail"]["privacy_mode"] == "metadata_only"


def test_setup_run_resumes_pending_connect_sync_verify_handoffs(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    first = commands.setup(goals=["sleep"], already_uses=["oura"])
    resumed = commands.setup(setup_run_id=first["setup_run"]["id"])

    assert resumed["setup_run"]["id"] == first["setup_run"]["id"]
    assert resumed["setup_run"]["next_action"] in {
        "connect:oura",
        "sync:oura",
        "verify:oura",
    }


def test_status_reports_primary_chat_guidance_and_per_source_freshness(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.status()

    assert result["ok"] is True
    assert result["primary_path"] == "normal_hermes_chat"
    assert result["admin_commands"] == ["setup", "status", "sync"]
    assert result["freshness_policy_hours"] == 6
    assert 'hermes`, then ask "How did I sleep last night?"' in result["guidance"]
    assert "Oura developer app" in result["guidance"]
    assert "Google Workspace helper" in result["guidance"]
    assert "http://localhost:1/" in result["guidance"]
    assert "hermes health ask" in result["debug_command"]


def test_status_includes_local_install_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    install_root = tmp_path / "plugins" / "health-data"
    install_root.mkdir(parents=True)
    metadata = {
        "version": "0.1.0",
        "commit": "abc123",
        "dirty": True,
        "source_path": "/workspace/health-data",
    }
    (install_root / ".health-data-install.json").write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    monkeypatch.setattr(commands, "_plugin_root", lambda: install_root, raising=False)

    result = commands.status()

    assert result["installed_plugin"]["installed"] is True
    assert result["installed_plugin"]["version"] == "0.1.0"
    assert result["installed_plugin"]["commit"] == "abc123"
    assert result["installed_plugin"]["dirty"] is True
    assert result["installed_plugin"]["source_path"] == "/workspace/health-data"


def test_status_reports_sync_cron_will_not_run_when_gateway_is_stopped(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    install_fake_cron(
        monkeypatch,
        jobs=[
            {
                "id": "sync",
                "name": "health-data-sync",
                "schedule": {"kind": "interval", "minutes": 360},
                "script": "health_sync.py",
                "no_agent": True,
                "enabled": True,
            }
        ],
    )
    commands = load_module("commands")

    def fake_run(args, **kwargs):
        assert args[-2:] == ["cron", "status"]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "Gateway is not running - cron jobs will NOT fire\n"
                "To enable automatic execution: hermes gateway install\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    result = commands.status()

    assert result["sync_cron"]["registered"] is True
    assert result["sync_cron"]["expected"] is True
    assert result["sync_cron"]["gateway"]["running"] is False
    assert result["sync_cron"]["will_run_automatically"] is False
    assert "hermes gateway install" in result["sync_cron"]["guidance"]


def test_status_explains_health_sync_cron_is_not_reminder_scheduling(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.status()

    assert result["reminders"]["health_sync_is_reminder_scheduler"] is False
    assert "remind me to eat every day at 1pm" in result["reminders"]["example"]
    assert "hermes cron" in result["reminders"]["guidance"]


def test_local_install_copies_git_visible_workspace_and_writes_metadata(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.install_local()

    destination = tmp_path / "plugins" / "health-data"
    metadata_path = destination / ".health-data-install.json"
    assert result["ok"] is True
    assert result["destination"] == str(destination)
    assert (destination / "commands.py").exists()
    assert (destination / "plugin.yaml").exists()
    assert not (destination / "uv.lock").exists()
    assert not (destination / ".omx").exists()
    assert metadata_path.exists()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["version"] == "0.1.0"
    assert "installed_at" in metadata
    assert "commit" in metadata


def test_local_install_copies_only_tracked_non_secret_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    commands = load_module("commands")
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text("version: 9.9.9\n", encoding="utf-8")
    (source / "commands.py").write_text("# tracked\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (source / "client_secret_google.json").write_text("{}", encoding="utf-8")
    (source / "loose_module.py").write_text("# untracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "add", "plugin.yaml", "commands.py"],
        check=True,
        capture_output=True,
    )

    result = commands.install_local(source=source)

    destination = Path(result["destination"])
    assert result["ok"] is True
    assert (destination / "plugin.yaml").exists()
    assert (destination / "commands.py").exists()
    assert not (destination / ".env").exists()
    assert not (destination / "client_secret_google.json").exists()
    assert not (destination / "loose_module.py").exists()
    assert not commands._is_installable_relative_path(Path(".env"))
    assert not commands._is_installable_relative_path(Path("client_secret_google.json"))


def test_local_install_refuses_to_replace_non_plugin_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    commands = load_module("commands")
    destination = tmp_path / "not-health"
    destination.mkdir()
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = commands.install_local(destination=destination)

    assert result["ok"] is False
    assert "destination not named health-data" in result["error"]
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_local_install_refuses_existing_unmarked_health_data_destination(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    commands = load_module("commands")
    destination = tmp_path / "health-data"
    destination.mkdir()
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = commands.install_local(destination=destination)

    assert result["ok"] is False
    assert "without health-data install metadata" in result["error"]
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_verify_local_install_reports_extra_destination_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    commands = load_module("commands")
    verifier = load_script("verify_local_install")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "plugin.yaml").write_text("version: 9.9.9\n", encoding="utf-8")
    (destination / "plugin.yaml").write_text("version: 9.9.9\n", encoding="utf-8")
    (destination / "stale.py").write_text("# stale\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(source), "add", "plugin.yaml"],
        check=True,
        capture_output=True,
    )

    drift = verifier._install_drift(commands, source, destination)

    assert drift["has_drift"] is True
    assert drift["extra"] == ["stale.py"]


def test_cli_health_setup_status_sync_are_the_documented_short_admin_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    monkeypatch.setattr(
        commands.oura,
        "sync_oura",
        lambda **_kwargs: {"ok": True, "daily_rows": 0, "sleep_sessions": 0},
    )
    monkeypatch.setattr(
        commands.context,
        "sync_google_workspace",
        lambda *_args, **_kwargs: {"ok": True, "calendar_days": 0, "email_days": 0},
    )

    setup = commands.cli_health("setup")
    status = commands.cli_health("status")
    sync = commands.cli_health("sync")
    reminder = commands.cli_health("reminder")

    assert setup["admin_commands"] == ["setup", "status", "sync"]
    assert status["primary_path"] == "normal_hermes_chat"
    assert sync["freshness_policy_hours"] == 6
    assert reminder["ok"] is True
    assert reminder["health_sync_is_reminder_scheduler"] is False


def test_connect_does_not_echo_supplied_oura_client_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.connect(
        client_id="client-id",
        client_secret="super-secret",
        loopback_timeout=0,
    )

    assert "super-secret" not in json.dumps(result, sort_keys=True)


def test_cli_health_connect_forwards_explicit_scopes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.cli_health(
        "connect --client-id client-id --client-secret client-secret "
        "--scopes 'daily session spo2' --manual"
    )

    assert result["requested_oauth_scopes"] == "daily session spo2"
    assert "scope=daily+session+spo2" in result["authorize_url"]


def test_cli_health_connect_forwards_loopback_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    captured = {}

    def fake_connect_oura(**kwargs):
        captured.update(kwargs)
        return {"ok": False}

    monkeypatch.setattr(commands.oura, "connect_oura", fake_connect_oura)

    result = commands.cli_health("connect --loopback-timeout 0.5")

    assert result["ok"] is False
    assert captured["loopback_timeout"] == 0.5


def test_cli_health_sync_forwards_backfill_days(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    captured = {}
    google_window = {}

    def fake_sync_oura(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "daily_rows": 0, "sleep_sessions": 0}

    monkeypatch.setattr(commands.oura, "sync_oura", fake_sync_oura)
    monkeypatch.setattr(
        commands.context,
        "sync_google_workspace",
        lambda *args, **kwargs: google_window.update({"args": args, "kwargs": kwargs})
        or {"ok": True, "skipped": "Google Workspace is not connected."},
    )

    result = commands.cli_health("sync --days 30")

    assert result["ok"] is True
    assert captured["lookback_days"] == 30
    assert google_window["kwargs"]["days"] == 30


def test_cli_health_sync_includes_today_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    google_window = {}

    monkeypatch.setattr(
        commands.oura,
        "sync_oura",
        lambda **_kwargs: {"ok": True, "daily_rows": 0, "sleep_sessions": 0},
    )
    monkeypatch.setattr(
        commands.context,
        "sync_google_workspace",
        lambda *args, **kwargs: google_window.update({"args": args, "kwargs": kwargs})
        or {"ok": True, "calendar_days": 0, "email_days": 0, "skipped": None},
    )

    result = commands.cli_health("sync")

    assert result["ok"] is True
    assert google_window["kwargs"]["days"] == 2


def test_health_ask_force_syncs_and_runs_health_coach(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    sync_calls = []
    run_calls = []

    def fake_sync_now(**kwargs):
        sync_calls.append(kwargs)
        return {"ok": True, "oura": {"ok": True}, "google_workspace": {"ok": True}}

    def fake_run(args, **kwargs):
        run_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="Use recovery today.\n", stderr="")

    monkeypatch.setattr(commands, "sync_now", fake_sync_now)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    result = commands.ask(question="Why was I stressed yesterday?", days=2, force_sync=True)

    assert result["ok"] is True
    assert result["answer"] == "Use recovery today."
    assert sync_calls == [{"days": 2}]
    assert result["sync"]["ran"] is True
    assert result["sync"]["reason"] == "forced"
    assert run_calls[0][0][-4:-1] == ["--skills", "health-coach", "-z"]
    assert "Why was I stressed yesterday?" in run_calls[0][0][-1]


def test_health_ask_skips_sync_when_recent_state_is_fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    commands.store.initialize()
    with commands.store.connect() as conn:
        conn.execute(
            """
            INSERT INTO sync_state(provider, last_sync_date, last_status, updated_at)
            VALUES ('oura', '2026-06-11', 'ok', ?)
            """,
            (commands.datetime.now(commands.UTC).isoformat(),),
        )
    run_calls = []

    def fail_sync_now(**_kwargs):
        raise AssertionError("fresh question should not sync")

    def fake_run(args, **kwargs):
        run_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="Cached answer\n", stderr="")

    monkeypatch.setattr(commands, "sync_now", fail_sync_now)
    monkeypatch.setattr(commands.context, "google_workspace_available", lambda: False)
    monkeypatch.setattr(commands.oura, "token_path", lambda: tmp_path / "oura_token.json")
    commands.oura.token_path().write_text("{}", encoding="utf-8")
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    result = commands.ask(question="Why did I sleep badly?")

    assert result["ok"] is True
    assert result["answer"] == "Cached answer"
    assert result["sync"]["ran"] is False
    assert result["sync"]["skipped"] == "Recent health sync is still fresh."
    assert run_calls[0][0][-4:-1] == ["--skills", "health-coach", "-z"]


def test_health_ask_resolves_runtime_hermes_without_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    fake_python = bin_dir / "python3"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_hermes = bin_dir / "hermes"
    fake_hermes.write_text("#!/bin/sh\n", encoding="utf-8")
    run_calls = []

    def fake_run(args, **kwargs):
        run_calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="Answer\n", stderr="")

    monkeypatch.setattr(commands.sys, "executable", str(fake_python))
    monkeypatch.setattr(commands.sys, "argv", ["pytest"])
    monkeypatch.setattr(commands.shutil, "which", lambda _name: None)
    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    result = commands.ask(question="How did I sleep?", no_sync=True)

    assert result["ok"] is True
    assert run_calls[0][0][0] == str(fake_hermes)
    assert run_calls[0][0][-4:-1] == ["--skills", "health-coach", "-z"]


def test_health_ask_redacts_debug_command_and_stderr(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    def fail_run(args, **kwargs):
        raise OSError(
            "failed under "
            + "/"
            + "Users/alice/bin/hermes with "
            + "access_"
            + "token=secret-value"
        )

    monkeypatch.setattr(commands.subprocess, "run", fail_run)

    question = "Why did my resting heart rate spike?"
    result = commands.ask(question=question, no_sync=True)

    assert result["ok"] is False
    assert result["question"] == question
    assert question not in result["fallback_command"]
    assert "-z '[REDACTED]'" in result["fallback_command"]
    serialized = json.dumps(result, sort_keys=True)
    assert "/" + "Users/alice" not in serialized
    assert "secret-value" not in serialized


def test_health_ask_redacts_subprocess_stderr(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=(
                "failed under "
                + "/"
                + "Users/alice/bin/hermes with "
                + "client_"
                + "secret=secret-value"
            ),
        )

    monkeypatch.setattr(commands.subprocess, "run", fake_run)

    result = commands.ask(question="How did I sleep?", no_sync=True)

    assert result["ok"] is False
    assert "/" + "Users/alice" not in result["stderr"]
    assert "secret-value" not in result["stderr"]
    assert "[LOCAL_PATH]" in result["stderr"]


def test_cli_health_ask_dispatches_question_without_sync(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    captured = {}

    def fake_ask(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "answer": "Answer"}

    monkeypatch.setattr(commands, "ask", fake_ask)

    result = commands.cli_health("ask --no-sync --days 1 why was I stressed")

    assert result == {"ok": True, "answer": "Answer"}
    assert captured == {
        "question": "why was I stressed",
        "days": "1",
        "no_sync": True,
        "force_sync": False,
    }


def test_cli_health_connect_google_forwards_oauth_args(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    captured = {}

    def fake_connect_google_workspace(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "connected": False, "authorize_url": "https://example.test/auth"}

    monkeypatch.setattr(commands.context, "connect_google_workspace", fake_connect_google_workspace)

    result = commands.cli_health(
        "connect-google --client-secret /tmp/client.json --auth-url --check-live --install-deps --open"
    )

    assert result["ok"] is True
    assert captured == {
        "client_secret": "/tmp/client.json",
        "auth_code": None,
        "auth_url": True,
        "check": False,
        "check_live": True,
        "install_deps": True,
        "open_browser": True,
        "revoke": False,
    }


def test_cli_health_connect_google_health_forwards_oauth_args(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    captured = {}

    def fake_connect(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "connected": False, "authorize_url": "https://accounts.google.com/x"}

    monkeypatch.setattr(commands.google_health_auth, "connect_google_health", fake_connect)

    result = commands.cli_health(
        "connect-google-health --client-id cid --client-secret csecret --code abc --state st"
    )

    assert result["ok"] is False
    assert captured == {
        "client_id": "cid",
        "client_secret": "csecret",
        "code": "abc",
        "state": "st",
    }


def test_cli_health_disconnect_google_health_removes_token(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    commands.google_health_auth.save_token(
        {"access_token": "gh-access", "refresh_token": "gh-refresh"}
    )
    assert commands.google_health_auth.token_path().exists()

    result = commands.cli_health("disconnect-google-health")

    assert result["ok"] is True
    assert result["connected"] is False
    assert not commands.google_health_auth.token_path().exists()
    assert str(commands.google_health_auth.token_path()) in result["removed"]


def test_sync_now_includes_google_health_and_degrades_when_not_connected(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    monkeypatch.setattr(commands.oura, "sync_oura", lambda **_k: {"ok": True})
    monkeypatch.setattr(
        commands.context, "sync_google_workspace", lambda *_a, **_k: {"ok": False}
    )

    def not_connected(**_kwargs):
        raise commands.google_health_auth.GoogleHealthNotConnected("not connected")

    monkeypatch.setattr(commands.google_health, "sync_google_health", not_connected)

    result = commands.cli_health("sync")

    assert "google_health" in result
    assert result["google_health"]["ok"] is False
    assert "skipped" in result["google_health"]
    assert result["ok"] is True  # oura succeeded


def test_sync_now_reports_google_health_api_error(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")
    monkeypatch.setattr(commands.oura, "sync_oura", lambda **_k: {"ok": False, "skipped": "x"})
    monkeypatch.setattr(
        commands.context, "sync_google_workspace", lambda *_a, **_k: {"ok": False}
    )

    def api_error(**_kwargs):
        raise commands.google_health.GoogleHealthAPIError("boom")

    monkeypatch.setattr(commands.google_health, "sync_google_health", api_error)

    result = commands.cli_health("sync")

    assert result["google_health"] == {"ok": False, "error": "boom"}


def test_connect_google_health_does_not_echo_client_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.connect_google_health(
        client_id="client-id", client_secret="super-secret-value"
    )

    assert "super-secret-value" not in json.dumps(result, sort_keys=True)


def test_cli_health_sync_reports_invalid_backfill_days(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.cli_health("sync --days nope")

    assert result["ok"] is False
    assert result["oura"] == {
        "ok": False,
        "error": "Oura sync lookback_days must be an integer.",
    }
    assert result["google_workspace"]["ok"] is False


def test_cli_health_sync_reports_invalid_backfill_date(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    result = commands.cli_health("sync --start-date not-a-date --end-date 2026-06-10")

    assert result["ok"] is False
    assert result["oura"] == {
        "ok": False,
        "error": "Oura sync start_date must be YYYY-MM-DD.",
    }


def test_slash_health_dispatches_raw_args(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = load_module("commands")

    assert commands.slash_health("status")["ok"] is True
    assert commands.slash_health("unknown")["ok"] is False
