"""Officer ``/audit`` command group extracted from ``cogs/admin.py``.

Investigative / diagnostic commands kept in their own top-level slash
group so we don't blow Discord's 25-subcommand-per-group hard cap on
``/admin``. Imported and registered as a tree command from
``cogs/admin.py`` so the cog auto-loader (which skips ``_*.py``)
doesn't have to learn about it.
"""
from __future__ import annotations

from cogs._typing import Bot
import asyncio
import io

import discord
from discord import app_commands
from discord.ext import commands

import albion_api
from debug import info_log
from utils import error_embed, info_embed, success_embed


class AuditGroup(app_commands.Group, name="audit", description="Officer audit/diagnostic commands."):
    """Top-level group separate from /admin so we don't run into Discord's
    25-subcommand-per-group hard cap. New investigative tooling lives here."""

    def __init__(self, bot: Bot):
        super().__init__()
        self.bot: Bot = bot

    @app_commands.command(
        name="stale-albion",
        description="List registered members whose Albion stats haven't synced in N days.",
    )
    @app_commands.describe(days="Show profiles whose last_updated is older than this many days. Default 7.")
    async def stale_albion(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        import datetime
        db = self.bot.db
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
        cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        try:
            if not db.connection:
                db.connect()
            rows = db.cursor.execute(
                '''SELECT discord_id, albion_name, guild_name, lifecycle_role, last_updated
                   FROM user_profiles
                   WHERE albion_player_id IS NOT NULL
                     AND (last_updated IS NULL OR last_updated < ?)
                   ORDER BY (last_updated IS NULL) DESC, last_updated ASC''',
                (cutoff_iso,),
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Query failed", f"`{exc}`"),
                ephemeral=True,
            )
            return

        if not rows:
            await interaction.followup.send(
                embed=success_embed(
                    "Nothing stale",
                    f"All registered members synced within the last {days} day(s).",
                ),
                ephemeral=True,
            )
            return

        lines = []
        for r in rows[:30]:
            name = r["albion_name"] or "?"
            last = r["last_updated"] or "_never_"
            lc = r["lifecycle_role"] or "_none_"
            guild = r["guild_name"] or "_none_"
            lines.append(f"• <@{r['discord_id']}> `{name}` · last={last} · {lc} · guild=`{guild}`")
        more = f"\n…and {len(rows) - 30} more." if len(rows) > 30 else ""
        embed = info_embed(
            f"⏳ Stale Albion sync — {len(rows)} member(s) > {days}d",
            "\n".join(lines) + more,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(f"{interaction.user} ran /audit stale-albion days={days}; {len(rows)} stale.")

    @app_commands.command(
        name="debts",
        description="Show every member with a non-zero silver balance with the guild.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def debts(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = self.bot.db.fetch_silver_debts()
        if not rows:
            await interaction.followup.send(
                embed=success_embed("All settled", "Every member's silver balance is at zero."),
                ephemeral=True,
            )
            return
        owed_by_guild: list[str] = []      # guild → member (positive)
        owed_to_guild: list[str] = []      # member → guild (negative)
        total_out = 0
        total_in = 0
        for r in rows:
            bal = int(r["silver_balance"] or 0)
            name = r["albion_name"] or r.get("username") or r["discord_id"]
            line = f"• <@{r['discord_id']}> `{name}` — **{abs(bal):,}**"
            if bal > 0:
                owed_by_guild.append(line)
                total_out += bal
            else:
                owed_to_guild.append(line)
                total_in += -bal
        embed = info_embed(
            "💰 Outstanding silver balances",
            f"**Guild owes:** {total_out:,} silver across {len(owed_by_guild)} member(s)\n"
            f"**Members owe:** {total_in:,} silver across {len(owed_to_guild)} member(s)\n"
            f"**Net:** {total_out - total_in:+,} silver",
        )
        if owed_by_guild:
            embed.add_field(
                name=f"Guild → member ({len(owed_by_guild)})",
                value="\n".join(owed_by_guild[:15])
                + (f"\n…+{len(owed_by_guild) - 15} more" if len(owed_by_guild) > 15 else ""),
                inline=False,
            )
        if owed_to_guild:
            embed.add_field(
                name=f"Member → guild ({len(owed_to_guild)})",
                value="\n".join(owed_to_guild[:15])
                + (f"\n…+{len(owed_to_guild) - 15} more" if len(owed_to_guild) > 15 else ""),
                inline=False,
            )
        embed.set_footer(text="Use /audit settle to record an in-game payment.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="settle",
        description="Record an in-game silver payment that adjusts a member's balance toward zero.",
    )
    @app_commands.describe(
        member="Member whose balance to adjust.",
        amount="Silver paid. Positive = guild paid the member; negative = member paid the guild.",
        note="Optional note (e.g. 'paid in mailbox 2026-05-09').",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settle(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, -1_000_000_000, 1_000_000_000],
        note: str | None = None,
    ) -> None:
        if amount == 0:
            await interaction.response.send_message(
                embed=error_embed("Bad amount", "Settlement amount cannot be zero."),
                ephemeral=True,
            )
            return
        # A settlement is the *opposite* sign of the balance: paying the
        # member zeros out a positive balance, so we apply -amount.
        delta = -int(amount)
        reason = f"Settlement by {interaction.user}"
        if note:
            reason += f" — {note}"
        new_bal = self.bot.db.adjust_silver_balance(
            str(member.id), delta,
            reason=reason,
            ref_type="settle", ref_id=None,
            actor_id=str(interaction.user.id),
        )
        if new_bal is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Settlement failed",
                    f"{member.mention} has no profile yet, or the write failed. "
                    "Make sure they're registered.",
                ),
                ephemeral=True,
            )
            return
        direction = (
            f"Recorded **{abs(amount):,}** silver paid "
            f"{'to ' + member.mention if amount > 0 else 'from ' + member.mention}."
        )
        bal_line = (
            f"New balance: **{new_bal:+,}** "
            f"({'guild owes them' if new_bal > 0 else 'they owe the guild' if new_bal < 0 else 'settled'})."
        )
        await interaction.response.send_message(
            embed=success_embed("Settlement logged", f"{direction}\n{bal_line}"),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} settled {amount:+,} silver with {member} "
            f"(delta={delta:+,}, new_bal={new_bal:+,}, note={note!r})."
        )

    @app_commands.command(
        name="ledger",
        description="Show a member's silver-balance history (most recent first).",
    )
    @app_commands.describe(member="Member to look up.", limit="How many entries (1-30, default 15).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def ledger(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        limit: app_commands.Range[int, 1, 30] = 15,
    ) -> None:
        bal = self.bot.db.fetch_silver_balance(str(member.id))
        rows = self.bot.db.fetch_silver_ledger(str(member.id), limit=int(limit))
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    f"{member.display_name} — ledger",
                    f"Current balance: **{bal:+,}**\n_No ledger entries._",
                ),
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            d = int(r["delta"])
            sign = "+" if d > 0 else ""
            ts = (r["created_at"] or "")[:16]
            reason = r["reason"] or "?"
            lines.append(f"`{ts}` `{sign}{d:,}` — {reason}")
        embed = info_embed(
            f"{member.display_name} — ledger ({len(rows)})",
            f"Current balance: **{bal:+,}** silver\n\n" + "\n".join(lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="treasury-record",
        description="Manually record today's in-game guild bank balance.",
    )
    @app_commands.describe(
        amount="Current guild silver balance (whole silver, no commas).",
        note="Optional context (e.g. 'after weekly payouts').",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def treasury_record(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 0, 1_000_000_000_000],
        note: str | None = None,
    ) -> None:
        import datetime as _dt
        date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        ok = self.bot.db.record_guild_treasury(
            date, int(amount),
            recorded_by=str(interaction.user.id),
            note=note,
        )
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Save failed", "Database write failed; check logs."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                f"Treasury recorded — {date}",
                f"Balance: **{int(amount):,}** silver "
                + (f"\n_{note}_" if note else ""),
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} recorded guild treasury {date}: {int(amount):,} silver.")

    @app_commands.command(
        name="treasury-graph",
        description="Show the guild treasury history as a line graph.",
    )
    @app_commands.describe(days="How many days back to plot (1-365, default 30).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def treasury_graph(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = 30,
    ) -> None:
        await interaction.response.defer(ephemeral=False)
        rows = self.bot.db.fetch_guild_treasury_history(days=int(days))
        try:
            from cogs.graphs import render_treasury_graph
            file = render_treasury_graph(rows)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Render failed", f"`{exc}`"),
                ephemeral=True,
            )
            return
        await interaction.followup.send(file=file)

    @app_commands.command(
        name="treasury-report",
        description="Estimated guild revenue vs gear losses, derived from daily snapshots.",
    )
    @app_commands.describe(
        days="Window in days (1-90, default 7).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def treasury_report(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        """Treasury Δbalance is the only reliable revenue signal we have,
        so derive everything from it. Inflow ≈ Δbalance + officer
        settlements (debits in silver_ledger) within the window. No
        additional manual data entry required."""
        import datetime as _dt
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            # Pull snapshots covering the window plus one earlier row for
            # the baseline delta.
            db.cursor.execute(
                "SELECT date, balance FROM guild_treasury_history "
                "ORDER BY date ASC"
            )
            snaps = [dict(r) for r in db.cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            await interaction.response.send_message(
                embed=error_embed("DB error", f"`{exc}`"), ephemeral=True,
            )
            return
        if len(snaps) < 2:
            await interaction.response.send_message(
                embed=info_embed(
                    "Not enough data",
                    "Need at least 2 daily treasury snapshots to compute "
                    "revenue. Use `/audit treasury-record` daily or hit "
                    "the daily automation prompt for a few days, then try "
                    "again.",
                ),
                ephemeral=True,
            )
            return

        # Window endpoints (UTC dates).
        today = _dt.datetime.now(_dt.timezone.utc).date()
        cutoff = today - _dt.timedelta(days=int(days))
        latest = snaps[-1]
        # Find the snapshot closest to (but not after) cutoff.
        baseline = snaps[0]
        for s in snaps:
            d = _dt.date.fromisoformat(s["date"])
            if d <= cutoff:
                baseline = s
        delta_balance = int(latest["balance"]) - int(baseline["balance"])

        # Settlements paid out to members in the window (debits → guild
        # silver actually leaving the bank).
        cutoff_iso = cutoff.isoformat()
        try:
            db.cursor.execute(
                "SELECT COALESCE(SUM(-delta), 0) AS paid_out "
                "FROM silver_ledger "
                "WHERE delta < 0 AND substr(created_at, 1, 10) >= ?",
                (cutoff_iso,),
            )
            paid_out = int(db.cursor.fetchone()["paid_out"] or 0)
        except Exception:
            paid_out = 0

        # Approved regear payouts that hit the books in the window.
        try:
            db.cursor.execute(
                "SELECT COALESCE(SUM(gear_value), 0) AS s, COUNT(*) AS n "
                "FROM regear_requests "
                "WHERE status='approved' "
                "  AND substr(COALESCE(decided_at, submitted_at), 1, 10) >= ?",
                (cutoff_iso,),
            )
            r = db.cursor.fetchone()
            regear_paid = int(r["s"] or 0)
            regear_n = int(r["n"] or 0)
        except Exception:
            regear_paid = regear_n = 0

        # Outstanding silver the guild owes (current snapshot, not windowed).
        try:
            db.cursor.execute(
                "SELECT COALESCE(SUM(silver_balance), 0) AS owed "
                "FROM user_profiles WHERE silver_balance > 0"
            )
            owed_to_members = int(db.cursor.fetchone()["owed"] or 0)
        except Exception:
            owed_to_members = 0

        # Estimated revenue = Δbalance + silver that left the bank.
        # (If treasury rose AND we paid people, the inflow had to cover both.)
        est_revenue = delta_balance + paid_out
        days_actual = max(
            1,
            (_dt.date.fromisoformat(latest["date"])
             - _dt.date.fromisoformat(baseline["date"])).days,
        )
        per_day = est_revenue // days_actual if days_actual else est_revenue

        bal = int(latest["balance"])
        coverage = (
            f"{(bal / owed_to_members):.2f}× outstanding"
            if owed_to_members else "no outstanding debt"
        )

        sign = "📈" if est_revenue >= 0 else "📉"
        emoji_net = "🟢" if est_revenue >= regear_paid else "🟡" if est_revenue >= 0 else "🔴"

        embed = discord.Embed(
            title=f"💰 Guild P&L — last {days_actual}d",
            colour=discord.Colour.gold(),
            description=(
                f"Window: `{baseline['date']}` → `{latest['date']}`\n"
                f"5% tax + any donations show up here automatically as "
                f"treasury Δ.\n"
            ),
        )
        embed.add_field(
            name=f"{sign} Estimated revenue",
            value=(
                f"**{est_revenue:+,}** silver\n"
                f"(~{per_day:+,}/day)\n"
                f"_Δbalance ({delta_balance:+,}) "
                f"+ paid out ({paid_out:,})_"
            ),
            inline=True,
        )
        embed.add_field(
            name="🛡️ Regear paid (window)",
            value=f"**{regear_paid:,}** silver\n({regear_n} approved)",
            inline=True,
        )
        embed.add_field(
            name=f"{emoji_net} Net position",
            value=(
                f"**{(est_revenue - regear_paid):+,}** silver\n"
                f"_revenue − regear payouts_"
            ),
            inline=True,
        )
        embed.add_field(
            name="🏦 Treasury",
            value=(
                f"**{bal:,}** silver\n"
                f"as of `{latest['date']}`"
            ),
            inline=True,
        )
        embed.add_field(
            name="💸 Owed to members",
            value=(
                f"**{owed_to_members:,}** silver\n"
                f"coverage: {coverage}"
            ),
            inline=True,
        )
        embed.add_field(
            name="📋 Method",
            value=(
                "Revenue is **derived**, not logged: "
                "`Δtreasury + officer settlements`. "
                "Only requires you to keep recording the daily balance — "
                "which you're already doing."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Baseline snapshot: {baseline['balance']:,} on "
                f"{baseline['date']}"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
        info_log(
            f"{interaction.user} ran treasury-report "
            f"({days_actual}d): est_rev={est_revenue:,}, "
            f"regear_paid={regear_paid:,}, owed={owed_to_members:,}."
        )

    @app_commands.command(
        name="profile-doctor",
        description="Find ghost / corrupted profile rows (registered but with missing or zeroed data).",
    )
    @app_commands.describe(
        fix="If True, attempt to re-fetch stats from Albion for each ghost (slow).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def profile_doctor(
        self, interaction: discord.Interaction,
        fix: bool = False,
    ) -> None:
        """Detect profile rows where albion_player_id is set but the rest of
        the row is empty/zeroed — typically the result of a failed registration
        stats fetch. Optionally re-fetches stats from Albion."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            rows = db.cursor.execute(
                '''SELECT discord_id, albion_player_id, albion_name,
                          guild_name, kill_fame, pve_total, last_updated
                   FROM user_profiles
                   WHERE albion_player_id IS NOT NULL
                     AND (
                          guild_name IS NULL
                          OR (kill_fame = 0 AND pve_total = 0)
                          OR last_updated IS NULL
                     )
                   ORDER BY (last_updated IS NULL) DESC, last_updated ASC'''
            ).fetchall()
            ghosts = [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Query failed", f"`{exc}`"),
                ephemeral=True,
            )
            return

        if not ghosts:
            await interaction.followup.send(
                embed=info_embed(
                    "Profile doctor",
                    "No ghost profiles found. ✅",
                ),
                ephemeral=True,
            )
            return

        if not fix:
            lines = []
            for g in ghosts[:25]:
                reasons = []
                if not g.get("guild_name"):
                    reasons.append("no guild")
                if not g.get("kill_fame") and not g.get("pve_total"):
                    reasons.append("zeroed")
                if not g.get("last_updated"):
                    reasons.append("never synced")
                lines.append(
                    f"• <@{g['discord_id']}> (`{g.get('albion_name') or '—'}`) — "
                    f"{', '.join(reasons) or 'unknown'}"
                )
            if len(ghosts) > 25:
                lines.append(f"…and {len(ghosts) - 25} more.")
            embed = discord.Embed(
                title=f"🩺  Profile doctor — {len(ghosts)} ghost(s)",
                description="\n".join(lines) + (
                    "\n\nRun `/audit profile-doctor fix:True` to attempt "
                    "automatic stat refresh from the Albion API."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # fix=True: re-fetch each ghost's stats
        loop = asyncio.get_running_loop()
        fixed: list[str] = []
        still_broken: list[str] = []
        for g in ghosts:
            pid = g["albion_player_id"]
            try:
                data = await loop.run_in_executor(
                    None, lambda p=pid: albion_api.get_player_stats(p),
                )
                if not data:
                    still_broken.append(
                        f"{g.get('albion_name') or pid}: API timeout"
                    )
                    continue
                stats = albion_api.parse_stats(data)
                db.update_user_albion_info(
                    str(g["discord_id"]), pid,
                    stats.get("albion_name") or g.get("albion_name") or "",
                    stats,
                )
                fixed.append(
                    f"{g.get('albion_name') or pid} → "
                    f"{stats.get('guild_name') or '(no guild)'}"
                )
            except Exception as exc:  # noqa: BLE001
                still_broken.append(f"{g.get('albion_name') or pid}: {exc!r}")

        lines = []
        if fixed:
            lines.append(f"**Repaired ({len(fixed)}):**")
            lines.extend(f"• {f}" for f in fixed[:20])
            if len(fixed) > 20:
                lines.append(f"…and {len(fixed) - 20} more.")
        if still_broken:
            lines.append(f"\n**Still broken ({len(still_broken)}):**")
            lines.extend(f"• {b}" for b in still_broken[:10])
        # Also clean up zero-baseline history rows that break LAG-based delta queries
        # (hourly fame chart, top movers). These come from prior registration glitches
        # / API timeouts that wrote zeros; once a real row exists we don't need them.
        # NB: must run on the main thread — sqlite3.Connection is thread-local.
        try:
            purged = db.purge_zero_stats_history()
            if purged:
                lines.append(f"\n🧹 Pruned **{purged}** zero-baseline history row(s).")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"\n⚠️ Zero-history purge failed: `{exc!r}`")
        embed = discord.Embed(
            title=f"🩺  Profile doctor — repaired {len(fixed)}/{len(ghosts)}",
            description="\n".join(lines) or "Nothing happened.",
            color=discord.Color.green() if fixed and not still_broken else discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran profile-doctor (fix=True): "
            f"{len(fixed)} repaired, {len(still_broken)} failed."
        )

    @app_commands.command(
        name="reload-cog",
        description="Hot-reload a single cog without restarting the bot (dev tool).",
    )
    @app_commands.describe(
        cog="Cog module name (e.g. 'automation' for cogs.automation).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def reload_cog(
        self, interaction: discord.Interaction, cog: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        ext = cog if cog.startswith("cogs.") else f"cogs.{cog}"
        try:
            await bot.reload_extension(ext)
        except commands.ExtensionNotLoaded:
            try:
                await bot.load_extension(ext)
            except Exception as exc:  # noqa: BLE001
                await interaction.followup.send(
                    embed=error_embed("Reload failed", f"`{ext}`: `{exc!r}`"),
                    ephemeral=True,
                )
                return
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Reload failed", f"`{ext}`: `{exc!r}`"),
                ephemeral=True,
            )
            return
        # Re-sync slash commands so any added/removed sub-commands appear.
        try:
            if bot.dev_guild:
                await bot.tree.sync(guild=bot.dev_guild)
            else:
                await bot.tree.sync()
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed(
                    "Reloaded but sync failed",
                    f"`{ext}` reloaded — `tree.sync()` raised `{exc!r}`.",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed("Cog reloaded", f"`{ext}` reloaded and slash commands re-synced."),
            ephemeral=True,
        )
        info_log(f"{interaction.user} hot-reloaded {ext}.")

    @app_commands.command(
        name="whois",
        description="One-shot member lookup: profile, recent activity, and lifecycle.",
    )
    @app_commands.describe(member="The Discord member to inspect.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def whois(
        self, interaction: discord.Interaction, member: discord.Member,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        import datetime as _dt
        db = self.bot.db
        did = str(member.id)
        profile = db.fetch_user_profile(did) or {}

        embed = discord.Embed(
            title=f"🔍 whois — {member.display_name}",
            color=discord.Color.blurple(),
            timestamp=_dt.datetime.now(_dt.timezone.utc),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        # Identity / lifecycle
        identity_lines = [
            f"**Discord:** {member.mention} (`{member.id}`)",
            f"**Joined Discord:** "
            f"<t:{int(member.joined_at.timestamp())}:R>" if member.joined_at else "**Joined Discord:** unknown",
            f"**Albion:** {profile.get('albion_name') or '_unregistered_'}",
            f"**Guild:** {profile.get('guild_name') or 'N/A'}",
            f"**Lifecycle:** {profile.get('lifecycle_role') or '_none_'}",
        ]
        last_act = profile.get("last_activity_date")
        if last_act:
            identity_lines.append(f"**Last activity:** {last_act}")
        last_upd = profile.get("last_updated")
        if last_upd:
            identity_lines.append(f"**Last sync:** {last_upd}")
        embed.add_field(name="Identity", value="\n".join(identity_lines), inline=False)

        # Stats summary
        if profile.get("albion_player_id"):
            stats_lines = [
                f"⚔️ Kill {int(profile.get('kill_fame') or 0):,} · "
                f"💀 Death {int(profile.get('death_fame') or 0):,} · "
                f"📈 IP {float(profile.get('average_item_power') or 0.0):.0f}",
                f"🐗 PvE {int(profile.get('pve_total') or 0):,} · "
                f"⛏️ Gather {int(profile.get('gather_all') or 0):,}",
                f"🔨 Craft {int(profile.get('crafting_fame') or 0):,} · "
                f"🎣 Fish {int(profile.get('fishing_fame') or 0):,} · "
                f"🌾 Farm {int(profile.get('farming_fame') or 0):,}",
            ]
            embed.add_field(name="Stats", value="\n".join(stats_lines), inline=False)

        # Engagement: streak, voice, points
        cur_streak = int(profile.get("activity_streak_days") or 0)
        best_streak = int(profile.get("activity_streak_best") or 0)
        engagement_bits = []
        if cur_streak or best_streak:
            engagement_bits.append(
                f"🔥 Streak {cur_streak}d (best {best_streak}d)"
            )
        try:
            total_v = int(db.fetch_voice_seconds_total(did) or 0)
            if total_v > 0:
                hours = total_v / 3600
                engagement_bits.append(f"🎤 Voice {hours:.1f}h total")
        except Exception:  # noqa: BLE001
            pass
        try:
            pts = db.get_points(did)
            engagement_bits.append(
                f"⭐ Points: weekly {pts['weekly']:,} · "
                f"monthly {pts['monthly']:,} · season {pts['season']:,}"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            silver = int(db.fetch_silver_balance(did) or 0)
            if silver:
                engagement_bits.append(f"💰 Silver balance: {silver:,}")
        except Exception:  # noqa: BLE001
            pass
        if engagement_bits:
            embed.add_field(name="Engagement", value="\n".join(engagement_bits), inline=False)

        # Discord roles (top 5 non-default, sorted by position)
        role_names = [
            r.name for r in sorted(member.roles, key=lambda x: -x.position)
            if r.name != "@everyone"
        ][:8]
        if role_names:
            embed.add_field(name="Roles", value=", ".join(role_names), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(f"{interaction.user} ran /audit whois on {member}.")

    # ── Weekly guild tax / revenue tracker ───────────────────────────────
    #
    # Officer copies the weekly fame/contribution leaderboard from the Albion
    # website (which exports as TSV with quoted fields), saves the clipboard
    # paste as a .tsv/.csv/.txt file, attaches it to /audit weekly-tax.
    #
    # This command **does not charge members** — it only logs the computed
    # tax as a guild-revenue line item. The actual silver is collected
    # in-game; the bot just keeps the running tally for accounting.
    #
    # Workflow:
    #   1. Bot parses the table (auto-detects tab vs comma).
    #   2. Each row's Player is matched to user_profiles.albion_name (case-
    #      insensitive). Unmatched rows are listed but still counted toward
    #      the gross — change ``matched_only:True`` to exclude them.
    #   3. Computes total revenue = sum( ceil(amount * rate / 100) ).
    #   4. With ``commit:False`` (default) → preview only.
    #      With ``commit:True``           → appends one row to guild_revenue
    #      (source='weekly_tax') for the audit log. No member balances
    #      change.

    @app_commands.command(
        name="weekly-tax",
        description="Log a weekly guild tax (revenue) from an attached Albion contribution table.",
    )
    @app_commands.describe(
        file="The .tsv/.csv/.txt export from the Albion guild contribution leaderboard.",
        tax_rate="Percent of each member's amount to tally as revenue (1-50). Default: 5%.",
        commit="False = preview only (default). True = append the revenue line to the log.",
        matched_only="Only count members who match a registered profile (default False).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def weekly_tax(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        tax_rate: app_commands.Range[int, 1, 50] = 5,
        commit: bool = False,
        matched_only: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        # ── 1. Read the file ────────────────────────────────────────────
        if file.size > 256 * 1024:
            await interaction.followup.send(
                embed=error_embed("File too large", "Keep the export under 256 KB."),
                ephemeral=True,
            )
            return
        try:
            raw = (await file.read()).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Couldn't read file", f"`{exc!r}`"),
                ephemeral=True,
            )
            return

        # ── 2. Parse rows ───────────────────────────────────────────────
        import csv as _csv
        sample = raw[:2048]
        delim = "\t" if sample.count("\t") >= sample.count(",") else ","
        try:
            reader = _csv.reader(io.StringIO(raw), delimiter=delim)
            rows = [r for r in reader if any(c.strip() for c in r)]
        except _csv.Error as exc:
            await interaction.followup.send(
                embed=error_embed("Parse error", f"`{exc}`"),
                ephemeral=True,
            )
            return
        if not rows:
            await interaction.followup.send(
                embed=error_embed("Empty file", "No rows to import."),
                ephemeral=True,
            )
            return

        header = [c.strip().lower() for c in rows[0]]
        if "player" not in header or "amount" not in header:
            await interaction.followup.send(
                embed=error_embed(
                    "Header missing",
                    "First row must include `Player` and `Amount` columns.",
                ),
                ephemeral=True,
            )
            return
        col_player = header.index("player")
        col_amount = header.index("amount")
        data_rows = rows[1:]

        # ── 3. Build the import plan ────────────────────────────────────
        import math
        db = self.bot.db
        matched: list[tuple[str, str, int, int]] = []   # (albion, did, amount, tax)
        unmatched: list[tuple[str, int, int]] = []      # (albion, amount, tax)
        zero_rows = 0
        bad_rows = 0
        for r in data_rows:
            if len(r) <= max(col_player, col_amount):
                bad_rows += 1
                continue
            name = (r[col_player] or "").strip()
            amt_raw = (r[col_amount] or "").strip().replace(",", "")
            if not name:
                bad_rows += 1
                continue
            try:
                amount = int(amt_raw)
            except ValueError:
                bad_rows += 1
                continue
            if amount <= 0:
                zero_rows += 1
                continue
            tax = math.ceil(amount * tax_rate / 100)
            profile = db.fetch_profile_by_albion_name(name)
            if profile and profile.get("discord_id"):
                matched.append((name, str(profile["discord_id"]), amount, tax))
            else:
                unmatched.append((name, amount, tax))

        # Totals (matched_only flag controls whether unmatched contributors count).
        matched_amount = sum(a for _, _, a, _ in matched)
        matched_tax    = sum(t for _, _, _, t in matched)
        unmatched_amount = sum(a for _, a, _ in unmatched)
        unmatched_tax    = sum(t for _, _, t in unmatched)
        if matched_only:
            total_base = matched_amount
            total_tax  = matched_tax
        else:
            total_base = matched_amount + unmatched_amount
            total_tax  = matched_tax + unmatched_tax

        # ── 4. Append revenue row if commit ────────────────────────────
        import datetime as _dt
        today_iso = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        row_id = 0
        if commit and total_tax > 0:
            note_bits = [f"{file.filename}"]
            if matched_only:
                note_bits.append("matched-only")
            row_id = db.record_guild_revenue(
                date=today_iso,
                source="weekly_tax",
                amount=int(total_tax),
                rate=int(tax_rate),
                recorded_by=str(interaction.user.id),
                note=" · ".join(note_bits),
                base_amount=int(total_base),
                matched_count=len(matched),
                unmatched_count=len(unmatched),
            )

        # Running totals for context.
        ytd_iso = today_iso[:4] + "-01-01"
        ytd_total = db.fetch_guild_revenue_total(since_iso=ytd_iso)
        all_time_total = db.fetch_guild_revenue_total()

        # ── 5. Build summary embed ──────────────────────────────────────
        title_state = "✅ Logged" if commit and row_id else "🔍 Preview"
        color = discord.Color.green() if commit and row_id else discord.Color.blurple()
        embed = discord.Embed(
            title=f"{title_state} — Weekly tax @ {tax_rate}%",
            description=(
                f"**Matched:** {len(matched)} · **Unmatched:** {len(unmatched)} · "
                f"**Skipped 0-amount:** {zero_rows} · **Bad rows:** {bad_rows}"
            ),
            color=color,
        )
        embed.add_field(
            name="💰 Tax this run",
            value=(
                f"Base amount: **{total_base:,}**\n"
                f"Revenue: **{total_tax:,}** silver\n"
                + (
                    f"_Logged as guild_revenue #{row_id}._"
                    if commit and row_id else
                    "_Re-run with `commit:True` to log it._"
                )
            ),
            inline=False,
        )
        if commit and row_id:
            ytd_after = ytd_total  # already includes the row we just inserted
            embed.add_field(
                name="📊 Running totals",
                value=(
                    f"YTD revenue: **{ytd_after:,}**\n"
                    f"All-time: **{all_time_total:,}**"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="📊 Running totals (before this run)",
                value=f"YTD: **{ytd_total:,}** · All-time: **{all_time_total:,}**",
                inline=False,
            )

        top = sorted(matched + [(n, "", a, t) for n, a, t in unmatched],
                     key=lambda x: x[3], reverse=True)[:10]
        if top:
            lines = [f"• **{n}** — {a:,} → {t:,}" for n, _, a, t in top]
            embed.add_field(name="Top contributors", value="\n".join(lines), inline=False)
        if unmatched:
            shown = unmatched[:15]
            text = ", ".join(f"`{n}` ({a:,})" for n, a, _ in shown)
            if len(unmatched) > 15:
                text += f" …+{len(unmatched) - 15} more"
            embed.add_field(
                name=(
                    f"❓ Unmatched ({len(unmatched)}) "
                    + ("— excluded" if matched_only else "— still counted")
                ),
                value=text or "—",
                inline=False,
            )
        embed.set_footer(
            text="Revenue tally only — no member silver balances are changed.",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran /audit weekly-tax "
            f"(rate={tax_rate}% commit={commit} matched_only={matched_only}): "
            f"matched={len(matched)} unmatched={len(unmatched)} "
            f"total_tax={total_tax} row_id={row_id}"
        )

    @app_commands.command(
        name="revenue",
        description="Show the guild revenue ledger (running totals + recent entries).",
    )
    @app_commands.describe(
        days="Window for the 'period' total in days (default 7).",
        limit="How many recent entries to list (1-25, default 10).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def revenue(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = 7,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        import datetime as _dt
        db = self.bot.db
        now = _dt.datetime.now(_dt.timezone.utc)
        period_iso = (now - _dt.timedelta(days=int(days))).date().isoformat()
        ytd_iso = now.date().isoformat()[:4] + "-01-01"

        period_total   = db.fetch_guild_revenue_total(since_iso=period_iso)
        ytd_total      = db.fetch_guild_revenue_total(since_iso=ytd_iso)
        all_time_total = db.fetch_guild_revenue_total()
        recent = db.fetch_recent_guild_revenue(limit=int(limit))

        embed = discord.Embed(
            title="💰  Guild revenue",
            description=(
                f"Last **{days}d**: **{period_total:,}** silver\n"
                f"YTD: **{ytd_total:,}** · All-time: **{all_time_total:,}**"
            ),
            color=discord.Color.gold(),
        )
        if recent:
            lines = []
            for r in recent:
                src = r.get("source") or "—"
                rate = r.get("rate")
                rate_part = f" @{rate}%" if rate else ""
                lines.append(
                    f"• `{r['date']}` **{int(r['amount']):,}** "
                    f"({src}{rate_part})"
                )
            embed.add_field(name="Recent entries", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Recent entries",
                value="_No revenue logged yet._",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran /audit revenue (days={days}, limit={limit})."
        )

    @app_commands.command(
        name="grant-alliance-voice-access",
        description="Grant the Alliance role Connect/Speak/View on every voice channel under the Content Voice category.",
    )
    @app_commands.describe(
        category_name="Voice category name. Defaults to '🎮 Content Voice'.",
    )
    async def grant_alliance_voice_access(
        self,
        interaction: discord.Interaction,
        category_name: str = "🎮 Content Voice",
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        alliance_role = discord.utils.get(guild.roles, name="Alliance")
        if not alliance_role:
            await interaction.followup.send(
                embed=error_embed(
                    "Alliance role missing",
                    "Run `/admin setup-roles` first to create it.",
                ),
                ephemeral=True,
            )
            return

        target_cat = None
        for cat in guild.categories:
            if cat.name.strip().lower() == category_name.strip().lower():
                target_cat = cat
                break
        if not target_cat:
            available = ", ".join(c.name for c in guild.categories) or "(none)"
            await interaction.followup.send(
                embed=error_embed(
                    "Category not found",
                    f"No category named **{category_name}**.\n\nAvailable: {available}",
                ),
                ephemeral=True,
            )
            return

        overwrite = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True,
        )
        updated, failed = 0, 0
        for ch in target_cat.channels:
            try:
                await ch.set_permissions(
                    alliance_role,
                    overwrite=overwrite,
                    reason=f"grant-alliance-voice-access by {interaction.user}",
                )
                updated += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        # Also apply on the category itself so newly-created channels inherit.
        try:
            await target_cat.set_permissions(
                alliance_role,
                overwrite=overwrite,
                reason=f"grant-alliance-voice-access by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.followup.send(
            embed=success_embed(
                "Alliance voice access granted",
                f"Category **{target_cat.name}**: {updated} channel(s) updated"
                + (f", {failed} failed" if failed else "")
                + ".\n\nNew channels added to this category will inherit these perms automatically.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} granted Alliance role voice access on "
            f"{updated} channel(s) under '{target_cat.name}' (failed={failed})."
        )


