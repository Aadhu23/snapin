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
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import auth  # noqa: E402
from detector import StudyGuardDetector  # noqa: E402

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

logging.basicConfig(
    level=os.environ.get("SNAPIN_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("snapin.server")

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


@app.post("/api/login")
async def api_login(request: Request):
    """Validate credentials and set a signed session cookie."""
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

    try:
        identity = auth.authenticate(username, password)
    except auth.AuthError as exc:
        log.info("Failed login attempt for %r", username)
        raise HTTPException(status_code=401, detail=str(exc))

    response = JSONResponse({"status": "ok", "user": identity.as_dict()})
    auth.set_session_cookie(response, identity)
    log.info("User %r logged in", identity.username)
    return response


@app.post("/api/logout")
async def api_logout():
    """Clear the session cookie."""
    response = JSONResponse({"status": "ok"})
    auth.clear_session_cookie(response)
    return response


@app.get("/api/me")
async def api_me(request: Request):
    """Report the current session, or 401 when anonymous."""
    identity = auth.current_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return {"status": "ok", "user": identity.as_dict()}


@app.get("/api/health")
async def api_health():
    """Liveness probe that also surfaces model status."""
    return {
        "status": "ok" if detector is not None else "degraded",
        "model_loaded": detector is not None,
        "model_path": getattr(detector, "model_path", None),
        "users_configured": len(auth.load_users()) > 0,
    }


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