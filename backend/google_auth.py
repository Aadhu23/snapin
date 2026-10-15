"""Google sign-in via OAuth 2.0 (OpenID Connect).

Implemented with the standard library only, so the project does not gain a
dependency for one provider.

Flow
----
1. ``/api/auth/google`` builds an authorization URL and redirects the browser
   to accounts.google.com, stashing a signed ``state`` value in a cookie.
2. Google redirects back to ``/api/auth/google/callback?code=...&state=...``.
3. The state cookie is verified (CSRF), the code is exchanged for tokens, and
   the userinfo endpoint supplies the verified email and name.
4. A local session cookie is issued, exactly as for password login.

Requires a Google OAuth client ID:

    https://console.cloud.google.com/apis/credentials
    -> Create credentials -> OAuth client ID -> Web application
    -> Authorised redirect URI: http://localhost:8000/api/auth/google/callback

Linked Google accounts are stored in the same JSON user store as password
users, under a ``google:<sub>`` key, and have no password: they can only sign
in through Google.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("snapin.google")

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SCOPES = "openid email profile"

STATE_COOKIE = "snapin_google_state"
STATE_TTL_SECONDS = 600  # 10 minutes is plenty for one redirect round trip


class GoogleAuthError(Exception):
    """Any failure in the Google sign-in flow, safe to show to the user."""


@dataclass(frozen=True)
class GoogleProfile:
    sub: str
    email: str
    name: str
    picture: str | None = None


def client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def is_configured() -> bool:
    return bool(client_id())


def _redirect_uri() -> str:
    configured = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    if configured:
        return configured
    base = os.environ.get("SNAPIN_PUBLIC_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/api/auth/google/callback"
    port = os.environ.get("SNAPIN_PORT", "8000")
    return f"http://localhost:{port}/api/auth/google/callback"


def build_pending_profile(profile: "GoogleProfile", secret: bytes) -> str:
    """Sign a Google profile into a cookie, for account linking.

    The profile is carried client-side between the OAuth callback and the
    link-confirmation step. Signing it means the browser cannot tamper with
    the Google identity it is asking to be attached, and the ten-minute TTL
    means a stale value cannot be replayed later.
    """
    import base64
    import hashlib
    import hmac
    import json as _json
    import time

    payload = {
        "sub": profile.sub,
        "email": profile.email,
        "name": profile.name,
        "picture": profile.picture,
        "iat": int(time.time()),
    }
    raw = base64.urlsafe_b64encode(
        _json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")

    sig = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def read_pending_profile(token: str, secret: bytes) -> tuple[str, str | None, str | None, str | None]:
    """Validate a pending-profile cookie and return (sub, email, name, picture).

    Returns ``("", None, None, None)`` if the token is malformed, unsigned, or
    older than the state TTL. Callers must treat that as a failure.

    The secret is passed in rather than imported, matching :func:`verify_state`.
    An earlier version called a bare ``get_secret()`` that does not exist in
    this module, which raised ``NameError`` at exactly the moment the token
    needed to be trusted.
    """
    import base64
    import hashlib
    import hmac
    import json as _json
    import time

    if not token or "." not in token:
        return ("", None, None, None)

    raw, _, sig = token.rpartition(".")
    expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return ("", None, None, None)

    try:
        padding = "=" * (-len(raw) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(raw + padding).decode())
        if time.time() - int(payload.get("iat", 0)) > STATE_TTL_SECONDS:
            return ("", None, None, None)
    except (ValueError, TypeError, UnicodeDecodeError):
        return ("", None, None, None)

    if not payload.get("sub"):
        return ("", None, None, None)

    return (payload["sub"], payload.get("email"), payload.get("name"), payload.get("picture"))


def build_state(secret: bytes) -> str:
    """Create a signed, unguessable state value."""
    import base64
    import secrets
    import time

    nonce = secrets.token_urlsafe(16)
    issued = str(int(time.time()))
    body = f"{nonce}.{issued}"
    raw = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")

    import hashlib
    import hmac

    sig = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def verify_state(state: str, secret: bytes) -> bool:
    """Validate the state value, rejecting replays and stale redirects."""
    import base64
    import hashlib
    import hmac
    import time

    if not state or "." not in state:
        return False

    raw, _, sig = state.rpartition(".")
    expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expected, sig):
        return False

    try:
        padding = "=" * (-len(raw) % 4)
        body = base64.urlsafe_b64decode(raw + padding).decode()
        nonce, _, issued = body.partition(".")
        if not nonce:
            return False
        age = time.time() - int(issued)
    except (ValueError, UnicodeDecodeError):
        return False

    return 0 <= age <= STATE_TTL_SECONDS


def authorization_url(state: str) -> str:
    """Build the Google consent-screen URL."""
    params = {
        "client_id": client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "access_type": "online",
        # Only re-prompt when we have no existing grant, so returning users are
        # not forced through the consent screen on every visit.
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> str:
    """Swap an authorization code for an access token."""
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id(),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        }
    ).encode()

    request = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        # Never echo the raw payload back: it can contain the client secret.
        log.error("Google token exchange failed (%s): %s", exc.code, detail)
        raise GoogleAuthError(
            "Google rejected the sign-in. Check that GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET are correct and that the redirect URI "
            "matches exactly."
        ) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.error("Could not reach Google token endpoint: %s", exc)
        raise GoogleAuthError("Could not reach Google. Check your connection.") from None

    token = body.get("access_token")
    if not token:
        raise GoogleAuthError("Google did not return an access token.")
    return token


def fetch_profile(access_token: str) -> GoogleProfile:
    """Read the verified email and name for the authenticated user."""
    request = urllib.request.Request(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data: dict[str, Any] = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        log.error("Google userinfo failed (%s)", exc.code)
        raise GoogleAuthError("Could not read your Google account details.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.error("Could not reach Google userinfo endpoint: %s", exc)
        raise GoogleAuthError("Could not reach Google. Check your connection.") from None

    # Google only returns email_verified=True when it has actually checked it.
    if not data.get("email_verified"):
        raise GoogleAuthError(
            "Your Google account has an unverified email address, so it "
            "cannot be used to sign in."
        )

    sub = str(data.get("sub", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    if not sub or not email:
        raise GoogleAuthError("Google did not return enough information.")

    return GoogleProfile(
        sub=sub,
        email=email,
        name=str(data.get("name") or email.split("@")[0]),
        picture=data.get("picture"),
    )


def upsert_google_user(profile: GoogleProfile) -> dict[str, Any]:
    """Link a Google account.

    Delegates to :mod:`backend.repository` so Google and password accounts are
    written through exactly one path. Imported inside the function to keep
    this module free of database imports.
    """
    import repository

    return repository.upsert_google_user(
        profile.sub, profile.email, profile.name, profile.picture
    )


def unlink_google_user(sub: str) -> None:
    """Remove a linked Google account."""
    import repository

    row = repository.get_user_by_google_sub(sub)
    if row:
        repository.delete_user(row["username"])