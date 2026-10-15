"""Focus-session and distraction tracking, plus the queries Stats reads.

The state machine
-----------------
A focus session is open or closed. While it is open, each frame is either a
phone sighting or not. The first sighting opens a *distraction episode*; every
later sighting extends it; the first clear frame closes it.

That transition rule is the whole design. It is what turns a 5 FPS stream into
"you picked your phone up 14 times" instead of "the model returned true 1,340
times".
"""

from __future__ import annotations

import logging
from typing import Any

import db

log = logging.getLogger("snapin.stats")


# --------------------------------------------------------------------------
# Focus sessions
# --------------------------------------------------------------------------

def start_focus_session(user_id: int) -> int:
    """Open a focus session. Any still-open session for this user is closed
    first, so a crash cannot leave a session running forever."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE focus_sessions
               SET ended_at = now(),
                   duration_seconds = GREATEST(
                       0, EXTRACT(EPOCH FROM (now() - started_at))::int
                   )
             WHERE user_id = %s AND ended_at IS NULL
            """,
            (user_id,),
        )
        cur.execute(
            "INSERT INTO focus_sessions (user_id) VALUES (%s) RETURNING id",
            (user_id,),
        )
        return cur.fetchone()["id"]


def end_focus_session(session_id: int) -> dict[str, Any] | None:
    """Close a session and store its duration.

    The frame counters are left alone: they are already accumulated
    incrementally by :func:`bump_session_counters` as frames arrive, so
    overwriting them here would discard most of the session.

    ``duration_seconds`` is computed by Postgres from ``started_at`` rather
    than trusted from the client, so a wrong browser clock cannot corrupt the
    totals.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE focus_sessions
               SET ended_at = now(),
                   duration_seconds = GREATEST(
                       0, EXTRACT(EPOCH FROM (now() - started_at))::int
                   )
             WHERE id = %s AND ended_at IS NULL
            RETURNING id, started_at, ended_at, duration_seconds,
                      frames_analyzed, phone_detections
            """,
            (session_id,),
        )
        return cur.fetchone()


def get_open_session(user_id: int) -> dict[str, Any] | None:
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, started_at, frames_analyzed, phone_detections
            FROM focus_sessions
            WHERE user_id = %s AND ended_at IS NULL
            ORDER BY started_at DESC LIMIT 1
            """,
            (user_id,),
        )
        return cur.fetchone()


def bump_session_counters(session_id: int, frames: int = 1, detections: int = 0) -> None:
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE focus_sessions
               SET frames_analyzed  = frames_analyzed + %s,
                   phone_detections = phone_detections + %s
             WHERE id = %s
            """,
            (frames, detections, session_id),
        )


# --------------------------------------------------------------------------
# Distraction episodes
# --------------------------------------------------------------------------

