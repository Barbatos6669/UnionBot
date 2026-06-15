"""Officer dashboard embed + view for ``/automation dashboard``.

Composes a single read-only embed summarising every workload bucket the
Automation cog watches: pending bounties, regear requests, applications,
help tickets, treasury status, inactivity counts, policy snapshots,
events needing reconciliation, and silver-ledger balances.

Pulls out of ``cogs/automation.py`` so the cog file stays focused on the
schedulers, slash commands, and hooks.
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime

import discord

from cogs._automation_helpers import (
    _DEFAULT_INACTIVE_DAYS,
    _get_int_config,
    _now,
    _snooze_key,
)


# ── Officer dashboard helpers (used by /automation dashboard) ───────────────

def _dash_count(db, sql: str, params: tuple = ()) -> int:
    """Run a single SELECT COUNT(*) query and return the int. Returns -1 on
    failure so the dashboard can show 'err' instead of crashing."""
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(sql, params)
        row = db.cursor.fetchone()
        if not row:
            return 0
        return int(row[0] if not hasattr(row, "keys") else list(row)[0])
    except Exception:  # noqa: BLE001
        return -1


def _dash_oldest_age(db, sql: str, params: tuple = ()) -> str:
    """Return a human age string ('2d 4h') for the oldest timestamp matched."""
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(sql, params)
        row = db.cursor.fetchone()
        if not row:
            return "—"
        ts = row[0] if not hasattr(row, "keys") else list(row)[0]
        if not ts:
            return "—"
        when = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        delta = _now() - when
        days = delta.days
        hours = delta.seconds // 3600
        if days >= 1:
            return f"{days}d {hours}h"
        mins = (delta.seconds % 3600) // 60
        return f"{hours}h {mins}m"
    except Exception:  # noqa: BLE001
        return "?"


def _dash_snooze_status(bot: Bot, scope: str) -> str:
    """Return ' · 💤 until <relative>' suffix if snoozed, else ''."""
    raw = bot.db.get_config(_snooze_key(scope))
    if not raw:
        return ""
    try:
        until = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if _now() >= until:
        return ""
    return f" · 💤 until <t:{int(until.timestamp())}:R>"


def _build_officer_dashboard_embed(
    bot: Bot, requester,
) -> discord.Embed:
    """Compose the read-only officer dashboard embed. Reads recent SQL counts
    across every workload bucket and renders one ephemeral overview."""
    db = bot.db
    now = _now()

    # ── Pending review queues ────────────────────────────────────────────
    bounties_pending = _dash_count(
        db,
        "SELECT COUNT(*) FROM bounties "
        "WHERE submitted_at IS NOT NULL "
        "AND status NOT IN ('completed','cancelled')",
    )
    bounties_oldest = _dash_oldest_age(
        db,
        "SELECT MIN(submitted_at) FROM bounties "
        "WHERE submitted_at IS NOT NULL "
        "AND status NOT IN ('completed','cancelled')",
    )
    regear_pending = _dash_count(
        db, "SELECT COUNT(*) FROM regear_requests WHERE status='pending'",
    )
    regear_oldest = _dash_oldest_age(
        db,
        "SELECT MIN(created_at) FROM regear_requests WHERE status='pending'",
    )
    guild_apps = _dash_count(
        db, "SELECT COUNT(*) FROM guild_applications WHERE status='pending'",
    )
    staff_apps = _dash_count(
        db, "SELECT COUNT(*) FROM staff_applications WHERE status='pending'",
    )
    help_open = _dash_count(
        db, "SELECT COUNT(*) FROM help_tickets WHERE status IN ('open','claimed')",
    )

    # ── Treasury ─────────────────────────────────────────────────────────
    today = now.strftime("%Y-%m-%d")
    latest_treasury = db.fetch_latest_guild_treasury()
    if latest_treasury and latest_treasury.get("date") == today:
        treasury_line = (
            f"✅ Recorded today: **{int(latest_treasury['balance']):,}** silver"
        )
    elif latest_treasury:
        treasury_line = (
            f"❌ Not recorded today (last: `{latest_treasury['date']}`, "
            f"**{int(latest_treasury['balance']):,}** silver)"
        )
    else:
        treasury_line = "❌ No treasury snapshots yet"

    # ── Inactivity ───────────────────────────────────────────────────────
    days = _get_int_config(
        db, "automation_inactivity_threshold_days", _DEFAULT_INACTIVE_DAYS,
    )
    threshold_iso = (now - datetime.timedelta(days=days)).isoformat()
    home = (db.get_config("home_guild_name") or "").strip() or None
    try:
        inactive_rows = db.fetch_inactive_profiles(threshold_iso, home_guild=home)
        inactive_count = len(inactive_rows)
    except Exception:  # noqa: BLE001
        inactive_count = -1
    inactive_snooze = _dash_snooze_status(bot, "inactivity")

    # ── Policy snapshots (count only — drift detection is async/expensive) ─
    try:
        policy_count = len(db.fetch_all_policy_snapshots())
    except Exception:  # noqa: BLE001
        policy_count = -1
    policy_snooze = _dash_snooze_status(bot, "policy")

    # ── Events needing reconciliation ────────────────────────────────────
    try:
        recon_pending = len(db.fetch_events_needing_reconciliation())
    except Exception:  # noqa: BLE001
        recon_pending = -1

    # ── Silver debts ─────────────────────────────────────────────────────
    try:
        debts = db.fetch_silver_debts()
    except Exception:  # noqa: BLE001
        debts = []
    guild_owes  = sum(int(r["silver_balance"]) for r in debts if int(r["silver_balance"]) > 0)
    members_owe = -sum(int(r["silver_balance"]) for r in debts if int(r["silver_balance"]) < 0)
    debtor_count = len(debts)

    def _fmt(n: int) -> str:
        return "err" if n < 0 else f"{n}"

    queue_lines = [
        f"🪙 **Bounties** awaiting review: **{_fmt(bounties_pending)}**"
        + (f" · oldest `{bounties_oldest}`" if bounties_pending > 0 else ""),
        f"🛡️ **Regear** requests pending: **{_fmt(regear_pending)}**"
        + (f" · oldest `{regear_oldest}`" if regear_pending > 0 else ""),
        f"📨 **Guild applications**: **{_fmt(guild_apps)}**",
        f"📨 **Staff applications**: **{_fmt(staff_apps)}**",
        f"❓ **Help tickets** open: **{_fmt(help_open)}**",
    ]
    ops_lines = [
        f"💰 **Treasury**: {treasury_line}",
        f"🧹 **Inactive** ({days}d+): **{_fmt(inactive_count)}**{inactive_snooze}",
        f"📜 **Policy snapshots tracked**: **{_fmt(policy_count)}**{policy_snooze}",
        f"✅ **Events needing reconcile**: **{_fmt(recon_pending)}**",
    ]
    silver_lines: list[str] = []
    if debtor_count > 0:
        if guild_owes:
            silver_lines.append(f"Guild owes members: **{guild_owes:,}** silver")
        if members_owe:
            silver_lines.append(f"Members owe guild: **{members_owe:,}** silver")
        silver_lines.append(f"Accounts with non-zero balance: **{debtor_count}**")
    else:
        silver_lines.append("All silver balances are zero. ✅")

    # ── Legacy Registration Grace Rows ──────────────────────────────────────
    # The registration flow no longer requires home/alliance membership. This
    # block only surfaces stale rows written by the old gate so officers can
    # tell those users to click Register again.
    grace_rows = db.fetch_pending_home_guild_grace()
    grace_lines: list[str] = []
    if grace_rows:
        for row in grace_rows:
            who = (
                row.get("albion_name")
                or row.get("username")
                or row.get("discord_id")
            )
            guild = row.get("guild_name") or "?"
            until = row.get("pending_home_guild_until")
            try:
                dt_until = datetime.datetime.fromisoformat(
                    str(until).replace("Z", "+00:00")
                )
                now_aware = (
                    now if now.tzinfo
                    else now.replace(tzinfo=datetime.timezone.utc)
                )
                delta = dt_until - now_aware
                if delta.total_seconds() > 0:
                    days = delta.days
                    hours = delta.seconds // 3600
                    mins = (delta.seconds % 3600) // 60
                    left = (
                        f"{days}d {hours}h {mins}m" if days
                        else f"{hours}h {mins}m"
                    )
                else:
                    left = "expired"
            except Exception:
                left = "?"
            grace_lines.append(
                f"• **{who}** (guild: `{guild}`) — ⏳ {left} left"
            )
        grace_lines.append(
            "Legacy registration-gate row. Registration now only verifies "
            "Albion identity; tell them to click Register again."
        )

    actionable = sum(
        max(0, n) for n in (
            bounties_pending, regear_pending, guild_apps, staff_apps,
            help_open, recon_pending,
        )
    )
    if inactive_count > 0:
        actionable += 1
    if not (latest_treasury and latest_treasury.get("date") == today):
        actionable += 1

    title = (
        f"🛂  Officer dashboard — {actionable} item(s) need attention"
        if actionable else
        "🛂  Officer dashboard — all caught up ✨"
    )
    color = discord.Color.gold() if actionable else discord.Color.green()
    embed = discord.Embed(title=title, color=color, timestamp=now)
    embed.add_field(name="Review queues",  value="\n".join(queue_lines),  inline=False)
    embed.add_field(name="Operations",     value="\n".join(ops_lines),    inline=False)
    embed.add_field(name="Silver ledger",  value="\n".join(silver_lines), inline=False)
    if grace_lines:
        embed.add_field(name="Legacy Registration Grace", value="\n".join(grace_lines), inline=False)
    embed.set_footer(text=f"Requested by {requester} · click Refresh to update")
    return embed


class OfficerDashboardView(discord.ui.View):
    """Ephemeral-message view: a single Refresh button. Not persistent — the
    ephemeral message itself disappears on bot restart, so re-running the
    slash command is the recovery path."""

    def __init__(self) -> None:
        super().__init__(timeout=15 * 60)

    @discord.ui.button(
        label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄",
    )
    async def refresh(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        bot: Bot = interaction.client  # type: ignore[assignment]
        embed = _build_officer_dashboard_embed(bot, interaction.user)
        await interaction.response.edit_message(embed=embed, view=self)

