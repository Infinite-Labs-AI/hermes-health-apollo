from __future__ import annotations

import importlib.util
import argparse
import json
import os
import shutil
import subprocess
import sys
import types
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "health_query",
    "health_log_food",
    "health_sync",
    "health_calendar_peek",
    "health_coverage",
    "health_event_query",
    "health_feature_query",
    "health_analysis_catalog",
    "health_analysis_plan",
    "health_analyze",
    "health_analysis_explain",
}
INSTALLED_MODULES = [
    "health_data_entry.py",
    "commands.py",
    "onboarding.py",
    "context.py",
    "food.py",
    "normalize.py",
    "oura.py",
    "query.py",
    "store.py",
    "sync_control.py",
    "semantic_layer.py",
    "feature_registry.py",
    "feature_engineering.py",
    "analysis_registry.py",
    "analysis_tools.py",
]


class FakeContext:
    def __init__(self):
        self.tools = []
        self.commands = []
        self.cli_commands = []
        self.skills = []

    def register_tool(self, name, *args, **kwargs):
        handler = args[-1]
        self.tools.append((name, handler, kwargs))

    def register_command(self, name, handler, **kwargs):
        self.commands.append((name, handler, kwargs))

    def register_cli_command(self, name, *args, **kwargs):
        self.cli_commands.append((name, args, kwargs))

    def register_skill(self, name, path, **kwargs):
        self.skills.append((name, path, kwargs))


def test_register_exposes_health_surfaces_without_core_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.health_data",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules["hermes_plugins.health_data"] = module
    spec.loader.exec_module(module)

    ctx = FakeContext()
    module.register(ctx)

    assert EXPECTED_TOOLS.issubset({name for name, _handler, _kwargs in ctx.tools})
    assert any(name == "health" for name, _handler, _kwargs in ctx.commands)
    assert any(name == "health" for name, _args, _kwargs in ctx.cli_commands)
    registered_skills = {name for name, _path, _kwargs in ctx.skills}
    assert {"health-coach", "health-visuals"}.issubset(registered_skills)

    handlers = {name: handler for name, handler, _kwargs in ctx.tools}
    query_result = handlers["health_query"]({"query_type": "recent"})
    assert isinstance(query_result, str)
    assert json.loads(query_result) == {"days": []}

    calendar_result = handlers["health_calendar_peek"]({"days": 1})
    assert isinstance(calendar_result, str)
    assert json.loads(calendar_result)["events"] == []

    cli_args = next(args for name, args, _kwargs in ctx.cli_commands if name == "health")
    assert cli_args[0] == "Manage local health data."
    setup_fn = cli_args[1]
    assert callable(setup_fn)

    parser = argparse.ArgumentParser(prog="hermes health")
    setup_fn(parser)
    parsed = parser.parse_args(["status"])
    assert callable(parsed.func)
    parsed = parser.parse_args(["sync", "--days", "30"])
    assert callable(parsed.func)
    assert parsed.days == 30
    parsed = parser.parse_args(["connect-google", "--client-secret", "client.json", "--open"])
    assert callable(parsed.func)
    assert parsed.client_secret == "client.json"
    assert parsed.open_browser is True
    parsed = parser.parse_args(["ask", "--sync", "why", "was", "I", "stressed"])
    assert callable(parsed.func)
    assert parsed.force_sync is True
    assert parsed.question == ["why", "was", "I", "stressed"]
    parsed = parser.parse_args(["reminder"])
    assert callable(parsed.func)


