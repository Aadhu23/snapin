# SnapIN

Focus-mode study guard. Point your webcam at yourself, turn the guard on, and
SnapIN raises an alert whenever it sees a **cell phone** in frame — then keeps
score, so you can see whether your focus is actually improving.

| | |
| --- | --- |
| **Live detection** | YOLOv8 over 640×480 webcam frames at ~5 FPS, with confidence, size and shape gates that reject bottles, remotes and hands |
| **Accounts** | Sign up from the web, or sign in with Google. One account covers both |
| **Statistics** | Focus time, phone pickups and an hour-of-day breakdown, stored in PostgreSQL |

Detection runs on the server: the browser captures frames, sends them as JPEG
over a WebSocket, and renders whatever comes back.

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
- PostgreSQL 13 or newer, running
- A YOLO weights file (`yolov8s.pt` downloads on first run if absent)
- A modern browser. Camera access needs a secure context, so use `localhost`
  or HTTPS — opening the page as a `file://` URL will not work.

## Setup

```bash
cd ~/projects/snapin
python3 -m venv .venv
source .venv/bin/activate

# CPU-only PyTorch first: avoids ~2.5GB of unused CUDA libraries.
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# Configuration. Copy this first: the commands below read it.
cp .env.example .env
$EDITOR .env                       # set PGUSER / PGPASSWORD at minimum

# Create the database (once)
createdb snapin

# Optional: create the first account from the CLI.
# You can also do this from the web page, so this is not required.
python backend/manage_users.py add yourname

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

### Checking it works

With the server running, open <http://localhost:8000>, create an account from
the **Create an account** tab, then press **Start guard** and hold a phone in
frame of the camera.

To exercise the detection API without a webcam, use the local smoke-test client
(not tracked in git, since it targets a developer's machine):

```bash
SNAPIN_USERNAME=yourname SNAPIN_PASSWORD='yourpassword' python test.py
```

Or diagnose a saved frame without starting the server at all:

```bash
python backend/tune.py frame.jpg
```

## Configuration

Every setting is an environment variable. Put them in `.env` (see
`.env.example` for the full list with defaults) and the server loads that file
automatically at startup.

Two rules worth knowing:

- **A real environment variable beats the file.** `SNAPIN_PORT=9000 python
  backend/server.py` overrides `.env` for that one run.
- **`.env` is found relative to the project, not the working directory**, so it
  works whether you start the server from the root, from inside `backend/`, or
  through gunicorn.

If `python-dotenv` is not installed the app parses `.env` itself rather than
ignoring it.

The settings you are most likely to touch:

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
| `SNAPIN_SECRET` | *(generated)* | Signs OAuth state. Auto-generated into `backend/.secret` if unset |
| `PGHOST` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` | `localhost` / `snapin` / — / `snapin` | PostgreSQL connection, or use `DATABASE_URL` |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | *(unset)* | Enables **Continue with Google** |
| `SNAPIN_SAVE_DEBUG` | `0` | Write received frames to disk |

### Google sign-in

The login screen offers **Continue with Google**, implemented as a real OAuth
2.0 flow against `accounts.google.com`. SnapIN never asks for a Google
password — the browser goes to Google, comes back with an authorization code,
and SnapIN exchanges it server-side.

It is disabled until you create a client ID:

1. Open <https://console.cloud.google.com/apis/credentials>
2. **Create credentials → OAuth client ID → Web application**
3. Under *Authorised redirect URIs*, add exactly:
   `http://localhost:8000/api/auth/google/callback`
4. Copy the client ID and client secret into `.env`:

```bash
GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
```

Restart and the button becomes active. While it is unconfigured the button is
disabled with a note, so it never silently fails.

Linked Google accounts are stored under a `google:<sub>` key and have **no
password** — they can only sign in through Google. `email_verified` is enforced.

### Linking Google to an existing account

If you already have a password account and its email matches the Google
account you sign in with, SnapIN does **not** quietly create a second empty
account. It signs you in and asks you to confirm your password before
attaching the Google identity to the existing one:

