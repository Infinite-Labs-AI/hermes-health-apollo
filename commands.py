from __future__ import annotations

import contextlib
import importlib.resources
import importlib.util
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import audio, context, location, onboarding, oura, store

SYNC_CRON_JOB_NAME = "health-data-sync"
SYNC_CRON_SCHEDULE = "every 6h"
SYNC_CRON_SCRIPT = "health_sync.py"
SYNC_CRON_MINUTES = 360
ASK_SYNC_STALE_SECONDS = SYNC_CRON_MINUTES * 60
INSTALL_METADATA_FILE = ".health-data-install.json"
LOCAL_INSTALL_EXCLUDES = {
    ".context",
    ".git",
    ".mypy_cache",
    ".omx",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "AGENTS.md",
    "uv.lock",
}
LOCAL_INSTALL_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "google_client_secret.json",
    "google_token.json",
    "oura_token.json",
}
LOCAL_INSTALL_SECRET_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".p8",
}
_SENSITIVE_COMMAND_FLAGS = {"-z", "--auth-code", "--client-secret"}
_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)("
    + re.escape("/" + "Users/")
    + r"[^/\\ \t\"']+|"
    + re.escape("/" + "home/")
    + r"[^/\\ \t\"']+|"
    + re.escape("C:" + "\\Users\\")
    + r"[^/\\ \t\"']+)"
)
_OAUTH_CODE_PATTERN = re.compile(r"(?i)([?&]code=)[^&\s]+")
_TOKEN_VALUE_PATTERN = re.compile(
    r"(?i)\b("
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"api[_-]?key|secret[_-]?key|private[_-]?key|password"
    r")\b['\"]?(\s*[:=]\s*['\"]?)[^'\"\s,}]+"
)
_GOOGLE_TOKEN_PATTERN = re.compile(r"\b(?:ya29\.[A-Za-z0-9_-]{20,}|GOCSPX-[A-Za-z0-9_-]{10,})\b")


def setup(*_args, **kwargs) -> dict:
    store.initialize()
    launcher = install_sync_launcher()
    skills_dir = plugin_skills_dir()
    add_external_skills_dir(skills_dir)
    cron_registered = register_sync_cron()
    setup_run = onboarding.start_or_resume_setup_run(
        setup_run_id=kwargs.get("setup_run_id"),
        goals=kwargs.get("goals"),
        already_uses=kwargs.get("already_uses"),
        privacy_email_body=bool(kwargs.get("privacy_email_body", False)),
        privacy_precise_location=bool(kwargs.get("privacy_precise_location", False)),
        routine=kwargs.get("routine"),
        timezone=kwargs.get("timezone"),
    )
    return {
        "ok": True,
        "database": str(store.database_path()),
        "launcher": str(launcher),
        "skills_dir": str(skills_dir),
        "cron_registered": cron_registered,
        "sync_cron": sync_cron_status(),
        "installed_plugin": installed_plugin_metadata(),
        "reminders": reminder_guidance(),
        "admin_commands": onboarding.ADMIN_COMMANDS,
        "setup_run": setup_run,
        "next_action": setup_run["next_action"],
        "recommendations": setup_run["recommendations"],
        "sources": setup_run["sources"],
        "guidance": "Connect recommended sources, sync them, then verify queryability.",
    }


def sync_now(*_args, **kwargs) -> dict:
    store.initialize()
    start_date = kwargs.get("start_date")
    end_date = kwargs.get("end_date")
    sync_args_valid = True
    try:
        lookback_days = _int_or_none(kwargs.get("lookback_days") or kwargs.get("days"))
        oura_result = oura.sync_oura(
            lookback_days=lookback_days,
            start_date=start_date,
            end_date=end_date,
        )
    except oura.OuraNotConnected as exc:
        oura_result = {"ok": False, "skipped": str(exc)}
    except oura.OuraAPIError as exc:
        lookback_days = None
        sync_args_valid = False
        oura_result = {"ok": False, "error": str(exc)}
    if sync_args_valid:
        google_start, google_days = _google_sync_window(
            lookback_days=lookback_days,
            start_date=start_date,
            end_date=end_date,
        )
        google_result = context.sync_google_workspace(google_start, days=google_days)
    else:
        google_result = {
            "ok": False,
            "calendar_days": 0,
            "email_days": 0,
            "skipped": "Google Workspace sync skipped because sync arguments were invalid.",
        }
    try:
        audio_sync = audio.sync_audio(trigger_kind="manual")
        audio_result = {"ok": audio_sync["status"] == "ok", **audio_sync}
    except audio.AudioNotConnected as exc:
        audio_result = {"ok": False, "skipped": str(exc)}
    except audio.AudioSyncError as exc:
        audio_result = {"ok": False, "error": str(exc)}
    try:
        location_sync = location.sync_location(trigger_kind="manual")
        location_result = {"ok": location_sync.get("status") == "ok", **location_sync}
    except location.LocationNotConnected as exc:
        location_result = {"ok": False, "skipped": str(exc)}
    except location.LocationSyncError as exc:
        location_result = {"ok": False, "error": str(exc)}
    return {
        "ok": bool(oura_result.get("ok"))
        or bool(google_result.get("ok"))
        or bool(audio_result.get("ok"))
        or bool(location_result.get("ok")),
        "freshness_policy_hours": onboarding.FRESHNESS_POLICY_HOURS,
        "oura": oura_result,
        "google_workspace": google_result,
        "audio": audio_result,
        "location": location_result,
    }


