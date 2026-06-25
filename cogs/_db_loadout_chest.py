"""Database mixin for loadout chest / quartermaster inventory."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

import debug


class LoadoutChestDatabaseMixin:
    # ──────────────────────────────────────────────────────────────────────
    # Loadout chest / quartermaster inventory.
    #
    # Tracks gear stock the guild keeps on hand for member loadouts. Stock
    # is keyed by (item_id, quality, enchant) so a T6 plate armor @ Q3 .2
    # is a different row than a T6 plate armor @ Q1 .0. ``loadout_chest_log``
    # is an append-only audit trail of every add/remove for accountability.
    # ──────────────────────────────────────────────────────────────────────
    def initialize_loadout_chest_tables(self) -> None:
        self.execute('''
            CREATE TABLE IF NOT EXISTS loadout_chest (
                item_id     TEXT NOT NULL,
                quality     INTEGER NOT NULL DEFAULT 1,
                enchant     INTEGER NOT NULL DEFAULT 0,
                count       INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                PRIMARY KEY (item_id, quality, enchant)
            )
        ''')
        self.execute('''
            CREATE TABLE IF NOT EXISTS loadout_chest_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     TEXT NOT NULL,
                quality     INTEGER NOT NULL DEFAULT 1,
                enchant     INTEGER NOT NULL DEFAULT 0,
                delta       INTEGER NOT NULL,
                reason      TEXT,
                actor_id    TEXT,
                ref_type    TEXT,
                ref_id      TEXT,
                created_at  TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            )
        ''')
        self.execute(
            'CREATE INDEX IF NOT EXISTS ix_chest_log_item '
            'ON loadout_chest_log(item_id, created_at DESC)'
        )
        debug.info_log("Initialized loadout_chest tables.")

    def chest_adjust(
        self, item_id: str, delta: int, *,
        quality: int = 1, enchant: int = 0,
        reason: str | None = None, actor_id: str | None = None,
        ref_type: str | None = None, ref_id: str | None = None,
    ) -> int:
        """Add (delta>0) or remove (delta<0) stock. Floors at 0 — overdrawing
        clamps to whatever was on hand. Returns the new on-hand count, or
        -1 on error."""
        item_id = (item_id or "").strip().upper()
        if not item_id or delta == 0:
            return -1
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT count FROM loadout_chest "
                "WHERE item_id = ? AND quality = ? AND enchant = ?",
                (item_id, int(quality), int(enchant)),
            )
            row = self.cursor.fetchone()
            current = int(row["count"]) if row else 0
            new_count = max(0, current + int(delta))
            applied_delta = new_count - current
            if row is None:
                self.cursor.execute(
                    "INSERT INTO loadout_chest "
                    "(item_id, quality, enchant, count, updated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (item_id, int(quality), int(enchant), new_count),
                )
            else:
                self.cursor.execute(
                    "UPDATE loadout_chest SET count = ?, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE item_id = ? AND quality = ? AND enchant = ?",
                    (new_count, item_id, int(quality), int(enchant)),
                )
            if applied_delta != 0:
                self.cursor.execute(
                    "INSERT INTO loadout_chest_log "
                    "(item_id, quality, enchant, delta, reason, "
                    " actor_id, ref_type, ref_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (item_id, int(quality), int(enchant), applied_delta,
                     reason, actor_id, ref_type, ref_id),
                )
            self.connection.commit()
            return new_count
        except sqlite3.Error as e:
            debug.error_log(f"chest_adjust error: {e}")
            return -1

    def chest_get(
        self, item_id: str, *, quality: int = 1, enchant: int = 0,
    ) -> int:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT count FROM loadout_chest "
                "WHERE item_id = ? AND quality = ? AND enchant = ?",
                ((item_id or "").strip().upper(), int(quality), int(enchant)),
            )
            row = self.cursor.fetchone()
            return int(row["count"]) if row else 0
        except sqlite3.Error as e:
            debug.error_log(f"chest_get error: {e}")
            return 0

    def chest_list_stock(
        self, *, search: str | None = None, limit: int = 200,
    ) -> list[dict]:
        """List rows with count > 0. Optional substring search joins the
        items dictionary for human-readable names."""
        try:
            if not self.connection:
                self.connect()
            sql = (
                "SELECT c.item_id, c.quality, c.enchant, c.count, c.updated_at, "
                "       i.name AS item_name, i.category, i.tier "
                "FROM loadout_chest c "
                "LEFT JOIN items i ON i.unique_name = c.item_id "
                "WHERE c.count > 0"
            )
            params: list = []
            if search:
                q = f"%{search.strip()}%"
                sql += " AND (c.item_id LIKE ? OR i.name LIKE ? COLLATE NOCASE)"
                params.extend([q, q])
            sql += " ORDER BY i.category, c.item_id, c.quality, c.enchant LIMIT ?"
            params.append(int(limit))
            self.cursor.execute(sql, tuple(params))
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"chest_list_stock error: {e}")
            return []

    def chest_required_for_comp(self, comp_id: int) -> list[dict]:
        """Aggregate item requirements for a comp. Each slot contributes one
        per gear field (weapon/head/chest/shoes/cape/offhand/mount/food/
        potion) that is a known item_id (from the items dictionary).

        Returns one row per (item_id, slot_field) with a 'needed' count
        based on how many slots reference that item. Quality is assumed Q1
        / enchant 0 because comps don't pin those today."""
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT weapon, offhand, head, chest, shoes, cape, mount, "
                "       food, potion FROM comp_slots WHERE comp_id = ?",
                (int(comp_id),),
            )
            rows = self.cursor.fetchall()
            slot_fields = (
                "weapon", "offhand", "head", "chest", "shoes",
                "cape", "mount", "food", "potion",
            )
            counts: dict[tuple[str, str], int] = defaultdict(int)
            for r in rows:
                for f in slot_fields:
                    name = (r[f] or "").strip()
                    if not name:
                        continue
                    counts[(name, f)] += 1
            # Resolve names → item_ids via the items dictionary. Comps store
            # localized names; the chest is keyed by unique_name. Two-stage
            # match per (name, field):
            #   1. Exact name (COLLATE NOCASE)
            #   2. Fall back to ``search_items`` with the slot field
            #      appended as a hint (e.g. "lymhurst" + "cape" →
            #      "Adept's Lymhurst Cape"). Only accept if the top hit is
            #      unambiguous enough — i.e. the query tokens appear in
            #      the resolved name. Otherwise leave unresolved so the
            #      officer fixes the typo.
            # Map slot field → items table category (constrains the
            # fuzzy search so "lymhurst" can't accidentally match the
            # Lymhurst Sword for a cape slot).
            field_categories = {
                "weapon":  ["2H", "MAIN"],
                "offhand": ["OFF"],
                "head":    ["HEAD"],
                "chest":   ["ARMOR"],
                "shoes":   ["SHOES"],
                "cape":    ["CAPE"],
                "mount":   ["MOUNT"],
                "food":    ["MEAL"],
                "potion":  ["POTION"],
            }
            out: list[dict] = []
            for (name, field), needed in counts.items():
                # Officers sometimes write a gear field as "A / B" meaning
                # "either A or B" — that belongs in the slot's swaps
                # column, but the legacy data has it in the gear field
                # itself. Try each ``/``-separated alternative in order
                # and use the first one that resolves; the rest are just
                # writeup notes from the officer.
                alternatives = [a.strip() for a in name.split("/") if a.strip()]
                if not alternatives:
                    alternatives = [name]
                resolved_any = False
                for alt_name in alternatives:
                    self.cursor.execute(
                        "SELECT unique_name, name, category "
                        "FROM items WHERE name = ? COLLATE NOCASE LIMIT 1",
                        (alt_name,),
                    )
                    irow = self.cursor.fetchone()
                    if irow:
                        out.append({
                            "item_id": irow["unique_name"],
                            "item_name": irow["name"],
                            "category": irow["category"],
                            "slot_field": field,
                            "needed": int(needed),
                        })
                        resolved_any = True
                        break
                    # Strip parenthetical comments like ``(cleanse)`` or
                    # ``(med)`` because they encode a build variant, not
                    # part of the item's name.
                    import re as _re
                    cleaned = _re.sub(r"\([^)]*\)", " ", alt_name).strip()
                    cleaned = _re.sub(r"\s+", " ", cleaned)
                    cats = field_categories.get(field)
                    try:
                        hits = self.search_items(
                            cleaned, categories=cats, limit=30,
                        )
                    except sqlite3.Error:
                        hits = []
                    # Also try a collapsed-spaces variant — handles
                    # "oath keepers" → "Oathkeepers".
                    collapsed = cleaned.replace(" ", "")
                    if collapsed and collapsed.lower() != cleaned.lower():
                        try:
                            hits.extend(self.search_items(
                                collapsed, categories=cats, limit=10,
                            ))
                        except sqlite3.Error:
                            pass
                    # Accept only hits where every token of the cleaned
                    # name appears in the hit's display name (or the hit
                    # contains the collapsed form). Among accepted hits,
                    # prefer the **highest tier** — comps that omit a
                    # tier (e.g. "Cleric Robe") almost always mean the
                    # top tier the guild is fielding for ZvZ.
                    name_tokens = [t for t in cleaned.lower().split() if t]
                    candidates = []
                    seen_uids: set[str] = set()
                    for h in hits:
                        uid = h.get("unique_name") or ""
                        if uid in seen_uids:
                            continue
                        seen_uids.add(uid)
                        hit_name = (h.get("name") or "").lower()
                        if all(t in hit_name for t in name_tokens):
                            candidates.append(h)
                        elif collapsed and collapsed.lower() in hit_name:
                            candidates.append(h)
                    accepted = None
                    if candidates:
                        accepted = max(
                            candidates,
                            key=lambda h: int(h.get("tier") or 0),
                        )
                    if accepted:
                        out.append({
                            "item_id": accepted["unique_name"],
                            "item_name": accepted["name"],
                            "category": accepted.get("category"),
                            "slot_field": field,
                            "needed": int(needed),
                            "resolved_from": alt_name,
                        })
                        resolved_any = True
                        break
                if not resolved_any:
                    # Unmatched comp entry — surface it so officers can fix.
                    out.append({
                        "item_id": None,
                        "item_name": name,
                        "category": None,
                        "slot_field": field,
                        "needed": int(needed),
                    })
            # Merge resolved entries that point at the same item — three
            # variants of Assassin Hood should collapse into one row
            # whose needed count is the sum of the three.
            merged: dict[tuple[str, str], dict] = {}
            unresolved_out: list[dict] = []
            for r in out:
                if not r["item_id"]:
                    unresolved_out.append(r)
                    continue
                key = (r["item_id"], r["slot_field"])
                if key in merged:
                    merged[key]["needed"] += int(r["needed"])
                else:
                    merged[key] = dict(r)
                    # Drop ``resolved_from`` once collapsed — multiple
                    # source strings make it ambiguous.
                    merged[key].pop("resolved_from", None)
            out = list(merged.values()) + unresolved_out
            out.sort(key=lambda r: (r["slot_field"], r["item_name"] or ""))
            return out
        except sqlite3.Error as e:
            debug.error_log(f"chest_required_for_comp error: {e}")
            return []

    def chest_family_stock(self, item_id: str) -> dict:
        """Sum chest stock across every tier/quality/enchant of an item
        family. Comp slots specify the item family (e.g. Lymhurst Cape);
        the player chooses what tier to actually run based on their IP.

        Returns ``{"total": int, "by_tier": {tier: count, ...}}``.
        ``item_id`` may carry a tier prefix (``T8_FOO``) — it gets
        stripped to derive the family key.
        """
        item_id = (item_id or "").strip().upper()
        if not item_id:
            return {"total": 0, "by_tier": {}}
        # Strip leading ``T<digit>_`` so we match all tiers of the family.
        family = re.sub(r"^T\d_", "", item_id)
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT item_id, SUM(count) AS n FROM loadout_chest "
                "GROUP BY item_id"
            )
            total = 0
            by_tier: dict[int, int] = {}
            for row in self.cursor.fetchall():
                iid = (row["item_id"] or "").upper()
                m = re.match(r"^T(\d)_(.+)$", iid)
                if m and m.group(2) == family:
                    tier = int(m.group(1))
                elif iid == family:
                    tier = 0
                else:
                    continue
                n = int(row["n"] or 0)
                total += n
                by_tier[tier] = by_tier.get(tier, 0) + n
            return {"total": total, "by_tier": by_tier}
        except sqlite3.Error as e:
            debug.error_log(f"chest_family_stock error: {e}")
            return {"total": 0, "by_tier": {}}

    def chest_missing_for_comp(self, comp_id: int) -> dict:
        """Returns {"requirements": [...], "shortfall": [...], "ok": [...]}.
        Each requirement is annotated with on_hand and short. Items not in
        the items dictionary are returned in 'unresolved'.

        Stock is counted **by item family** (across all tiers/qualities),
        because comps specify the item but each player picks the tier
        that matches their IP. Each row carries a ``tiers`` dict so the
        embed can show what's available where.
        """
        reqs = self.chest_required_for_comp(comp_id)
        shortfall: list[dict] = []
        ok: list[dict] = []
        unresolved: list[dict] = []
        for r in reqs:
            if not r["item_id"]:
                unresolved.append(r)
                continue
            stock = self.chest_family_stock(r["item_id"])
            on_hand = stock["total"]
            short = max(0, r["needed"] - on_hand)
            row = dict(r)
            row["on_hand"] = on_hand
            row["short"] = short
            row["tiers"] = stock["by_tier"]
            if short > 0:
                shortfall.append(row)
            else:
                ok.append(row)
        return {
            "requirements": reqs,
            "shortfall": shortfall,
            "ok": ok,
            "unresolved": unresolved,
        }

    def chest_recent_log(self, *, limit: int = 25) -> list[dict]:
        try:
            if not self.connection:
                self.connect()
            self.cursor.execute(
                "SELECT l.*, i.name AS item_name "
                "FROM loadout_chest_log l "
                "LEFT JOIN items i ON i.unique_name = l.item_id "
                "ORDER BY l.id DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in self.cursor.fetchall()]
        except sqlite3.Error as e:
            debug.error_log(f"chest_recent_log error: {e}")
            return []