```
https://localhost:8000/link-account
```

The password confirmation is the point. Without it, anyone could sign up using
someone else's email address and then quietly take over that person's account
the first time the real owner used Google sign-in. After linking, one account
answers to both methods and your stats stay together.

The account bar on the dashboard shows which methods are active, and offers
**Link Google** when it would do something.

Skipping the prompt keeps the two accounts separate — the Google one starts
empty, with no stats history.

### If you serve from a real hostname, set `GOOGLE_REDIRECT_URI` (and
`SNAPIN_HTTPS=1` so session cookies get the `Secure` flag) or Google will
reject the callback as a redirect-URI mismatch.

### Detection tuning

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

## Accounts

You can create an account from the web page — press **Create an account** on
the login screen, or go straight to <http://localhost:8000/?mode=register>.
Sign-up signs you in immediately, so there is no second step.

### From the CLI

Accounts live in PostgreSQL, so the database must be running:

```bash
python backend/manage_users.py add alice            # prompts for password
python backend/manage_users.py add alice 'mypass'   # or pass it directly
python backend/manage_users.py list
python backend/manage_users.py remove alice
```

Re-adding an existing username replaces the password, which is how you change
or reset one.

If you are coming from the file-based version, import the old accounts:

```bash
python backend/manage_users.py import-json backend/users.json
```

### Username rules

3–32 characters, letters and numbers plus `.`, `-` and `_`, starting with a
letter or number. `google:` is reserved. Being narrow here is deliberate —
usernames end up in cookies and log lines.

### Password rules

Eight characters minimum. Nothing else is enforced: composition rules
("one uppercase, one symbol") reliably push people toward predictable
substitutions like `Password1!`. The sign-up form *hints* at weak passwords
(length, all-digits) without blocking them.

### Brute-force protection

`backend/ratelimit.py` throttles the credential endpoints: 10 failed logins
per 5 minutes, 5 sign-ups per 10 minutes, 5 link attempts per 10 minutes.
Three consecutive failures lengthen the window, and a successful sign-in clears
the counter so a typo cannot lock you out.

Two honest limitations:

- **It is in-process.** Restarting clears the counters, and each gunicorn
  worker keeps its own, so `--workers 4` gives four separate budgets.
- **`X-Forwarded-For` is trusted** so limits still work behind a reverse
  proxy, and that header is trivially spoofable. These limits stop casual
  guessing, not a determined attacker. If you expose this to the public
  internet, move the counters to Redis or Postgres.

## Statistics

The Stats panel records what the guard actually saw, so you can tell whether
focus time is improving.

| Metric | What it means |
| --- | --- |
| **Focus time** | Total time the guard was running with the camera on |
| **Phone pickups** | Number of *episodes*, not frames — one pickup is one episode |
| **Time on phone** | Wall-clock seconds a phone was visible inside a session |
| **Focus ratio** | Share of studied time uninterrupted. The number to bring up. |

Two views are included: a stacked per-day chart (white is focus time, red is
the portion spent on the phone) and an hour-of-day chart showing when you are
most likely to get distracted.

Episode collapse is the important detail. At 5 FPS a single phone pickup spans
dozens of alerting frames. Logging each frame separately would report "1,340
detections" for what was really one distraction. Instead the first alerting
frame opens an episode, later frames extend it, and the first clear frame
closes it — so the count means something.

## Architecture

```
browser (getUserMedia, 640x480)
   │  canvas → JPEG q0.6, one frame in flight at a time
   ▼  WebSocket /ws/detect   (session cookie required)
backend/server.py           FastAPI, routes, auth endpoints
   │
   ├──► backend/detector.py     decode → YOLO → class + confidence + area + shape gates
   │
   ├──► backend/auth.py         PBKDF2 hashing, cookie helpers (no DB, no FastAPI)
   │
   └──► backend/stats.py        frame → episode state machine
            │
            ▼
        backend/db.py         connection pool, schema
            │
        PostgreSQL
```

### Data model

Four tables, all created automatically on startup.

