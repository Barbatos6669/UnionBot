"""Performance reward pool backed by the guild treasury.

The intent is to reward top contributors without turning the treasury into
an open faucet. Officers preview a guarded pool first, then commit credits
to the existing silver ledger.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from cogs.users_profile import _resolve_home_guild
from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed


WINDOW_CHOICES = [
    app_commands.Choice(name="Weekly", value="weekly"),
    app_commands.Choice(name="Monthly", value="monthly"),
    app_commands.Choice(name="Season", value="season"),
]

CFG_WINDOW = "performance_rewards_window"
CFG_TOP_N = "performance_rewards_top_n"
CFG_MIN_POINTS = "performance_rewards_min_points"
CFG_POOL_PERCENT = "performance_rewards_pool_percent"
CFG_POOL_CAP = "performance_rewards_pool_cap"
CFG_RESERVE = "performance_rewards_reserve"
CFG_ROUND_TO = "performance_rewards_round_to"
CFG_LAST_PREFIX = "performance_rewards_last_commit_"

DEFAULT_WINDOW = "weekly"
DEFAULT_TOP_N = 5
DEFAULT_MIN_POINTS = 50
DEFAULT_POOL_PERCENT = 5
DEFAULT_POOL_CAP = 10_000_000
DEFAULT_RESERVE = 50_000_000
DEFAULT_ROUND_TO = 10_000


@dataclass(frozen=True)
class RewardSettings:
    window: str
    top_n: int
    min_points: int
    pool_percent: int
    pool_cap: int
    reserve: int
    round_to: int


@dataclass(frozen=True)
class RewardCandidate:
    rank: int
    discord_id: str
    name: str
    points: int
    payout: int


@dataclass(frozen=True)
class RewardSlate:
    settings: RewardSettings
    period_key: str
    treasury_balance: int
    treasury_date: str
    outstanding_guild_debt: int
    available_after_reserve: int
    pool: int
    total_points: int
    candidates: tuple[RewardCandidate, ...]
    home_guild: str | None


def _cfg_int(db, key: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = db.get_config(key)
    try:
        value = int(str(raw).replace(",", "").strip()) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


def _cfg_window(db) -> str:
    value = (db.get_config(CFG_WINDOW) or DEFAULT_WINDOW).strip().lower()
    return value if value in {"weekly", "monthly", "season"} else DEFAULT_WINDOW


def _load_settings(db, *, window: str | None = None, top_n: int | None = None) -> RewardSettings:
    selected_window = (window or _cfg_window(db)).strip().lower()
    if selected_window not in {"weekly", "monthly", "season"}:
        selected_window = DEFAULT_WINDOW
    return RewardSettings(
        window=selected_window,
        top_n=max(1, min(25, int(top_n or _cfg_int(db, CFG_TOP_N, DEFAULT_TOP_N, min_value=1, max_value=25)))),
        min_points=_cfg_int(db, CFG_MIN_POINTS, DEFAULT_MIN_POINTS, min_value=0, max_value=1_000_000),
        pool_percent=_cfg_int(db, CFG_POOL_PERCENT, DEFAULT_POOL_PERCENT, min_value=0, max_value=100),
        pool_cap=_cfg_int(db, CFG_POOL_CAP, DEFAULT_POOL_CAP, min_value=0, max_value=1_000_000_000),
        reserve=_cfg_int(db, CFG_RESERVE, DEFAULT_RESERVE, min_value=0, max_value=10_000_000_000),
        round_to=_cfg_int(db, CFG_ROUND_TO, DEFAULT_ROUND_TO, min_value=1, max_value=10_000_000),
    )


def _period_key(window: str, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    if window == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if window == "monthly":
        return f"{now.year}-{now.month:02d}"
    return "season"


def _round_down(value: int, step: int) -> int:
    step = max(1, int(step))
    return int(value) // step * step


def _silver(value: int) -> str:
    return f"{int(value):,}"


def _short_name(row: dict) -> str:
    return str(row.get("albion_name") or row.get("username") or row.get("discord_id") or "Unknown")


def _build_slate(db, *, window: str | None = None, top_n: int | None = None) -> RewardSlate | None:
    settings = _load_settings(db, window=window, top_n=top_n)
    latest = db.fetch_latest_guild_treasury()
    if not latest:
        return None

    treasury_balance = int(latest.get("balance") or 0)
    debts = db.fetch_silver_debts() or []
    outstanding = sum(max(0, int(row.get("silver_balance") or 0)) for row in debts)
    available = max(0, treasury_balance - settings.reserve - outstanding)
    pool_raw = min(settings.pool_cap, available * settings.pool_percent // 100)
    pool = _round_down(pool_raw, settings.round_to)

    home_guild = _resolve_home_guild(db)
    rows = db.top_points(settings.window, limit=settings.top_n, home_guild=home_guild) or []
    rows = [row for row in rows if int(row.get("points") or 0) >= settings.min_points]
    total_points = sum(int(row.get("points") or 0) for row in rows)

    candidates: list[RewardCandidate] = []
    if pool > 0 and total_points > 0:
        for rank, row in enumerate(rows, start=1):
            points = int(row.get("points") or 0)
            raw_share = pool * points // total_points
            payout = _round_down(raw_share, settings.round_to)
            if payout <= 0:
                continue
            candidates.append(
                RewardCandidate(
                    rank=rank,
                    discord_id=str(row.get("discord_id")),
                    name=_short_name(row),
                    points=points,
                    payout=payout,
                )
            )

    return RewardSlate(
        settings=settings,
        period_key=_period_key(settings.window),
        treasury_balance=treasury_balance,
        treasury_date=str(latest.get("date") or "unknown"),
        outstanding_guild_debt=outstanding,
        available_after_reserve=available,
        pool=pool,
        total_points=total_points,
        candidates=tuple(candidates),
        home_guild=home_guild,
    )


def _slate_embed(slate: RewardSlate, *, committed: bool = False) -> discord.Embed:
    title = "Performance Rewards Committed" if committed else "Performance Rewards Preview"
    description = (
        f"Window: **{slate.settings.window}** `{slate.period_key}`\n"
        f"Home guild filter: **{slate.home_guild or 'none'}**\n"
        f"Treasury snapshot: **{_silver(slate.treasury_balance)}** silver on `{slate.treasury_date}`\n"
        f"Reserve floor: **{_silver(slate.settings.reserve)}**\n"
        f"Existing guild debt: **{_silver(slate.outstanding_guild_debt)}**\n"
        f"Rewardable surplus: **{_silver(slate.available_after_reserve)}**\n"
        f"Pool rule: **{slate.settings.pool_percent}%** of surplus, capped at "
        f"**{_silver(slate.settings.pool_cap)}**, rounded to **{_silver(slate.settings.round_to)}**\n"
        f"Reward pool: **{_silver(sum(c.payout for c in slate.candidates))}** silver"
    )
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.green() if committed else discord.Color.blurple(),
    )

    if slate.candidates:
        lines = [
            f"`#{c.rank}` <@{c.discord_id}> `{c.name}` - {c.points:,} pts -> **{_silver(c.payout)}**"
            for c in slate.candidates
        ]
        embed.add_field(name="Payouts", value="\n".join(lines), inline=False)
    else:
        reasons: list[str] = []
        if slate.pool <= 0:
            reasons.append("No reward pool is available after reserve, debts, cap, and rounding.")
        if slate.total_points <= 0:
            reasons.append(f"No eligible players met the {slate.settings.min_points:,} point minimum.")
        embed.add_field(
            name="No payouts",
            value="\n".join(reasons) or "No eligible payouts were calculated.",
            inline=False,
        )

    embed.set_footer(text="Commit credits the silver ledger; staff still controls actual in-game payments.")
    return embed


class Rewards(commands.Cog):
    rewards = app_commands.Group(
        name="rewards",
        description="Preview and commit treasury-guarded performance rewards.",
    )

    def __init__(self, bot: Bot):
        self.bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    @rewards.command(name="config", description="Set the default performance reward rules.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        window="Default points window.",
        top_n="How many leaderboard members can qualify.",
        min_points="Minimum points needed to qualify.",
        pool_percent="Percent of rewardable surplus to use.",
        pool_cap="Maximum silver pool per reward run.",
        reserve="Treasury floor that rewards will not touch.",
        round_to="Round every payout down to this silver amount.",
    )
    @app_commands.choices(window=WINDOW_CHOICES)
    async def config(
        self,
        interaction: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
        top_n: Optional[app_commands.Range[int, 1, 25]] = None,
        min_points: Optional[app_commands.Range[int, 0, 1_000_000]] = None,
        pool_percent: Optional[app_commands.Range[int, 0, 100]] = None,
        pool_cap: Optional[app_commands.Range[int, 0, 1_000_000_000]] = None,
        reserve: Optional[app_commands.Range[int, 0, 10_000_000_000]] = None,
        round_to: Optional[app_commands.Range[int, 1, 10_000_000]] = None,
    ) -> None:
        db = self.bot.db
        if window is not None:
            db.set_config(CFG_WINDOW, window.value)
        if top_n is not None:
            db.set_config(CFG_TOP_N, str(int(top_n)))
        if min_points is not None:
            db.set_config(CFG_MIN_POINTS, str(int(min_points)))
        if pool_percent is not None:
            db.set_config(CFG_POOL_PERCENT, str(int(pool_percent)))
        if pool_cap is not None:
            db.set_config(CFG_POOL_CAP, str(int(pool_cap)))
        if reserve is not None:
            db.set_config(CFG_RESERVE, str(int(reserve)))
        if round_to is not None:
            db.set_config(CFG_ROUND_TO, str(int(round_to)))

        settings = _load_settings(db)
        body = (
            f"Window: **{settings.window}**\n"
            f"Top N: **{settings.top_n}**\n"
            f"Minimum points: **{settings.min_points:,}**\n"
            f"Pool: **{settings.pool_percent}%** of surplus, cap **{_silver(settings.pool_cap)}**\n"
            f"Reserve floor: **{_silver(settings.reserve)}**\n"
            f"Round payouts to: **{_silver(settings.round_to)}**"
        )
        await interaction.response.send_message(
            embed=success_embed("Reward rules saved", body),
            ephemeral=True,
        )
        info_log(f"{interaction.user} updated performance reward config.")

    @rewards.command(name="preview", description="Preview guarded payouts without writing the ledger.")
    @app_commands.describe(
        window="Override the configured points window.",
        top_n="Override the configured leaderboard size.",
    )
    @app_commands.choices(window=WINDOW_CHOICES)
    async def preview(
        self,
        interaction: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
        top_n: Optional[app_commands.Range[int, 1, 25]] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        slate = _build_slate(
            self.bot.db,
            window=window.value if window else None,
            top_n=int(top_n) if top_n is not None else None,
        )
        if slate is None:
            await interaction.followup.send(
                embed=error_embed(
                    "No treasury snapshot",
                    "Record the guild treasury first with `/audit treasury-record`, then preview rewards.",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=_slate_embed(slate), ephemeral=True)

    @rewards.command(name="commit", description="Credit performance rewards to member silver ledgers.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        window="Override the configured points window.",
        top_n="Override the configured leaderboard size.",
        force="Allow another payout for the same period.",
        announce="Post the committed rewards publicly in this channel.",
    )
    @app_commands.choices(window=WINDOW_CHOICES)
    async def commit(
        self,
        interaction: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
        top_n: Optional[app_commands.Range[int, 1, 25]] = None,
        force: bool = False,
        announce: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slate = _build_slate(
            self.bot.db,
            window=window.value if window else None,
            top_n=int(top_n) if top_n is not None else None,
        )
        if slate is None:
            await interaction.followup.send(
                embed=error_embed(
                    "No treasury snapshot",
                    "Record the guild treasury first with `/audit treasury-record`, then commit rewards.",
                ),
                ephemeral=True,
            )
            return
        if not slate.candidates:
            await interaction.followup.send(embed=_slate_embed(slate), ephemeral=True)
            return

        lock_key = f"{CFG_LAST_PREFIX}{slate.settings.window}"
        previous = self.bot.db.get_config(lock_key)
        if previous == slate.period_key and not force:
            await interaction.followup.send(
                embed=error_embed(
                    "Already rewarded",
                    f"`{slate.settings.window}` rewards were already committed for `{slate.period_key}`. "
                    "Use `force: True` only if staff intentionally wants a second payout.",
                ),
                ephemeral=True,
            )
            return

        ref_id = f"{slate.settings.window}:{slate.period_key}"
        failures: list[str] = []
        for candidate in slate.candidates:
            result = self.bot.db.adjust_silver_balance(
                candidate.discord_id,
                candidate.payout,
                reason=f"Performance reward ({slate.settings.window} {slate.period_key})",
                ref_type="performance_reward",
                ref_id=ref_id,
                actor_id=str(interaction.user.id),
            )
            if result is None:
                failures.append(candidate.name)

        if failures:
            error_log(f"performance rewards partial failure: {failures!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Reward commit had failures",
                    "Some ledger credits failed: " + ", ".join(failures[:10]),
                ),
                ephemeral=True,
            )
            return

        self.bot.db.set_config(lock_key, slate.period_key)
        embed = _slate_embed(slate, committed=True)
        if announce and interaction.channel is not None:
            try:
                msg = await interaction.channel.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                error_log(f"performance rewards public announcement failed: {exc!r}")
                await interaction.followup.send(
                    embed=success_embed(
                        "Rewards committed",
                        "Ledger credits were recorded, but the public announcement failed.",
                    ),
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    embed=success_embed("Rewards committed", f"Public post: {msg.jump_url}"),
                    ephemeral=True,
                )
        else:
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        info_log(
            f"{interaction.user} committed {slate.settings.window} performance rewards "
            f"{slate.period_key}: {sum(c.payout for c in slate.candidates):,} silver "
            f"to {len(slate.candidates)} member(s)."
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(Rewards(bot))
