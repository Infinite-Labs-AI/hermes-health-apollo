"""In-repo BYO Google OAuth for the Google Health API v4 (pure stdlib).

This connector runs its OWN OAuth consent for the ``googlehealth.*`` read scopes
and stores its OWN token at ``hermes_home()/google_health_token.json`` -- kept
deliberately separate from the Google Workspace ``google_token.json`` so the two
never clobber each other.

It reuses the shared :mod:`oauth_token` primitives (atomic private writes, file
lock, ``.pending`` refresh sidecar, refresh/exchange, PKCE) and adds only the
Google-specific consent flow: the authorization-code + PKCE (S256) flow against
``accounts.google.com`` with ``access_type=offline`` + ``prompt=consent`` and a
manual-paste loopback redirect (Google deprecated the OOB flow).

The user brings their own Google OAuth *Desktop app* client. Credentials are read
from ``HERMES_GOOGLE_HEALTH_CLIENT_ID`` / ``HERMES_GOOGLE_HEALTH_CLIENT_SECRET``,
then the ``.env`` file, then the existing ``google_client_secret.json``.

Verified against developers.google.com/health: scopes are
``https://www.googleapis.com/auth/googlehealth.<category>.readonly`` and are all
classified Restricted -- in Google "Testing" publishing mode the issued refresh
token expires after ~7 days and the app is limited to <100 test users.
"""

from __future__ import annotations

import contextlib
import json
import secrets
from pathlib import Path
from typing import Any, Callable
from urllib import parse

from . import oauth_token, store


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CREDENTIALS_URL = "https://console.cloud.google.com/apis/credentials"

# Manual-paste loopback: Google deprecated the OOB ("urn:ietf:wg:oauth:2.0:oob")
# flow, so we use a localhost redirect and have the user copy the ``?code=...``
# from the browser URL bar. Port 1 never binds anything, so nothing actually
# listens -- this mirrors the Google Workspace setup helper's approach.
DEFAULT_REDIRECT_URI = "http://localhost:1"

# Restricted read scopes for the Google Health API v4 (developers.google.com/health/scopes).
GOOGLE_HEALTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.profile.readonly",
)

CLIENT_ID_ENV = "HERMES_GOOGLE_HEALTH_CLIENT_ID"
CLIENT_SECRET_ENV = "HERMES_GOOGLE_HEALTH_CLIENT_SECRET"

TESTING_MODE_NOTE = (
    "In Google 'Testing' publishing mode the googlehealth.* scopes are Restricted: "
    "the refresh token expires after ~7 days and the app is limited to <100 test "
    "users, so you must reconnect weekly until the OAuth app is verified."
)


class GoogleHealthNotConnected(RuntimeError):
    pass


class GoogleHealthAuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def token_path() -> Path:
    return store.hermes_home() / "google_health_token.json"


def pending_token_path(path: Path | None = None) -> Path:
    return oauth_token.pending_path(path or token_path())


def lock_path(path: Path | None = None) -> Path:
    return oauth_token.lock_path(path or token_path())


def pending_state_path() -> Path:
    return store.hermes_home() / "google_health_oauth_state.json"


def env_path() -> Path:
    return store.hermes_home() / ".env"


def client_secret_path() -> Path:
    return store.hermes_home() / "google_client_secret.json"


# --------------------------------------------------------------------------- #
# Token I/O
# --------------------------------------------------------------------------- #
def load_token(path: Path | None = None) -> dict[str, Any]:
    return oauth_token.load_token(
        path or token_path(),
        missing_exc=GoogleHealthNotConnected,
        missing_message=(
            "Run `hermes health connect-google-health` before Google Health sync."
        ),
        invalid_message="Google Health token file is invalid; reconnect.",
    )


def save_token(token: dict[str, Any], path: Path | None = None) -> Path:
    return oauth_token.save_token(token, path or token_path())


