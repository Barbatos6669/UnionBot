"""Auto-recheck for manually-overridden member registrations.

When an officer relinks a member or manually fixes their lifecycle while
the Albion public API is returning an empty/wrong guild_name, we
enqueue a *link audit*. A background loop then pulls the player's stats
once a day until either:

* The API confirms ``guild_name == expected_guild`` → audit resolved
  ``confirmed`` and a notice is posted/DMed to the requesting officer.
  No further action needed; the standard reconcile loop now drives the
  member normally.
* ``max_checks`` days pass without confirmation → audit resolved
  ``stale`` and the officer is asked to re-verify manually (the member
  may have actually left).

A persistent ``link_audits`` table tracks state across restarts.
"""

from __future__ import annotations

from cogs._typing import Bot
import asyncio
import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import albion_api
from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed


# ── DB schema + helpers ─────────────────────────────────────────────────────

def _ensure_table(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS link_audits (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id        TEXT    NOT NULL,
            expected_guild    TEXT    NOT NULL,
            expected_player_id TEXT,
            reason            TEXT,
            requested_by      TEXT,
            created_at        TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            last_checked_at   TEXT,
            checks_done       INTEGER NOT NULL DEFAULT 0,
            max_checks        INTEGER NOT NULL DEFAULT 7,
            status            TEXT    NOT NULL DEFAULT 'pending',
            resolved_at       TEXT,
            resolved_note     TEXT
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_link_audits_status "
        "ON link_audits (status, last_checked_at)"
    )


def enqueue_link_audit(
    db,
    *,
    discord_id: str,
    expected_guild: str,
    expected_player_id: Optional[str] = None,
    reason: str = "",
    requested_by: Optional[str] = None,
    max_checks: int = 7,
) -> int:
    """Insert a pending audit. Returns the new row id.

    If an unresolved audit already exists for this ``discord_id`` with the
    same ``expected_guild``, no new row is created and the existing id is
    returned (idempotent — safe to call multiple times).
    """
    _ensure_table(db)
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "SELECT id FROM link_audits "
        "WHERE discord_id = ? AND status = 'pending' "
        "  AND LOWER(expected_guild) = LOWER(?) "
        "LIMIT 1",
        (discord_id, expected_guild),
    )
    row = db.cursor.fetchone()
    if row:
        return int(row[0])
    db.execute(
        "INSERT INTO link_audits "
        "(discord_id, expected_guild, expected_player_id, reason, "
        " requested_by, max_checks) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (discord_id, expected_guild, expected_player_id, reason or None,
         requested_by, int(max_checks)),
    )
    new_id = int(db.cursor.lastrowid)
    info_log(
        f"link_audits: enqueued #{new_id} for {discord_id} "
        f"expected={expected_guild!r} player_id={expected_player_id!r} "
        f"requested_by={requested_by} reason={reason!r}"
    )
    return new_id


