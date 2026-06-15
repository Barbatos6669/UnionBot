"""Leave of Absence (LOA) cog.

Lets a member tell the guild they'll be away for a known stretch so the
bot stops nudging them and the lifecycle automation doesn't demote them
to Inactive while they're gone.

Commands:
    /loa start <days> [reason]   — self-serve up to 60 days
    /loa end                     — return early
    /loa status [user]           — show LOA state (self by default)
    /loa list                    — officers: everyone currently away
    /loa set <user> <until> [reason]
                                 — officers: extend / place on LOA

A LOA expires by date — no cron needed. ``loa_until`` ≥ today means
active; below today means it auto-clears in every check.
"""

from __future__ import annotations

import datetime as _dt

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from debug import info_log
from utils import error_embed, info_embed, is_officer, success_embed


_SELF_SERVE_MAX_DAYS = 60
_ISO_DATE = "%Y-%m-%d"


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _format_loa_line(row: dict) -> str:
    did = row.get("discord_id") or "?"
    name = row.get("albion_name") or "—"
    until = row.get("loa_until") or "?"
    reason = (row.get("loa_reason") or "").strip()
    tail = f" — _{reason}_" if reason else ""
    return f"<@{did}> · **{name}** · returns **{until}**{tail}"


class LOACog(commands.Cog):
    """Leave-of-Absence management."""

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot

    loa = app_commands.Group(
        name="loa",
        description="Plan time away so the bot doesn't flag you as inactive.",
    )

    # ── /loa start ──────────────────────────────────────────────────────────

    @loa.command(
        name="start",
        description="Tell the bot you'll be away for N days (self-serve, up to 60).",
    )
    @app_commands.describe(
        days="How many days you'll be away (1–60).",
        reason="Optional note officers will see (vacation, exams, etc.).",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, _SELF_SERVE_MAX_DAYS],
        reason: str | None = None,
    ) -> None:
        discord_id = str(interaction.user.id)
        db = self.bot.db
        profile = db.fetch_user_profile(discord_id)
        if not profile:
            await interaction.response.send_message(
                embed=error_embed(
                    "Not registered",
                    "Click the **Register** button in your registration channel first so the bot knows who you are.",
                ),
                ephemeral=True,
            )
            return
        until = (_dt.date.today() + _dt.timedelta(days=int(days))).isoformat()
        db.set_loa(discord_id, until, reason)
        await interaction.response.send_message(
            embed=success_embed(
                "LOA scheduled",
                (
                    f"You're flagged as **away until {until}** "
                    f"({int(days)} days).\n"
                    "While LOA is active, the bot won't send inactivity "
                    "nudges or demote your rank.\n\n"
                    "Use `/loa end` to come back early."
                    + (f"\n\n**Reason logged:** {reason}" if reason else "")
                ),
            ),
            ephemeral=True,
        )
        info_log(
            f"LOA started for {interaction.user} ({discord_id}) until {until} "
            f"reason={reason!r}."
        )

    # ── /loa end ────────────────────────────────────────────────────────────

    @loa.command(
        name="end",
        description="End your Leave of Absence right now.",
    )
    async def end(self, interaction: discord.Interaction) -> None:
        discord_id = str(interaction.user.id)
        db = self.bot.db
        if not db.is_on_loa(discord_id):
            await interaction.response.send_message(
                embed=info_embed(
                    "Not on LOA",
                    "You don't have an active leave on file.",
                ),
                ephemeral=True,
            )
            return
        db.clear_loa(discord_id)
        await interaction.response.send_message(
            embed=success_embed(
                "Welcome back",
                "Your LOA has been cleared. Activity tracking resumes "
                "immediately. 💚",
            ),
            ephemeral=True,
        )
        info_log(f"LOA cleared by user for {interaction.user} ({discord_id}).")

    # ── /loa status ─────────────────────────────────────────────────────────

    @loa.command(
        name="status",
        description="Show your LOA status, or another member's (officers).",
    )
    @app_commands.describe(user="Member to inspect (officers only).")
    async def status(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        target = user or interaction.user
        if user and user.id != interaction.user.id and not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(
                    "Officers only",
                    "Only officers can look up other members.",
                ),
                ephemeral=True,
            )
            return
        profile = self.bot.db.fetch_user_profile(str(target.id))
        if not profile:
            await interaction.response.send_message(
                embed=info_embed(
                    "No profile",
                    f"{target.mention} hasn't clicked the **Register** button yet.",
                ),
                ephemeral=True,
            )
            return
        until = (profile.get("loa_until") or "").strip()
        reason = (profile.get("loa_reason") or "").strip()
        today = _today_iso()
        if not until or until < today:
            await interaction.response.send_message(
                embed=info_embed(
                    "Not on LOA",
                    f"{target.mention} is not currently on leave.",
                ),
                ephemeral=True,
            )
            return
        try:
            days_left = (
                _dt.datetime.strptime(until, _ISO_DATE).date()
                - _dt.date.today()
            ).days
        except ValueError:
            days_left = None
        tail = f"\n**Reason:** {reason}" if reason else ""
        days_line = (
            f"\n**Days remaining:** {days_left}" if days_left is not None else ""
        )
        await interaction.response.send_message(
            embed=info_embed(
                f"🌴 On LOA — {target.display_name}",
                f"**Returns:** {until}{days_line}{tail}",
            ),
            ephemeral=True,
        )

    # ── /loa list ───────────────────────────────────────────────────────────

    @loa.command(
        name="list",
        description="Officers: list everyone currently on Leave of Absence.",
    )
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This is for officers."),
                ephemeral=True,
            )
            return
        rows = self.bot.db.fetch_active_loa() or []
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    "Nobody on LOA",
                    "No active leaves on file.",
                ),
                ephemeral=True,
            )
            return
        lines = [_format_loa_line(r) for r in rows[:25]]
        if len(rows) > 25:
            lines.append(f"_…and {len(rows) - 25} more._")
        await interaction.response.send_message(
            embed=info_embed(
                f"🌴 Active LOA — {len(rows)} member(s)",
                "\n".join(lines),
            ),
            ephemeral=True,
        )

    # ── /loa set ────────────────────────────────────────────────────────────

    @loa.command(
        name="set",
        description="Officers: place a member on LOA through a given date.",
    )
    @app_commands.describe(
        user="Member going on leave.",
        until="Return date in YYYY-MM-DD format.",
        reason="Optional context note.",
    )
    async def set_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        until: str,
        reason: str | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This is for officers."),
                ephemeral=True,
            )
            return
        try:
            until_date = _dt.datetime.strptime(until.strip(), _ISO_DATE).date()
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bad date",
                    "Use **YYYY-MM-DD** (example: `2026-06-15`).",
                ),
                ephemeral=True,
            )
            return
        if until_date < _dt.date.today():
            await interaction.response.send_message(
                embed=error_embed(
                    "Date in the past",
                    "Pick a return date today or later, or use `/loa end` "
                    "to clear a LOA.",
                ),
                ephemeral=True,
            )
            return
        if not self.bot.db.fetch_user_profile(str(user.id)):
            await interaction.response.send_message(
                embed=error_embed(
                    "No profile",
                    f"{user.mention} hasn't clicked the **Register** button yet.",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_loa(str(user.id), until_date.isoformat(), reason)
        await interaction.response.send_message(
            embed=success_embed(
                "LOA set",
                f"{user.mention} is on leave until **{until_date.isoformat()}**."
                + (f"\n**Reason:** {reason}" if reason else ""),
            ),
            ephemeral=True,
        )
        info_log(
            f"LOA set by {interaction.user} for {user} ({user.id}) "
            f"until {until_date} reason={reason!r}."
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(LOACog(bot))
    info_log("Initialized LOA cog.")
