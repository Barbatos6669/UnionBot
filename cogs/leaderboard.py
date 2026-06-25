"""Leaderboard cog — top point earners by window (weekly, monthly, season).

Reads from `user_profiles.points_<window>` via `Database.top_points()`.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

from cogs._typing import Bot
import discord
from discord import app_commands
from discord.ext import commands

from debug import info_log
from utils import info_embed
from cogs.users_profile import _resolve_home_guild
from time_utils import utc_now_naive


_WINDOW_CHOICES = [
    app_commands.Choice(name="Weekly",  value="weekly"),
    app_commands.Choice(name="Monthly", value="monthly"),
    app_commands.Choice(name="Season",  value="season"),
]

_WINDOW_LABELS = {
    "weekly":  "🏆 Weekly Leaderboard",
    "monthly": "🥇 Monthly Leaderboard",
    "season":  "👑 Season Leaderboard",
}


class Leaderboard(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    @app_commands.command(name="leaderboard", description="Show the top point earners.")
    @app_commands.describe(
        window="Which leaderboard to show (default: Weekly)",
        limit="How many entries to show (1-25, default 10)",
    )
    @app_commands.choices(window=_WINDOW_CHOICES)
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        window: "Optional[app_commands.Choice[str]]" = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        win = window.value if window else "weekly"
        home_guild = _resolve_home_guild(self.bot.db)
        rows = self.bot.db.top_points(win, limit=int(limit), home_guild=home_guild)  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    _WINDOW_LABELS.get(win, "Leaderboard"),
                    "No points have been earned in this window yet.",
                ),
                ephemeral=False,
            )
            return

        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines: list[str] = []
        for i, r in enumerate(rows):
            name = r.get("albion_name") or r.get("username") or "Unknown"
            points = int(r.get("points") or 0)
            prefix = medal.get(i, f"`#{i + 1:>2}`")
            lines.append(f"{prefix} **{name}** — {points:,} pts")

        embed = discord.Embed(
            title=_WINDOW_LABELS.get(win, "Leaderboard"),
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"Top {len(rows)} • window: {win}")
        await interaction.response.send_message(embed=embed)
        info_log(f"{interaction.user} viewed /leaderboard {win} (limit={limit}).")

    @app_commands.command(
        name="streak-leaderboard",
        description="Show the longest activity streaks (current or all-time best).",
    )
    @app_commands.describe(
        mode="Active streaks (default) or all-time best.",
        limit="How many entries to show (1-25, default 10).",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Current", value="current"),
        app_commands.Choice(name="Best",    value="best"),
    ])
    async def streak_leaderboard(
        self,
        interaction: discord.Interaction,
        mode: "Optional[app_commands.Choice[str]]" = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        m = mode.value if mode else "current"
        home_guild = _resolve_home_guild(self.bot.db)
        rows = self.bot.db.top_streaks(by=m, limit=int(limit), home_guild=home_guild)  # type: ignore[attr-defined]
        title = "🔥 Active Streaks" if m == "current" else "🏅 All-Time Best Streaks"
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(title, "No streaks recorded yet."),
                ephemeral=False,
            )
            return
        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines: list[str] = []
        for i, r in enumerate(rows):
            name = r.get("albion_name") or r.get("username") or "Unknown"
            days = int((r.get("current_streak") if m == "current" else r.get("best_streak")) or 0)
            prefix = medal.get(i, f"`#{i + 1:>2}`")
            best = int(r.get("best_streak") or 0)
            extra = ""
            if m == "current" and best > days:
                extra = f" · best {best}d"
            lines.append(f"{prefix} **{name}** — {days}d{extra}")
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"Top {len(rows)} • mode: {m}")
        await interaction.response.send_message(embed=embed)
        info_log(f"{interaction.user} viewed /streak-leaderboard {m} (limit={limit}).")

    @app_commands.command(
        name="voice-leaderboard",
        description="Show who's spent the most time in voice channels.",
    )
    @app_commands.describe(
        window="7-day, 30-day, or all-time (default: 7d).",
        limit="How many entries to show (1-25, default 10).",
    )
    @app_commands.choices(window=[
        app_commands.Choice(name="7 days",   value="7d"),
        app_commands.Choice(name="30 days",  value="30d"),
        app_commands.Choice(name="All-time", value="all"),
    ])
    async def voice_leaderboard(
        self,
        interaction: discord.Interaction,
        window: "Optional[app_commands.Choice[str]]" = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        win = window.value if window else "7d"
        if win == "all":
            since = "0000-01-01"
            label = "🎤 All-Time Voice Leaderboard"
        elif win == "30d":
            since = (utc_now_naive() - _dt.timedelta(days=30)).strftime("%Y-%m-%d")
            label = "🎤 30-Day Voice Leaderboard"
        else:
            since = (utc_now_naive() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
            label = "🎤 7-Day Voice Leaderboard"

        rows = self.bot.db.top_voice(since, limit=int(limit), home_guild=_resolve_home_guild(self.bot.db))  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(label, "No voice activity recorded in this window yet."),
                ephemeral=False,
            )
            return

        def _fmt_dur(s: int) -> str:
            h, rem = divmod(int(s), 3600)
            m, _sec = divmod(rem, 60)
            if h:
                return f"{h}h {m}m"
            return f"{m}m"

        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines: list[str] = []
        for i, r in enumerate(rows):
            name = r.get("albion_name") or r.get("username") or "Unknown"
            seconds = int(r.get("seconds") or 0)
            prefix = medal.get(i, f"`#{i + 1:>2}`")
            lines.append(f"{prefix} **{name}** — {_fmt_dur(seconds)}")
        embed = discord.Embed(
            title=label,
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"Top {len(rows)} • window: {win}")
        await interaction.response.send_message(embed=embed)
        info_log(f"{interaction.user} viewed /voice-leaderboard {win} (limit={limit}).")

async def setup(bot: Bot):
    await bot.add_cog(Leaderboard(bot))
