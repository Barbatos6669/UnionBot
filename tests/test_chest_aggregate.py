"""Pure-logic tests for cogs.loadout_chest helpers."""
import sqlite3

from cogs.loadout_chest import (
    aggregate_stock_by_base_name,
    format_chest_item_display,
    resolve_item_display_name,
    _fallback_item_name,
    _strip_tier_prefix,
)


def test_strip_tier_prefix_loadout_chest():
    assert _strip_tier_prefix("Adept's Lymhurst Cape") == "Lymhurst Cape"
    assert _strip_tier_prefix("Elder's Dual Swords") == "Dual Swords"


def test_aggregate_collapses_per_tier_and_quality():
    rows = [
        # All variants of Lymhurst Cape — different qualities, enchants, names.
        {"item_id": "T4_CAPEITEM_FW_LYMHURST", "item_name": "Adept's Lymhurst Cape",
         "category": "CAPE", "count": 10, "quality": 1, "enchant": 3},
        {"item_id": "T4_CAPEITEM_FW_LYMHURST", "item_name": "Adept's Lymhurst Cape",
         "category": "CAPE", "count": 38, "quality": 2, "enchant": 3},
        {"item_id": "T6_CAPEITEM_FW_LYMHURST", "item_name": "Master's Lymhurst Cape",
         "category": "CAPE", "count": 5, "quality": 1, "enchant": 1},
        # Different cape, should stay separate.
        {"item_id": "T4_CAPEITEM_FW_MARTLOCK", "item_name": "Adept's Martlock Cape",
         "category": "CAPE", "count": 8, "quality": 3, "enchant": 3},
    ]
    out = aggregate_stock_by_base_name(rows)
    assert len(out) == 2
    # Lymhurst should be first (53 > 8) and have 3 variants.
    assert out[0]["name"] == "Lymhurst Cape"
    assert out[0]["count"] == 53
    assert out[0]["variants"] == 3
    assert out[0]["category"] == "CAPE"
    assert out[1]["name"] == "Martlock Cape"
    assert out[1]["count"] == 8
    assert out[1]["variants"] == 1


def test_aggregate_keeps_categories_separate():
    rows = [
        {"item_id": "T4_2H_X", "item_name": "Adept's Bow",
         "category": "2H", "count": 3},
        # Same base name, different category — keep separate.
        {"item_id": "T4_OFF_X", "item_name": "Adept's Bow",
         "category": "OFF", "count": 2},
    ]
    out = aggregate_stock_by_base_name(rows)
    assert len(out) == 2
    cats = sorted(a["category"] for a in out)
    assert cats == ["2H", "OFF"]


def test_aggregate_sort_by_count_desc_then_name():
    rows = [
        {"item_id": "X", "item_name": "Adept's Zebra",
         "category": "MOUNT", "count": 5},
        {"item_id": "Y", "item_name": "Adept's Aardvark",
         "category": "MOUNT", "count": 5},
        {"item_id": "Z", "item_name": "Adept's Buffalo",
         "category": "MOUNT", "count": 10},
    ]
    out = aggregate_stock_by_base_name(rows)
    names = [a["name"] for a in out]
    assert names == ["Buffalo", "Aardvark", "Zebra"]


def test_aggregate_handles_missing_item_name():
    rows = [
        # No item_name — falls back to item_id (which has no tier prefix).
        {"item_id": "T6_BAG", "item_name": None,
         "category": "OTHER", "count": 4},
    ]
    out = aggregate_stock_by_base_name(rows)
    assert len(out) == 1
    assert out[0]["name"] == "T6_BAG"
    assert out[0]["count"] == 4


def test_aggregate_empty_input():
    assert aggregate_stock_by_base_name([]) == []


def test_fallback_item_name_is_readable():
    assert _fallback_item_name("T5_HEAD_PLATE_UNDEAD") == "T5 Head Plate Undead"
    assert _fallback_item_name("T6_MOUNT_ARMORED_HORSE") == "T6 Mount Armored Horse"


def test_resolve_item_display_name_uses_items_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE items (unique_name TEXT PRIMARY KEY, name TEXT)")
    conn.execute(
        "INSERT INTO items (unique_name, name) VALUES (?, ?)",
        ("T5_HEAD_PLATE_UNDEAD", "Expert's Graveguard Helmet"),
    )

    class FakeDb:
        cursor = conn.cursor()

    assert resolve_item_display_name(FakeDb(), "T5_HEAD_PLATE_UNDEAD") == "Expert's Graveguard Helmet"


def test_format_chest_item_display_adds_variant_bits_only_when_needed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE items (unique_name TEXT PRIMARY KEY, name TEXT)")
    conn.execute(
        "INSERT INTO items (unique_name, name) VALUES (?, ?)",
        ("T5_ARMOR_CLOTH_SET2", "Expert's Cleric Robe"),
    )

    class FakeDb:
        cursor = conn.cursor()

    assert format_chest_item_display(FakeDb(), "T5_ARMOR_CLOTH_SET2") == "Expert's Cleric Robe"
    assert (
        format_chest_item_display(FakeDb(), "T5_ARMOR_CLOTH_SET2", quality=3, enchant=1)
        == "Expert's Cleric Robe (+1 Q3)"
    )
