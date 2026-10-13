"""Authentication for SnapIN.

Deliberately dependency-free and storage-light so the project stays runnable
before we settle on a database. Users live either in a JSON file
(``users.json``) or in the ``SNAPIN_USERS`` environment variable, and sessions
are stateless HMAC-signed cookies.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with a per-user salt.
Plaintext passwords are never written to disk; when users are configured
through the environment, hashing happens on first successful login and is
cached in memory only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response, WebSocket

log = logging.getLogger("snapin.auth")

COOKIE_NAME = "snapin_session"
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours

_PBKDF2_ROUNDS = 240_000
_SECRET_BYTES = 32

# A fixed-but-random secret is generated per process if none is configured.
# That means restarting the server logs everybody out, which is the safe
# default for a project without a configured secret.
_SECRET_FILE = Path(__file__).resolve().parent / ".secret"


def _load_or_create_secret() -> bytes:
    """Return a stable secret, generating and persisting one on first use.

    Without this, a fresh random secret per process silently logs every user
    out on restart, which reads as "the app broke when I restarted it".
    SNAPIN_SECRET still wins when set.
    """
    if _SECRET_FILE.exists():
        try:
            stored = _SECRET_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored.encode("utf-8")
        except OSError:
            pass

    secret = secrets.token_urlsafe(_SECRET_BYTES)
    try:
        _SECRET_FILE.write_text(secret, encoding="utf-8")
        _SECRET_FILE.chmod(0o600)
        log.warning(
            "SNAPIN_SECRET is not set. Generated one and saved it to %s so "
            "sessions survive a restart. Set SNAPIN_SECRET in production, and "
            "keep this file out of version control.",
            _SECRET_FILE,
        )
    except OSError:
        # Read-only filesystem: sessions will not survive a restart, but the
        # server still works for the current process.
        log.warning(
            "SNAPIN_SECRET is not set and %s is not writable. Sessions will "
            "not survive a restart.",
            _SECRET_FILE,
        )
    return secret.encode("utf-8")


class AuthError(Exception):
    """Raised when credentials are rejected."""


@dataclass(frozen=True)
class Identity:
    username: str

    def as_dict(self) -> dict[str, str]:
        return {"username": self.username}


def _b64e(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    import base64

    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def get_secret() -> bytes:
    """Return the signing secret, preferring an explicit environment value."""
    configured = os.environ.get("SNAPIN_SECRET", "").strip()
    if configured:
        return configured.encode("utf-8")
    return _load_or_create_secret()


def hash_password(password: str, salt: bytes | None = None) -> dict[str, Any]:
    """Hash a password with PBKDF2-HMAC-SHA256."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS
    )
    return {
        "algo": "pbkdf2_sha256",
        "rounds": _PBKDF2_ROUNDS,
        "salt": _b64e(salt),
        "hash": _b64e(digest),
    }


def verify_password(password: str, record: dict[str, Any]) -> bool:
    """Constant-time verification of a password against a stored hash record."""
    try:
        salt = _b64d(record["salt"])
        expected = _b64d(record["hash"])
        rounds = int(record.get("rounds", _PBKDF2_ROUNDS))
    except (KeyError, ValueError, TypeError):
        return False

    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, rounds
    )
    return hmac.compare_digest(digest, expected)


# --------------------------------------------------------------------------
# User store
# --------------------------------------------------------------------------