def connect_audio(*_args, **_kwargs) -> dict:
    return audio.connect_audio()


def connect_location(*_args, **_kwargs) -> dict:
    return location.connect_location()


def calibrate_audio(*_args, **kwargs) -> dict:
    try:
        suggestion = audio.calibrate_suggestion(seconds=kwargs.get("seconds"))
    except audio.AudioNotConnected as error:
        return {"status": "unavailable", "reason": str(error)}
    except audio.AudioSyncError as error:
        return {"status": "error", "reason": str(error)}
    return {"status": "ok", **suggestion}


def set_audio_silence_threshold(*_args, **kwargs) -> dict:
    return audio.set_silence_threshold(threshold_db=float(kwargs["threshold_db"]))


def connect(*_args, **kwargs) -> dict:
    return oura.connect_oura(
        client_id=kwargs.get("client_id"),
        client_secret=kwargs.get("client_secret"),
        code=kwargs.get("code"),
        state=kwargs.get("state"),
        scopes=kwargs.get("scopes"),
        loopback_timeout=kwargs.get("loopback_timeout", 120),
    )


def connect_google(*_args, **kwargs) -> dict:
    return context.connect_google_workspace(
        client_secret=kwargs.get("client_secret"),
        auth_code=kwargs.get("auth_code") or kwargs.get("code"),
        auth_url=bool(kwargs.get("auth_url")),
        check=bool(kwargs.get("check")),
        check_live=bool(kwargs.get("check_live")),
        install_deps=bool(kwargs.get("install_deps")),
        open_browser=bool(kwargs.get("open_browser")),
        revoke=bool(kwargs.get("revoke")),
    )


def calendar_peek(days: int = 3, *_args, **_kwargs) -> dict:
    return context.calendar_peek(days=days)


def ask(*_args, **kwargs) -> dict:
    question = _question_text(kwargs.get("question"), _args)
    if not question:
        return {
            "ok": False,
            "error": "Question required.",
            "guidance": (
                'Normal Hermes chat is the primary path. Use `hermes -z "Why was I '
                'stressed yesterday?"`; `hermes health ask ...` is debug/compatibility.'
            ),
        }

    days = _int_or_default(kwargs.get("days"), 3)
    sync_result = _sync_for_question(
        question,
        days=days,
        skip_sync=bool(kwargs.get("no_sync") or kwargs.get("skip_sync")),
        force_sync=bool(kwargs.get("force_sync") or kwargs.get("sync")),
    )

    prompt = _health_ask_prompt(question, sync_result=sync_result, days=days)
    command = [*_hermes_command_prefix(), "--skills", "health-coach", "-z", prompt]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "ok": False,
            "question": question,
            "sync": sync_result,
            "error": _redact_tool_text(f"Could not run Hermes health-coach question: {exc}"),
            "fallback_command": _redacted_command_text(command),
            "mode": "debug_compatibility",
        }

    answer = result.stdout.strip()
    return {
        "ok": result.returncode == 0,
        "question": question,
        "answer": answer,
        "stderr": _redact_tool_text(result.stderr.strip()),
        "returncode": result.returncode,
        "sync": sync_result,
        "mode": "debug_compatibility",
        "guidance": "Normal Hermes chat is the primary path; this wrapper is for debug/compatibility.",
    }


def slash_health(*_args, **_kwargs) -> dict:
    return _dispatch(_args[0] if _args else "")


def cli_health(*_args, **_kwargs) -> dict:
    return _dispatch(_args[0] if _args else "")


def status() -> dict:
    store.initialize()
    snapshot = onboarding.status_snapshot()
    return {
        "ok": True,
        "database": str(store.database_path()),
        "installed_plugin": installed_plugin_metadata(),
        "sync_cron": sync_cron_status(),
        "reminders": reminder_guidance(),
        **snapshot,
    }


