from __future__ import annotations

import importlib
import importlib.resources
import importlib.util
import json
import logging
from pathlib import Path
import sys
import types
from typing import Any, Callable

logger = logging.getLogger(__name__)

HEALTH_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "query_type": {
            "type": "string",
            "enum": [
                "recent",
                "date_range",
                "stress_days",
                "correlate",
                "heart_rate",
                "workouts",
                "sessions",
                "tags",
                "coverage",
            ],
        },
        "start": {"type": "string"},
        "end": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1},
        "left": {"type": "string"},
        "right": {"type": "string"},
        "shift_days": {"type": "integer"},
        "source": {"type": "string"},
    },
    "additionalProperties": False,
}
FOOD_LOG_SCHEMA = {
    "type": "object",
    "properties": {
        "day": {"type": "string"},
        "description": {"type": "string"},
        "analysis_text": {
            "anyOf": [{"type": "string"}, {"type": "object"}, {"type": "null"}]
        },
    },
    "required": ["day", "description"],
    "additionalProperties": False,
}
EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
SYNC_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {"type": "integer", "minimum": 1},
        "lookback_days": {"type": "integer", "minimum": 1},
        "start_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "end_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    },
    "additionalProperties": False,
}
CALENDAR_PEEK_SCHEMA = {
    "type": "object",
    "properties": {"days": {"type": "integer", "minimum": 0}},
    "additionalProperties": False,
}


def register(ctx):
    plugin_init = Path(__file__).with_name("__init__.py")
    if not plugin_init.exists():
        _register_from_installed_modules(ctx, Path(__file__).parent)
        return
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.health_data", plugin_init, submodule_search_locations=[str(plugin_init.parent)]
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load health-data plugin entry point")
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(plugin_init.parent)]
    sys.modules["hermes_plugins.health_data"] = module
    spec.loader.exec_module(module)
    module.register(ctx)


