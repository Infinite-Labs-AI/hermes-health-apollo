"""Provider-neutral OAuth2 token primitives (pure stdlib).

Extracted from ``oura.py`` so multiple BYO-OAuth connectors (Oura, Google
Health) share one audited implementation of the fiddly bits: atomic private
file writes, cross-platform file locking, the ``.pending`` refresh sidecar,
token-expiry checks with clock skew, form-encoded token POSTs, PKCE pair
generation, and the lock+pending refresh algorithm.

Everything here is parameterized -- each caller supplies its own token file
path, token URL, client-credential env keys, scope, and (for messages users
will see) its own exception classes and message strings. This module imports no
sibling modules and reads no global config, so it stays connector-neutral and
trivially testable.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping
from urllib import error as urllib_error
from urllib import parse, request as urllib_request


class TokenError(RuntimeError):
    """Generic OAuth/token transport failure (default ``api_error`` class)."""


class NotConnected(RuntimeError):
    """No usable token or credentials exist (default ``not_connected`` class)."""


# --------------------------------------------------------------------------- #
# Sidecar paths
# --------------------------------------------------------------------------- #
def pending_path(token_file: Path) -> Path:
    """Backup written before a refresh POST so a crash mid-refresh can recover."""
    return token_file.with_name(f"{token_file.name}.pending")


def lock_path(token_file: Path) -> Path:
    """Advisory lock file that serializes concurrent refreshes of one token."""
    return token_file.with_name(f"{token_file.name}.lock")


# --------------------------------------------------------------------------- #
# Atomic private file writes
# --------------------------------------------------------------------------- #
def write_json_atomic(path: Path, payload: dict[str, Any], *, private: bool = True) -> None:
    """Write ``payload`` as JSON via a temp file + ``os.replace`` (atomic).

    When ``private`` is set the file is ``chmod 0o600`` on POSIX so secrets are
    not world/group readable. Keys are sorted for stable, diff-friendly output.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        if private and os.name != "nt":
            tmp_path.chmod(0o600)
        os.replace(tmp_path, path)
        if private and os.name != "nt":
            path.chmod(0o600)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


