"""Comp builder cog — define and view team composition templates.

A "comp" is a named build template made of one or more slots. Officers
build comps for the content the guild runs (ZvZ, Hellgate 5v5, static
dungeons, etc.). Each slot describes one player position: role, weapon,
armor pieces, IP minimum, and whether the slot is required or flex.

Assignment of real players to slots is Phase 2 — this Phase 1 cog just
covers creating and viewing the templates.

All gear fields are free text in v1. We may swap to item-ID autocomplete
later once we know which comps the guild actually uses.
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from cogs._autocomplete import make_item_name_autocomplete, strip_tier_prefix
from cogs._typing import Bot
from debug import info_log
from utils import error_embed, info_embed, is_officer, success_embed

CONTENT_TYPES = [
    "ZvZ", "Hellgate 2v2", "Hellgate 5v5", "Crystal League",
    "Static Dungeon", "Avalonian", "Faction Warfare", "World Boss",
    "Ganking", "Mists", "Other",
]

BUILD_TYPES = ["tank", "healer", "dps", "support"]


def _two_handed_keywords() -> tuple[str, ...]:
    """Substrings (lowercase) that indicate a weapon is two-handed and locks
    the offhand slot. Not exhaustive — officers can also flip the flag
    manually in /comp edit-slot."""
    return (
        "great", "halberd", "claymore", "carving", "realmbreaker",
        "bedrock", "grovekeeper", "incubus", "longbow", "warbow",
        "wailing", "whispering", "deathgivers", "permafrost",
        "great fire", "great frost", "great curse", "great holy",
        "great nature", "great arcane", "blazing", "infernal",
        "wildfire", "brimstone", "lifecurse", "demonfang",
        "rampant", "occult", "redemption", "blight",
        "great axe", "great hammer", "polehammer", "tombhammer",
        "camlann", "great sword", "kingmaker", "clarent",
    )


def _guess_two_handed(weapon: str) -> bool:
    w = (weapon or "").lower()
    return any(k in w for k in _two_handed_keywords())


def _fmt_yesno(v: int | None) -> str:
    return "Yes" if int(v or 0) else "No"


def _fmt_ip(ip: int | None) -> str:
    n = int(ip or 0)
    return f"{n}+ IP" if n > 0 else "any"


def _clean_gear_value(v: str | None) -> str | None:
    """Normalize a comp-slot gear field to a single concrete item name.

    Officers historically typed things like ``Knight Armor / Demon`` or
    ``Cleric Robe (cleanse)`` directly into the gear field. Alternatives
    belong in the slot's swaps column (or the comp description); build
    variants are just commentary. On write we keep only the first
    ``/``-separated piece, drop ``(...)`` comments, and strip the Albion
    tier-quality prefix so the DB stores one clean item name per slot.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    # Take only the first alternative when officers wrote "A / B".
    if "/" in s:
        first = s.split("/", 1)[0].strip()
        if first:
            s = first
    # Drop parenthetical comments like "(cleanse)".
    import re as _re
    s = _re.sub(r"\([^)]*\)", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    if not s:
        return None
    return strip_tier_prefix(s)


_ac_weapon  = make_item_name_autocomplete(["2H", "MAIN"])
_ac_offhand = make_item_name_autocomplete(["OFF"])
_ac_head    = make_item_name_autocomplete(["HEAD"])
_ac_chest   = make_item_name_autocomplete(["ARMOR"])
_ac_shoes   = make_item_name_autocomplete(["SHOES"])
_ac_cape    = make_item_name_autocomplete(["CAPE"])
_ac_mount   = make_item_name_autocomplete(["MOUNT"])
_ac_food    = make_item_name_autocomplete(["MEAL"])
_ac_potion  = make_item_name_autocomplete(["POTION"])


def _slot_summary_line(slot: dict) -> str:
    """One-line summary used in /comp view. Pulls out the most important
    fields and leaves the full detail for the slot-detail subcommand.

    Tier-quality prefixes (Adept's, Master's, ...) are stripped on
    display so old slots saved before the autocomplete dedupe still read
    cleanly. New slots are already stored without the prefix.
    """
    def _g(field: str) -> str | None:
        v = slot.get(field)
        return strip_tier_prefix(str(v)) if v else None

    parts: list[str] = []
    w = _g("weapon")
    if w:
        parts.append(w)
    armor_bits = [_g("head"), _g("chest"), _g("shoes")]
    armor = " / ".join(b for b in armor_bits if b)
    if armor:
        parts.append(armor)
    cape = _g("cape")
    if cape:
        parts.append(f"cape {cape}")
    parts.append(_fmt_ip(slot.get("ip_min")))
    flex = "" if int(slot.get("required") or 1) else " *(flex)*"
    return (
        f"**{slot['slot_order']}. {slot['role']}** — "
        + " · ".join(parts) + flex
    )


def _chunk_lines_for_field(lines: list[str], limit: int = 1024) -> list[str]:
    """Pack ``lines`` into successive newline-joined chunks, each at most
    ``limit`` chars. Very long individual lines are split rather than
    truncated so comp views do not silently drop gear or role text.
    """
    chunks: list[str] = []
    buf: list[str] = []
    used = 0
    for line in lines:
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for ln in pieces:
            add = len(ln) + (1 if buf else 0)  # +1 for the joining newline
            if used + add > limit and buf:
                chunks.append("\n".join(buf))
                buf = [ln]
                used = len(ln)
            else:
                buf.append(ln)
                used += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _build_comp_embeds(comp: dict, slots: list[dict]) -> list[discord.Embed]:
    """Render a full comp grouped by build_type, paging instead of truncating.

    Honors Discord's real embed limits:
      * 1024 chars per field — long sections (e.g. a ZvZ DPS group with
        20+ slots) are split across multiple ``(cont.)`` fields rather
        than silently chopped.
      * 25 fields per embed and ~6000 total chars — when one embed fills,
        another embed is created.
    """
    title = comp["name"]
    if comp.get("content_type"):
        title = f"{comp['name']}  ·  {comp['content_type']}"
    EMBED_BUDGET = 5500
    FIELD_CAP = 25

    embeds: list[discord.Embed] = []
    used_chars: list[int] = []

    def _new_embed() -> discord.Embed:
        page = len(embeds) + 1
        suffix = "" if page == 1 else " (continued)"
        e = discord.Embed(
            title=f"⚔️ {title}{suffix}",
            color=discord.Color.dark_red(),
        )
        embeds.append(e)
        used_chars.append(len(e.title or ""))
        return e

    embed = _new_embed()

    def _add_logical_field(name: str, value: str, *, inline: bool = False) -> None:
        nonlocal embed
        chunks = _chunk_lines_for_field(str(value or "").splitlines() or ["—"])
        for idx, chunk in enumerate(chunks, start=1):
            label = name if len(chunks) == 1 else f"{name} ({idx}/{len(chunks)})"
            field_cost = len(label) + len(chunk)
            if (
                len(embed.fields) >= FIELD_CAP
                or used_chars[-1] + field_cost > EMBED_BUDGET
            ):
                embed = _new_embed()
            embed.add_field(name=label[:256], value=chunk, inline=inline)
            used_chars[-1] += field_cost

    desc = (comp.get("description") or "").strip()
    if desc:
        _add_logical_field("Overview", desc, inline=False)

    # Group by build_type, keep tank → healer → dps → support → (unset) order.
    groups: dict[str, list[dict]] = {bt: [] for bt in BUILD_TYPES}
    groups["other"] = []
    for s in slots:
        bt = (s.get("build_type") or "").strip().lower()
        groups.setdefault(bt if bt in BUILD_TYPES else "other", []).append(s)

    section_order = ["tank", "healer", "dps", "support", "other"]
    section_titles = {
        "tank": "🛡️ Tanks", "healer": "✨ Healers",
        "dps": "⚔️ DPS", "support": "🔧 Support", "other": "❔ Unassigned",
    }
    for key in section_order:
        rows = groups.get(key) or []
        if not rows:
            continue
        lines = [_slot_summary_line(s) for s in rows]
        chunks = _chunk_lines_for_field(lines, limit=1024)
        for idx, chunk in enumerate(chunks):
            label = (
                f"{section_titles[key]} ({len(rows)})"
                if idx == 0
                else f"{section_titles[key]} (cont.)"
            )
            _add_logical_field(label, chunk, inline=False)

    required_count = sum(1 for s in slots if int(s.get("required") or 1))
    for idx, e in enumerate(embeds, start=1):
        footer = (
            f"Comp #{comp['id']} • {len(slots)} slots total • "
            f"{required_count} required"
        )
        if len(embeds) > 1:
            footer += f" • page {idx}/{len(embeds)}"
        e.set_footer(text=footer)
    return embeds


def _build_comp_embed(comp: dict, slots: list[dict]) -> discord.Embed:
    """Compatibility wrapper for callers that only need the first page."""
    return _build_comp_embeds(comp, slots)[0]


class CreateCompModal(discord.ui.Modal, title="Create comp"):
    def __init__(self, content_type: str) -> None:
        super().__init__(timeout=None)
        self._content_type = content_type
        self.comp_name = discord.ui.TextInput(
            label="Comp name (unique)",
            placeholder="e.g. ZvZ Standard, HG5v5 A, Static Mage",
            max_length=80, required=True,
        )
        self.description = discord.ui.TextInput(
            label="Description (optional)",
            style=discord.TextStyle.paragraph,
            max_length=1000, required=False,
        )
        self.add_item(self.comp_name)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: Bot = interaction.client  # type: ignore[assignment]
        new_id = bot.db.create_comp(
            name=str(self.comp_name.value),
            content_type=self._content_type or None,
            description=str(self.description.value or "") or None,
            created_by=str(interaction.user.id),
        )
        if not new_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Couldn't create comp",
                    "A comp with that name already exists — pick a unique name.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Comp created",
                f"**{self.comp_name.value}** is now in the library "
                f"(id `{new_id}`). Add slots with "
                f"`/comp add-slot comp:{self.comp_name.value} role:<...>`.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} created comp #{new_id} '{self.comp_name.value}' "
            f"(content_type={self._content_type})."
        )