def _fetch_pending(db, *, due_before: dt.datetime) -> list[dict]:
    """Return pending audits whose ``last_checked_at`` is older than
    ``due_before`` (or null = never checked)."""
    _ensure_table(db)
    if not db.connection:
        db.connect()
    cutoff = due_before.strftime("%Y-%m-%d %H:%M:%S")
    db.cursor.execute(
        "SELECT id, discord_id, expected_guild, expected_player_id, "
        "       reason, requested_by, created_at, last_checked_at, "
        "       checks_done, max_checks "
        "FROM link_audits "
        "WHERE status = 'pending' "
        "  AND (last_checked_at IS NULL OR last_checked_at < ?) "
        "ORDER BY id ASC LIMIT 50",
        (cutoff,),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _fetch_all_pending(db) -> list[dict]:
    _ensure_table(db)
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "SELECT id, discord_id, expected_guild, expected_player_id, "
        "       reason, requested_by, created_at, last_checked_at, "
        "       checks_done, max_checks "
        "FROM link_audits WHERE status = 'pending' "
        "ORDER BY id ASC LIMIT 50"
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _mark_resolved(db, audit_id: int, status: str, note: str) -> None:
    db.execute(
        "UPDATE link_audits SET status = ?, resolved_at = CURRENT_TIMESTAMP, "
        "resolved_note = ? WHERE id = ?",
        (status, note, int(audit_id)),
    )


def _bump_check(db, audit_id: int) -> None:
    db.execute(
        "UPDATE link_audits "
        "SET checks_done = checks_done + 1, "
        "    last_checked_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (int(audit_id),),
    )


# ── audit runner ───────────────────────────────────────────────────────────


async def _check_one(bot: Bot, row: dict) -> tuple[str, str]:
    """Run one API pull. Returns ``(status, note)`` where status is one of
    'confirmed', 'pending', 'stale'. The caller writes the result."""
    db = bot.db  # type: ignore[attr-defined]
    discord_id   = row["discord_id"]
    expected     = (row["expected_guild"] or "").strip()
    player_id    = row.get("expected_player_id")
    checks_done  = int(row.get("checks_done") or 0)
    max_checks   = int(row.get("max_checks") or 7)

    profile = db.fetch_user_profile(discord_id)
    if not profile:
        return ("stale", f"profile gone for {discord_id}")
    player_id = player_id or profile.get("albion_player_id")
    if not player_id:
        return ("stale", "no albion_player_id on profile")

    try:
        data = await asyncio.to_thread(albion_api.get_player_stats, player_id)
    except Exception as exc:  # noqa: BLE001
        return ("pending", f"API error: {exc!r}")
    if not data:
        return ("pending", "API returned no data")
    stats = albion_api.parse_stats(data)
    api_guild = (stats.get("guild_name") or "").strip()
    if api_guild and api_guild.lower() == expected.lower():
        # Confirmed — push the fresh stats so the profile is now driven by
        # real API values and the standard reconcile loop owns the member.
        try:
            db.update_user_albion_info(
                discord_id, player_id,
                stats.get("albion_name") or profile.get("albion_name"),
                stats,
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"link_audits: update_user_albion_info failed: {exc!r}")
        return (
            "confirmed",
            f"API now reports {api_guild!r} (kf={stats.get('kill_fame', 0):,}, "
            f"pve={stats.get('pve_total', 0):,}, ip={stats.get('average_item_power', 0.0):.0f})",
        )
    if checks_done + 1 >= max_checks:
        return (
            "stale",
            f"checked {max_checks}× — API guild={api_guild!r} ≠ expected={expected!r}",
        )
    return (
        "pending",
        f"API guild={api_guild!r} (still mismatched, check {checks_done + 1}/{max_checks})",
    )


async def _notify_officer(bot: Bot, row: dict, status: str, note: str) -> None:
    """Best-effort: DM the officer who requested the audit, and post in
    the officer channel if configured. Silent on failure."""
    db = bot.db  # type: ignore[attr-defined]
    requested_by = row.get("requested_by")
    discord_id   = row["discord_id"]
    expected     = row["expected_guild"]

    pretty_status = {
        "confirmed": "✅ Confirmed",
        "stale":     "⚠️ Stale",
    }.get(status, status)

    member_mention = f"<@{discord_id}>"
    body = (
        f"**Audit #{row['id']}** for {member_mention}\n"
        f"Expected guild: **{expected}**\n"
        f"Result: {pretty_status}\n"
        f"> {note}"
    )

    # DM the requesting officer
    if requested_by:
        try:
            user = bot.get_user(int(requested_by)) or await bot.fetch_user(int(requested_by))
            if user:
                embed = info_embed("Link audit result", body)
                await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, ValueError):
            pass

    # And the officer channel
    try:
        chan_id = db.get_config("officer_channel_id")
        if chan_id:
            ch = bot.get_channel(int(chan_id))
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                embed = info_embed("Link audit result", body)
                await ch.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException, ValueError):
        pass


async def run_pending_audits(bot: Bot, *, force: bool = False) -> dict:
    """Run all due audits once. Returns a small summary dict.

    ``force=True`` checks every pending audit regardless of the
    once-per-24h cooldown. Used by the manual ``/link-audit check`` cmd.
    """
    db = bot.db  # type: ignore[attr-defined]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=23)
    rows = _fetch_all_pending(db) if force else _fetch_pending(db, due_before=cutoff)

    confirmed = stale = still_pending = 0
    for r in rows:
        status, note = await _check_one(bot, r)
        _bump_check(db, r["id"])
        if status in ("confirmed", "stale"):
            _mark_resolved(db, r["id"], status, note)
            await _notify_officer(bot, r, status, note)
            if status == "confirmed":
                confirmed += 1
            else:
                stale += 1
        else:
            still_pending += 1
    summary = {
        "checked": len(rows),
        "confirmed": confirmed,
        "stale": stale,
        "pending": still_pending,
    }
    if rows:
        info_log(f"link_audits: ran {summary}")
    return summary


# ── slash commands ─────────────────────────────────────────────────────────


