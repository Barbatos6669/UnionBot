"""Cross-cog dashboards and lookups.

Commands that pull from every other cog in one shot — officer
situational awareness, guild health, member self-status, and a reverse-lookup tool.

* ``/dashboard`` — Officer at-a-glance: pending apps, pending regears,
  open LFG events, low chest items, stale recruits, members past
  Probationary threshold. Replaces 8 different status checks.
* ``/guild-health`` — Officer guild health scorecard: roster, content,
  economy, stockpile, queues, and tracking coverage.
* ``/me`` — A member's full picture: lifecycle, tenure, points, silver,
  recent regears, signups, sync state.
* ``/whois`` — Lookup a player by Albion name or Discord mention and
  return the same picture from every table the bot tracks.
"""

from __future__ import annotations

import contextlib
import datetime as _dt

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, is_officer
from time_utils import utc_now_naive

# ── helpers ──────────────────────────────────────────────────────────────────


def _safe_int(val) -> int:
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _iso_days_ago(iso: str | None) -> int | None:
    """Return whole days between ``iso`` and now (UTC). None if unparseable."""
    if not iso:
        return None
    try:
        # Tolerate both "YYYY-MM-DD HH:MM:SS" and ISO with 'T'.
        s = str(iso).replace("T", " ").split(".")[0]
        dt = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        delta = utc_now_naive() - dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        try:
            dt = _dt.datetime.strptime(str(iso)[:10], "%Y-%m-%d")
            return max(0, (utc_now_naive() - dt).days)
        except ValueError:
            return None


def _fmt_silver(n: int) -> str:
    n = int(n or 0)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}{n / 1_000:.1f}k"
    return f"{sign}{n}"


def _pct(part: int, whole: int) -> int:
    """Whole-number percentage, defensive around empty denominators."""
    try:
        whole_i = int(whole)
        if whole_i <= 0:
            return 0
        return round(int(part) * 100 / whole_i)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


def _health_emoji(score: int) -> str:
    """Traffic-light emoji for a 0-100 health score."""
    if score >= 80:
        return "🟢"
    if score >= 55:
        return "🟡"
    return "🔴"


def _health_label(score: int) -> str:
    if score >= 80:
        return "healthy"
    if score >= 55:
        return "watch"
    return "needs action"


def _queue_score(count: int, *, warn: int = 1, bad: int = 5) -> int:
    """Score a pending workload queue: empty is best, larger is worse."""
    count = max(0, int(count or 0))
    if count == 0:
        return 100
    if count <= warn:
        return 75
    if count <= bad:
        return 50
    return 25


def _score_from_pct(percent: int, *, green: int, yellow: int) -> int:
    """Map a higher-is-better percentage to a health score."""
    percent = max(0, min(100, int(percent or 0)))
    if percent >= green:
        return 100
    if percent >= yellow:
        return 65
    return 35


def _format_health_line(label: str, score: int, detail: str) -> str:
    return f"{_health_emoji(score)} **{label}:** {detail}"


def _score_color(score: int) -> discord.Color:
    if score >= 80:
        return discord.Color.green()
    if score >= 55:
        return discord.Color.gold()
    return discord.Color.red()


def _count_pending_regears(db) -> int:
    """Direct query — there's no list helper for regear status."""
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT COUNT(*) AS c FROM regear_requests WHERE status = 'pending'",
        )
        row = db.cursor.fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        error_log(f"_count_pending_regears failed: {exc!r}")
        return 0


def _count_low_chest(db, threshold: int) -> int:
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT COUNT(*) AS c FROM loadout_chest WHERE count <= ?",
            (int(threshold),),
        )
        row = db.cursor.fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        error_log(f"_count_low_chest failed: {exc!r}")
        return 0


def _count_stale_recruits(db, days: int = 7) -> int:
    """Recruits in 'contacted' or 'discord' status, last updated > N days ago."""
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT COUNT(*) AS c FROM recruits "
            "WHERE status IN ('contacted','discord') "
            "AND julianday('now') - julianday(updated_at) > ?",
            (int(days),),
        )
        row = db.cursor.fetchone()
        return int(row["c"]) if row else 0
    except Exception as exc:  # noqa: BLE001
        error_log(f"_count_stale_recruits failed: {exc!r}")
        return 0


