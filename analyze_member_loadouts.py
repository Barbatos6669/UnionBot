"""Build a shopping list from recent member deaths.

Pulls the most recent N deaths per linked member from the Albion killboard,
aggregates the gear they were wearing, and prints a shopping list grouped
by slot showing the most-used items, average quality, and what's already
in the loadout chest.

Usage:
    python analyze_member_loadouts.py [--deaths 5] [--top 10]
                                      [--shopping] [--tsv out.tsv]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from statistics import mean
from typing import Any

import albion_api
from sql_database import Database


def _split_enchant(item_id: str) -> tuple[str, int]:
    raw = str(item_id or "").strip()
    if "@" in raw:
        base, _, suffix = raw.partition("@")
        try:
            return base, int(suffix)
        except ValueError:
            return base, 0
    return raw, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deaths", type=int, default=5,
                        help="Recent deaths per player to fetch (max 50).")
    parser.add_argument("--top", type=int, default=15,
                        help="Top N items per slot to show.")
    parser.add_argument("--sleep", type=float, default=0.15,
                        help="Seconds between API calls (politeness).")
    parser.add_argument("--shopping", action="store_true",
                        help="Also print a flat shopping list including "
                             "current chest stock and 'buy N more' suggestion.")
    parser.add_argument("--tsv", type=str, default=None,
                        help="Write the shopping list to this TSV file.")
    args = parser.parse_args()

    deaths_per_player = max(1, min(50, args.deaths))

    db = Database("data/database.db")
    db.connect()

    db.cursor.execute(
        "SELECT discord_id, username, albion_name, albion_player_id "
        "FROM user_profiles "
        "WHERE albion_player_id IS NOT NULL AND albion_player_id != ''"
    )
    members = [dict(r) for r in db.cursor.fetchall()]
    print(f"Scanning {len(members)} linked members "
          f"({deaths_per_player} deaths each)...")

    # Aggregation: (slot, base_id, enchant) -> {count, qualities[], players{}}
    Key = tuple[str, str, int]
    agg: dict[Key, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "qualities": [], "players": set()}
    )
    scanned = 0
    no_deaths = 0
    api_errors = 0

    for m in members:
        pid = m["albion_player_id"]
        try:
            events = albion_api.get_player_deaths(
                pid, limit=deaths_per_player,
            )
        except Exception as exc:  # noqa: BLE001
            api_errors += 1
            print(f"  ! {m.get('albion_name') or pid}: {exc}")
            time.sleep(args.sleep)
            continue
        if not events:
            no_deaths += 1
            time.sleep(args.sleep)
            continue
        scanned += 1
        for ev in events:
            summary = albion_api.format_death_event(ev)
            for it in summary.get("gear_items") or []:
                raw_id = it.get("item_id")
                if not raw_id:
                    continue
                base_id, ench = _split_enchant(raw_id)
                slot = str(it.get("slot") or "Unknown")
                qual = int(it.get("quality") or 1)
                cnt = int(it.get("count") or 1)
                key: Key = (slot, base_id, ench)
                agg[key]["count"] += cnt
                agg[key]["qualities"].append(qual)
                agg[key]["players"].add(pid)
        time.sleep(args.sleep)

    print(f"\nScanned {scanned} members with deaths "
          f"({no_deaths} had none, {api_errors} errored).\n")

    # Resolve unique_name -> display name via items table.
    unique_ids = {k[1] for k in agg.keys()}
    names: dict[str, str] = {}
    if unique_ids:
        placeholders = ",".join("?" * len(unique_ids))
        db.cursor.execute(
            f"SELECT unique_name, name FROM items "
            f"WHERE unique_name IN ({placeholders})",
            tuple(unique_ids),
        )
        for r in db.cursor.fetchall():
            names[r["unique_name"]] = r["name"]

    def _name(uid: str) -> str:
        return names.get(uid, uid)

    # Group by slot and rank.
    by_slot: dict[str, list[tuple[Key, dict]]] = defaultdict(list)
    for k, v in agg.items():
        by_slot[k[0]].append((k, v))
    for slot in by_slot:
        by_slot[slot].sort(key=lambda kv: kv[1]["count"], reverse=True)

    slot_order = [
        "MainHand", "OffHand", "Head", "Armor", "Shoes",
        "Cape", "Bag", "Mount", "Potion", "Food",
    ]
    ordered = [s for s in slot_order if s in by_slot]
    ordered += sorted(s for s in by_slot if s not in slot_order)

    for slot in ordered:
        rows = by_slot[slot][: args.top]
        print(f"── {slot} ──")
        for (s, base_id, ench), v in rows:
            avg_q = mean(v["qualities"]) if v["qualities"] else 0.0
            ench_s = f" +{ench}" if ench else ""
            print(f"  {v['count']:4d}×  {_name(base_id)}{ench_s}"
                  f"   (avgQ {avg_q:.1f}, {len(v['players'])} players)"
                  f"   [{base_id}]")
        print()

    if args.shopping or args.tsv:
        flat: list[dict[str, Any]] = []
        for (slot, base_id, ench), v in agg.items():
            avg_q = round(mean(v["qualities"])) if v["qualities"] else 1
            avg_q = max(1, min(5, int(avg_q)))
            on_hand = int(
                db.chest_get(base_id, quality=avg_q, enchant=ench) or 0
            )
            need = max(0, v["count"] - on_hand)
            flat.append({
                "slot": slot,
                "item_id": base_id,
                "name": _name(base_id),
                "enchant": ench,
                "avg_quality": avg_q,
                "uses": v["count"],
                "players": len(v["players"]),
                "on_hand": on_hand,
                "buy_more": need,
            })
        flat.sort(key=lambda r: (r["buy_more"], r["uses"]), reverse=True)

        if args.shopping:
            print("══ SHOPPING LIST (buy_more > 0, sorted by deficit) ══")
            print(f"{'Buy':>5}  {'Have':>5}  {'Used':>5}  Q  E  Item")
            for r in flat:
                if r["buy_more"] <= 0:
                    continue
                print(f"{r['buy_more']:5d}  {r['on_hand']:5d}  "
                      f"{r['uses']:5d}  {r['avg_quality']}  {r['enchant']}  "
                      f"{r['name']}  [{r['item_id']}]")
            print()

        if args.tsv:
            with open(args.tsv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=["slot", "item_id", "name", "enchant",
                                "avg_quality", "uses", "players",
                                "on_hand", "buy_more"],
                    delimiter="\t",
                )
                w.writeheader()
                for r in flat:
                    w.writerow(r)
            print(f"Wrote shopping TSV → {args.tsv}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
