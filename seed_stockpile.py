"""One-off seeder: imports rows from data/stockpile_seed.tsv into the loadout chest.

TSV columns: Date, Player, Item, Enchantment, Quality, Amount
Skips header rows and any row whose item name can't be resolved.
"""
import csv
import sys
from sql_database import Database


def main(path: str = "data/stockpile_seed.tsv") -> int:
    db = Database("data/database.db")
    db.connect()

    seeded = 0
    total_units = 0
    unmatched: list[str] = []
    errors: list[str] = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t", quotechar='"')
        for lineno, row in enumerate(reader, start=1):
            if len(row) < 6:
                continue
            date_s, player, item_name, ench_s, qual_s, amt_s = row[:6]
            if date_s.strip().lower() == "date":
                continue  # skip header rows
            try:
                enchant = int(ench_s)
                quality = int(qual_s)
                amount = int(amt_s)
            except ValueError:
                errors.append(f"line {lineno}: bad int in {row!r}")
                continue
            if amount <= 0:
                continue

            # exact case-insensitive match on items.name
            db.cursor.execute(
                "SELECT unique_name, name FROM items "
                "WHERE name = ? COLLATE NOCASE LIMIT 1",
                (item_name,),
            )
            r = db.cursor.fetchone()
            if r is None:
                # fall back to search_items
                hits = db.search_items(item_name, limit=1)
                if not hits:
                    unmatched.append(item_name)
                    continue
                unique_name = hits[0]["unique_name"]
            else:
                unique_name = r["unique_name"]

            new_count = db.chest_adjust(
                item_id=unique_name,
                delta=amount,
                quality=quality,
                enchant=enchant,
                reason="initial seed",
                actor_id=player,
            )
            if new_count < 0:
                errors.append(f"line {lineno}: chest_adjust failed for {item_name}")
                continue
            seeded += 1
            total_units += amount

    print(f"Seeded {seeded} rows ({total_units} units total).")
    if unmatched:
        uniq = sorted(set(unmatched))
        print(f"\nUnmatched items ({len(uniq)} distinct):")
        for n in uniq:
            print(f"  - {n}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
