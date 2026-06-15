from typing import Optional
from cogs._typing import Bot
import asyncio
import datetime
from collections import defaultdict

import discord
import matplotlib
import matplotlib.dates as mdates
import matplotlib.ticker
matplotlib.use("Agg")  # headless — no display needed
import matplotlib.pyplot as plt
from discord import app_commands
from discord.ext import commands
from debug import error_log, info_log
from utils import autocomplete_guild_name, error_embed, info_embed, success_embed
from cogs.users_profile import _resolve_home_guild

# ── theme + primitives (extracted to keep this file readable) ────────────────
# These helpers live in cogs/_graphs_theme.py and cogs/_graphs_primitives.py.
# The leading underscore tells the cog auto-loader in bot.py to skip them.
from cogs._graphs_theme import (
    BG_AXES, BG_FIG, GRID_COLOR, MUTED_TEXT, SPINE_COLOR,
    TEXT_COLOR, ACCENT, CANDLE_DOWN, CANDLE_FLAT, CANDLE_UP,
    PLAYER_METRICS, PLAYER_METRIC_BY_KEY, CUMULATIVE_METRICS,
)
from cogs._graphs_primitives import (
    _fmt_compact, _fig_to_file,
    _plot_series, _empty_panel,
)

# Backwards-compat alias for in-file references that pre-date the extraction.
_CUMULATIVE_METRICS = CUMULATIVE_METRICS


# ── chart builders: player / guild / hourly / K-D (extracted) ───────────────

from cogs._graphs_player import (
    _build_player_chart,
    _build_guild_chart,
    _build_hourly_bar,
    _build_kd_candles,
)


# ── tracker id helpers ────────────────────────────────────────────────────────

def _player_tracker_id(discord_id: str, stat_key: str | None) -> str:
    return f"{discord_id}:{stat_key}" if stat_key else str(discord_id)


def _split_player_tracker(raw_id: str) -> tuple[str, str | None]:
    if ":" in raw_id:
        pid, stat_key = raw_id.split(":", 1)
        return pid, stat_key
    return raw_id, None


def _activity_tracker_id(metric_key: str) -> str:
    return f"global:{metric_key}"


def _split_activity_tracker(raw_id: str) -> str:
    return raw_id.split(":", 1)[1] if ":" in raw_id else "kill_fame"


def _utc_ts() -> int:
    return int(discord.utils.utcnow().timestamp())


# ── analytics chart builders (extracted) ─────────────────────────────────────
# Pure functions: each takes rows / dicts, returns a discord.File. Live in
# cogs/_graphs_analytics.py — bot.py's auto-loader skips _*.py files.
from cogs._graphs_analytics import (
    _build_roster_chart,
    _build_content_mix_chart,
    _build_staff_funnel_chart,
    _build_movers_chart,
    _build_heatmap_chart,
    _build_dashboard_chart,
    _build_finance_dashboard_chart,
    _build_recruitment_dashboard_chart,
    _build_combat_dashboard_chart,
    _build_cohort_retention_chart,
    _build_standing_chart,
    _build_attendance_chart,
    _build_attendance_trend_chart,
    _build_recruitment_funnel_chart,
)
from cogs._primetime_claims import (
    TRACKER_TYPE as PRIME_CLAIMS_TRACKER_TYPE,
    PrimeClaimsRefreshView,
    build_prime_claims_embed,
    normalize_claim_window,
)
from cogs._timer_claims_guide import (
    TRACKER_TYPE as TIMER_CLAIM_GUIDE_TRACKER_TYPE,
    build_timer_claim_guide_embed,
)


# ── cog ───────────────────────────────────────────────────────────────────────

# Choices reused across player & activity commands
_STAT_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for label, key, _ in PLAYER_METRICS
]

_HOURLY_MODE_CHOICES = [
    app_commands.Choice(name="Avg per day (recommended)", value="avg_per_day"),
    app_commands.Choice(name="Total over window",        value="sum"),
]


