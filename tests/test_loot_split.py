"""Tests for the loot-split math extracted into ``cogs.loot.compute_loot_split``.

Critical invariants: every silver coin is accounted for (no money created,
none lost beyond the explicit ``rounding`` bucket that goes to the bank).
"""

from __future__ import annotations

import pytest

from cogs.loot import compute_loot_split, _parse_member_ids, _parse_silver_split_field
from sql_database import Database


# ── compute_loot_split ────────────────────────────────────────────────────


def test_no_tax_no_bonus_clean_split() -> None:
    r = compute_loot_split(1000, 0, 0, n_attendees=4, has_shotcaller=False)
    assert r == {
        "tax": 0,
        "payable": 1000,
        "sc_bonus": 0,
        "per_head": 250,
        "rounding": 0,
        "silver_total": 0,
        "silver_per_head": 0,
        "silver_rounding": 0,
        "silver_recipients": 4,
    }


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
                        + r["silver_per_head"] * r["silver_recipients"]
                        + r["silver_rounding"]
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


def test_silver_bag_pool_splits_separately_from_tradable_loot() -> None:
    r = compute_loot_split(
        1000,
        10,
        10,
        n_attendees=4,
        has_shotcaller=True,
        silver_total=503,
        n_silver_attendees=3,
    )
    assert r["tax"] == 100
    assert r["sc_bonus"] == 90
    assert r["per_head"] == 202
    assert r["rounding"] == 2
    assert r["silver_per_head"] == 167
    assert r["silver_rounding"] == 2
    assert r["silver_recipients"] == 3
    accounted = (
        r["tax"] + r["sc_bonus"] + r["per_head"] * 4 + r["rounding"]
        + r["silver_per_head"] * 3 + r["silver_rounding"]
    )
    assert accounted == 1503


def test_silver_bag_pool_with_everyone_opted_out_goes_to_rounding() -> None:
    r = compute_loot_split(
        0,
        0,
        0,
        n_attendees=4,
        has_shotcaller=False,
        silver_total=500,
        n_silver_attendees=0,
    )
    assert r["per_head"] == 0
    assert r["silver_per_head"] == 0
    assert r["silver_rounding"] == 500


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


# ── _parse_silver_split_field ─────────────────────────────────────────────


def test_parse_silver_split_field_amount_only() -> None:
    amount, optouts = _parse_silver_split_field("5,000,000")
    assert amount == 5_000_000
    assert optouts == []


@pytest.mark.parametrize("raw", ["5m", "5 m", "5 million", "5,000,000", "5_000_000", "5m silver"])
def test_parse_silver_split_field_human_amounts(raw: str) -> None:
    amount, optouts = _parse_silver_split_field(raw)
    assert amount == 5_000_000
    assert optouts == []


def test_parse_silver_split_field_with_optouts() -> None:
    amount, optouts = _parse_silver_split_field(
        "2.5m | optout: <@111111111111111111> <@!222222222222222222>"
    )
    assert amount == 2_500_000
    assert optouts == ["111111111111111111", "222222222222222222"]


# ── silver balance batch transaction ─────────────────────────────────────


def _fresh_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "loot.db"))
    db.connect()
    db.initialize_all_tables()
    return db


def _insert_profile(db: Database, discord_id: str, *, balance: int = 0) -> None:
    db.execute(
        "INSERT INTO user_profiles (discord_id, username, silver_balance) VALUES (?, ?, ?)",
        (discord_id, f"user-{discord_id}", balance),
    )


def test_adjust_silver_balances_batch_writes_balances_and_ledger(tmp_path) -> None:
    db = _fresh_db(tmp_path)
    try:
        _insert_profile(db, "101", balance=100)
        _insert_profile(db, "202", balance=0)

        result = db.adjust_silver_balances_batch(
            [
                {
                    "discord_id": "101",
                    "delta": 25,
                    "reason": "loot split",
                    "ref_type": "event",
                    "ref_id": "88",
                    "actor_id": "999",
                },
                {
                    "discord_id": "202",
                    "delta": 50,
                    "reason": "loot split",
                    "ref_type": "event",
                    "ref_id": "88",
                    "actor_id": "999",
                },
            ]
        )

        assert result == ({"101": 125, "202": 50}, [])
        assert db.fetch_silver_balance("101") == 125
        assert db.fetch_silver_balance("202") == 50
        rows = db.connection.execute("SELECT discord_id, delta FROM silver_ledger").fetchall()
        assert [(row["discord_id"], row["delta"]) for row in rows] == [
            ("101", 25),
            ("202", 50),
        ]
    finally:
        db.close()


def test_adjust_silver_balances_batch_rolls_back_when_profile_missing(tmp_path) -> None:
    db = _fresh_db(tmp_path)
    try:
        _insert_profile(db, "101", balance=100)

        result = db.adjust_silver_balances_batch(
            [
                {"discord_id": "101", "delta": 25, "reason": "loot split"},
                {"discord_id": "missing", "delta": 50, "reason": "loot split"},
            ]
        )

        assert result == ({}, ["missing"])
        assert db.fetch_silver_balance("101") == 100
        count = db.connection.execute("SELECT COUNT(*) AS n FROM silver_ledger").fetchone()["n"]
        assert count == 0
    finally:
        db.close()