def _users_path() -> Path:
    configured = os.environ.get("SNAPIN_USERS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent / "users.json"


def _load_users_from_env() -> dict[str, dict[str, Any]]:
    """Parse ``SNAPIN_USERS`` as ``{"user": "plaintext-password"}`` or a JSON list.

    Values that are already hash records are passed through untouched.
    """
    raw = os.environ.get("SNAPIN_USERS", "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Fall back to the friendlier "alice:pw,bob:pw" form.
        parsed = {}
        for pair in raw.split(","):
            if ":" not in pair:
                continue
            name, _, secret = pair.partition(":")
            name, secret = name.strip(), secret.strip()
            if name and secret:
                parsed[name] = secret

    users: dict[str, dict[str, Any]] = {}
    if isinstance(parsed, dict):
        items = parsed.items()
    elif isinstance(parsed, list):
        items = [(entry.get("username", ""), entry) for entry in parsed]
    else:
        return {}

    for name, value in items:
        if not name:
            continue
        if isinstance(value, dict) and "hash" in value:
            users[name] = value
        elif isinstance(value, str):
            # Plaintext password supplied via env. Hash lazily on first login so
            # a plaintext secret is never persisted anywhere.
            users[name] = {"_plaintext": value}
    return users


def load_users() -> dict[str, dict[str, Any]]:
    """Load the user store, merging the env config over the JSON file."""
    users: dict[str, dict[str, Any]] = {}

    path = _users_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for name, value in data.items():
                    if isinstance(value, dict) and "hash" in value:
                        users[name] = value
                    elif isinstance(value, str):
                        users[name] = {"_plaintext": value}
            log.info("Loaded %d user(s) from %s", len(users), path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read %s: %s", path, exc)

    env_users = _load_users_from_env()
    if env_users:
        users.update(env_users)
        log.info("Loaded %d user(s) from SNAPIN_USERS", len(env_users))

    return users


def set_user(username: str, password: str) -> None:
    """Create or replace a user in the JSON store."""
    path = _users_path()
    users: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                users = loaded
        except json.JSONDecodeError:
            log.warning("Overwriting malformed user file %s", path)

    users[username] = hash_password(password)
    path.write_text(json.dumps(users, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    log.info("Stored user %r in %s", username, path)


def authenticate(username: str, password: str) -> Identity:
    """Validate credentials, raising :class:`AuthError` on failure."""
    users = load_users()

    if not users:
        raise AuthError(
            "No users are configured. Run "
            "'python backend/manage_users.py add <name>' or set SNAPIN_USERS."
        )

    record = users.get(username)
    if record is None:
        # Hash a throwaway value so a missing user costs roughly the same as a
        # wrong password, which keeps login timing from leaking valid names.
        hash_password(password)
        raise AuthError("Invalid username or password.")

    if "_plaintext" in record:
        if not hmac.compare_digest(record["_plaintext"].encode(), password.encode()):
            raise AuthError("Invalid username or password.")
    elif not verify_password(password, record):
        raise AuthError("Invalid username or password.")

    return Identity(username=username)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def issue_token(identity: Identity, issued_at: int | None = None) -> str:
    """Build a signed, self-expiring session token."""
    issued_at = int(time.time()) if issued_at is None else issued_at
    payload = f"{identity.username}:{issued_at}"
    body = _b64e(payload.encode("utf-8"))
    signature = hmac.new(get_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def read_token(token: str) -> Identity | None:
    """Validate a session token and return its identity, or None."""
    if not token or "." not in token:
        return None

    body, _, signature = token.rpartition(".")
    if not body or not signature:
        return None

    expected = hmac.new(get_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None

    try:
        payload = _b64d(body).decode("utf-8")
        username, _, issued_at = payload.rpartition(":")
        issued_at_i = int(issued_at)
    except (ValueError, UnicodeDecodeError):
        return None

    if not username:
        return None
    if time.time() - issued_at_i > SESSION_TTL_SECONDS:
        return None

    return Identity(username=username)


def set_session_cookie(response: Response, identity: Identity) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_token(identity),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _cookie_secure() -> bool:
    """Only mark cookies Secure when actually served over HTTPS."""
    flag = os.environ.get("SNAPIN_COOKIE_SECURE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return os.environ.get("SNAPIN_HTTPS", "").strip().lower() in {"1", "true", "yes"}


def current_identity(request: Request) -> Identity | None:
    return read_token(request.cookies.get(COOKIE_NAME, ""))


def current_identity_ws(websocket: WebSocket) -> Identity | None:
    """WebSocket auth.

    Browsers cannot set headers on a WebSocket handshake, so the session
    cookie is the only credential available. Same-origin deployments send it
    automatically.
    """
    return read_token(websocket.cookies.get(COOKIE_NAME, ""))


def require_identity(request: Request) -> Identity:
    """FastAPI dependency that rejects anonymous HTTP requests."""
    identity = current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return identity


def require_identity_ws(websocket: WebSocket) -> Identity:
    """Close the socket with 4401 if the caller is not authenticated."""
    identity = current_identity_ws(websocket)
    if identity is None:
        raise AuthError("Authentication required.")
    return identity