```
users              ── one row per account
  id                BIGSERIAL PRIMARY KEY
  username          UNIQUE          "alice" or "google:<sub>"
  password_hash     NULL for Google-only accounts
  google_sub        UNIQUE          Google's stable user id
  email, display_name, picture
  created_at

sessions            ── server-side, so they can be revoked
  token             PRIMARY KEY      opaque random string
  user_id           → users(id) ON DELETE CASCADE
  created_at, expires_at, user_agent

focus_sessions      ── one row per guard run
  id, user_id
  started_at, ended_at
  frames_analyzed, phone_detections
  duration_seconds                  computed by Postgres, not the client

distractions        ── one row per phone sighting
  id, user_id, focus_session_id
  started_at, ended_at             NULL while the phone is visible
  peak_confidence                  highest score during the episode
  frames
```

Two details worth calling out:

**A partial unique index enforces one open episode per session:**

```sql
CREATE UNIQUE INDEX distractions_one_open_per_session
    ON distractions(focus_session_id)
    WHERE ended_at IS NULL AND focus_session_id IS NOT NULL;
```

That moves a rule from application code into the schema, so two concurrent
frames cannot open a second episode even if the state machine gets confused.

**Durations are computed by Postgres** with `EXTRACT(EPOCH FROM (now() -
started_at))`, not sent from the browser. A wrong system clock on the client
cannot corrupt the totals.

### Why server-side sessions

The earlier version used a stateless HMAC-signed cookie, which the server
validated without any lookup. Moving sessions into Postgres costs one indexed
`SELECT` per request and buys three things:

- **Real logout.** Deleting the row invalidates the token immediately.
- **Sign out everywhere.** Possible because sessions are enumerable per user.
- **No stolen-token window.** An old cookie used to be valid for up to 12
  hours with no way to kill it.

It also removes the need to keep `SNAPIN_SECRET` valid for old cookies; the
secret now only signs the OAuth `state` value.

### Database layer

`backend/db.py` owns the connection pool and schema; `backend/repository.py`
holds user and session queries; `backend/stats.py` holds focus and distraction
queries. Nothing outside those three files builds SQL, and every value is
bound as a parameter.

`auth.py` has no database import and no FastAPI import — it only hashes
passwords and sets cookies — so the cryptography can be tested without either.

## File layout

```
static/index.html      login and sign-up (one page, two modes)
static/link-account.html  confirm a password to link a Google identity
static/dashboard.html  webcam view, alert overlay, guard toggle
static/stats.html      statistics panel
backend/auth.py        PBKDF2 hashing, validators, cookie helpers (stdlib only)
backend/env.py         loads .env into the environment
backend/ratelimit.py   throttling for the credential endpoints
backend/db.py          connection pool and schema
backend/repository.py  user and session queries
backend/stats.py       focus sessions, distraction episodes, Stats queries
backend/google_auth.py Google OAuth 2.0 (stdlib only)
backend/detector.py    YOLO inference and detection gates
backend/manage_users.py  CLI for accounts
backend/tune.py        detection diagnostics
```

### API

| Endpoint | Purpose |
| --- | --- |
| `POST /api/login` | `{username, password}` → sets a session cookie |
| `POST /api/register` | `{username, password, confirm, email?}` → creates account and session |
| `POST /api/link/google` | Confirms the account password and links the pending Google identity |
| `POST /api/link/google/skip` | Declines linking, keeps the accounts separate |
| `POST /api/logout` | Clears the session cookie |
| `GET /api/me` | Current user, or `401` |
| `GET /api/config` | Feature flags for the login screen |
| `GET /api/stats?days=N` | Everything the Stats panel needs, one round trip |
| `GET /api/auth/google` | Redirects to Google's consent screen |
| `GET /api/auth/google/callback` | Completes the OAuth round trip |
| `GET /api/health` | Liveness plus model and database status |
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

## Docker

SnapIN needs a PostgreSQL server. The quickest local setup runs one in Docker
alongside the app:

