"""Self-assign content-role picker.

Posts a persistent panel in a configured channel that lets members opt in
to ping-roles for the content they care about (PvP, Ganking, Roads, etc.).
It also manages a second persistent panel for broader Albion weapon-tree
roles (Holy, Nature, Arcane, Mace, Sword, etc.) so shotcallers can see who
can fill comp roles without creating one role per individual weapon.

Content ping roles are not created by this cog — it reuses the existing
pingable content roles already mapped in ``guild_config`` under the LFG
system's ``lfg_role_<event_type>`` keys (see :mod:`cogs._lfg_config`).
That means a single source of truth: whatever role is set as the LFG ping
target for ZvZ is also what members can self-assign here. Weapon-tree
roles are created/repaired on demand by :mod:`cogs._weapon_roles`.

Layout:
    The panel renders one **multi-select per category** (Discord caps at 5
    selects per message). Picking N options in a
    category sets exactly those N roles for that category — i.e. roles
    you didn't keep selected in that category get removed. This makes it
    a true "category picker" rather than a one-way add button.

Persistence:
    Each select's ``custom_id`` encodes only the category key, so the
    view survives restarts via ``bot.add_view()``. Per-user current
    selections are computed live from ``Member.roles`` on each render
    (we re-send an ephemeral picker for the user on click) — Discord
    persistent views can't bake user-specific defaults into a shared
    public message.

Config keys consumed/written:
    content_roles_channel_id     — channel where the public panel lives
    content_roles_message_id     — id of the panel message (for repost)
    lfg_role_<event_type_key>    — read-only; the role IDs to offer
    weapon_roles_channel_id      — channel where the weapon panel lives
    weapon_roles_message_id      — id of the weapon panel message
    weapon_role_<tree_key>       — role IDs for weapon-tree self-assign roles

Slash commands (``/content-roles ...``, manage_guild gated):
    set-channel <channel>   — record where the panel should live
    post-panel              — (re)post the public panel message
    ensure-weapon-roles     — create missing weapon-tree roles
    post-weapon-panel       — (re)post the weapon-tree picker panel
    show-config             — debug; show resolved roles per category
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs._lfg_config import CFG_ROLE_PREFIX, EVENT_TYPES
from cogs._typing import Bot
from cogs._weapon_roles import (
    CFG_WEAPON_PANEL_CHANNEL,
    CFG_WEAPON_PANEL_MESSAGE,
    WEAPON_CATEGORIES,
    WeaponRolesPanelView,
    ensure_weapon_roles as _ensure_weapon_roles,
    resolve_weapon_roles as _resolve_weapon_roles,
    weapon_panel_embed as _weapon_panel_embed,
)
from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed

# ── Category layout ──────────────────────────────────────────────────────
# Each tuple is (category_key, display_label, list of EventType keys).
# Keep this focused on the public content-ping roles the guild actually wants
# members to self-assign. Legacy LFG event keys are aliased in _lfg_config.py.
CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("pvp", "⚔️ PvP & Fights",
     ("alliance", "pvp", "faction", "gank", "small_scale", "zvz", "hellgate",
      "crystal_arena", "duo_mists")),
    ("pve", "🛣️ Roads, Dungeons & Objectives",
     ("abyssal_depths", "roads", "group_dungeon", "static_dungeon",
      "ava_dungeon", "world_boss", "tracking")),
    ("economy", "💰 Economy & Logistics",
     ("gathering", "transport", "economy")),
)

CFG_PANEL_CHANNEL = "content_roles_channel_id"
CFG_PANEL_MESSAGE = "content_roles_message_id"

# Discord SelectOption / select-menu hard limits.
MAX_OPTIONS_PER_SELECT = 25


# ── Helpers ──────────────────────────────────────────────────────────────
def _parse_config_channel_id(raw: str | None) -> tuple[int | None, str | None]:
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "Stored channel ID is invalid."


def _resolve_category_roles(
    guild: discord.Guild, db, category_keys: tuple[str, ...],
) -> list[tuple[str, discord.Role]]:
    """Return ``[(event_type_key, role), ...]`` for the given category,
    skipping any event type that doesn't have a real role mapped or whose
    mapped role no longer exists.
    """
    out: list[tuple[str, discord.Role]] = []
    for key in category_keys:
        rid = db.get_config(CFG_ROLE_PREFIX + key)
        if not rid:
            continue
        try:
            role = guild.get_role(int(rid))
        except (TypeError, ValueError):
            role = None
        if role is None:
            continue
        out.append((key, role))
    return out


def _event_type_label(key: str) -> tuple[str, str]:
    """Return (emoji, label) for an EventType key. Falls back gracefully."""
    for t in EVENT_TYPES:
        if t.key == key:
            return (t.emoji, t.label)
    return ("📌", key)


# ── Panel embed (public, top of the view) ────────────────────────────────
def _panel_embed(guild: discord.Guild, db) -> discord.Embed:
    embed = discord.Embed(
        title="🔔 Content Roles",
        description=(
            "Pick the content you want to be pinged for. Click a category "
            "below, choose your roles, and submit — your roles update "
            "instantly. Click again any time to change them.\n\n"
            "Selecting **nothing** in a category removes you from every "
            "role in that category."
        ),
        color=discord.Color.blurple(),
    )
    # Show how many roles are available per category so members know
    # what's behind each button.
    for _key, label, type_keys in CATEGORIES:
        pairs = _resolve_category_roles(guild, db, type_keys)
        if not pairs:
            continue
        names = ", ".join(role.name for _k, role in pairs)
        embed.add_field(
            name=f"{label}  ({len(pairs)})",
            value=names if len(names) <= 1024 else names[:1020] + "…",
            inline=False,
        )
    embed.set_footer(text="Tip: officers manage which roles appear here via the LFG config.")
    return embed


# ── Picker views ─────────────────────────────────────────────────────────
class CategoryButton(discord.ui.Button):
    """Top-level category button. Opens an ephemeral, user-specific picker
    pre-populated with the user's current roles for that category.
    """

    def __init__(self, category_key: str, label: str) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"content_roles:cat:{category_key}",
        )
        self.category_key = category_key

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Use this inside the server."),
                ephemeral=True,
            )
            return

        # Look up the category definition.
        cat = next(
            (c for c in CATEGORIES if c[0] == self.category_key), None,
        )
        if cat is None:
            await interaction.response.send_message(
                embed=error_embed("Unknown category", "This button is stale; please ask staff to repost the panel."),
                ephemeral=True,
            )
            return

        _key, label, type_keys = cat
        pairs = _resolve_category_roles(guild, bot.db, type_keys)
        if not pairs:
            await interaction.response.send_message(
                embed=info_embed(
                    "No roles configured",
                    f"There are no pingable roles set up under **{label}** yet. "
                    "Officers can configure them via `/lfg set-type-role`.",
                ),
                ephemeral=True,
            )
            return

        # Discord caps a select at 25 options. Our largest category (PvE)
        # has 9 entries today, so this is just defensive.
        pairs = pairs[:MAX_OPTIONS_PER_SELECT]

        view = CategoryPickerView(self.category_key, label, pairs, interaction.user)
        await interaction.response.send_message(
            embed=info_embed(
                f"Configure: {label}",
                "Select all the roles you want from this category, then click "
                "**Save**. Anything you leave unselected will be removed.",
            ),
            view=view,
            ephemeral=True,
        )


class CategoryRoleSelect(discord.ui.Select):
    """Per-user select populated with the category's roles, with the user's
    current roles preselected.
    """

    def __init__(
        self,
        category_key: str,
        label: str,
        pairs: list[tuple[str, discord.Role]],
        member: discord.Member,
    ) -> None:
        # Build options with current-state defaults.
        member_role_ids = {r.id for r in member.roles}
        options: list[discord.SelectOption] = []
        for type_key, role in pairs:
            emoji, label_text = _event_type_label(type_key)
            options.append(discord.SelectOption(
                label=label_text[:100],
                value=str(role.id),
                emoji=emoji,
                default=role.id in member_role_ids,
            ))
        super().__init__(
            placeholder=f"Pick your {label} roles…",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"content_roles:select:{category_key}",  # ephemeral; id reuse is fine
        )
        self.category_key = category_key
        self.role_ids = [role.id for _k, role in pairs]

    async def callback(self, interaction: discord.Interaction) -> None:
        # The select callback fires on every change. We don't apply changes
        # here — we wait for the Save button. Defer so Discord stays happy.
        # (Discord would otherwise show a "this interaction failed" toast
        # if neither the select nor a sibling component responds in 3s.)
        await interaction.response.defer()


class CategorySaveButton(discord.ui.Button):
    """Apply the user's selection: add roles they ticked, remove the ones
    they didn't (within this category only — doesn't touch other roles).
    """

    def __init__(self) -> None:
        super().__init__(
            label="Save",
            style=discord.ButtonStyle.success,
            emoji="✅",
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CategoryPickerView):
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Use this inside the server."),
                ephemeral=True,
            )
            return

        # The select's .values is a list[str] of role IDs the user wants to keep.
        select = view.select
        wanted_ids = {int(v) for v in select.values}
        category_role_ids = set(select.role_ids)

        to_add: list[discord.Role] = []
        to_remove: list[discord.Role] = []
        current_role_ids = {r.id for r in member.roles}
        for rid in category_role_ids:
            role = interaction.guild.get_role(rid) if interaction.guild else None
            if role is None:
                continue
            if rid in wanted_ids and rid not in current_role_ids:
                to_add.append(role)
            elif rid not in wanted_ids and rid in current_role_ids:
                to_remove.append(role)

        try:
            if to_add:
                await member.add_roles(*to_add, reason="Self-assigned via content-roles panel")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Self-removed via content-roles panel")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bot is missing permissions",
                    "I can't manage one of those roles — make sure my role is "
                    "above all the content-ping roles in the role list.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"content_roles save failed for {member}: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("Couldn't update roles", "Try again in a moment."),
                ephemeral=True,
            )
            return

        # Build a friendly recap.
        added_names = ", ".join(r.name for r in to_add) or "—"
        removed_names = ", ".join(r.name for r in to_remove) or "—"
        unchanged = len(category_role_ids) - len(to_add) - len(to_remove)
        embed = success_embed(
            f"{view.category_label} updated",
            f"**Added:** {added_names}\n**Removed:** {removed_names}\n"
            f"_Unchanged: {unchanged} role(s) in this category._",
        )
        # Disable the view so it's clear the action completed.
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(embed=embed, view=view)


class CategoryPickerView(discord.ui.View):
    """Ephemeral, per-user picker spawned from a category button click."""

    def __init__(
        self,
        category_key: str,
        category_label: str,
        pairs: list[tuple[str, discord.Role]],
        member: discord.Member,
    ) -> None:
        super().__init__(timeout=180)
        self.category_label = category_label
        self.select = CategoryRoleSelect(category_key, category_label, pairs, member)
        self.add_item(self.select)
        self.add_item(CategorySaveButton())


class ContentRolesPanelView(discord.ui.View):
    """Persistent public view: one button per category. Each button opens
    an ephemeral picker for the clicking user.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for cat_key, label, _types in CATEGORIES:
            self.add_item(CategoryButton(cat_key, label))


