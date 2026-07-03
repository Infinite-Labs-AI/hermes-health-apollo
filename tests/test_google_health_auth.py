from __future__ import annotations

import importlib.util
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import parse

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
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def modules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Avoid leaking real BYO creds from the developer environment into the test.
    monkeypatch.delenv("HERMES_GOOGLE_HEALTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("HERMES_GOOGLE_HEALTH_CLIENT_SECRET", raising=False)
    return {
        "store": load_module("store"),
        "auth": load_module("google_health_auth"),
    }


def test_token_path_is_separate_from_workspace_token(modules, tmp_path):
    auth = modules["auth"]
    assert auth.token_path() == tmp_path / "google_health_token.json"
    # Must NOT collide with the Google Workspace token.
    assert auth.token_path().name != "google_token.json"


def test_connect_without_credentials_returns_guidance(modules):
    auth = modules["auth"]
    result = auth.connect_google_health()
    assert result["ok"] is False
    assert result["connected"] is False
    assert "authorize_url" not in result
    assert "console.cloud.google.com" in result["registration_url"]
    # The Testing-mode 7-day refresh caveat must be surfaced somewhere in the UX.
    assert "7" in (result["guidance"] + json.dumps(result))


def test_connect_step_one_saves_credentials_and_builds_pkce_auth_url(modules, tmp_path):
    auth = modules["auth"]
    result = auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        state="state-123",
        pkce=("verifier-xyz", "challenge-xyz"),
    )

    assert result["ok"] is False
    assert result["connected"] is False

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert 'HERMES_GOOGLE_HEALTH_CLIENT_ID="client-id"' in env_text
    assert 'HERMES_GOOGLE_HEALTH_CLIENT_SECRET="client-secret"' in env_text

    parsed = parse.urlparse(result["authorize_url"])
    params = parse.parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert params["client_id"] == ["client-id"]
    assert params["response_type"] == ["code"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert params["code_challenge"] == ["challenge-xyz"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["state-123"]
    # Restricted googlehealth.* read scopes are requested.
    scope = params["scope"][0]
    assert "googlehealth.activity_and_fitness.readonly" in scope
    assert "googlehealth.sleep.readonly" in scope

    pending = json.loads(auth.pending_state_path().read_text(encoding="utf-8"))
    assert pending["state"] == "state-123"
    assert pending["code_verifier"] == "verifier-xyz"


def test_connect_step_two_exchanges_code_with_pkce_and_saves_token(modules, tmp_path):
    auth = modules["auth"]
    auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        state="state-123",
        pkce=("verifier-xyz", "challenge-xyz"),
    )

    calls = []

    def http_post(url, data, headers=None):
        calls.append((url, data, headers))
        return {
            "access_token": "gh-access",
            "refresh_token": "gh-refresh",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
        }

    result = auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        code="returned-code",
        state="state-123",
        http_post=http_post,
    )

    assert result["ok"] is True
    assert result["connected"] is True
    assert auth.load_token()["refresh_token"] == "gh-refresh"
    assert not auth.pending_state_path().exists()
    url, data, headers = calls[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "returned-code"
    assert data["code_verifier"] == "verifier-xyz"
    assert data["redirect_uri"] == "http://localhost:1"


def test_connect_step_two_accepts_full_pasted_redirect_url(modules):
    auth = modules["auth"]
    auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        state="state-abc",
        pkce=("verifier-1", "challenge-1"),
    )

    def http_post(url, data, headers=None):
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    pasted = "http://localhost:1/?state=state-abc&code=pasted-code&scope=x"
    result = auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        code=pasted,
        http_post=http_post,
    )
    assert result["ok"] is True
    assert auth.load_token()["access_token"] == "a"


def test_connect_step_two_rejects_state_mismatch(modules):
    auth = modules["auth"]
    auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        state="real-state",
        pkce=("v", "c"),
    )
    with pytest.raises(auth.GoogleHealthNotConnected, match="state mismatch"):
        auth.connect_google_health(
            client_id="client-id",
            client_secret="client-secret",
            code="returned-code",
            state="wrong-state",
            http_post=lambda *a, **k: {},
        )


def test_connect_step_two_requires_state(modules):
    auth = modules["auth"]
    auth.connect_google_health(
        client_id="client-id",
        client_secret="client-secret",
        state="real-state",
        pkce=("v", "c"),
    )
    with pytest.raises(auth.GoogleHealthNotConnected, match="state is required"):
        auth.connect_google_health(
            client_id="client-id",
            client_secret="client-secret",
            code="raw-code-no-state",
            http_post=lambda *a, **k: {},
        )


def test_credentials_fall_back_to_google_client_secret_json(modules, tmp_path):
    auth = modules["auth"]
    (tmp_path / "google_client_secret.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "file-client-id",
                    "client_secret": "file-client-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    cid, secret = auth.load_client_credentials()
    assert cid == "file-client-id"
    assert secret == "file-client-secret"


def test_refresh_uses_google_token_url_and_not_connected_on_invalid_grant(modules):
    auth = modules["auth"]
    auth.save_token(
        {
            "access_token": "old",
            "refresh_token": "r",
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
    )
    with pytest.raises(auth.GoogleHealthNotConnected):
        auth.refresh_access_token(
            client_id="c",
            client_secret="s",
            http_post=lambda *a, **k: {"error": "invalid_grant"},
        )
