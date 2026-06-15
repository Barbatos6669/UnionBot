"""Pure-logic tests for the LFG ping role lookup and event update helpers.

We don't spin up the bot — we exercise the slice of the data layer the
new create-event flow depends on:

* ``_get_ping_for_type`` returns a properly formatted ``<@&id>`` mention
  when a role is mapped via the ``lfg_role_<key>`` config key, and ``None``
  otherwise.
* ``update_lfg_event`` patches only allow-listed columns and refuses to
  touch ``creator_id`` / ``status`` / ``message_id``.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import tempfile
from collections.abc import Iterator

import pytest

from cogs._lfg_config import CFG_ROLE_PREFIX
from cogs._lfg_helpers import _format_event_embed, _get_ping_for_type
from cogs._primetime_claims import _fetch_prime_claims
from sql_database import Database


@pytest.fixture()
def db() -> Iterator[Database]:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = Database(path)
    d.connect()
    d.initialize_all_tables()
    yield d
    with contextlib.suppress(Exception):
        d.close()
    with contextlib.suppress(OSError):
        os.unlink(path)


# ── _get_ping_for_type ──────────────────────────────────────────────────


def test_ping_none_for_no_event_type(db):
    assert _get_ping_for_type(db, None) is None


def test_ping_none_when_no_role_mapped(db):
    assert _get_ping_for_type(db, "zvz") is None


def test_ping_mention_when_role_mapped(db):
    db.set_config(f"{CFG_ROLE_PREFIX}zvz", "1234567890")
    assert _get_ping_for_type(db, "zvz") == "<@&1234567890>"


def test_ping_none_when_role_id_blank(db):
    # Empty string is how /lfg clear-role stores a cleared mapping.
    db.set_config(f"{CFG_ROLE_PREFIX}gvg", "")
    assert _get_ping_for_type(db, "gvg") is None


# ── update_lfg_event ────────────────────────────────────────────────────


def _make_event(db) -> int:
    return db.create_lfg_event(
        slot_label="GENERAL",
        is_prime=False,
        title="Original Title",
        description="Original description",
        comp_notes="",
        starts_at="2026-06-01T20:00:00+00:00",
        ends_at="2026-06-01T21:00:00+00:00",
        prep_minutes=30,
        review_minutes=15,
        creator_id="111",
        event_type="zvz",
        ip_requirement="1500 IP",
    )


def test_update_patches_allow_listed_columns(db):
    event_id = _make_event(db)
    ok = db.update_lfg_event(event_id, {
        "title": "New Title",
        "description": "Edited",
        "starts_at": "2026-06-02T20:00:00+00:00",
        "ends_at": "2026-06-02T21:00:00+00:00",
    })
    assert ok is True
    row = db.fetch_lfg_event(event_id)
    assert row["title"] == "New Title"
    assert row["description"] == "Edited"
    assert row["starts_at"] == "2026-06-02T20:00:00+00:00"


def test_update_ignores_unknown_columns(db):
    event_id = _make_event(db)
    # ``creator_id`` and ``status`` must never be writable via this path.
    ok = db.update_lfg_event(event_id, {
        "creator_id": "999",
        "status": "cancelled",
        "message_id": "abc",
        "title": "Touched",  # one real change so the call returns True
    })
    assert ok is True
    row = db.fetch_lfg_event(event_id)
    assert row["creator_id"] == "111"
    assert row["status"] == "open"  # unchanged
    assert row["message_id"] is None
    assert row["title"] == "Touched"


def test_update_no_allowed_fields_returns_false(db):
    event_id = _make_event(db)
    ok = db.update_lfg_event(event_id, {"creator_id": "999"})
    assert ok is False


def test_create_lfg_event_stamps_event_type(db):
    event_id = _make_event(db)
    row = db.fetch_lfg_event(event_id)
    assert row["event_type"] == "zvz"


def test_create_lfg_event_stores_ip_requirement(db):
    event_id = _make_event(db)
    row = db.fetch_lfg_event(event_id)
    assert row["ip_requirement"] == "1500 IP"


def test_set_lfg_discussion_thread_id(db):
    event_id = _make_event(db)
    db.set_lfg_discussion_thread_id(event_id, "555")
    row = db.fetch_lfg_event(event_id)
    assert row["discussion_thread_id"] == "555"


def test_event_embed_includes_discussion_thread(db):
    event_id = _make_event(db)
    db.set_lfg_discussion_thread_id(event_id, "555")
    embed = _format_event_embed(db, db.fetch_lfg_event(event_id))
    assert any(
        field.name == "Discussion" and field.value == "<#555>"
        for field in embed.fields
    )


def test_event_embed_includes_minimum_ip(db):
    event_id = _make_event(db)
    embed = _format_event_embed(db, db.fetch_lfg_event(event_id))
    assert any(
        field.name == "Minimum IP" and field.value == "1500 IP"
        for field in embed.fields
    )


def test_prime_claim_fetch_excludes_cancelled_events(db):
    event_id = db.create_lfg_event(
        slot_label="PRIME 04:00-05:00",
        is_prime=True,
        title="Cancelled Prime",
        description="",
        comp_notes="",
        starts_at="2026-06-03T04:00:00+00:00",
        ends_at="2026-06-03T05:00:00+00:00",
        prep_minutes=30,
        review_minutes=15,
        creator_id="111",
        event_type="ganking",
    )
    db.cancel_lfg_event(event_id)

    rows = _fetch_prime_claims(
        db,
        dt.datetime(2026, 6, 3, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 6, 4, tzinfo=dt.timezone.utc),
    )

    assert rows == []


# ── set_lfg_event_comp ─────────────────────────────────────────────────


def test_changing_lfg_comp_clears_old_build_claims(db):
    event_id = _make_event(db)
    comp_one = db.create_comp(
        name="Clap Comp",
        content_type="zvz",
        description="",
        created_by="111",
    )
    comp_two = db.create_comp(
        name="Brawl Comp",
        content_type="zvz",
        description="",
        created_by="111",
    )
    slot_id = db.add_comp_slot(comp_one, {"role": "DPS", "weapon": "Brimstone"})

    assert db.set_lfg_event_comp(event_id, comp_one) is True
    ok, reason = db.claim_lfg_slot(event_id, "222", slot_id)
    assert (ok, reason) == (True, "claimed")
    assert db.fetch_lfg_signups(event_id)[0]["slot_id"] == slot_id

    assert db.set_lfg_event_comp(event_id, comp_two) is True
    signup = db.fetch_lfg_signups(event_id)[0]
    assert signup["discord_id"] == "222"
    assert signup["slot_id"] is None