# --------------------------------------------------------------------------- #
# Client credentials (env -> .env -> google_client_secret.json)
# --------------------------------------------------------------------------- #
def load_client_credentials(*, required: bool = True) -> tuple[str | None, str | None]:
    client_id, client_secret = oauth_token.load_client_credentials(
        env_file=env_path(),
        id_key=CLIENT_ID_ENV,
        secret_key=CLIENT_SECRET_ENV,
        required=False,
    )
    if not client_id or not client_secret:
        file_id, file_secret = _read_client_secret_file()
        client_id = client_id or file_id
        client_secret = client_secret or file_secret
    if required and (not client_id or not client_secret):
        raise GoogleHealthNotConnected(
            "Google Health client credentials missing; re-run "
            "`hermes health connect-google-health`."
        )
    return client_id, client_secret


def save_client_credentials(client_id: str, client_secret: str) -> Path:
    return oauth_token.save_client_credentials(
        env_file=env_path(),
        values={CLIENT_ID_ENV: client_id, CLIENT_SECRET_ENV: client_secret},
    )


def clear_client_credentials() -> bool:
    return oauth_token.clear_client_credentials(
        env_file=env_path(), keys=[CLIENT_ID_ENV, CLIENT_SECRET_ENV]
    )


def _read_client_secret_file(path: Path | None = None) -> tuple[str | None, str | None]:
    """Read client_id/secret from a Google ``client_secret.json`` (installed/web)."""
    path = path or client_secret_path()
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None
    block = data.get("installed") or data.get("web") or {}
    if not isinstance(block, dict):
        return None, None
    return block.get("client_id"), block.get("client_secret")


# --------------------------------------------------------------------------- #
# Authorization URL
# --------------------------------------------------------------------------- #
def authorize_url(
    *,
    client_id: str,
    state: str,
    code_challenge: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scopes: tuple[str, ...] = GOOGLE_HEALTH_SCOPES,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTH_URL}?{parse.urlencode(params)}"


# --------------------------------------------------------------------------- #
# Refresh / token POST
# --------------------------------------------------------------------------- #
def refresh_access_token(
    *,
    client_id: str,
    client_secret: str,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]],
    now: Callable[..., Any] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    return oauth_token.refresh_access_token(
        token_file=path or token_path(),
        token_url=GOOGLE_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        http_post=http_post,
        now=now,
        not_connected_exc=GoogleHealthNotConnected,
        api_error_exc=GoogleHealthAuthError,
        missing_token_message=(
            "Run `hermes health connect-google-health` before Google Health sync."
        ),
        invalid_token_message="Google Health token file is invalid; reconnect.",
        missing_refresh_message="Google Health refresh token missing; reconnect.",
        invalid_grant_message=(
            "Google Health refresh failed (the Testing-mode refresh token may have "
            "expired after ~7 days); re-run `hermes health connect-google-health`."
        ),
        no_access_token_message=(
            "Google Health token refresh response did not include an access token."
        ),
    )


