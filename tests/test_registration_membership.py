from __future__ import annotations

from cogs.users_profile import (
    _api_membership_missing,
    _is_registered,
    _registration_upload_nudge,
)


def test_stale_preverification_profile_is_not_registered() -> None:
    assert not _is_registered({
        "albion_player_id": "player-id",
        "pending_home_guild_until": "2026-06-04T23:22:05",
        "pending_verification": 0,
        "verified_date": None,
        "lifecycle_role": None,
    })


def test_pending_review_profile_counts_as_registered() -> None:
    assert _is_registered({
        "albion_player_id": "player-id",
        "pending_verification": 1,
        "verified_date": None,
        "lifecycle_role": None,
    })


def test_verified_profile_counts_as_registered() -> None:
    assert _is_registered({
        "albion_player_id": "player-id",
        "pending_verification": 0,
        "verified_date": "2026-06-02T00:00:00",
        "lifecycle_role": "Recruit",
    })


def test_api_membership_missing_when_all_guild_fields_blank() -> None:
    assert _api_membership_missing({
        "guild_id": "",
        "guild_name": None,
        "alliance_id": "",
        "alliance_name": "",
        "alliance_tag": None,
    })


def test_api_membership_present_when_guild_or_alliance_exists() -> None:
    assert not _api_membership_missing({"guild_name": "HomeGuild"})
    assert not _api_membership_missing({"alliance_id": "abc"})


def test_early_registration_screenshot_nudges_to_click_register() -> None:
    title, body, include_button = _registration_upload_nudge(None)

    assert title == "Click Register first"
    assert "click **Register**" in body
    assert include_button


def test_pending_review_registration_screenshot_nudge_does_not_restart() -> None:
    title, body, include_button = _registration_upload_nudge({
        "albion_player_id": "player-id",
        "pending_verification": 1,
        "verified_date": None,
        "lifecycle_role": None,
    })

    assert title == "Screenshot already submitted"
    assert "officer review" in body
    assert not include_button
