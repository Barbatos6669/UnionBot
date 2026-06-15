"""Shared Albion item autocomplete helpers.

Two flavors are exposed:

* ``make_item_name_autocomplete(categories)`` — returns the **base item
  name** with the Albion tier-quality prefix stripped (``Adept's Assassin
  Hood`` → ``Assassin Hood``). Per-tier rows are collapsed so users see
  one entry per base item. Comps/loadouts then store just the base name;
  per-event ``ip_min`` gates which tier members actually have to bring.
* ``make_item_id_autocomplete(categories)`` — returns the **UniqueName**
  (e.g. ``T6_BAG``) as the value with a friendly ``Name · T6_BAG`` label.
  Used by /market commands where the item ID is the canonical key
  (markets are inherently per-tier).

Both read from the ``items`` table seeded by
``Database.seed_items_from_url()``.
"""

from __future__ import annotations

import discord
from discord import app_commands


# Albion prepends a tier-quality word to the localized name of every gear
# row (T4 = "Adept's", T5 = "Expert's", ..., T8 = "Elder's"). Stripping
# these lets us collapse all 5 tier rows of one item into a single
# autocomplete entry.
_TIER_PREFIXES = (
    "Beginner's ", "Novice's ", "Journeyman's ", "Adept's ",
    "Expert's ", "Master's ", "Grandmaster's ", "Elder's ",
)


def strip_tier_prefix(name: str) -> str:
    """Return ``name`` with any leading Albion tier-quality prefix removed."""
    s = name or ""
    for p in _TIER_PREFIXES:
        if s.startswith(p):
            return s[len(p):]
    return s


def dedupe_by_base_name(rows: list[dict], limit: int = 25) -> list[dict]:
    """Collapse per-tier item rows down to one row per base name.

    Pure helper — exposed for testing. Keeps the highest-tier row per
    base name (assuming the input is already roughly relevance-ordered)
    and preserves first-seen order so the caller's ranking still wins.
    Empty/zero-tier rows are kept as-is since they have nothing to merge.
    """
    seen: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        raw_name = str(r.get("name") or "")
        base = strip_tier_prefix(raw_name)
        key = base.lower()
        if not key:
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = {**r, "name": base}
            order.append(key)
        else:
            # Same base item, different tier — keep whichever is higher.
            if int(r.get("tier") or 0) > int(existing.get("tier") or 0):
                seen[key] = {**r, "name": base}
        if len(order) >= limit and all(k in seen for k in order):
            # We have ``limit`` distinct bases; further rows can only
            # upgrade tier of existing entries, never add new ones.
            # Keep scanning the small tail for that case.
            pass
    return [seen[k] for k in order[:limit]]


def _search(interaction: discord.Interaction, query: str, categories: list[str] | None, *, limit: int = 25):
    db = getattr(interaction.client, "db", None)
    if db is None:
        return []
    try:
        return db.search_items(query or "", categories=categories, limit=limit)
    except Exception:
        return []


def make_item_name_autocomplete(categories: list[str] | None = None):
    """Autocomplete whose value is the **base item name** (free text).

    Per-tier rows are deduped so users only see one entry per item
    (Dual Swords, Assassin Hood, …) rather than five quality-prefixed
    variants. Tier selection is handled at event time via slot ``ip_min``.
    """

    async def _cb(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        # Fetch a wider window so dedupe doesn't starve us of choices
        # when an item has 5 tier rows clustered at the top.
        rows = _search(interaction, current, categories, limit=100)
        deduped = dedupe_by_base_name(rows, limit=25)
        return [
            app_commands.Choice(
                name=str(r["name"])[:100],
                value=str(r["name"])[:100],
            )
            for r in deduped
        ]

    return _cb


def make_item_id_autocomplete(categories: list[str] | None = None):
    """Autocomplete whose value is the Albion UniqueName (item id).

    Labels are kept simple: ``T6 Dual Swords`` (with the Albion tier-
    quality prefix stripped from the localized name). The UniqueName
    is no longer crammed into the label — it's still the choice value
    so the bot resolves correctly on submit, but users don't need to
    see ``T6_2H_DUALSWORD`` next to every entry.
    """

    async def _cb(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        rows = _search(interaction, current, categories)
        out: list[app_commands.Choice[str]] = []
        for r in rows:
            tier = r.get("tier") or 0
            tier_bit = f"T{tier} " if tier else ""
            base = strip_tier_prefix(str(r["name"]))
            label = f"{tier_bit}{base}"
            out.append(app_commands.Choice(
                name=label[:100],
                value=str(r["unique_name"])[:100],
            ))
        return out

    return _cb
