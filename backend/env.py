"""Load ``.env`` into the process environment.

Deliberately tiny and dependency-tolerant. It exists because every entry point
needs the same two steps -- find the project root, load the file -- and getting
that wrong is how a correctly configured ``.env`` ends up silently ignored.

The bug this fixed: ``python-dotenv`` was in requirements.txt and the README
told you to copy ``.env.example`` to ``.env``, but ``load_dotenv()`` was never
called from anywhere. Every setting in the file did nothing.

Rules this follows:

* **Resolve the path from ``__file__``, not the working directory.** The server
  is started from the project root, from inside ``backend/``, and from a
  gunicorn worker that changes directory. ``.env`` has to be found in all three.
* **Never override a real environment variable.** ``override=False`` is the
  default and is what you want: `PGHOST=... python backend/server.py` must beat
  the file, otherwise you cannot override anything for one run.
* **Work without python-dotenv.** A missing optional dependency should degrade
  to "no .env", not crash the app.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("snapin.env")

#: Project root, i.e. the directory containing ``backend/`` and ``static/``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Path searched when the app is run from a packaged or unusual location.
FALLBACK_PATHS = (
    PROJECT_ROOT / ".env",
    Path.cwd() / ".env",
)


def load_env(path: str | os.PathLike[str] | None = None) -> bool:
    """Load configuration into ``os.environ``.

    Returns True if a file was found and read. Calling this more than once is
    harmless: the first call wins, later files cannot clobber it.
    """
    candidates = [Path(path)] if path else list(FALLBACK_PATHS)

    for candidate in candidates:
        if not candidate.is_file():
            continue
        return _load_with_dotenv(candidate)

    log.debug(
        "No .env found (looked in: %s); relying on the environment",
        ", ".join(str(c) for c in candidates),
    )
    return False


def _load_with_dotenv(path: Path) -> bool:
    try:
        from dotenv import load_dotenv
    except ImportError:
        # No python-dotenv: parse it ourselves rather than silently ignoring a
        # file the user wrote on purpose.
        log.warning(
            "python-dotenv is not installed; parsing %s without it. "
            "Install it with: pip install python-dotenv",
            path,
        )
        return _load_manually(path)

    load_dotenv(path, override=False)
    log.info("Loaded configuration from %s", path)
    return True


def _load_manually(path: Path) -> bool:
    """Minimal .env parser for the common `KEY=value` form.

    Handles quotes, `export ` prefixes, and `#` comments. Good enough to keep
    the app configurable when the optional dependency is missing; not a
    replacement for python-dotenv.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("Could not read %s: %s", path, exc)
        return False

    loaded = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # Strip matching quotes, then any trailing inline comment.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()

        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1

    log.info("Loaded %d setting(s) from %s", loaded, path)
    return True