"""SnapIN HTTP + WebSocket server.

Fixes relative to the original single-file server:

* a real ``uvicorn`` entrypoint, so ``python backend/server.py`` actually serves
  (previously it loaded the model and exited);
* the static frontend is mounted and reachable at ``/``;
* the response payload now carries the ``alert`` boolean the frontend reads,
  while preserving the richer ``detections`` object;
* inference runs in a worker thread so a slow frame cannot stall the event loop;
* incoming frames are size-capped;
* the duplicated ``except`` blocks are gone;
* login/logout/session endpoints, with the WebSocket handshake gated on the
  session cookie.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent

# Make sibling modules importable regardless of how the process is launched
# (``python backend/server.py``, ``uvicorn server:app`` from inside backend/,
# or ``uvicorn backend.server:app`` from the project root).
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

# Load .env BEFORE anything reads configuration. Several settings below are
# module-level constants, and db.migrate() needs the database credentials, so
# this has to happen first. A real environment variable still wins, which is
# what makes `SNAPIN_PORT=9000 python backend/server.py` work.
import env as env_loader  # noqa: E402

env_loader.load_env()

import auth  # noqa: E402
import db  # noqa: E402
import ratelimit  # noqa: E402
import google_auth  # noqa: E402
import repository  # noqa: E402
import stats as stats_store  # noqa: E402
from detector import StudyGuardDetector  # noqa: E402

logging.basicConfig(
    level=os.environ.get("SNAPIN_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("snapin.server")

# Create tables and repair anything a crash left open.
try:
    db.migrate()
    # Any episode left open by a crash would report an inflated duration.
    stats_store.close_stale_distractions()
except Exception as exc:  # noqa: BLE001 - keep serving so health reports it
    log.error("Database unavailable: %s", exc)
    db.close_pool()

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

STATIC_DIR = PROJECT_ROOT / "static"

# Reject absurd uploads before decoding them into memory.
MAX_FRAME_BYTES = int(os.environ.get("SNAPIN_MAX_FRAME_BYTES", 5 * 1024 * 1024))
HOST = os.environ.get("SNAPIN_HOST", "0.0.0.0")
PORT = int(os.environ.get("SNAPIN_PORT", "8000"))

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("SNAPIN_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

app = FastAPI(title="SnapIN", version="1.0.0")

if ALLOWED_ORIGINS:
    from fastapi.middleware.cors import CORSMiddleware

    # Credentials + a wildcard origin is rejected by browsers, so an explicit
    # allowlist is required for cookie-based auth to work cross-origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )
    log.info("CORS enabled for: %s", ", ".join(ALLOWED_ORIGINS))
else:
    # Same-origin is the default deployment, so CORS is off unless configured.
    # This also removes the invalid allow_origins=["*"] + allow_credentials=True
    # combination from the original code.
    log.info("CORS disabled (same-origin only)")

# --------------------------------------------------------------------------
# Detector singleton
# --------------------------------------------------------------------------

try:
    detector = StudyGuardDetector()
    log.info("Model loaded successfully")
except Exception as exc:  # noqa: BLE001 - keep serving so /api/health reports it
    log.error("Failed to load model: %s", exc)
    detector = None

# --------------------------------------------------------------------------
# Auth endpoints
# --------------------------------------------------------------------------


@app.post("/api/register")
async def api_register(request: Request):
    """Create an account and sign in, so sign-up is one round trip."""
    from fastapi.concurrency import run_in_threadpool

    client = ratelimit.client_address(request)

    allowed, retry_after = ratelimit.register_limiter.check("register", client)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many sign-up attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Expected a JSON body.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")

    # ValidationError messages are written for humans, so they go straight
    # back to the user rather than being logged and replaced.
    try:
        username = auth.validate_username(str(payload.get("username", "")))
        password = auth.validate_password(str(payload.get("password", "")))
        email = auth.validate_email(payload.get("email"))
    except auth.ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if payload.get("confirm") is not None and payload["confirm"] != password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    row = await run_in_threadpool(
        repository.register_password_user,
        username,
        auth.hash_password(password),
        email,
    )
    if row is None:
        ratelimit.register_limiter.record_failure("register", client)
        raise HTTPException(
            status_code=409,
            detail=f"The username {username!r} is taken. Try signing in instead.",
        )

    ratelimit.register_limiter.record_success("register", client)

    identity = repository.identity_from_row(row)
    token = repository.create_session(
        row["id"], user_agent=request.headers.get("user-agent")
    )

    response = JSONResponse(
        {"status": "ok", "user": identity.as_dict(), "created": True},
        status_code=201,
    )
    auth.set_session_cookie(response, token)
    log.info("Registered %r", username)
    return response


@app.post("/api/login")
async def api_login(request: Request):
    """Validate credentials and open a session.

    PBKDF2 at 240k rounds takes tens of milliseconds by design, so the check
    runs in a worker thread. Inline it would stall every other request for the
    duration.
    """
    from fastapi.concurrency import run_in_threadpool

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body
        raise HTTPException(status_code=400, detail="Expected a JSON body.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")

    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    client = ratelimit.client_address(request)
    allowed, retry_after = ratelimit.login_limiter.check("login", client, username)
    if not allowed:
        log.warning("Login rate limit hit for %r from %s", username, client)
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        row = await run_in_threadpool(_verify_credentials, username, password)
    except auth.AuthError as exc:
        ratelimit.login_limiter.record_failure("login", client, username)
        if ratelimit.login_limiter.strikes_count("login", client, username) >= 3:
            ratelimit.login_limiter.penalise("login", client, username)
        log.info("Failed login attempt for %r from %s", username, client)
        raise HTTPException(status_code=401, detail=str(exc))

    ratelimit.login_limiter.record_success("login", client, username)

    identity = repository.identity_from_row(row)
    token = repository.create_session(
        row["id"], user_agent=request.headers.get("user-agent")
    )

    response = JSONResponse({"status": "ok", "user": identity.as_dict()})
    auth.set_session_cookie(response, token)
    log.info("User %r logged in", identity.username)
    return response


def _verify_credentials(username: str, password: str) -> dict:
    """Blocking credential check. Never called from the event loop directly."""
    row = repository.get_user(username)

    if row is None or not row.get("password_hash"):
        # Burn an equivalent hash so a missing account and a wrong password
        # take the same time, and neither reveals which usernames exist.
        auth.hash_password(password)
        raise auth.AuthError("Invalid username or password.")

    record = repository.decode_password_hash(row["password_hash"])
    if not auth.verify_password(password, record):
        raise auth.AuthError("Invalid username or password.")

    return row


@app.post("/api/logout")
async def api_logout(request: Request):
    """Close this session and clear the cookie."""
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if token:
        repository.delete_session(token)

    response = JSONResponse({"status": "ok"})
    auth.clear_session_cookie(response)
    return response


@app.get("/api/config")
async def api_config():
    """Feature flags the login screen needs before anyone is signed in."""
    return {
        "status": "ok",
        "google_sign_in": google_auth.is_configured(),
    }


@app.get("/api/me")
async def api_me(request: Request):
    """Report the current session, or 401 when anonymous."""
    identity = auth.current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return {"status": "ok", "user": identity.as_dict()}


@app.get("/api/health")
async def api_health():
    """Report whether the app can actually do its job.

    Returns 503 when either the model or the database is unavailable, so an
    orchestrator can act on the status code instead of having to parse the
    body. The JSON always names which part is broken.
    """
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse

    db_ok, db_error = await run_in_threadpool(db.available)
    model_ok = detector is not None
    healthy = model_ok and db_ok

    payload = {
        "status": "ok" if healthy else "degraded",
        "model_loaded": model_ok,
        "model_path": getattr(detector, "model_path", None),
        "database": "ok" if db_ok else db_error,
        "google_sign_in": google_auth.is_configured(),
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


@app.get("/api/stats")
async def api_stats(request: Request, days: int = 7):
    """Everything the Stats panel needs, in one round trip.

    One endpoint rather than four: the panel renders all at once, and four
    requests would show a panel whose sections disagreed with each other.
    """
    identity = auth.require_identity(request)

    from fastapi.concurrency import run_in_threadpool

    days = max(1, min(days, 365))
    data = await run_in_threadpool(_build_stats, identity.username, days)
    return data


def _build_stats(username: str, days: int) -> dict:
    row = repository.get_user(username)
    if row is None:
        return {"status": "error", "error": "Unknown user."}

    user_id = row["id"]
    session = stats_store.get_open_session(user_id)

    return {
        "status": "ok",
        "range_days": days,
        "summary": stats_store.summary(user_id, days=days),
        "daily": stats_store.daily_breakdown(user_id, days=days),
        "hourly": stats_store.hourly_pattern(user_id, days=30),
        "recent": stats_store.recent_distractions(user_id, limit=15),
        # Lets the panel show a live indicator without a second request.
        "active_session": (
            {
                "id": session["id"],
                "started_at": session["started_at"].isoformat(),
                "frames_analyzed": session["frames_analyzed"],
                "phone_detections": session["phone_detections"],
            }
            if session
            else None
        ),
    }


# --------------------------------------------------------------------------
# Google OAuth
# --------------------------------------------------------------------------


@app.get("/api/auth/google")
async def google_start():
    """Redirect to Google's consent screen, stashing a signed state cookie."""
    if not google_auth.is_configured():
        return JSONResponse(
            {
                "status": "not_configured",
                "error": (
                    "Google sign-in is not configured. Set GOOGLE_CLIENT_ID and "
                    "GOOGLE_CLIENT_SECRET; see README for the setup steps."
                ),
            },
            status_code=503,
        )

    state = google_auth.build_state(auth.get_secret())
    response = RedirectResponse(google_auth.authorization_url(state), status_code=302)
    response.set_cookie(
        google_auth.STATE_COOKIE,
        state,
        max_age=google_auth.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",  # must survive the redirect back from accounts.google.com
        secure=auth._cookie_secure(),
        path="/api/auth",
    )
    return response


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Complete the OAuth round trip and establish a local session."""
    if not google_auth.is_configured():
        return _login_error_page("Google sign-in is not configured.")

    if error:
        log.info("Google returned an error: %s", error)
        return _login_error_page(
            "Google sign-in was cancelled or refused."
            if error == "access_denied"
            else f"Google sign-in failed ({error})."
        )

    expected_state = request.cookies.get(google_auth.STATE_COOKIE, "")
    if not google_auth.verify_state(state, auth.get_secret()):
        log.warning("Rejected Google callback with bad state")
        return _login_error_page(
            "That sign-in link expired or was tampered with. Please try again."
        )
    if not expected_state or not _constant_time_eq(expected_state, state):
        log.warning("Rejected Google callback: state cookie mismatch")
        return _login_error_page("That sign-in link expired. Please try again.")
    if not code:
        return _login_error_page("Google did not return an authorization code.")

    try:
        token = google_auth.exchange_code(code)
        profile = google_auth.fetch_profile(token)
    except google_auth.GoogleAuthError as exc:
        return _login_error_page(str(exc))

    # If a password account already uses this address, do not silently create a
    # second, empty account. Sign the visitor in and ask them to confirm the
    # password before attaching the Google identity to it.
    existing = repository.get_user_by_email(profile.email)
    if existing is not None:
        pending = google_auth.build_pending_profile(profile, auth.get_secret())
        identity = repository.identity_from_row(existing)
        session_token = repository.create_session(existing["id"])

        response = RedirectResponse("/link-account", status_code=302)
        auth.set_session_cookie(response, session_token)
        response.set_cookie(
            LINK_COOKIE,
            pending,
            max_age=google_auth.STATE_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=auth._cookie_secure(),
            path="/",
        )
        response.delete_cookie(google_auth.STATE_COOKIE, path="/api/auth")
        log.info(
            "Google sign-in matched existing account %r; awaiting confirmation",
            existing["username"],
        )
        return response

    row = google_auth.upsert_google_user(profile)
    identity = repository.identity_from_row(row)
    session_token = repository.create_session(row["id"])

    response = RedirectResponse("/dashboard", status_code=302)
    auth.set_session_cookie(response, session_token)
    response.delete_cookie(google_auth.STATE_COOKIE, path="/api/auth")
    log.info("Google sign-in succeeded for %s", profile.email)
    return response


# --------------------------------------------------------------------------
# Linking a Google identity to an existing password account
# --------------------------------------------------------------------------

LINK_COOKIE = "snapin_pending_google"


@app.post("/api/link/google")
async def api_link_google(request: Request):
    """Attach the pending Google identity to the signed-in account.

    Reached from the Google callback when the address already belongs to a
    password account. That account's own password is required, so signing up
    with someone else's email address cannot be used to take over their
    account when that person later signs in with Google.
    """
    from fastapi.concurrency import run_in_threadpool

    identity = auth.require_identity(request)

    # Validate with read_pending_profile, not verify_state: the two tokens use
    # different formats, and verify_state rejects a perfectly good pending
    # profile.
    sub, google_email, google_name, picture = google_auth.read_pending_profile(
        request.cookies.get(LINK_COOKIE, ""), auth.get_secret()
    )
    if not sub:
        raise HTTPException(
            status_code=400, detail="That link request expired. Start again."
        )

    client = ratelimit.client_address(request)
    allowed, retry_after = ratelimit.link_limiter.check("link", client, identity.username)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Expected a JSON body.")

    password = str((payload or {}).get("password", ""))
    if not password:
        raise HTTPException(status_code=400, detail="Enter your current password.")

    user_row = repository.get_user(identity.username)
    if user_row is None or not user_row.get("password_hash"):
        raise HTTPException(
            status_code=400,
            detail="This account has no password to confirm. Use Continue with "
            "Google instead.",
        )

    try:
        await run_in_threadpool(_verify_credentials, identity.username, password)
    except auth.AuthError:
        ratelimit.link_limiter.record_failure("link", client, identity.username)
        raise HTTPException(status_code=401, detail="That password is not correct.")

    ratelimit.link_limiter.record_success("link", client, identity.username)

    row = repository.link_google_account(
        user_row["id"], sub, google_email, google_name, picture
    )

    linked = repository.identity_from_row(row)
    response = JSONResponse({"status": "ok", "user": linked.as_dict()})
    response.delete_cookie(LINK_COOKIE, path="/")
    return response


@app.post("/api/link/google/skip")
async def api_link_google_skip():
    """Decline linking and keep the separate Google account."""
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(LINK_COOKIE, path="/")
    return response


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(a.encode(), b.encode())


def _login_error_page(message: str):
    """Send the user back to the login screen with a readable reason."""
    from urllib.parse import quote

    return RedirectResponse(f"/?error={quote(message)}", status_code=302)


# --------------------------------------------------------------------------
# Detection WebSocket
# --------------------------------------------------------------------------

_CLOSE_UNAUTHENTICATED = 4401
_CLOSE_MODEL_UNAVAILABLE = 1011


@app.websocket("/ws/detect")
async def websocket_endpoint(websocket: WebSocket):
    # Browsers cannot attach headers to a WebSocket handshake, so the session
    # cookie is the credential. Origin checking covers the CSRF case where a
    # third-party site rides an existing cookie.
    if not _origin_allowed(websocket):
        await websocket.close(code=_CLOSE_UNAUTHENTICATED, reason="Origin not allowed")
        return

    try:
        identity = auth.require_identity_ws(websocket)
    except auth.AuthError:
        await websocket.close(code=_CLOSE_UNAUTHENTICATED, reason="Authentication required")
        return

    await websocket.accept()
    log.info("Client connected: %s", identity.username)

    if detector is None:
        await websocket.close(code=_CLOSE_MODEL_UNAVAILABLE, reason="Model not loaded")
        return

    from fastapi.concurrency import run_in_threadpool

    # Tracking state for this connection.
    #
    # ``open_distraction`` is the episode id of a phone sighting that has not
    # ended yet. The first alerting frame opens an episode, later alerting
    # frames extend it, and the first clear frame closes it. Without that,
    # 5 FPS would log a phone pickup as forty separate distractions.
    user_row = repository.get_user(identity.username)
    user_id = user_row["id"] if user_row else None
    focus_session_id: int | None = None
    open_distraction: int | None = None

    if user_id is not None:
        focus_session_id = await run_in_threadpool(
            stats_store.start_focus_session, user_id
        )

    try:
        while True:
            data = await websocket.receive_bytes()

            if not data:
                await websocket.send_json(
                    {"status": "error", "alert": False, "error": "Empty frame"}
                )
                continue

            if len(data) > MAX_FRAME_BYTES:
                await websocket.send_json(
                    {
                        "status": "error",
                        "alert": False,
                        "error": f"Frame too large ({len(data)} bytes, max {MAX_FRAME_BYTES})",
                    }
                )
                continue

            # Run inference off the event loop. Without this, a single slow
            # frame blocks every other request in the process.
            results = await run_in_threadpool(detector.detect_phone, data)
            found = bool(results.get("found"))

            if user_id is not None and focus_session_id is not None:
                try:
                    open_distraction = await run_in_threadpool(
                        _record_frame,
                        user_id,
                        focus_session_id,
                        found,
                        float(results.get("confidence") or 0.0),
                        open_distraction,
                    )
                except Exception:  # noqa: BLE001 - never break detection over stats
                    log.exception("Could not record frame for stats")

            await websocket.send_json(
                {
                    "status": "ok",
                    # The frontend keys off this flag.
                    "alert": found,
                    "detections": results,
                }
            )
    except WebSocketDisconnect:
        log.info("Client disconnected: %s", identity.username)
    except Exception as exc:  # noqa: BLE001 - one bad client must not kill the server
        log.exception("Error during detection: %s", exc)
        try:
            await websocket.close(code=1011, reason="Detection error")
        except RuntimeError:
            pass
    finally:
        # Close the episode and the session even if the socket dropped, so
        # durations are not left open.
        if user_id is not None and focus_session_id is not None:
            try:
                await run_in_threadpool(
                    _finish_tracking,
                    user_id,
                    focus_session_id,
                    open_distraction,
                )
            except Exception:  # noqa: BLE001
                log.exception("Could not close the focus session")


def _record_frame(
    user_id: int,
    focus_session_id: int,
    found: bool,
    confidence: float,
    open_distraction: int | None,
) -> int | None:
    """Apply one frame to the tracking state machine.

    Returns the new ``open_distraction`` value, which the caller carries to
    the next frame.
    """
    if found:
        if open_distraction is None:
            open_distraction = stats_store.begin_distraction(
                user_id, focus_session_id, confidence, 0.0
            )
        else:
            stats_store.extend_distraction(open_distraction, confidence)
    elif open_distraction is not None:
        stats_store.end_distraction(open_distraction)
        open_distraction = None

    stats_store.bump_session_counters(focus_session_id, 1, 1 if found else 0)
    return open_distraction


def _finish_tracking(user_id: int, focus_session_id: int, open_distraction: int | None) -> None:
    if open_distraction is not None:
        stats_store.end_distraction(open_distraction)
    stats_store.end_focus_session(focus_session_id)


def _origin_allowed(websocket: WebSocket) -> bool:
    """Reject WebSocket handshakes from unexpected origins.

    Same-origin requests carry no Origin header on some clients, so those are
    allowed; a present Origin must be in the allowlist.
    """
    if not ALLOWED_ORIGINS:
        return True
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    return origin in ALLOWED_ORIGINS


# --------------------------------------------------------------------------
# Static frontend
# --------------------------------------------------------------------------

STATIC_DIR.mkdir(parents=True, exist_ok=True)

def _page_response(filename: str) -> FileResponse:
    target = STATIC_DIR / filename
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(target)


# Friendly URLs registered before the catch-all mount, so they win over it.
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return _page_response("index.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard() -> FileResponse:
    return _page_response("dashboard.html")


@app.get("/stats", include_in_schema=False)
async def stats_page() -> FileResponse:
    return _page_response("stats.html")


@app.get("/link-account", include_in_schema=False)
async def link_account_page() -> FileResponse:
    return _page_response("link-account.html")


class _RevalidatingStaticFiles(StaticFiles):
    """Serve static assets with revalidation instead of blind browser caching.

    Without this, Chrome keeps a stale dashboard.js in its memory cache and
    silently runs old frontend code against a new backend, which makes fixes
    look like they had no effect. `no-cache` still allows a 304 when nothing
    changed, so it costs one conditional request per asset.
    """

    def file_response(self, *args, **kwargs) -> FileResponse:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Mounted last so it never shadows /api/* or /ws/*.
app.mount(
    "/",
    _RevalidatingStaticFiles(directory=str(STATIC_DIR), html=True),
    name="static",
)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    log.info("Starting SnapIN on http://%s:%d", HOST, PORT)
    # Pass the app object rather than an import string: the module path differs
    # depending on whether this was launched as `python backend/server.py` or
    # via `uvicorn server:app`, and an import string would need a matching
    # sys.path entry to resolve.
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        reload=False,
        log_level=os.environ.get("SNAPIN_LOG_LEVEL", "info").lower(),
    )