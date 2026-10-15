# Official lightweight Python image
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Keep weights inside the image layer so they survive restarts.
    YOLO_CONFIG_DIR=/app/.ultralytics

WORKDIR /app

# System dependencies for OpenCV. On bookworm+ the GL library is `libgl1`;
# `libgl1-mesa-glx` was renamed and no longer resolves.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so they stay cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY static/ ./static/

# yolov8s.pt is gitignored, so bake the weights in at build time instead of
# downloading them on every container start (which also breaks offline runs).
ARG YOLO_MODEL=yolov8s.pt
RUN python -c "from ultralytics import YOLO; YOLO('${YOLO_MODEL}')"

# Drop privileges: nothing here needs root.
RUN useradd --create-home --uid 10001 snapin \
    && chown -R snapin:snapin /app
USER snapin

EXPOSE 8000

# The app needs PostgreSQL. Link a running server with --network, or point
# DATABASE_URL at a hosted instance.
ENV PGHOST=postgres \
    PGPORT=5432 \
    PGDATABASE=snapin

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# WebSockets need an ASGI worker class; plain sync gunicorn workers cannot
# serve them and will silently fail every handshake.
# WORKDIR stays /app so the `static/` lookup from the parent directory works.
# --chdir backend lets `server:app` resolve, since server.py is not a package
# member and imports its siblings by name.
CMD ["gunicorn", "--chdir", "/app/backend", "server:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--timeout", "120"]