# ── Cog + slash commands ─────────────────────────────────────────────────
class ContentRoles(commands.Cog):
    """Posts and manages the self-assign content-role panel."""

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        # Re-register the persistent panel view so the buttons keep working
        # across restarts.
        self.bot.add_view(ContentRolesPanelView())
        self.bot.add_view(WeaponRolesPanelView())

    group = app_commands.Group(
        name="content-roles",
        description="Manage the self-assign content-role panel.",
    )

    @group.command(name="set-channel", description="Set the channel where the content-role panel lives.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel where the panel will be posted.")
    async def set_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config(CFG_PANEL_CHANNEL, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Panel channel set",
                f"The content-role panel will live in {channel.mention}. "
                "Run `/content-roles post-panel` next to post it.",
            ),
            ephemeral=True,
        )

    @group.command(name="post-panel", description="Post (or repost) the content-role picker panel.")
    @app_commands.default_permissions(manage_guild=True)
    async def post_panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return

        db = self.bot.db
        chan_id = db.get_config(CFG_PANEL_CHANNEL)
        # Default to the channel the command was invoked from if nothing
        # is configured, but only as a courtesy — the configured channel
        # always wins.
        channel: discord.TextChannel | None = None
        if chan_id:
            parsed_id, parse_error = _parse_config_channel_id(chan_id)
            if parse_error or parsed_id is None:
                await interaction.response.send_message(
                    embed=error_embed(
                        "Bad panel channel config",
                        f"{parse_error} Run `/content-roles set-channel` again.",
                    ),
                    ephemeral=True,
                )
                return
            ch = guild.get_channel(parsed_id)
            if isinstance(ch, discord.TextChannel):
                channel = ch
            else:
                await interaction.response.send_message(
                    embed=error_embed(
                        "Panel channel not found",
                        "The saved content-role panel channel no longer exists "
                        "or is not a text channel. Run `/content-roles set-channel` again.",
                    ),
                    ephemeral=True,
                )
                return
        if channel is None and isinstance(interaction.channel, discord.TextChannel):
            channel = interaction.channel
        if channel is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "No channel set",
                    "Run `/content-roles set-channel` first, or invoke this in a text channel.",
                ),
                ephemeral=True,
            )
            return

        # Best-effort: delete the previous panel message if we still have its id.
        old_msg_id = db.get_config(CFG_PANEL_MESSAGE)
        if old_msg_id:
            try:
                old = await channel.fetch_message(int(old_msg_id))
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            msg = await channel.send(
                embed=_panel_embed(guild, db),
                view=ContentRolesPanelView(),
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Can't post",
                    f"I don't have permission to send messages in {channel.mention}.",
                ),
                ephemeral=True,
            )
            return

        db.set_config(CFG_PANEL_CHANNEL, str(channel.id))
        db.set_config(CFG_PANEL_MESSAGE, str(msg.id))
        info_log(f"Posted content-roles panel to #{channel.name} ({channel.id}).")
        await interaction.response.send_message(
            embed=success_embed(
                "Panel posted",
                f"Members can now self-assign content roles in {channel.mention}.",
            ),
            ephemeral=True,
        )

    @group.command(name="set-weapon-channel", description="Set the channel where the weapon-tree panel lives.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Text channel where the weapon-tree panel will be posted.")
    async def set_weapon_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config(CFG_WEAPON_PANEL_CHANNEL, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Weapon panel channel set",
                f"The weapon-tree panel will live in {channel.mention}. "
                "Run `/content-roles post-weapon-panel` next.",
            ),
            ephemeral=True,
        )

    @group.command(name="ensure-weapon-roles", description="Create or repair weapon-tree self-assign roles.")
    @app_commands.default_permissions(manage_guild=True)
    async def ensure_weapon_roles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            existing, created = await _ensure_weapon_roles(guild, self.bot.db)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Can't create roles",
                    "I need Manage Roles, and my bot role must be high enough in the role list.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"weapon role creation failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Couldn't create roles", "Discord rejected the role update. Try again shortly."),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=success_embed(
                "Weapon roles ready",
                f"Created **{len(created)}** role(s); mapped **{len(existing)}** existing role(s).",
            ),
            ephemeral=True,
        )

    @group.command(name="post-weapon-panel", description="Post (or repost) the weapon-tree picker panel.")
    @app_commands.default_permissions(manage_guild=True)
    async def post_weapon_panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db
        try:
            await _ensure_weapon_roles(guild, db)
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Can't create roles",
                    "I need Manage Roles, and my bot role must be high enough in the role list.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"weapon role creation failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Couldn't create roles", "Discord rejected the role update. Try again shortly."),
                ephemeral=True,
            )
            return

        chan_id = db.get_config(CFG_WEAPON_PANEL_CHANNEL) or db.get_config(CFG_PANEL_CHANNEL)
        channel: discord.TextChannel | None = None
        if chan_id:
            parsed_id, parse_error = _parse_config_channel_id(chan_id)
            if parse_error or parsed_id is None:
                await interaction.followup.send(
                    embed=error_embed(
                        "Bad weapon panel channel config",
                        f"{parse_error} Run `/content-roles set-weapon-channel` again.",
                    ),
                    ephemeral=True,
                )
                return
            ch = guild.get_channel(parsed_id)
            if isinstance(ch, discord.TextChannel):
                channel = ch
            else:
                await interaction.followup.send(
                    embed=error_embed(
                        "Weapon panel channel not found",
                        "The saved weapon-role panel channel no longer exists "
                        "or is not a text channel. Run `/content-roles set-weapon-channel` again.",
                    ),
                    ephemeral=True,
                )
                return
        if channel is None and isinstance(interaction.channel, discord.TextChannel):
            channel = interaction.channel
        if channel is None:
            await interaction.followup.send(
                embed=error_embed(
                    "No channel set",
                    "Run `/content-roles set-weapon-channel` first, or invoke this in a text channel.",
                ),
                ephemeral=True,
            )
            return

        old_msg_id = db.get_config(CFG_WEAPON_PANEL_MESSAGE)
        if old_msg_id:
            try:
                old = await channel.fetch_message(int(old_msg_id))
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        try:
            msg = await channel.send(
                embed=_weapon_panel_embed(guild, db),
                view=WeaponRolesPanelView(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Can't post",
                    f"I don't have permission to send messages in {channel.mention}.",
                ),
                ephemeral=True,
            )
            return

        db.set_config(CFG_WEAPON_PANEL_CHANNEL, str(channel.id))
        db.set_config(CFG_WEAPON_PANEL_MESSAGE, str(msg.id))
        info_log(f"Posted weapon-roles panel to #{channel.name} ({channel.id}).")
        await interaction.followup.send(
            embed=success_embed(
                "Weapon panel posted",
                f"Members can now self-assign weapon-tree roles in {channel.mention}.",
            ),
            ephemeral=True,
        )

    @group.command(name="show-config", description="Show which roles will appear in the panel.")
    @app_commands.default_permissions(manage_guild=True)
    async def show_config(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return

        db = self.bot.db
        lines: list[str] = []
        chan_id = db.get_config(CFG_PANEL_CHANNEL)
        msg_id = db.get_config(CFG_PANEL_MESSAGE)
        lines.append(f"**Panel channel:** {('<#' + chan_id + '>') if chan_id else '_(unset)_'}")
        lines.append(f"**Panel message id:** {msg_id or '_(none)_'}")
        lines.append("")

        total = 0
        for _key, label, type_keys in CATEGORIES:
            pairs = _resolve_category_roles(guild, db, type_keys)
            total += len(pairs)
            if not pairs:
                lines.append(f"__{label}__ — _(no roles configured)_")
                continue
            names = ", ".join(role.mention for _k, role in pairs)
            lines.append(f"__{label}__ ({len(pairs)}): {names}")

        embed = info_embed(
            f"Content-roles config · {total} role(s)",
            "\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="show-weapon-config", description="Show weapon-tree role mappings.")
    @app_commands.default_permissions(manage_guild=True)
    async def show_weapon_config(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        db = self.bot.db
        lines: list[str] = []
        chan_id = db.get_config(CFG_WEAPON_PANEL_CHANNEL)
        msg_id = db.get_config(CFG_WEAPON_PANEL_MESSAGE)
        lines.append(f"**Weapon panel channel:** {('<#' + chan_id + '>') if chan_id else '_(unset)_'}")
        lines.append(f"**Weapon panel message id:** {msg_id or '_(none)_'}")
        lines.append("")
        total = 0
        for _key, label, weapon_keys in WEAPON_CATEGORIES:
            pairs = _resolve_weapon_roles(guild, db, weapon_keys)
            total += len(pairs)
            if not pairs:
                lines.append(f"__{label}__ — _(no roles configured)_")
                continue
            names = ", ".join(role.mention for _k, role in pairs)
            lines.append(f"__{label}__ ({len(pairs)}): {names}")
        embed = info_embed(
            f"Weapon-role config · {total} role(s)",
            "\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(ContentRoles(bot))
