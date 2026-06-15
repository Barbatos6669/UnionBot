from __future__ import annotations

import sqlite3

from cogs._bounties_config import STATUS_CLAIMED, STATUS_OPEN
from cogs._bounties_db import db_claim_open, db_get


class _Db:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.cursor = self.connection.cursor()
        self.cursor.execute(
            """
            CREATE TABLE bounties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                reward_points INTEGER NOT NULL DEFAULT 0,
                posted_by TEXT NOT NULL,
                posted_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                deadline TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                claimed_by TEXT,
                claimed_at TEXT,
                submitted_at TEXT,
                proof TEXT,
                completed_by TEXT,
                completed_at TEXT,
                paid_by TEXT,
                paid_at TEXT,
                channel_id TEXT,
                message_id TEXT
            )
            """
        )
        self.connection.commit()

    def connect(self) -> None:
        pass


def _insert_open_bounty(db: _Db) -> int:
    db.cursor.execute(
        """
        INSERT INTO bounties (title, description, reward_points, posted_by, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("BZ Group Dungeon Mission", "Clear 2 BZ group dungeons.", 1_000_000, "officer", STATUS_OPEN),
    )
    db.connection.commit()
    return int(db.cursor.lastrowid)


def test_db_claim_open_only_allows_first_claimant() -> None:
    db = _Db()
    bounty_id = _insert_open_bounty(db)

    assert db_claim_open(db, bounty_id, "111", claimed_at="2026-06-15 12:00:00")
    assert not db_claim_open(db, bounty_id, "222", claimed_at="2026-06-15 12:00:01")

    bounty = db_get(db, bounty_id)
    assert bounty is not None
    assert bounty["status"] == STATUS_CLAIMED
    assert bounty["claimed_by"] == "111"
    assert bounty["claimed_at"] == "2026-06-15 12:00:00"


def test_db_claim_open_rejects_non_open_bounty() -> None:
    db = _Db()
    bounty_id = _insert_open_bounty(db)
    db.cursor.execute(
        "UPDATE bounties SET status = ?, claimed_by = ? WHERE id = ?",
        (STATUS_CLAIMED, "333", bounty_id),
    )
    db.connection.commit()

    assert not db_claim_open(db, bounty_id, "444")
    bounty = db_get(db, bounty_id)
    assert bounty is not None
    assert bounty["claimed_by"] == "333"
