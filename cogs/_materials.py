"""Heuristic material estimator for Albion crafted items.

We don't have exact recipes locally, but Albion's crafting is formulaic
enough that we can give a useful first estimate from the item's
``UniqueName`` and category. The wiki link returned alongside the
estimate has the authoritative number.

Coverage:
- Plate / leather / cloth: head (8), armor (16), shoes (8) refined mats.
- Weapons: 1H/main (16), 2H (24), off-hand (12) refined mats.
- Capes (8 cloth), bags (8 cloth + 8 leather).
- Mounts, potions, food, consumables: returns a friendly fallback string.
"""
from __future__ import annotations

import re

# Refined material display names by tier and main material family.
_REFINED: dict[str, dict[int, str]] = {
    "metal": {
        2: "Iron Bar (T2)",
        3: "Bronze Bar (T3)",
        4: "Steel Bar (T4)",
        5: "Titanium Steel Bar (T5)",
        6: "Runite Steel Bar (T6)",
        7: "Meteorite Steel Bar (T7)",
        8: "Adamantium Steel Bar (T8)",
    },
    "leather": {
        2: "Stiff Leather (T2)",
        3: "Cured Leather (T3)",
        4: "Rugged Leather (T4)",
        5: "Worked Leather (T5)",
        6: "Heavy Leather (T6)",
        7: "Reinforced Leather (T7)",
        8: "Hardened Leather (T8)",
    },
    "cloth": {
        2: "Simple Cloth (T2)",
        3: "Neat Cloth (T3)",
        4: "Fine Cloth (T4)",
        5: "Ornate Cloth (T5)",
        6: "Lavish Cloth (T6)",
        7: "Opulent Cloth (T7)",
        8: "Baroque Cloth (T8)",
    },
    "wood": {
        2: "Birch Planks (T2)",
        3: "Chestnut Planks (T3)",
        4: "Pine Planks (T4)",
        5: "Cedar Planks (T5)",
        6: "Bloodoak Planks (T6)",
        7: "Ashenbark Planks (T7)",
        8: "Whitewood Planks (T8)",
    },
}

_WIKI_BASE = "https://www.albiononline2d.com/en/item/id"

_TIER_RE = re.compile(r"^T(\d+)_")


def _tier(uid: str) -> int:
    m = _TIER_RE.match(uid)
    return int(m.group(1)) if m else 0


# ── Reward pricing heuristic ───────────────────────────────────────────
# Rough median crafted-item market prices (silver, base Q1 +0).
# Keep these conservative — they're a *floor* for the bounty payout; the
# logistician can always tip extra. Numbers are deliberately easy to
# tune.
_BASE_PRICE_GEAR: dict[int, int] = {
    4: 5_000,
    5: 20_000,
    6: 80_000,
    7: 300_000,
    8: 1_200_000,
}
_BASE_PRICE_CONSUMABLE: dict[int, int] = {
    2: 200, 3: 500, 4: 1_500, 5: 4_000,
    6: 10_000, 7: 25_000, 8: 60_000,
}
_BASE_PRICE_MOUNT: dict[int, int] = {
    3: 30_000, 4: 80_000, 5: 250_000,
    6: 700_000, 7: 2_500_000, 8: 6_000_000,
}
# Multipliers vs +0 base. Roughly matches market enchantment premiums.
_ENCHANT_MULT: dict[int, float] = {0: 1.0, 1: 3.0, 2: 8.0, 3: 20.0, 4: 50.0}
# Quality premium (1=normal, 5=masterpiece).
_QUALITY_MULT: dict[int, float] = {1: 1.0, 2: 1.15, 3: 1.4, 4: 2.0, 5: 3.5}

# Flat fee per item type claimed (covers learning-point burn / time).
DEFAULT_SERVICE_FEE = 100_000


def _round_silver(n: float) -> int:
    """Round to a nice presentable silver amount."""
    n = max(0, int(n))
    if n >= 1_000_000:
        return round(n / 50_000) * 50_000
    if n >= 100_000:
        return round(n / 5_000) * 5_000
    if n >= 10_000:
        return round(n / 1_000) * 1_000
    if n >= 1_000:
        return round(n / 100) * 100
    return n


def estimate_unit_price(
    item_id: str, *, quality: int = 1, enchant: int = 0,
) -> int:
    """Return a rough silver value for ONE unit of ``item_id`` at the
    given quality+enchant. Used to compute bounty rewards."""
    uid = (item_id or "").strip().upper()
    tier = _tier(uid)
    if tier == 0:
        return 0

    is_consumable = ("_POTION" in uid) or ("_MEAL" in uid)
    is_mount = "_MOUNT_" in uid

    if is_mount:
        base = _BASE_PRICE_MOUNT.get(tier, 0)
    elif is_consumable:
        base = _BASE_PRICE_CONSUMABLE.get(tier, 0)
    else:
        base = _BASE_PRICE_GEAR.get(tier, 0)

    if base == 0:
        return 0

    e = max(0, min(4, int(enchant or 0)))
    q = max(1, min(5, int(quality or 1)))
    price = base * _ENCHANT_MULT.get(e, 1.0) * _QUALITY_MULT.get(q, 1.0)
    # Consumables and mounts don't enchant or scale by quality the same
    # way — use the flat base.
    if is_consumable or is_mount:
        price = base
    return _round_silver(price)


