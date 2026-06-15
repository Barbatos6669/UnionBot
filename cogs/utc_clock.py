"""UTC clock voice channel.

Maintains a locked voice channel whose name shows the current UTC time.
Discord rate-limits channel renames, so this intentionally updates on a
10-minute cadence rather than trying to tick every minute.
"""

from __future__ import annotations

import datetime as _dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._lfg_config import prime_timer_status_emoji
from cogs._typing import Bot
from debug import error_log, info_log, warning_log
from utils import error_embed, success_embed

CFG_CLOCK_CHANNEL = "utc_clock_channel_id"
CFG_CLOCK_ENABLED = "utc_clock_enabled"
UPDATE_MINUTES = 10
BETWEEN_TIMER_EMOJI = "⏳"
OFF_TIMER_EMOJI = "💤"


def _utc_clock_bucket(now: _dt.datetime) -> _dt.datetime:
    minute = (now.minute // UPDATE_MINUTES) * UPDATE_MINUTES
    return now.replace(minute=minute, second=0, microsecond=0)


def _utc_clock_name(now: _dt.datetime | None = None) -> str:
    now = now or discord.utils.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    now = _utc_clock_bucket(now.astimezone(_dt.timezone.utc))
    emoji = prime_timer_status_emoji(
        now,
        between_emoji=BETWEEN_TIMER_EMOJI,
        off_emoji=OFF_TIMER_EMOJI,
    )
    return f"{emoji} UTC {now:%H:%M} (10m) {emoji}"


class UtcClock(commands.Cog):
    """Keep a display-only voice channel renamed to the current UTC time."""

    group = app_commands.Group(
        name="utc-clock",
        description="Manage the UTC clock channel.",
    )

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        self.clock_loop.start()

    async def cog_unload(self) -> None:
        self.clock_loop.cancel()

    def _enabled(self) -> bool:
        return (self.bot.db.get_config(CFG_CLOCK_ENABLED) or "1").strip() != "0"

    async def _configured_channel(self) -> discord.VoiceChannel | None:
        channel_id = (self.bot.db.get_config(CFG_CLOCK_CHANNEL) or "").strip()
        if not channel_id:
            return None
        try:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, ValueError):
            self.bot.db.set_config(CFG_CLOCK_CHANNEL, "")
            return None
        if not isinstance(channel, discord.VoiceChannel):
            return None
        return channel

    async def _update_clock(self, *, force: bool = False) -> bool:
        if not self._enabled():
            return False
        channel = await self._configured_channel()
        if channel is None:
            return False
        name = _utc_clock_name()
        if channel.name == name and not force:
            return False
        try:
            await channel.edit(name=name, reason="UTC clock channel update")
            info_log(f"UTC clock channel renamed to {name}.")
            return True
        except discord.Forbidden:
            warning_log("UTC clock update failed: missing Manage Channels permission.")
        except discord.HTTPException as exc:
            error_log(f"UTC clock update failed: {exc!r}")
        return False

    @tasks.loop(minutes=UPDATE_MINUTES)
    async def clock_loop(self) -> None:
        await self._update_clock()

    @clock_loop.before_loop
    async def _before_clock_loop(self) -> None:
        await self.bot.wait_until_ready()
        await self._update_clock(force=True)

    @clock_loop.error
    async def _clock_loop_error(self, exc: BaseException) -> None:
        error_log(f"UTC clock loop crashed: {exc!r}; restarting loop.")
        try:
            self.clock_loop.restart()
        except Exception as restart_exc:  # pragma: no cover - defensive
            error_log(f"Failed to restart UTC clock loop: {restart_exc!r}")

    @group.command(
        name="create",
        description="Create a locked voice channel that displays UTC time.",
    )
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(category="Optional category to place the clock channel in.")
    async def create(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this inside the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=False),
        }
        channel = await guild.create_voice_channel(
            name=_utc_clock_name(),
            category=category,
            overwrites=overwrites,
            reason=f"UTC clock channel created by {interaction.user}",
        )
        self.bot.db.set_config(CFG_CLOCK_CHANNEL, str(channel.id))
        self.bot.db.set_config(CFG_CLOCK_ENABLED, "1")
        await interaction.followup.send(
            embed=success_embed(
                "UTC clock created",
                f"Clock channel: {channel.mention}\n"
                f"Updates every {UPDATE_MINUTES} minutes to respect Discord limits.",
            ),
            ephemeral=True,
        )

    @group.command(
        name="set",
        description="Use an existing voice channel as the UTC clock.",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        self.bot.db.set_config(CFG_CLOCK_CHANNEL, str(channel.id))
        self.bot.db.set_config(CFG_CLOCK_ENABLED, "1")
        await self._update_clock(force=True)
        await interaction.response.send_message(
            embed=success_embed(
                "UTC clock channel set",
                f"{channel.mention} will update every {UPDATE_MINUTES} minutes.",
            ),
            ephemeral=True,
        )

    @group.command(name="refresh", description="Rename the UTC clock channel now.")
    @app_commands.default_permissions(manage_channels=True)
    async def refresh(self, interaction: discord.Interaction) -> None:
        changed = await self._update_clock(force=True)
        title = "UTC clock refreshed" if changed else "UTC clock not updated"
        detail = (
            "The configured channel was renamed."
            if changed
            else "No configured voice channel was found, or Discord rejected the rename."
        )
        await interaction.response.send_message(
            embed=success_embed(title, detail),
            ephemeral=True,
        )

    @group.command(name="disable", description="Stop updating the UTC clock channel.")
    @app_commands.default_permissions(manage_channels=True)
    async def disable(self, interaction: discord.Interaction) -> None:
        self.bot.db.set_config(CFG_CLOCK_ENABLED, "0")
        await interaction.response.send_message(
            embed=success_embed("UTC clock disabled", "The channel will no longer rename."),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(UtcClock(bot))
