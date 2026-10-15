# SnapIN

Focus-mode study guard. Point your webcam at yourself, start the guard, and SnapIN alerts you whenever it detects a **cell phone** in frame — then records your focus history so you can track improvements over time.

---

## Features

- **Live Detection**: Real-time YOLOv8 inference over 640×480 webcam frames (~5 FPS) with confidence, size, and shape gates to reject bottles, remotes, and hands.
- **Episode Tracking**: Consecutive alerting frames collapse into single distraction *episodes*, measuring actual phone pickups rather than raw frame counts.
- **Focus Statistics**: Dashboard with focus time, distraction pickups, uninterrupted focus ratio, and hour-of-day distraction trends.
- **Authentication**: Built-in accounts, optional Google OAuth 2.0, safe account linking, and server-side revocable sessions.
- **Docker-Ready**: Self-contained container with pre-baked model weights and CPU-only PyTorch (~1GB image, no CUDA bloat).

---

## Quickstart

### Option A: Docker (Fastest)

Run PostgreSQL and SnapIN with Docker:

```bash
docker network create snapin-net

docker run -d --name snapin-db --network snapin-net \
  -e POSTGRES_DB=snapin -e POSTGRES_USER=snapin -e POSTGRES_PASSWORD=snapin \
  postgres:17-alpine

docker build -t snapin .

docker run --rm -it -p 8000:8000 --network snapin-net \
  -e PGHOST=snapin-db -e PGPASSWORD=snapin \
  snapin
```

Open **<http://localhost:8000>** in your browser.

---

### Option B: Local Setup

#### 1. Prerequisites
- Python 3.10+
- PostgreSQL 13+ running locally

#### 2. Install

```bash
# Set up virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install CPU-only PyTorch first (avoids ~2.5 GB of unused CUDA packages)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

#### 3. Configure Database

```bash
cp .env.example .env
createdb snapin
```

Edit `.env` with your PostgreSQL credentials (`PGUSER`, `PGPASSWORD`, or `DATABASE_URL`). Tables and schema are created automatically on startup.

#### 4. Run

```bash
python backend/server.py
```

Open **<http://localhost:8000>**, create an account, and click **Start guard**.

---

## How It Works

```
Browser (Webcam 640x480)
   │  Canvas → JPEG (q0.6), 1 frame in flight via WebSocket /ws/detect
   ▼
FastAPI Backend (backend/server.py)
   │
   ├──► YOLOv8 Detection (backend/detector.py)
   │    Cleared against 3 gates: Confidence (≥0.4) · Area (≥0.015) · Aspect (0.28–0.92)
   │
   ├──► Stats State Machine (backend/stats.py)
   │    First alert opens episode · Following frames extend · Clear frame ends episode
   │
   └──► PostgreSQL Database (backend/db.py)
        Users · Server sessions · Focus sessions · Distraction episodes
```

---

## Essential Configuration

All settings are configured via environment variables in `.env`. Key settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | *(empty)* | PostgreSQL URI (e.g., `postgresql://user:pass@localhost:5432/snapin`) |
| `PGHOST` / `PGUSER` / `PGPASSWORD` | `localhost` / `snapin` / *(empty)* | Discrete database connection options |
| `SNAPIN_PORT` | `8000` | Port to bind |
| `SNAPIN_CONF_THRESHOLD` | `0.4` | Confidence floor (0.0–1.0). Real phones score ~0.42–0.67 |
| `SNAPIN_AREA_THRESHOLD` | `0.015` | Minimum fraction of frame covered (increase to ignore distant phones) |
| `SNAPIN_MIN_ASPECT` / `SNAPIN_MAX_ASPECT` | `0.28` / `0.92` | Width/height ratio gate (rejects flat remotes, keyboards, hands) |
| `SNAPIN_SECRET` | *(generated)* | Signs OAuth state cookies (persisted to `backend/.secret` if unset) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | *(empty)* | Optional: Enables **Continue with Google** OAuth |

*See [.env.example](.env.example) for all available parameters.*

---

## CLI & Developer Tools

### Account Management
Manage user accounts without the web UI:

```bash
python backend/manage_users.py add alice           # Create/reset user password
python backend/manage_users.py list                # View existing users & auth provider
python backend/manage_users.py remove alice        # Remove user and associated stats
```

### Frame Diagnostics & Threshold Tuning
Diagnose why an image did or did not trigger an alert:

```bash
python backend/tune.py frame.jpg                   # Inspect detected objects & gate scores
python backend/tune.py frame.jpg --sweep-imgsz     # Test inference resolutions (320–960)
python backend/tune.py frame.jpg --tta             # Test with Test-Time Augmentation
python backend/tune.py frame.jpg --conf 0.05       # Try a lower confidence floor
python backend/tune.py a.jpg b.jpg --compare-models yolov8s.pt yolo11s.pt
```

### Headless WebSocket Smoke Test
Test the detection WebSocket without a browser or webcam. `test.py` is a local
helper and is not tracked in git, so it is absent from a fresh clone:

```bash
SNAPIN_USERNAME=alice SNAPIN_PASSWORD='secret' python test.py [path/to/image.jpg]
```

---

## API Summary

| Route | Type | Description |
| --- | --- | --- |
| `/` | Page | Login and registration |
| `/dashboard` | Page | Webcam guard monitor |
| `/stats` | Page | Focus and distraction analytics |
| `/link-account` | Page | Confirm a password to link a Google identity to an existing account |
| `POST /api/register` | REST | Create account and receive session cookie |
| `POST /api/login` | REST | Authenticate with username and password |
| `POST /api/logout` | REST | Invalidate session |
| `POST /api/link/google` | REST | Link the pending Google identity after password confirmation |
| `GET /api/stats?days=7` | REST | Aggregated metrics, daily bars, and hourly patterns |
| `GET /api/health` | REST | `200` only when model **and** database are up, else `503` |
| `WS /ws/detect` | WebSocket | Send JPEG bytes → receive `{alert: bool, detections: {...}}` |

---

## Troubleshooting

- **Database Connection Error**: Verify PostgreSQL is running and credentials in `.env` match. Check `/api/health` for connection details.
- **Camera Permission Blocked**: Browsers require a secure context (`localhost` or `https://`). Remote IP addresses like `http://192.168.x.x` will block camera access.
- **Port Conflict**: Find and kill lingering server processes:
  ```bash
  kill $(ss -tlnp | grep 8000 | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
  ```
- **Torchvision NMS Mismatch**: If you see `operator torchvision::nms does not exist`, reinstall matching CPU wheels:
  ```bash
  pip install --force-reinstall --no-deps torch torchvision --index-url https://download.pytorch.org/whl/cpu
  ```
- **Unhealthy container**: `/api/health` returns `503` when the model or database is unreachable, and the JSON names which one. `docker logs` shows the startup error.
- **Two servers on port 8000**: gunicorn binds with `SO_REUSEPORT`, so a second copy does not fail — both serve, splitting requests across two model instances and two rate-limit budgets. Kill the stray process.