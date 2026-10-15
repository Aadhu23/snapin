# SnapIN container image
#
# Four things this file is deliberate about:
#
#  1. CPU-only PyTorch. The default PyPI wheel bundles roughly 2.5GB of CUDA
#     libraries that a headless container never touches. Installing from the
#     PyTorch CPU index keeps the image near 1GB instead of 3.5GB.
#  2. One OpenCV. Ultralytics depends on `opencv-python`, while the
#     requirements pin `opencv-python-headless`. They ship the same `cv2`
#     files, so the headless build is reinstalled last to win deterministically.
#  3. The database is external. This image contains no database; point
#     PGHOST or DATABASE_URL at one.
#  4. A health check that can fail. /api/health answers 503 when the model or
#     the database is missing, so an orchestrator can act on the status code.

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# PYTHONUNBUFFERED  so logs stream instead of sitting in a buffer
# PYTHONDONTWRITEBYTECODE  keep .pyc files out of the image
# PIP_NO_CACHE_DIR  no wheels cached in layers
# YOLO_CONFIG_DIR / MPLCONFIGDIR  writable dirs for the non-root user, which
#   ultralytics and matplotlib both try to write to at import time
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YOLO_CONFIG_DIR=/app/.ultralytics \
    MPLCONFIGDIR=/app/.matplotlib

# libgl1 / libglib2.0-0 are OpenCV runtime needs. On bookworm+ the GL library
# is `libgl1`; the old name `libgl1-mesa-glx` no longer resolves.
#
# libgl1 is kept even though the headless OpenCV build does not link against
# it, because `opencv-python` is still installed as an ultralytics dependency
# and is what a stray import would reach for. It costs a few megabytes and
# removes a class of runtime surprises.
#
# curl is intentionally absent: the health check uses Python, which is here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
# Requirements are copied alone so this layer survives code-only changes.

# PyTorch before requirements.txt: ultralytics asks for torch>=1.8, which the
# CPU build already satisfies, so pip leaves it alone. Installing in the other
# order would resolve torch from PyPI and pull the CUDA build.
RUN pip install --no-cache-dir \
        torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .

# Two OpenCV builds have to be reconciled here.
#
# Ultralytics depends on `opencv-python`, and this project also pins
# `opencv-python-headless`. The two distributions ship the *same* `cv2`
# payload -- 51 overlapping files -- so whichever installs last wins, which
# is not something to leave to chance.
#
# The fix reinstalls headless last, so its files are written last. It is
# deliberately NOT `pip uninstall opencv-python`: uninstall removes every
# path in that distribution's RECORD, including the ones headless also
# claims, which deletes cv2 outright and leaves the image unable to import
# it. Measured, not assumed.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless \
    # Fail the build rather than discover this at runtime.
    && python -c "import cv2, numpy, torch, ultralytics, psycopg, fastapi; \
        print('imports OK:', cv2.__version__, torch.__version__, ultralytics.__version__)"

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
COPY backend/ ./backend/
COPY static/ ./static/

# The weights file is gitignored, so fetch it now and bake it in. That also
# means the container needs no network access at startup, which matters for
# air-gapped or offline deployments.
ARG YOLO_MODEL=yolov8s.pt
RUN python -c "from ultralytics import YOLO; YOLO('${YOLO_MODEL}')" \
    && test -f "${YOLO_MODEL}" \
    || (echo "Failed to fetch ${YOLO_MODEL}" && exit 1)

# ---------------------------------------------------------------------------
# Runtime user
# ---------------------------------------------------------------------------
# Nothing here needs root. The uid is fixed so a bind-mounted volume has a
# predictable owner.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin snapin \
    # Own the whole tree: the code, the weights, and the config dirs that
    # ultralytics writes to on first import.
    && mkdir -p "${YOLO_CONFIG_DIR}" "${MPLCONFIGDIR}" \
    && chown -R snapin:snapin /app

USER snapin

EXPOSE 8000

# The database lives elsewhere. Defaults suit a `postgres` service on a Docker
# network; override PGHOST or set DATABASE_URL.
ENV PGHOST=postgres \
    PGPORT=5432 \
    PGUSER=snapin \
    PGDATABASE=snapin

# /api/health answers 200 only when both the model and the database are up, so
# this check is meaningful rather than always passing. The 90s start period
# covers first-run model load and the initial database connect.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import json,urllib.request,sys; \
        r=urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=8); \
        sys.exit(0 if json.load(r).get('status')=='ok' else 1)"

# WebSockets need an ASGI worker class; gunicorn's default sync workers cannot
# serve them and every handshake fails silently.
#
# --chdir backend   lets `server:app` resolve. server.py is not an importable
#                   package member; it locates its own siblings at runtime, so
#                   the app object is found by name rather than by dotted path.
# --workers 1       the detector is a single shared model instance behind one
#                   lock, and the rate limiter is per-process. More workers
#                   would mean more copies of both, not more throughput.
CMD ["gunicorn", "--chdir", "/app/backend", "server:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--timeout", "120", \
     "--graceful-timeout", "30"]