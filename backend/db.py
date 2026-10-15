"""Database access for SnapIN.

PostgreSQL via psycopg 3. Everything SQL lives here, so the rest of the code
never builds queries by hand.

Design notes
------------
*One pool, small.* The server runs a single process with a handful of clients,
so the pool is deliberately modest. Opening a connection per request is what
makes a Postgres app slow, so connections are checked out of the pool instead.

*Typed columns, not strings.* Timestamps are ``timestamptz`` and durations are
``interval``, so Postgres does the date arithmetic in
:mod:`backend.stats` instead of Python.

*Money-free, but explicit about duration.* Focus time is stored as an
``interval`` because that is what it is. Summing intervals is exact and needs
no unit juggling at the query layer.

*Parameterized everywhere.* No query is ever built by concatenating a value.
``search_path`` is pinned per connection so a stray ``users`` reference cannot
silently bind to a system table.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("snapin.db")

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()

# Fail fast if Postgres is unreachable rather than hanging a request.
_CONNECT_TIMEOUT = int(os.environ.get("SNAPIN_DB_CONNECT_TIMEOUT", "5"))

SCHEMA = """
-- Accounts. A row is either a password account, a Google account, or both.
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      TEXT        NOT NULL UNIQUE,
    -- NULL for accounts that can only sign in through Google.
    password_hash TEXT,
    google_sub    TEXT        UNIQUE,
    email         TEXT,
    display_name  TEXT,
    picture       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- A password account must have a hash; a Google account must have a sub.
    CONSTRAINT users_need_a_credential CHECK (
        password_hash IS NOT NULL OR google_sub IS NOT NULL
    )
);

-- Server-side sessions, replacing the stateless signed cookie. Storing the
-- token means a session can be revoked, and every session can be listed and
-- signed out at once -- neither was possible with a self-validating cookie.
CREATE TABLE IF NOT EXISTS sessions (
    -- The token itself is stored, not a hash of it. If this database is read
    -- by someone who already has the file, they have the app's secrets
    -- anyway, so hashing buys nothing while making lookups more expensive.
    token       TEXT        PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);

-- A period of studying. Opened when the guard starts, closed when it stops.
CREATE TABLE IF NOT EXISTS focus_sessions (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at         TIMESTAMPTZ,
    frames_analyzed  INTEGER     NOT NULL DEFAULT 0,
    phone_detections INTEGER     NOT NULL DEFAULT 0,
    -- Present once the session has been closed and counted.
    duration_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS focus_sessions_user_started_idx
    ON focus_sessions(user_id, started_at DESC);

-- One row per *distraction episode*: the span between the first frame that
-- saw a phone and the frame that stopped seeing it.
--
-- The obvious design -- a row per alerting frame -- is wrong. At 5 FPS, one
-- phone pickup becomes forty near-identical rows, which makes the table huge
-- and the counts meaningless. An episode is the unit a person actually cares
-- about: "I picked my phone up at 14:32 and put it down at 14:35".
CREATE TABLE IF NOT EXISTS distractions (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    focus_session_id BIGINT      REFERENCES focus_sessions(id) ON DELETE SET NULL,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL while the phone is still visible; set when the episode ends.
    ended_at         TIMESTAMPTZ,
    -- Highest confidence seen during the episode: how sure we were at best.
    peak_confidence  REAL        NOT NULL,
    -- How many frames the phone stayed visible for, a rough proxy for how
    -- long the distraction actually lasted.
    frames           INTEGER     NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS distractions_user_time_idx
    ON distractions(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS distractions_session_idx
    ON distractions(focus_session_id);

-- At most one open episode per focus session. Enforced in the schema rather
-- than only in code, so a concurrent frame cannot open a second one.
CREATE UNIQUE INDEX IF NOT EXISTS distractions_one_open_per_session
    ON distractions(focus_session_id)
    WHERE ended_at IS NULL AND focus_session_id IS NOT NULL;
"""


def dsn() -> str:
    """Connection string, from DATABASE_URL or discrete parts.

    ``DATABASE_URL`` wins because that is what PaaS providers hand you; the
    discrete variables exist so a local dev setup needs no URL escaping.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url

    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("PGUSER", "snapin")
    password = os.environ.get("PGPASSWORD", "")
    database = os.environ.get("PGDATABASE", "snapin")

    parts = [f"host={host}", f"port={port}", f"user={user}", f"dbname={database}"]
    if password:
        parts.append(f"password={password}")
    return " ".join(parts)


def init_pool(min_size: int | None = None, max_size: int | None = None) -> ConnectionPool:
    """Create the connection pool. Safe to call more than once."""
    global _pool

    with _pool_lock:
        if _pool is not None:
            return _pool

        _pool = ConnectionPool(
            conninfo=dsn(),
            min_size=min_size if min_size is not None else int(os.environ.get("SNAPIN_DB_POOL_MIN", "1")),
            max_size=max_size if max_size is not None else int(os.environ.get("SNAPIN_DB_POOL_MAX", "8")),
            timeout=_CONNECT_TIMEOUT,
            kwargs={"row_factory": dict_row, "options": "-c search_path=public"},
            open=True,
        )
        _pool.wait(timeout=_CONNECT_TIMEOUT)
        log.info("Connected to PostgreSQL")
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Check a connection out of the pool for the duration of the block."""
    pool = init_pool()
    with pool.connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """Check out a connection and hand back a dict-row cursor.

    Commits on clean exit, rolls back on error. Every write path in the app
    goes through this, so transaction handling lives in exactly one place.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            yield cur


def migrate() -> None:
    """Create tables and indexes if they do not exist.

    Written to be re-runnable so it can be called on every startup. This is a
    deliberately small schema, so an ``IF NOT EXISTS`` script is enough; move
    to a migration tool if the schema starts changing shape.
    """
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    log.info("Schema ready")


def available() -> tuple[bool, str | None]:
    """Probe the database. Returns (ok, error message)."""
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
        return True, None
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return False, str(exc).strip().splitlines()[0]


def rows_to_dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise driver rows into JSON-safe values."""
    out = []
    for row in rows:
        item = {}
        for key, value in row.items():
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
            else:
                item[key] = value
        out.append(item)
    return out