def test_health_data_entry_register_seeds_package_for_root_init(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("hermes_plugins.health_data", None)
    sys.modules.pop("hermes_plugins", None)
    spec = importlib.util.spec_from_file_location(
        "health_data_entry_under_test",
        ROOT / "health_data_entry.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ctx = FakeContext()
    module.register(ctx)

    assert EXPECTED_TOOLS.issubset({name for name, _handler, _kwargs in ctx.tools})
    assert any(name == "health" for name, _handler, _kwargs in ctx.commands)
    assert any(name == "health" for name, _args, _kwargs in ctx.cli_commands)


def test_plugin_code_uses_relative_imports_only():
    offenders = []
    for path in ROOT.glob("*.py"):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "from plugins." in text or "import plugins." in text:
            offenders.append(path.name)

    assert offenders == []


def test_entry_point_loads_without_root_plugin_init(tmp_path, monkeypatch):
    installed = tmp_path / "site"
    installed.mkdir()
    for name in INSTALLED_MODULES:
        shutil.copy2(ROOT / name, installed / name)

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location(
        "health_data_entry", installed / "health_data_entry.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ctx = FakeContext()
    module.register(ctx)

    assert EXPECTED_TOOLS.issubset({name for name, _handler, _kwargs in ctx.tools})
    assert ctx.skills
    assert Path(ctx.skills[0][1]).exists()
    cli_args = next(args for name, args, _kwargs in ctx.cli_commands if name == "health")
    parser = argparse.ArgumentParser(prog="hermes health")
    cli_args[1](parser)
    parsed = parser.parse_args(["setup"])
    assert callable(parsed.func)
    parsed = parser.parse_args(["sync", "--start-date", "2026-05-12"])
    assert callable(parsed.func)
    assert parsed.start_date == "2026-05-12"
    parsed = parser.parse_args(["google", "--auth-code", "http://localhost:1/?code=abc", "--open"])
    assert callable(parsed.func)
    assert parsed.auth_code == "http://localhost:1/?code=abc"
    assert parsed.open_browser is True
    parsed = parser.parse_args(["ask", "--days", "2", "how", "did", "I", "sleep"])
    assert callable(parsed.func)
    assert parsed.days == 2
    assert parsed.force_sync is False
    assert parsed.question == ["how", "did", "I", "sleep"]


AUDIO_LOCATION_SUBCOMMANDS = {"connect-audio", "connect-location", "calibrate-audio"}


def _load_package_entry():
    sys.modules.pop("hermes_plugins.health_data", None)
    sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.health_data",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(ROOT)]
    sys.modules["hermes_plugins.health_data"] = module
    spec.loader.exec_module(module)
    return module


def _load_standalone_entry():
    sys.modules.pop("hermes_plugins.health_data", None)
    sys.modules.pop("hermes_plugins", None)
    spec = importlib.util.spec_from_file_location(
        "health_data_entry_parity", ROOT / "health_data_entry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _health_cli_subcommands(module) -> set[str]:
    ctx = FakeContext()
    module.register(ctx)
    cli_args = next(args for name, args, _kwargs in ctx.cli_commands if name == "health")
    parser = argparse.ArgumentParser(prog="hermes health")
    cli_args[1](parser)
    choices: set[str] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices.update(action.choices.keys())
    return choices


def test_both_entry_loaders_expose_identical_health_subcommands(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    package_subcommands = _health_cli_subcommands(_load_package_entry())
    standalone_subcommands = _health_cli_subcommands(_load_standalone_entry())

    assert package_subcommands == standalone_subcommands
    assert AUDIO_LOCATION_SUBCOMMANDS.issubset(package_subcommands)
    assert AUDIO_LOCATION_SUBCOMMANDS.issubset(standalone_subcommands)


def test_wheel_contains_entry_point_assets(tmp_path):
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--ignore-requires-python",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(ROOT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    wheels = list(wheel_dir.glob("hermes_health_data-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        entry_points = archive.read(
            "hermes_health_data-0.1.0.dist-info/entry_points.txt"
        ).decode("utf-8")

    assert "health_data_assets/plugin.yaml" in names
    assert "health_data_assets/skills/health-coach/SKILL.md" in names
    assert "health_data_assets/skills/health-visuals/SKILL.md" in names
    assert (
        "health_data_assets/skills/health-visuals/references/cli_visual_patterns.md"
        in names
    )
    expected_visual_assets = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "health_data_assets" / "visuals" / "cli").rglob("*")
        if path.is_file()
    ]
    assert expected_visual_assets
    for asset_name in expected_visual_assets:
        assert asset_name in names
    for module_name in INSTALLED_MODULES:
        assert module_name in names
    assert "health-data = health_data_entry\n" in entry_points
    assert "health-data = health_data_entry:register" not in entry_points

    target = tmp_path / "target"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--ignore-requires-python",
            "--no-deps",
            "--target",
            str(target),
            str(wheels[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.metadata as md;"
                "ep = next(ep for ep in md.entry_points().select("
                "group='hermes_agent.plugins') if ep.name == 'health-data');"
                "obj = ep.load();"
                "assert hasattr(obj, 'register')"
            ),
        ],
        check=True,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(target)},
        text=True,
    )
    assert probe.returncode == 0
