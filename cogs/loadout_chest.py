"""Loadout chest / quartermaster cog.

The chest is the guild's gear stockpile. Quartermaster (or any officer)
runs ``/chest add`` after a crafting run, ``/chest remove`` when handing
sets out, and ``/chest missing`` before an event to see what still needs
to be crafted/gathered to fill a comp.

Stock is keyed by (item_id, quality). Enchant defaults to 0 in v1 — most
guilds keep loadouts at +0 to keep regear costs low. We can layer
enchant tracking on later without a migration since the column exists.

Approved regear requests submitted through the death-flow auto-decrement
the chest (see ``cogs/regear.py``).
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from cogs._autocomplete import make_item_id_autocomplete
from debug import info_log
from utils import error_embed, info_embed, is_officer, success_embed


def _comp_name_autocomplete():
    """Autocomplete comp names by querying ``list_comps``."""

    async def _cb(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        db = getattr(interaction.client, "db", None)
        if db is None:
            return []
        try:
            rows = db.list_comps()
        except Exception:
            return []
        q = (current or "").lower()
        out: list[app_commands.Choice[str]] = []
        for r in rows:
            name = str(r.get("name") or "")
            if not name:
                continue
            if q and q not in name.lower():
                continue
            ctype = r.get("content_type") or "—"
            out.append(app_commands.Choice(
                name=f"{name} · {ctype}"[:100], value=name[:100],
            ))
            if len(out) >= 25:
                break
        return out

    return _cb


_ac_item_id = make_item_id_autocomplete()
_ac_comp = _comp_name_autocomplete()


_UNIQUE_NAME_HINT = ("T0_", "T1_", "T2_", "T3_", "T4_", "T5_",
                     "T6_", "T7_", "T8_", "UNIQUE_")


# Albion tier-name prefixes the items table prepends to localized names.
# Strip these for friendlier display when the underlying item is the same
# across tiers (e.g. "Elder's Lymhurst Cape" → "Lymhurst Cape").
_TIER_PREFIXES = (
    "Beginner's ", "Novice's ", "Journeyman's ", "Adept's ",
    "Expert's ", "Master's ", "Grandmaster's ", "Elder's ",
)
_SLOT_WORDS = {
    "2H": "Two-Handed",
    "ARMOR": "Armor",
    "BAG": "Bag",
    "CAPEITEM": "Cape",
    "CLOTH": "Cloth",
    "FW": "Faction",
    "HEAD": "Head",
    "LEATHER": "Leather",
    "MEAL": "Food",
    "MOUNT": "Mount",
    "OFF": "Off-Hand",
    "PLATE": "Plate",
    "POTION": "Potion",
    "SHOES": "Shoes",
}


def _strip_tier_prefix(name: str) -> str:
    """Return ``name`` with any leading Albion tier-quality prefix removed."""
    for p in _TIER_PREFIXES:
        if name.startswith(p):
            return name[len(p):]
    return name


def _fallback_item_name(item_id: str) -> str:
    """Turn an Albion unique_name into a readable fallback label.

    The item dictionary should be the source of truth, but officer alerts
    should still be readable if the dictionary is stale or missing a row.
    """
    raw = str(item_id or "").strip().upper()
    if not raw:
        return "Unknown item"
    enchant = ""
    if "@" in raw:
        raw, _, suffix = raw.partition("@")
        enchant = f" +{suffix}" if suffix.isdigit() else f" @{suffix}"
    parts = [p for p in raw.split("_") if p]
    tier = parts.pop(0) if parts and parts[0].startswith("T") else ""
    words = [_SLOT_WORDS.get(part, part.title()) for part in parts]
    label = " ".join(([tier] if tier else []) + words).strip()
    return f"{label or item_id}{enchant}".strip()


def resolve_item_display_name(db, item_id: str) -> str:
    """Resolve an Albion unique_name to its localized name for display."""
    iid = str(item_id or "").strip().upper()
    if not iid:
        return "Unknown item"
    try:
        db.cursor.execute(
            "SELECT name FROM items WHERE unique_name = ?",
            (iid,),
        )
        row = db.cursor.fetchone()
        if row and row["name"]:
            return str(row["name"])
    except Exception:
        pass
    return _fallback_item_name(iid)


def format_chest_item_display(
    db,
    item_id: str,
    *,
    quality: int = 1,
    enchant: int = 0,
) -> str:
    """Return a player-facing chest item label.

    Example: ``Expert's Cleric Robe`` or ``Master's Armored Horse +1``.
    Quality is only shown for non-normal quality because most chest rows are
    Q1 and officers mainly need the actual item name at a glance.
    """
    name = resolve_item_display_name(db, item_id)
    bits: list[str] = []
    ench = int(enchant or 0)
    qual = int(quality or 1)
    if ench > 0:
        bits.append(f"+{ench}")
    if qual > 1:
        bits.append(f"Q{qual}")
    suffix = f" ({' '.join(bits)})" if bits else ""
    return f"{name}{suffix}"


def _resolve_item_input(db, raw: str) -> tuple[str | None, str | None]:
    """Resolve user-supplied text to a canonical ``(unique_name, name)``.

    Accepts either an item id picked from the autocomplete dropdown
    (e.g. ``T6_2H_HAMMER``) or a free-text localized name a user typed
    in (e.g. ``Master's Hammer``). Returns ``(None, None)`` if nothing
    matches so the caller can show an error.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    # If it looks like a unique_name already, verify and return it.
    candidate = raw.upper()
    if candidate.startswith(_UNIQUE_NAME_HINT):
        try:
            db.cursor.execute(
                "SELECT unique_name, name FROM items WHERE unique_name = ?",
                (candidate,),
            )
            row = db.cursor.fetchone()
            if row:
                return row["unique_name"], row["name"]
        except Exception:
            pass
    # Otherwise treat as a localized name: exact case-insensitive match first.
    try:
        db.cursor.execute(
            "SELECT unique_name, name FROM items "
            "WHERE name = ? COLLATE NOCASE LIMIT 1",
            (raw,),
        )
        row = db.cursor.fetchone()
        if row:
            return row["unique_name"], row["name"]
    except Exception:
        pass
    # Fall back to substring search and take the top hit.
    try:
        hits = db.search_items(raw, limit=1)
    except Exception:
        hits = []
    if hits:
        return hits[0]["unique_name"], hits[0]["name"]
    return None, None


def _quality_label(q: int) -> str:
    return {1: "Normal", 2: "Good", 3: "Outstanding",
            4: "Excellent", 5: "Masterpiece"}.get(int(q or 1), str(q))


def _stock_line(row: dict) -> str:
    name = row.get("item_name") or row.get("item_id")
    iid = row.get("item_id")
    qual = int(row.get("quality") or 1)
    enc = int(row.get("enchant") or 0)
    count = int(row.get("count") or 0)
    qbit = f" Q{qual}" if qual > 1 else ""
    ebit = f" +{enc}" if enc > 0 else ""
    return f"**{count}×** {name} (`{iid}`{qbit}{ebit})"


def aggregate_stock_by_base_name(rows: list[dict]) -> list[dict]:
    """Collapse per-tier/quality/enchant chest rows into one entry per base
    item. Returns a list of ``{name, count, variants, category}`` dicts
    sorted by total count desc, then by name. Pure helper for testability.

    'Base item' is the localized name with the Albion tier prefix
    (Adept's, Master's, etc.) stripped — so all five tier rows of
    'Lymhurst Cape', across all qualities and enchantments, collapse into
    a single line that reads ``43× Lymhurst Cape (5 variants)``.
    """
    buckets: dict[tuple[str, str], dict] = {}
    for r in rows:
        raw = str(r.get("item_name") or r.get("item_id") or "")
        base = _strip_tier_prefix(raw) or raw
        cat = str(r.get("category") or "Other")
        key = (cat, base.lower())
        b = buckets.get(key)
        if b is None:
            b = {"name": base, "category": cat, "count": 0, "variants": 0}
            buckets[key] = b
        b["count"] += int(r.get("count") or 0)
        b["variants"] += 1
    # Sort: highest count first, then name asc.
    return sorted(
        buckets.values(),
        key=lambda x: (-int(x["count"]), x["name"].lower()),
    )


class LoadoutChestCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot

    chest = app_commands.Group(
        name="chest",
        description="Loadout chest — guild gear stockpile.",
    )

    # ── /chest add ──────────────────────────────────────────────────────────
    @chest.command(name="add", description="Add gear to the loadout chest.")
    @app_commands.describe(
        item="Item name — start typing and pick from the dropdown.",
        count="How many to add.",
        quality="Quality (1=Normal, 5=Masterpiece). Defaults to 1.",
        enchant="Enchantment level (0-4). Defaults to 0.",
        note="Optional note for the audit log.",
    )
    @app_commands.autocomplete(item=_ac_item_id)
    async def chest_add(
        self, interaction: discord.Interaction,
        item: str, count: app_commands.Range[int, 1, 10000],
        quality: app_commands.Range[int, 1, 5] = 1,
        enchant: app_commands.Range[int, 0, 4] = 0,
        note: str | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Only officers can edit the chest."),
                ephemeral=True,
            )
            return
        unique_name, display_name = _resolve_item_input(self.bot.db, item)
        if not unique_name:
            await interaction.response.send_message(
                embed=error_embed(
                    "Item not found",
                    f"No item matches `{item}`. Pick one from the "
                    "autocomplete dropdown.",
                ),
                ephemeral=True,
            )
            return
        new_count = self.bot.db.chest_adjust(
            unique_name, int(count), quality=int(quality), enchant=int(enchant),
            reason=note or "manual /chest add",
            actor_id=str(interaction.user.id),
        )
        if new_count < 0:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't update chest."),
                ephemeral=True,
            )
            return
        info_log(
            f"{interaction.user} +{count} {unique_name} → chest={new_count}"
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Chest updated",
                f"Added **{count}× {display_name}** "
                f"(Q{quality}{f' +{enchant}' if enchant else ''}). "
                f"On hand: **{new_count}**.",
            ),
            ephemeral=True,
        )

    # ── /chest remove ───────────────────────────────────────────────────────
    @chest.command(name="remove", description="Remove gear from the chest.")
    @app_commands.describe(
        item="Item name — start typing and pick from the dropdown.",
        count="How many to remove.",
        quality="Quality (defaults to 1).",
        enchant="Enchant (defaults to 0).",
        note="Optional note for the audit log.",
    )
    @app_commands.autocomplete(item=_ac_item_id)
    async def chest_remove(
        self, interaction: discord.Interaction,
        item: str, count: app_commands.Range[int, 1, 10000],
        quality: app_commands.Range[int, 1, 5] = 1,
        enchant: app_commands.Range[int, 0, 4] = 0,
        note: str | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Only officers can edit the chest."),
                ephemeral=True,
            )
            return
        unique_name, display_name = _resolve_item_input(self.bot.db, item)
        if not unique_name:
            await interaction.response.send_message(
                embed=error_embed(
                    "Item not found",
                    f"No item matches `{item}`. Pick one from the "
                    "autocomplete dropdown.",
                ),
                ephemeral=True,
            )
            return
        before = self.bot.db.chest_get(
            unique_name, quality=int(quality), enchant=int(enchant),
        )
        new_count = self.bot.db.chest_adjust(
            unique_name, -int(count),
            quality=int(quality), enchant=int(enchant),
            reason=note or "manual /chest remove",
            actor_id=str(interaction.user.id),
        )
        if new_count < 0:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't update chest."),
                ephemeral=True,
            )
            return
        removed = before - new_count
        note_short = ""
        if removed < int(count):
            note_short = (
                f"\n*Only **{removed}** were on hand — chest was short."
            )
        info_log(
            f"{interaction.user} -{removed} {unique_name} → chest={new_count}"
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Chest updated",
                f"Removed **{removed}× {display_name}**. "
                f"On hand: **{new_count}**.{note_short}",
            ),
            ephemeral=True,
        )

    # ── /chest stock ────────────────────────────────────────────────────────
    @chest.command(name="stock", description="Show what's in the chest.")
    @app_commands.describe(
        search="Filter by item name or id substring.",
        detailed="Show every tier/quality/enchant variant separately.",
    )
    async def chest_stock(
        self, interaction: discord.Interaction,
        search: str | None = None,
        detailed: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # Pull a wide window so aggregation isn't starved when the chest
        # has thousands of variant rows. 1000 is well above any realistic
        # chest size.
        rows = self.bot.db.chest_list_stock(search=search, limit=1000)
        if not rows:
            await interaction.followup.send(
                embed=info_embed(
                    "Chest is empty",
                    "Add gear with `/chest add`." if not search
                    else f"No items match `{search}`.",
                ),
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="Loadout chest stock",
            colour=discord.Colour.gold(),
        )
        total = sum(int(r.get("count") or 0) for r in rows)

        if detailed:
            # Old per-variant view, grouped by category.
            groups: dict[str, list[dict]] = {}
            for r in rows:
                groups.setdefault(r.get("category") or "Other", []).append(r)
            embed.description = (
                f"**{total:,}** items across **{len(rows)}** variant rows."
            )
            for cat, items in sorted(groups.items()):
                chunk = "\n".join(_stock_line(r) for r in items[:15])
                extra = ""
                if len(items) > 15:
                    extra = f"\n…and {len(items) - 15} more."
                embed.add_field(name=cat, value=chunk + extra, inline=False)
                if len(embed.fields) >= 24:
                    break
        else:
            # Aggregated view — one line per base item, summed across all
            # tiers/qualities/enchants. This is the default because the
            # raw view is unreadable at chest sizes >50 items.
            aggregated = aggregate_stock_by_base_name(rows)
            groups_a: dict[str, list[dict]] = {}
            for a in aggregated:
                groups_a.setdefault(a["category"], []).append(a)
            embed.description = (
                f"**{total:,}** items across **{len(aggregated)}** base items "
                f"({len(rows)} variant rows)."
                "\n_Use `detailed:true` to see each tier/quality variant._"
            )
            for cat, items in sorted(groups_a.items()):
                def _line(a: dict) -> str:
                    vbit = f" ({a['variants']} variants)" if a["variants"] > 1 else ""
                    return f"**{a['count']}×** {a['name']}{vbit}"
                chunk = "\n".join(_line(a) for a in items[:15])
                extra = ""
                if len(items) > 15:
                    extra = f"\n…and {len(items) - 15} more."
                embed.add_field(name=cat, value=chunk + extra, inline=False)
                if len(embed.fields) >= 24:
                    break
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /chest missing ──────────────────────────────────────────────────────
    @chest.command(
        name="missing",
        description="What's missing to fully kit a comp from the chest.",
    )
    @app_commands.describe(comp="Comp name.")
    @app_commands.autocomplete(comp=_ac_comp)
    async def chest_missing(
        self, interaction: discord.Interaction, comp: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        row = self.bot.db.fetch_comp(comp)
        if not row:
            await interaction.followup.send(
                embed=error_embed("Comp not found",
                                  f"No comp named `{comp}`."),
                ephemeral=True,
            )
            return
        report = self.bot.db.chest_missing_for_comp(int(row["id"]))
        embed = discord.Embed(
            title=f"Chest readiness — {row['name']}",
            colour=discord.Colour.orange()
            if report["shortfall"] else discord.Colour.green(),
        )
        embed.description = (
            f"Content type: **{row.get('content_type') or '—'}**\n"
            f"Slots in comp: **{len(self.bot.db.list_comp_slots(int(row['id'])))}**\n"
            "_Stock is counted across all tiers of each item — players pick "
            "the tier that fits their IP._"
        )
        if report["shortfall"]:
            lines = []
            for r in report["shortfall"][:20]:
                tiers = r.get("tiers") or {}
                tier_str = (
                    " · " + ", ".join(
                        f"T{t}×{n}" for t, n in sorted(tiers.items())
                    )
                ) if tiers else ""
                lines.append(
                    f"⚠️ **{r['short']}× short** — "
                    f"{_strip_tier_prefix(r['item_name'])} "
                    f"(need {r['needed']}, have {r['on_hand']}{tier_str})"
                )
            embed.add_field(
                name=f"Shortfall ({len(report['shortfall'])})",
                value="\n".join(lines)[:1024],
                inline=False,
            )
        if report["ok"]:
            lines = []
            for r in report["ok"][:15]:
                tiers = r.get("tiers") or {}
                tier_str = (
                    " · " + ", ".join(
                        f"T{t}×{n}" for t, n in sorted(tiers.items())
                    )
                ) if tiers else ""
                lines.append(
                    f"✅ {_strip_tier_prefix(r['item_name'])} — "
                    f"{r['on_hand']}/{r['needed']}{tier_str}"
                )
            embed.add_field(
                name=f"Covered ({len(report['ok'])})",
                value="\n".join(lines)[:1024] or "—",
                inline=False,
            )
        if report["unresolved"]:
            lines = [
                f"❓ {r['item_name']} ({r['slot_field']})"
                for r in report["unresolved"][:10]
            ]
            embed.add_field(
                name=f"Unmatched items ({len(report['unresolved'])})",
                value=(
                    "These comp entries don't match the Albion item "
                    "dictionary — likely typos.\n"
                    + "\n".join(lines)
                )[:1024],
                inline=False,
            )
        if not (report["shortfall"] or report["ok"] or report["unresolved"]):
            embed.add_field(
                name="Empty comp",
                value="This comp has no gear fields filled in yet.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /chest log ──────────────────────────────────────────────────────────
    @chest.command(name="log", description="Recent chest adjustments.")
    async def chest_log(
        self, interaction: discord.Interaction,
    ) -> None:
        rows = self.bot.db.chest_recent_log(limit=20)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Audit log", "No chest activity yet."),
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            sign = "+" if int(r["delta"]) > 0 else ""
            actor = f"<@{r['actor_id']}>" if r.get("actor_id") else "?"
            lines.append(
                f"`{r['created_at'][:16]}` {actor} "
                f"**{sign}{int(r['delta'])}× "
                f"{r.get('item_name') or r['item_id']}** "
                f"— {r.get('reason') or '(no note)'}"
            )
        embed = discord.Embed(
            title="Recent chest adjustments",
            description="\n".join(lines)[:4000],
            colour=discord.Colour.dark_gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(LoadoutChestCog(bot))
    info_log("Initialized LoadoutChest cog.")