def estimate_line_reward(
    item_id: str, *, count: int = 1, quality: int = 1, enchant: int = 0,
    service_fee: int = DEFAULT_SERVICE_FEE,
) -> tuple[int, int, int]:
    """Return ``(unit_price, total_items_silver, service_fee)`` for a
    shopping line. Total payout = ``count * unit_price + service_fee``."""
    unit = estimate_unit_price(item_id, quality=quality, enchant=enchant)
    items_total = unit * max(1, int(count))
    return unit, _round_silver(items_total), int(service_fee)


def _enchant_note(enchant: int) -> str:
    if enchant <= 0:
        return ""
    runes = {1: "Runes", 2: "Souls", 3: "Relics", 4: "Avalonian Energy"}.get(
        enchant, "Enchantment ingredients"
    )
    # Rough enchant cost = 1 unit of runes per craft unit needed.
    return f"\n• Plus enchantment ingredient: **{runes}** (×{enchant})"


def _refined(family: str, tier: int) -> str:
    return _REFINED.get(family, {}).get(tier, f"T{tier} {family}")


def estimate_materials(
    item_id: str, *, count: int = 1, enchant: int = 0,
) -> tuple[str, str]:
    """Return ``(estimate_text, wiki_url)``.

    ``estimate_text`` is a multi-line markdown string suitable for an
    embed field. The wiki URL points at albiononline2d.com which always
    has the authoritative recipe.
    """
    uid = (item_id or "").strip().upper()
    tier = _tier(uid)
    wiki = f"{_WIKI_BASE}/{uid}"
    n = max(1, int(count))

    if tier == 0:
        return ("Unknown tier — open the wiki link for the exact recipe.", wiki)

    # ── Armor pieces ────────────────────────────────────────────────────
    armor_match = re.match(
        r"^T\d+_(HEAD|ARMOR|SHOES)_(PLATE|LEATHER|CLOTH)", uid,
    )
    if armor_match:
        slot, fam = armor_match.group(1), armor_match.group(2).lower()
        per_unit = {"HEAD": 8, "ARMOR": 16, "SHOES": 8}[slot]
        family = {"plate": "metal", "leather": "leather",
                  "cloth": "cloth"}[fam]
        mat = _refined(family, tier)
        total = per_unit * n
        return (
            f"• **{total}× {mat}** ({per_unit}× per piece)"
            + _enchant_note(enchant),
            wiki,
        )

    # ── Weapons ────────────────────────────────────────────────────────
    if re.match(r"^T\d+_(2H|MAIN|OFF)_", uid):
        if uid.startswith(f"T{tier}_2H_"):
            per_unit, slot = 24, "2H weapon"
        elif uid.startswith(f"T{tier}_MAIN_"):
            per_unit, slot = 16, "1H weapon"
        else:
            per_unit, slot = 12, "Off-hand"
        # Pick mat family from sub-token.
        if any(k in uid for k in ("STAFF", "BOW", "CROSSBOW", "SPEAR",
                                  "NATURESTAFF", "QUARTERSTAFF")):
            family = "wood"
        elif any(k in uid for k in ("DAGGER", "SWORD", "AXE", "HAMMER",
                                    "MACE", "KNUCKLES", "POLEHAMMER")):
            family = "metal"
        elif "SHIELD" in uid or "TORCH" in uid or "BOOK" in uid:
            family = "metal"  # close enough; wiki has truth
        else:
            family = "metal"
        mat = _refined(family, tier)
        total = per_unit * n
        return (
            f"• **{total}× {mat}** ({per_unit}× per {slot})"
            + _enchant_note(enchant),
            wiki,
        )

    # ── Cape ────────────────────────────────────────────────────────────
    if "CAPE" in uid:
        per_unit = 8
        mat = _refined("cloth", tier)
        total = per_unit * n
        return (
            f"• **{total}× {mat}** ({per_unit}× per cape)"
            + _enchant_note(enchant),
            wiki,
        )

    # ── Bag ─────────────────────────────────────────────────────────────
    if "_BAG" in uid:
        cloth = _refined("cloth", tier)
        leather = _refined("leather", tier)
        return (
            f"• **{8 * n}× {cloth}**\n• **{8 * n}× {leather}**"
            + _enchant_note(enchant),
            wiki,
        )

    # ── Consumables / mounts / fallthrough ─────────────────────────────
    if "_POTION" in uid:
        return (
            "Alchemy recipe — see the wiki for exact herbs and "
            "potion ingredients.", wiki,
        )
    if "_MEAL" in uid:
        return (
            "Cooking recipe — see the wiki for fish/meat/vegetable "
            "components.", wiki,
        )
    if "_MOUNT_" in uid:
        return (
            "Saddler recipe — combine a baby pet with a saddle of the "
            "correct tier. See wiki for exact mats.", wiki,
        )

    return ("Open the wiki link for the exact recipe.", wiki)