def http_post_form(
    url: str, data: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    return oauth_token.http_post_form(
        url,
        data,
        headers,
        error_exc=GoogleHealthAuthError,
        not_object_message="Google Health token endpoint response JSON was not an object.",
        http_error_message="Google Health token endpoint failed with status {status}.",
    )


# --------------------------------------------------------------------------- #
# Pending OAuth session (state + PKCE verifier)
# --------------------------------------------------------------------------- #
def save_pending(state: str, code_verifier: str, redirect_uri: str) -> Path:
    path = pending_state_path()
    oauth_token.write_json_atomic(
        path,
        {"state": state, "code_verifier": code_verifier, "redirect_uri": redirect_uri},
        private=True,
    )
    return path


def load_pending() -> dict[str, Any]:
    path = pending_state_path()
    if not path.exists():
        raise GoogleHealthNotConnected(
            "No pending Google Health OAuth session; restart with "
            "`hermes health connect-google-health`."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GoogleHealthNotConnected(
            "Pending Google Health OAuth session is invalid; restart connect."
        ) from exc
    if not isinstance(data, dict) or not data.get("state") or not data.get("code_verifier"):
        raise GoogleHealthNotConnected(
            "Pending Google Health OAuth session is missing PKCE data; restart connect."
        )
    return data


def clear_pending() -> None:
    with contextlib.suppress(FileNotFoundError):
        pending_state_path().unlink()


def _extract_code_and_state(code_or_url: str) -> tuple[str, str | None]:
    """Accept either a raw auth code or the full redirect URL pasted by the user."""
    if not code_or_url.startswith("http"):
        return code_or_url, None
    parsed = parse.urlparse(code_or_url)
    params = parse.parse_qs(parsed.query)
    code_values = params.get("code")
    if not code_values:
        raise GoogleHealthNotConnected(
            "No `code` parameter found in the pasted Google redirect URL."
        )
    state_values = params.get("state")
    return code_values[0], (state_values[0] if state_values else None)


# --------------------------------------------------------------------------- #
# Consent flow
# --------------------------------------------------------------------------- #
def registration_details(*, redirect_uri: str = DEFAULT_REDIRECT_URI) -> dict[str, Any]:
    return {
        "registration_url": GOOGLE_CREDENTIALS_URL,
        "redirect_uri": redirect_uri,
        "requested_scopes": list(GOOGLE_HEALTH_SCOPES),
        "testing_mode_note": TESTING_MODE_NOTE,
    }


def connect_google_health(
    *,
    client_id: str | None = None,
    client_secret: str | None = None,
    code: str | None = None,
    state: str | None = None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scopes: tuple[str, ...] = GOOGLE_HEALTH_SCOPES,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]
    | None = None,
    pkce: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Two-step BYO Google OAuth consent.

    Step 1 (no ``code``): persist client creds, mint a PKCE pair + state, save the
    pending session, and return the ``authorize_url`` for the user to open. Step 2
    (``code`` supplied, optionally as the full pasted redirect URL): validate state,
    exchange the code (with the PKCE ``code_verifier``), and save the token.
    """
    if client_id and client_secret:
        save_client_credentials(client_id, client_secret)
    else:
        client_id, client_secret = load_client_credentials(required=False)

    if not client_id or not client_secret:
        return {
            "ok": False,
            "connected": False,
            **registration_details(redirect_uri=redirect_uri),
            "guidance": (
                "Create a Google OAuth client of type 'Desktop app' at "
                "registration_url, enable the Google Health API, store the "
                "downloaded client JSON as google_client_secret.json (or pass "
                "--client-id/--client-secret), then re-run "
                "`hermes health connect-google-health`. " + TESTING_MODE_NOTE
            ),
        }

    if not code:
        verifier, challenge = pkce or oauth_token.generate_pkce_pair()
        oauth_state = state or secrets.token_urlsafe(24)
        save_pending(oauth_state, verifier, redirect_uri)
        url = authorize_url(
            client_id=client_id,
            state=oauth_state,
            code_challenge=challenge,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )
        return {
            "ok": False,
            "connected": False,
            "authorize_url": url,
            "state": oauth_state,
            **registration_details(redirect_uri=redirect_uri),
            "guidance": (
                "Open authorize_url, approve the Google Health permissions, then "
                "copy the `code` query parameter from the localhost redirect URL "
                "(the page will fail to load -- that is expected) and re-run "
                "`hermes health connect-google-health --code <code> --state <state>`. "
                + TESTING_MODE_NOTE
            ),
        }

    pending = load_pending()
    code, returned_state = _extract_code_and_state(code)
    final_state = state or returned_state
    if not final_state:
        clear_pending()
        raise GoogleHealthNotConnected(
            "Google Health OAuth state is required; restart connect."
        )
    if final_state != pending["state"] or (
        returned_state is not None and returned_state != pending["state"]
    ):
        clear_pending()
        raise GoogleHealthNotConnected(
            "Google Health OAuth state mismatch; restart connect."
        )

    token = oauth_token.exchange_code_for_token(
        token_url=GOOGLE_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=pending.get("redirect_uri", redirect_uri),
        http_post=http_post or http_post_form,
        api_error_exc=GoogleHealthAuthError,
        error_message="Google Health authorization failed.",
        missing_tokens_message=(
            "Google Health token response did not include required tokens "
            "(ensure access_type=offline and prompt=consent so a refresh token is issued)."
        ),
        extra_fields={"code_verifier": pending["code_verifier"]},
    )
    save_token(token)
    clear_pending()
    return {"ok": True, "connected": True, "token_path": str(token_path())}
