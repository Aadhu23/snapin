# SnapIN

Focus-mode study guard. Point your webcam at yourself, turn the guard on, and
SnapIN raises an alert whenever it sees a **cell phone** in frame.

Detection is YOLOv8 (`yolov8s.pt`, COCO) running on the server. Frames come
from the browser as JPEG over a WebSocket; the server answers each frame with
whether a phone was found.

A detection must clear **three** gates:

| Gate | Default | Rejects |
| --- | --- | --- |
| `SNAPIN_CONF_THRESHOLD` | `0.4` | Low-confidence guesses |
| `SNAPIN_AREA_THRESHOLD` | `0.015` | A phone across the room |
| `SNAPIN_MIN_ASPECT` / `SNAPIN_MAX_ASPECT` | `0.28` / `0.92` | Remotes lying flat, hands, speaker grilles |

The confidence floor is a measured value rather than a guess: on live frames a
real phone scores around **0.42–0.67**, while the objects that get misread as
phones (water bottles, AC remotes, bare hands) land at **0.10–0.24**. The shape
gate is a cheap second opinion — a phone held up to a webcam is a tall
rectangle, and the shapes of the common false positives are not.

Defaults are tuned for a small, oblique phone in a 640×480 webcam frame:
`imgsz=320` with test-time augmentation on. That combination catches phones the
640/no-TTA setup misses, and runs at roughly 100 ms/frame.

## Requirements

- Python 3.10+
- A YOLO weights file (`yolov8s.pt` is downloaded on first run if absent)
- A modern browser. Camera access needs a secure context, so use `localhost`
  or HTTPS — opening the page as a `file://` URL will not work.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Create a login (prompts for a password, minimum 8 characters).
python backend/manage_users.py add yourname

# Copy the sample config and edit if you want to change thresholds.
cp .env.example .env

python backend/server.py
```

Then open <http://localhost:8000>.

> **Set `SNAPIN_SECRET` before deploying.** Without it, one is generated on
> first run and saved to `backend/.secret` (gitignored) so sessions survive a
> restart. Set it explicitly in production so the secret is not tied to a file
> on one machine.
>
> ```bash
> python -c "import secrets; print(secrets.token_urlsafe(48))"
> ```

### Running the tests

```bash
# Terminal 1
python backend/server.py

# Terminal 2
python test.py                    # defaults to phone.png
python test.py path/to/your.jpg
```

## Configuration

Every setting is an environment variable; `.env.example` documents all of them
with defaults. The ones you are most likely to touch:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SNAPIN_PORT` | `8000` | Port to bind |
| `SNAPIN_MODEL` | `yolov8s.pt` | Weights file |
| `SNAPIN_CONF_THRESHOLD` | `0.4` | Minimum confidence (lower = more sensitive, more false alerts) |
| `SNAPIN_AREA_THRESHOLD` | `0.015` | Minimum fraction of frame a phone must cover |
| `SNAPIN_MIN_ASPECT` | `0.28` | Min bounding-box width/height ratio |
| `SNAPIN_MAX_ASPECT` | `0.92` | Max bounding-box width/height ratio |
| `SNAPIN_IMGSZ` | `320` | Inference resolution. See below before raising |
| `SNAPIN_TTA` | `1` | Test-time augmentation; better recall, ~60ms/frame |
| `SNAPIN_SECRET` | *(random)* | Session signing key. **Set this.** |
| `SNAPIN_USERS` | *(unset)* | Users as JSON; overrides `backend/users.json` |
| `SNAPIN_SAVE_DEBUG` | `0` | Write received frames to disk |

### Tuning detection

**Start with the diagnostic tool.** It explains *why* a phone was missed
instead of just returning `found: false`:

```bash
python backend/tune.py webcam_frame.jpg
```

It prints every phone candidate with the two gate scores, which gate blocked
it, and what the other classes were. Some useful flags:

```bash
python backend/tune.py frame.jpg --sweep-imgsz      # score at each imgsz
python backend/tune.py frame.jpg --tta               # with augmentation
python backend/tune.py frame.jpg --conf 0.05         # try a lower floor
python backend/tune.py frame.jpg --compare-models yolov8s.pt yolo11s.pt
```

A negative result is also visible live: every detection response carries a
`reason` (`confidence_too_low`, `phone_too_small`, `no_phone_detected`) and a
`hint` with the threshold value that would have caught it, and the dashboard
logs it to the browser console.

To ignore a phone sitting far away on a desk, raise the area threshold:

```bash
SNAPIN_AREA_THRESHOLD=0.05 python backend/server.py
```

