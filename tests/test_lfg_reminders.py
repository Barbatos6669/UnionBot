"""Tests for the LFG pre-event reminder query helpers.

The actual DM dispatcher in ``cogs.lfg.LFG.dispatch_reminders`` is a
discord.py task loop that needs an event loop + a bot object, so we test
the database layer it relies on: ``fetch_lfg_events_to_remind`` returns
only matching events and ``mark_lfg_event_reminded`` makes the same
event drop out of subsequent fetches (one-shot semantics).
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
from types import SimpleNamespace
from typing import Iterator

import pytest

from cogs.automation import _legacy_event_reminders_enabled
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


def _iso(offset_minutes: int, base: _dt.datetime) -> str:
    return (base + _dt.timedelta(minutes=offset_minutes)).isoformat()


def _make(db, starts_offset_min: int, base: _dt.datetime, *, title="Evt") -> int:
    return db.create_lfg_event(
        slot_label="GENERAL",
        is_prime=False,
        title=title,
        description="",
        comp_notes="",
        starts_at=_iso(starts_offset_min, base),
        ends_at=_iso(starts_offset_min + 60, base),
        prep_minutes=30,
        review_minutes=15,
        creator_id="111",
    )


def test_returns_events_inside_window(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    in_window = _make(db, 15, now, title="soon")
    out_of_window = _make(db, 90, now, title="later")
    rows = db.fetch_lfg_events_to_remind(now.isoformat(), window_minutes=30)
    ids = {r["id"] for r in rows}
    assert in_window in ids
    assert out_of_window not in ids


def test_excludes_already_reminded(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, 10, now)
    db.mark_lfg_event_reminded(event_id, now.isoformat())
    rows = db.fetch_lfg_events_to_remind(now.isoformat(), window_minutes=30)
    assert all(r["id"] != event_id for r in rows)


def test_excludes_past_events(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    past_id = _make(db, -5, now, title="already started")
    rows = db.fetch_lfg_events_to_remind(now.isoformat(), window_minutes=30)
    assert all(r["id"] != past_id for r in rows)


def test_excludes_cancelled_events(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, 10, now)
    db.cancel_lfg_event(event_id)
    rows = db.fetch_lfg_events_to_remind(now.isoformat(), window_minutes=30)
    assert all(r["id"] != event_id for r in rows)


def test_mark_is_idempotent_one_shot(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, 5, now)
    rows = db.fetch_lfg_events_to_remind(now.isoformat(), window_minutes=30)
    assert any(r["id"] == event_id for r in rows)
    db.mark_lfg_event_reminded(event_id, now.isoformat())
    # Second tick of the loop must NOT see it again.
    rows2 = db.fetch_lfg_events_to_remind(now.isoformat(), window_minutes=30)
    assert all(r["id"] != event_id for r in rows2)


def test_legacy_automation_reminders_disabled_by_default(db):
    bot = SimpleNamespace(db=db)

    assert not _legacy_event_reminders_enabled(bot)

    db.set_config("automation_event_reminders_enabled", "1")

    assert _legacy_event_reminders_enabled(bot)


# ── finished-post cleanup ────────────────────────────────────────────────


def test_cleanup_includes_completed_events_after_review_window(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, -90, now, title="done")
    db.set_lfg_message(event_id, "123", "456")
    db.execute("UPDATE lfg_events SET status = 'completed' WHERE id = ?", (event_id,))

    rows = db.fetch_lfg_events_to_cleanup(now.isoformat())

    assert [r["id"] for r in rows] == [event_id]


def test_cleanup_waits_for_review_window(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, -65, now, title="review active")
    db.set_lfg_message(event_id, "123", "456")
    db.execute("UPDATE lfg_events SET status = 'completed' WHERE id = ?", (event_id,))

    rows = db.fetch_lfg_events_to_cleanup(now.isoformat())

    assert all(r["id"] != event_id for r in rows)


def test_cleanup_includes_cancelled_events_immediately(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, 60, now, title="cancelled")
    db.set_lfg_message(event_id, "123", "456")
    db.cancel_lfg_event(event_id)

    rows = db.fetch_lfg_events_to_cleanup(now.isoformat())

    assert [r["id"] for r in rows] == [event_id]


def test_cleanup_mark_prevents_retry(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, -90, now, title="done")
    db.set_lfg_message(event_id, "123", "456")
    db.execute("UPDATE lfg_events SET status = 'completed' WHERE id = ?", (event_id,))
    db.mark_lfg_event_cleaned(event_id, now.isoformat())

    rows = db.fetch_lfg_events_to_cleanup(now.isoformat())

    assert all(r["id"] != event_id for r in rows)


def test_cleanup_waits_for_event_voice_to_end(db):
    now = _dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)
    event_id = _make(db, -90, now, title="still in voice")
    db.set_lfg_message(event_id, "123", "456")
    db.set_lfg_voice_channel_id(event_id, "789", now.isoformat())
    db.execute("UPDATE lfg_events SET status = 'completed' WHERE id = ?", (event_id,))

    rows = db.fetch_lfg_events_to_cleanup(now.isoformat())

    assert all(r["id"] != event_id for r in rows)

    db.mark_lfg_voice_deleted(event_id, now.isoformat())

    rows = db.fetch_lfg_events_to_cleanup(now.isoformat())

    assert [r["id"] for r in rows] == [event_id]


# ── long-running event voice attendance ──────────────────────────────────


def test_active_event_window_includes_ended_event_with_live_voice(db):
    now = _dt.datetime.now(_dt.timezone.utc)
    event_id = _make(db, -90, now, title="overtime")
    db.set_lfg_voice_channel_id(event_id, "789", now.isoformat())

    rows = db.fetch_active_event_window()

    assert event_id in {r["id"] for r in rows}


def test_active_event_window_excludes_ended_event_after_voice_deleted(db):
    now = _dt.datetime.now(_dt.timezone.utc)
    event_id = _make(db, -90, now, title="overtime ended")
    db.set_lfg_voice_channel_id(event_id, "789", now.isoformat())
    db.mark_lfg_voice_deleted(event_id, now.isoformat())

    rows = db.fetch_active_event_window()

    assert event_id not in {r["id"] for r in rows}


def test_reconciliation_waits_for_event_voice_deletion(db):
    now = _dt.datetime.now(_dt.timezone.utc)
    event_id = _make(db, -90, now, title="wait for voice")
    db.set_lfg_voice_channel_id(event_id, "789", now.isoformat())

    rows = db.fetch_events_needing_reconciliation(fallback_grace_minutes=30)

    assert event_id not in {r["id"] for r in rows}

    db.mark_lfg_voice_deleted(event_id, now.isoformat())

    rows = db.fetch_events_needing_reconciliation(fallback_grace_minutes=30)

    assert event_id in {r["id"] for r in rows}


def test_reconciliation_uses_fallback_grace_for_no_voice_events(db):
    now = _dt.datetime.now(_dt.timezone.utc)
    recent_no_voice = _make(db, -75, now, title="recent no voice")
    old_no_voice = _make(db, -120, now, title="old no voice")

    rows = db.fetch_events_needing_reconciliation(fallback_grace_minutes=30)
    ids = {r["id"] for r in rows}

    assert recent_no_voice not in ids
    assert old_no_voice in ids
