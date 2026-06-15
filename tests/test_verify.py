"""Tests for the pure helper in ``cogs.verify``."""

from __future__ import annotations

from cogs.verify import pick_best_candidate


def test_empty_returns_none() -> None:
    assert pick_best_candidate([], "anyguild") is None


def test_single_candidate_is_returned_as_is() -> None:
    only = {"Id": "x", "Name": "Foo", "GuildName": "Whatever"}
    assert pick_best_candidate([only], "target") is only


def test_prefers_candidate_already_in_target_guild() -> None:
    a = {"Id": "1", "Name": "Foo", "GuildName": "OtherGuild", "KillFame": 9_000_000}
    b = {"Id": "2", "Name": "Foo", "GuildName": "TargetGuild", "KillFame": 1}
    # Even with massively higher fame on `a`, `b` wins because it's in the target.
    assert pick_best_candidate([a, b], "targetguild") is b


def test_target_match_is_case_insensitive() -> None:
    a = {"Id": "1", "Name": "Foo", "GuildName": "OtherGuild"}
    b = {"Id": "2", "Name": "Foo", "GuildName": "TARGETguild"}
    assert pick_best_candidate([a, b], "targetguild") is b


def test_falls_back_to_guilded_then_alliance_then_fame() -> None:
    guildless = {"Id": "1", "Name": "Foo", "KillFame": 5_000_000}
    in_guild = {"Id": "2", "Name": "Foo", "GuildName": "SomeGuild", "GuildId": "g1"}
    # Neither matches the target, but `in_guild` should beat `guildless`.
    assert pick_best_candidate([guildless, in_guild], "nope") is in_guild


def test_higher_fame_wins_among_otherwise_equal() -> None:
    low = {"Id": "1", "Name": "Foo", "GuildName": "G1", "GuildId": "g1",
           "AllianceId": "a1", "KillFame": 100}
    high = {"Id": "2", "Name": "Foo", "GuildName": "G2", "GuildId": "g2",
            "AllianceId": "a2", "KillFame": 9_000_000, "DeathFame": 500_000}
    assert pick_best_candidate([low, high], "target-not-here") is high


def test_handles_missing_fame_fields() -> None:
    a = {"Id": "1", "Name": "Foo"}
    b = {"Id": "2", "Name": "Foo", "GuildName": "G", "GuildId": "g"}
    # `b` should win on the guild presence tiebreak; no KeyError from missing fame.
    assert pick_best_candidate([a, b], "irrelevant") is b
