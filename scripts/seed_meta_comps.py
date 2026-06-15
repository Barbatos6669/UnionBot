"""
One-shot seeder for curated meta comps.

Run with:  python scripts/seed_meta_comps.py
Idempotent: skips comps whose name already exists.

All gear strings are plain item names — the chest-readiness resolver
normalises them at query time (category-restricted fuzzy match), so
small wording variations are fine. Edit slots in-bot with /comp edit-slot.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sql_database import Database


# ── Curated meta comps ──────────────────────────────────────────────────────
# Each entry is a tuple of (comp dict, [slot dicts]).
# Slot fields: role, build_type ('tank'|'dps'|'healer'|'support'),
#   weapon, is_two_handed (0/1), offhand, head, chest, shoes, cape,
#   mount, food, potion, ip_min, required (0/1), notes, swaps.
# Anything you leave out is fine — the DB tolerates NULLs everywhere
# except role.
META_COMPS: list[tuple[dict, list[dict]]] = [
    # ── Hellgate 5v5 (open-source 2026 meta: 1 tank, 1 healer, 3 dps) ──────
    (
        {
            "name":         "Hellgate 5v5 — Standard",
            "content_type": "Hellgate 5v5",
            "description": (
                "Bread-and-butter 5v5 HG comp. Tank engages and locks, "
                "Hallowfall sustains the team, three DPS rotate burst "
                "windows. Swap Fire Staff for Cursed Staff in low-MMR "
                "lobbies."
            ),
        },
        [
            {"role": "Tank", "build_type": "tank",
             "weapon": "Camlann Mace", "is_two_handed": 1,
             "head": "Helmet of Valor", "chest": "Guardian Armor",
             "shoes": "Graveguard Boots", "cape": "Stalker Cape",
             "food": "Beef Stew", "potion": "Resistance Potion"},
            {"role": "Healer", "build_type": "healer",
             "weapon": "Hallowfall", "is_two_handed": 0,
             "offhand": "Eye of Secrets",
             "head": "Cleric Cowl", "chest": "Cultist Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "Melee DPS", "build_type": "dps",
             "weapon": "Realmbreaker", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Mercenary Shoes", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Ranged DPS", "build_type": "dps",
             "weapon": "Fire Staff", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Bridgewatch Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion",
             "swaps": "Weapon: Cursed Staff | Brimstone Staff"},
            {"role": "Ranged DPS", "build_type": "dps",
             "weapon": "Longbow", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Hunter Jacket",
             "shoes": "Mercenary Shoes", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion",
             "swaps": "Weapon: Whispering Bow | Bow of Badon"},
        ],
    ),

    # ── Hellgate 2v2 (corrupted/HG duo — 1 bruiser + 1 sustain) ────────────
    (
        {
            "name":         "Hellgate 2v2 — Carving + Holy",
            "content_type": "Hellgate 2v2",
            "description": (
                "Standard 2v2: Carving Sword applies sustain pressure, "
                "Holy Staff keeps the duo topped and dispels enemy CC. "
                "Heavy invest on jackets for cooldown reduction."
            ),
        },
        [
            {"role": "Bruiser", "build_type": "dps",
             "weapon": "Carving Sword", "is_two_handed": 0,
             "offhand": "Caitiff Shield",
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Mercenary Shoes", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Healing Potion"},
            {"role": "Healer", "build_type": "healer",
             "weapon": "Redemption Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Cleric Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion",
             "swaps": "Weapon: Holy Staff | Fallen Staff"},
        ],
    ),

    # ── Ganking 5-man (mounted, divebombs) ─────────────────────────────────
    (
        {
            "name":         "Ganking 5-man — Dive",
            "content_type": "Ganking",
            "description": (
                "Mounted gank squad — Spirit Hunter or Dawnsong drops the "
                "bomb after Stalker initiates with stuns; Realmbreaker "
                "follows up for the kill; Locus heals through the dive."
            ),
        },
        [
            {"role": "Initiator", "build_type": "dps",
             "weapon": "Stalker Bow", "is_two_handed": 1,
             "head": "Assassin Hood", "chest": "Assassin Jacket",
             "shoes": "Assassin Shoes", "cape": "Thetford Cape",
             "mount": "Swiftclaw",
             "food": "Roast Pork", "potion": "Invisibility Potion"},
            {"role": "Bomber",    "build_type": "dps",
             "weapon": "Dawnsong", "is_two_handed": 1,
             "head": "Assassin Hood", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Thetford Cape",
             "mount": "Swiftclaw",
             "food": "Roast Pork", "potion": "Invisibility Potion"},
            {"role": "Finisher",  "build_type": "dps",
             "weapon": "Realmbreaker", "is_two_handed": 1,
             "head": "Assassin Hood", "chest": "Mercenary Jacket",
             "shoes": "Assassin Shoes", "cape": "Thetford Cape",
             "mount": "Swiftclaw",
             "food": "Roast Pork", "potion": "Healing Potion"},
            {"role": "Pin",       "build_type": "support",
             "weapon": "Heavy Crossbow", "is_two_handed": 1,
             "head": "Hunter Hood", "chest": "Hunter Jacket",
             "shoes": "Hunter Shoes", "cape": "Thetford Cape",
             "mount": "Swiftclaw",
             "food": "Roast Pork", "potion": "Invisibility Potion"},
            {"role": "Healer",    "build_type": "healer",
             "weapon": "Locus Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Cleric Robe",
             "shoes": "Cleric Sandals", "cape": "Thetford Cape",
             "mount": "Swiftclaw",
             "food": "Pork Omelette", "potion": "Invisibility Potion"},
        ],
    ),

    # ── Static dungeon 5-man fame farm ─────────────────────────────────────
    (
        {
            "name":         "Static Dungeon — Standard 5m",
            "content_type": "Static Dungeon",
            "description": (
                "Reliable solo-instance / static-dungeon farm. AoE-cleave "
                "weapons + Wild Staff sustain + Heavy Mace for the boss "
                "stuns. Mage Robe everywhere for cooldown reduction."
            ),
        },
        [
            {"role": "Tank",      "build_type": "tank",
             "weapon": "Heavy Mace", "is_two_handed": 0,
             "offhand": "Taproot",
             "head": "Knight Helmet", "chest": "Knight Armor",
             "shoes": "Knight Boots", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Healer",    "build_type": "healer",
             "weapon": "Wild Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Druid Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "AoE DPS",   "build_type": "dps",
             "weapon": "Permafrost Prism", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "AoE DPS",   "build_type": "dps",
             "weapon": "Great Fire Staff", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Single-target DPS", "build_type": "dps",
             "weapon": "Bloodletter", "is_two_handed": 0,
             "offhand": "Muisak",
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Mercenary Shoes", "cape": "Thetford Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
        ],
    ),

    # ── Avalonian dungeon (group fame, 7-man) ──────────────────────────────
    (
        {
            "name":         "Avalonian — Group Fame 7m",
            "content_type": "Avalonian",
            "description": (
                "Avalonian-road / open-elite fame farm. Two off-tanks "
                "share aggro and chains, two heals for sustain, one big "
                "AoE bomb, two single-target / cleave DPS. Lymhurst "
                "capes everywhere for sustained DPS uptime."
            ),
        },
        [
            {"role": "Main Tank", "build_type": "tank",
             "weapon": "Grovekeeper", "is_two_handed": 1,
             "head": "Guardian Helmet", "chest": "Guardian Armor",
             "shoes": "Guardian Boots", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Off-Tank",  "build_type": "tank",
             "weapon": "Mace", "is_two_handed": 0,
             "offhand": "Taproot",
             "head": "Knight Helmet", "chest": "Knight Armor",
             "shoes": "Knight Boots", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Main Healer", "build_type": "healer",
             "weapon": "Great Holy Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Cleric Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "Off-Heal",  "build_type": "healer",
             "weapon": "Nature Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Druid Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "Bomb",      "build_type": "dps",
             "weapon": "Permafrost Prism", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Melee DPS", "build_type": "dps",
             "weapon": "Carving Sword", "is_two_handed": 0,
             "offhand": "Muisak",
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Mercenary Shoes", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Ranged DPS", "build_type": "dps",
             "weapon": "Cursed Staff", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
        ],
    ),

    # ── Crystal League 10v10 (basic meta) ──────────────────────────────────
    (
        {
            "name":         "Crystal League — Standard 10v10",
            "content_type": "Crystal League",
            "description": (
                "Foundation 10v10: 2 tanks set the line, 2 healers behind, "
                "burst stack (Permafrost + Dawnsong + Realmbreaker) for "
                "the engage, ranged poke (Longbow) for between fights, "
                "Stalker + Locus support for picks and rezzes."
            ),
        },
        [
            {"role": "Main Tank", "build_type": "tank",
             "weapon": "Grovekeeper", "is_two_handed": 1,
             "head": "Guardian Helmet", "chest": "Guardian Armor",
             "shoes": "Graveguard Boots", "cape": "Stalker Cape",
             "food": "Beef Stew", "potion": "Resistance Potion"},
            {"role": "Off-Tank",  "build_type": "tank",
             "weapon": "Heavy Mace", "is_two_handed": 0,
             "offhand": "Taproot",
             "head": "Knight Helmet", "chest": "Knight Armor",
             "shoes": "Graveguard Boots", "cape": "Stalker Cape",
             "food": "Beef Stew", "potion": "Resistance Potion"},
            {"role": "Main Healer", "build_type": "healer",
             "weapon": "Great Holy Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Cleric Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "Off-Heal",  "build_type": "healer",
             "weapon": "Fallen Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Cleric Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "Bomb DPS",  "build_type": "dps",
             "weapon": "Permafrost Prism", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Bomb DPS",  "build_type": "dps",
             "weapon": "Dawnsong", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Melee DPS", "build_type": "dps",
             "weapon": "Realmbreaker", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Mercenary Shoes", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Poke DPS",  "build_type": "dps",
             "weapon": "Longbow", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Hunter Jacket",
             "shoes": "Mercenary Shoes", "cape": "Martlock Cape",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Support",   "build_type": "support",
             "weapon": "Stalker Bow", "is_two_handed": 1,
             "head": "Assassin Hood", "chest": "Mercenary Jacket",
             "shoes": "Assassin Shoes", "cape": "Thetford Cape",
             "food": "Roast Pork", "potion": "Invisibility Potion"},
            {"role": "Support",   "build_type": "support",
             "weapon": "Locus Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Druid Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "food": "Pork Omelette", "potion": "Healing Potion"},
        ],
    ),

    # ── Faction Warfare roam (small-scale, mounted) ────────────────────────
    (
        {
            "name":         "Faction Warfare — Roam 7m",
            "content_type": "Faction Warfare",
            "description": (
                "Small-scale faction roam comp — engage with Spirit Hunter, "
                "lock with Heavy Mace, sustain with two healers, kite "
                "out with Whispering Bow when fights go bad."
            ),
        },
        [
            {"role": "Tank",       "build_type": "tank",
             "weapon": "Heavy Mace", "is_two_handed": 0,
             "offhand": "Taproot",
             "head": "Knight Helmet", "chest": "Guardian Armor",
             "shoes": "Graveguard Boots", "cape": "Stalker Cape",
             "mount": "Armored Horse",
             "food": "Beef Stew", "potion": "Resistance Potion"},
            {"role": "Initiator",  "build_type": "dps",
             "weapon": "Spirit Hunter", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Graveguard Boots", "cape": "Martlock Cape",
             "mount": "Armored Horse",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Bruiser",    "build_type": "dps",
             "weapon": "Realmbreaker", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Mercenary Jacket",
             "shoes": "Mercenary Shoes", "cape": "Martlock Cape",
             "mount": "Armored Horse",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Ranged DPS", "build_type": "dps",
             "weapon": "Whispering Bow", "is_two_handed": 1,
             "head": "Mercenary Hood", "chest": "Hunter Jacket",
             "shoes": "Mercenary Shoes", "cape": "Thetford Cape",
             "mount": "Armored Horse",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Bomb DPS",   "build_type": "dps",
             "weapon": "Permafrost Prism", "is_two_handed": 1,
             "head": "Scholar Cowl", "chest": "Mage Robe",
             "shoes": "Scholar Sandals", "cape": "Lymhurst Cape",
             "mount": "Armored Horse",
             "food": "Beef Stew", "potion": "Gigantify Potion"},
            {"role": "Main Healer", "build_type": "healer",
             "weapon": "Great Holy Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Cleric Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "mount": "Armored Horse",
             "food": "Pork Omelette", "potion": "Healing Potion"},
            {"role": "Off-Heal",   "build_type": "healer",
             "weapon": "Wild Staff", "is_two_handed": 1,
             "head": "Cleric Cowl", "chest": "Druid Robe",
             "shoes": "Cleric Sandals", "cape": "Lymhurst Cape",
             "mount": "Armored Horse",
             "food": "Pork Omelette", "potion": "Healing Potion"},
        ],
    ),

    # ── Mists 1v1 solo (Carving Sword baseline) ────────────────────────────
    (
        {
            "name":         "Mists Solo — Carving Sword",
            "content_type": "Mists",
            "description": (
                "Strong all-rounder for Mists solo. Carving applies "
                "Lifesteal stacks, Cultist Robe gives the burst window, "
                "Soldier Boots tankiness. Sub T8 Caitiff Shield in "
                "T8 lobbies for armor."
            ),
        },
        [
            {"role": "Solo Bruiser", "build_type": "dps",
             "weapon": "Carving Sword", "is_two_handed": 0,
             "offhand": "Caitiff Shield",
             "head": "Mercenary Hood", "chest": "Cultist Robe",
             "shoes": "Soldier Boots", "cape": "Lymhurst Cape",
             "food": "Beef Stew", "potion": "Healing Potion",
             "swaps": "Offhand: Eye of Secrets (more burst); Chest: Mercenary Jacket (more sustain)"},
        ],
    ),
]


def main() -> None:
    db = Database("data/database.db")
    db.connect()

    summary: list[str] = []
    for comp_def, slot_defs in META_COMPS:
        name = comp_def["name"]
        if db.fetch_comp(name):
            summary.append(f"  ~ skipped (exists): {name}")
            continue
        new_id = db.create_comp(
            name=name,
            content_type=comp_def["content_type"],
            description=comp_def["description"],
            created_by="system",
        )
        if not new_id:
            summary.append(f"  ! create_comp failed: {name}")
            continue
        added = 0
        for slot in slot_defs:
            row = {"required": 1, "ip_min": 0, **slot}
            row.setdefault("is_two_handed", 0)
            if db.add_comp_slot(new_id, row):
                added += 1
        summary.append(
            f"  + #{new_id:>2}  [{comp_def['content_type']:16s}] "
            f"{name}  ({added}/{len(slot_defs)} slots)"
        )

    print("Seeded curated meta comps:")
    for line in summary:
        print(line)


if __name__ == "__main__":
    main()
