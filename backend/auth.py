"""Password hashing and session-token crypto for SnapIN.

This module is deliberately free of both storage and web-framework concerns:

* passwords are hashed and verified here (:func:`hash_password`,
  :func:`verify_password`);
* users and sessions are *stored* in :mod:`backend.repository` and
  :mod:`backend.db`;
* HTTP and WebSocket plumbing lives in :mod:`backend.server`.

Keeping it that way means the cryptography has no database connection and no
FastAPI import, so it can be unit-tested on its own.

Passwords use PBKDF2-HMAC-SHA256 with a per-user salt. Plaintext is never
stored anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import Request, Response, WebSocket

# FastAPI is imported inside the web helpers rather than at module level, so
# that command-line tools (manage_users.py) work with the standard library
# alone. Password hashing and the user store have no business depending on a
# web framework.

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
    display_name: str | None = None
    email: str | None = None
    picture: str | None = None
    provider: str = "password"
    # Whether a password still works for this account. An account linked to
    # Google can have both, and the client needs to know.
    can_use_password: bool = True

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "username": self.username,
            "provider": self.provider,
            "can_use_password": self.can_use_password,
        }
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.email:
            payload["email"] = self.email
        if self.picture:
            payload["picture"] = self.picture
        return payload



def get_secret() -> bytes:
    """Return the signing secret, preferring an explicit environment value."""
    configured = os.environ.get("SNAPIN_SECRET", "").strip()
    if configured:
        return configured.encode("utf-8")
    return _load_or_create_secret()


class ValidationError(ValueError):
    """Input the user can correct. Message is safe to show verbatim."""


# Deliberately narrow. Usernames end up in a cookie and in log lines, and this
# project has no need for unicode or whitespace in one.
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")

# Practical rather than exhaustive: one @, a dotted domain, no spaces. The only
# authority on an address is a message sent to it, which this project does not
# do, so over-validating would just reject valid addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 200  # PBKDF2 cost is linear in length; cap it

RESERVED_USERNAME_PREFIX = "google:"


def validate_username(username: str) -> str:
    """Check a username, returning the cleaned value or raising."""
    username = (username or "").strip()

    if not username:
        raise ValidationError("Enter a username.")
    if username.lower().startswith(RESERVED_USERNAME_PREFIX):
        raise ValidationError("That username starts with a reserved prefix.")
    if not _USERNAME_RE.match(username):
        raise ValidationError(
            "Username must be 3-32 characters, using letters, numbers, "
            "dot, dash or underscore, and start with a letter or number."
        )
    return username


def validate_password(password: str) -> str:
    if not password:
        raise ValidationError("Enter a password.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )
    return password


def validate_email(email: str | None) -> str | None:
    """Check an optional email. Blank means 'not provided'."""
    if email is None:
        return None
    email = email.strip()
    if not email:
        return None
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValidationError("That does not look like a valid email address.")
    return email.lower()


def password_problems(password: str) -> list[str]:
    """Non-blocking strength hints for the sign-up form.

    Returns problems rather than raising, because weak-but-acceptable
    passwords should be *flagged*, not blocked. Length is the real control;
    composition rules mostly push people toward predictable substitutions.
    """
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) >= MIN_PASSWORD_LENGTH and password.isdigit():
        problems.append("not only numbers")
    if password and password.lower() == password and len(set(password)) <= 3:
        problems.append("not a single repeated character")
    return problems


def _b64e(raw: bytes) -> str:
    """URL-safe base64 without padding, so a hash is storable as plain text."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


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
# Sessions
# --------------------------------------------------------------------------
# Tokens are opaque random strings, not signed payloads. They are minted and
# resolved in :mod:`backend.repository`, which stores them in Postgres. That
# is what makes a session revocable -- with the previous self-validating cookie
# there was no way to invalidate one before it expired.

def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
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
    """Resolve the session cookie to a user. One indexed SELECT."""
    import repository

    return repository.identity_from_row(
        repository.get_session_user(request.cookies.get(COOKIE_NAME, ""))
    )


def current_identity_ws(websocket: WebSocket) -> Identity | None:
    """WebSocket auth.

    Browsers cannot set headers on a WebSocket handshake, so the session
    cookie is the only credential available. Same-origin deployments send it
    automatically.
    """
    import repository

    return repository.identity_from_row(
        repository.get_session_user(websocket.cookies.get(COOKIE_NAME, ""))
    )


def require_identity(request: Request) -> Identity:
    """FastAPI dependency that rejects anonymous HTTP requests."""
    from fastapi import HTTPException

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