class GraphGroup(app_commands.Group, name="graph", description="Stat history charts."):

    def __init__(self, bot: Bot):
        super().__init__()
        self.bot: Bot = bot

    @app_commands.command(name="player", description="Show a stat history chart for a player.")
    @app_commands.describe(
        member="The member to show (defaults to yourself)",
        stat="Show only one stat (default: all 6)",
    )
    @app_commands.choices(stat=_STAT_CHOICES)
    async def graph_player(
        self,
        interaction: discord.Interaction,
        member: "Optional[discord.Member]" = None,
        stat: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        target = member or interaction.user
        discord_id = str(target.id)

        profile = self.bot.db.fetch_user_profile(discord_id)
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{target.mention} hasn’t linked an Albion character yet."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        loop = asyncio.get_running_loop()
        rows = self.bot.db.fetch_player_history(discord_id)

        if len(rows) < 2:
            await interaction.followup.send(
                embed=info_embed(
                    "Not enough history yet",
                    "Charts populate from the hourly sync. Check back after the next sync.",
                ),
                ephemeral=True,
            )
            return

        albion_name = profile.get("albion_name", target.display_name)
        stat_key = stat.value if stat else None
        file = await loop.run_in_executor(
            None, lambda: _build_player_chart(albion_name, rows, stat_key)
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested player graph for {albion_name} "
            f"(stat={stat_key or 'all'}, {len(rows)} data points)."
        )

    @app_commands.command(name="kd", description="K/D candlestick — net Kill − Death fame, with adjustable timeframe.")
    @app_commands.describe(
        member="The member to show (defaults to yourself)",
        timeframe="Candle size (default: Daily)",
    )
    @app_commands.choices(timeframe=[
        app_commands.Choice(name="1 Hour",  value="1h"),
        app_commands.Choice(name="4 Hour",  value="4h"),
        app_commands.Choice(name="Daily",   value="1d"),
        app_commands.Choice(name="Weekly",  value="1w"),
    ])
    async def graph_kd(
        self,
        interaction: discord.Interaction,
        member: "Optional[discord.Member]" = None,
        timeframe: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        target = member or interaction.user
        discord_id = str(target.id)

        profile = self.bot.db.fetch_user_profile(discord_id)
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{target.mention} hasn’t linked an Albion character yet."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        rows = self.bot.db.fetch_player_history(discord_id)
        if len(rows) < 2:
            await interaction.followup.send(
                embed=info_embed(
                    "Not enough history yet",
                    "K/D charts need at least one full bucket of snapshots. Check back later.",
                ),
                ephemeral=True,
            )
            return

        tf = timeframe.value if timeframe else "1d"
        loop = asyncio.get_running_loop()
        albion_name = profile.get("albion_name", target.display_name)
        file = await loop.run_in_executor(None, lambda: _build_kd_candles(albion_name, rows, tf))
        await interaction.followup.send(file=file)
        info_log(f"{interaction.user} requested K/D chart for {albion_name} (tf={tf}, {len(rows)} pts).")

    @app_commands.command(name="guild", description="Show a stat history chart for a tracked guild.")
    @app_commands.describe(guild_name="The Albion guild name (must be added via /admin add-guild)")
    @app_commands.autocomplete(guild_name=autocomplete_guild_name)
    async def graph_guild(self, interaction: discord.Interaction, guild_name: str) -> None:
        await interaction.response.defer()

        loop = asyncio.get_running_loop()

        # Find the guild in the DB by name (case-insensitive)
        guilds = self.bot.db.fetch_all_guilds()
        match = next((g for g in guilds if g["guild_name"].lower() == guild_name.lower()), None)
        if not match:
            await interaction.followup.send(
                embed=error_embed(
                    "Guild not tracked",
                    f"**{guild_name}** is not in the database.",
                    hint="Ask an admin to add it with `/admin add-guild`.",
                ),
                ephemeral=True,
            )
            return

        rows = self.bot.db.fetch_guild_history(match["guild_id"])

        if len(rows) < 2:
            await interaction.followup.send(
                embed=info_embed(
                    "Not enough history yet",
                    "This guild needs at least two hourly snapshots before a chart can be drawn.",
                ),
                ephemeral=True
            )
            return

        file = await loop.run_in_executor(None, lambda: _build_guild_chart(match["guild_name"], rows))
        await interaction.followup.send(file=file)
        info_log(f"{interaction.user} requested guild graph for {match['guild_name']} ({len(rows)} data points).")

    @app_commands.command(name="track-player", description="Post a live player chart that auto-updates every hour.")
    @app_commands.describe(
        member="Player to track (defaults to yourself)",
        channel="Channel to post in (defaults to here)",
        stat="Track only one stat (default: all 6)",
    )
    @app_commands.choices(stat=_STAT_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def track_player(
        self,
        interaction: discord.Interaction,
        member: "Optional[discord.Member]" = None,
        channel: "Optional[discord.TextChannel]" = None,
        stat: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        target = member or interaction.user
        discord_id = str(target.id)
        dest = channel or interaction.channel

        profile = self.bot.db.fetch_user_profile(discord_id)
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{target.mention} hasn’t linked an Albion character yet."),
                ephemeral=True,
            )
            return

        rows = self.bot.db.fetch_player_history(discord_id)
        if len(rows) < 2:
            await interaction.response.send_message(
                embed=info_embed(
                    "Not enough history yet",
                    "Check back after the next hourly sync.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        albion_name = profile.get("albion_name", target.display_name)
        stat_key = stat.value if stat else None
        # Encode chosen stat into the tracker key so update_live_graphs can rebuild the same view.
        tracker_id = _player_tracker_id(discord_id, stat_key)
        file = await loop.run_in_executor(
            None, lambda: _build_player_chart(albion_name, rows, stat_key)
        )
        msg = await dest.send(content=f"-# Last updated: <t:{_utc_ts()}:R>", file=file)
        self.bot.db.upsert_live_graph("player", tracker_id, str(dest.id), str(msg.id))
        await interaction.followup.send(
            embed=success_embed("Live chart posted", f"Now tracking in {dest.mention} — updates every hour."),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} started live player graph for {albion_name} "
            f"(stat={stat_key or 'all'}) in #{dest.name}."
        )

    @app_commands.command(name="track-guild", description="Post a live guild chart that auto-updates every hour.")
    @app_commands.describe(guild_name="The Albion guild name", channel="Channel to post in (defaults to here)")
    @app_commands.autocomplete(guild_name=autocomplete_guild_name)
    @app_commands.default_permissions(manage_guild=True)
    async def track_guild(self, interaction: discord.Interaction, guild_name: str, channel: "Optional[discord.TextChannel]" = None) -> None:
        dest = channel or interaction.channel

        guilds = self.bot.db.fetch_all_guilds()
        match = next((g for g in guilds if g["guild_name"].lower() == guild_name.lower()), None)
        if not match:
            await interaction.response.send_message(
                embed=error_embed(
                    "Guild not tracked",
                    f"**{guild_name}** is not in the database.",
                    hint="Ask an admin to add it with `/admin add-guild`.",
                ),
                ephemeral=True,
            )
            return

        rows = self.bot.db.fetch_guild_history(match["guild_id"])
        if len(rows) < 2:
            await interaction.response.send_message(
                embed=info_embed(
                    "Not enough history yet",
                    "This guild needs at least two hourly snapshots before a chart can be drawn. Check back later.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(None, lambda: _build_guild_chart(match["guild_name"], rows))
        msg = await dest.send(content=f"-# Last updated: <t:{_utc_ts()}:R>", file=file)
        self.bot.db.upsert_live_graph("guild", match["guild_id"], str(dest.id), str(msg.id))
        await interaction.followup.send(
            embed=success_embed("Live chart posted", f"Now tracking in {dest.mention} — updates every hour."),
            ephemeral=True,
        )
        info_log(f"{interaction.user} started live guild graph for {match['guild_name']} in #{dest.name}.")

    @app_commands.command(name="untrack-player", description="Stop auto-updating a player's live chart.")
    @app_commands.describe(
        member="Player to stop tracking (defaults to yourself)",
        stat="Which stat tracker to remove (omit if it tracks all stats)",
    )
    @app_commands.choices(stat=_STAT_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def untrack_player(
        self,
        interaction: discord.Interaction,
        member: "Optional[discord.Member]" = None,
        stat: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        target = member or interaction.user
        tracker_id = _player_tracker_id(str(target.id), stat.value if stat else None)
        self.bot.db.delete_live_graph("player", tracker_id)
        await interaction.response.send_message(
            embed=success_embed("Tracking stopped", f"Live chart for {target.mention} will no longer update."),
            ephemeral=True,
        )

    @app_commands.command(name="untrack-guild", description="Stop auto-updating a guild's live chart.")
    @app_commands.describe(guild_name="The Albion guild name")
    @app_commands.autocomplete(guild_name=autocomplete_guild_name)
    @app_commands.default_permissions(manage_guild=True)
    async def untrack_guild(self, interaction: discord.Interaction, guild_name: str) -> None:
        guilds = self.bot.db.fetch_all_guilds()
        match = next((g for g in guilds if g["guild_name"].lower() == guild_name.lower()), None)
        if not match:
            await interaction.response.send_message(
                embed=error_embed("Guild not tracked", f"**{guild_name}** is not in the database."),
                ephemeral=True,
            )
            return
        self.bot.db.delete_live_graph("guild", match["guild_id"])
        await interaction.response.send_message(
            embed=success_embed("Tracking stopped", f"Live chart for **{match['guild_name']}** will no longer update."),
            ephemeral=True,
        )

    @app_commands.command(name="activity", description="Hourly chart of fame/stat earned per UTC hour-of-day.")
    @app_commands.describe(
        stat="Which stat to bucket by hour (default: Kill Fame)",
        days="Rolling window in days (1-30, default 7)",
        mode="Avg per day (default) normalizes by active days; total = raw window sum",
    )
    @app_commands.choices(stat=_STAT_CHOICES, mode=_HOURLY_MODE_CHOICES)
    async def graph_activity(
        self,
        interaction: discord.Interaction,
        stat: "Optional[app_commands.Choice[str]]" = None,
        days: app_commands.Range[int, 1, 30] = 7,
        mode: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        await interaction.response.defer()
        metric_key = stat.value if stat else "kill_fame"
        mode_key = mode.value if mode else "avg_per_day"
        rows = self.bot.db.fetch_hourly_deltas(metric_key, days=days, mode=mode_key)
        if not rows:
            await interaction.followup.send(
                embed=info_embed(
                    "Not enough history yet",
                    "The chart fills in from the hourly sync. Check back later.",
                ),
                ephemeral=True,
            )
            return
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None,
            lambda: _build_hourly_bar(metric_key, rows, days=days, mode=mode_key),
        )
        await interaction.followup.send(file=file)
        info_log(f"{interaction.user} requested hourly activity ({metric_key}, {days}d, mode={mode_key}, {len(rows)} buckets).")

    @app_commands.command(name="track-activity", description="Post a live hourly bar chart that auto-updates every hour.")
    @app_commands.describe(
        channel="Channel to post in (defaults to here)",
        stat="Which stat to bucket by hour (default: Kill Fame)",
    )
    @app_commands.choices(stat=_STAT_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def track_activity(
        self,
        interaction: discord.Interaction,
        channel: "Optional[discord.TextChannel]" = None,
        stat: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        dest = channel or interaction.channel
        metric_key = stat.value if stat else "kill_fame"
        rows = self.bot.db.fetch_hourly_deltas(metric_key, mode="avg_per_day")
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    "Not enough history yet",
                    "Check back after the next hourly sync.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(None, lambda: _build_hourly_bar(metric_key, rows, days=7, mode="avg_per_day"))
        msg = await dest.send(content=f"-# Last updated: <t:{_utc_ts()}:R>", file=file)
        # Encode metric in target_id so update_live_graphs can rebuild it.
        self.bot.db.upsert_live_graph("activity", _activity_tracker_id(metric_key), str(dest.id), str(msg.id))
        await interaction.followup.send(
            embed=success_embed("Live hourly chart posted", f"Now tracking in {dest.mention} — updates every hour."),
            ephemeral=True,
        )
        info_log(f"{interaction.user} started live hourly chart ({metric_key}) in #{dest.name}.")

    @app_commands.command(name="untrack-activity", description="Stop auto-updating a live hourly chart.")
    @app_commands.describe(stat="Which hourly chart to stop (default: Kill Fame)")
    @app_commands.choices(stat=_STAT_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def untrack_activity(
        self,
        interaction: discord.Interaction,
        stat: "Optional[app_commands.Choice[str]]" = None,
    ) -> None:
        metric_key = stat.value if stat else "kill_fame"
        self.bot.db.delete_live_graph("activity", _activity_tracker_id(metric_key))
        await interaction.response.send_message(
            f"Stopped tracking the live hourly chart ({metric_key}).", ephemeral=True
        )

    # ── analytics ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="roster",
        description="Lifecycle distribution donut + staff-rank holders bar.",
    )
    async def graph_roster(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        profiles = self.bot.db.fetch_all_registered_profiles()
        if not profiles:
            await interaction.followup.send(
                "No registered members yet.", ephemeral=True,
            )
            return
        # Count current staff-role holders by walking the guild's role list
        # rather than ``staff_role_grants`` so demotions are reflected.
        from config import STAFF_ROLES
        guild = interaction.guild
        staff_holders: dict[str, int] = {}
        if guild is not None:
            for rank in STAFF_ROLES:
                role = discord.utils.get(guild.roles, name=rank)
                staff_holders[rank] = len(role.members) if role else 0
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_roster_chart(profiles, staff_holders),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested roster chart ({len(profiles)} members)."
        )

    @app_commands.command(
        name="content-mix",
        description="Stacked weekly bar of LFG events grouped by content type.",
    )
    @app_commands.describe(weeks="How many recent weeks to show (1–26, default 8).")
    async def graph_content_mix(
        self,
        interaction: discord.Interaction,
        weeks: app_commands.Range[int, 1, 26] = 8,
    ) -> None:
        await interaction.response.defer()
        # Pull only events whose start time falls in the window; cheaper than
        # loading the whole table once it has a year of data.
        cutoff = (
            discord.utils.utcnow() - datetime.timedelta(weeks=weeks + 1)
        ).isoformat()
        try:
            if not self.bot.db.connection:
                self.bot.db.connect()
            self.bot.db.cursor.execute(
                "SELECT starts_at, event_type FROM lfg_events "
                "WHERE starts_at >= ? AND status != 'cancelled' "
                "ORDER BY starts_at ASC",
                (cutoff,),
            )
            events = [dict(r) for r in self.bot.db.cursor.fetchall()]
        except Exception as exc:
            error_log(f"content-mix query failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Query failed",
                    "The chart query hit an unexpected error. "
                    "Try again in a moment — the details are in the bot log.",
                ),
                ephemeral=True,
            )
            return
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_content_mix_chart(events, weeks),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested content-mix ({weeks}w, {len(events)} events)."
        )

    @app_commands.command(
        name="staff-funnel",
        description="Per-rank applied → approved/denied → currently-held funnel.",
    )
    async def graph_staff_funnel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        from config import STAFF_ROLES
        guild = interaction.guild
        # Aggregate applications per rank via a single grouped query.
        try:
            if not self.bot.db.connection:
                self.bot.db.connect()
            self.bot.db.cursor.execute(
                "SELECT rank, status, COUNT(*) AS n "
                "FROM staff_applications GROUP BY rank, status"
            )
            rows = self.bot.db.cursor.fetchall()
        except Exception as exc:
            error_log(f"staff-pipeline query failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Query failed",
                    "The chart query hit an unexpected error. "
                    "Try again in a moment — the details are in the bot log.",
                ),
                ephemeral=True,
            )
            return
        per_rank: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for r in rows:
            per_rank[r["rank"]][r["status"]] = r["n"]

        funnel: list[tuple[str, int, int, int, int]] = []
        for rank in STAFF_ROLES:
            approved = per_rank[rank].get("approved", 0)
            denied = per_rank[rank].get("denied", 0)
            pending = per_rank[rank].get("pending", 0)
            applied = approved + denied + pending
            held = 0
            if guild is not None:
                role = discord.utils.get(guild.roles, name=rank)
                held = len(role.members) if role else 0
            # Hide ranks with no signal at all to keep the chart legible.
            if applied or held:
                funnel.append((rank, applied, approved, denied, held))

        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_staff_funnel_chart(funnel),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested staff-funnel ({len(funnel)} ranks)."
        )

    @app_commands.command(
        name="movers",
        description="Top fame gainers in the selected window.",
    )
    @app_commands.describe(
        days="Window length in days (1–90, default 7).",
        stat="Metric to rank by (default: Kill Fame).",
        limit="How many players to show (3–20, default 10).",
    )
    @app_commands.choices(stat=_STAT_CHOICES)
    async def graph_movers(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 7,
        stat: "Optional[app_commands.Choice[str]]" = None,
        limit: app_commands.Range[int, 3, 20] = 10,
    ) -> None:
        await interaction.response.defer()
        metric_key = stat.value if stat else "kill_fame"
        metric_label = (
            stat.name if stat else PLAYER_METRIC_BY_KEY[metric_key][0]
        )
        since = (
            discord.utils.utcnow() - datetime.timedelta(days=days)
        ).isoformat()
        movers = self.bot.db.fetch_top_movers(metric_key, since, int(limit), home_guild=_resolve_home_guild(self.bot.db))
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_movers_chart(movers, metric_label, days),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested movers "
            f"({metric_key}, {days}d, {len(movers)} rows)."
        )

    @app_commands.command(
        name="heatmap",
        description="Day-of-week × hour-of-day activity heatmap (UTC).",
    )
    @app_commands.describe(days="Window length in days (1–90, default 30).")
    async def graph_heatmap(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 30,
    ) -> None:
        await interaction.response.defer()
        since = (
            discord.utils.utcnow() - datetime.timedelta(days=days)
        ).isoformat()
        rows = self.bot.db.fetch_activity_heatmap(since)
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_heatmap_chart(rows, days),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested heatmap ({days}d, {len(rows)} buckets)."
        )

    @app_commands.command(
        name="dashboard",
        description="One-image guild snapshot: roster, content, funnel, and top movers.",
    )
    @app_commands.describe(
        variant="Which dashboard to show (default: main).",
        days="Window for content mix and movers (1–90, default 30).",
    )
    @app_commands.choices(variant=[
        app_commands.Choice(name="Main (overview)", value="main"),
        app_commands.Choice(name="Finance (treasury & silver)", value="finance"),
        app_commands.Choice(name="Recruitment & retention", value="recruitment"),
        app_commands.Choice(name="Combat performance", value="combat"),
    ])
    async def graph_dashboard(
        self,
        interaction: discord.Interaction,
        variant: "Optional[app_commands.Choice[str]]" = None,
        days: app_commands.Range[int, 1, 90] = 30,
    ) -> None:
        variant_key = variant.value if variant else "main"
        await interaction.response.defer()
        try:
            file = await _render_dashboard_variant(self.bot, variant_key, int(days))
        except Exception as exc:
            error_log(f"dashboard render ({variant_key}, {days}d) failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Dashboard failed",
                    "Couldn't build that dashboard. "
                    "Check the bot log for details.",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file)
        info_log(f"{interaction.user} requested {variant_key} dashboard ({days}d).")

    @app_commands.command(
        name="track-dashboard",
        description="Post a live dashboard that auto-updates every hour.",
    )
    @app_commands.describe(
        variant="Which dashboard to post (default: main).",
        channel="Channel to post in (defaults to here)",
        days="Window for content mix and movers (1–90, default 30).",
    )
    @app_commands.choices(variant=[
        app_commands.Choice(name="Main (overview)", value="main"),
        app_commands.Choice(name="Finance (treasury & silver)", value="finance"),
        app_commands.Choice(name="Recruitment & retention", value="recruitment"),
        app_commands.Choice(name="Combat performance", value="combat"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def track_dashboard(
        self,
        interaction: discord.Interaction,
        variant: "Optional[app_commands.Choice[str]]" = None,
        channel: "Optional[discord.TextChannel]" = None,
        days: app_commands.Range[int, 1, 90] = 30,
    ) -> None:
        variant_key = variant.value if variant else "main"
        dest = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)
        try:
            file = await _render_dashboard_variant(self.bot, variant_key, int(days))
        except Exception as exc:
            error_log(f"dashboard post ({variant_key}, {days}d) failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Dashboard failed",
                    "Couldn't build that dashboard. "
                    "Check the bot log for details.",
                ),
                ephemeral=True,
            )
            return
        msg = await dest.send(
            content=f"-# Last updated: <t:{_utc_ts()}:R>",
            file=file,
            view=DashboardRefreshView(),
        )
        self.bot.db.upsert_live_graph(
            "dashboard",
            _dashboard_target_id(variant_key, int(days)),
            str(dest.id), str(msg.id),
        )
        pretty = {"main": "Main", "finance": "Finance",
                  "recruitment": "Recruitment & retention",
                  "combat": "Combat performance"}[variant_key]
        await interaction.followup.send(
            embed=success_embed(
                "Live dashboard posted",
                f"**{pretty}** now tracking in {dest.mention} — auto-refreshes every sync tick.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} started live {variant_key} dashboard ({days}d) in #{dest.name}."
        )

    @app_commands.command(
        name="untrack-dashboard",
        description="Stop auto-updating a live dashboard.",
    )
    @app_commands.describe(
        variant="Which dashboard to stop (default: main).",
        days="Window of the dashboard to stop (default 30).",
    )
    @app_commands.choices(variant=[
        app_commands.Choice(name="Main", value="main"),
        app_commands.Choice(name="Finance", value="finance"),
        app_commands.Choice(name="Recruitment & retention", value="recruitment"),
        app_commands.Choice(name="Combat performance", value="combat"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    async def untrack_dashboard(
        self,
        interaction: discord.Interaction,
        variant: "Optional[app_commands.Choice[str]]" = None,
        days: app_commands.Range[int, 1, 90] = 30,
    ) -> None:
        variant_key = variant.value if variant else "main"
        # Try both the new "<variant>:<days>" key and the legacy bare-int key
        # (for main-variant trackers created before variants existed).
        self.bot.db.delete_live_graph(
            "dashboard", _dashboard_target_id(variant_key, int(days)),
        )
        if variant_key == "main":
            self.bot.db.delete_live_graph("dashboard", str(int(days)))
        await interaction.response.send_message(
            embed=success_embed(
                "Tracking stopped",
                f"Live **{variant_key}** dashboard ({days}d) will no longer update.",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="cohort",
        description="Retention of registration cohorts: % active per week after joining.",
    )
    @app_commands.describe(
        weeks_back="How far back to pull cohorts (1–26, default 8).",
        retention_weeks="How many follow-up weeks per cohort (1–12, default 8).",
    )
    async def graph_cohort(
        self,
        interaction: discord.Interaction,
        weeks_back: app_commands.Range[int, 1, 26] = 8,
        retention_weeks: app_commands.Range[int, 1, 12] = 8,
    ) -> None:
        await interaction.response.defer()
        cohorts = self.bot.db.fetch_cohort_retention(
            int(weeks_back), int(retention_weeks),
        )
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None,
            lambda: _build_cohort_retention_chart(cohorts, int(retention_weeks)),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested cohort retention "
            f"({weeks_back}w back, {retention_weeks}w follow-up, {len(cohorts)} cohorts)."
        )

    @app_commands.command(
        name="standing",
        description="Where a member ranks vs. the whole guild for a stat in a window.",
    )
    @app_commands.describe(
        member="The member to highlight (defaults to yourself).",
        stat="Stat to rank by (default: Kill Fame).",
        days="Window in days (1–90, default 7).",
    )
    @app_commands.choices(stat=_STAT_CHOICES)
    async def graph_standing(
        self,
        interaction: discord.Interaction,
        member: "Optional[discord.Member]" = None,
        stat: "Optional[app_commands.Choice[str]]" = None,
        days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        target = member or interaction.user
        metric = stat.value if stat else "kill_fame"
        label = stat.name if stat else "Kill Fame"
        await interaction.response.defer()
        since = (
            discord.utils.utcnow() - datetime.timedelta(days=int(days))
        ).isoformat()
        # Pull every mover, not just the top 10.
        movers = self.bot.db.fetch_top_movers(metric, since, 10000, home_guild=_resolve_home_guild(self.bot.db))
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None,
            lambda: _build_standing_chart(
                movers, str(target.id), label, int(days),
            ),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested standing for {target} "
            f"({metric}, {days}d, {len(movers)} candidates)."
        )

    @app_commands.command(
        name="attendance",
        description="Attendance funnel for a single LFG event.",
    )
    @app_commands.describe(event_id="The LFG event ID.")
    async def graph_attendance(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        await interaction.response.defer()
        event = self.bot.db.fetch_lfg_event(int(event_id))
        if not event:
            await interaction.followup.send(
                embed=error_embed("Not found", f"No LFG event with id {event_id}."),
                ephemeral=True,
            )
            return
        counts = self.bot.db.fetch_event_attendance(int(event_id))
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_attendance_chart(event, counts),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested attendance for event #{event_id}."
        )

    @app_commands.command(
        name="attendance-trend",
        description="Show-up rate for marked LFG events over time.",
    )
    @app_commands.describe(weeks="Window in weeks (1–26, default 8).")
    async def graph_attendance_trend(
        self,
        interaction: discord.Interaction,
        weeks: app_commands.Range[int, 1, 26] = 8,
    ) -> None:
        await interaction.response.defer()
        since = (
            discord.utils.utcnow() - datetime.timedelta(weeks=int(weeks))
        ).isoformat()
        rows = self.bot.db.fetch_attendance_trend(since)
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_attendance_trend_chart(rows, int(weeks)),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested attendance trend "
            f"({weeks}w, {len(rows)} events)."
        )

    @app_commands.command(
        name="recruitment-funnel",
        description="Discord → registered → verified → in-guild → active conversion.",
    )
    async def graph_recruitment_funnel(
        self, interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer()
        funnel = self.bot.db.fetch_recruitment_funnel()
        loop = asyncio.get_running_loop()
        file = await loop.run_in_executor(
            None, lambda: _build_recruitment_funnel_chart(funnel),
        )
        await interaction.followup.send(file=file)
        info_log(
            f"{interaction.user} requested recruitment funnel "
            f"({funnel.get('discord_members', 0)} → {funnel.get('active_30d', 0)})."
        )

    @app_commands.command(
        name="set-digest-channel",
        description="Set the channel that receives the weekly Monday digest.",
    )
    @app_commands.describe(channel="Channel to receive the weekly digest.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_digest_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config("digest_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Weekly digest configured",
                f"Digest will post to {channel.mention} every Monday at noon UTC.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set digest channel → #{channel.name}.")


async def _render_dashboard(bot: Bot, days: int) -> discord.File:
    """Gather all data + render the 2x2 dashboard. Shared by command, live
    update loop, and weekly digest."""
    from config import STAFF_ROLES
    db = bot.db

    profiles = db.fetch_all_registered_profiles()
    staff_holders: dict[str, int] = {}
    for guild in bot.guilds:
        for rank in STAFF_ROLES:
            role = discord.utils.get(guild.roles, name=rank)
            if role:
                staff_holders[rank] = max(staff_holders.get(rank, 0), len(role.members))
        if staff_holders:
            break

    weeks = max(1, (days // 7) + 1)
    cutoff = (
        discord.utils.utcnow() - datetime.timedelta(weeks=weeks + 1)
    ).isoformat()
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "SELECT starts_at, event_type FROM lfg_events "
        "WHERE starts_at >= ? AND status != 'cancelled' ORDER BY starts_at ASC",
        (cutoff,),
    )
    events = [dict(r) for r in db.cursor.fetchall()]

    db.cursor.execute(
        "SELECT rank, status, COUNT(*) AS n FROM staff_applications GROUP BY rank, status"
    )
    funnel_rows = db.cursor.fetchall()
    per_rank: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in funnel_rows:
        per_rank[r["rank"]][r["status"]] = r["n"]
    funnel: list[tuple[str, int, int, int, int]] = []
    for rank in STAFF_ROLES:
        approved = per_rank[rank].get("approved", 0)
        denied = per_rank[rank].get("denied", 0)
        pending = per_rank[rank].get("pending", 0)
        held = staff_holders.get(rank, 0)
        if approved + denied + pending or held:
            funnel.append(
                (rank, approved + denied + pending, approved, denied, held)
            )

    since = (
        discord.utils.utcnow() - datetime.timedelta(days=days)
    ).isoformat()
    movers = db.fetch_top_movers("kill_fame", since, 10, home_guild=_resolve_home_guild(db))

    silver = db.fetch_silver_top(limit=5, home_guild=_resolve_home_guild(db))
    lifecycle_weekly = db.fetch_lifecycle_weekly(days=days)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _build_dashboard_chart(
            profiles, staff_holders, events, weeks, funnel, movers, days,
            silver=silver, lifecycle_weekly=lifecycle_weekly,
        ),
    )


# ── Dashboard variants ────────────────────────────────────────────────────────
# Each takes the bot + window in days and returns a discord.File. The cog
# dispatches to one of these based on the tracker's `target_id` prefix
# (e.g. "finance:30", "recruitment:14"). Variant "main" (or a bare int) routes
# back to `_render_dashboard` for backwards compatibility.

_DASHBOARD_VARIANTS = ("main", "finance", "recruitment", "combat")


def _parse_dashboard_target(target_id: str) -> tuple[str, int]:
    """Decode a dashboard tracker ``target_id`` into ``(variant, days)``.

    Accepts:
    - ``"30"`` → main variant, 30 days (legacy format)
    - ``"finance:14"`` → finance variant, 14 days
    """
    variant = "main"
    days_str = target_id
    if ":" in target_id:
        variant, days_str = target_id.split(":", 1)
    if variant not in _DASHBOARD_VARIANTS:
        variant = "main"
    try:
        days = max(1, min(90, int(days_str)))
    except (TypeError, ValueError):
        days = 30
    return variant, days


def _dashboard_target_id(variant: str, days: int) -> str:
    return f"{variant}:{int(days)}"


async def _render_finance_dashboard(bot: Bot, days: int) -> discord.File:
    db = bot.db
    since = (discord.utils.utcnow() - datetime.timedelta(days=days)).isoformat()
    home_guild = _resolve_home_guild(db)
    treasury_latest = db.fetch_latest_guild_treasury()
    treasury_history = db.fetch_guild_treasury_history(days=days)
    silver_debts = db.fetch_silver_debts()
    unpaid_aged = db.fetch_unpaid_silver_aged(min_age_days=1, home_guild=home_guild)
    revenue_rows = db.fetch_recent_guild_revenue(limit=200)
    revenue_30d = db.fetch_guild_revenue_total(since_iso=since)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _build_finance_dashboard_chart(
            treasury_latest=treasury_latest,
            treasury_history=treasury_history,
            silver_debts=silver_debts,
            unpaid_aged=unpaid_aged,
            revenue_rows=revenue_rows,
            revenue_30d=revenue_30d,
            days=days,
        ),
    )


async def _render_recruitment_dashboard(bot: Bot, days: int) -> discord.File:
    db = bot.db
    home_guild = _resolve_home_guild(db)
    funnel = db.fetch_recruitment_funnel()
    weeks_back = max(4, min(12, (days // 7) + 2))
    cohorts = db.fetch_cohort_retention(weeks_back=weeks_back, retention_weeks=weeks_back)
    profiles = db.fetch_all_registered_profiles()
    lifecycle_weekly = db.fetch_lifecycle_weekly(days=days)
    pending_apps = db.fetch_pending_guild_applications()
    threshold_30 = (discord.utils.utcnow() - datetime.timedelta(days=30)).isoformat()
    threshold_60 = (discord.utils.utcnow() - datetime.timedelta(days=60)).isoformat()
    inactive_30d = db.fetch_inactive_profiles(threshold_30, home_guild=home_guild)
    inactive_60d = db.fetch_inactive_profiles(threshold_60, home_guild=home_guild)
    streaks = db.top_streaks(by="current", limit=10)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _build_recruitment_dashboard_chart(
            funnel=funnel,
            cohorts=cohorts,
            profiles=profiles,
            lifecycle_weekly=lifecycle_weekly,
            pending_apps=pending_apps,
            inactive_30d=inactive_30d,
            inactive_60d=inactive_60d,
            streaks=streaks,
            days=days,
        ),
    )


async def _render_combat_dashboard(bot: Bot, days: int) -> discord.File:
    db = bot.db
    home_guild = _resolve_home_guild(db)
    since = (discord.utils.utcnow() - datetime.timedelta(days=days)).isoformat()
    profiles = db.fetch_all_registered_profiles()
    movers = db.fetch_top_movers("kill_fame", since, 10, home_guild=home_guild)
    heatmap_rows = db.fetch_activity_heatmap(since)
    hourly_rows = db.fetch_hourly_deltas("kill_fame", days=days, mode="avg_per_day")
    attendance_rows = db.fetch_attendance_trend(since)
    voice_rows = db.top_voice(since, limit=10, home_guild=home_guild)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _build_combat_dashboard_chart(
            profiles=profiles,
            movers=movers,
            heatmap_rows=heatmap_rows,
            hourly_rows=hourly_rows,
            attendance_rows=attendance_rows,
            voice_rows=voice_rows,
            days=days,
        ),
    )


async def _render_dashboard_variant(
    bot: Bot, variant: str, days: int,
) -> discord.File:
    """Single entry point: dispatch to the right dashboard builder."""
    if variant == "finance":
        return await _render_finance_dashboard(bot, days)
    if variant == "recruitment":
        return await _render_recruitment_dashboard(bot, days)
    if variant == "combat":
        return await _render_combat_dashboard(bot, days)
    return await _render_dashboard(bot, days)


async def update_live_graphs(bot: Bot) -> None:
    """Called by the hourly sync loop to refresh all tracked live graph messages."""
    trackers = bot.db.fetch_all_live_graphs()
    if not trackers:
        return

    loop = asyncio.get_running_loop()
    timestamp = _utc_ts()

    for tracker in trackers:
        try:
            channel = bot.get_channel(int(tracker["channel_id"]))
            if not channel:
                continue
            try:
                message = await channel.fetch_message(int(tracker["message_id"]))
            except discord.NotFound:
                bot.db.delete_live_graph(tracker["type"], tracker["target_id"])
                continue

            if tracker["type"] == "player":
                pid, stat_key = _split_player_tracker(tracker["target_id"])
                profile = bot.db.fetch_user_profile(pid)
                if not profile:
                    continue
                rows = bot.db.fetch_player_history(pid)
                if len(rows) < 2:
                    continue
                albion_name = profile.get("albion_name", pid)
                file = await loop.run_in_executor(
                    None,
                    lambda n=albion_name, r=rows, s=stat_key: _build_player_chart(n, r, s),
                )
            elif tracker["type"] == "guild":
                guild_row = bot.db.fetch_guild(tracker["target_id"])
                if not guild_row:
                    continue
                rows = bot.db.fetch_guild_history(tracker["target_id"])
                if len(rows) < 2:
                    continue
                gname = guild_row["guild_name"]
                file = await loop.run_in_executor(None, lambda n=gname, r=rows: _build_guild_chart(n, r))
            elif tracker["type"] == "dashboard":
                # target_id format: "<variant>:<days>" (e.g. "finance:30")
                # Legacy bare-int target_ids ("30") still resolve to the
                # main dashboard for backwards compatibility.
                variant, days = _parse_dashboard_target(tracker["target_id"])
                file = await _render_dashboard_variant(bot, variant, days)
            elif tracker["type"] == PRIME_CLAIMS_TRACKER_TYPE:
                window = normalize_claim_window(tracker["target_id"])
                embed = build_prime_claims_embed(bot, window)
                view = PrimeClaimsRefreshView()
                try:
                    await message.edit(
                        content=f"-# Last updated: <t:{timestamp}:R>",
                        embed=embed,
                        attachments=[],
                        view=view,
                    )
                    info_log(f"Updated live graph (edit): {tracker['type']} {window}")
                except (discord.NotFound, discord.HTTPException):
                    import contextlib

                    with contextlib.suppress(discord.HTTPException):
                        await message.delete()
                    new_msg = await channel.send(
                        content=f"-# Last updated: <t:{timestamp}:R>",
                        embed=embed,
                        view=view,
                    )
                    bot.db.upsert_live_graph(
                        tracker["type"], tracker["target_id"],
                        str(channel.id), str(new_msg.id),
                    )
                    info_log(f"Updated live graph (resend): {tracker['type']} {window}")
                continue
            elif tracker["type"] == TIMER_CLAIM_GUIDE_TRACKER_TYPE:
                try:
                    await message.edit(embed=build_timer_claim_guide_embed(bot.db))
                    info_log("Updated live Timer Claim System guide.")
                except (discord.NotFound, discord.HTTPException):
                    import contextlib

                    with contextlib.suppress(discord.HTTPException):
                        await message.delete()
                    new_msg = await channel.send(embed=build_timer_claim_guide_embed(bot.db))
                    bot.db.upsert_live_graph(
                        tracker["type"], tracker["target_id"],
                        str(channel.id), str(new_msg.id),
                    )
                    info_log("Resent live Timer Claim System guide.")
                continue
            else:  # activity — target_id is "global:<metric_key>" (legacy "global" → kill_fame)
                metric_key = _split_activity_tracker(tracker["target_id"])
                rows = bot.db.fetch_hourly_deltas(metric_key, mode="avg_per_day")
                if not rows:
                    continue
                file = await loop.run_in_executor(
                    None, lambda m=metric_key, r=rows: _build_hourly_bar(m, r, mode="avg_per_day")
                )

            # Edit the existing message in place. This keeps the dashboard
            # pinned to its current channel position (no scroll churn), and
            # preserves the persistent refresh button on dashboard trackers.
            view = DashboardRefreshView() if tracker["type"] == "dashboard" else discord.utils.MISSING
            try:
                await message.edit(
                    content=f"-# Last updated: <t:{timestamp}:R>",
                    attachments=[file],
                    view=view,
                )
                info_log(f"Updated live graph (edit): {tracker['type']} {tracker['target_id']}")
            except (discord.NotFound, discord.HTTPException):
                # Message was deleted by a mod, or edit unexpectedly failed —
                # fall back to delete+resend so the tracker self-heals. The
                # file's underlying BytesIO was already read by the failed
                # edit, so rewind it before re-uploading.
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                try:
                    file.fp.seek(0)
                except Exception:
                    pass
                new_msg = await channel.send(
                    content=f"-# Last updated: <t:{timestamp}:R>",
                    file=file,
                    view=view if view is not discord.utils.MISSING else None,
                )
                bot.db.upsert_live_graph(tracker["type"], tracker["target_id"], str(channel.id), str(new_msg.id))
                info_log(f"Updated live graph (resend): {tracker['type']} {tracker['target_id']}")

        except Exception as e:
            info_log(f"Failed to update live graph {tracker['type']} {tracker['target_id']}: {e}")

    # ── Weekly digest (Mondays around noon UTC) ─────────────────────────────
    try:
        await _maybe_post_weekly_digest(bot)
    except Exception as e:
        info_log(f"Weekly digest hook failed: {e}")


async def _maybe_post_weekly_digest(bot: Bot) -> None:
    """Post a guild summary every Monday between 12:00–12:59 UTC. Idempotent
    via guild_config['digest_last_sent'] = '<isoyear>-W<week>'."""
    db = bot.db
    channel_id = db.get_config("digest_channel_id")
    if not channel_id:
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() != 0 or now.hour != 12:
        return
    iso_year, iso_week, _ = now.isocalendar()
    tag = f"{iso_year}-W{iso_week:02d}"
    if db.get_config("digest_last_sent") == tag:
        return

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    days = 7
    file = await _render_dashboard(bot, days)

    # Summary numbers: events run, new members, top mover, total kill fame.
    since = (now - datetime.timedelta(days=days)).isoformat()
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "SELECT COUNT(*) AS n FROM lfg_events "
        "WHERE starts_at >= ? AND status != 'cancelled'",
        (since,),
    )
    events_run = int((db.cursor.fetchone() or {"n": 0})["n"] or 0)
    db.cursor.execute(
        "SELECT COUNT(*) AS n FROM user_profiles WHERE verified_date >= ?",
        (since,),
    )
    new_members = int((db.cursor.fetchone() or {"n": 0})["n"] or 0)

    movers = db.fetch_top_movers("kill_fame", since, 1, home_guild=_resolve_home_guild(db))
    top_line = "—"
    if movers:
        m = movers[0]
        top_line = f"**{m.get('name') or m['discord_id']}** — {_fmt_compact(int(m['delta']))} kill fame"

    total_fame = sum(int(m.get("delta") or 0)
                     for m in db.fetch_top_movers("kill_fame", since, 10000, home_guild=_resolve_home_guild(db)))

    embed = discord.Embed(
        title=f"Weekly Digest  •  {tag}",
        description=(
            f"**Events run:** {events_run}\n"
            f"**New members:** {new_members}\n"
            f"**Top performer:** {top_line}\n"
            f"**Guild kill fame (7d):** {_fmt_compact(total_fame)}"
        ),
        color=int(ACCENT.lstrip('#'), 16),
    )
    embed.set_image(url="attachment://dashboard.png")
    try:
        await channel.send(embed=embed, file=file)
        db.set_config("digest_last_sent", tag)
        info_log(f"Posted weekly digest to #{channel.name} ({tag}).")
    except discord.HTTPException as e:
        info_log(f"Weekly digest send failed: {e}")


def render_treasury_graph(rows: list[dict]) -> discord.File:
    """Render a daily-snapshot line chart of the in-game guild treasury.
    Each row is a dict with `date` (YYYY-MM-DD) and `balance` (int)."""
    if not rows:
        fig, ax = plt.subplots(figsize=(10, 4.5), constrained_layout=True)
        fig.patch.set_facecolor(BG_FIG)
        fig.suptitle("Guild Treasury", fontsize=15, fontweight="700",
                     color=TEXT_COLOR, x=0.02, ha="left")
        _empty_panel(ax, "No treasury snapshots yet")
        return _fig_to_file(fig, "treasury.png")

    rows = sorted(rows, key=lambda r: str(r.get("date") or ""))
    dates = [datetime.datetime.strptime(r["date"], "%Y-%m-%d") for r in rows]
    values = [int(r["balance"]) for r in rows]
    deltas = [0] + [curr - prev for prev, curr in zip(values, values[1:])]

    first = values[0]
    latest = values[-1]
    high = max(values)
    low = min(values)
    delta = latest - first if len(values) > 1 else 0
    pct = (delta / first * 100) if first else 0.0
    sign = "+" if delta >= 0 else "-"
    trend_color = CANDLE_UP if delta > 0 else CANDLE_DOWN if delta < 0 else CANDLE_FLAT

    fig = plt.figure(figsize=(12, 6.2), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    gs = fig.add_gridspec(2, 1, height_ratios=[3.4, 1.15], hspace=0.08)
    ax = fig.add_subplot(gs[0])
    ax_flow = fig.add_subplot(gs[1], sharex=ax)

    fig.suptitle(
        f"Guild Treasury / Silver  -  {_fmt_compact(latest)}  "
        f"{sign}{_fmt_compact(abs(delta))} ({sign}{abs(pct):.1f}%)",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )
    fig.text(
        0.02, 0.91,
        (
            f"Open {_fmt_compact(first)}  |  High {_fmt_compact(high)}  |  "
            f"Low {_fmt_compact(low)}  |  Last {dates[-1].strftime('%b %d')}  |  "
            f"{len(rows)} snapshot(s)"
        ),
        ha="left", va="center", fontsize=8.5, color=MUTED_TEXT, fontweight="600",
    )

    for axis in (ax, ax_flow):
        axis.set_facecolor(BG_AXES)
        axis.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        axis.set_axisbelow(True)
        axis.tick_params(colors=MUTED_TEXT, labelsize=8, length=0)
        axis.yaxis.tick_right()
        axis.yaxis.set_label_position("right")
        for side in ("top", "left"):
            axis.spines[side].set_visible(False)
        for side in ("right", "bottom"):
            axis.spines[side].set_color(SPINE_COLOR)
            axis.spines[side].set_linewidth(0.8)
        axis.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))

    # Draw each daily segment in its own gain/loss color, closer to a market
    # equity curve than a filled-to-zero area chart.
    if len(dates) == 1:
        ax.scatter(dates, values, s=42, color=trend_color, zorder=4)
    else:
        for prev_dt, curr_dt, prev_val, curr_val in zip(dates, dates[1:], values, values[1:]):
            seg_color = CANDLE_UP if curr_val > prev_val else CANDLE_DOWN if curr_val < prev_val else CANDLE_FLAT
            ax.plot(
                [prev_dt, curr_dt], [prev_val, curr_val],
                color=seg_color, linewidth=2.4, solid_capstyle="round", zorder=3,
            )
        point_colors = [
            CANDLE_FLAT if i == 0 else CANDLE_UP if values[i] > values[i - 1]
            else CANDLE_DOWN if values[i] < values[i - 1] else CANDLE_FLAT
            for i in range(len(values))
        ]
        ax.scatter(dates, values, s=22, color=point_colors, zorder=4)

    ax.axhline(first, color=MUTED_TEXT, linestyle="--", linewidth=0.9, alpha=0.45, zorder=1)
    ax.fill_between(dates, values, first, color=trend_color, alpha=0.07, linewidth=0, zorder=2)
    ax.set_title("Balance trend", color=TEXT_COLOR, fontsize=10, fontweight="700", loc="left", pad=8)

    span = max(high - low, abs(latest) * 0.02, 1)
    ax.set_ylim(low - span * 0.25, high + span * 0.35)
    ax.margins(x=0.04)
    ax.annotate(
        _fmt_compact(latest),
        xy=(dates[-1], latest),
        xytext=(9, 0),
        textcoords="offset points",
        ha="left",
        va="center",
        color="white",
        fontsize=8,
        fontweight="700",
        bbox=dict(boxstyle="round,pad=0.28", facecolor=trend_color, edgecolor="none"),
        zorder=5,
        clip_on=False,
    )
    ax.tick_params(axis="x", labelbottom=False)

    bar_colors = [
        CANDLE_UP if d > 0 else CANDLE_DOWN if d < 0 else CANDLE_FLAT
        for d in deltas
    ]
    ax_flow.bar(dates, deltas, width=0.72, color=bar_colors, alpha=0.86, edgecolor="none", zorder=3)
    ax_flow.axhline(0, color=SPINE_COLOR, linewidth=0.9, zorder=2)
    ax_flow.set_title("Daily net flow", color=TEXT_COLOR, fontsize=10, fontweight="700", loc="left", pad=6)
    flow_span = max((abs(d) for d in deltas), default=0)
    flow_span = max(flow_span, 1)
    ax_flow.set_ylim(-flow_span * 1.25, flow_span * 1.25)

    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax_flow.xaxis.set_major_locator(locator)
    ax_flow.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, show_offset=False))
    return _fig_to_file(fig, "treasury.png")


class DashboardRefreshView(discord.ui.View):
    """Persistent view attached to live-dashboard messages.

    Adds a single "🔄 Refresh now" button that re-renders the dashboard in
    place. Uses a 30-second per-user cooldown to prevent spam. The view is
    timeout-less and registered as persistent in setup() so it survives bot
    restarts — the days window is parsed back out of the message content.
    """

    _COOLDOWN_SECS = 30

    def __init__(self) -> None:
        super().__init__(timeout=None)
        # discord_id -> last_refresh_ts; tiny in-memory state, fine to lose on restart.
        self._last_refresh: dict[int, float] = {}

    @discord.ui.button(
        label="Refresh now",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="graphs:dashboard:refresh",
    )
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        import time
        bot = interaction.client  # type: ignore[assignment]
        now = time.monotonic()
        last = self._last_refresh.get(interaction.user.id, 0.0)
        wait = self._COOLDOWN_SECS - (now - last)
        if wait > 0:
            await interaction.response.send_message(
                embed=error_embed("Slow down", f"Try again in {wait:.0f}s."),
                ephemeral=True,
            )
            return
        self._last_refresh[interaction.user.id] = now

        # Recover the variant + days window from the trackers table by matching
        # the live message id. Fall back to main / 30d if not found.
        variant = "main"
        days = 30
        try:
            for tracker in bot.db.fetch_all_live_graphs():  # type: ignore[attr-defined]
                if (tracker["type"] == "dashboard"
                        and str(tracker["message_id"]) == str(interaction.message.id)):
                    variant, days = _parse_dashboard_target(tracker["target_id"])
                    break
        except Exception:
            pass

        await interaction.response.defer()
        try:
            file = await _render_dashboard_variant(bot, variant, days)  # type: ignore[arg-type]
        except Exception as exc:
            error_log(f"dashboard refresh ({variant}, {days}d) failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Refresh failed",
                    "Couldn't rebuild the dashboard. "
                    "Check the bot log for details.",
                ),
                ephemeral=True,
            )
            return
        await interaction.message.edit(
            content=f"-# Last updated: <t:{_utc_ts()}:R> · by {interaction.user.mention}",
            attachments=[file],
            view=self,
        )


class Graphs(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.bot.tree.add_command(GraphGroup(bot))
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:
        # Reload-safety: drop the manually-added group so the next setup()
        # doesn't hit CommandAlreadyRegistered.
        try:
            self.bot.tree.remove_command("graph")
        except Exception:  # noqa: BLE001
            pass


async def setup(bot: Bot):
    await bot.add_cog(Graphs(bot))
    # Register the dashboard refresh view as persistent so the button survives
    # bot restarts. Discord matches it by custom_id ("graphs:dashboard:refresh").
    bot.add_view(DashboardRefreshView())
