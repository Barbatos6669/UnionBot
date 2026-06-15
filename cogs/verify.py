"""In-game guild membership verification.

Officers (or anyone, if we keep it public) can quickly check whether an
Albion character is currently in a specified guild — useful for alliance
checks, recruiting filters, or spot-verifying applicants before approval.

Backed by the same gameinfo lookup the application flow uses: we resolve
the character to a player id (preferring the candidate already in the
target guild when names are ambiguous) and compare ``GuildName``.
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

import albion_api
from cogs._typing import Bot
from debug import error_log
from utils import (
    autocomplete_guild_name,
    error_embed,
    success_embed,
    warning_embed,
)


def pick_best_candidate(candidates: list[dict], target_guild_lower: str) -> dict | None:
    """Choose the best matching candidate from an Albion name-search result.

    Prefers (in order):
      1. A candidate already in the target guild (so we don't false-negative
         when two characters share a name and one of them is in the guild).
      2. Any candidate in *some* guild (more likely to be an active player).
      3. Higher combined kill+death fame (more active character).

    Returns None on empty input.
    """
    if not candidates:
        return None

    def _score(p: dict) -> tuple[int, int, int, int]:
        gname = (p.get("GuildName") or "").strip().lower()
        return (
            1 if gname == target_guild_lower else 0,
            1 if (p.get("GuildId") or "") else 0,
            1 if (p.get("AllianceId") or "") else 0,
            int(p.get("KillFame") or 0) + int(p.get("DeathFame") or 0),
        )

    return max(candidates, key=_score)


class Verify(commands.Cog):
    """Slash commands for in-game verification."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="verify",
        description="Verify in-game facts about an Albion character.",
    )

    @group.command(
        name="guild",
        description="Check whether an Albion character is currently in a given guild.",
    )
    @app_commands.describe(
        character="In-game Albion character name (case-sensitive on the API side).",
        guild_name="Name of the guild to check membership for.",
        server="Albion server — defaults to Americas.",
    )
    @app_commands.choices(server=[
        app_commands.Choice(name="Americas", value="americas"),
        app_commands.Choice(name="Europe",   value="europe"),
        app_commands.Choice(name="Asia",     value="asia"),
    ])
    @app_commands.autocomplete(guild_name=autocomplete_guild_name)
    async def verify_guild(
        self,
        interaction: discord.Interaction,
        character: str,
        guild_name: str,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        server_value = server.value if server else "americas"
        target = (guild_name or "").strip()
        char_name = (character or "").strip()
        if not target or not char_name:
            await interaction.response.send_message(
                embed=error_embed("Missing input", "Character and guild name are required."),
                ephemeral=True,
            )
            return

        # The Albion gameinfo endpoint can be slow (5–20s) so always defer.
        await interaction.response.defer(ephemeral=True, thinking=True)

        loop = asyncio.get_running_loop()
        try:
            candidates = await loop.run_in_executor(
                None,
                lambda: albion_api.find_player_candidates(char_name, server_value),
            )
        except Exception as exc:  # noqa: BLE001 — network/API errors are expected
            error_log(f"verify_guild: API lookup failed for {char_name!r}: {exc}")
            await interaction.followup.send(
                embed=error_embed("Lookup failed", "Albion API did not respond. Try again in a minute."),
                ephemeral=True,
            )
            return

        if not candidates:
            await interaction.followup.send(
                embed=error_embed(
                    "Character not found",
                    f"No player named `{char_name}` on **{server_value.title()}**.",
                    hint="Capitalization counts. Try the spelling shown on their killboard.",
                ),
                ephemeral=True,
            )
            return

        target_lower = target.lower()
        best = pick_best_candidate(candidates, target_lower)
        if best is None:  # defensive — candidates was non-empty so this shouldn't happen
            await interaction.followup.send(
                embed=error_embed("Lookup failed", "Unexpected empty candidate set."),
                ephemeral=True,
            )
            return

        actual_guild = (best.get("GuildName") or "").strip()
        exact_name = best.get("Name") or char_name
        ambiguous = len(candidates) > 1
        in_guild = actual_guild.lower() == target_lower

        if in_guild:
            embed = success_embed(
                "✅ Verified",
                (
                    f"**{exact_name}** is currently a member of **{actual_guild}** "
                    f"on {server_value.title()}."
                ),
            )
        elif actual_guild:
            embed = warning_embed(
                "❌ Not in that guild",
                (
                    f"**{exact_name}** is in **{actual_guild}**, not **{target}** "
                    f"(on {server_value.title()})."
                ),
            )
        else:
            embed = warning_embed(
                "❌ Guildless",
                (
                    f"**{exact_name}** is not currently in any guild "
                    f"(on {server_value.title()})."
                ),
            )

        if ambiguous:
            embed.add_field(
                name="⚠️ Multiple characters share that name",
                value=(
                    f"Picked the most likely match ({len(candidates)} candidates). "
                    "If this looks wrong, double-check the killboard URL."
                ),
                inline=False,
            )
        # Always include the GuildName/Id we resolved so officers can spot bad picks.
        embed.add_field(
            name="Resolved character",
            value=(
                f"`Id` `{best.get('Id', '?')}`\n"
                f"`KillFame` {int(best.get('KillFame') or 0):,} · "
                f"`DeathFame` {int(best.get('DeathFame') or 0):,}"
            ),
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command(self.group.name)
        except (AttributeError, ValueError) as exc:
            error_log(f"verify cog_unload: {exc}")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Verify(bot))
