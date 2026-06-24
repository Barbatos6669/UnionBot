"""Tests for the per-user LFG attendance aggregation helper."""
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


def _make(db, starts: _dt.datetime, title: str = "Evt") -> int:
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


def test_zero_for_user_with_no_signups(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    stats = db.fetch_user_lfg_attendance(
        "42", (now - _dt.timedelta(days=30)).isoformat(),
    )
    assert stats == {"signups": 0, "attended": 0, "not_marked_attended": 0}


def test_counts_mixed_outcomes(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    e1 = _make(db, now - _dt.timedelta(days=1))
    e2 = _make(db, now - _dt.timedelta(days=2))
    e3 = _make(db, now - _dt.timedelta(days=3))
    e4 = _make(db, now - _dt.timedelta(days=4))
    for ev in (e1, e2, e3, e4):
        db.add_lfg_signup(ev, "42")
    db.set_signup_attendance(e1, "42", True)
    db.set_signup_attendance(e2, "42", True)
    db.set_signup_attendance(e3, "42", False)
    # e4 stays unmarked
    stats = db.fetch_user_lfg_attendance(
        "42", (now - _dt.timedelta(days=30)).isoformat(),
    )
    assert stats == {
        "signups": 4,
        "attended": 2,
        "not_marked_attended": 2,
    }


def test_window_excludes_older_events(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    old = _make(db, now - _dt.timedelta(days=60))
    new = _make(db, now - _dt.timedelta(days=5))
    db.add_lfg_signup(old, "42")
    db.add_lfg_signup(new, "42")
    db.set_signup_attendance(old, "42", True)
    db.set_signup_attendance(new, "42", False)
    stats = db.fetch_user_lfg_attendance(
        "42", (now - _dt.timedelta(days=30)).isoformat(),
    )
    assert stats == {
        "signups": 1,
        "attended": 0,
        "not_marked_attended": 1,
    }


def test_only_counts_target_user(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    ev = _make(db, now - _dt.timedelta(days=1))
    db.add_lfg_signup(ev, "42")
    db.add_lfg_signup(ev, "99")
    db.set_signup_attendance(ev, "42", True)
    db.set_signup_attendance(ev, "99", False)
    stats_42 = db.fetch_user_lfg_attendance(
        "42", (now - _dt.timedelta(days=30)).isoformat(),
    )
    stats_99 = db.fetch_user_lfg_attendance(
        "99", (now - _dt.timedelta(days=30)).isoformat(),
    )
    assert stats_42["attended"] == 1 and stats_42["not_marked_attended"] == 0
    assert stats_99["attended"] == 0 and stats_99["not_marked_attended"] == 1


def test_fetch_regear_request_for_death_dedupes_by_member_and_killboard_event(db):
    first = db.create_regear_request(
        discord_id="42",
        event_id=987654,
        content_type="CTA",
        gear_value=123456,
        image_url="https://albiononline.com/en/killboard/kill/987654",
        notes="auto event regear",
    )
    db.create_regear_request(
        discord_id="99",
        event_id=987654,
        content_type="CTA",
        gear_value=654321,
        image_url="https://albiononline.com/en/killboard/kill/987654",
        notes="same death id, different member",
    )

    found = db.fetch_regear_request_for_death("42", 987654)

    assert found is not None
    assert found["id"] == first
    assert found["discord_id"] == "42"
    assert db.fetch_regear_request_for_death("42", 123) is None
