"""Queries for SnapIN.

One function per thing the app does. No SQL lives outside this file and
:mod:`backend.stats`, and every value is bound as a parameter -- nothing is
concatenated into a query string.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

import db

log = logging.getLogger("snapin.repo")

DEFAULT_SESSION_TTL = timedelta(hours=12)


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------

USER_COLUMNS = """
    id, username, password_hash, google_sub, email,
    display_name, picture, created_at
"""


def create_password_user(username: str, password_hash: dict[str, Any],
                         email: str | None = None) -> dict[str, Any]:
    """Create or replace a password account.

    Re-adding an existing username updates the hash, which is how a password
    is changed. Web sign-up uses :func:`register_password_user` instead, which
    refuses to overwrite.
    """
    encoded = encode_password_hash(password_hash)

    with db.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO users (username, password_hash, email)
            VALUES (%s, %s, %s)
            ON CONFLICT (username) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    -- COALESCE keeps an existing address when re-adding a user
                    -- without one.
                    email = COALESCE(EXCLUDED.email, users.email)
            RETURNING {USER_COLUMNS}
            """,
            (username, encoded, email),
        )
        row = cur.fetchone()

    log.info("Stored user %r", username)
    return row


def register_password_user(username: str, password_hash: dict[str, Any],
                           email: str | None = None) -> dict[str, Any]:
    """Create a password account, refusing to touch an existing one.

    Insert-only: web sign-up must never be able to silently overwrite an
    account, which is what ``ON CONFLICT DO UPDATE`` would do. The unique
    index on ``username`` is the real guard -- this function just turns the
    resulting error into ``None`` so the caller can report it properly.
    """
    encoded = encode_password_hash(password_hash)

    try:
        with db.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO users (username, password_hash, email)
                VALUES (%s, %s, %s)
                RETURNING {USER_COLUMNS}
                """,
                (username, encoded, email),
            )
            row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        log.info("Registration refused: username %r already exists", username)
        return None

    log.info("Registered user %r", username)
    return row


def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Find a password account by email, for Google account linking.

    Only ever called with an address Google has confirmed, so this is an
    ownership proof rather than a lookup anybody can influence.
    """
    if not email:
        return None

    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT {USER_COLUMNS}
            FROM users
            WHERE lower(email) = lower(%s) AND google_sub IS NULL
            """,
            (email,),
        )
        return cur.fetchone()


def link_google_account(user_id: int, sub: str, email: str | None,
                        name: str | None, picture: str | None) -> dict[str, Any]:
    """Attach a Google identity to an existing password account.

    Requires the password account's own password to have been presented
    first (see ``server.api_link_google``). Without that check, anyone able to
    sign up with a given email could claim an account belonging to whoever
    really owns it.
    """
    with db.cursor() as cur:
        cur.execute(
            f"""
            UPDATE users
               SET google_sub    = %s,
                   email         = COALESCE(%s, email),
                   display_name  = COALESCE(%s, display_name),
                   picture       = COALESCE(%s, picture)
             WHERE id = %s
            RETURNING {USER_COLUMNS}
            """,
            (sub, email, name, picture, user_id),
        )
        row = cur.fetchone()

    log.info("Linked Google identity to existing account id=%s", user_id)
    return row


def upsert_google_user(sub: str, email: str, name: str, picture: str | None) -> dict[str, Any]:
    """Link a Google account, keyed on Google's stable user id.

    The username is ``google:<sub>`` so two people with the same email address
    cannot collide, and re-signing-in updates the existing row instead of
    creating a duplicate.
    """
    username = f"google:{sub}"

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (username, google_sub, email, display_name, picture)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (google_sub) DO UPDATE
                SET email         = EXCLUDED.email,
                    display_name  = EXCLUDED.display_name,
                    picture       = EXCLUDED.picture
            RETURNING id, username, password_hash, google_sub, email,
                      display_name, picture, created_at
            """,
            (username, sub, email, name, picture),
        )
        row = cur.fetchone()

    log.info("Linked Google account %s", email)
    return row


def get_user(username: str) -> dict[str, Any] | None:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, username, password_hash, google_sub, email,
                   display_name, picture, created_at
            FROM users WHERE username = %s
            """,
            (username,),
        )
        return cur.fetchone()


def get_user_by_google_sub(sub: str) -> dict[str, Any] | None:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, username, password_hash, google_sub, email,
                   display_name, picture, created_at
            FROM users WHERE google_sub = %s
            """,
            (sub,),
        )
        return cur.fetchone()


def list_users() -> list[dict[str, Any]]:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, username, email, display_name, google_sub, created_at
            FROM users ORDER BY created_at
            """
        )
        return cur.fetchall()


def delete_user(username: str) -> bool:
    with db.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username = %s", (username,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Password hashes
# --------------------------------------------------------------------------
# The hash is stored as one JSON document rather than a wider table. It is
# self-describing, so the algorithm and round count travel with the value and
# an old hash stays verifiable after the defaults change.
#
# Stored form: {"algo":"pbkdf2_sha256","rounds":240000,"salt":"...","hash":"..."}
# Encoded as:  pbkdf2_sha256$240000$<salt>$<hash>

def encode_password_hash(record: dict[str, Any]) -> str:
    return f"{record['algo']}${record['rounds']}${record['salt']}${record['hash']}"


def decode_password_hash(encoded: str) -> dict[str, Any]:
    algo, rounds, salt, digest = encoded.split("$", 3)
    return {
        "algo": algo,
        "rounds": int(rounds),
        "salt": salt,
        "hash": digest,
    }


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def create_session(user_id: int, ttl: timedelta = DEFAULT_SESSION_TTL,
                  user_agent: str | None = None) -> str:
    """Issue a session token. Returns the raw token; only this function sees it."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + ttl

    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (token, user_id, expires_at, user_agent)
            VALUES (%s, %s, %s, %s)
            """,
            (token, user_id, expires, user_agent),
        )
    return token


def get_session_user(token: str) -> dict[str, Any] | None:
    """Resolve a token to its user, or None if missing or expired.

    Expired rows are deleted on the way past, so the sessions table does not
    grow without bound and no separate cleanup job is needed.
    """
    if not token:
        return None

    with db.cursor() as cur:
        cur.execute(
            """
            DELETE FROM sessions
            WHERE token = %s AND expires_at <= now()
            """,
            (token,),
        )
        cur.execute(
            """
            SELECT u.id, u.username, u.password_hash, u.google_sub, u.email,
                   u.display_name, u.picture, u.created_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = %s AND s.expires_at > now()
            """,
            (token,),
        )
        return cur.fetchone()


def delete_session(token: str) -> None:
    with db.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


def delete_all_sessions(user_id: int) -> int:
    """Sign out everywhere. Possible now because sessions are server-side."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        return cur.rowcount


def count_sessions(user_id: int) -> int:
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM sessions WHERE user_id = %s AND expires_at > now()",
            (user_id,),
        )
        return cur.fetchone()["n"]


def purge_expired_sessions() -> int:
    with db.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at <= now()")
        return cur.rowcount


def identity_from_row(row: dict[str, Any] | None):
    """Build an :class:`auth.Identity` from a user row."""
    from auth import Identity

    if row is None:
        return None

    is_google = bool(row.get("google_sub"))
    return Identity(
        username=row["username"],
        display_name=row.get("display_name") or (row["username"] if not is_google else None),
        email=row.get("email"),
        picture=row.get("picture"),
        provider="google" if is_google else "password",
        # An account can have both. Reporting only one would hide the fact
        # that a password still works after Google is linked.
        can_use_password=bool(row.get("password_hash")),
    )