"""Tests for ``config.derive_lifecycle`` — the function that decides which
lifecycle role (Probationary / Member / Veteran) a user should hold based on
how long they've been in the server.
"""

from __future__ import annotations

import datetime as dt

import pytest

from config import derive_lifecycle


def _iso_days_ago(days: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()


def test_none_returns_probationary() -> None:
    assert derive_lifecycle(None) == "Probationary"


def test_empty_string_returns_probationary() -> None:
    assert derive_lifecycle("") == "Probationary"


def test_brand_new_member_is_probationary() -> None:
    assert derive_lifecycle(_iso_days_ago(0)) == "Probationary"


def test_just_under_threshold_is_probationary() -> None:
    # 29 days < 30-day Member cutoff.
    assert derive_lifecycle(_iso_days_ago(29)) == "Probationary"


def test_exactly_at_member_threshold() -> None:
    assert derive_lifecycle(_iso_days_ago(30)) == "Member"


def test_member_range() -> None:
    assert derive_lifecycle(_iso_days_ago(60)) == "Member"


def test_exactly_at_veteran_threshold() -> None:
    assert derive_lifecycle(_iso_days_ago(90)) == "Veteran"


def test_well_past_veteran_threshold() -> None:
    assert derive_lifecycle(_iso_days_ago(500)) == "Veteran"


def test_custom_thresholds() -> None:
    # 7-day Member cutoff, 14-day Veteran cutoff.
    assert derive_lifecycle(_iso_days_ago(5),  probationary_days=7,  member_days=14) == "Probationary"
    assert derive_lifecycle(_iso_days_ago(10), probationary_days=7,  member_days=14) == "Member"
    assert derive_lifecycle(_iso_days_ago(20), probationary_days=7,  member_days=14) == "Veteran"


def test_invalid_iso_raises() -> None:
    # The function deliberately does not silently swallow garbage ISO inputs —
    # an upstream bug should surface, not produce a wrong-lifecycle.
    with pytest.raises(ValueError):
        derive_lifecycle("not-a-date")
