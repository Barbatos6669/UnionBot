"""Tests for the pure helpers in ``cogs.raffle``."""

from __future__ import annotations

from cogs.raffle import pick_winner, gather_event_attendee_ids


# ── pick_winner ───────────────────────────────────────────────────────────


def test_pick_winner_empty_returns_none() -> None:
    assert pick_winner([]) is None


def test_pick_winner_single_entry_returns_that_entry() -> None:
    assert pick_winner(["abc"]) == "abc"


def test_pick_winner_always_in_pool() -> None:
    pool = ["a", "b", "c", "d", "e"]
    for _ in range(200):
        assert pick_winner(pool) in pool


# ── gather_event_attendee_ids ─────────────────────────────────────────────


class _FakeDB:
    """Minimal stub mimicking the db.fetch_lfg_signups contract."""

    def __init__(self, signups: list[dict]) -> None:
        self._signups = signups

    def fetch_lfg_signups(self, event_id: int) -> list[dict]:  # noqa: ARG002
        return self._signups


def test_gather_attendees_filters_attended_only_by_default() -> None:
    db = _FakeDB([
        {"discord_id": "1", "attended": 1},
        {"discord_id": "2", "attended": 0},
        {"discord_id": "3", "attended": 1},
    ])
    assert gather_event_attendee_ids(db, 7, include_all_signups=False) == ["1", "3"]


def test_gather_attendees_includes_all_signups_when_requested() -> None:
    db = _FakeDB([
        {"discord_id": "1", "attended": 1},
        {"discord_id": "2", "attended": 0},
    ])
    assert gather_event_attendee_ids(db, 7, include_all_signups=True) == ["1", "2"]


def test_gather_attendees_dedupes_preserving_order() -> None:
    db = _FakeDB([
        {"discord_id": "1", "attended": 1},
        {"discord_id": "1", "attended": 1},
        {"discord_id": "2", "attended": 1},
    ])
    assert gather_event_attendee_ids(db, 7, include_all_signups=False) == ["1", "2"]


def test_gather_attendees_handles_missing_attended_field() -> None:
    db = _FakeDB([
        {"discord_id": "1"},                    # missing → not attended
        {"discord_id": "2", "attended": None},  # None → not attended
        {"discord_id": "3", "attended": 1},
    ])
    assert gather_event_attendee_ids(db, 7, include_all_signups=False) == ["3"]


def test_gather_attendees_empty_signups() -> None:
    assert gather_event_attendee_ids(_FakeDB([]), 7, include_all_signups=False) == []
