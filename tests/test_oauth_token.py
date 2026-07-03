from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


# Mirrors the per-test-file loader convention used elsewhere in this suite;
# there is no shared conftest. oauth_token has no relative imports, so it loads
# standalone, but we register it under the package name so connector modules
# (oura, google_health_auth) resolve `from . import oauth_token`.
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
def oauth_token():
    return load_module("oauth_token")


def test_token_expired_respects_skew(oauth_token):
    now = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    fresh = {"expires_at": (now + timedelta(minutes=10)).isoformat()}
    within_skew = {"expires_at": (now + timedelta(minutes=2)).isoformat()}
    assert oauth_token.token_expired(fresh, now=lambda: now) is False
    assert oauth_token.token_expired(within_skew, now=lambda: now) is True
    assert oauth_token.token_expired({}, now=lambda: now) is True


def test_save_token_is_private_and_round_trips(oauth_token, tmp_path):
    token_file = tmp_path / "p_token.json"
    token = {"access_token": "loopback-access", "refresh_token": "loopback-refresh"}
    oauth_token.save_token(token, token_file)
    assert oauth_token.load_token(token_file) == token
    import os

    if os.name != "nt":
        assert oct(token_file.stat().st_mode & 0o777) == "0o600"


def test_load_token_missing_raises_configured_exception(oauth_token, tmp_path):
    class MyNotConnected(RuntimeError):
        pass

    with pytest.raises(MyNotConnected, match="connect first"):
        oauth_token.load_token(
            tmp_path / "missing.json",
            missing_exc=MyNotConnected,
            missing_message="connect first",
        )


def test_refresh_uses_pending_sidecar_and_preserves_unrotated_refresh(
    oauth_token, tmp_path
):
    token_file = tmp_path / "p_token.json"
    clock = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    oauth_token.save_token(
        {
            "access_token": "old-access",
            "refresh_token": "keep-refresh",
            "scope": "x",
            "expires_at": (clock - timedelta(minutes=1)).isoformat(),
        },
        token_file,
    )
    seen = {}

    def http_post(url, data, headers=None):
        seen["url"] = url
        seen["data"] = data
        seen["headers"] = headers
        # The pending sidecar must exist while the network call is in flight.
        assert (tmp_path / "p_token.json.pending").exists()
        return {"access_token": "new-access", "expires_in": 3600}  # no rotation

    refreshed = oauth_token.refresh_access_token(
        token_file=token_file,
        token_url="https://example.test/token",
        client_id="cid",
        client_secret="sec",
        http_post=http_post,
        now=lambda: clock,
    )

    assert refreshed["access_token"] == "new-access"
    assert refreshed["refresh_token"] == "keep-refresh"  # preserved when not rotated
    assert refreshed["scope"] == "x"
    assert oauth_token.load_token(token_file)["access_token"] == "new-access"
    assert not (tmp_path / "p_token.json.pending").exists()
    assert seen["url"] == "https://example.test/token"
    assert seen["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "keep-refresh",
        "client_id": "cid",
        "client_secret": "sec",
    }
    assert seen["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}


def test_refresh_skips_network_when_token_still_fresh(oauth_token, tmp_path):
    token_file = tmp_path / "p_token.json"
    clock = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
    oauth_token.save_token(
        {
            "access_token": "still-good",
            "refresh_token": "r",
            "expires_at": (clock + timedelta(hours=1)).isoformat(),
        },
        token_file,
    )

    def http_post(*_args, **_kwargs):
        raise AssertionError("must not refresh a fresh token")

    out = oauth_token.refresh_access_token(
        token_file=token_file,
        token_url="https://example.test/token",
        client_id="c",
        client_secret="s",
        http_post=http_post,
        now=lambda: clock,
    )
    assert out["access_token"] == "still-good"


def test_refresh_invalid_grant_raises_configured_not_connected(oauth_token, tmp_path):
    token_file = tmp_path / "p_token.json"
    oauth_token.save_token(
        {
            "access_token": "old",
            "refresh_token": "r",
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
        token_file,
    )

    class MyNotConnected(RuntimeError):
        pass

    with pytest.raises(MyNotConnected, match="reconnect please"):
        oauth_token.refresh_access_token(
            token_file=token_file,
            token_url="https://example.test/token",
            client_id="c",
            client_secret="s",
            http_post=lambda *_a, **_k: {"error": "invalid_grant"},
            not_connected_exc=MyNotConnected,
            invalid_grant_message="reconnect please",
        )


def test_exchange_includes_extra_pkce_fields(oauth_token):
    captured = {}

    def http_post(url, data, headers=None):
        captured.update(data)
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    token = oauth_token.exchange_code_for_token(
        token_url="https://example.test/token",
        client_id="c",
        client_secret="s",
        code="auth-code",
        redirect_uri="http://localhost:1",
        http_post=http_post,
        extra_fields={"code_verifier": "verifier-123"},
        now=lambda: datetime(2026, 6, 9, tzinfo=UTC),
    )
    assert captured["grant_type"] == "authorization_code"
    assert captured["code"] == "auth-code"
    assert captured["code_verifier"] == "verifier-123"
    assert token["expires_at"].startswith("2026-06-09")


def test_env_credentials_round_trip(oauth_token, tmp_path):
    env_file = tmp_path / ".env"
    oauth_token.save_client_credentials(
        env_file=env_file,
        values={"HERMES_X_CLIENT_ID": "id-1", "HERMES_X_CLIENT_SECRET": "sec-1"},
    )
    text = env_file.read_text(encoding="utf-8")
    assert 'HERMES_X_CLIENT_ID="id-1"' in text

    cid, sec = oauth_token.load_client_credentials(
        env_file=env_file,
        id_key="HERMES_X_CLIENT_ID",
        secret_key="HERMES_X_CLIENT_SECRET",
        env={},
    )
    assert (cid, sec) == ("id-1", "sec-1")

    assert (
        oauth_token.clear_client_credentials(
            env_file=env_file,
            keys=["HERMES_X_CLIENT_ID", "HERMES_X_CLIENT_SECRET"],
        )
        is True
    )
    assert not env_file.exists()


def test_pkce_pair_is_valid_s256(oauth_token):
    verifier, challenge = oauth_token.generate_pkce_pair()
    # RFC 7636: verifier 43-128 chars; challenge is base64url(sha256(verifier)) no padding.
    import base64
    import hashlib

    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge
