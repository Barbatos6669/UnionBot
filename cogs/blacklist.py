"""Blacklist cog — permanently deny specific Discord users or Albion characters
from joining/registering with the home guild.

A blacklist row matches by ``discord_id`` and/or ``albion_player_id``. On
``on_member_join``, blacklisted users are DM'd a polite notice and kicked.
Registration also checks the blacklist (both the registering Discord ID and
the resolved Albion player ID) to prevent alt-account workarounds.

Officer slash commands:
    /blacklist add        — add a Discord user and/or Albion name
    /blacklist remove     — remove by Discord user or Albion name
    /blacklist list       — list current entries
    /blacklist check      — check whether a user/character is blacklisted
"""

from __future__ import annotations

from cogs._typing import Bot
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from debug import info_log, error_log
from utils import error_embed, info_embed, success_embed
import albion_api


_DM_KICK_MESSAGE = (
    "Hi {name}, thanks for your interest in **{guild}**.\n\n"
    "Unfortunately, you are currently on our blacklist and cannot join the "
    "server at this time. If you believe this is a mistake, please reach out "
    "to a staff member to discuss it.\n\n"
    "Wishing you the best out there in Albion. o7"
)


async def _dm_and_kick(member: discord.Member, reason: str | None) -> None:
    """DM a polite notice (best-effort), then kick. Quiet on DM/kick failure."""
    try:
        msg = _DM_KICK_MESSAGE.format(name=member.display_name, guild=member.guild.name)
        if reason:
            msg += f"\n\n*Note from staff:* {reason}"
        await member.send(msg)
    except (discord.Forbidden, discord.HTTPException):
        # DMs closed or rate-limited — not fatal.
        pass
    try:
        await member.kick(reason=f"Blacklisted: {reason or 'no reason given'}")
        info_log(f"Blacklist auto-kicked {member} ({member.id}).")
    except discord.Forbidden:
        error_log(f"Blacklist: missing kick permission for {member}.")
    except discord.HTTPException as exc:
        error_log(f"Blacklist: kick failed for {member}: {exc!r}")