```bash
docker network create snapin-net

docker run -d --name snapin-db --network snapin-net \
  -e POSTGRES_DB=snapin -e POSTGRES_USER=snapin -e POSTGRES_PASSWORD=snapin \
  postgres:17-alpine

docker build -t snapin .

docker run --rm -p 8000:8000 --network snapin-net \
  -e PGHOST=snapin-db -e PGPASSWORD=snapin \
  -e SNAPIN_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  snapin
```

Tables are created automatically on first start. Weights are baked into the
image, so the container needs no network at startup. For a managed database,
pass `DATABASE_URL` and skip the database container.

## Troubleshooting

**`could not reach the database`** — PostgreSQL is not running, or the
credentials are wrong. `/api/health` reports the driver's own error, which is
usually enough to tell which of the two it is.

**`no such table: users`** — the schema was never created. Run
`python backend/manage_users.py list`; it creates the schema on connect.

**`ModuleNotFoundError: No module named 'fastapi'`** — you ran the command
without the venv active. Use `source .venv/bin/activate` first, or call
`.venv/bin/python` directly.

**Port 8000 already in use** — an old copy is still running. Find the owner
rather than `pkill -f`, which would also match (and kill) your own shell:

```bash
ss -tlnp | grep 8000
PID=$(ss -tlnp | grep 8000 | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
kill "$PID"
```

**Camera prompt never appears** — you are probably not on `localhost`. Browsers
only grant camera access on a secure context, so the webcam fails silently on
any `http://192.168.x.x` address.

**Frontend ignores your changes** — the browser cached it. Hard reload with
`Ctrl+Shift+R` (or `Cmd+Shift+R`).

**`operator torchvision::nms does not exist`** — torch and torchvision versions
disagree. Reinstall both from the CPU index:

```bash
pip install --force-reinstall --no-deps torch torchvision \
  --index-url https://download.pytorch.org/whl/cpu
```

## Security notes

- Passwords are stored as PBKDF2-HMAC-SHA256 hashes (240k rounds, per-user
  salt). Plaintext is never written to disk.
- Sessions are opaque random tokens stored server-side, in an `HttpOnly` and
  `SameSite=Strict` cookie. Logging out deletes the row, so the token stops
  working immediately rather than at expiry. Add `SNAPIN_HTTPS=1` when serving
  over HTTPS so the cookie also gets the `Secure` flag.
- Every SQL statement binds its values as parameters; nothing is concatenated
  into a query string.
- Login compares in constant time, and burns an equivalent hash for unknown
  usernames so response timing does not reveal which accounts exist.
- PBKDF2 is slow on purpose, and the login path runs it in a worker thread so
  it cannot stall the event loop.
- Google sign-in uses a signed `state` value in an `HttpOnly` cookie, so a
  redirect cannot be forged or replayed. `email_verified` is enforced.
- CORS is off by default. Set `SNAPIN_ALLOWED_ORIGINS` only if you need it; it
  also controls which origins may open the WebSocket.
- The detector writes your camera frames to disk when `SNAPIN_SAVE_DEBUG=1`.
  Leave it off outside of debugging.
- `backend/users.json` and `backend/.secret` are both gitignored, and the
  secret file is written with `0600`. Neither is used any more — accounts live
  in PostgreSQL — but they stay ignored in case an old checkout has them.

## Known gaps

- **No email delivery.** Accounts cannot verify ownership of an address, so
  password reset is a CLI operation. That is also why Google linking requires
  the password rather than trusting an address that was never confirmed.
- **Rate limiting is in-process** (see *Brute-force protection*).
- **One shared model instance** serialises inference across all clients, so
  throughput does not scale horizontally yet. Raise
  `SNAPIN_DB_POOL_MAX` and add gunicorn workers before expecting more.
- **The schema is created by an idempotent script**, not a migration tool.
  Fine while the shape is stable; move to Alembic if it starts changing.
- **The dashboard page is served to anyone**; protection is enforced at the
  API and WebSocket layer instead.
- `/api/health` is anonymous and reveals whether the database and model are up.