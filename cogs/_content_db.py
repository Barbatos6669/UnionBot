"""Schema and DB-access helpers for the content-curator cog."""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from debug import info_log
from cogs._lfg_config import EVENT_TYPES_BY_KEY
from cogs._content_config import now_utc


# ── schema ──────────────────────────────────────────────────────────────────
def ensure_schema(db) -> None:
    cur = db.cursor
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_polls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at   TEXT NOT NULL,
            closes_at   TEXT NOT NULL,
            channel_id  TEXT,
            message_id  TEXT,
            status      TEXT NOT NULL DEFAULT 'open',
            closed_at   TEXT,
            winners     TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_suggestions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id       INTEGER,
            suggester_id  TEXT NOT NULL,
            event_type    TEXT NOT NULL,
            title         TEXT NOT NULL,
            notes         TEXT,
            created_at    TEXT NOT NULL,
            FOREIGN KEY (poll_id) REFERENCES content_polls(id) ON DELETE SET NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_votes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            suggestion_id  INTEGER NOT NULL,
            voter_id       TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            UNIQUE(suggestion_id, voter_id),
            FOREIGN KEY (suggestion_id) REFERENCES content_suggestions(id) ON DELETE CASCADE
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_suggestions_poll "
        "ON content_suggestions(poll_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_votes_suggestion "
        "ON content_votes(suggestion_id)"
    )
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_quickpolls (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at         TEXT NOT NULL,
            closes_at         TEXT NOT NULL,
            channel_id        TEXT,
            message_id        TEXT,
            status            TEXT NOT NULL DEFAULT 'open',
            closed_at         TEXT,
            options           TEXT NOT NULL,
            winner_event_type TEXT,
            lfg_event_id      INTEGER,
            creator_id        TEXT,
            lead_minutes      INTEGER NOT NULL DEFAULT 15,
            duration_minutes  INTEGER NOT NULL DEFAULT 90,
            target_starts_at  TEXT,
            target_ends_at    TEXT,
            target_slot_label TEXT,
            target_is_prime   INTEGER NOT NULL DEFAULT 0
        )
    """)
    for ddl in (
        "ALTER TABLE content_quickpolls ADD COLUMN target_starts_at TEXT",
        "ALTER TABLE content_quickpolls ADD COLUMN target_ends_at TEXT",
        "ALTER TABLE content_quickpolls ADD COLUMN target_slot_label TEXT",
        "ALTER TABLE content_quickpolls ADD COLUMN target_is_prime INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_quickvotes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id      INTEGER NOT NULL,
            voter_id     TEXT NOT NULL,
            event_type   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            UNIQUE(poll_id, voter_id),
            FOREIGN KEY (poll_id) REFERENCES content_quickpolls(id) ON DELETE CASCADE
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_quickvotes_poll "
        "ON content_quickvotes(poll_id)"
    )
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_availability_polls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            opened_at   TEXT NOT NULL,
            closes_at   TEXT NOT NULL,
            channel_id  TEXT,
            message_id  TEXT,
            status      TEXT NOT NULL DEFAULT 'open',
            closed_at   TEXT,
            options     TEXT NOT NULL,
            creator_id  TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_availability_votes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id     INTEGER NOT NULL,
            voter_id    TEXT NOT NULL,
            slot_index  INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE(poll_id, voter_id, slot_index),
            FOREIGN KEY (poll_id) REFERENCES content_availability_polls(id) ON DELETE CASCADE
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_availability_votes_poll "
        "ON content_availability_votes(poll_id)"
    )
    cur.execute("""
        CREATE TABLE IF NOT EXISTS content_daily_timer_funnels (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            target_date          TEXT NOT NULL UNIQUE,
            availability_poll_id INTEGER,
            quickpoll_id         INTEGER,
            lfg_event_id         INTEGER,
            status               TEXT NOT NULL DEFAULT 'availability',
            selected_slot_index  INTEGER,
            selected_slot_label  TEXT,
            selected_starts_at   TEXT,
            selected_ends_at     TEXT,
            selected_headcount   INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT NOT NULL,
            vote_duration_min    INTEGER,
            vote_opened_at       TEXT,
            closed_at            TEXT
        )
    """)
    try:
        cur.execute("ALTER TABLE content_daily_timer_funnels ADD COLUMN vote_duration_min INTEGER")
    except sqlite3.OperationalError:
        pass
    db.connection.commit()
    info_log("Initialized content_curator tables.")


# ── weekly poll ─────────────────────────────────────────────────────────────
def fetch_open_poll(db) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_polls WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def fetch_pending_suggestions(db) -> list[dict]:
    """Suggestions waiting for the next poll (poll_id IS NULL)."""
    db.cursor.execute(
        "SELECT * FROM content_suggestions WHERE poll_id IS NULL ORDER BY created_at"
    )
    return [dict(r) for r in db.cursor.fetchall()]


def fetch_poll_suggestions(db, poll_id: int) -> list[dict]:
    db.cursor.execute(
        """
        SELECT s.*, COALESCE(v.vote_count, 0) AS vote_count
        FROM content_suggestions s
        LEFT JOIN (
            SELECT suggestion_id, COUNT(*) AS vote_count
            FROM content_votes
            GROUP BY suggestion_id
        ) v ON v.suggestion_id = s.id
        WHERE s.poll_id = ?
        ORDER BY vote_count DESC, s.id ASC
        """,
        (poll_id,),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def count_user_suggestions(db, poll_id: Optional[int], suggester_id: str) -> int:
    if poll_id is None:
        db.cursor.execute(
            "SELECT COUNT(*) AS n FROM content_suggestions "
            "WHERE poll_id IS NULL AND suggester_id = ?",
            (suggester_id,),
        )
    else:
        db.cursor.execute(
            "SELECT COUNT(*) AS n FROM content_suggestions "
            "WHERE poll_id = ? AND suggester_id = ?",
            (poll_id, suggester_id),
        )
    row = db.cursor.fetchone()
    return int(row["n"]) if row else 0


def fetch_user_votes(db, poll_id: int, voter_id: str) -> set[int]:
    db.cursor.execute(
        """
        SELECT v.suggestion_id
        FROM content_votes v
        JOIN content_suggestions s ON s.id = v.suggestion_id
        WHERE s.poll_id = ? AND v.voter_id = ?
        """,
        (poll_id, voter_id),
    )
    return {int(r["suggestion_id"]) for r in db.cursor.fetchall()}


def set_user_votes(db, poll_id: int, voter_id: str, suggestion_ids: list[int]) -> None:
    """Replace this voter's selections on the given poll atomically."""
    db.cursor.execute(
        """
        DELETE FROM content_votes
        WHERE voter_id = ?
          AND suggestion_id IN (SELECT id FROM content_suggestions WHERE poll_id = ?)
        """,
        (voter_id, poll_id),
    )
    now = now_utc().isoformat()
    for sid in suggestion_ids:
        try:
            db.cursor.execute(
                "INSERT INTO content_votes (suggestion_id, voter_id, created_at) "
                "VALUES (?, ?, ?)",
                (int(sid), voter_id, now),
            )
        except sqlite3.IntegrityError:
            pass
    db.connection.commit()