class Blacklist(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    # ── auto-kick on join ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        try:
            row = self.bot.db.is_blacklisted(discord_id=str(member.id))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            error_log(f"Blacklist on_member_join lookup failed: {exc!r}")
            return
        if not row:
            return
        await _dm_and_kick(member, row.get("reason"))

    # ── slash command group ───────────────────────────────────────────────
    blacklist_group = app_commands.Group(
        name="blacklist",
        description="Manage the guild blacklist (officer-only).",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @blacklist_group.command(name="add", description="Add a Discord user and/or Albion character to the blacklist.")
    @app_commands.describe(
        member="The Discord user to blacklist (optional if albion_name is given).",
        albion_name="The Albion character name to blacklist (optional if member is given).",
        server="Albion server when blacklisting by name (default: Americas).",
        reason="Why are they being blacklisted? (visible to staff only)",
    )
    @app_commands.choices(server=[
        app_commands.Choice(name="Americas", value="americas"),
        app_commands.Choice(name="Europe",   value="europe"),
        app_commands.Choice(name="Asia",     value="asia"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        albion_name: str | None = None,
        server: app_commands.Choice[str] | None = None,
        reason: str | None = None,
    ) -> None:
        if member is None and not albion_name:
            await interaction.response.send_message(
                embed=error_embed(
                    "Need a target",
                    "Provide a Discord member, an Albion character name, or both.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Resolve Albion name → player_id (so a Discord rename/ID swap still
        # blocks them on registration).
        albion_player_id: str | None = None
        resolved_name: str | None = None
        if albion_name:
            srv = server.value if server else "americas"
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    None, lambda: albion_api.get_player_id(albion_name.strip(), srv),
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"Blacklist add: get_player_id failed: {exc!r}")
                result = None
            if not result:
                await interaction.followup.send(
                    embed=error_embed(
                        "Character not found",
                        f"`{albion_name}` was not found on **{srv.capitalize()}**. "
                        "Check spelling and try again.",
                    ),
                    ephemeral=True,
                )
                return
            albion_player_id, resolved_name = result

        try:
            self.bot.db.add_to_blacklist(  # type: ignore[attr-defined]
                discord_id=str(member.id) if member else None,
                albion_player_id=albion_player_id,
                albion_name=resolved_name,
                username=str(member) if member else None,
                reason=reason,
                added_by=str(interaction.user.id),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"Blacklist add failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Database error", "Couldn't write to the blacklist."),
                ephemeral=True,
            )
            return

        target_bits: list[str] = []
        if member:
            target_bits.append(member.mention)
        if resolved_name:
            target_bits.append(f"**{resolved_name}** (Albion)")
        info_log(
            f"{interaction.user} blacklisted {' / '.join(target_bits)} "
            f"reason={reason!r}"
        )

        # Kick now if they're already in the server.
        if member and member.guild is interaction.guild:
            await _dm_and_kick(member, reason)

        await interaction.followup.send(
            embed=success_embed(
                "Blacklisted",
                f"Added {' & '.join(target_bits)} to the blacklist."
                + (f"\n*Reason:* {reason}" if reason else ""),
            ),
            ephemeral=True,
        )

    @blacklist_group.command(name="remove", description="Remove a Discord user or Albion character from the blacklist.")
    @app_commands.describe(
        member="The Discord user to un-blacklist.",
        albion_name="The Albion character name to un-blacklist.",
        server="Albion server when un-blacklisting by name (default: Americas).",
    )
    @app_commands.choices(server=[
        app_commands.Choice(name="Americas", value="americas"),
        app_commands.Choice(name="Europe",   value="europe"),
        app_commands.Choice(name="Asia",     value="asia"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        albion_name: str | None = None,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        if member is None and not albion_name:
            await interaction.response.send_message(
                embed=error_embed(
                    "Need a target",
                    "Provide a Discord member, an Albion character name, or both.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        albion_player_id: str | None = None
        if albion_name:
            srv = server.value if server else "americas"
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    None, lambda: albion_api.get_player_id(albion_name.strip(), srv),
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"Blacklist remove: get_player_id failed: {exc!r}")
                result = None
            if result:
                albion_player_id, _ = result

        removed = self.bot.db.remove_from_blacklist(  # type: ignore[attr-defined]
            discord_id=str(member.id) if member else None,
            albion_player_id=albion_player_id,
        )
        if not removed:
            await interaction.followup.send(
                embed=info_embed("Nothing to remove", "No matching blacklist entry found."),
                ephemeral=True,
            )
            return

        info_log(f"{interaction.user} removed {removed} blacklist row(s).")
        await interaction.followup.send(
            embed=success_embed("Removed", f"Removed **{removed}** blacklist entry/entries."),
            ephemeral=True,
        )

    @blacklist_group.command(name="list", description="Show all blacklisted users (officer-only).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_list(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.fetch_all_blacklist()  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Blacklist", "No one is blacklisted."),
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for r in rows[:25]:  # discord embed limit safety
            bits: list[str] = []
            if r.get("discord_id"):
                bits.append(f"<@{r['discord_id']}>")
            if r.get("albion_name"):
                bits.append(f"`{r['albion_name']}`")
            elif r.get("albion_player_id"):
                bits.append(f"player_id `{r['albion_player_id']}`")
            target = " · ".join(bits) or "(unknown)"
            reason = r.get("reason") or "—"
            lines.append(f"• {target} — *{reason}*")

        more = ""
        if len(rows) > 25:
            more = f"\n\n…and **{len(rows) - 25}** more."
        await interaction.response.send_message(
            embed=info_embed(f"Blacklist — {len(rows)} entries", "\n".join(lines) + more),
            ephemeral=True,
        )

    @blacklist_group.command(name="check", description="Check whether a user or character is blacklisted.")
    @app_commands.describe(
        member="The Discord user to check.",
        albion_name="The Albion character name to check.",
        server="Albion server when checking by name (default: Americas).",
    )
    @app_commands.choices(server=[
        app_commands.Choice(name="Americas", value="americas"),
        app_commands.Choice(name="Europe",   value="europe"),
        app_commands.Choice(name="Asia",     value="asia"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_check(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        albion_name: str | None = None,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        if member is None and not albion_name:
            await interaction.response.send_message(
                embed=error_embed("Need a target", "Provide a member or Albion name."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        albion_player_id: str | None = None
        if albion_name:
            srv = server.value if server else "americas"
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    None, lambda: albion_api.get_player_id(albion_name.strip(), srv),
                )
            except Exception:  # noqa: BLE001
                result = None
            if result:
                albion_player_id, _ = result

        row = self.bot.db.is_blacklisted(  # type: ignore[attr-defined]
            discord_id=str(member.id) if member else None,
            albion_player_id=albion_player_id,
        )
        if not row:
            await interaction.followup.send(
                embed=success_embed("Clean", "No matching blacklist entry."),
                ephemeral=True,
            )
            return
        bits: list[str] = []
        if row.get("discord_id"):
            bits.append(f"discord <@{row['discord_id']}>")
        if row.get("albion_name"):
            bits.append(f"albion `{row['albion_name']}`")
        await interaction.followup.send(
            embed=info_embed(
                "Blacklisted",
                f"{' / '.join(bits)}\n*Reason:* {row.get('reason') or '—'}\n"
                f"*Added:* {row.get('added_at') or '—'}",
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(Blacklist(bot))
