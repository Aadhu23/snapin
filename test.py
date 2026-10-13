"""Smoke test for the SnapIN detection WebSocket.

Sends an image to the running server and prints the JSON response.

Usage:
    python test.py                      # uses phone.png in the repo root
    python test.py path/to/frame.jpg
    SNAPIN_USERNAME / SNAPIN_PASSWORD   # required, no defaults

Start the server first:
    python backend/server.py
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import websockets

HOST = os.environ.get("SNAPIN_TEST_HOST", "127.0.0.1")
PORT = os.environ.get("SNAPIN_PORT", "8000")
HTTP_BASE = f"http://{HOST}:{PORT}"
WS_URI = f"ws://{HOST}:{PORT}/ws/detect"

USERNAME = os.environ.get("SNAPIN_USERNAME", "")
PASSWORD = os.environ.get("SNAPIN_PASSWORD", "")


def login() -> str:
    """Authenticate and return the session cookie header value."""
    if not USERNAME or not PASSWORD:
        raise SystemExit(
            "Set SNAPIN_USERNAME and SNAPIN_PASSWORD, for example:\n"
            "  SNAPIN_USERNAME=alice SNAPIN_PASSWORD=... python test.py"
        )

    body = json.dumps({"username": USERNAME, "password": PASSWORD}).encode()
    request = urllib.request.Request(
        f"{HTTP_BASE}/api/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            cookie = response.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Login failed ({exc.code}): {detail}") from None

    if not cookie:
        raise SystemExit("Login succeeded but no session cookie was returned.")
    return cookie.split(";")[0]


async def test_detection(image_path: str) -> None:
    path = Path(image_path)
    if not path.exists():
        raise SystemExit(f"Image not found: {path}")

    cookie = login()
    image_bytes = path.read_bytes()
    print(f"Connected to {WS_URI} as {USERNAME}")
    print(f"Sending {len(image_bytes)} bytes from {path.name}...")

    async with websockets.connect(WS_URI, additional_headers={"Cookie": cookie}) as ws:
        await ws.send(image_bytes)
        response = await ws.recv()
        parsed = json.loads(response)

        print(f"\nResponse: {json.dumps(parsed, indent=2)}")
        if parsed.get("alert"):
            print("\nALERT: a phone was detected.")
        else:
            print("\nNo phone detected.")


if __name__ == "__main__":
    image = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "phone.png")
    asyncio.run(test_detection(image))