def begin_distraction(user_id: int, focus_session_id: int | None,
                      confidence: float, rel_area: float) -> int | None:
    """Open an episode on the first alerting frame.

    The partial unique index ``distractions_one_open_per_session`` means a
    double-open cannot create two episodes, so the ON CONFLICT clause can be a
    no-op rather than an error.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO distractions
                (user_id, focus_session_id, peak_confidence, frames)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (user_id, focus_session_id, confidence),
        )
        row = cur.fetchone()
        return row["id"] if row else None


def extend_distraction(distraction_id: int, confidence: float) -> None:
    """Record another frame on an open episode, keeping the peak confidence."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE distractions
               SET frames = frames + 1,
                   peak_confidence = GREATEST(peak_confidence, %s)
             WHERE id = %s AND ended_at IS NULL
            """,
            (confidence, distraction_id),
        )


def end_distraction(distraction_id: int) -> None:
    """Close an episode on the first clear frame."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE distractions
               SET ended_at = now()
             WHERE id = %s AND ended_at IS NULL
            """,
            (distraction_id,),
        )


def close_stale_distractions() -> int:
    """Close episodes left open by a crash or restart, so durations are real."""
    with db.cursor() as cur:
        cur.execute(
            "UPDATE distractions SET ended_at = now() WHERE ended_at IS NULL"
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

def summary(user_id: int, days: int = 7) -> dict[str, Any]:
    """Headline numbers for the Stats panel."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(fs.duration_seconds), 0)::bigint      AS focus_seconds,
                COUNT(DISTINCT fs.id)::bigint                     AS sessions,
                COALESCE(SUM(fs.phone_detections), 0)::bigint      AS detections
            FROM focus_sessions fs
            WHERE fs.user_id = %s
              AND fs.ended_at IS NOT NULL
              AND fs.started_at >= now() - make_interval(days => %s)
            """,
            (user_id, days),
        )
        totals = cur.fetchone()

        cur.execute(
            """
            SELECT
                COUNT(*)::bigint                                 AS episodes,
                COALESCE(SUM(
                    EXTRACT(EPOCH FROM (COALESCE(ended_at, now()) - started_at))
                ), 0)::bigint                                     AS distracted_seconds,
                COALESCE(MAX(peak_confidence), 0)::real           AS peak_confidence
            FROM distractions d
            WHERE d.user_id = %s
              AND d.started_at >= now() - make_interval(days => %s)
            """,
            (user_id, days),
        )
        distractions = cur.fetchone()

    focus_seconds = int(totals["focus_seconds"])
    distracted_seconds = int(distractions["distracted_seconds"])
    episodes = int(distractions["episodes"])

    return {
        "days": days,
        "focus_seconds": focus_seconds,
        "sessions": int(totals["sessions"]),
        "episodes": episodes,
        "distracted_seconds": distracted_seconds,
        # Share of studied time with the phone visible. The number a person
        # would actually want to bring down.
        "focus_ratio": (
            round(1 - distracted_seconds / focus_seconds, 3) if focus_seconds > 0 else None
        ),
        "detections_per_session": (
            round(episodes / int(totals["sessions"]), 2)
            if int(totals["sessions"]) > 0
            else None
        ),
    }


def daily_breakdown(user_id: int, days: int = 7) -> list[dict[str, Any]]:
    """One row per day, oldest first, including days with no activity.

    The empty days are filled in Python because SQL should not be made to
    generate a calendar for the sake of a chart.
    """
    from datetime import date, timedelta

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                started_at::date                                AS day,
                COALESCE(SUM(duration_seconds), 0)::bigint       AS focus_seconds,
                COUNT(*)::bigint                                AS sessions,
                COALESCE(SUM(phone_detections), 0)::bigint       AS detections
            FROM focus_sessions
            WHERE user_id = %s
              AND ended_at IS NOT NULL
              AND started_at >= now() - make_interval(days => %s)
            GROUP BY started_at::date
            """,
            (user_id, days),
        )
        rows = {r["day"]: r for r in cur.fetchall()}

        cur.execute(
            """
            SELECT
                started_at::date                                AS day,
                COUNT(*)::bigint                                AS episodes,
                COALESCE(SUM(EXTRACT(EPOCH FROM (COALESCE(ended_at, now()) - started_at))), 0)::bigint
                                                        AS distracted_seconds
            FROM distractions
            WHERE user_id = %s
              AND started_at >= now() - make_interval(days => %s)
            GROUP BY started_at::date
            """,
            (user_id, days),
        )
        distract = {r["day"]: r for r in cur.fetchall()}

    today = date.today()
    out = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        focus = rows.get(day)
        dist = distract.get(day)
        out.append({
            "date": day.isoformat(),
            "focus_seconds": int(focus["focus_seconds"]) if focus else 0,
            "sessions": int(focus["sessions"]) if focus else 0,
            "episodes": int(dist["episodes"]) if dist else 0,
            "distracted_seconds": int(dist["distracted_seconds"]) if dist else 0,
        })
    return out


def hourly_pattern(user_id: int, days: int = 30) -> list[dict[str, Any]]:
    """Episodes by hour of day, to show when you actually get distracted."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                EXTRACT(HOUR FROM started_at)::int              AS hour,
                COUNT(*)::bigint                                AS episodes
            FROM distractions
            WHERE user_id = %s
              AND started_at >= now() - make_interval(days => %s)
            GROUP BY EXTRACT(HOUR FROM started_at)
            ORDER BY hour
            """,
            (user_id, days),
        )
        found = {r["hour"]: int(r["episodes"]) for r in cur.fetchall()}

    return [{"hour": h, "episodes": found.get(h, 0)} for h in range(24)]


def recent_distractions(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """The most recent episodes, newest first."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT started_at, ended_at, peak_confidence, frames,
                   GREATEST(0, EXTRACT(EPOCH FROM (COALESCE(ended_at, now()) - started_at)))::int
                       AS seconds
            FROM distractions
            WHERE user_id = %s
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return db.rows_to_dicts(cur.fetchall())