@app_commands.default_permissions(manage_guild=True)
class LinkAuditGroup(app_commands.Group):
    """Manage pending link-audit follow-ups."""

    def __init__(self, bot: Bot) -> None:
        super().__init__(name="link-audit", description="Manage member API-link audits.")
        self.bot: Bot = bot

    @app_commands.command(name="list", description="Show pending link audits.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = _fetch_all_pending(self.bot.db)  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No pending audits", "Nothing queued."),
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for r in rows:
            mention = f"<@{r['discord_id']}>"
            last = r.get("last_checked_at") or "never"
            lines.append(
                f"**#{r['id']}** · {mention} · expect **{r['expected_guild']}** · "
                f"{r['checks_done']}/{r['max_checks']} checks · last={last}"
            )
        await interaction.response.send_message(
            embed=info_embed(f"Pending link audits ({len(rows)})", "\n".join(lines)),
            ephemeral=True,
        )

    @app_commands.command(
        name="add",
        description="Manually enqueue a member for daily API recheck against an expected guild.",
    )
    @app_commands.describe(
        member="Member to audit.",
        expected_guild="Guild name the API should eventually report (default: home guild).",
        max_checks="How many daily checks before giving up (default 7).",
        reason="Optional note (why this audit was enqueued).",
    )
    async def add_cmd(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        expected_guild: Optional[str] = None,
        max_checks: app_commands.Range[int, 1, 30] = 7,
        reason: Optional[str] = None,
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        expected = (expected_guild or db.get_config("home_guild_name") or "").strip()
        if not expected:
            await interaction.response.send_message(
                embed=error_embed(
                    "No expected_guild",
                    "Pass `expected_guild` explicitly, or set "
                    "`home_guild_name` in config first.",
                ),
                ephemeral=True,
            )
            return
        profile = db.fetch_user_profile(str(member.id))
        player_id = profile.get("albion_player_id") if profile else None
        rid = enqueue_link_audit(
            db,
            discord_id=str(member.id),
            expected_guild=expected,
            expected_player_id=player_id,
            reason=reason or "manual enqueue",
            requested_by=str(interaction.user.id),
            max_checks=int(max_checks),
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Audit queued",
                f"#{rid} · {member.mention} · expecting **{expected}** · "
                f"up to {int(max_checks)} daily checks.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="cancel", description="Cancel a pending audit by id.")
    @app_commands.describe(audit_id="ID shown in /link-audit list.")
    async def cancel_cmd(
        self,
        interaction: discord.Interaction,
        audit_id: app_commands.Range[int, 1, 1_000_000_000],
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "UPDATE link_audits SET status = 'cancelled', "
            "resolved_at = CURRENT_TIMESTAMP, resolved_note = ? "
            "WHERE id = ? AND status = 'pending'",
            (f"cancelled by {interaction.user}", int(audit_id)),
        )
        db.connection.commit()
        ok = db.cursor.rowcount > 0
        if ok:
            await interaction.response.send_message(
                embed=success_embed("Cancelled", f"Audit #{int(audit_id)} cancelled."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} cancelled link audit #{int(audit_id)}.")
        else:
            await interaction.response.send_message(
                embed=error_embed("Nothing to cancel", f"No pending audit #{int(audit_id)}."),
                ephemeral=True,
            )

    @app_commands.command(
        name="run-now",
        description="Force-run all pending audits right now (bypasses 24h cooldown).",
    )
    async def run_now_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        summary = await run_pending_audits(self.bot, force=True)
        await interaction.followup.send(
            embed=info_embed(
                "Audits run",
                f"Checked **{summary['checked']}** — "
                f"✅ confirmed **{summary['confirmed']}** · "
                f"⚠️ stale **{summary['stale']}** · "
                f"⏳ still pending **{summary['pending']}**.",
            ),
            ephemeral=True,
        )


# ── cog ────────────────────────────────────────────────────────────────────


class LinkAudit(commands.Cog):
    """Owns the daily background loop and registers the slash group."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        _ensure_table(self.bot.db)  # type: ignore[attr-defined]
        self.bot.tree.add_command(LinkAuditGroup(bot))
        self._daily.start()
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:  # type: ignore[override]
        self._daily.cancel()
        # Reload-safety: drop the manually-added /link-audit group.
        try:
            self.bot.tree.remove_command("link-audit")
        except Exception:  # noqa: BLE001
            pass

    @tasks.loop(hours=6)
    async def _daily(self) -> None:
        # Loop every 6h so a fresh enqueue doesn't sit a full day before
        # its first check, but the per-audit cooldown in _fetch_pending
        # still guarantees ~once per 23h per row.
        try:
            await run_pending_audits(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"link_audits daily loop: {exc!r}")

    @_daily.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Bot) -> None:
    await bot.add_cog(LinkAudit(bot))
