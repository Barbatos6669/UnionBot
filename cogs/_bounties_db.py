"""Schema, DB helpers, and flex/milestone queries for the bounties cog."""

from __future__ import annotations

from cogs._bounties_config import (
    ACTIVE_STATUSES,
    BOUNTY_MILESTONES,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_OPEN,
    STATUS_PENDING,
    STATUS_SUBMITTED,
    now_iso,
)


# ── flex / shoutout schema ──────────────────────────────────────────────────
def ensure_flex_schema(db) -> None:
    """Track which (user, threshold) milestones already got a shoutout."""
    db.cursor.execute("""
        CREATE TABLE IF NOT EXISTS bounty_milestones (
            user_id    TEXT NOT NULL,
            threshold  INTEGER NOT NULL,
            reached_at TEXT NOT NULL,
            PRIMARY KEY (user_id, threshold)
        )
    """)
    db.connection.commit()


# ── earner queries ──────────────────────────────────────────────────────────
def player_total_earned(db, user_id: str) -> int:
    """Lifetime silver earned by ``user_id`` across all completed bounties."""
    db.cursor.execute(
        "SELECT COALESCE(SUM(reward_points), 0) AS total "
        "FROM bounties WHERE claimed_by = ? AND status = ?",
        (str(user_id), STATUS_COMPLETED),
    )
    row = db.cursor.fetchone()
    if not row:
        return 0
    try:
        return int(row["total"] or 0)
    except (TypeError, KeyError, ValueError):
        try:
            return int(row[0] or 0)
        except (TypeError, ValueError):
            return 0


def player_bounty_count(db, user_id: str) -> int:
    db.cursor.execute(
        "SELECT COUNT(*) AS n FROM bounties WHERE claimed_by = ? AND status = ?",
        (str(user_id), STATUS_COMPLETED),
    )
    row = db.cursor.fetchone()
    try:
        return int(row["n"] or 0) if row else 0
    except (TypeError, KeyError, ValueError):
        return 0


def top_earners(db, since_iso: str | None, limit: int = 10) -> list[dict]:
    """Top bounty earners. ``since_iso`` filters by ``completed_at >= since``."""
    base = (
        "SELECT claimed_by AS user_id, "
        "SUM(reward_points) AS total_silver, "
        "COUNT(*) AS bounty_count "
        "FROM bounties WHERE status = ? AND claimed_by IS NOT NULL"
    )
    args: list = [STATUS_COMPLETED]
    if since_iso:
        base += " AND completed_at >= ?"
        args.append(since_iso)
    base += " GROUP BY claimed_by ORDER BY total_silver DESC LIMIT ?"
    args.append(int(limit))
    db.cursor.execute(base, args)
    return [dict(r) for r in db.cursor.fetchall()]


def player_rank(db, user_id: str) -> tuple[int, int]:
    """Return (rank, total_players) for ``user_id`` on the all-time board."""
    db.cursor.execute(
        "SELECT claimed_by AS uid, SUM(reward_points) AS total "
        "FROM bounties WHERE status = ? AND claimed_by IS NOT NULL "
        "GROUP BY claimed_by ORDER BY total DESC",
        (STATUS_COMPLETED,),
    )
    rows = db.cursor.fetchall()
    total = len(rows)
    for i, row in enumerate(rows, 1):
        try:
            uid = row["uid"]
        except (TypeError, KeyError):
            uid = row[0]
        if str(uid) == str(user_id):
            return i, total
    return 0, total


def new_milestone(db, user_id: str, lifetime_total: int) -> int | None:
    """If crossing a milestone, return its threshold and mark it claimed."""
    for tier in BOUNTY_MILESTONES:
        if lifetime_total < tier:
            return None
        db.cursor.execute(
            "SELECT 1 FROM bounty_milestones WHERE user_id = ? AND threshold = ?",
            (str(user_id), int(tier)),
        )
        if db.cursor.fetchone():
            continue
        # The highest unclaimed tier we've crossed is the shoutout-worthy one.
        highest: int | None = None
        for t in BOUNTY_MILESTONES:
            if lifetime_total >= t:
                db.cursor.execute(
                    "SELECT 1 FROM bounty_milestones WHERE user_id = ? AND threshold = ?",
                    (str(user_id), int(t)),
                )
                if not db.cursor.fetchone():
                    highest = t
        if highest is None:
            return None
        db.cursor.execute(
            "INSERT OR IGNORE INTO bounty_milestones (user_id, threshold, reached_at) "
            "VALUES (?, ?, ?)",
            (str(user_id), int(highest), now_iso()),
        )
        db.connection.commit()
        return highest
    return None


# ── bounty CRUD ─────────────────────────────────────────────────────────────
def db_create(db, *, title: str, description: str, reward: int,
              posted_by: str, deadline: str | None) -> int:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        '''INSERT INTO bounties
           (title, description, reward_points, posted_by, deadline, status)
           VALUES (?, ?, ?, ?, ?, ?)''',
        (title, description, reward, posted_by, deadline, STATUS_PENDING),
    )
    db.connection.commit()
    return int(db.cursor.lastrowid or 0)


def db_get(db, bounty_id: int) -> dict | None:
    if not db.connection:
        db.connect()
    db.cursor.execute('SELECT * FROM bounties WHERE id = ?', (bounty_id,))
    row = db.cursor.fetchone()
    return dict(row) if row else None


def db_list(db, *, statuses: tuple[str, ...] = ACTIVE_STATUSES,
            limit: int = 25) -> list[dict]:
    if not db.connection:
        db.connect()
    placeholders = ",".join("?" * len(statuses))
    db.cursor.execute(
        f'''SELECT * FROM bounties
            WHERE status IN ({placeholders})
            ORDER BY posted_at DESC LIMIT ?''',
        (*statuses, limit),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def db_list_for_user(db, discord_id: str) -> list[dict]:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        '''SELECT * FROM bounties
           WHERE claimed_by = ? AND status IN (?, ?)
           ORDER BY posted_at DESC''',
        (discord_id, STATUS_CLAIMED, STATUS_SUBMITTED),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def db_update(db, bounty_id: int, **fields) -> None:
    if not fields:
        return
    if not db.connection:
        db.connect()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [bounty_id]
    db.cursor.execute(f'UPDATE bounties SET {cols} WHERE id = ?', values)
    db.connection.commit()


def db_claim_open(db, bounty_id: int, user_id: str, claimed_at: str | None = None) -> bool:
    """Atomically claim an open bounty.

    The Discord button and slash command can both be clicked at almost the
    same time. Keep the "is it still open?" check inside the UPDATE so only
    one caller can move the row from open -> claimed.
    """
    if not db.connection:
        db.connect()
    db.cursor.execute(
        '''UPDATE bounties
           SET status = ?, claimed_by = ?, claimed_at = ?
           WHERE id = ?
             AND status = ?
             AND (claimed_by IS NULL OR claimed_by = '')''',
        (
            STATUS_CLAIMED,
            str(user_id),
            claimed_at or now_iso(),
            int(bounty_id),
            STATUS_OPEN,
        ),
    )
    db.connection.commit()
    return int(db.cursor.rowcount or 0) == 1


def db_overdue(db) -> list[dict]:
    if not db.connection:
        db.connect()
    now = now_iso()
    db.cursor.execute(
        '''SELECT * FROM bounties
           WHERE deadline IS NOT NULL AND deadline < ?
             AND status IN (?, ?)''',
        (now, STATUS_OPEN, STATUS_CLAIMED),
    )
    return [dict(r) for r in db.cursor.fetchall()]