def _count_open_lfg(db) -> int:
    try:
        rows = db.fetch_open_lfg_events() or []
        return sum(1 for r in rows if (r.get("status") or "").lower() == "open")
    except Exception:  # noqa: BLE001
        return 0


def _count_open_help_tickets(db) -> int:
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT COUNT(*) AS c FROM help_tickets "
            "WHERE status IN ('open','claimed')",
        )
        row = db.cursor.fetchone()
        return int(row["c"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def _count_silver_owed(db) -> tuple[int, int]:
    """Returns (members_owed, total_silver_owed)."""
    try:
        rows = db.fetch_silver_debts() or []
        owed = [r for r in rows if int(r.get("silver_balance") or 0) > 0]
        return len(owed), sum(int(r.get("silver_balance") or 0) for r in owed)
    except Exception:  # noqa: BLE001
        return 0, 0


def _query_scalar(db, sql: str, params: tuple = ()) -> int:
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(sql, params)
        row = db.cursor.fetchone()
        if row is None:
            return 0
        try:
            return int(row[0] or 0)
        except (KeyError, TypeError, ValueError):
            return int((row.get("n") if hasattr(row, "get") else 0) or 0)
    except Exception as exc:  # noqa: BLE001
        error_log(f"_query_scalar failed: {exc!r} sql={sql!r}")
        return 0


def _count_policy_snapshots(db) -> int:
    try:
        return len(db.fetch_all_policy_snapshots() or [])
    except Exception:  # noqa: BLE001
        return 0


def _count_ready_comps(db) -> tuple[int, int, list[str]]:
    """Return (ready, total, problem_names) using the existing chest readiness logic."""
    try:
        comps = db.list_comps() or []
    except Exception:  # noqa: BLE001
        return 0, 0, []
    ready = 0
    problems: list[str] = []
    for comp in comps:
        try:
            report = db.chest_missing_for_comp(int(comp["id"]))
        except Exception:  # noqa: BLE001
            problems.append(str(comp.get("name") or f"Comp #{comp.get('id')}"))
            continue
        short = len(report.get("shortfall") or [])
        unresolved = len(report.get("unresolved") or [])
        if short == 0 and unresolved == 0:
            ready += 1
        else:
            problems.append(str(comp.get("name") or f"Comp #{comp.get('id')}"))
    return ready, len(comps), problems[:5]


# ── cog ──────────────────────────────────────────────────────────────────────


class DashboardCog(commands.Cog):
    """One-shot summaries that read across every other cog."""

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot

    # ── /dashboard ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="dashboard",
        description="Officer at-a-glance: everything that needs attention.",
    )
    async def dashboard(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This view is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db

        pending_apps = len(db.fetch_pending_guild_applications() or [])
        pending_regears = _count_pending_regears(db)
        open_lfg = _count_open_lfg(db)
        threshold_raw = db.get_config("chest_low_stock_threshold")
        try:
            threshold = int(threshold_raw) if threshold_raw else 3
        except (TypeError, ValueError):
            threshold = 3
        low_chest = _count_low_chest(db, threshold)
        stale_recruits = _count_stale_recruits(db, 7)
        open_tickets = _count_open_help_tickets(db)
        debtors, total_owed = _count_silver_owed(db)
        try:
            active_loa = len(db.fetch_active_loa() or [])
        except Exception:  # noqa: BLE001
            active_loa = 0

        embed = discord.Embed(
            title="Officer Dashboard",
            description=(
                "Snapshot of everything that needs attention right now. "
                "Click into the linked command to drill down."
            ),
            color=discord.Color.gold(),
            timestamp=utc_now_naive(),
        )

        # Applications & recruitment
        apps_line = (
            f"**{pending_apps}** pending — `/apply pending`"
            if pending_apps else "_no pending applications_"
        )
        stale_line = (
            f"**{stale_recruits}** stale (>7d in funnel) — `/recruit list`"
            if stale_recruits else "_funnel is healthy_"
        )
        embed.add_field(
            name="📨 Applications",
            value=f"{apps_line}\n{stale_line}",
            inline=False,
        )

        # Regear & chest
        regear_line = (
            f"**{pending_regears}** pending — `/regear pending`"
            if pending_regears else "_no pending regears_"
        )
        chest_line = (
            f"**{low_chest}** item(s) at/below threshold ({threshold}) — `/chest list`"
            if low_chest else "_chest stock is healthy_"
        )
        embed.add_field(
            name="🛡️ Regear & Chest",
            value=f"{regear_line}\n{chest_line}",
            inline=False,
        )

        # Events
        lfg_line = (
            f"**{open_lfg}** open event(s)"
            if open_lfg else "_no open events — try `/schedule generate`_"
        )
        embed.add_field(
            name="📅 LFG",
            value=lfg_line,
            inline=False,
        )

        # Silver ledger
        if debtors:
            silver_line = (
                f"Guild owes **{debtors}** member(s) a total of "
                f"**{_fmt_silver(total_owed)}** silver."
            )
        else:
            silver_line = "_ledger is clear_"
        embed.add_field(
            name="💰 Silver Ledger",
            value=silver_line,
            inline=False,
        )

        # Help tickets
        if open_tickets:
            embed.add_field(
                name="🎫 Help Tickets",
                value=(
                    f"**{open_tickets}** open — `/help-ticket list`"
                ),
                inline=False,
            )

        # Leave of Absence
        if active_loa:
            embed.add_field(
                name="🌴 On Leave",
                value=(
                    f"**{active_loa}** member(s) currently on LOA — "
                    f"`/loa list`"
                ),
                inline=False,
            )

        embed.set_footer(text="UTC • Refreshed on each call")
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(f"{interaction.user} ran /dashboard.")

    # ── /guild-health ──────────────────────────────────────────────────────

    @app_commands.command(
        name="guild-health",
        description="Officer guild health scorecard using roster, content, economy, and stock data.",
    )
    async def guild_health(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This scorecard is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db
        now = utc_now_naive()
        now_iso = now.isoformat(" ", "seconds")
        since_7d = (now - _dt.timedelta(days=7)).isoformat(" ", "seconds")
        ahead_7d = (now + _dt.timedelta(days=7)).isoformat(" ", "seconds")
        since_30d = (now - _dt.timedelta(days=30)).isoformat(" ", "seconds")

        try:
            funnel = db.fetch_recruitment_funnel() or {}
        except Exception as exc:  # noqa: BLE001
            error_log(f"/guild-health recruitment funnel failed: {exc!r}")
            funnel = {}
        discord_members = _safe_int(funnel.get("discord_members"))
        registered = _safe_int(funnel.get("registered"))
        verified = _safe_int(funnel.get("verified"))
        in_home_guild = _safe_int(funnel.get("in_home_guild"))
        active_30d = _safe_int(funnel.get("active_30d"))
        active_7d = _query_scalar(
            db,
            "SELECT COUNT(*) FROM user_profiles "
            "WHERE last_activity_date >= datetime('now', '-7 days')",
        )
        home_guild = (db.get_config("home_guild_name") or "").strip()
        if home_guild:
            active_home_30d = _query_scalar(
                db,
                "SELECT COUNT(*) FROM user_profiles "
                "WHERE last_activity_date >= datetime('now', '-30 days') "
                "AND LOWER(guild_name) = LOWER(?)",
                (home_guild,),
            )
        else:
            active_home_30d = active_30d

        registered_pct = _pct(registered, discord_members)
        verified_pct = _pct(verified, registered)
        home_pct = _pct(in_home_guild, registered)
        active_home_pct = _pct(active_home_30d, in_home_guild or registered)
        roster_score = round(
            (
                _score_from_pct(registered_pct, green=70, yellow=50)
                + _score_from_pct(home_pct, green=75, yellow=50)
                + _score_from_pct(active_home_pct, green=70, yellow=45)
            ) / 3
        )

        events_7d = _query_scalar(
            db,
            "SELECT COUNT(*) FROM lfg_events "
            "WHERE starts_at >= ? AND starts_at <= ? AND status != 'cancelled'",
            (since_7d, now_iso),
        )
        upcoming_events = _query_scalar(
            db,
            "SELECT COUNT(*) FROM lfg_events "
            "WHERE starts_at > ? AND starts_at <= ? "
            "AND status IN ('open', 'scheduled')",
            (now_iso, ahead_7d),
        )
        signups_7d = _query_scalar(
            db,
            "SELECT COUNT(*) FROM lfg_signups s "
            "JOIN lfg_events e ON e.id = s.event_id "
            "WHERE e.starts_at >= ? AND e.starts_at <= ? "
            "AND e.status != 'cancelled'",
            (since_7d, now_iso),
        )
        attended_7d = _query_scalar(
            db,
            "SELECT COUNT(DISTINCT s.discord_id) FROM lfg_signups s "
            "JOIN lfg_events e ON e.id = s.event_id "
            "WHERE e.starts_at >= ? AND e.starts_at <= ? "
            "AND e.status != 'cancelled' "
            "AND s.attended = 1",
            (since_7d, now_iso),
        )
        voice_configured = bool(
            (db.get_config("automation_voice_channel_id") or "").strip()
        )
        content_pulse = events_7d + upcoming_events
        if content_pulse >= 4:
            content_score = 100
        elif content_pulse >= 2:
            content_score = 75
        elif content_pulse == 1:
            content_score = 55
        else:
            content_score = 35
        if not voice_configured:
            content_score = min(content_score, 65)

        try:
            treasury_latest = db.fetch_latest_guild_treasury()
        except Exception as exc:  # noqa: BLE001
            error_log(f"/guild-health treasury lookup failed: {exc!r}")
            treasury_latest = None
        treasury_balance = _safe_int(
            treasury_latest.get("balance") if treasury_latest else 0
        )
        treasury_date = str(treasury_latest.get("date")) if treasury_latest else ""
        treasury_age = (
            _iso_days_ago(f"{treasury_date} 00:00:00")
            if treasury_date else None
        )
        if treasury_age is None:
            treasury_score = 35
        elif treasury_age <= 1:
            treasury_score = 100
        elif treasury_age <= 3:
            treasury_score = 65
        else:
            treasury_score = 35

        try:
            debts = db.fetch_silver_debts() or []
        except Exception as exc:  # noqa: BLE001
            error_log(f"/guild-health silver debts failed: {exc!r}")
            debts = []
        guild_owes = sum(
            int(r.get("silver_balance") or 0)
            for r in debts
            if int(r.get("silver_balance") or 0) > 0
        )
        members_owe = -sum(
            int(r.get("silver_balance") or 0)
            for r in debts
            if int(r.get("silver_balance") or 0) < 0
        )
        bounty_liability = _query_scalar(
            db,
            "SELECT COALESCE(SUM(reward_points), 0) FROM bounties "
            "WHERE status IN ('open', 'claimed', 'submitted')",
        )
        exposure = guild_owes + bounty_liability
        exposure_pct = (
            _pct(exposure, treasury_balance)
            if treasury_balance else (100 if exposure else 0)
        )
        exposure_score = 100 if exposure_pct <= 5 else 65 if exposure_pct <= 15 else 35
        try:
            revenue_30d = int(db.fetch_guild_revenue_total(since_iso=since_30d) or 0)
            revenue_total = int(db.fetch_guild_revenue_total() or 0)
        except Exception as exc:  # noqa: BLE001
            error_log(f"/guild-health revenue lookup failed: {exc!r}")
            revenue_30d = 0
            revenue_total = 0
        revenue_score = 100 if revenue_30d > 0 else 65 if revenue_total > 0 else 45
        economy_score = round((treasury_score + exposure_score + revenue_score) / 3)

        threshold_raw = db.get_config("chest_low_stock_threshold")
        try:
            threshold = int(threshold_raw) if threshold_raw else 3
        except (TypeError, ValueError):
            threshold = 3
        ready_comps, total_comps, problem_comps = _count_ready_comps(db)
        low_chest = _count_low_chest(db, threshold)
        ready_pct = _pct(ready_comps, total_comps)
        stockpile_score = (
            _score_from_pct(ready_pct, green=75, yellow=45)
            if total_comps else 45
        )
        if low_chest:
            stockpile_score = min(stockpile_score, 65 if low_chest <= 5 else 45)

        pending_apps = len(db.fetch_pending_guild_applications() or [])
        staff_apps = _query_scalar(
            db,
            "SELECT COUNT(*) FROM staff_applications WHERE status = 'pending'",
        )
        pending_regears = _count_pending_regears(db)
        bounty_review = _query_scalar(
            db,
            "SELECT COUNT(*) FROM bounties "
            "WHERE status = 'pending' "
            "OR (submitted_at IS NOT NULL AND status NOT IN ('completed', 'cancelled'))",
        )
        open_tickets = _count_open_help_tickets(db)
        workload_score = round(
            (
                _queue_score(pending_apps, warn=1, bad=4)
                + _queue_score(staff_apps, warn=1, bad=3)
                + _queue_score(pending_regears, warn=1, bad=5)
                + _queue_score(bounty_review, warn=1, bad=5)
                + _queue_score(open_tickets, warn=1, bad=4)
            ) / 5
        )

        policy_snapshots = _count_policy_snapshots(db)
        gaps: list[str] = []
        if not voice_configured:
            gaps.append("voice attendance")
        if not (db.get_config("digest_channel_id") or "").strip():
            gaps.append("graph digest")
        if not (db.get_config("automation_topic_channel_id") or "").strip():
            gaps.append("topic channel")
        if policy_snapshots == 0:
            gaps.append("policy snapshots")
        if revenue_total <= 0:
            gaps.append("revenue ledger")
        data_score = 100 if not gaps else 75 if len(gaps) <= 2 else 45

        scores = [
            roster_score,
            content_score,
            economy_score,
            stockpile_score,
            workload_score,
            data_score,
        ]
        overall = round(sum(scores) / len(scores))
        embed = discord.Embed(
            title="Guild Health",
            description=(
                f"{_health_emoji(overall)} **{overall}/100** — "
                f"{_health_label(overall).title()} snapshot for "
                f"**{home_guild or 'configured home guild'}**."
            ),
            color=_score_color(overall),
            timestamp=now,
        )

        embed.add_field(
            name="Roster",
            value=_format_health_line(
                "Roster",
                roster_score,
                (
                    f"{registered}/{discord_members} registered ({registered_pct}%), "
                    f"{verified_pct}% verified, {in_home_guild} in home guild, "
                    f"{active_7d} active in 7d"
                ),
            ),
            inline=False,
        )
        embed.add_field(
            name="Content",
            value=_format_health_line(
                "Content",
                content_score,
                (
                    f"{events_7d} past 7d, {upcoming_events} next 7d, "
                    f"{signups_7d} signups, {attended_7d} attended"
                    + (
                        "; voice tracking configured"
                        if voice_configured else "; voice tracking missing"
                    )
                ),
            ),
            inline=False,
        )
        treasury_bits = (
            f"{_fmt_silver(treasury_balance)} last `{treasury_date}`"
            if treasury_latest else "no treasury snapshot"
        )
        embed.add_field(
            name="Economy",
            value=_format_health_line(
                "Economy",
                economy_score,
                (
                    f"Treasury {treasury_bits}; guild owes {_fmt_silver(guild_owes)}, "
                    f"members owe {_fmt_silver(members_owe)}, "
                    f"active bounty liability {_fmt_silver(bounty_liability)}, "
                    f"30d revenue {_fmt_silver(revenue_30d)}"
                ),
            ),
            inline=False,
        )
        comp_tail = (
            f"; check {', '.join(problem_comps[:3])}"
            if problem_comps else ""
        )
        embed.add_field(
            name="Stockpile",
            value=_format_health_line(
                "Stockpile",
                stockpile_score,
                (
                    f"{ready_comps}/{total_comps} comps ready ({ready_pct}%), "
                    f"{low_chest} chest rows at/below {threshold}{comp_tail}"
                ),
            ),
            inline=False,
        )
        embed.add_field(
            name="Queues",
            value=_format_health_line(
                "Queues",
                workload_score,
                (
                    f"apps {pending_apps}, staff {staff_apps}, regear {pending_regears}, "
                    f"bounties {bounty_review}, tickets {open_tickets}"
                ),
            ),
            inline=False,
        )
        coverage = "all core signals configured" if not gaps else ", ".join(gaps)
        embed.add_field(
            name="Data Coverage",
            value=_format_health_line("Coverage", data_score, coverage),
            inline=False,
        )

        actions: list[str] = []
        if not voice_configured:
            actions.append("Set `/automation set-voice-channel` for attendance capture.")
        if policy_snapshots == 0:
            actions.append("Run `/automation snapshot-policy` after policy channels are clean.")
        if revenue_total <= 0:
            actions.append("Use `/audit weekly-tax` with `commit:True` to start revenue history.")
        if stockpile_score < 80:
            actions.append("Run `/chest missing` for the comps called out above.")
        if content_score < 80:
            actions.append("Use `/schedule generate` or `/lfg post-board` to fill the next week.")
        if workload_score < 80:
            actions.append("Clear review queues from `/dashboard` before they pile up.")
        if roster_score < 80:
            actions.append("Review `/graph recruitment-funnel` for registration or retention leaks.")
        if not actions:
            actions.append("No urgent action. Keep the current cadence and watch the trend graphs.")
        embed.add_field(
            name="Next Actions",
            value="\n".join(f"• {line}" for line in actions[:5]),
            inline=False,
        )
        embed.set_footer(
            text="Read-only health snapshot • refresh by running the command again"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran /guild-health "
            f"(overall={overall}, roster={roster_score}, content={content_score}, "
            f"economy={economy_score}, stockpile={stockpile_score}, "
            f"workload={workload_score}, data={data_score})."
        )

    # ── /me ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="me",
        description="Show what the bot knows about you.",
    )
    async def me(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._send_member_card(interaction, interaction.user, self_view=True)

    # ── /whois ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="whois",
        description="Look up a guild member by Albion name or Discord mention (officers only).",
    )
    @app_commands.describe(
        member="Discord member to look up",
        albion_name="Albion in-game name (case-insensitive) — used when no member is given",
    )
    async def whois(
        self, interaction: discord.Interaction,
        member: discord.Member | None = None,
        albion_name: str | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This lookup is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db
        target_user: discord.abc.User | None = member
        profile: dict | None = None

        if target_user is None and albion_name:
            profile = db.fetch_profile_by_albion_name(albion_name.strip())
            if profile:
                target_user = self._resolve_user(profile.get("discord_id"))
        if target_user is None and not albion_name:
            await interaction.followup.send(
                embed=error_embed(
                    "Need a target",
                    "Pass either **member** or **albion_name**.",
                ),
                ephemeral=True,
            )
            return

        await self._send_member_card(
            interaction, target_user, self_view=False,
            preloaded_profile=profile, fallback_name=albion_name,
        )

    # ── shared rendering ────────────────────────────────────────────────────

    def _resolve_user(self, discord_id: str | None) -> discord.abc.User | None:
        if not discord_id:
            return None
        try:
            for guild in self.bot.guilds:
                m = guild.get_member(int(discord_id))
                if m:
                    return m
            return self.bot.get_user(int(discord_id))
        except (TypeError, ValueError):
            return None

    async def _send_member_card(
        self, interaction: discord.Interaction,
        user: discord.abc.User | None, *,
        self_view: bool,
        preloaded_profile: dict | None = None,
        fallback_name: str | None = None,
    ) -> None:
        db = self.bot.db

        if user is None and fallback_name:
            # We have a name but no Discord user — show a slimmer card.
            recruit = db.recruit_find_by_name(fallback_name)
            profile = preloaded_profile
            embed = discord.Embed(
                title=f"Lookup: {fallback_name}",
                color=discord.Color.dark_grey(),
                description=(
                    "No Discord profile found by that Albion name."
                    if not profile and not recruit
                    else "No Discord member is registered to this name."
                ),
            )
            if recruit:
                embed.add_field(
                    name="📋 Recruitment",
                    value=(
                        f"Status: **{recruit.get('status') or '?'}** "
                        f"(since {recruit.get('updated_at', '?')[:10]})\n"
                        f"Source: {recruit.get('source') or '?'}"
                    ),
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        assert user is not None
        discord_id = str(user.id)
        profile = preloaded_profile or db.fetch_user_profile(discord_id) or {}
        albion_name = (
            profile.get("albion_name")
            or (user.display_name if isinstance(user, discord.Member) else user.name)
        )

        embed = discord.Embed(
            title=("Your profile" if self_view else f"Profile: {user}"),
            color=discord.Color.blue(),
            timestamp=utc_now_naive(),
        )
        avatar = getattr(user, "display_avatar", None)
        if avatar:
            embed.set_thumbnail(url=avatar.url)

        # ── Identity & lifecycle
        lifecycle = profile.get("lifecycle_role") or "—"
        guild_name = profile.get("guild_name") or "—"
        member_since = profile.get("member_since") or (
            user.joined_at.isoformat(" ", "seconds")
            if isinstance(user, discord.Member) and user.joined_at else None
        )
        tenure_days = _iso_days_ago(member_since) if member_since else None
        identity_lines = [
            f"**Albion:** {albion_name}",
            f"**Guild:** {guild_name}",
            f"**Lifecycle:** {lifecycle}",
        ]
        if tenure_days is not None:
            identity_lines.append(f"**Tenure:** {tenure_days} days")
        loa_until = (profile.get("loa_until") or "").strip()
        if loa_until and loa_until >= _dt.date.today().isoformat():
            reason = (profile.get("loa_reason") or "").strip()
            tail = f" — {reason}" if reason else ""
            identity_lines.append(
                f"**🌴 On LOA until {loa_until}**{tail}"
            )
        tz_name = (profile.get("timezone") or "").strip()
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                local_now = _dt.datetime.now(ZoneInfo(tz_name)).strftime("%H:%M %Z")
                identity_lines.append(f"**Timezone:** {tz_name} (`{local_now}`)")
            except Exception:  # noqa: BLE001
                identity_lines.append(f"**Timezone:** {tz_name}")
        embed.add_field(
            name="🪪 Identity",
            value="\n".join(identity_lines),
            inline=False,
        )

        # ── Albion stats (if synced)
        if profile.get("albion_player_id"):
            ip = profile.get("average_item_power")
            kf = _safe_int(profile.get("kill_fame"))
            df = _safe_int(profile.get("death_fame"))
            embed.add_field(
                name="⚔️ Albion",
                value=(
                    f"**Kill Fame:** {kf:,}\n"
                    f"**Death Fame:** {df:,}\n"
                    f"**Avg IP:** {int(ip) if ip else '—'}"
                ),
                inline=True,
            )

        # ── Points & silver
        try:
            pts = db.get_points(discord_id) or {}
        except Exception:  # noqa: BLE001
            pts = {}
        silver = 0
        with contextlib.suppress(Exception):
            silver = db.fetch_silver_balance(discord_id)
        embed.add_field(
            name="📊 Economy",
            value=(
                f"**Points:** weekly {_safe_int(pts.get('weekly'))} • "
                f"monthly {_safe_int(pts.get('monthly'))} • "
                f"season {_safe_int(pts.get('season'))}\n"
                f"**Silver owed:** "
                + (f"**{_fmt_silver(silver)}**" if silver else "—")
            ),
            inline=False,
        )

        # ── Recruitment funnel (if tracked)
        recruit = None
        try:
            if albion_name and albion_name != "—":
                recruit = db.recruit_find_by_name(albion_name)
        except Exception:  # noqa: BLE001
            recruit = None
        if recruit:
            embed.add_field(
                name="📋 Recruitment",
                value=(
                    f"Stage: **{recruit.get('status') or '?'}** "
                    f"(updated {recruit.get('updated_at', '?')[:10]})\n"
                    f"Source: {recruit.get('source') or '?'}"
                    + (
                        f"\nRecruiter: <@{recruit['recruiter_id']}>"
                        if recruit.get("recruiter_id") else ""
                    )
                ),
                inline=False,
            )

        # ── Application history (most recent)
        try:
            pending_app = db.fetch_pending_guild_application(discord_id)
        except Exception:  # noqa: BLE001
            pending_app = None
        if pending_app:
            embed.add_field(
                name="📨 Pending application",
                value=(
                    f"#{pending_app['id']} for **{pending_app.get('albion_name')}** "
                    f"(submitted {pending_app.get('applied_at', '?')[:10]})"
                ),
                inline=False,
            )

        # ── Recent silver ledger (top 3)
        try:
            ledger = db.fetch_silver_ledger(discord_id, limit=3) or []
        except Exception:  # noqa: BLE001
            ledger = []
        if ledger:
            ledger_lines = []
            for row in ledger:
                delta = int(row.get("delta") or 0)
                sign = "+" if delta > 0 else ""
                ledger_lines.append(
                    f"`{sign}{_fmt_silver(delta)}` — {row.get('reason') or '—'} "
                    f"({(row.get('created_at') or '')[:10]})"
                )
            embed.add_field(
                name="🧾 Recent ledger",
                value="\n".join(ledger_lines),
                inline=False,
            )

        # Officer-only extras
        if not self_view:
            current = _safe_int(profile.get("activity_streak_days"))
            best = _safe_int(profile.get("activity_streak_best"))
            if current > 0 or best > 0:
                embed.add_field(
                    name="🔥 Streak",
                    value=f"Current: **{current}** • Best: **{best}**",
                    inline=True,
                )

        embed.set_footer(text=("Your data" if self_view else f"Discord ID: {discord_id}"))
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(DashboardCog(bot))
    info_log("Initialized Dashboard cog.")
