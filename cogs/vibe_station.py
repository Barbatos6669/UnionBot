"""
Vibe Station moderation cog.

The Vibe Station is a chill-zone category meant for gaming OUTSIDE of Albion.
Any mention of "albion" in channels under that category earns the speaker a
1-hour timeout (officers exempt). The cog auto-detects the category by name
("vibe station" in the category name, case-insensitive) so no manual config
is required — adding a new channel under that category automatically applies
the rule.

Slash commands (officer-only):
- /vibe status               — show which category is being moderated + stats
- /vibe enable / disable     — toggle moderation (default: enabled)
- /vibe pardon <member>      — clear an active Vibe-Station timeout early
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed, warning_embed
from utils import is_officer as _is_officer


# ── Config keys (stored in guild_config) ─────────────────────────────────────
CFG_ENABLED = "vibe_station_enabled"            # "1" / "0" — default "1"
CFG_CATEGORY_NAME = "vibe_station_category_name"  # override name match; default below
CFG_TIMEOUT_MINUTES = "vibe_station_timeout_minutes"  # default 60

# Substring matched (case-insensitive) against category names to identify the
# Vibe Station category. Editable via /vibe set-category-name if needed.
DEFAULT_CATEGORY_NAME_MATCH = "vibe station"

# Word-boundary regex so "albion" matches "Albion!", "Albion.", "ALBION" — but
# NOT "balconial" or "rebellion". Re-compiled at module load.
_ALBION_RX = re.compile(r"\balbion\b", re.IGNORECASE)




def _category_name_match(db) -> str:
    raw = db.get_config(CFG_CATEGORY_NAME)
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return DEFAULT_CATEGORY_NAME_MATCH


def _is_enabled(db) -> bool:
    raw = db.get_config(CFG_ENABLED)
    if raw is None:
        return True
    return str(raw).strip() in {"1", "true", "True", "yes", "on"}


def _timeout_minutes(db) -> int:
    raw = db.get_config(CFG_TIMEOUT_MINUTES)
    try:
        n = int(raw) if raw is not None else 60
    except (TypeError, ValueError):
        n = 60
    return max(1, min(n, 1440))


def _channel_in_vibe(channel: discord.abc.GuildChannel | discord.Thread, match: str) -> bool:
    """True if the channel (or its parent thread/forum) sits under a category
    whose name contains the ``match`` substring (case-insensitive)."""
    cat: discord.CategoryChannel | None = getattr(channel, "category", None)
    # Threads expose .parent (the parent text channel); walk one hop up.
    if cat is None:
        parent = getattr(channel, "parent", None)
        if parent is not None:
            cat = getattr(parent, "category", None)
    if cat is None:
        return False
    return match in (cat.name or "").lower()


class VibeStation(commands.Cog):
    """Auto-timeout Albion mentions in the Vibe Station category."""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    # ── Listener ────────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        db = self.bot.db  # type: ignore[attr-defined]
        if not _is_enabled(db):
            return
        match_name = _category_name_match(db)
        if not _channel_in_vibe(message.channel, match_name):
            return
        if not _ALBION_RX.search(message.content or ""):
            return

        author = message.author
        # Officers / admins bypass the rule.
        if _is_officer(author):
            return
        if not isinstance(author, discord.Member):
            return

        minutes = _timeout_minutes(db)
        until_dt = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)

        # Try the timeout first. If we lack perms or the member is higher in
        # the role hierarchy, log and bail out (don't spam the channel).
        try:
            await author.timeout(
                until_dt,
                reason=f"Mentioned 'Albion' in the Vibe Station ({match_name!r})",
            )
        except discord.Forbidden:
            error_log(
                f"vibe_station: timeout denied for {author} in #{message.channel} "
                "(missing Moderate Members or role hierarchy)."
            )
            return
        except discord.HTTPException as exc:
            error_log(f"vibe_station: timeout failed for {author}: {exc!r}")
            return

        info_log(
            f"vibe_station: timed out {author} ({author.id}) for {minutes}m — "
            f"channel=#{message.channel} content={message.content!r}"
        )

        # Try to delete the offending message (best-effort) and reply playfully.
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            unix_ts = int(until_dt.timestamp())
            embed = warning_embed(
                "⛔ NO ALBION IN THE VIBE STATION ⛔",
                f"{author.mention} — this is a **chill zone**. We're here to game, "
                f"vent, and *not* think about Albion.\n\n"
                f"Enjoy your {minutes}-minute timeout. Touch grass. Pet a dog. "
                f"Play literally anything else.\n\n"
                f"You can speak again <t:{unix_ts}:R>.",
            )
            await message.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"vibe_station: announce send failed: {exc!r}")

    # ── Slash commands ──────────────────────────────────────────────────────
    vibe_group = app_commands.Group(
        name="vibe",
        description="Manage the Vibe Station chill-zone moderation.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @vibe_group.command(name="status", description="Show Vibe Station moderation status.")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        db = self.bot.db  # type: ignore[attr-defined]
        match_name = _category_name_match(db)
        enabled = _is_enabled(db)
        minutes = _timeout_minutes(db)

        matching = [
            c for c in interaction.guild.categories
            if match_name in (c.name or "").lower()
        ]
        if matching:
            cat_lines = "\n".join(
                f"• **{c.name}** (id `{c.id}`) — {len(c.channels)} channel(s)"
                for c in matching
            )
        else:
            cat_lines = (
                f"_No category contains `{match_name}` in its name. "
                f"Use `/vibe set-category-name` to change the match._"
            )

        await interaction.response.send_message(
            embed=info_embed(
                "Vibe Station status",
                f"**Enabled:** {'✅ yes' if enabled else '❌ no'}\n"
                f"**Timeout:** {minutes} minute(s)\n"
                f"**Category-name match:** `{match_name}`\n"
                f"**Officers exempt:** yes\n\n"
                f"**Matched categories:**\n{cat_lines}",
            ),
            ephemeral=True,
        )

    @vibe_group.command(name="enable", description="Enable Albion-mention timeouts in the Vibe Station.")
    async def enable(self, interaction: discord.Interaction) -> None:
        self.bot.db.set_config(CFG_ENABLED, "1")  # type: ignore[attr-defined]
        info_log(f"/vibe enable by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed("Vibe Station enabled", "Albion-mention timeouts are now active."),
            ephemeral=True,
        )

    @vibe_group.command(name="disable", description="Disable Albion-mention timeouts.")
    async def disable(self, interaction: discord.Interaction) -> None:
        self.bot.db.set_config(CFG_ENABLED, "0")  # type: ignore[attr-defined]
        info_log(f"/vibe disable by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed("Vibe Station disabled", "No timeouts will be issued."),
            ephemeral=True,
        )

    @vibe_group.command(name="set-timeout", description="Set the timeout duration in minutes (1–1440).")
    @app_commands.describe(minutes="How long the timeout lasts (1 to 1440 minutes).")
    async def set_timeout(self, interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 1440]) -> None:
        self.bot.db.set_config(CFG_TIMEOUT_MINUTES, str(int(minutes)))  # type: ignore[attr-defined]
        info_log(f"/vibe set-timeout {minutes} by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed("Timeout updated", f"Albion mentions now earn a **{minutes}-minute** timeout."),
            ephemeral=True,
        )

    @vibe_group.command(
        name="set-category-name",
        description="Set the substring used to match the Vibe Station category name.",
    )
    @app_commands.describe(name="Case-insensitive substring (e.g. 'vibe station' or 'chill zone').")
    async def set_category_name(self, interaction: discord.Interaction, name: str) -> None:
        clean = (name or "").strip()
        if len(clean) < 3:
            await interaction.response.send_message(
                embed=error_embed("Too short", "Category-name match must be at least 3 characters."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(CFG_CATEGORY_NAME, clean)  # type: ignore[attr-defined]
        info_log(f"/vibe set-category-name {clean!r} by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed("Category match updated", f"Now matching categories containing **`{clean}`**."),
            ephemeral=True,
        )

    @vibe_group.command(name="pardon", description="Clear an active Vibe-Station timeout early.")
    @app_commands.describe(member="The member to pardon.")
    async def pardon(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if member.timed_out_until is None:
            await interaction.response.send_message(
                embed=info_embed("Nothing to pardon", f"{member.mention} is not currently timed out."),
                ephemeral=True,
            )
            return
        try:
            await member.timeout(None, reason=f"Vibe-Station pardon by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "I can't remove this member's timeout (role hierarchy)."),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=error_embed("Discord error", f"```{exc!r}```"),
                ephemeral=True,
            )
            return
        info_log(f"/vibe pardon {member} by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed("Pardoned", f"{member.mention} has been released early. Behave."),
            ephemeral=False,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(VibeStation(bot))