# --------------------------------------------------------------------------- #
# Cross-platform file locking
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _platform_lock(handle: Any) -> Iterator[None]:
    if os.name == "nt":
        try:
            import msvcrt
        except ImportError:
            yield
            return
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    try:
        import fcntl
    except ImportError:
        yield
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def token_lock(token_file: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of a refresh."""
    lock_file = lock_path(token_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+b") as handle:
        with _platform_lock(handle):
            yield


# --------------------------------------------------------------------------- #
# Token file I/O
# --------------------------------------------------------------------------- #
def load_token(
    token_file: Path,
    *,
    missing_exc: type[Exception] = NotConnected,
    missing_message: str = "No OAuth token found; connect first.",
    invalid_message: str = "OAuth token file is invalid; reconnect.",
) -> dict[str, Any]:
    if not token_file.exists():
        raise missing_exc(missing_message)
    with token_file.open("r", encoding="utf-8") as handle:
        token = json.load(handle)
    if not isinstance(token, dict):
        raise missing_exc(invalid_message)
    return token


def save_token(token: dict[str, Any], token_file: Path) -> Path:
    write_json_atomic(token_file, token, private=True)
    return token_file


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def token_expired(
    token: Mapping[str, Any],
    *,
    now: Callable[[], datetime] | None = None,
    skew: timedelta = timedelta(minutes=5),
) -> bool:
    """True if the token is missing/malformed ``expires_at`` or expires within ``skew``."""
    expires_at = token.get("expires_at")
    if not expires_at:
        return True
    clock = now or (lambda: datetime.now(UTC))
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= clock() + skew


# --------------------------------------------------------------------------- #
# Form-encoded token POST
# --------------------------------------------------------------------------- #
def http_post_form(
    url: str,
    data: dict[str, Any],
    headers: dict[str, str] | None = None,
    *,
    error_exc: type[Exception] = TokenError,
    not_object_message: str = "Token endpoint response JSON was not an object.",
    http_error_message: str = "Token endpoint failed with status {status}.",
    timeout: float = 30,
) -> dict[str, Any]:
    """POST ``data`` form-encoded and return the parsed JSON object.

    On an HTTP error whose body is still a JSON object (e.g. ``{"error":
    "invalid_grant"}``) the object is returned so callers can branch on
    ``error``; only a non-JSON-object error body raises ``error_exc``.
    """
    encoded = parse.urlencode(data).encode("utf-8")
    req = urllib_request.Request(url, data=encoded, method="POST", headers=headers or {})
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            body = _read_json_body(response, error_exc=error_exc)
    except urllib_error.HTTPError as exc:
        body = _read_json_body(exc, error_exc=error_exc)
        if not isinstance(body, dict):
            raise error_exc(http_error_message.format(status=exc.code)) from exc
    if not isinstance(body, dict):
        raise error_exc(not_object_message)
    return body


def _read_json_body(response: Any, *, error_exc: type[Exception]) -> Any:
    try:
        raw = response.read().decode("utf-8")
    except OSError as exc:
        raise error_exc("OAuth response body could not be read.") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise error_exc("OAuth response body was not valid JSON.") from exc


# --------------------------------------------------------------------------- #
# Refresh (lock + pending sidecar + grant_type=refresh_token)
# --------------------------------------------------------------------------- #
def refresh_access_token(
    *,
    token_file: Path,
    token_url: str,
    client_id: str,
    client_secret: str,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]],
    now: Callable[[], datetime] | None = None,
    not_connected_exc: type[Exception] = NotConnected,
    api_error_exc: type[Exception] = TokenError,
    missing_token_message: str = "No OAuth token found; connect first.",
    invalid_token_message: str = "OAuth token file is invalid; reconnect.",
    missing_refresh_message: str = "OAuth refresh token missing; reconnect.",
    invalid_grant_message: str = "OAuth refresh failed; reconnect.",
    no_access_token_message: str = "Token refresh response did not include an access token.",
) -> dict[str, Any]:
    """Refresh the stored token if expired, returning the (possibly) refreshed token.

    The whole operation is serialized by a file lock; the prior token is written
    to a ``.pending`` sidecar before the network call so a crash can recover; the
    refresh token is preserved when the provider does not rotate it; and
    ``expires_at`` is recomputed from ``expires_in``.
    """
    clock = now or (lambda: datetime.now(UTC))
    with token_lock(token_file):
        current = load_token(
            token_file,
            missing_exc=not_connected_exc,
            missing_message=missing_token_message,
            invalid_message=invalid_token_message,
        )
        if not token_expired(current, now=clock):
            return current
        refresh_token = current.get("refresh_token")
        if not refresh_token:
            raise not_connected_exc(missing_refresh_message)

        write_json_atomic(pending_path(token_file), current, private=True)
        response = http_post(
            token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.get("error") == "invalid_grant":
            raise not_connected_exc(invalid_grant_message)
        if "access_token" not in response:
            raise api_error_exc(no_access_token_message)

        refreshed = dict(current)
        refreshed["access_token"] = response["access_token"]
        refreshed["refresh_token"] = response.get("refresh_token", refresh_token)
        if "scope" in response:
            refreshed["scope"] = response["scope"]
        expires_in = int(response.get("expires_in", 3600))
        refreshed["expires_at"] = (clock() + timedelta(seconds=expires_in)).isoformat()

        save_token(refreshed, token_file)
        with contextlib.suppress(FileNotFoundError):
            pending_path(token_file).unlink()
        return refreshed


# --------------------------------------------------------------------------- #
# Authorization-code exchange (supports PKCE via extra_fields)
# --------------------------------------------------------------------------- #
def exchange_code_for_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    http_post: Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]],
    now: Callable[[], datetime] | None = None,
    api_error_exc: type[Exception] = TokenError,
    error_message: str = "Authorization failed.",
    missing_tokens_message: str = "Token response did not include required tokens.",
    require_refresh_token: bool = True,
    extra_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Exchange an authorization ``code`` for a token, stamping ``expires_at``.

    ``extra_fields`` lets callers add provider-specific parameters such as the
    PKCE ``code_verifier``.
    """
    clock = now or (lambda: datetime.now(UTC))
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if extra_fields:
        payload.update(extra_fields)
    response = http_post(
        token_url, payload, {"Content-Type": "application/x-www-form-urlencoded"}
    )
    if response.get("error"):
        raise api_error_exc(error_message)
    if "access_token" not in response or (
        require_refresh_token and "refresh_token" not in response
    ):
        raise api_error_exc(missing_tokens_message)
    token = dict(response)
    expires_in = int(token.get("expires_in", 3600))
    token["expires_at"] = (clock() + timedelta(seconds=expires_in)).isoformat()
    return token


# --------------------------------------------------------------------------- #
# PKCE (RFC 7636, S256)
# --------------------------------------------------------------------------- #
def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for the S256 PKCE method."""
    verifier = secrets.token_urlsafe(64)  # ~86 chars, within the 43-128 range
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# --------------------------------------------------------------------------- #
# .env-backed client credentials
# --------------------------------------------------------------------------- #
def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = unquote_env_value(value.strip())
    return values


def quote_env_value(value: str) -> str:
    return json.dumps(str(value))


def unquote_env_value(value: str) -> str:
    if not value:
        return ""
    with contextlib.suppress(json.JSONDecodeError):
        decoded = json.loads(value)
        if isinstance(decoded, str):
            return decoded
    return value.strip("'\"")


def load_client_credentials(
    *,
    env_file: Path,
    id_key: str,
    secret_key: str,
    env: Mapping[str, str] | None = None,
    required: bool = True,
    missing_exc: type[Exception] = NotConnected,
    missing_message: str = "OAuth client credentials missing.",
) -> tuple[str | None, str | None]:
    """Resolve client id/secret from the environment, then the ``.env`` file."""
    environ = os.environ if env is None else env
    client_id = environ.get(id_key)
    client_secret = environ.get(secret_key)
    if (not client_id or not client_secret) and env_file.exists():
        values = read_env_file(env_file)
        client_id = client_id or values.get(id_key)
        client_secret = client_secret or values.get(secret_key)
    if required and (not client_id or not client_secret):
        raise missing_exc(missing_message)
    return client_id, client_secret


def save_client_credentials(*, env_file: Path, values: dict[str, str]) -> Path:
    """Merge ``values`` into the ``.env`` file, preserving other keys, mode 0o600."""
    existing = read_env_file(env_file) if env_file.exists() else {}
    existing.update(values)
    lines = [f"{key}={quote_env_value(value)}" for key, value in sorted(existing.items())]
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        env_file.chmod(0o600)
    return env_file


def clear_client_credentials(*, env_file: Path, keys: list[str]) -> bool:
    """Remove ``keys`` from the ``.env`` file; unlink it if nothing else remains."""
    if not env_file.exists():
        return False
    values = read_env_file(env_file)
    removed = False
    for key in keys:
        if key in values:
            removed = True
            values.pop(key, None)
    if not removed:
        return False
    if not values:
        with contextlib.suppress(FileNotFoundError):
            env_file.unlink()
        return True
    lines = [f"{key}={quote_env_value(value)}" for key, value in sorted(values.items())]
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        env_file.chmod(0o600)
    return True
