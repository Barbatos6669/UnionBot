"""
Emergency kill switch cog.

Adds a /emergency shutdown command that immediately logs out the bot and disables all commands and listeners.
Only available to officers (manage_guild or STAFF_ROLES).
"""
from __future__ import annotations

from cogs._typing import Bot
import discord
from discord import app_commands
from discord.ext import commands
import sys

from debug import info_log
from utils import error_embed, success_embed
from utils import is_officer as _is_officer




class Emergency(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    emergency_group = app_commands.Group(
        name="emergency",
        description="Emergency kill switch (officers only)",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @emergency_group.command(name="shutdown", description="Immediately log out and disable the bot (officers only)")
    async def shutdown(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        info_log(f"EMERGENCY SHUTDOWN triggered by {interaction.user} ({interaction.user.id})")
        await interaction.response.send_message(
            embed=success_embed(
                "Emergency shutdown",
                "Bot is logging out and will stop responding to all commands and events. Manual restart required.",
            ),
            ephemeral=True,
        )
        await self.bot.close()
        sys.exit(0)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Emergency(bot))