def install_local(*_args, **kwargs) -> dict:
    source = Path(kwargs.get("source") or _plugin_root()).resolve()
    destination = Path(
        kwargs.get("destination") or store.hermes_home() / "plugins" / "health-data"
    ).resolve()
    if source == destination or source in destination.parents:
        return {
            "ok": False,
            "error": "Refusing to install health-data into itself.",
            "source": str(source),
            "destination": str(destination),
        }

    unsafe_reason = _unsafe_install_destination_reason(destination)
    if unsafe_reason:
        return {
            "ok": False,
            "error": unsafe_reason,
            "source": str(source),
            "destination": str(destination),
        }

    files = _installable_source_files(source)
    if not files:
        return {
            "ok": False,
            "error": "No installable health-data files found.",
            "source": str(source),
            "destination": str(destination),
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.install-", dir=destination.parent)
    )
    try:
        for relative in files:
            source_path = source / relative
            target_path = temp_root / relative
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
        metadata = _local_install_metadata(source, destination)
        (temp_root / INSTALL_METADATA_FILE).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        temp_root.replace(destination)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise

    return {
        "ok": True,
        "source": str(source),
        "destination": str(destination),
        "files_copied": len(files),
        "metadata": metadata,
    }


def uninstall(*_args, purge: bool = False, yes: bool = False, **kwargs) -> dict:
    purge = bool(kwargs.get("purge", purge))
    yes = bool(kwargs.get("yes", yes))
    launcher = store.hermes_home() / "scripts" / "health_sync.py"
    removed_launcher = False
    try:
        launcher.unlink()
        removed_launcher = True
    except FileNotFoundError:
        pass

    remove_external_skills_dir(plugin_skills_dir())
    cron_removed = unregister_sync_cron()
    purged = False
    if purge:
        if not yes:
            return {
                "ok": False,
                "launcher_removed": removed_launcher,
                "cron_removed": cron_removed,
                "purged": False,
                "confirmation_required": True,
                "guidance": (
                    "Re-run `hermes health uninstall --purge --yes` to delete "
                    "local health.db, SQLite sidecars, Oura tokens, and stored "
                    "Oura client credentials."
                ),
            }
        purged = bool(_purge_local_health_data())

    return {
        "ok": True,
        "launcher_removed": removed_launcher,
        "cron_removed": cron_removed,
        "purged": purged,
    }


def _dispatch(raw_args: object) -> dict:
    if isinstance(raw_args, (list, tuple)):
        parts = [str(part) for part in raw_args]
    elif isinstance(raw_args, str):
        parts = shlex.split(raw_args)
    elif raw_args:
        parts = [str(raw_args)]
    else:
        parts = []
    action = parts[0] if parts else "status"
    flags = set(parts[1:])
    if action == "setup":
        parsed = _parse_flags(parts[1:])
        return setup(
            setup_run_id=parsed.get("setup-run-id") or parsed.get("setup_run_id"),
        )
    if action == "connect":
        parsed = _parse_flags(parts[1:])
        timeout = parsed.get("loopback-timeout") or parsed.get("loopback_timeout")
        return connect(
            client_id=parsed.get("client-id") or parsed.get("client_id"),
            client_secret=parsed.get("client-secret") or parsed.get("client_secret"),
            code=parsed.get("code"),
            state=parsed.get("state"),
            scopes=parsed.get("scopes"),
            loopback_timeout=0 if parsed.get("manual") else _float_or_default(timeout, 120),
        )
    if action in {"connect-google", "google-connect", "google"}:
        parsed = _parse_flags(parts[1:])
        return connect_google(
            client_secret=parsed.get("client-secret") or parsed.get("client_secret"),
            auth_code=parsed.get("auth-code") or parsed.get("auth_code") or parsed.get("code"),
            auth_url=bool(parsed.get("auth-url") or parsed.get("auth_url")),
            check=bool(parsed.get("check")),
            check_live=bool(parsed.get("check-live") or parsed.get("check_live")),
            install_deps=bool(parsed.get("install-deps") or parsed.get("install_deps")),
            open_browser=bool(
                parsed.get("open-browser") or parsed.get("open_browser") or parsed.get("open")
            ),
            revoke=bool(parsed.get("revoke")),
        )
    if action == "sync":
        parsed = _parse_flags(parts[1:])
        return sync_now(
            lookback_days=parsed.get("days") or parsed.get("lookback-days"),
            start_date=parsed.get("start-date") or parsed.get("start_date"),
            end_date=parsed.get("end-date") or parsed.get("end_date"),
        )
    if action == "ask":
        parsed = _parse_flags(parts[1:])
        question_parts = _strip_flags(parts[1:])
        return ask(
            question=" ".join(question_parts),
            days=parsed.get("days"),
            no_sync=bool(parsed.get("no-sync") or parsed.get("no_sync")),
            force_sync=bool(parsed.get("sync") or parsed.get("force-sync")),
        )
    if action == "status":
        return status()
    if action in {"reminder", "reminders"}:
        return {"ok": True, **reminder_guidance()}
    if action == "uninstall":
        return uninstall(purge="--purge" in flags, yes="--yes" in flags)
    return {
        "ok": False,
        "error": f"Unknown health action: {action}",
        "actions": [
            "setup",
            "connect",
            "connect-google",
            "sync",
            "ask",
            "status",
            "reminder",
            "uninstall",
        ],
    }


