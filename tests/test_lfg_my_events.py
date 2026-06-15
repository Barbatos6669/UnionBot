"""Tests for the user-facing /lfg my-events DB helper."""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
from typing import Iterator

import pytest

from sql_database import Database


@pytest.fixture()
def db() -> Iterator[Database]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(path)
    d.connect()
    d.initialize_all_tables()
    yield d
    try:
        d.close()
    except Exception:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _make_event(db, starts: _dt.datetime, *, title="Evt") -> int:
    return db.create_lfg_event(
        slot_label="GENERAL",
        is_prime=False,
        title=title,
        description="",
        comp_notes="",
        starts_at=starts.isoformat(),
        ends_at=(starts + _dt.timedelta(hours=1)).isoformat(),
        prep_minutes=30,
        review_minutes=15,
        creator_id="111",
    )


def test_returns_only_users_upcoming_signups(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    e1 = _make_event(db, now + _dt.timedelta(hours=1), title="future")
    e2 = _make_event(db, now + _dt.timedelta(hours=2), title="not signed up")
    db.add_lfg_signup(e1, "42")
    db.add_lfg_signup(e2, "99")  # someone else
    rows = db.fetch_user_upcoming_lfg_events("42", now.isoformat(), limit=10)
    assert len(rows) == 1
    assert rows[0]["id"] == e1
    assert rows[0]["title"] == "future"


def test_excludes_past_events(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    past = _make_event(db, now - _dt.timedelta(hours=1), title="done")
    future = _make_event(db, now + _dt.timedelta(hours=1), title="next")
    db.add_lfg_signup(past, "42")
    db.add_lfg_signup(future, "42")
    rows = db.fetch_user_upcoming_lfg_events("42", now.isoformat(), limit=10)
    assert [r["id"] for r in rows] == [future]


def test_excludes_cancelled_events(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    ev = _make_event(db, now + _dt.timedelta(hours=1))
    db.add_lfg_signup(ev, "42")
    db.cancel_lfg_event(ev)
    rows = db.fetch_user_upcoming_lfg_events("42", now.isoformat(), limit=10)
    assert rows == []


def test_ordered_by_starts_at_ascending(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    later = _make_event(db, now + _dt.timedelta(hours=5), title="later")
    soon = _make_event(db, now + _dt.timedelta(hours=1), title="soon")
    middle = _make_event(db, now + _dt.timedelta(hours=3), title="middle")
    for ev in (later, soon, middle):
        db.add_lfg_signup(ev, "42")
    rows = db.fetch_user_upcoming_lfg_events("42", now.isoformat(), limit=10)
    assert [r["id"] for r in rows] == [soon, middle, later]


def test_limit_is_respected(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    for i in range(5):
        ev = _make_event(db, now + _dt.timedelta(hours=i + 1))
        db.add_lfg_signup(ev, "42")
    rows = db.fetch_user_upcoming_lfg_events("42", now.isoformat(), limit=2)
    assert len(rows) == 2