def _register_from_installed_modules(ctx, base_dir: Path) -> None:
    package_name = "hermes_plugins.health_data"
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    package = sys.modules.get(package_name)
    if package is None:
        package = types.ModuleType(package_name)
        package.__path__ = [str(base_dir)]
        sys.modules[package_name] = package
    try:
        analysis_tools = importlib.import_module(f"{package_name}.analysis_tools")
        commands = importlib.import_module(f"{package_name}.commands")
        food = importlib.import_module(f"{package_name}.food")
        query = importlib.import_module(f"{package_name}.query")
    except ImportError:
        logger.warning("health-data entry point failed to import modules", exc_info=True)
        _register_degraded(ctx)
        return

    _register_tool(
        ctx,
        "health_query",
        lambda args=None, **kwargs: query.health_query(_tool_payload(args, kwargs)),
        HEALTH_QUERY_SCHEMA,
        description=(
            "Query local health data. For any health question, follow the "
            "health-coach skill."
        ),
    )
    _register_tool(
        ctx,
        "health_log_food",
        lambda args=None, **kwargs: food.log_food(**_tool_payload(args, kwargs)),
        FOOD_LOG_SCHEMA,
        description="Insert a food log after photo vision analysis.",
    )
    _register_tool(
        ctx,
        "health_sync",
        lambda args=None, **kwargs: commands.sync_now(**_tool_payload(args, kwargs)),
        SYNC_SCHEMA,
        description="Sync local health data.",
    )
    _register_tool(
        ctx,
        "health_calendar_peek",
        lambda args=None, **kwargs: commands.calendar_peek(**_tool_payload(args, kwargs)),
        CALENDAR_PEEK_SCHEMA,
        description="Peek at forward Google Calendar context.",
    )
    _register_tool(
        ctx,
        "health_coverage",
        lambda args=None, **kwargs: analysis_tools.health_coverage(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_COVERAGE_SCHEMA,
        description="Report health source coverage and freshness.",
    )
    _register_tool(
        ctx,
        "health_event_query",
        lambda args=None, **kwargs: analysis_tools.health_event_query(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_EVENT_QUERY_SCHEMA,
        description="Query canonical health events and linked entities.",
    )
    _register_tool(
        ctx,
        "health_feature_query",
        lambda args=None, **kwargs: analysis_tools.health_feature_query(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_FEATURE_QUERY_SCHEMA,
        description="Materialize and query canonical health features.",
    )
    _register_tool(
        ctx,
        "health_analysis_catalog",
        lambda args=None, **kwargs: analysis_tools.health_analysis_catalog(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_ANALYSIS_CATALOG_SCHEMA,
        description="List supported health analysis packs.",
    )
    _register_tool(
        ctx,
        "health_analysis_plan",
        lambda args=None, **kwargs: analysis_tools.health_analysis_plan(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_ANALYSIS_PLAN_SCHEMA,
        description="Map a broad health question to supported analysis packs.",
    )
    _register_tool(
        ctx,
        "health_analyze",
        lambda args=None, **kwargs: analysis_tools.health_analyze(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_ANALYZE_SCHEMA,
        description="Run a registered health analysis pack.",
    )
    _register_tool(
        ctx,
        "health_analysis_explain",
        lambda args=None, **kwargs: analysis_tools.health_analysis_explain(_tool_payload(args, kwargs)),
        analysis_tools.HEALTH_ANALYSIS_EXPLAIN_SCHEMA,
        description="Explain a prior health analysis run with provenance.",
    )
    ctx.register_command("health", commands.slash_health)
    _register_cli_command(ctx, "health", commands)
    ctx.register_skill("health-coach", _skill_dir(base_dir, "health-coach"))
    ctx.register_skill("health-visuals", _skill_dir(base_dir, "health-visuals"))


def _skill_dir(base_dir: Path, name: str) -> Path:
    repo_skill_dir = base_dir / "skills" / name
    if repo_skill_dir.exists():
        return repo_skill_dir
    try:
        return Path(
            importlib.resources.files("health_data_assets").joinpath(
                "skills", name
            )
        )
    except (ImportError, ModuleNotFoundError, FileNotFoundError):
        return repo_skill_dir


def _register_degraded(ctx) -> None:
    def degraded(*_args, **_kwargs):
        return {
            "ok": False,
            "error": "health-data could not load. Update the plugin or Hermes.",
        }

    _register_tool(
        ctx,
        "health_status",
        degraded,
        EMPTY_SCHEMA,
        description="Reports that the health-data plugin could not load.",
    )


def _register_tool(ctx, name, handler, schema, description="") -> None:
    json_handler = _json_tool_handler(handler)
    try:
        ctx.register_tool(
            name,
            "health-data",
            schema,
            json_handler,
            description=description,
        )
    except TypeError:
        ctx.register_tool(name, json_handler, description=description)


def _tool_payload(args: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(args, dict) and not kwargs:
        return args
    payload = dict(kwargs)
    if isinstance(args, dict):
        payload.update(args)
    return payload


def _json_tool_handler(handler: Callable[..., Any]) -> Callable[..., str]:
    def wrapped(*args: Any, **kwargs: Any) -> str:
        result = handler(*args, **kwargs)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)

    return wrapped


def _register_cli_command(ctx, name, commands_module) -> None:
    try:
        ctx.register_cli_command(
            name,
            "Manage local health data.",
            lambda parser: _setup_health_cli_parser(parser, commands_module),
            description="Manage local health data.",
        )
    except TypeError:
        ctx.register_cli_command(name, commands_module.cli_health)


def _setup_health_cli_parser(parser, commands_module) -> None:
    parser.set_defaults(func=lambda _args: _print_cli_result(commands_module.status()))
    subparsers = parser.add_subparsers(dest="health_action")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Install the local health database, skill path, and sync launcher.",
    )
    setup_parser.set_defaults(func=lambda _args: _print_cli_result(commands_module.setup()))

    connect_parser = subparsers.add_parser(
        "connect",
        help="Connect Oura using OAuth; requires an Oura developer app Client ID and Secret.",
    )
    connect_parser.add_argument("--client-id", dest="client_id", help="Oura developer app Client ID.")
    connect_parser.add_argument(
        "--client-secret",
        dest="client_secret",
        help="Oura developer app Client Secret; stored in ~/.hermes/.env when supplied.",
    )
    connect_parser.add_argument("--code")
    connect_parser.add_argument("--state")
    connect_parser.add_argument("--scopes")
    connect_parser.add_argument("--loopback-timeout", type=float, default=120.0)
    connect_parser.add_argument(
        "--manual",
        action="store_true",
        help="Print the authorization URL without waiting for a loopback callback.",
    )
    connect_parser.set_defaults(
        func=lambda args: _print_cli_result(
            commands_module.connect(
                client_id=args.client_id,
                client_secret=args.client_secret,
                code=args.code,
                state=args.state,
                scopes=args.scopes,
                loopback_timeout=0 if args.manual else args.loopback_timeout,
            )
        )
    )

    connect_audio_parser = subparsers.add_parser(
        "connect-audio",
        help="Register the local ambient-audio source and report the spool status.",
    )
    connect_audio_parser.set_defaults(
        func=lambda _args: _print_cli_result(commands_module.connect_audio())
    )

    connect_location_parser = subparsers.add_parser(
        "connect-location",
        help="Register the local location source (requires precise_location_opt_in).",
    )
    connect_location_parser.set_defaults(
        func=lambda _args: _print_cli_result(commands_module.connect_location())
    )

    calibrate_audio_parser = subparsers.add_parser(
        "calibrate-audio",
        help="Measure the ambient noise floor and set the audio silence threshold.",
    )
    calibrate_audio_parser.add_argument("--seconds", type=int, default=None)
    calibrate_audio_parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Skip measuring and set this silence threshold (dBFS) directly.",
    )
    calibrate_audio_parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept the suggested threshold without prompting.",
    )
    calibrate_audio_parser.set_defaults(
        func=lambda args: _run_calibrate_audio(args, commands_module)
    )

    google_parser = subparsers.add_parser(
        "connect-google",
        aliases=["google-connect", "google"],
        help=(
            "Connect Google Workspace using OAuth; requires the Google Workspace "
            "helper and a Desktop OAuth JSON with redirect http://localhost:1/."
        ),
    )
    google_parser.add_argument(
        "--client-secret",
        dest="client_secret",
        help=(
            "Path to a downloaded Google Cloud Desktop app OAuth JSON file with "
            "authorized redirect URI http://localhost:1/."
        ),
    )
    google_parser.add_argument(
        "--auth-code",
        "--code",
        dest="auth_code",
        help="Full localhost redirect URL or OAuth code returned by Google.",
    )
    google_parser.add_argument(
        "--auth-url",
        action="store_true",
        help="Print a fresh Google authorization URL using the stored client secret.",
    )
    google_parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether Google Workspace OAuth is connected.",
    )
    google_parser.add_argument(
        "--check-live",
        action="store_true",
        help="Check auth with a real Google API call.",
    )
    google_parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Install Google API Python dependencies for the workspace helper.",
    )
    google_parser.add_argument(
        "--open-browser",
        "--open",
        dest="open_browser",
        action="store_true",
        help="Open the Google authorization URL in the default browser.",
    )
    google_parser.add_argument(
        "--revoke",
        action="store_true",
        help="Revoke and delete the stored Google Workspace token.",
    )
    google_parser.set_defaults(
        func=lambda args: _print_cli_result(
            commands_module.connect_google(
                client_secret=args.client_secret,
                auth_code=args.auth_code,
                auth_url=args.auth_url,
                check=args.check,
                check_live=args.check_live,
                install_deps=args.install_deps,
                open_browser=args.open_browser,
                revoke=args.revoke,
            )
        )
    )

    sync_parser = subparsers.add_parser("sync", help="Sync local health data.")
    sync_parser.add_argument(
        "--days",
        type=int,
        help="Backfill this many days ending today instead of the rolling sync window.",
    )
    sync_parser.add_argument("--start-date", dest="start_date")
    sync_parser.add_argument("--end-date", dest="end_date")
    sync_parser.set_defaults(
        func=lambda args: _print_cli_result(
            commands_module.sync_now(
                days=args.days,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        )
    )

    ask_parser = subparsers.add_parser(
        "ask",
        help="Ask a health question; syncs recent data first by default.",
    )
    ask_parser.add_argument("question", nargs="+")
    ask_parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Sync this many recent days when a refresh is needed (default: 3).",
    )
    ask_parser.add_argument(
        "--sync",
        "--force-sync",
        dest="force_sync",
        action="store_true",
        help="Force a pre-question sync even when recent data is fresh.",
    )
    ask_parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip the automatic pre-question sync.",
    )
    ask_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full command result as JSON instead of only the answer.",
    )
    ask_parser.set_defaults(
        func=lambda args: _print_ask_result(
            commands_module.ask(
                question=" ".join(args.question),
                days=args.days,
                no_sync=args.no_sync,
                force_sync=args.force_sync,
            ),
            json_output=args.json,
        )
    )

    status_parser = subparsers.add_parser("status", help="Show health-data status.")
    status_parser.set_defaults(func=lambda _args: _print_cli_result(commands_module.status()))

    reminder_parser = subparsers.add_parser(
        "reminder",
        aliases=["reminders"],
        help="Show how health sync differs from user-visible reminders.",
    )
    reminder_parser.set_defaults(
        func=lambda _args: _print_cli_result(
            {"ok": True, **commands_module.reminder_guidance()}
        )
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="Remove the sync launcher and optional local health data.",
    )
    uninstall_parser.add_argument("--purge", action="store_true")
    uninstall_parser.add_argument("--yes", action="store_true")
    uninstall_parser.set_defaults(
        func=lambda args: _print_cli_result(
            commands_module.uninstall(purge=args.purge, yes=args.yes)
        )
    )


