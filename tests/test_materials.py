"""Tests for the pure helpers in ``cogs._materials`` — the shopping-bounty
reward estimator. Numbers here are deliberately rough (Albion market drifts
weekly), so the tests only assert *shape* and rounding behaviour, not exact
silver values.
"""

from __future__ import annotations

import pytest

from cogs._materials import (
    _round_silver,
    _tier,
    estimate_unit_price,
    estimate_line_reward,
    DEFAULT_SERVICE_FEE,
)


# ── _round_silver ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("inp, expected", [
    (0, 0),
    (-50, 0),          # clamp to zero
    (999, 999),        # below 1k stays exact
    (1234, 1200),      # rounds to nearest 100
    (12_345, 12_000),  # rounds to nearest 1k
    (123_456, 125_000),  # rounds to nearest 5k
    (1_234_567, 1_250_000),  # rounds to nearest 50k
])
def test_round_silver(inp: int, expected: int) -> None:
    assert _round_silver(inp) == expected


def test_round_silver_returns_int_for_float_input() -> None:
    assert isinstance(_round_silver(1234.7), int)


# ── _tier ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("uid, expected", [
    ("T4_HEAD_PLATE_SET1", 4),
    ("T8_BAG", 8),
    ("INVALID", 0),
    ("", 0),
])
def test_tier_parsing(uid: str, expected: int) -> None:
    assert _tier(uid) == expected


# ── estimate_unit_price ───────────────────────────────────────────────────


def test_unknown_item_returns_zero() -> None:
    assert estimate_unit_price("NOT_AN_ITEM") == 0


def test_unit_price_scales_with_tier() -> None:
    t4 = estimate_unit_price("T4_HEAD_PLATE_SET1")
    t6 = estimate_unit_price("T6_HEAD_PLATE_SET1")
    t8 = estimate_unit_price("T8_HEAD_PLATE_SET1")
    assert 0 < t4 < t6 < t8


def test_unit_price_scales_with_enchantment() -> None:
    base = estimate_unit_price("T7_HEAD_PLATE_SET1", enchant=0)
    e3 = estimate_unit_price("T7_HEAD_PLATE_SET1", enchant=3)
    assert e3 > base


def test_consumables_ignore_enchant_and_quality() -> None:
    a = estimate_unit_price("T6_POTION_HEAL", enchant=0, quality=1)
    b = estimate_unit_price("T6_POTION_HEAL", enchant=4, quality=5)
    assert a == b > 0


def test_mount_ignores_enchant_and_quality() -> None:
    a = estimate_unit_price("T5_MOUNT_HORSE", enchant=0, quality=1)
    b = estimate_unit_price("T5_MOUNT_HORSE", enchant=4, quality=5)
    assert a == b > 0


def test_enchant_clamp_to_valid_range() -> None:
    # An enchant of 99 should clamp at 4, not crash.
    out_oob = estimate_unit_price("T7_HEAD_PLATE_SET1", enchant=99)
    out_max = estimate_unit_price("T7_HEAD_PLATE_SET1", enchant=4)
    assert out_oob == out_max


# ── estimate_line_reward ──────────────────────────────────────────────────


def test_line_reward_returns_three_ints() -> None:
    unit, items_total, fee = estimate_line_reward("T6_HEAD_PLATE_SET1", count=3)
    assert isinstance(unit, int) and isinstance(items_total, int) and isinstance(fee, int)
    assert unit > 0
    assert items_total >= unit  # rounding might fold but never grows


def test_line_reward_count_scales() -> None:
    _, one, _ = estimate_line_reward("T6_HEAD_PLATE_SET1", count=1)
    _, ten, _ = estimate_line_reward("T6_HEAD_PLATE_SET1", count=10)
    assert ten >= one  # not strictly 10x because of rounding


def test_line_reward_default_service_fee() -> None:
    _, _, fee = estimate_line_reward("T6_HEAD_PLATE_SET1")
    assert fee == DEFAULT_SERVICE_FEE


def test_line_reward_override_service_fee() -> None:
    _, _, fee = estimate_line_reward("T6_HEAD_PLATE_SET1", service_fee=42_000)
    assert fee == 42_000


def test_zero_count_is_clamped_to_one() -> None:
    _, items_total, _ = estimate_line_reward("T6_HEAD_PLATE_SET1", count=0)
    # Implementation uses max(1, count) so a zero count still gives a payable line.
    assert items_total > 0