def install_sync_launcher() -> Path:
    scripts_dir = store.hermes_home() / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    launcher = scripts_dir / "health_sync.py"
    command_json = json.dumps([*_hermes_command_prefix(), "health", "sync"])
    content = f"""\
from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    command = json.loads({command_json!r})
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        print(f"health-data sync failed: {{exc}}", file=sys.stderr)
        return 1
    output = result.stderr or result.stdout or ""
    if result.returncode != 0:
        print(output or "health-data sync failed", file=sys.stderr)
        if "invalid choice: 'health'" in output or "No such command" in output:
            return 0
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
    launcher.write_text(content, encoding="utf-8")
    if os.name != "nt":
        launcher.chmod(0o700)
    return launcher


def installed_plugin_metadata() -> dict:
    metadata_path = _plugin_root() / INSTALL_METADATA_FILE
    if not metadata_path.exists():
        return {
            "installed": False,
            "metadata_path": str(metadata_path),
            "guidance": "Run `make install-local` from the health-data workspace to refresh the installed plugin copy.",
        }
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "installed": False,
            "metadata_path": str(metadata_path),
            "error": f"Could not read local install metadata: {exc}",
            "guidance": "Re-run `make install-local` from the health-data workspace.",
        }
    metadata["installed"] = True
    metadata["metadata_path"] = str(metadata_path)
    return metadata


def _read_install_metadata(destination: Path) -> dict | None:
    metadata_path = destination / INSTALL_METADATA_FILE
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return metadata if isinstance(metadata, dict) else None


def sync_cron_status() -> dict:
    jobs = _health_sync_cron_jobs()
    expected = [job for job in jobs if _is_expected_sync_cron_job(job)]
    gateway = _cron_gateway_status()
    registered = bool(expected)
    running = gateway.get("running")
    will_run = registered and running is True
    status_payload = {
        "name": SYNC_CRON_JOB_NAME,
        "schedule": SYNC_CRON_SCHEDULE,
        "script": SYNC_CRON_SCRIPT,
        "registered": registered,
        "expected": registered,
        "job_count": len(jobs),
        "gateway": gateway,
        "will_run_automatically": will_run if running is not None else None,
    }
    if not registered:
        status_payload["guidance"] = (
            "Run `hermes health setup` to register the health-data-sync cron job."
        )
    elif running is False:
        status_payload["guidance"] = (
            "health-data-sync is registered, but Hermes gateway is not running, "
            "so scheduled syncs will not fire automatically. Run `hermes gateway run` "
            "for a foreground gateway or `hermes gateway install` to install a user service."
        )
    elif running is None:
        status_payload["guidance"] = (
            "health-data-sync is registered, but gateway status could not be verified. "
            "Run `hermes cron status` or `hermes gateway status` to confirm scheduled jobs can fire."
        )
    else:
        status_payload["guidance"] = "health-data-sync is registered and Hermes gateway appears to be running."
    return status_payload


def reminder_guidance() -> dict:
    return {
        "health_sync_is_reminder_scheduler": False,
        "mechanism": "hermes_cron",
        "example": 'For "remind me to eat every day at 1pm", create an explicit Hermes reminder job.',
        "guidance": (
            "health-data-sync only refreshes local health data. Use the Hermes cron/reminder "
            "path for user-visible reminders, for example: "
            '`hermes cron create --name eat-reminder --deliver local "0 13 * * *" '
            '"Remind me to eat."`'
        ),
    }


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent


def _installable_source_files(source: Path) -> list[Path]:
    git_files = _git_visible_files(source)
    if git_files is not None:
        return [path for path in git_files if _is_installable_relative_path(path)]
    return [
        path
        for path in _walk_source_files(source)
        if _is_installable_relative_path(path)
    ]


def _unsafe_install_destination_reason(destination: Path) -> str | None:
    default_destination = (store.hermes_home() / "plugins" / "health-data").resolve()
    if destination == default_destination:
        return None
    if destination.name != "health-data":
        return "Refusing to install health-data into a destination not named health-data."
    if not destination.exists():
        return None
    metadata = _read_install_metadata(destination)
    if metadata and metadata.get("plugin") == "health-data":
        return None
    return (
        "Refusing to replace an existing destination without "
        "health-data install metadata."
    )


def _git_visible_files(source: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), "ls-files", "--cached"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    files: list[Path] = []
    for line in result.stdout.splitlines():
        relative = Path(line)
        if relative.is_absolute():
            continue
        source_path = source / relative
        if source_path.is_file():
            files.append(relative)
    return files


def _walk_source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(source)
        except ValueError:
            continue
        files.append(relative)
    return files


def _is_installable_relative_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts & LOCAL_INSTALL_EXCLUDES:
        return False
    if path.name == INSTALL_METADATA_FILE:
        return False
    if _looks_like_secret_path(path):
        return False
    return True


def _looks_like_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name in LOCAL_INSTALL_SECRET_NAMES:
        return True
    if name.startswith("client_secret") and name.endswith(".json"):
        return True
    if name.endswith(tuple(LOCAL_INSTALL_SECRET_SUFFIXES)):
        return True
    return False


def _local_install_metadata(source: Path, destination: Path) -> dict:
    return {
        "plugin": "health-data",
        "version": _plugin_version(source),
        "source_path": str(source),
        "destination": str(destination),
        "installed_at": datetime.now(UTC).isoformat(),
        "commit": _git_output(source, "rev-parse", "HEAD") or "unknown",
        "branch": _git_output(source, "rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "dirty": _install_source_dirty(source),
    }


def _plugin_version(source: Path) -> str:
    plugin_yaml = source / "plugin.yaml"
    if not plugin_yaml.exists():
        return "unknown"
    for line in plugin_yaml.read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return "unknown"


def _git_output(source: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(source), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _install_source_dirty(source: Path) -> bool:
    return bool(_git_output(source, "status", "--short", "--untracked-files=no"))


def _cron_gateway_status() -> dict:
    command = [*_hermes_command_prefix(), "cron", "status"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "running": None,
            "checked": False,
            "error": str(exc),
            "command": " ".join(shlex.quote(part) for part in command),
        }
    output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    running = _parse_gateway_running(output, result.returncode)
    return {
        "running": running,
        "checked": True,
        "returncode": result.returncode,
        "summary": _first_nonempty_line(output),
        "command": " ".join(shlex.quote(part) for part in command),
    }


def _parse_gateway_running(output: str, returncode: int) -> bool | None:
    normalized = output.lower()
    if "not running" in normalized or "won't fire" in normalized or "will not fire" in normalized:
        return False
    if "gateway is running" in normalized or "cron scheduler is running" in normalized:
        return True
    if returncode != 0:
        return False
    return None


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def _purge_local_health_data() -> list[str]:
    removed: list[str] = []
    database = store.database_path()
    for path in [
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
        database.with_name(f"{database.name}-journal"),
        oura.token_path(),
        oura.pending_token_path(),
        oura.lock_path(),
        oura.pending_state_path(),
        context.google_token_path(),
        context.google_client_secret_path(),
    ]:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed.append(str(path))
    if oura.clear_oura_client_credentials():
        removed.append(str(oura.env_path()))
    return removed


def plugin_skills_dir() -> Path:
    repo_skills_dir = Path(__file__).resolve().parent / "skills"
    if repo_skills_dir.exists():
        return repo_skills_dir
    try:
        return Path(importlib.resources.files("health_data_assets").joinpath("skills"))
    except (ImportError, ModuleNotFoundError, FileNotFoundError):
        return repo_skills_dir


def add_external_skills_dir(skills_dir: Path) -> None:
    config_path = store.hermes_home() / "config.yaml"
    line = f"    - {skills_dir}"
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if line in text:
        return
    updated = _add_external_dir_to_config(text, line)
    _atomic_write_text(config_path, updated)


def remove_external_skills_dir(skills_dir: Path) -> None:
    config_path = store.hermes_home() / "config.yaml"
    if not config_path.exists():
        return
    line = f"    - {skills_dir}"
    text = config_path.read_text(encoding="utf-8")
    updated_lines = [
        existing
        for existing in text.splitlines()
        if existing.strip() not in {line.strip(), f"- {skills_dir}"}
    ]
    _atomic_write_text(config_path, "\n".join(updated_lines).rstrip() + "\n")


def register_sync_cron() -> bool:
    create_job = _import_cron_function("create_job")
    if create_job is None:
        return False
    existing_jobs = _health_sync_cron_jobs()
    expected_jobs = [job for job in existing_jobs if _is_expected_sync_cron_job(job)]
    remove_jobs = [
        job for job in existing_jobs if not _is_expected_sync_cron_job(job)
    ] + expected_jobs[1:]
    if remove_jobs:
        _remove_cron_jobs(remove_jobs)
    if expected_jobs:
        return True
    attempts = (
        lambda: create_job(
            prompt=None,
            schedule=SYNC_CRON_SCHEDULE,
            name=SYNC_CRON_JOB_NAME,
            script=SYNC_CRON_SCRIPT,
            no_agent=True,
        ),
        lambda: create_job(
            None,
            SYNC_CRON_SCHEDULE,
            name=SYNC_CRON_JOB_NAME,
            script=SYNC_CRON_SCRIPT,
            no_agent=True,
        ),
    )
    return _call_first_supported(attempts)


def unregister_sync_cron() -> bool:
    existing_jobs = _health_sync_cron_jobs()
    if existing_jobs:
        return _remove_cron_jobs(existing_jobs)
    for name in ("delete_job", "remove_job"):
        remove_job = _import_cron_function(name)
        if remove_job is None:
            continue
        attempts = (
            lambda remove_job=remove_job: remove_job(name=SYNC_CRON_JOB_NAME),
            lambda remove_job=remove_job: remove_job(SYNC_CRON_JOB_NAME),
        )
        if _call_first_supported(attempts):
            return True
    return False


def _health_sync_cron_jobs() -> list[dict]:
    list_jobs = _import_cron_function("list_jobs")
    if list_jobs is None:
        return []
    attempts = (
        lambda: list_jobs(include_disabled=True),
        lambda: list_jobs(),
    )
    for attempt in attempts:
        try:
            jobs = attempt()
        except TypeError:
            continue
        except Exception:
            return []
        return [
            job
            for job in jobs
            if isinstance(job, dict) and job.get("name") == SYNC_CRON_JOB_NAME
        ]
    return []


def _is_expected_sync_cron_job(job: dict) -> bool:
    if job.get("name") != SYNC_CRON_JOB_NAME:
        return False
    if job.get("script") != SYNC_CRON_SCRIPT:
        return False
    if job.get("no_agent") is not True:
        return False
    if job.get("enabled") is False or job.get("state") in {"paused", "completed"}:
        return False
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        return (
            schedule.get("kind") == "interval"
            and int(schedule.get("minutes") or 0) == SYNC_CRON_MINUTES
        )
    schedule_text = str(schedule or job.get("schedule_display") or "")
    return schedule_text in {SYNC_CRON_SCHEDULE, "every 360m"}


def _remove_cron_jobs(jobs: list[dict]) -> bool:
    remove_job = _import_cron_function("remove_job") or _import_cron_function("delete_job")
    if remove_job is None:
        return False
    removed_any = False
    for job in jobs:
        job_ref = job.get("id") or job.get("job_id") or job.get("name")
        if not job_ref:
            continue
        attempts = (
            lambda job_ref=job_ref: remove_job(job_ref),
            lambda job_ref=job_ref: remove_job(job_id=job_ref),
        )
        removed_any = _call_first_supported(attempts) or removed_any
    return removed_any


def _add_external_dir_to_config(text: str, line: str) -> str:
    if not text.strip():
        return "skills:\n  external_dirs:\n" + line + "\n"

    lines = text.splitlines()
    skills_index = _find_section(lines, "skills:")
    if skills_index is None:
        return text.rstrip() + "\nskills:\n  external_dirs:\n" + line + "\n"

    external_index = _find_nested_key(lines, skills_index, "external_dirs:")
    if external_index is None:
        insert_at = _section_end(lines, skills_index)
        lines[insert_at:insert_at] = ["  external_dirs:", line]
        return "\n".join(lines).rstrip() + "\n"

    insert_at = external_index + 1
    while insert_at < len(lines) and lines[insert_at].startswith("    - "):
        insert_at += 1
    lines.insert(insert_at, line)
    return "\n".join(lines).rstrip() + "\n"


def _find_section(lines: list[str], key: str) -> int | None:
    for index, line in enumerate(lines):
        if line == key:
            return index
    return None


def _find_nested_key(lines: list[str], section_index: int, key: str) -> int | None:
    for index in range(section_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith(" "):
            return None
        if line == f"  {key}":
            return index
    return None


def _section_end(lines: list[str], section_index: int) -> int:
    index = section_index + 1
    while index < len(lines):
        line = lines[index]
        if line and not line.startswith(" "):
            return index
        index += 1
    return index


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    try:
        from hermes_cli.utils import atomic_replace
    except ImportError:
        os.replace(temp_path, path)
        return
    try:
        atomic_replace(temp_path, path)
    except TypeError:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _import_cron_function(name: str):
    for module_name in ("hermes_cli.cron.jobs", "cron.jobs"):
        try:
            module = __import__(module_name, fromlist=[name])
        except ImportError:
            continue
        function = getattr(module, name, None)
        if function is not None:
            return function
    return None


def _call_first_supported(attempts) -> bool:
    for attempt in attempts:
        try:
            attempt()
            return True
        except TypeError:
            continue
        except Exception:
            return False
    return False


def _google_sync_window(
    *,
    lookback_days: int | None,
    start_date: object,
    end_date: object,
) -> tuple[date, int]:
    if start_date and end_date:
        start = _date_from_iso(start_date, "start_date")
        end = _date_from_iso(end_date, "end_date")
        if end < start:
            raise oura.OuraAPIError("Google sync end_date must be on or after start_date.")
        return start, (end - start).days + 1
    if start_date:
        start = _date_from_iso(start_date, "start_date")
        return start, (date.today() - start).days + 1
    if lookback_days:
        days = max(1, lookback_days)
        return date.today() - timedelta(days=days - 1), days
    # Default question/sync path should include yesterday and today. Yesterday
    # captures completed sleep/stress context; today captures current calendar.
    return date.today() - timedelta(days=1), 2


def _sync_for_question(
    question: str,
    *,
    days: int,
    skip_sync: bool,
    force_sync: bool,
) -> dict:
    if skip_sync:
        return {
            "ok": True,
            "ran": False,
            "skipped": "Pre-question sync disabled by --no-sync.",
        }

    freshness = _sync_freshness(max_age_seconds=ASK_SYNC_STALE_SECONDS)
    reason = ""
    should_sync = False
    if force_sync:
        should_sync = True
        reason = "forced"
    elif _question_requests_fresh_sync(question):
        should_sync = True
        reason = "question requested fresh/current data"
    elif not freshness["fresh"]:
        should_sync = True
        reason = "local sync is stale"

    if not should_sync:
        return {
            "ok": True,
            "ran": False,
            "skipped": "Recent health sync is still fresh.",
            "freshness": freshness,
        }

    result = sync_now(days=days)
    result["ran"] = True
    result["reason"] = reason
    return result


def _health_ask_prompt(question: str, *, sync_result: dict | None, days: int) -> str:
    if sync_result and sync_result.get("ran"):
        sync_note = (
            f"I refreshed local health data with `hermes health sync --days {days}` "
            f"before this question because {sync_result.get('reason', 'a refresh was needed')}. "
        )
    elif sync_result and sync_result.get("skipped"):
        sync_note = f"I did not refresh local health data before this question: {sync_result['skipped']} "
    else:
        sync_note = "I did not refresh local health data before this question. "
    analysis_note = ""
    if _question_needs_cross_domain_context(question):
        analysis_note = (
            "For cross-domain explanations, compare Oura stress/recovery/heart-rate/sleep "
            "data with email volume and calendar load, but describe links as hypotheses "
            "or associations, not proof of causality. "
        )
    return (
        f"{sync_note}"
        "Answer using the health-coach skill and health-data tools. Query local "
        "health data before answering. "
        f"{analysis_note}"
        "If data is missing or stale, say exactly what is "
        f"missing. User question: {question}"
    )


def _sync_freshness(*, max_age_seconds: int) -> dict:
    store.initialize()
    providers = _freshness_providers()
    if not providers:
        return {
            "fresh": True,
            "max_age_seconds": max_age_seconds,
            "providers": {},
            "reason": "No connected health providers found.",
        }

    now = datetime.now(UTC)
    provider_rows: dict[str, dict] = {}
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT provider, last_status, updated_at FROM sync_state"
        ).fetchall()
    for row in rows:
        provider_rows[str(row["provider"])] = {
            "last_status": row["last_status"],
            "updated_at": row["updated_at"],
        }

    details: dict[str, dict] = {}
    stale_reasons: list[str] = []
    for provider in providers:
        detail = provider_rows.get(provider)
        if detail is None:
            details[provider] = {"fresh": False, "reason": "never synced"}
            stale_reasons.append(f"{provider}: never synced")
            continue
        status = str(detail.get("last_status") or "never")
        updated_at = str(detail.get("updated_at") or "")
        age_seconds = _sync_age_seconds(updated_at, now)
        provider_fresh = (
            status in {"ok", "partial"}
            and age_seconds is not None
            and age_seconds <= max_age_seconds
        )
        details[provider] = {
            "fresh": provider_fresh,
            "last_status": status,
            "updated_at": updated_at,
            "age_seconds": age_seconds,
        }
        if not provider_fresh:
            if status not in {"ok", "partial"}:
                stale_reasons.append(f"{provider}: status {status}")
            elif age_seconds is None:
                stale_reasons.append(f"{provider}: invalid updated_at")
            else:
                stale_reasons.append(f"{provider}: {int(age_seconds)}s old")

    return {
        "fresh": not stale_reasons,
        "max_age_seconds": max_age_seconds,
        "providers": details,
        "stale_reasons": stale_reasons,
    }


def _freshness_providers() -> list[str]:
    providers: list[str] = []
    if oura.token_path().exists():
        providers.append("oura")
    if context.google_workspace_available():
        providers.append("google_workspace")
    return providers


def _sync_age_seconds(updated_at: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (now - parsed.astimezone(UTC)).total_seconds())


def _question_requests_fresh_sync(question: str) -> bool:
    normalized = question.lower()
    fresh_markers = (
        "right now",
        "latest",
        "current",
        "fresh",
        "refresh",
        "sync",
        "up to date",
        "up-to-date",
    )
    return any(marker in normalized for marker in fresh_markers)


def _question_needs_cross_domain_context(question: str) -> bool:
    normalized = question.lower()
    cross_domain_markers = (
        "why",
        "cause",
        "caused",
        "because",
        "stress",
        "calendar",
        "meeting",
        "meetings",
        "email",
        "inbox",
        "workload",
        "schedule",
        "context",
        "line up",
        "correlat",
        "associate",
    )
    return any(marker in normalized for marker in cross_domain_markers)


def _hermes_command_prefix() -> list[str]:
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name == "hermes":
        if argv0.is_absolute() and argv0.exists():
            return [str(argv0)]
        resolved_argv0 = shutil.which(str(argv0))
        if resolved_argv0:
            return [resolved_argv0]

    sibling = Path(sys.executable).with_name("hermes")
    if sibling.exists():
        return [str(sibling)]

    if importlib.util.find_spec("hermes_cli.main") is not None:
        return [sys.executable, "-m", "hermes_cli.main"]

    resolved = shutil.which("hermes")
    if resolved:
        return [resolved]

    return [sys.executable, "-m", "hermes_cli.main"]


def _redacted_command_text(command: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for part in command:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        if part in _SENSITIVE_COMMAND_FLAGS:
            redacted.append(part)
            redact_next = True
            continue
        flag, separator, _value = part.partition("=")
        if separator and flag in _SENSITIVE_COMMAND_FLAGS:
            redacted.append(f"{flag}=[REDACTED]")
            continue
        redacted.append(_redact_tool_text(part))
    return " ".join(shlex.quote(part) for part in redacted)


def _redact_tool_text(text: str) -> str:
    if not text:
        return text
    text = _LOCAL_PATH_PATTERN.sub("[LOCAL_PATH]", text)
    text = _OAUTH_CODE_PATTERN.sub(r"\1[REDACTED]", text)
    text = _TOKEN_VALUE_PATTERN.sub(r"\1\2[REDACTED]", text)
    return _GOOGLE_TOKEN_PATTERN.sub("[REDACTED]", text)


def _question_text(value: object, args: tuple[object, ...]) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value).strip()
    if args:
        return " ".join(str(part) for part in args).strip()
    return ""


def _strip_flags(parts: list[str]) -> list[str]:
    question: list[str] = []
    index = 0
    flags_with_values = {"--days", "--lookback-days", "--lookback_days"}
    while index < len(parts):
        part = parts[index]
        if part.startswith("--"):
            if "=" not in part and part in flags_with_values and index + 1 < len(parts):
                index += 2
            else:
                index += 1
            continue
        question.append(part)
        index += 1
    return question


def _date_from_iso(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise oura.OuraAPIError(f"Google sync {label} must be YYYY-MM-DD.") from exc


def _parse_flags(parts: list[str]) -> dict[str, str | bool]:
    parsed: dict[str, str | bool] = {}
    index = 0
    while index < len(parts):
        part = parts[index]
        if not part.startswith("--"):
            index += 1
            continue
        key = part[2:]
        if "=" in key:
            key, value = key.split("=", 1)
            parsed[key] = value
            index += 1
            continue
        if index + 1 < len(parts) and not parts[index + 1].startswith("--"):
            parsed[key] = parts[index + 1]
            index += 2
            continue
        parsed[key] = True
        index += 1
    return parsed


def _float_or_default(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise oura.OuraAPIError("Oura sync lookback_days must be an integer.") from exc


def _int_or_default(value: object, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise oura.OuraAPIError("Health ask days must be an integer.") from exc