**If desk objects trigger false alerts**, raise the confidence floor rather than
the area threshold — a bottle or remote is usually large in frame, so area will
not separate it from a phone:

```bash
SNAPIN_CONF_THRESHOLD=0.5 python backend/server.py
```

If a remote lying flat is the specific problem, tighten the shape gate:

```bash
SNAPIN_MAX_ASPECT=0.75 python backend/server.py
```

Detection only runs once per frame while a single frame is in flight, so a slow
inference pass slows the feed instead of piling up messages.

### Why `imgsz=320`

Measured on a small phone/no-phone test set, `imgsz=320` matched `imgsz=640`
on sensitivity (3/3 detections, zero false positives) while running ~3.5x
faster, because small-object recall degrades sharply once the letterbox scale
drops. `imgsz=768` and above is actively worse — the phone score collapsed from
0.57 to 0.04 on the same image. Raise `imgsz` only if you need to catch phones
that are very small in frame, and confirm with `tune.py --sweep-imgsz`.

Test-time augmentation (`SNAPIN_TTA`, on by default) scores a small or oblique
phone 3–10x higher. It costs ~60 ms/frame at `imgsz=320`. Set
`SNAPIN_TTA=0` to halve latency if you are CPU-bound.

> **Note on `phone.png`:** this image is a screenshot of an *annotated* YOLO
> render, so the drawn boxes and labels are baked into the pixels. It is a
> poor detection target and its scores swing by two orders of magnitude with
> `imgsz`. Prefer a raw webcam frame for testing.

## Docker

```bash
docker build -t snapin .
docker run -p 8000:8000 -e SNAPIN_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" snapin
```

The image bakes the YOLO weights in at build time, so it does not need network
access at startup. It runs as a non-root user and exposes `/api/health` for
health checks.

## Architecture

```
browser (getUserMedia, 640x480)
   │  canvas → JPEG q0.6, one frame in flight at a time
   ▼  WebSocket /ws/detect   (session cookie required)
backend/server.py           FastAPI, CORS, auth endpoints
   │
   ▼  run_in_threadpool  (keeps the event loop free)
backend/detector.py         decode → YOLO → class + confidence + area gates
```

```
static/index.html      login page
static/dashboard.html  webcam view, alert overlay, guard toggle
backend/auth.py        PBKDF2 password hashing, HMAC-signed session cookies
backend/manage_users.py  CLI for adding/removing users
```

### API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/login` | `{username, password}` → sets a session cookie |
| `POST /api/logout` | Clears the session cookie |
| `GET /api/me` | Current user, or `401` |
| `GET /api/health` | Liveness plus model status |
| `WS /ws/detect` | Send JPEG bytes → get a detection result |

Detection responses carry both shapes, so either field works:

```json
{
  "status": "ok",
  "alert": true,
  "detections": { "found": true, "confidence": 0.62, "rel_area": 0.041, "bbox": [404.4, 134.3, 466.5, 271.4] }
}
```

A non-alert response explains itself instead of returning a bare `false`:

```json
{
  "status": "ok",
  "alert": false,
  "detections": {
    "found": false,
    "confidence": 0.19,
    "rel_area": 0.031,
    "aspect": 0.5,
    "reason": "confidence_too_low",
    "hint": "Lower SNAPIN_CONF_THRESHOLD to ~0.19 to catch this."
  }
}
```

`reason` is one of `confidence_too_low`, `phone_too_small`, `wrong_shape`, or
`no_phone_detected`.

The WebSocket closes with `4401` when the session is missing or expired; the
frontend then returns you to the login page.

## Security notes

- Passwords are stored as PBKDF2-HMAC-SHA256 hashes (240k rounds, per-user
  salt). Plaintext is never written to disk.
- Session cookies are `HttpOnly` and `SameSite=Strict`. Add
  `SNAPIN_HTTPS=1` when serving over HTTPS so they also get the `Secure` flag.
- CORS is off by default. Set `SNAPIN_ALLOWED_ORIGINS` only if you need it; it
  also controls which origins may open the WebSocket.
- The detector writes your camera frames to disk when `SNAPIN_SAVE_DEBUG=1`.
  Leave it off outside of debugging.
- `backend/users.json` and `backend/.secret` are both gitignored, and the
  secret file is written with `0600`. Regenerate credentials on each deploy
  rather than committing them.

## Known gaps

- **Stats is a stub.** There is no persistence layer yet, so there is nothing
  behind the Stats button.
- **Sessions are stateless.** There is no database, no session history, and no
  record of alerts or focus streaks.
- One shared model instance serialises inference across all clients, so
  throughput does not scale horizontally yet.