def _run_calibrate_audio(args: Any, commands_module: Any) -> Any:
    if args.threshold is not None:
        return _print_cli_result(
            commands_module.set_audio_silence_threshold(threshold_db=args.threshold)
        )
    suggestion = commands_module.calibrate_audio(seconds=args.seconds)
    if suggestion.get("status") != "ok":
        return _print_cli_result(suggestion)
    floor = suggestion["floor_db"]
    suggested = suggestion["suggested_db"]
    if suggestion.get("noisy"):
        print("Note: sound was detected during calibration; consider re-running in a quiet moment.")
    print(f"Measured quiet level ~{floor} dBFS. Suggested silence threshold: {suggested} dBFS.")
    if args.yes:
        chosen = suggested
    else:
        reply = input(
            f"Accept {suggested} dBFS? [Enter=accept, a number to set your own, d=keep current]: "
        ).strip()
        if reply.lower() == "d":
            return _print_cli_result({"status": "unchanged"})
        if reply == "":
            chosen = suggested
        else:
            try:
                chosen = float(reply)
            except ValueError:
                return _print_cli_result({"status": "aborted", "reason": "not a number"})
    return _print_cli_result(
        commands_module.set_audio_silence_threshold(threshold_db=float(chosen))
    )


def _print_cli_result(result: Any) -> Any:
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result


def _print_ask_result(result: Any, *, json_output: bool = False) -> Any:
    if json_output or not isinstance(result, dict) or not result.get("ok"):
        return _print_cli_result(result)
    print(result.get("answer", ""))
    return result
