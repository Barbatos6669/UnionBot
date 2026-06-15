"""Tests for the loot-split math extracted into ``cogs.loot.compute_loot_split``.

Critical invariants: every silver coin is accounted for (no money created,
none lost beyond the explicit ``rounding`` bucket that goes to the bank).
"""

from __future__ import annotations

import pytest

from cogs.loot import compute_loot_split, _parse_member_ids


# ── compute_loot_split ────────────────────────────────────────────────────


def test_no_tax_no_bonus_clean_split() -> None:
    r = compute_loot_split(1000, 0, 0, n_attendees=4, has_shotcaller=False)
    assert r == {"tax": 0, "payable": 1000, "sc_bonus": 0, "per_head": 250, "rounding": 0}


def test_tax_only() -> None:
    r = compute_loot_split(1000, 10, 0, n_attendees=4, has_shotcaller=False)
    # tax 100, payable 900, /4 = 225 per head, no rounding.
    assert r["tax"] == 100
    assert r["payable"] == 900
    assert r["per_head"] == 225
    assert r["rounding"] == 0
    assert r["sc_bonus"] == 0


def test_bonus_applied_only_when_shotcaller_present() -> None:
    r_yes = compute_loot_split(1000, 0, 10, n_attendees=4, has_shotcaller=True)
    r_no = compute_loot_split(1000, 0, 10, n_attendees=4, has_shotcaller=False)
    assert r_yes["sc_bonus"] == 100
    assert r_no["sc_bonus"] == 0


def test_zero_attendees_no_per_head() -> None:
    r = compute_loot_split(1000, 0, 0, n_attendees=0, has_shotcaller=False)
    assert r["per_head"] == 0
    # All payable becomes rounding (goes to bank) so silver isn't lost.
    assert r["rounding"] == 1000


def test_silver_conservation_is_exact() -> None:
    """Tax + sc_bonus + per_head*n + rounding must equal the original pool
    for every combination — this is the property officers actually care about.
    """
    for total in (1, 100, 999, 12_345, 7_777_777):
        for tax_pct in (0, 1, 10, 25):
            for bonus_pct in (0, 5, 15):
                for n in (1, 3, 5, 20):
                    r = compute_loot_split(total, tax_pct, bonus_pct, n_attendees=n, has_shotcaller=True)
                    accounted = (
                        r["tax"] + r["sc_bonus"] + r["per_head"] * n + r["rounding"]
                    )
                    assert accounted == total, (
                        f"silver leak: total={total} tax%={tax_pct} bonus%={bonus_pct} "
                        f"n={n} → {r}"
                    )


def test_negative_inputs_are_clamped() -> None:
    r = compute_loot_split(-100, -5, -10, n_attendees=-3, has_shotcaller=True)
    assert r["tax"] == 0
    assert r["payable"] == 0
    assert r["per_head"] == 0
    assert r["rounding"] == 0


def test_rounding_goes_to_bank_not_one_player() -> None:
    # 100 silver, 3 attendees → 33 per head, 1 silver rounding to bank.
    r = compute_loot_split(100, 0, 0, n_attendees=3, has_shotcaller=False)
    assert r["per_head"] == 33
    assert r["rounding"] == 1


# ── _parse_member_ids ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("", []),
    ("nothing here", []),
    ("<@111111111111111111>", ["111111111111111111"]),
    ("<@!222222222222222222>", ["222222222222222222"]),
    ("333333333333333333", ["333333333333333333"]),
    (
        "<@111111111111111111> and <@!222222222222222222>",
        ["111111111111111111", "222222222222222222"],
    ),
])
def test_parse_member_ids_basic(raw: str, expected: list[str]) -> None:
    assert _parse_member_ids(raw) == expected


def test_parse_member_ids_dedupes_preserving_order() -> None:
    out = _parse_member_ids(
        "<@111111111111111111> <@!222222222222222222> 111111111111111111"
    )
    assert out == ["111111111111111111", "222222222222222222"]


def test_parse_member_ids_skips_short_ids() -> None:
    # 16-digit number is too short for a Discord snowflake.
    assert _parse_member_ids("1234567890123456") == []