class Comp(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        # Seed the Albion item dictionary in the background the first time
        # the cog loads. ~1500 rows, single HTTP GET, then cached forever.

        async def _seed() -> None:
            try:
                if self.bot.db.count_items() > 0:
                    return
                inserted = await asyncio.to_thread(self.bot.db.seed_items_from_url)
                if inserted:
                    info_log(f"Comp cog: seeded {inserted} Albion items for autocomplete.")
            except Exception as e:  # pragma: no cover — best-effort
                info_log(f"Comp cog: item seed skipped ({e}).")

        async def _normalize_existing_slot_names() -> None:
            # One-off backfill: collapse legacy gear values to a single
            # concrete item name. Strips tier-quality prefixes, drops
            # ``(...)`` comments, and keeps only the first piece of any
            # ``A / B`` alternative (those should live in swaps or the
            # description, not the gear field). Idempotent — re-cleaning
            # an already-clean value is a no-op.
            try:
                db = self.bot.db
                gear_cols = ("weapon", "offhand", "head", "chest",
                             "shoes", "cape", "mount", "food", "potion")
                cleaned = 0
                for comp in db.list_comps(include_archived=True):
                    for slot in db.list_comp_slots(int(comp["id"])):
                        updates: dict = {}
                        for col in gear_cols:
                            val = slot.get(col)
                            if not val:
                                continue
                            new_val = _clean_gear_value(str(val))
                            if new_val != val:
                                updates[col] = new_val
                        if updates:
                            db.update_comp_slot(int(slot["id"]), updates)
                            cleaned += 1
                if cleaned:
                    info_log(
                        f"Comp cog: normalized {cleaned} existing slot "
                        f"row(s) (tier prefix / alternatives / comments)."
                    )
            except Exception as e:  # pragma: no cover — best-effort
                info_log(f"Comp cog: slot-name normalize skipped ({e}).")

        asyncio.create_task(_seed())
        asyncio.create_task(_normalize_existing_slot_names())

    comp = app_commands.Group(name="comp", description="Manage team composition templates.")

    # ── Autocomplete: comp names (live DB query, case-insensitive substring) ──
    async def _comp_name_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            rows = self.bot.db.list_comps(include_archived=True)
        except Exception:
            return []
        q = (current or "").lower().strip()
        matches = [
            c for c in rows
            if not q or q in str(c.get("name", "")).lower()
            or q in str(c.get("content_type") or "").lower()
        ]
        # Discord caps at 25 choices. Mark archived comps with 📦.
        return [
            app_commands.Choice(
                name=(
                    f"{'📦 ' if int(c.get('archived') or 0) else ''}"
                    f"{c['name']} · {c.get('content_type') or 'Other'}"
                )[:100],
                value=str(c["name"]),
            )
            for c in matches[:25]
        ]

    # ── Autocomplete: Albion items, filtered per-slot ───────────────────────
    # See module-level ``_make_item_autocomplete`` below — autocomplete
    # callbacks have to be referenced at class-definition time, so we keep
    # them outside the class and read the DB off of ``interaction.client``.

    # Officer guard helper.
    @staticmethod
    async def _require_officer(interaction: discord.Interaction) -> bool:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This command is restricted to officers."),
                ephemeral=True,
            )
            return False
        return True

    # ── /comp create ────────────────────────────────────────────────────────
    @comp.command(name="create", description="Create a new comp template.")
    @app_commands.describe(content_type="What content this comp is for")
    @app_commands.choices(content_type=[
        app_commands.Choice(name=ct, value=ct) for ct in CONTENT_TYPES
    ])
    async def create(
        self, interaction: discord.Interaction,
        content_type: app_commands.Choice[str],
    ) -> None:
        if not await self._require_officer(interaction):
            return
        await interaction.response.send_modal(CreateCompModal(content_type.value))

    # ── /comp list ──────────────────────────────────────────────────────────
    @comp.command(name="list", description="List all comp templates.")
    @app_commands.describe(include_archived="Also show archived comps")
    async def list_(
        self, interaction: discord.Interaction,
        include_archived: bool = False,
    ) -> None:
        comps = self.bot.db.list_comps(include_archived=include_archived)
        if not comps:
            await interaction.response.send_message(
                embed=info_embed(
                    "No comps yet",
                    "Officers can create one with `/comp create`.",
                ),
                ephemeral=True,
            )
            return
        # Group by content_type.
        groups: dict[str, list[dict]] = {}
        for c in comps:
            groups.setdefault(c.get("content_type") or "Other", []).append(c)
        embed = info_embed(
            f"⚔️ Comp Library — {len(comps)} comps",
            "Use `/comp view name:<x>` to see a comp's slots.",
        )
        for content_type in sorted(groups):
            lines = []
            for c in groups[content_type]:
                tag = " 📦" if int(c.get("archived") or 0) else ""
                lines.append(f"• **{c['name']}**{tag}  (id `{c['id']}`)")
            embed.add_field(
                name=content_type, value="\n".join(lines)[:1024], inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /comp view ──────────────────────────────────────────────────────────
    @comp.command(name="view", description="Show a comp's full slot list.")
    @app_commands.describe(name="Comp name (case-insensitive)")
    @app_commands.autocomplete(name=_comp_name_autocomplete)
    async def view(self, interaction: discord.Interaction, name: str) -> None:
        comp = self.bot.db.fetch_comp(name)
        if not comp:
            await interaction.response.send_message(
                embed=error_embed(
                    "Not found",
                    f"No comp named `{name}`. Try `/comp list`.",
                ),
                ephemeral=True,
            )
            return
        slots = self.bot.db.list_comp_slots(int(comp["id"]))
        embeds = _build_comp_embeds(comp, slots)
        await interaction.response.send_message(embeds=embeds[:10])
        for start in range(10, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[start:start + 10])

    # ── /comp add-slot ──────────────────────────────────────────────────────
    @comp.command(name="add-slot", description="Add a slot to a comp.")
    @app_commands.describe(
        comp="Comp name",
        role="Role label (e.g. Main Tank, Off Heal, Ranged DPS)",
        build_type="Broad build category for grouping",
        weapon="Weapon name (free text, e.g. 'Heavy Mace' or 'Holy Staff')",
        ip_min="Minimum IP for this slot (0 = any)",
        offhand="Off-hand item (leave blank for two-handed weapons)",
        head="Head armor (e.g. 'Knight Helmet')",
        chest="Chest armor",
        shoes="Boots",
        cape="Cape",
        mount="Mount (e.g. 'Armored Horse', 'any T4+ riding')",
        food="Food (e.g. 'Pork Omelette')",
        potion="Potion (e.g. 'Healing Potion')",
        required="Required slot? Defaults to True (flex slots = false)",
        notes="Extra notes",
        swaps="Officer-approved alternates (e.g. 'Weapon: Bedrock Mace | Camlann Mace; Head: Soldier T8')",
    )
    @app_commands.choices(build_type=[
        app_commands.Choice(name=bt.title(), value=bt) for bt in BUILD_TYPES
    ])
    @app_commands.autocomplete(
        comp=_comp_name_autocomplete,
        weapon=_ac_weapon, offhand=_ac_offhand,
        head=_ac_head, chest=_ac_chest, shoes=_ac_shoes,
        cape=_ac_cape, mount=_ac_mount,
        food=_ac_food, potion=_ac_potion,
    )
    async def add_slot(
        self, interaction: discord.Interaction,
        comp: str, role: str,
        build_type: app_commands.Choice[str] | None = None,
        weapon: str | None = None,
        ip_min: app_commands.Range[int, 0, 2000] = 0,
        offhand: str | None = None,
        head: str | None = None,
        chest: str | None = None,
        shoes: str | None = None,
        cape: str | None = None,
        mount: str | None = None,
        food: str | None = None,
        potion: str | None = None,
        required: bool = True,
        notes: str | None = None,
        swaps: str | None = None,
    ) -> None:
        if not await self._require_officer(interaction):
            return
        comp_row = self.bot.db.fetch_comp(comp)
        if not comp_row:
            await interaction.response.send_message(
                embed=error_embed("Comp not found", f"No comp named `{comp}`."),
                ephemeral=True,
            )
            return
        two_handed = _guess_two_handed(weapon or "")
        if two_handed and offhand:
            # Don't fight the officer — store the offhand but flag it.
            two_handed = False

        # Normalize gear fields to a single concrete item name. Strips
        # tier-quality prefixes, drops ``(...)`` comments, and keeps only
        # the first piece of any ``A / B`` alternative — those belong in
        # the swaps column or the comp description, not the gear field.
        _norm = _clean_gear_value

        fields = {
            "role":         role.strip(),
            "build_type":   build_type.value if build_type else None,
            "weapon":       _norm(weapon),
            "is_two_handed": 1 if two_handed else 0,
            "offhand":      None if two_handed else _norm(offhand),
            "head":         _norm(head),
            "chest":        _norm(chest),
            "shoes":        _norm(shoes),
            "cape":         _norm(cape),
            "mount":        _norm(mount),
            "food":         _norm(food),
            "potion":       _norm(potion),
            "ip_min":       int(ip_min or 0),
            "required":     1 if required else 0,
            "notes":        notes,
            "swaps":        swaps,
        }
        slot_id = self.bot.db.add_comp_slot(int(comp_row["id"]), fields)
        if not slot_id:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't insert the slot."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Slot added",
                f"Slot `{slot_id}` (**{role}**) added to **{comp_row['name']}**. "
                + ("Detected 2H weapon — offhand cleared. "
                   if two_handed else "")
                + "Use `/comp view` to see the full comp.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} added slot #{slot_id} ({role}) "
            f"to comp #{comp_row['id']} ({comp_row['name']})."
        )

    # ── /comp remove-slot ───────────────────────────────────────────────────
    @comp.command(name="remove-slot", description="Delete a slot from a comp.")
    @app_commands.describe(slot_id="Slot id (visible in /comp view footer or after add-slot)")
    async def remove_slot(
        self, interaction: discord.Interaction,
        slot_id: int,
    ) -> None:
        if not await self._require_officer(interaction):
            return
        slot = self.bot.db.fetch_comp_slot(int(slot_id))
        if not slot:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No slot with id `{slot_id}`."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.remove_comp_slot(int(slot_id))
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Failed", "Couldn't delete the slot."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed("Slot removed", f"Slot `{slot_id}` ({slot.get('role')}) deleted."),
            ephemeral=True,
        )
        info_log(f"{interaction.user} removed slot #{slot_id} ({slot.get('role')}).")

    # ── /comp set-swaps ─────────────────────────────────────────────────────
    @comp.command(
        name="set-swaps",
        description="Set the approved gear-swap alternates for a comp slot.",
    )
    @app_commands.describe(
        slot_id="Slot id (visible in /comp view)",
        swaps=(
            "Free-text list of alternates that still fulfil the role. "
            "Empty string clears."
        ),
    )
    async def set_swaps(
        self, interaction: discord.Interaction,
        slot_id: int, swaps: str,
    ) -> None:
        if not await self._require_officer(interaction):
            return
        slot = self.bot.db.fetch_comp_slot(int(slot_id))
        if not slot:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No slot with id `{slot_id}`."),
                ephemeral=True,
            )
            return
        cleaned = swaps.strip() or None
        ok = self.bot.db.update_comp_slot(int(slot_id), {"swaps": cleaned})
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Failed", "Couldn't update the slot."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Swaps updated" if cleaned else "Swaps cleared",
                f"Slot `{slot_id}` ({slot.get('role')}) — "
                + (f"swaps now:\n```\n{cleaned}\n```"
                   if cleaned else "no alternates set."),
            ),
            ephemeral=True,
        )

    # ── /comp duplicate ─────────────────────────────────────────────────────
    @comp.command(name="duplicate", description="Copy a comp to tweak.")
    @app_commands.describe(name="Source comp name", new_name="Name for the copy")
    @app_commands.autocomplete(name=_comp_name_autocomplete)
    async def duplicate(
        self, interaction: discord.Interaction, name: str, new_name: str,
    ) -> None:
        if not await self._require_officer(interaction):
            return
        src = self.bot.db.fetch_comp(name)
        if not src:
            await interaction.response.send_message(
                embed=error_embed("Source not found", f"No comp named `{name}`."),
                ephemeral=True,
            )
            return
        new_id = self.bot.db.duplicate_comp(
            int(src["id"]), new_name.strip(), str(interaction.user.id),
        )
        if not new_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Couldn't duplicate",
                    "Maybe the new name is already taken? Try another.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Duplicated",
                f"**{src['name']}** → **{new_name}** (id `{new_id}`).",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} duplicated comp #{src['id']} → #{new_id} "
            f"({new_name})."
        )

    # ── /comp archive ───────────────────────────────────────────────────────
    @comp.command(name="archive", description="Hide a comp from /comp list (reversible).")
    @app_commands.describe(name="Comp name", restore="Set true to un-archive")
    @app_commands.autocomplete(name=_comp_name_autocomplete)
    async def archive(
        self, interaction: discord.Interaction,
        name: str, restore: bool = False,
    ) -> None:
        if not await self._require_officer(interaction):
            return
        comp_row = self.bot.db.fetch_comp(name)
        if not comp_row:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No comp named `{name}`."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.archive_comp(int(comp_row["id"]), archived=not restore)
        verb = "restored" if restore else "archived"
        if ok:
            await interaction.response.send_message(
                embed=success_embed(f"Comp {verb}", f"**{comp_row['name']}** is now {verb}."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} {verb} comp #{comp_row['id']}.")
        else:
            await interaction.response.send_message(
                embed=error_embed("No change", "Couldn't update the comp."),
                ephemeral=True,
            )

    # ── /comp delete ────────────────────────────────────────────────────────
    @comp.command(name="delete", description="Permanently delete a comp (officer).")
    @app_commands.describe(name="Comp name", confirm="Type the comp name again to confirm")
    @app_commands.autocomplete(name=_comp_name_autocomplete)
    async def delete(
        self, interaction: discord.Interaction, name: str, confirm: str,
    ) -> None:
        if not await self._require_officer(interaction):
            return
        if name.strip().lower() != confirm.strip().lower():
            await interaction.response.send_message(
                embed=error_embed(
                    "Confirmation mismatch",
                    "`name` and `confirm` must match. Nothing was deleted.",
                ),
                ephemeral=True,
            )
            return
        comp_row = self.bot.db.fetch_comp(name)
        if not comp_row:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No comp named `{name}`."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.delete_comp(int(comp_row["id"]))
        if ok:
            await interaction.response.send_message(
                embed=success_embed(
                    "Deleted",
                    f"**{comp_row['name']}** and all its slots are gone.",
                ),
                ephemeral=True,
            )
            info_log(f"{interaction.user} DELETED comp #{comp_row['id']} ({comp_row['name']}).")
        else:
            await interaction.response.send_message(
                embed=error_embed("Failed", "Couldn't delete the comp."),
                ephemeral=True,
            )

    # ── /comp refresh-items ─────────────────────────────────────────────────
    @comp.command(
        name="refresh-items",
        description="Re-download the Albion item dictionary used for autocomplete (officer).",
    )
    async def refresh_items(self, interaction: discord.Interaction) -> None:
        if not await self._require_officer(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await asyncio.to_thread(
                self.bot.db.seed_items_from_url, force=True,
            )
        except Exception as e:
            await interaction.followup.send(
                embed=error_embed("Refresh failed", str(e)), ephemeral=True,
            )
            return
        if not count:
            await interaction.followup.send(
                embed=error_embed(
                    "Refresh failed",
                    "Download or parse returned no items. Check bot logs.",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "Items refreshed",
                f"Loaded **{count}** Albion items into the autocomplete index.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} refreshed item dictionary ({count} rows).")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Comp(bot))