# ── quickpoll ───────────────────────────────────────────────────────────────
def fetch_open_quickpoll(db) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_quickpolls WHERE status = 'open' "
        "ORDER BY id DESC LIMIT 1"
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def fetch_quickpoll(db, poll_id: int) -> Optional[dict]:
    db.cursor.execute("SELECT * FROM content_quickpolls WHERE id = ?", (poll_id,))
    row = db.cursor.fetchone()
    return dict(row) if row else None


def quickpoll_option_keys(poll: dict) -> list[str]:
    raw = (poll.get("options") or "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return [k for k in keys if k in EVENT_TYPES_BY_KEY]


def quickpoll_tallies(db, poll_id: int) -> dict[str, int]:
    db.cursor.execute(
        "SELECT event_type, COUNT(*) AS n FROM content_quickvotes "
        "WHERE poll_id = ? GROUP BY event_type",
        (poll_id,),
    )
    return {r["event_type"]: int(r["n"]) for r in db.cursor.fetchall()}


def quickpoll_total_votes(db, poll_id: int) -> int:
    db.cursor.execute(
        "SELECT COUNT(*) AS n FROM content_quickvotes WHERE poll_id = ?",
        (poll_id,),
    )
    row = db.cursor.fetchone()
    return int(row["n"]) if row else 0


def cast_quickvote(db, poll_id: int, voter_id: str, event_type: str) -> None:
    """One vote per voter; re-voting overwrites the previous pick."""
    now = now_utc().isoformat()
    db.cursor.execute(
        "INSERT INTO content_quickvotes (poll_id, voter_id, event_type, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(poll_id, voter_id) DO UPDATE SET "
        "event_type = excluded.event_type, created_at = excluded.created_at",
        (poll_id, voter_id, event_type, now),
    )
    db.connection.commit()


def set_quickpoll_lfg_event(db, poll_id: int, lfg_event_id: int | None) -> None:
    db.cursor.execute(
        "UPDATE content_quickpolls SET lfg_event_id = ? WHERE id = ?",
        (lfg_event_id, int(poll_id)),
    )
    db.connection.commit()


# ── availability poll ──────────────────────────────────────────────────────
def fetch_open_availability_poll(db) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_availability_polls WHERE status = 'open' "
        "ORDER BY id DESC LIMIT 1"
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def fetch_availability_poll(db, poll_id: int) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_availability_polls WHERE id = ?",
        (int(poll_id),),
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def availability_slot_labels(poll: dict) -> list[str]:
    raw = poll.get("options") or "[]"
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        decoded = []
    if isinstance(decoded, list):
        return [str(v).strip()[:90] for v in decoded if str(v).strip()][:25]
    return []


def availability_tallies(db, poll_id: int) -> dict[int, int]:
    db.cursor.execute(
        "SELECT slot_index, COUNT(*) AS n FROM content_availability_votes "
        "WHERE poll_id = ? GROUP BY slot_index",
        (int(poll_id),),
    )
    return {int(r["slot_index"]): int(r["n"]) for r in db.cursor.fetchall()}


def availability_total_voters(db, poll_id: int) -> int:
    db.cursor.execute(
        "SELECT COUNT(DISTINCT voter_id) AS n "
        "FROM content_availability_votes WHERE poll_id = ?",
        (int(poll_id),),
    )
    row = db.cursor.fetchone()
    return int(row["n"] or 0) if row else 0


def set_availability_votes(
    db, poll_id: int, voter_id: str, slot_indexes: list[int],
) -> None:
    """Replace one member's available windows on a poll."""
    labels = availability_slot_labels(fetch_availability_poll(db, poll_id) or {})
    valid = sorted({
        int(i) for i in slot_indexes
        if isinstance(i, int) and 0 <= int(i) < len(labels)
    })
    db.cursor.execute(
        "DELETE FROM content_availability_votes WHERE poll_id = ? AND voter_id = ?",
        (int(poll_id), voter_id),
    )
    now = now_utc().isoformat()
    for slot_index in valid:
        db.cursor.execute(
            "INSERT OR IGNORE INTO content_availability_votes "
            "(poll_id, voter_id, slot_index, created_at) VALUES (?, ?, ?, ?)",
            (int(poll_id), voter_id, slot_index, now),
        )
    db.connection.commit()


# ── daily timer funnel ─────────────────────────────────────────────────────
def fetch_daily_timer_funnel(db, target_date: str) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_daily_timer_funnels WHERE target_date = ?",
        (target_date,),
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def fetch_daily_timer_funnel_by_quickpoll(db, quickpoll_id: int) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_daily_timer_funnels WHERE quickpoll_id = ?",
        (int(quickpoll_id),),
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def fetch_daily_timer_funnel_by_availability_poll(
    db, availability_poll_id: int,
) -> Optional[dict]:
    db.cursor.execute(
        "SELECT * FROM content_daily_timer_funnels WHERE availability_poll_id = ?",
        (int(availability_poll_id),),
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def create_daily_timer_funnel(
    db, *, target_date: str, availability_poll_id: int,
) -> int:
    now = now_utc().isoformat()
    db.cursor.execute(
        "INSERT OR IGNORE INTO content_daily_timer_funnels "
        "(target_date, availability_poll_id, status, created_at) "
        "VALUES (?, ?, 'availability', ?)",
        (target_date, int(availability_poll_id), now),
    )
    db.connection.commit()
    row = fetch_daily_timer_funnel(db, target_date)
    return int(row["id"]) if row else 0


def update_daily_timer_funnel(db, funnel_id: int, fields: dict) -> bool:
    allowed = {
        "availability_poll_id", "quickpoll_id", "lfg_event_id", "status",
        "selected_slot_index", "selected_slot_label", "selected_starts_at",
        "selected_ends_at", "selected_headcount", "vote_opened_at", "closed_at",
        "vote_duration_min",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return False
    cols = ", ".join(f"{k} = ?" for k in clean)
    db.cursor.execute(
        f"UPDATE content_daily_timer_funnels SET {cols} WHERE id = ?",
        [*clean.values(), int(funnel_id)],
    )
    db.connection.commit()
    return db.cursor.rowcount > 0
