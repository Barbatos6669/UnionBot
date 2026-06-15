"""Pure-logic tests for cogs._autocomplete helpers."""
from cogs._autocomplete import strip_tier_prefix, dedupe_by_base_name


def test_strip_tier_prefix_strips_all_tier_words():
    assert strip_tier_prefix("Adept's Assassin Hood") == "Assassin Hood"
    assert strip_tier_prefix("Expert's Dual Swords") == "Dual Swords"
    assert strip_tier_prefix("Master's Hammer") == "Hammer"
    assert strip_tier_prefix("Grandmaster's Bedrock Mace") == "Bedrock Mace"
    assert strip_tier_prefix("Elder's Lymhurst Cape") == "Lymhurst Cape"


def test_strip_tier_prefix_passthrough_for_unprefixed():
    assert strip_tier_prefix("Riding Horse") == "Riding Horse"
    assert strip_tier_prefix("") == ""


def test_dedupe_collapses_per_tier_rows_to_one_entry():
    rows = [
        {"unique_name": "T4_2H_DUALSWORD", "name": "Adept's Dual Swords", "tier": 4, "category": "2H"},
        {"unique_name": "T5_2H_DUALSWORD", "name": "Expert's Dual Swords", "tier": 5, "category": "2H"},
        {"unique_name": "T6_2H_DUALSWORD", "name": "Master's Dual Swords", "tier": 6, "category": "2H"},
        {"unique_name": "T7_2H_DUALSWORD", "name": "Grandmaster's Dual Swords", "tier": 7, "category": "2H"},
        {"unique_name": "T8_2H_DUALSWORD", "name": "Elder's Dual Swords", "tier": 8, "category": "2H"},
    ]
    out = dedupe_by_base_name(rows)
    assert len(out) == 1
    assert out[0]["name"] == "Dual Swords"
    # Highest-tier row's metadata wins.
    assert out[0]["tier"] == 8
    assert out[0]["unique_name"] == "T8_2H_DUALSWORD"


def test_dedupe_preserves_first_seen_order_across_distinct_bases():
    rows = [
        {"unique_name": "T4_HEAD_LEATHER_SET1", "name": "Adept's Assassin Hood", "tier": 4, "category": "HEAD"},
        {"unique_name": "T4_HEAD_PLATE_SET1", "name": "Adept's Knight Helmet", "tier": 4, "category": "HEAD"},
        {"unique_name": "T8_HEAD_LEATHER_SET1", "name": "Elder's Assassin Hood", "tier": 8, "category": "HEAD"},
    ]
    out = dedupe_by_base_name(rows)
    names = [r["name"] for r in out]
    assert names == ["Assassin Hood", "Knight Helmet"]
    # Assassin Hood was upgraded to tier 8 by the later row.
    assert out[0]["tier"] == 8


def test_dedupe_respects_limit():
    rows = [
        {"unique_name": f"T8_X_{i}", "name": f"Item {i}", "tier": 8, "category": "2H"}
        for i in range(40)
    ]
    out = dedupe_by_base_name(rows, limit=25)
    assert len(out) == 25
    assert out[0]["name"] == "Item 0"
    assert out[-1]["name"] == "Item 24"


def test_dedupe_skips_empty_names():
    rows = [
        {"unique_name": "T4_X", "name": "", "tier": 4, "category": "2H"},
        {"unique_name": "T4_Y", "name": "Adept's Bow", "tier": 4, "category": "2H"},
    ]
    out = dedupe_by_base_name(rows)
    assert len(out) == 1
    assert out[0]["name"] == "Bow"


def test_comp_slot_summary_strips_tier_prefix():
    """Display layer should hide Adept's/Master's/etc. prefixes on legacy
    rows that were saved before the autocomplete dedupe."""
    from cogs.comp import _slot_summary_line

    slot = {
        "slot_order": 1,
        "role": "Main DPS",
        "weapon": "Adept's Dual Swords",
        "head": "Master's Assassin Hood",
        "chest": "Elder's Hellion Jacket",
        "shoes": None,
        "cape": "Grandmaster's Lymhurst Cape",
        "ip_min": 1200,
        "required": 1,
    }
    line = _slot_summary_line(slot)
    assert "Dual Swords" in line
    assert "Assassin Hood" in line
    assert "Hellion Jacket" in line
    assert "Lymhurst Cape" in line
    # Make sure the tier-quality words got stripped out.
    for prefix in ("Adept's", "Master's", "Elder's", "Grandmaster's"):
        assert prefix not in line
