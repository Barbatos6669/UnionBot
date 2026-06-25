"""Automation cog — schedulers and hooks that automate guild ops.

Scheduled loops (started by ``cog_load``):

* ``daily_tick``        — once per UTC day at 00:05.
                          Anniversaries, inactivity sweep, SOP drift,
                          officer ops digest, daily channel topic,
                          treasury check-in prompt, stale LFG event archival,
                          orphan-guild cleanup.

* ``minute_tick``       — every 5 minutes.
                          Voice-presence snapshots
                          during live events, post-event reconciliation,
                          orphan reminder cleanup.

Hooks called by ``cogs/events.py`` during the hourly Albion sync:

* :func:`check_fame_milestones`  — posts hall-of-fame embeds for big jumps
                                   in any tracked fame metric.
* :func:`check_anti_poach`       — alerts officers when an in-home-guild
                                   member's guild_name changes.

All notification destinations are configured via guild_config keys; they
silently skip if unset.

Config keys consumed:
    automation_officer_channel_id
    automation_announcements_channel_id
    automation_hall_of_fame_channel_id
    automation_topic_channel_id
    automation_voice_channel_id
    automation_event_reminders_enabled     (legacy, default off; LFG cog owns reminders)
    automation_event_reminder_minutes      (legacy, int, default 30)
    automation_inactivity_threshold_days   (int, default 21)
    automation_kill_milestone_threshold    (int, default 1_000_000)
    automation_voice_attendance_min_pct    (int, default 50)
    automation_event_reconcile_grace_minutes (int, default 30; no-VC fallback)
    home_guild_name                        (set elsewhere)
"""
from __future__ import annotations

from cogs._lfg_config import display_slot_label
from cogs._typing import Bot
import asyncio
import datetime
import hashlib

import discord
from discord.ext import commands, tasks

from debug import info_log, error_log
from config import LIFECYCLE_ROLES, STAFF_ROLES
from utils import error_embed, info_embed, success_embed


# ── Helpers / dashboard (extracted to sibling _automation_*.py modules) ─────
from cogs._automation_helpers import (
    _DEFAULT_REMINDER_MIN,
    _DEFAULT_INACTIVE_DAYS,
    _DEFAULT_UNVERIFIED_KICK_DAYS,
    _DEFAULT_UNVERIFIED_NUDGE_DAYS,
    _DEFAULT_UNVERIFIED_NUDGE_COOLDOWN_DAYS,
    _DEFAULT_UNVERIFIED_NUDGE_MAX,
    _DEFAULT_AUTO_ALUMNI_DAYS,
    _DEFAULT_INACTIVITY_NUDGE_LEAD_DAYS,
    _DEFAULT_INACTIVITY_NUDGE_COOLDOWN_DAYS,
    _DEFAULT_HELP_TICKET_SLA_HOURS,
    _DEFAULT_VOICE_PCT,
    _DEFAULT_UNDERFILL_LEAD_MIN,
    _DEFAULT_UNDERFILL_THRESHOLD,
    _DEFAULT_FAME_METRICS,
    _now,
    _get_int_config,
    _channel,
    _snooze_key,
    _is_snoozed,
    _clear_snooze,
)
from cogs._automation_dashboard import (
    _build_officer_dashboard_embed,
    OfficerDashboardView,
)
from cogs._automation_unverified import (
    _collect_stale_unverified_role_members,
    _collect_unverified_kick_targets,
    _run_unverified_kicks,
)
from cogs._automation_registration import (
    _run_unverified_nudges,
    _unverified_age_days,
)
from cogs._event_reports import (
    batch_embeds_for_send,
    build_event_report_embed,
    build_event_report_view,
    register_persistent_event_report_views,
)


_HOF_QUEUE_TABLE = "hall_of_fame_digest_queue"
_DEFAULT_EVENT_RECONCILE_GRACE_MIN = 30
_EVENT_REPORT_PENDING_RETRY_LIMIT = 2


def _utc_iso(when: datetime.datetime) -> str:
    aware = when.astimezone(datetime.timezone.utc).replace(microsecond=0)
    return aware.replace(tzinfo=None).isoformat()


def _ensure_hof_queue(db) -> None:
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_HOF_QUEUE_TABLE} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            discord_id  TEXT NOT NULL,
            albion_name TEXT,
            metric_key  TEXT NOT NULL,
            label       TEXT NOT NULL,
            emoji       TEXT,
            delta       INTEGER NOT NULL,
            total       INTEGER
        )
        """,
        quiet=True,
    )


def _queue_hof_entry(
    bot: Bot,
    *,
    discord_id: str,
    albion_name: str,
    metric_key: str,
    label: str,
    emoji: str,
    delta: int,
    total: int,
) -> None:
    _ensure_hof_queue(bot.db)
    bot.db.execute(
        f"""
        INSERT INTO {_HOF_QUEUE_TABLE}
            (created_at, discord_id, albion_name, metric_key, label, emoji, delta, total)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _now().isoformat(),
            str(discord_id),
            str(albion_name or discord_id),
            str(metric_key),
            str(label),
            str(emoji or "🏆"),
            int(delta),
            int(total or 0),
        ),
        quiet=True,
    )


# ── Hooks called from events.py ─────────────────────────────────────────────

async def check_fame_milestones(
    bot: Bot, profile: dict, deltas: dict[str, int],
) -> None:
    """If a player gained at least the configured threshold of any tracked
    fame metric *during the past sync window*, queue a Hall of Fame line for
    the combined hourly activity dispatch and award the configured points bonus.

    Only home-guild members are eligible. Dedup key is the wall-clock hour
    so a given player/metric is shouted out at most once per hour even if
    the sync runs twice.

    Each metric has its own default threshold (see ``_DEFAULT_FAME_METRICS``).
    Override per metric via config ``automation_milestone_threshold_<metric>``
    or override all metrics with ``automation_kill_milestone_threshold``."""
    global_override = _get_int_config(
        bot.db, "automation_kill_milestone_threshold", 0,
    )
    if not bot.db.get_config("points_announce_channel_id"):
        return

    # Restrict shoutouts to home-guild members.
    home = (bot.db.get_config("home_guild_name") or "").strip()
    if home:
        current_guild = (profile.get("guild_name") or "").strip()
        if current_guild.lower() != home.lower():
            return

    name = profile.get("albion_name") or profile["discord_id"]
    discord_id = str(profile["discord_id"])
    hour_bucket = int(_now().timestamp()) // 3600

    posted_any = False
    for metric_key, label, emoji, default_threshold in _DEFAULT_FAME_METRICS:
        threshold = _get_int_config(
            bot.db,
            f"automation_milestone_threshold_{metric_key}",
            global_override or default_threshold,
        )
        if threshold <= 0:
            continue
        delta = int(deltas.get(metric_key) or 0)
        if delta < threshold:
            continue
        # Dedup per (player, metric, wall-clock hour) — celebrates the
        # session/window, not the lifetime cumulative total.
        if bot.db.has_milestone_posted(discord_id, metric_key, hour_bucket):
            continue
        bot.db.mark_milestone_posted(discord_id, metric_key, hour_bucket)
        posted_any = True

        _queue_hof_entry(
            bot,
            discord_id=discord_id,
            albion_name=str(name),
            metric_key=metric_key,
            label=label,
            emoji=emoji,
            delta=delta,
            total=int(profile.get(metric_key) or 0),
        )

        # Bonus points.
        try:
            from cogs.points import get_point_setting
            bonus = get_point_setting(bot.db, "points_kill_milestone")
            if bonus > 0:
                bot.db.add_points(discord_id, bonus)
                info_log(
                    f"Awarded {bonus} milestone bonus point(s) to {name} "
                    f"({metric_key})."
                )
        except Exception as exc:  # noqa: BLE001
            error_log(f"milestone points hook failed: {exc!r}")

    if posted_any:
        info_log(f"Queued fame milestone(s) for {name}.")


async def check_anti_poach(
    bot: Bot, profile: dict, old_guild: str | None, new_guild: str | None,
) -> None:
    """Alert officers when an in-home-guild member moves to another guild."""
    if old_guild == new_guild:
        return
    home = (bot.db.get_config("home_guild_name") or "").strip()
    if not home:
        return
    was_home = (old_guild or "").strip().lower() == home.lower()
    is_home = (new_guild or "").strip().lower() == home.lower()
    if not was_home or is_home:
        return  # only flag *leaving* the home guild

    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return

    name = profile.get("albion_name") or profile.get("discord_id")
    embed = discord.Embed(
        title="⚠️  Member left home guild",
        description=(
            f"**{name}** (<@{profile['discord_id']}>) is now in "
            f"**{new_guild or 'no guild'}** — was **{home}**."
        ),
        color=discord.Color.orange(),
    )
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        info_log(f"Anti-poach alert: {name} moved {old_guild} → {new_guild}.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"anti-poach post failed: {exc!r}")


def cleanup_orphan_guilds(bot: Bot) -> None:
    """Drop tracked guilds that no longer have any registered member."""
    n = bot.db.delete_orphan_guilds()
    if n:
        info_log(f"Pruned {n} orphan guild row(s).")


def archive_completed_events(bot: Bot) -> None:
    """Mark passed open events as completed."""
    n = bot.db.archive_completed_events()
    if n:
        info_log(f"Archived {n} completed LFG event(s).")


# ── Daily routines ──────────────────────────────────────────────────────────

async def _post_anniversaries(bot: Bot) -> None:
    channel = _channel(bot, "automation_announcements_channel_id")
    if channel is None:
        return
    if not bot.db.connection:
        bot.db.connect()
    bot.db.cursor.execute(
        "SELECT discord_id, albion_name, verified_date "
        "FROM user_profiles WHERE verified_date IS NOT NULL"
    )
    rows = [dict(r) for r in bot.db.cursor.fetchall()]
    today = _now().date()
    posted = 0
    for r in rows:
        try:
            v = datetime.datetime.fromisoformat(
                str(r["verified_date"]).replace("Z", "+00:00")
            ).date()
        except (TypeError, ValueError):
            continue
        # An "anniversary" fires when same month-day as verified_date and at
        # least one full year has passed.
        if v.month != today.month or v.day != today.day:
            continue
        years = today.year - v.year
        if years < 1:
            continue
        did = str(r["discord_id"])
        if bot.db.has_anniversary_posted(did, years):
            continue
        bot.db.mark_anniversary_posted(did, years)

        suffix = "year" if years == 1 else "years"
        embed = discord.Embed(
            title=f"🎉  {years} {suffix} in the guild!",
            description=(
                f"<@{did}> hit **{years} {suffix}** with us today. "
                f"Thanks for sticking around!"
            ),
            color=discord.Color.purple(),
        )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            posted += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"anniversary post failed: {exc!r}")

        # Bonus points per year of tenure.
        try:
            from cogs.points import get_point_setting
            bonus = get_point_setting(bot.db, "points_anniversary") * years
            if bonus > 0:
                bot.db.add_points(did, bonus)
                info_log(
                    f"Awarded {bonus} anniversary point(s) to {did} "
                    f"({years}yr tenure)."
                )
        except Exception as exc:  # noqa: BLE001
            error_log(f"anniversary points failed: {exc!r}")
    if posted:
        info_log(f"Posted {posted} anniversary celebration(s).")


async def _post_inactivity_sweep(bot: Bot) -> None:
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return
    if _is_snoozed(bot, "inactivity"):
        info_log("Inactivity sweep snoozed; skipping post.")
        return
    embed, count = _build_inactivity_embed(bot)
    if embed is None:
        return
    try:
        await channel.send(
            embed=embed,
            view=InactivitySweepView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        info_log(f"Inactivity sweep posted: {count} candidates.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"inactivity sweep post failed: {exc!r}")


def _build_inactivity_embed(
    bot: Bot,
) -> tuple[discord.Embed | None, int]:
    """Run the inactivity query and return (embed, count). Returns (None, 0)
    if nobody is over threshold."""
    days = _get_int_config(
        bot.db, "automation_inactivity_threshold_days", _DEFAULT_INACTIVE_DAYS,
    )
    threshold_iso = (
        _now() - datetime.timedelta(days=days)
    ).isoformat()
    home = (bot.db.get_config("home_guild_name") or "").strip() or None
    rows = bot.db.fetch_inactive_profiles(threshold_iso, home_guild=home)
    if not rows:
        return None, 0
    lines = []
    for r in rows[:25]:
        last = r.get("last_activity_date") or "never"
        lines.append(
            f"• <@{r['discord_id']}> ({r.get('albion_name') or '—'}) — "
            f"`{r.get('lifecycle_role') or '—'}` · last active: `{last}`"
        )
    suffix = ""
    if len(rows) > 25:
        suffix = f"\n…and {len(rows) - 25} more."
    embed = discord.Embed(
        title=f"🧹  Inactivity sweep — {len(rows)} members > {days}d idle",
        description="\n".join(lines) + suffix,
        color=discord.Color.dark_gold(),
    )
    embed.set_footer(text="Review and decide: kick / warn / ignore.")
    return embed, len(rows)


_VC_ACTIVE_LIFECYCLES = {"Recruit", "Probationary", "Member", "Veteran"}


def _format_voice_seconds(seconds: int) -> str:
    minutes = max(0, int(seconds or 0)) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def _last_voice_date(bot: Bot, discord_id: str) -> str | None:
    try:
        if not bot.db.connection:
            bot.db.connect()
        bot.db.cursor.execute(
            "SELECT MAX(date_utc) AS last_voice "
            "FROM voice_activity WHERE discord_id = ? AND seconds > 0",
            (str(discord_id),),
        )
        row = bot.db.cursor.fetchone()
        return str(row["last_voice"]) if row and row["last_voice"] else None
    except Exception as exc:  # noqa: BLE001
        error_log(f"last_voice_date failed for {discord_id}: {exc!r}")
        return None


def _on_active_loa(profile: dict) -> bool:
    loa_until = str(profile.get("loa_until") or "").strip()
    if not loa_until:
        return False
    return loa_until >= _now().date().isoformat()


def _collect_vc_inactive_targets(
    bot: Bot,
    guild: discord.Guild,
    *,
    days: int,
    min_minutes: int,
) -> list[dict]:
    """Current TU members below the voice threshold.

    This intentionally checks current Discord membership and the current
    HomeGuild role, because stale Albion/API profile data can outlive a
    member leaving the server.
    """
    home = (bot.db.get_config("home_guild_name") or "HomeGuild").strip()
    since_date = (_now().date() - datetime.timedelta(days=int(days))).isoformat()
    min_seconds = max(0, int(min_minutes)) * 60
    tu_role = discord.utils.get(guild.roles, name="HomeGuild")
    staff_names = set(STAFF_ROLES)
    targets: list[dict] = []

    for member in guild.members:
        if member.bot:
            continue
        if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
            continue
        if any(role.name in staff_names for role in member.roles):
            continue
        if tu_role is not None and tu_role not in member.roles:
            continue

        profile = bot.db.fetch_user_profile(str(member.id))
        if not profile:
            continue
        if (profile.get("guild_name") or "").strip().lower() != home.lower():
            continue
        lifecycle = (profile.get("lifecycle_role") or "").strip()
        if lifecycle and lifecycle not in _VC_ACTIVE_LIFECYCLES:
            continue
        if _on_active_loa(profile):
            continue

        seconds = int(bot.db.fetch_voice_seconds_window(str(member.id), since_date) or 0)
        if seconds >= min_seconds:
            continue
        targets.append({
            "member": member,
            "discord_id": str(member.id),
            "albion_name": profile.get("albion_name") or member.display_name,
            "lifecycle_role": lifecycle or "—",
            "voice_seconds": seconds,
            "last_voice_date": _last_voice_date(bot, str(member.id)),
        })

    targets.sort(key=lambda r: (int(r["voice_seconds"]), str(r["albion_name"]).lower()))
    return targets


def _vc_inactive_line(index: int, row: dict) -> str:
    member = row["member"]
    voice = _format_voice_seconds(int(row.get("voice_seconds") or 0))
    last_voice = row.get("last_voice_date") or "never"
    return (
        f"`#{index:02d}` {member.mention} ({row.get('albion_name') or member.display_name}) — "
        f"`{row.get('lifecycle_role') or '—'}` · VC `{voice}` · last `{last_voice}`"
    )


def _build_vc_inactive_embeds(
    targets: list[dict],
    *,
    days: int,
    min_minutes: int,
    applied: bool = False,
    failures: list[str] | None = None,
) -> list[discord.Embed]:
    title = (
        f"🎙️ VC-inactivity sweep — {len(targets)} updated"
        if applied else
        f"🎙️ VC-inactivity preview — {len(targets)} candidate(s)"
    )
    if not targets:
        return [info_embed(
            "VC-inactivity preview",
            f"No active TU members are under **{min_minutes}m** voice time in the last **{days}d**.",
        )]

    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for idx, row in enumerate(targets, 1):
        line = _vc_inactive_line(idx, row)
        # Keep each embed comfortably below Discord's limits while showing
        # every member. Count newlines too so we do not land on the edge.
        if current and (len(current) >= 20 or current_len + len(line) + 1 > 3300):
            pages.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line) + 1
    if current:
        pages.append(current)

    action = (
        "Applied: moved listed members to Inactive and removed HomeGuild."
        if applied else
        "Dry run only. Use /automation vc-inactive-sweep apply:true to apply."
    )
    embeds: list[discord.Embed] = []
    for page_num, page_lines in enumerate(pages, 1):
        embed = discord.Embed(
            title=title if page_num == 1 else f"{title} — continued",
            description="\n".join(page_lines),
            color=discord.Color.orange() if not applied else discord.Color.dark_gold(),
        )
        embed.set_footer(
            text=(
                f"Page {page_num}/{len(pages)} · Threshold: < {min_minutes}m VC in {days}d · "
                f"Staff/admin/LOA skipped · {action}"
            ),
        )
        embeds.append(embed)

    if failures:
        failure_lines = [f"• {line}" for line in failures]
        chunks: list[list[str]] = []
        current = []
        current_len = 0
        for line in failure_lines:
            if current and (len(current) >= 20 or current_len + len(line) + 1 > 3300):
                chunks.append(current)
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append(current)
        for idx, chunk in enumerate(chunks, 1):
            embed = discord.Embed(
                title="⚠️ VC-inactivity sweep failures" if idx == 1 else "⚠️ VC-inactivity failures — continued",
                description="\n".join(chunk),
                color=discord.Color.red(),
            )
            embed.set_footer(text=f"Failures page {idx}/{len(chunks)}")
            embeds.append(embed)
    return embeds


async def _apply_vc_inactive_targets(
    bot: Bot,
    guild: discord.Guild,
    targets: list[dict],
    *,
    days: int,
    min_minutes: int,
    actor: discord.abc.User,
) -> tuple[list[dict], list[str]]:
    tu_role = discord.utils.get(guild.roles, name="HomeGuild")
    inactive_role = discord.utils.get(guild.roles, name="Inactive")
    lifecycle_names = set(LIFECYCLE_ROLES)
    applied: list[dict] = []
    failures: list[str] = []
    reason = f"VC inactive sweep by {actor}: < {min_minutes}m in {days}d"

    for row in targets:
        member: discord.Member = row["member"]
        lifecycle_roles = [
            role for role in member.roles
            if role.name in lifecycle_names and role.name != "Inactive"
        ]
        remove_roles = list(lifecycle_roles)
        if tu_role is not None and tu_role in member.roles:
            remove_roles.append(tu_role)
        try:
            if remove_roles:
                await member.remove_roles(*remove_roles, reason=reason)
            if inactive_role is not None and inactive_role not in member.roles:
                await member.add_roles(inactive_role, reason=reason)
            bot.db.set_lifecycle_role(str(member.id), "Inactive")
            applied.append(row)
            await asyncio.sleep(0.25)
        except discord.Forbidden:
            failures.append(f"{member.display_name}: missing role permissions or role hierarchy.")
        except discord.HTTPException as exc:
            failures.append(f"{member.display_name}: {exc}")
    return applied, failures


# ── Auto-Alumni: long-inactive members ──────────────────────────────────────
#
# Members already at lifecycle ``Inactive`` whose last_activity_date is older
# than ``automation_auto_alumni_days`` (default 60) get demoted to ``Alumni``.
# This closes the loop on the inactivity flow — without it, members sit
# at Inactive forever.
#
# Gated by ``automation_auto_alumni_enabled`` (default 0 = disabled).
# Posts a summary to the officer-tasks channel.

def _collect_auto_alumni_targets(
    bot: Bot, days: int,
) -> list[dict]:
    """Return profile rows eligible for Inactive → Alumni demotion."""
    cutoff = _now() - datetime.timedelta(days=days)
    cutoff_iso = cutoff.isoformat()
    db = bot.db
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT discord_id, albion_name, last_activity_date, "
            "       lifecycle_role, verified_date "
            "  FROM user_profiles "
            " WHERE lifecycle_role = 'Inactive' "
            "   AND last_activity_date IS NOT NULL "
            "   AND last_activity_date < ?",
            (cutoff_iso,),
        )
        return [dict(r) for r in db.cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001
        error_log(f"_collect_auto_alumni_targets failed: {exc!r}")
        return []


async def _run_auto_alumni(bot: Bot) -> None:
    """Daily sweep: demote long-Inactive members to Alumni.

    No-op unless ``automation_auto_alumni_enabled`` is truthy.
    """
    enabled = _get_int_config(bot.db, "automation_auto_alumni_enabled", 0)
    if not enabled:
        return
    days = _get_int_config(
        bot.db, "automation_auto_alumni_days", _DEFAULT_AUTO_ALUMNI_DAYS,
    )
    if days < 1:
        info_log(f"auto-alumni days={days} invalid; skipping.")
        return
    targets = _collect_auto_alumni_targets(bot, days)
    if not targets:
        return

    demoted: list[str] = []
    failed: list[str] = []
    for profile in targets:
        did = str(profile["discord_id"])
        target_member: discord.Member | None = None
        target_guild: discord.Guild | None = None
        for guild in bot.guilds:
            m = guild.get_member(int(did))
            if m:
                target_member = m
                target_guild = guild
                break
        if target_member is None or target_guild is None:
            # User left server — just update DB.
            try:
                bot.db.set_lifecycle_role(did, "Alumni")
                demoted.append(
                    f"{profile.get('albion_name') or did} (left server)"
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{did}: db error {exc!r}")
            continue
        old_role = discord.utils.get(target_guild.roles, name="Inactive")
        new_role = discord.utils.get(target_guild.roles, name="Alumni")
        tu_role = discord.utils.get(target_guild.roles, name="HomeGuild")
        try:
            remove_roles = [
                role for role in (old_role, tu_role)
                if role and role in target_member.roles
            ]
            if remove_roles:
                await target_member.remove_roles(
                    *remove_roles,
                    reason="Auto-Alumni after long inactivity",
                )
            if new_role and new_role not in target_member.roles:
                await target_member.add_roles(new_role, reason="Auto-Alumni after long inactivity")
            bot.db.set_lifecycle_role(did, "Alumni")
            demoted.append(
                f"{target_member.mention} ({profile.get('albion_name') or '—'})"
            )
            info_log(
                f"Auto-Alumni: {target_member} demoted Inactive → Alumni "
                f"after {days}+ days idle."
            )
        except discord.Forbidden:
            failed.append(f"{target_member}: missing role perms / hierarchy")
        except discord.HTTPException as exc:
            failed.append(f"{target_member}: {exc}")

    officer_channel = _channel(bot, "automation_officer_channel_id")
    if officer_channel is None:
        info_log(
            f"Auto-alumni: {len(demoted)} demoted, {len(failed)} failed; "
            "no officer channel configured."
        )
        return
    lines: list[str] = []
    if demoted:
        lines.append(f"**Demoted ({len(demoted)}):**")
        lines.extend(f"• {d}" for d in demoted[:25])
        if len(demoted) > 25:
            lines.append(f"…and {len(demoted) - 25} more.")
    if failed:
        lines.append(f"\n**Failed ({len(failed)}):**")
        lines.extend(f"• {f}" for f in failed[:10])
    embed = discord.Embed(
        title=f"📤  Auto-Alumni — {len(demoted)} demoted",
        description="\n".join(lines) or "Nothing to do.",
        color=discord.Color.dark_grey(),
    )
    embed.set_footer(text=f"Threshold: Inactive > {days} days")
    try:
        await officer_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"auto-alumni summary post failed: {exc!r}")


# ── Inactivity nudge DM ─────────────────────────────────────────────────────
#
# Members whose last_activity_date sits inside a "warning window" before the
# inactivity threshold get a friendly DM saying they're about to lose their
# active status. Helps with retention — currently the inactivity sweep is
# silent until officers act, so members get no chance to course-correct.
#
# Window math (defaults shown):
#   inactivity threshold = 21d            (automation_inactivity_threshold_days)
#   nudge lead           = 7d              (automation_inactivity_nudge_lead_days)
#   → nudge anyone whose last activity is between 14d and 21d ago
#   cooldown             = 14d             (automation_inactivity_nudge_cooldown_days)
#   → don't nudge the same member more than once every 14d
#
# Gated by ``automation_inactivity_nudge_enabled`` (default 0 = off) so
# nothing fires until an officer explicitly turns it on.
# Posts a summary to the officer-tasks channel.

async def _run_inactivity_nudge(bot: Bot) -> None:
    enabled = _get_int_config(bot.db, "automation_inactivity_nudge_enabled", 0)
    if not enabled:
        return
    threshold_days = _get_int_config(
        bot.db, "automation_inactivity_threshold_days", _DEFAULT_INACTIVE_DAYS,
    )
    lead_days = _get_int_config(
        bot.db, "automation_inactivity_nudge_lead_days",
        _DEFAULT_INACTIVITY_NUDGE_LEAD_DAYS,
    )
    cooldown_days = _get_int_config(
        bot.db, "automation_inactivity_nudge_cooldown_days",
        _DEFAULT_INACTIVITY_NUDGE_COOLDOWN_DAYS,
    )
    if threshold_days < 1 or lead_days < 1 or lead_days >= threshold_days:
        info_log(
            f"inactivity-nudge config invalid "
            f"(threshold={threshold_days}, lead={lead_days}); skipping."
        )
        return

    now = _now()
    today_iso = now.date().isoformat()
    # Window: members who went idle between (threshold) and (threshold-lead).
    # i.e. last_activity_date in [now - threshold, now - (threshold - lead))
    idle_low_iso  = (now - datetime.timedelta(days=threshold_days)).isoformat()
    idle_high_iso = (now - datetime.timedelta(days=threshold_days - lead_days)).isoformat()
    cooldown_iso  = (now - datetime.timedelta(days=cooldown_days)).date().isoformat()
    home = (bot.db.get_config("home_guild_name") or "").strip() or None

    rows = bot.db.fetch_inactivity_nudge_targets(
        idle_low_iso=idle_low_iso,
        idle_high_iso=idle_high_iso,
        cooldown_iso=cooldown_iso,
        home_guild=home,
    )
    if not rows:
        return

    nudged: list[str] = []
    skipped_dms: list[str] = []
    failed: list[str] = []
    for r in rows:
        did = str(r["discord_id"])
        member: discord.Member | None = None
        guild_name = "the guild"
        for guild in bot.guilds:
            m = guild.get_member(int(did))
            if m:
                member = m
                guild_name = guild.name
                break
        if member is None:
            # Left server — don't nudge, but don't burn cooldown either.
            continue
        if member.bot:
            continue
        # Compute days idle for the message body.
        last = r.get("last_activity_date") or ""
        days_idle = "?"
        try:
            last_dt = datetime.datetime.fromisoformat(last)
            days_idle = str((now - last_dt).days)
        except (ValueError, TypeError):
            pass
        days_left = max(threshold_days - int(days_idle), 0) if days_idle.isdigit() else lead_days
        try:
            await member.send(
                f"👋 Hey, just a heads-up from **{guild_name}** — we haven't seen "
                f"any tracked activity from you in **{days_idle} days**. "
                f"In about **{days_left} day(s)** you'd be flagged as Inactive.\n"
                "Hop in, run a few mobs, or just say hi — anything counts. 💚"
            )
            bot.db.mark_inactivity_nudge_sent(did, today_iso)
            nudged.append(
                f"{member.mention} ({r.get('albion_name') or '—'}) — {days_idle}d idle"
            )
        except discord.Forbidden:
            # DMs closed — record it so we don't retry on next run, then move on.
            bot.db.mark_inactivity_nudge_sent(did, today_iso)
            skipped_dms.append(f"{member} ({did}) — DMs closed")
        except discord.HTTPException as exc:
            failed.append(f"{member} ({did}): {exc}")

    info_log(
        f"inactivity-nudge: nudged={len(nudged)} dm-closed={len(skipped_dms)} "
        f"failed={len(failed)} threshold={threshold_days}d lead={lead_days}d."
    )
    officer_channel = _channel(bot, "automation_officer_channel_id")
    if officer_channel is None:
        return
    if not (nudged or skipped_dms or failed):
        return
    parts: list[str] = []
    if nudged:
        parts.append(f"**Nudged ({len(nudged)}):**")
        parts.extend(f"• {n}" for n in nudged[:25])
        if len(nudged) > 25:
            parts.append(f"…and {len(nudged) - 25} more.")
    if skipped_dms:
        parts.append(f"\n**DMs closed ({len(skipped_dms)}):**")
        parts.extend(f"• {n}" for n in skipped_dms[:10])
    if failed:
        parts.append(f"\n**Errors ({len(failed)}):**")
        parts.extend(f"• {n}" for n in failed[:10])
    embed = discord.Embed(
        title=f"💌  Inactivity nudge — {len(nudged)} DM(s) sent",
        description="\n".join(parts),
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text=(
            f"Window: {threshold_days - lead_days}–{threshold_days}d idle · "
            f"cooldown {cooldown_days}d"
        ),
    )
    try:
        await officer_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"inactivity-nudge summary post failed: {exc!r}")


# ── Help-ticket SLA report ──────────────────────────────────────────────────
#
# Daily digest of help tickets older than the SLA window that are still in
# states 'open' or 'taken'. No automatic action — just visibility.

async def _post_help_ticket_sla(bot: Bot) -> None:
    hours = _get_int_config(
        bot.db, "automation_help_ticket_sla_hours", _DEFAULT_HELP_TICKET_SLA_HOURS,
    )
    cutoff_iso = (_now() - datetime.timedelta(hours=hours)).isoformat()
    db = bot.db
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT id, asker_id, status, created_at, claimed_by "
            "  FROM help_tickets "
            " WHERE status IN ('open', 'claimed') "
            "   AND created_at < ? "
            " ORDER BY created_at ASC LIMIT 50",
            (cutoff_iso,),
        )
        rows = [dict(r) for r in db.cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001
        error_log(f"_post_help_ticket_sla query failed: {exc!r}")
        return
    if not rows:
        return
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        info_log(f"help-ticket SLA: {len(rows)} tickets > {hours}h; no officer channel.")
        return
    lines = []
    for r in rows[:25]:
        try:
            opened = datetime.datetime.fromisoformat(
                str(r["created_at"]).replace("Z", "+00:00")
            )
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=datetime.timezone.utc)
            age_h = int((_now() - opened).total_seconds() // 3600)
        except Exception:  # noqa: BLE001
            age_h = -1
        owner = (
            f"<@{r['claimed_by']}>" if r.get("claimed_by")
            else "_unassigned_"
        )
        lines.append(
            f"• #{r['id']} — <@{r['asker_id']}> · {r['status']} · "
            f"{age_h}h old · {owner}"
        )
    if len(rows) > 25:
        lines.append(f"…and {len(rows) - 25} more.")
    embed = discord.Embed(
        title=f"⏰  Help-ticket SLA — {len(rows)} stale (> {hours}h)",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Use /helpticket take or /helpticket solve to clear.")
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"help-ticket SLA post failed: {exc!r}")


# ── Streak-broken alerts ────────────────────────────────────────────────────
#
# When a member who'd built up a notable activity streak (default ≥ 7 days)
# misses a day, post a friendly note in the activity feed and zero their
# active streak. Their best-streak record is preserved.

async def _post_streak_broken_alerts(bot: Bot) -> None:
    raw_chan = bot.db.get_config("points_announce_channel_id")
    if not raw_chan:
        return
    channel = bot.get_channel(int(raw_chan))
    if not isinstance(channel, discord.TextChannel):
        return
    min_streak = _get_int_config(bot.db, "automation_streak_broken_min", 7)
    today_iso = _now().strftime("%Y-%m-%d")
    rows = bot.db.find_broken_streaks(today_iso, min_streak=min_streak)
    if not rows:
        return
    posted = 0
    for r in rows:
        name = r.get("albion_name") or r.get("username") or "Unknown"
        streak = int(r.get("streak") or 0)
        try:
            await channel.send(
                f"💔 **{name}**'s {streak}-day activity streak ended. "
                f"Welcome back any time!",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            posted += 1
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"streak-broken post failed for {name}: {exc!r}")
        try:
            bot.db.clear_streak(r["discord_id"])
        except Exception as exc:  # noqa: BLE001
            error_log(f"clear_streak failed for {r['discord_id']}: {exc!r}")
    if posted:
        info_log(f"Streak-broken sweep: posted {posted} alert(s).")


# ── Daily digest: guild rollup + per-metric top movers ─────────────────────
#
# Posts a single embed each day summarizing the past 24h of guild activity:
#   * Guild totals (sum of positive deltas per metric)
#   * Active-member count (any positive delta in any tracked metric)
#   * Top 5 movers per metric (kill, PvE, gather, craft, fish)
# Channel: points_announce_channel_id (the activity feed). Silently skips
# if not configured.

_DIGEST_METRICS = (
    ("kill_fame",     "⚔️ PvP",    "kill fame"),
    ("pve_total",     "🐗 PvE",    "PvE fame"),
    ("gather_all",    "⛏️ Gather", "gather fame"),
    ("crafting_fame", "🔨 Craft",  "crafting fame"),
)


def _fmt_compact(n: int) -> str:
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


async def _post_daily_digest(bot: Bot) -> None:
    channel = None
    raw = bot.db.get_config("points_announce_channel_id")
    if raw:
        ch = bot.get_channel(int(raw))
        if isinstance(ch, discord.TextChannel):
            channel = ch
    if channel is None:
        return

    since = (_now() - datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    home_guild = (bot.db.get_config("home_guild_name") or "").strip() or None

    totals: dict[str, int] = {}
    movers_by_metric: dict[str, list] = {}
    active_ids: set[str] = set()
    for metric, _emoji_label, _full in _DIGEST_METRICS:
        try:
            rows = bot.db.fetch_top_movers(metric, since, 5, home_guild=home_guild)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily digest fetch_top_movers({metric}) failed: {exc!r}")
            rows = []
        movers_by_metric[metric] = rows
        # Sum the top-mover deltas for the guild rollup. This undercounts
        # players ranked outside top-5, but the visible leaderboard always
        # carries 80%+ of the volume for any single metric.
        totals[metric] = sum(int(r.get("delta") or 0) for r in rows)
        for r in rows:
            did = r.get("discord_id")
            if did:
                active_ids.add(str(did))

    if not active_ids and not any(totals.values()):
        info_log("daily digest: no activity in window; skipping post.")
        return

    embed = discord.Embed(
        title="📊 Guild Daily Digest",
        description=(
            f"Activity over the past 24h.\n"
            f"**{len(active_ids)}** active member"
            f"{'s' if len(active_ids) != 1 else ''} contributed to top movers."
        ),
        color=discord.Color.blue(),
        timestamp=_now(),
    )

    # Guild totals row
    totals_str = " · ".join(
        f"{label} {_fmt_compact(totals.get(metric, 0))}"
        for metric, label, _ in _DIGEST_METRICS
        if totals.get(metric, 0) > 0
    ) or "_no fame gained_"
    embed.add_field(name="Guild Totals (top-5 sums)", value=totals_str, inline=False)

    # Per-metric top movers
    for metric, label, _full in _DIGEST_METRICS:
        rows = movers_by_metric.get(metric) or []
        if not rows:
            continue
        medals = ("🥇", "🥈", "🥉")
        lines = []
        for i, r in enumerate(rows):
            name = r.get("name") or "Unknown"
            delta = int(r.get("delta") or 0)
            prefix = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{prefix} {name} — {_fmt_compact(delta)}")
        embed.add_field(name=label, value="\n".join(lines), inline=True)

    # Voice top-3 over the same window. Voice rows are bucketed per UTC day,
    # so use yesterday's date as the floor — close enough to "last 24h" for
    # a daily summary without needing a second per-second table.
    try:
        voice_since = (_now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        voice_rows = bot.db.top_voice(voice_since, 3, home_guild=home_guild)
    except Exception as exc:  # noqa: BLE001
        error_log(f"daily digest top_voice failed: {exc!r}")
        voice_rows = []
    if voice_rows:
        def _fmt_dur(s: int) -> str:
            h, rem = divmod(int(s), 3600)
            m, _sec = divmod(rem, 60)
            if h:
                return f"{h}h {m}m"
            return f"{m}m"
        medals = ("🥇", "🥈", "🥉")
        v_lines = []
        for i, r in enumerate(voice_rows):
            v_name = r.get("albion_name") or r.get("username") or "Unknown"
            v_lines.append(f"{medals[i]} {v_name} — {_fmt_dur(int(r.get('seconds') or 0))}")
        embed.add_field(name="🎤 Voice", value="\n".join(v_lines), inline=True)

    embed.set_footer(text="Window: last 24h UTC")
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        info_log(f"Daily digest posted to #{channel.name}.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"daily digest post failed: {exc!r}")


def _digest_query(bot: Bot, sql: str, params: tuple = ()) -> list[dict]:
    try:
        if not bot.db.connection:
            bot.db.connect()
        bot.db.cursor.execute(sql, params)
        return [dict(row) for row in bot.db.cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001
        error_log(f"officer ops digest query failed: {exc!r}")
        return []


def _digest_one(bot: Bot, sql: str, params: tuple = ()) -> dict:
    rows = _digest_query(bot, sql, params)
    return rows[0] if rows else {}


def _digest_int(row: dict, key: str, default: int = 0) -> int:
    try:
        return int(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def _digest_field(lines: list[str], empty: str = "_nothing waiting_") -> str:
    if not lines:
        return empty
    body = "\n".join(lines)
    if len(body) <= 1024:
        return body
    return body[:1000].rstrip() + "\n…"


def _digest_days_old(raw: str | None) -> int | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            value = datetime.datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S")
            value = value.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return max(0, (_now() - value.astimezone(datetime.timezone.utc)).days)


def _digest_delta(current: int, previous: int | None) -> str:
    if previous is None:
        return ""
    delta = int(current) - int(previous)
    sign = "+" if delta >= 0 else "-"
    return f" ({sign}{_fmt_compact(abs(delta))})"


async def _post_officer_ops_digest(bot: Bot) -> None:
    """Post one compact daily officer queue digest.

    This is intentionally summary-level: it points officers toward work that
    is already in the bot rather than creating a new public notification.
    """
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return

    now = _now()
    now_iso = now.isoformat()
    now_sql = now.strftime("%Y-%m-%d %H:%M:%S")
    soon_iso = (now + datetime.timedelta(hours=48)).isoformat()
    week_ago_iso = (now - datetime.timedelta(days=7)).isoformat()
    week_start = (now - datetime.timedelta(days=now.weekday())).date().isoformat()

    funnel = bot.db.fetch_recruitment_funnel() or {}

    pending_join = _digest_query(
        bot,
        """
        SELECT id, discord_id, albion_name, reviewed_at, applied_at
          FROM guild_applications
         WHERE status = 'approved_pending_join'
         ORDER BY datetime(COALESCE(reviewed_at, applied_at)) ASC
         LIMIT 5
        """,
    )
    recruit_counts = _digest_query(
        bot,
        """
        SELECT status, COUNT(*) AS n
          FROM recruits
         WHERE status != 'retained'
         GROUP BY status
         ORDER BY n DESC
        """,
    )
    stale_recruits = _digest_query(
        bot,
        """
        SELECT albion_name, status, updated_at
          FROM recruits
         WHERE status IN ('contacted', 'discord', 'registered', 'first_event')
           AND datetime(updated_at) <= datetime(?, '-3 days')
         ORDER BY datetime(updated_at) ASC
         LIMIT 5
        """,
        (now_sql,),
    )

    sla_hours = _get_int_config(
        bot.db, "automation_help_ticket_sla_hours", _DEFAULT_HELP_TICKET_SLA_HOURS,
    )
    open_tickets = _digest_query(
        bot,
        """
        SELECT id, asker_name, question, created_at, claimed_by_name
          FROM help_tickets
         WHERE status = 'open'
         ORDER BY datetime(created_at) ASC
         LIMIT 5
        """,
    )
    stale_ticket_count = _digest_int(
        _digest_one(
            bot,
            """
            SELECT COUNT(*) AS n
              FROM help_tickets
             WHERE status = 'open'
               AND datetime(created_at) <= datetime(?, ?)
            """,
            (now_sql, f"-{sla_hours} hours"),
        ),
        "n",
    )

    open_bounties = _digest_query(
        bot,
        """
        SELECT id, title, status, deadline, claimed_by
          FROM bounties
         WHERE status IN ('open', 'pending')
         ORDER BY datetime(COALESCE(deadline, posted_at)) ASC
         LIMIT 5
        """,
    )
    shopping = _digest_one(
        bot,
        """
        SELECT COALESCE(SUM(needed), 0) AS needed,
               COALESCE(SUM(fulfilled), 0) AS fulfilled,
               COUNT(*) AS lines,
               SUM(CASE WHEN claimed_by IS NOT NULL THEN 1 ELSE 0 END) AS claimed
          FROM bounty_shopping_items
        """,
    )

    upcoming_lfg = _digest_query(
        bot,
        """
        SELECT e.id, e.title, e.starts_at, COALESCE(e.event_type, 'general') AS event_type,
               COUNT(s.id) AS signups
          FROM lfg_events e
          LEFT JOIN lfg_signups s ON s.event_id = e.id
         WHERE e.status = 'open'
           AND e.starts_at >= ?
           AND e.starts_at <= ?
         GROUP BY e.id
         ORDER BY datetime(e.starts_at) ASC
         LIMIT 8
        """,
        (now_iso, soon_iso),
    )
    unmarked_attendance = _digest_int(
        _digest_one(
            bot,
            """
            SELECT COUNT(*) AS n
              FROM lfg_signups s
              JOIN lfg_events e ON e.id = s.event_id
             WHERE e.status = 'completed'
               AND s.attended IS NULL
            """,
        ),
        "n",
    )
    voice_without_lfg = _digest_query(
        bot,
        """
        SELECT v.discord_id, COALESCE(u.albion_name, u.username, v.discord_id) AS name,
               SUM(v.seconds) AS seconds,
               COALESCE(ls.signups, 0) AS signups
          FROM voice_activity v
          LEFT JOIN user_profiles u ON u.discord_id = v.discord_id
          LEFT JOIN (
                SELECT discord_id, COUNT(*) AS signups
                  FROM lfg_signups
                 GROUP BY discord_id
          ) ls ON ls.discord_id = v.discord_id
         WHERE v.date_utc >= date('now', '-7 days')
         GROUP BY v.discord_id
        HAVING seconds >= 7200 AND signups = 0
         ORDER BY seconds DESC
         LIMIT 5
        """,
    )

    duties = _digest_one(
        bot,
        """
        SELECT (SELECT COUNT(*) FROM duty_definitions) AS definitions,
               (SELECT COUNT(*) FROM duty_completions WHERE completed_at >= ?) AS completions
        """,
        (week_start,),
    )

    treasury_rows = _digest_query(
        bot,
        """
        SELECT date, balance, recorded_by, note, recorded_at
          FROM guild_treasury_history
         ORDER BY date DESC
         LIMIT 2
        """,
    )
    latest_treasury = treasury_rows[0] if treasury_rows else None
    previous_balance = (
        int(treasury_rows[1]["balance"]) if len(treasury_rows) > 1 else None
    )
    silver = _digest_one(
        bot,
        """
        SELECT COALESCE(SUM(CASE WHEN silver_balance > 0 THEN silver_balance ELSE 0 END), 0) AS owed,
               COALESCE(SUM(CASE WHEN silver_balance < 0 THEN -silver_balance ELSE 0 END), 0) AS owed_back,
               COUNT(CASE WHEN silver_balance != 0 THEN 1 END) AS nonzero
          FROM user_profiles
        """,
    )

    home = (bot.db.get_config("home_guild_name") or "").strip()
    no_lifecycle = _digest_int(
        _digest_one(
            bot,
            "SELECT COUNT(*) AS n FROM user_profiles WHERE lifecycle_role IS NULL",
        ),
        "n",
    )
    missing_tag = 0
    if home:
        missing_tag = _digest_int(
            _digest_one(
                bot,
                """
                SELECT COUNT(*) AS n
                  FROM user_profiles
                 WHERE LOWER(COALESCE(guild_name, '')) = LOWER(?)
                   AND TRIM(COALESCE(alliance_tag, '')) = ''
                """,
                (home,),
            ),
            "n",
        )
    survey_count = _digest_int(
        _digest_one(bot, "SELECT COUNT(*) AS n FROM member_survey_responses"),
        "n",
    )

    embed = discord.Embed(
        title="🧭 Officer Daily Ops Digest",
        description=(
            f"Quiet summary for <t:{int(now.timestamp())}:D>. No pings. "
            "Use it to clear the small stuff before it turns into officer homework."
        ),
        color=discord.Color.dark_teal(),
        timestamp=now,
    )

    funnel_lines = [
        f"Discord **{funnel.get('discord_members', 0)}** · "
        f"Registered **{funnel.get('registered', 0)}** · "
        f"Home guild **{funnel.get('in_home_guild', 0)}** · "
        f"Active 30d **{funnel.get('active_30d', 0)}**",
    ]
    if pending_join:
        funnel_lines.append(f"**Approved, not joined:** {len(pending_join)} shown")
        for row in pending_join[:4]:
            age = _digest_days_old(row.get("reviewed_at") or row.get("applied_at"))
            age_txt = f"{age}d" if age is not None else "?"
            funnel_lines.append(
                f"• <@{row['discord_id']}> `{row.get('albion_name') or '?'}` - {age_txt}"
            )
    if recruit_counts:
        counts = ", ".join(f"{r['status']} {r['n']}" for r in recruit_counts)
        funnel_lines.append(f"Funnel: {counts}")
    if stale_recruits:
        names = ", ".join(str(r.get("albion_name") or "?") for r in stale_recruits[:4])
        funnel_lines.append(f"Stale 3d+: {names}")
    embed.add_field(
        name="Recruiting",
        value=_digest_field(funnel_lines),
        inline=False,
    )

    event_lines: list[str] = []
    if upcoming_lfg:
        event_lines.append("**Next 48h LFG:**")
        for row in upcoming_lfg[:5]:
            starts = row.get("starts_at")
            ts = ""
            if starts:
                try:
                    stamp = datetime.datetime.fromisoformat(str(starts)).timestamp()
                    ts = f"<t:{int(stamp)}:R>"
                except ValueError:
                    ts = str(starts)[:16]
            warn = " ⚠️" if int(row.get("signups") or 0) == 0 else ""
            event_lines.append(
                f"• #{row['id']} `{row['title'][:36]}` - {row['signups']} signed {ts}{warn}"
            )
    else:
        event_lines.append("No open LFG in the next 48h.")
    if unmarked_attendance:
        event_lines.append(f"Attendance still unmarked: **{unmarked_attendance}** signup(s).")
    if voice_without_lfg:
        top = ", ".join(
            f"{r['name']} {_fmt_compact(int(r.get('seconds') or 0) // 60)}m"
            for r in voice_without_lfg[:3]
        )
        event_lines.append(f"Voice-active with no LFG signups: {top}")
    embed.add_field(
        name="LFG / Attendance",
        value=_digest_field(event_lines),
        inline=False,
    )

    work_lines: list[str] = []
    if open_tickets:
        work_lines.append(
            f"Open help tickets: **{len(open_tickets)}**"
            + (f" · stale SLA **{stale_ticket_count}**" if stale_ticket_count else "")
        )
        for row in open_tickets[:3]:
            age = _digest_days_old(row.get("created_at"))
            work_lines.append(
                f"• ticket #{row['id']} `{str(row.get('question') or '')[:42]}`"
                + (f" - {age}d" if age is not None else "")
            )
    else:
        work_lines.append("No open help tickets.")
    if open_bounties:
        work_lines.append(f"Open/pending bounties: **{len(open_bounties)}**")
        for row in open_bounties[:3]:
            status = row.get("status") or "?"
            work_lines.append(f"• #{row['id']} `{str(row.get('title') or '')[:40]}` - {status}")
    if shopping:
        needed = _digest_int(shopping, "needed")
        fulfilled = _digest_int(shopping, "fulfilled")
        if needed:
            work_lines.append(
                f"Shopping bounties: **{fulfilled:,}/{needed:,}** fulfilled "
                f"across {_digest_int(shopping, 'lines')} line(s)."
            )
    defs = _digest_int(duties, "definitions")
    comps = _digest_int(duties, "completions")
    if defs:
        work_lines.append(f"Staff duties: **{defs}** defined, **{comps}** completion(s) this week.")
    embed.add_field(
        name="Officer Work Queue",
        value=_digest_field(work_lines),
        inline=False,
    )

    econ_lines: list[str] = []
    if latest_treasury:
        bal = int(latest_treasury.get("balance") or 0)
        econ_lines.append(
            f"Treasury: **{bal:,}** on `{latest_treasury.get('date')}`"
            f"{_digest_delta(bal, previous_balance)}"
        )
        if latest_treasury.get("date") != now.strftime("%Y-%m-%d"):
            econ_lines.append("Today has no treasury snapshot yet.")
    else:
        econ_lines.append("No treasury snapshot recorded yet.")
    owed = _digest_int(silver, "owed")
    owed_back = _digest_int(silver, "owed_back")
    nonzero = _digest_int(silver, "nonzero")
    econ_lines.append(
        f"Silver ledger: guild owes **{owed:,}**, members owe **{owed_back:,}** "
        f"({nonzero} non-zero balance)."
    )
    embed.add_field(
        name="Economy",
        value=_digest_field(econ_lines),
        inline=False,
    )

    hygiene_lines = [
        f"Profiles missing lifecycle role: **{no_lifecycle}**",
        f"Home-guild profiles missing alliance tag: **{missing_tag}**",
        f"Member survey responses: **{survey_count}**",
    ]
    embed.add_field(
        name="Data Hygiene",
        value=_digest_field(hygiene_lines),
        inline=False,
    )
    embed.set_footer(text="Officer digest · no pings · summary only")

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        info_log(f"Officer ops digest posted to #{channel.name}.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"officer ops digest post failed: {exc!r}")


# ── Nightly SQLite backup with rotation ─────────────────────────────────────
#
# Writes a hot backup using sqlite3 .backup() (WAL-safe) to data/backups/.
# Keeps the most recent ``automation_backup_keep_days`` files (default 7)
# and prunes older ones. SD cards die — this is cheap insurance.

async def _run_nightly_backup(bot: Bot) -> None:
    import os
    import pathlib
    import sqlite3

    keep = _get_int_config(bot.db, "automation_backup_keep_days", 7)
    backup_dir = pathlib.Path("data/backups")
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        error_log(f"nightly backup: mkdir failed: {exc!r}")
        return

    stamp = _now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"db-{stamp}-auto.db"
    db_path = getattr(bot.db, "database_path", None)
    if not db_path:
        error_log("nightly backup: DB path is unavailable.")
        return

    def _do_backup() -> tuple[bool, str]:
        try:
            with sqlite3.connect(str(db_path), timeout=30) as src:
                src.execute("PRAGMA busy_timeout=5000")
                with sqlite3.connect(str(dest)) as bck:
                    src.backup(bck)
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)

    ok, err = await asyncio.to_thread(_do_backup)
    if not ok:
        error_log(f"nightly backup failed: {err}")
        return
    size_mb = dest.stat().st_size / (1024 * 1024)
    info_log(f"Nightly backup written: {dest.name} ({size_mb:.2f} MB).")

    # Prune: keep the newest ``keep`` auto backups.
    autos = sorted(
        backup_dir.glob("db-*-auto.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    pruned = 0
    for old in autos[keep:]:
        try:
            os.remove(old)
            pruned += 1
        except OSError as exc:
            error_log(f"nightly backup: prune {old.name} failed: {exc!r}")
    if pruned:
        info_log(f"Nightly backup: pruned {pruned} old file(s); keeping {keep}.")


async def _check_policy_drift(bot: Bot) -> None:
    """For each saved policy snapshot, fetch the current pinned content and
    compare hashes. Alert on drift. Officers re-snapshot via /automation
    snapshot-policy after legitimate edits."""
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return
    if _is_snoozed(bot, "policy"):
        info_log("Policy drift snoozed; skipping post.")
        return
    drifted = await _detect_policy_drift(bot)
    if not drifted:
        return
    lines = [
        f"• <#{s['channel_id']}> — _{reason}_"
        for s, reason in drifted
    ]
    embed = discord.Embed(
        title="📜  Policy drift detected",
        description="\n".join(lines) + (
            "\n\nIf the change was intentional, click **Re-snapshot all** "
            "below or run `/automation snapshot-policy`."
        ),
        color=discord.Color.orange(),
    )
    try:
        await channel.send(
            embed=embed,
            view=PolicyDriftView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        info_log(f"Policy drift alert: {len(drifted)} channel(s).")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"policy drift post failed: {exc!r}")


async def _detect_policy_drift(
    bot: Bot,
) -> list[tuple[dict, str]]:
    """Return a list of (snapshot_row, reason) tuples for every drifted
    policy channel. Empty list = no drift."""
    snapshots = bot.db.fetch_all_policy_snapshots()
    if not snapshots:
        return []
    drifted: list[tuple[dict, str]] = []
    for snap in snapshots:
        ch = bot.get_channel(int(snap["channel_id"]))
        if not isinstance(ch, discord.TextChannel):
            continue
        try:
            msg = await ch.fetch_message(int(snap["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            drifted.append((dict(snap), "message gone"))
            continue
        current_hash = hashlib.sha256(msg.content.encode("utf-8")).hexdigest()
        if current_hash != snap["content_hash"]:
            drifted.append((dict(snap), "content changed"))
    return drifted


async def _update_channel_topic(bot: Bot) -> None:
    channel = _channel(bot, "automation_topic_channel_id")
    if channel is None:
        return
    funnel = bot.db.fetch_recruitment_funnel()
    since_iso = (_now() - datetime.timedelta(days=7)).isoformat()
    movers = bot.db.fetch_top_movers("kill_fame", since_iso, 1)
    top = "—"
    if movers:
        m = movers[0]
        top = m.get("name") or m["discord_id"]
    if not bot.db.connection:
        bot.db.connect()
    bot.db.cursor.execute(
        "SELECT COUNT(*) AS n FROM lfg_events "
        "WHERE starts_at >= ? AND status != 'cancelled'",
        (since_iso,),
    )
    events_run = int((bot.db.cursor.fetchone() or {"n": 0})["n"] or 0)
    topic = (
        f"📊 {funnel.get('in_home_guild', 0)} in guild · "
        f"{funnel.get('active_30d', 0)} active(30d) · "
        f"{events_run} events(7d) · "
        f"⭐ Top: {top}"
    )
    if len(topic) > 1024:
        topic = topic[:1020] + "…"
    try:
        await channel.edit(topic=topic, reason="Daily automation: vital signs.")
        info_log(f"Updated channel topic: {channel.name} → {topic[:80]}…")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"topic update failed: {exc!r}")


async def _post_recruitment_funnel(bot: Bot) -> None:
    """Daily mini-funnel summary for officers."""
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return
    f = bot.db.fetch_recruitment_funnel()
    if not f:
        return
    embed = info_embed(
        "📈  Recruitment funnel",
        (
            f"Discord members: **{f.get('discord_members', 0)}**\n"
            f"Registered:      **{f.get('registered', 0)}**\n"
            f"Verified:        **{f.get('verified', 0)}**\n"
            f"In home guild:   **{f.get('in_home_guild', 0)}**\n"
            f"Active (30d):    **{f.get('active_30d', 0)}**"
        ),
    )
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"funnel summary post failed: {exc!r}")


# ── Discord UI views (extracted to cogs/_automation_views.py) ──────────────
from cogs._automation_views import (
    TreasuryPromptView,
    InactivitySweepView,
    PolicyDriftView,
    RegistrationCleanupView,
    UnderfillAlertView,
)


async def _post_treasury_prompt(bot: Bot) -> None:
    """Once per UTC day: nudge officers in the officer-tasks channel to log
    the in-game guild bank balance. Idempotent — if a snapshot for today
    already exists, the prompt is skipped.
    """
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return
    today = _now().strftime("%Y-%m-%d")
    latest = bot.db.fetch_latest_guild_treasury()
    if latest and latest.get("date") == today:
        info_log(f"Treasury already recorded for {today}; skipping prompt.")
        return
    last_str = "_no snapshots yet_"
    if latest:
        last_str = (
            f"Last entry: **{int(latest['balance']):,}** silver on `{latest['date']}` "
            f"(<@{latest.get('recorded_by') or '?'}>)"
        )
    embed = info_embed(
        "💰  Daily treasury check-in",
        (
            f"Officers: please record the in-game guild bank balance for **{today}**.\n\n"
            f"{last_str}\n\n"
            "Click **Record today's balance** below, or run `/audit treasury-record`."
        ),
    )
    try:
        await channel.send(embed=embed, view=TreasuryPromptView())
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"treasury prompt post failed: {exc!r}")


async def _post_unpaid_silver_reminder(bot: Bot, *, force: bool = False) -> None:
    """Once per UTC day: nudge officers about members the guild owes silver
    to whose oldest credit is at least ``automation_unpaid_silver_min_days``
    old (default 7). Posts to the officer-tasks channel.

    Set ``automation_unpaid_silver_enabled`` to 0 to disable. Setting
    ``automation_unpaid_silver_min_days`` to 0 surfaces every outstanding
    debt regardless of age (useful when first onboarding the feature).
    Pass ``force=True`` to bypass the once-per-day guard (used by manual
    ``/automation run-now`` invocations).
    """
    enabled = _get_int_config(bot.db, "automation_unpaid_silver_enabled", 1)
    if not enabled and not force:
        return
    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return
    min_days = _get_int_config(bot.db, "automation_unpaid_silver_min_days", 7)
    min_amount = _get_int_config(bot.db, "automation_unpaid_silver_min_amount", 1)
    home_guild = (bot.db.get_config("home_guild_name") or "").strip() or None

    rows = bot.db.fetch_unpaid_silver_aged(
        min_age_days=min_days,
        min_amount=min_amount,
        home_guild=home_guild,
    )
    if not rows:
        info_log(
            f"unpaid silver reminder: nothing aged ≥{min_days}d "
            f"(threshold {min_amount:,}); skipping post."
        )
        return

    # Skip if we already posted today — keeps the channel quiet on catch-ups.
    today = _now().strftime("%Y-%m-%d")
    last_posted = bot.db.get_config("automation_unpaid_silver_last_run") or ""
    if last_posted == today and not force:
        info_log("unpaid silver reminder: already posted today; skipping.")
        return

    total_owed = sum(int(r["balance"] or 0) for r in rows)
    # Top 15 keeps the embed under Discord's 4096-char description limit even
    # in a worst-case "everyone is owed silver" scenario.
    lines = []
    for r in rows[:15]:
        bal = int(r["balance"] or 0)
        d = int(r.get("days_waiting") or 0)
        age_str = "today" if d == 0 else (f"{d}d ago" if d < 30 else f"**{d}d ago**")
        lines.append(
            f"• <@{r['discord_id']}>  ·  **{bal:,}** silver  ·  oldest credit {age_str}"
        )
    overflow = max(0, len(rows) - 15)
    if overflow:
        lines.append(f"…and **{overflow}** more.")

    age_label = (
        f"unpaid for **{min_days}+ days**" if min_days > 0 else "with any unpaid balance"
    )
    embed = info_embed(
        "💸  Unpaid silver reminder",
        (
            f"**{len(rows)}** member(s) {age_label}, totalling "
            f"**{total_owed:,}** silver owed.\n\n" + "\n".join(lines) +
            "\n\nUse `/audit ledger` to inspect a specific member, or settle "
            "in-game and run `/audit settle` to clear the balance."
        ),
    )
    try:
        await channel.send(embed=embed)
        bot.db.set_config("automation_unpaid_silver_last_run", today)
        info_log(
            f"unpaid silver reminder: posted {len(rows)} member(s), "
            f"total {total_owed:,} silver."
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"unpaid silver reminder post failed: {exc!r}")


# ── Per-5min routines ───────────────────────────────────────────────────────

def _parse_event_dt(value: object) -> datetime.datetime | None:
    """Parse an ISO-8601 string (with or without trailing 'Z') into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _legacy_event_reminders_enabled(bot: Bot) -> bool:
    raw = bot.db.get_config("automation_event_reminders_enabled")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _local_time_line(when_utc: datetime.datetime, tz_name: str | None) -> str | None:
    """Render '🕒 Your local time: Thu, May 14 · 14:30 CDT (America/Chicago)' or None."""
    if not tz_name:
        return None
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return None
    local = when_utc.astimezone(tz)
    return (
        f"🕒 **Your local time:** {local.strftime('%a, %b %d · %H:%M %Z')} "
        f"(`{tz_name}`)"
    )


async def _send_event_reminders(bot: Bot) -> None:
    if not _legacy_event_reminders_enabled(bot):
        info_log("automation event reminders skipped; LFG cog owns reminder DMs.")
        return

    minutes = _get_int_config(
        bot.db, "automation_event_reminder_minutes", _DEFAULT_REMINDER_MIN,
    )
    now = _now()
    upper_iso = (now + datetime.timedelta(minutes=minutes)).isoformat()
    lower_iso = (now + datetime.timedelta(minutes=minutes - 5)).isoformat()
    events = bot.db.fetch_upcoming_events(lower_iso, upper_iso)
    for ev in events:
        signups = bot.db.fetch_lfg_signups(int(ev["id"]))
        when_dt = _parse_event_dt(ev.get("starts_at"))
        if when_dt is None:
            continue
        when_unix = int(when_dt.timestamp())
        raw_slot_label = ev.get("slot_label") or ""
        slot_label = display_slot_label(raw_slot_label) if raw_slot_label else ""
        event_type = ev.get("event_type") or ""
        description = (ev.get("description") or "").strip()

        # Header chips: slot label + event type (only if present)
        chip_bits = [b for b in (slot_label, event_type) if b]
        chips = " · ".join(chip_bits)

        for s in signups:
            did = str(s["discord_id"])
            if bot.db.has_reminder_been_sent(int(ev["id"]), did):
                continue
            try:
                # Lookup profile for personal timezone (set via /profile timezone).
                profile = bot.db.fetch_user_profile(did) or {}
                tz_name = profile.get("timezone")
                local_line = _local_time_line(when_dt, tz_name)

                lines: list[str] = []
                if chips:
                    lines.append(f"*{chips}*")
                lines.append(
                    f"⏰ Starting **<t:{when_unix}:R>** "
                    f"(<t:{when_unix}:F>)"
                )
                if local_line:
                    lines.append(local_line)
                else:
                    # No timezone set — give a friendly nudge once.
                    lines.append(
                        "_Tip: run `/profile timezone` so future reminders "
                        "show your local time._"
                    )
                if description:
                    lines.append("")
                    lines.append(description[:1500])

                user = await bot.fetch_user(int(did))
                embed = info_embed(
                    f"⏰ Reminder — {ev.get('title')}",
                    "\n".join(lines),
                )
                await user.send(embed=embed)
                bot.db.mark_reminder_sent(int(ev["id"]), did)
            except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
                error_log(f"event reminder DM to {did} failed: {exc!r}")
                bot.db.mark_reminder_sent(int(ev["id"]), did)  # don't retry forever


async def _send_underfill_alerts(bot: Bot) -> None:
    """Post an officer-channel alert when an upcoming event has a comp
    attached but signups aren't filling the slots.

    Triggers once per event when:
      • automation_underfill_enabled is on (default on)
      • event has a comp_id (otherwise there's no "filled" count to measure)
      • starts_at is within the next `lead_minutes` minutes
      • filled / total < threshold_pct / 100
    """
    if _get_int_config(bot.db, "automation_underfill_enabled", 1) <= 0:
        return

    lead_minutes = _get_int_config(
        bot.db, "automation_underfill_lead_minutes", _DEFAULT_UNDERFILL_LEAD_MIN,
    )
    threshold_pct = _get_int_config(
        bot.db, "automation_underfill_threshold_pct", _DEFAULT_UNDERFILL_THRESHOLD,
    )
    if lead_minutes <= 0 or threshold_pct <= 0:
        return

    now = _now()
    upper_iso = (now + datetime.timedelta(minutes=lead_minutes)).isoformat()
    lower_iso = now.isoformat()
    events = bot.db.fetch_upcoming_events(lower_iso, upper_iso)
    if not events:
        return

    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return  # nothing to alert into

    for ev in events:
        event_id = int(ev["id"])
        if not ev.get("comp_id"):
            continue  # no comp → no slot fill metric
        if bot.db.has_underfill_alert_been_sent(event_id):
            continue

        try:
            grid = bot.db.fetch_lfg_slot_grid(event_id) or []
        except Exception as exc:  # noqa: BLE001
            error_log(f"underfill alert: slot-grid fetch failed for #{event_id}: {exc!r}")
            continue
        total = len(grid)
        if total <= 0:
            continue
        filled = sum(1 for r in grid if r.get("claimed_by"))
        pct = (filled * 100) // total
        if pct >= threshold_pct:
            continue

        when_dt = _parse_event_dt(ev.get("starts_at"))
        when_unix = int(when_dt.timestamp()) if when_dt else None
        open_slots = total - filled

        # Group open slots by role for an at-a-glance breakdown.
        from collections import Counter
        open_by_role: Counter[str] = Counter()
        for r in grid:
            if not r.get("claimed_by"):
                open_by_role[r.get("role") or "Other"] += 1
        role_lines = "\n".join(
            f"• **{role}** — {count} open"
            for role, count in open_by_role.most_common()
        ) or "_All slots accounted for somehow._"

        title = f"⚠️ Comp under-filled — {ev.get('title') or 'event #' + str(event_id)}"
        when_line = (
            f"Starts **<t:{when_unix}:R>** (<t:{when_unix}:F>)"
            if when_unix is not None else "Starts soon."
        )
        description = (
            f"{when_line}\n"
            f"Slots claimed: **{filled} / {total}** ({pct}%, "
            f"threshold {threshold_pct}%).\n\n"
            f"**Open by role:**\n{role_lines}\n\n"
            "Would you like to **cancel the event**, or keep it running and "
            "rally more signups?"
        )

        # Ping event creator inline so they can rally signups; ping officer
        # role if configured.
        ping_bits: list[str] = []
        creator_id = ev.get("creator_id")
        if creator_id:
            ping_bits.append(f"<@{creator_id}>")
        officer_role_id = bot.db.get_config("automation_officer_role_id")
        if officer_role_id:
            ping_bits.append(f"<@&{officer_role_id}>")
        content = " ".join(ping_bits) if ping_bits else None

        embed = info_embed(title, description)
        view = UnderfillAlertView(event_id, creator_id)
        try:
            await channel.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
            bot.db.mark_underfill_alert_sent(event_id, filled, total)
            info_log(
                f"Under-fill alert posted for event #{event_id} "
                f"({filled}/{total}, {pct}%)."
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(
                f"under-fill alert post failed for #{event_id} in "
                f"{channel}: {exc!r}"
            )


async def _snapshot_voice_attendance(bot: Bot) -> None:
    """Snapshot event voice presence for active LFGs.

    Per-event temporary VCs take priority. The older configured global voice
    channel remains as a fallback for events that do not have their own VC.
    The DB keeps returning events past their scheduled end while their
    temporary event VC is still alive, because Albion content often runs long.
    """
    active_events = bot.db.fetch_active_event_window()
    if not active_events:
        return

    fallback_channel: discord.VoiceChannel | None = None
    voice_id = bot.db.get_config("automation_voice_channel_id")
    if voice_id:
        try:
            ch = bot.get_channel(int(voice_id)) or await bot.fetch_channel(int(voice_id))
            if isinstance(ch, discord.VoiceChannel):
                fallback_channel = ch
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            fallback_channel = None

    for ev in active_events:
        voice_channel = None
        event_voice_id = str(ev.get("voice_channel_id") or "").strip()
        if event_voice_id:
            try:
                ch = bot.get_channel(int(event_voice_id)) or await bot.fetch_channel(int(event_voice_id))
                if isinstance(ch, discord.VoiceChannel):
                    voice_channel = ch
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                voice_channel = None
        if voice_channel is None:
            voice_channel = fallback_channel
        if voice_channel is None:
            continue
        members = [m for m in voice_channel.members if not m.bot]
        if not members:
            continue
        discord_ids = [str(m.id) for m in members]
        bot.db.record_voice_snapshot(int(ev["id"]), discord_ids)


async def _reconcile_finished_events(bot: Bot) -> None:
    """For ended events, mark members attended when voice presence confirms it.

    Members who are not seen in voice are left unmarked. The guild no longer
    tracks or penalizes missed LFG attendance.
    """
    threshold_pct = _get_int_config(
        bot.db, "automation_voice_attendance_min_pct", _DEFAULT_VOICE_PCT,
    )
    fallback_grace = _get_int_config(
        bot.db,
        "automation_event_reconcile_grace_minutes",
        _DEFAULT_EVENT_RECONCILE_GRACE_MIN,
    )
    events = bot.db.fetch_events_needing_reconciliation(fallback_grace)
    if not events:
        return

    for ev in events:
        event_id = int(ev["id"])
        snaps = bot.db.fetch_voice_snapshot_summary(event_id)
        signups = bot.db.fetch_lfg_signups(event_id)
        if not snaps and not signups:
            bot.db.mark_event_reconciled(event_id)
            continue
        max_count = max(snaps.values()) if snaps else 0
        threshold = max(1, max_count * threshold_pct // 100)

        attended_count = 0
        for s in signups:
            did = str(s["discord_id"])
            if int(s.get("attended") or 0) == 1:
                attended_count += 1
                continue
            seen = snaps.get(did, 0)
            attended = seen >= threshold
            if not attended:
                continue
            bot.db.set_signup_attendance(event_id, did, True)
            # Points hooks.
            try:
                from cogs.points import get_point_setting
                pts = get_point_setting(bot.db, "points_event_attended")
                if pts:
                    bot.db.add_points(did, pts)
                    info_log(
                        f"Awarded {pts} event point(s) to {did} "
                        f"(event #{event_id}, attended)."
                    )
            except Exception as exc:  # noqa: BLE001
                error_log(f"event-attendance points hook failed: {exc!r}")
            attended_count += 1

        bot.db.mark_event_reconciled(event_id)
        not_marked_count = max(0, len(signups) - attended_count)
        info_log(
            f"Reconciled event #{event_id}: "
            f"{attended_count}/{len(signups)} attended "
            f"(snapshot threshold {threshold})."
        )

        # Post the post-event analytics report to officer channel.
        channel = _channel(bot, "automation_officer_channel_id")
        if channel is not None and signups:
            try:
                ended_at = None
                try:
                    ended_at = datetime.datetime.fromisoformat(
                        str(ev.get("ends_at") or "").replace("Z", "+00:00")
                    )
                    if ended_at.tzinfo is None:
                        ended_at = ended_at.replace(tzinfo=datetime.timezone.utc)
                except (TypeError, ValueError):
                    ended_at = None
                recent = (
                    ended_at is None
                    or datetime.datetime.now(datetime.timezone.utc) - ended_at
                    <= datetime.timedelta(hours=24)
                )
                graph_files: list[discord.File] = []
                extra_embeds: list[discord.Embed] = []
                embed = await build_event_report_embed(
                    bot,
                    ev,
                    threshold_pct=threshold_pct,
                    fetch_killboard=recent,
                    create_regear_tasks=recent,
                    include_graph=True,
                    graph_files=graph_files,
                    extra_embeds=extra_embeds,
                )
                report_embeds = [embed, *extra_embeds]
                for idx, embed_batch in enumerate(batch_embeds_for_send(report_embeds)):
                    kwargs: dict = {
                        "embeds": embed_batch,
                        "allowed_mentions": discord.AllowedMentions.none(),
                    }
                    if idx == 0:
                        kwargs["view"] = build_event_report_view(event_id)
                        if graph_files:
                            kwargs["file"] = graph_files[0]
                    await channel.send(**kwargs)
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"event report post failed for #{event_id}: {exc!r}")
            except Exception as exc:  # noqa: BLE001
                error_log(f"event report post failed for #{event_id}: {exc!r}")
                try:
                    description = (
                        f"**{ev.get('title')}**\n"
                        f"Attended: **{attended_count}/{len(signups)}** "
                        f"(>={threshold_pct}% voice presence)."
                    )
                    if not_marked_count:
                        description += (
                            f"\nNot marked attended: **{not_marked_count}**. "
                            "No penalty is applied."
                        )
                    await channel.send(
                        embed=info_embed(f"Event #{event_id} reconciled", description),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass


async def _retry_pending_event_reports(bot: Bot) -> None:
    """Retry event reports that were missing killboard or pricing data."""
    now = datetime.datetime.now(datetime.timezone.utc)
    pending_rows = bot.db.fetch_due_event_report_pending_data(
        _utc_iso(now),
        limit=_EVENT_REPORT_PENDING_RETRY_LIMIT,
    )
    if not pending_rows:
        return

    channel = _channel(bot, "automation_officer_channel_id")
    if channel is None:
        return

    threshold_pct = _get_int_config(
        bot.db, "automation_voice_attendance_min_pct", _DEFAULT_VOICE_PCT,
    )
    for row in pending_rows:
        event_id = int(row.get("event_id") or 0)
        if event_id <= 0:
            continue
        ev = bot.db.fetch_lfg_event(event_id)
        if not ev or str(ev.get("status") or "").lower() == "cancelled":
            bot.db.clear_event_report_pending_data(event_id)
            continue

        try:
            graph_files: list[discord.File] = []
            extra_embeds: list[discord.Embed] = []
            embed = await build_event_report_embed(
                bot,
                ev,
                threshold_pct=threshold_pct,
                fetch_killboard=True,
                create_regear_tasks=True,
                include_graph=True,
                graph_files=graph_files,
                extra_embeds=extra_embeds,
            )
            if bot.db.fetch_event_report_pending_data(event_id):
                continue

            update = info_embed(
                f"Event #{event_id} data updated",
                (
                    "The delayed killboard/pricing data is now available. "
                    "This refreshed scorecard includes the newest gear-loss and regear details."
                ),
            )
            report_embeds = [update, embed, *extra_embeds]
            for idx, embed_batch in enumerate(batch_embeds_for_send(report_embeds)):
                kwargs: dict = {
                    "embeds": embed_batch,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if idx == 0:
                    kwargs["view"] = build_event_report_view(event_id)
                    if graph_files:
                        kwargs["file"] = graph_files[0]
                await channel.send(**kwargs)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"event report pending-data post failed for #{event_id}: {exc!r}")
            attempts = int(row.get("attempts") or 0)
            delay = min(60, 10 * (attempts + 1))
            bot.db.upsert_event_report_pending_data(
                event_id,
                reason=f"Discord post failed: {type(exc).__name__}",
                next_retry_at=_utc_iso(now + datetime.timedelta(minutes=delay)),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"event report pending-data retry failed for #{event_id}: {exc!r}")
            attempts = int(row.get("attempts") or 0)
            delay = min(60, 10 * (attempts + 1))
            bot.db.upsert_event_report_pending_data(
                event_id,
                reason=f"Retry failed: {type(exc).__name__}",
                next_retry_at=_utc_iso(now + datetime.timedelta(minutes=delay)),
            )


# ── Cog ─────────────────────────────────────────────────────────────────────

class Automation(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        self.daily_tick.start()
        self.minute_tick.start()
        # Re-register the persistent treasury prompt view so its button keeps
        # working across bot restarts.
        self.bot.add_view(TreasuryPromptView())
        # Officer-action views on daily alerts (inactivity, policy drift).
        self.bot.add_view(InactivitySweepView())
        self.bot.add_view(PolicyDriftView())
        self.bot.add_view(RegistrationCleanupView())
        register_persistent_event_report_views(self.bot)
        # Add slash commands group.
        self.bot.tree.add_command(AutomationGroup(self.bot))

    def cog_unload(self) -> None:
        self.daily_tick.cancel()
        self.minute_tick.cancel()
        # Reload-safety: drop the manually-added /automation group.
        try:
            self.bot.tree.remove_command("automation")
        except Exception:  # noqa: BLE001
            pass

    @tasks.loop(minutes=5)
    async def minute_tick(self) -> None:
        try:
            await _send_event_reminders(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"minute_tick reminders failed: {exc!r}")
        try:
            await _send_underfill_alerts(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"minute_tick underfill alerts failed: {exc!r}")
        try:
            await _snapshot_voice_attendance(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"minute_tick voice snapshot failed: {exc!r}")
        try:
            await _reconcile_finished_events(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"minute_tick reconcile failed: {exc!r}")
        try:
            await _retry_pending_event_reports(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"minute_tick pending event report retry failed: {exc!r}")

    @minute_tick.before_loop
    async def _before_minute(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(time=datetime.time(hour=0, minute=5, tzinfo=datetime.timezone.utc))
    async def daily_tick(self) -> None:
        await self._run_daily_routines()

    async def _run_daily_routines(self, *, reason: str = "scheduled") -> None:
        """Run the full daily automation chain. Called by ``daily_tick`` at
        00:05 UTC and also by the catch-up path on startup if the bot was
        offline at the scheduled time. Records ``automation_last_daily_run``
        (ISO date) so subsequent restarts on the same UTC day skip catch-up."""
        info_log(f"daily_tick: starting daily routines ({reason}).")
        try:
            await _post_anniversaries(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily anniversaries failed: {exc!r}")
        try:
            await _post_inactivity_sweep(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily inactivity sweep failed: {exc!r}")
        try:
            await _run_unverified_nudges(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily unverified-nudge sweep failed: {exc!r}")
        try:
            await _run_unverified_kicks(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily unverified-kick sweep failed: {exc!r}")
        try:
            await _run_auto_alumni(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily auto-alumni sweep failed: {exc!r}")
        try:
            await _run_inactivity_nudge(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily inactivity nudge failed: {exc!r}")
        try:
            await _post_help_ticket_sla(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily help-ticket SLA failed: {exc!r}")
        try:
            await _post_streak_broken_alerts(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily streak-broken sweep failed: {exc!r}")
        try:
            await _post_officer_ops_digest(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily officer ops digest failed: {exc!r}")
        try:
            await _post_daily_digest(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily digest failed: {exc!r}")
        try:
            await _run_nightly_backup(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily nightly backup failed: {exc!r}")
        try:
            await _check_policy_drift(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily policy drift failed: {exc!r}")
        try:
            await _update_channel_topic(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily channel topic failed: {exc!r}")
        try:
            await _post_treasury_prompt(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily treasury prompt failed: {exc!r}")
        try:
            await _post_unpaid_silver_reminder(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily unpaid silver reminder failed: {exc!r}")
        try:
            archive_completed_events(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily event archival failed: {exc!r}")
        try:
            cleanup_orphan_guilds(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily orphan-guild cleanup failed: {exc!r}")
        try:
            self.bot.db.set_config(
                "automation_last_daily_run",
                _now().strftime("%Y-%m-%d"),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_tick: failed to record last-run date: {exc!r}")
        info_log(f"daily_tick: finished daily routines ({reason}).")

    @daily_tick.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()
        # Catch-up: if the bot was offline at 00:05 UTC, discord.py's
        # tasks.loop(time=…) silently skips that day's run. Detect the miss
        # by comparing today's UTC date to ``automation_last_daily_run`` and
        # fire the routines once on startup. Also waits 30s so the rest of
        # the bot (cogs, gateway, sync) settles before we start hitting APIs.
        try:
            await asyncio.sleep(30)
            now = _now()
            today = now.strftime("%Y-%m-%d")
            last = self.bot.db.get_config("automation_last_daily_run") or ""
            scheduled_today = now.replace(hour=0, minute=5, second=0, microsecond=0)
            if last != today and now >= scheduled_today:
                info_log(
                    f"daily_tick catch-up: last run was {last or '(never)'}; "
                    f"today is {today} and we're past 00:05 UTC — running now."
                )
                await self._run_daily_routines(reason="startup catch-up")
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_tick startup catch-up failed: {exc!r}")


# ── Slash commands ──────────────────────────────────────────────────────────

from discord import app_commands


class AutomationGroup(
    app_commands.Group, name="automation",
    description="Automation system: schedules, channels, and policy snapshots.",
):
    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot = bot

    @app_commands.command(
        name="dashboard",
        description="Officer task dashboard — pending workload across the guild.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def dashboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = _build_officer_dashboard_embed(self.bot, interaction.user)
        await interaction.followup.send(
            embed=embed, view=OfficerDashboardView(), ephemeral=True,
        )
        info_log(f"{interaction.user} ran /automation dashboard.")

    @app_commands.command(
        name="set-channel",
        description="Set one of the channels the automation system posts into.",
    )
    @app_commands.describe(
        purpose="Which channel to set.",
        channel="Channel to use.",
    )
    @app_commands.choices(purpose=[
        app_commands.Choice(name="officer-tasks (alerts, sweeps, drift, recon)",
                            value="automation_officer_channel_id"),
        app_commands.Choice(name="announcements (anniversaries)",
                            value="automation_announcements_channel_id"),
        app_commands.Choice(name="hall-of-fame (fame milestones)",
                            value="automation_hall_of_fame_channel_id"),
        app_commands.Choice(name="topic-channel (daily vital-signs topic)",
                            value="automation_topic_channel_id"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(
        self, interaction: discord.Interaction,
        purpose: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config(purpose.value, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed("Channel set", f"`{purpose.name}` → {channel.mention}"),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set {purpose.value} → #{channel.name}.")

    @app_commands.command(
        name="set-voice-channel",
        description="Set the voice channel where event auto-attendance is tracked.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_voice_channel(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel,
    ) -> None:
        self.bot.db.set_config("automation_voice_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Voice channel configured",
                f"Auto-attendance will track {channel.mention}.",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="snapshot-policy",
        description="Save the current pinned content of a channel as the canonical copy.",
    )
    @app_commands.describe(
        channel="Channel whose first pinned bot/staff message becomes the canonical text.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def snapshot_policy(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pins = await channel.pins()
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Cannot read pins",
                    f"I don’t have permission to read pins in {channel.mention}.",
                    hint="Grant **Read Message History** to the bot in that channel.",
                ),
                ephemeral=True,
            )
            return
        if not pins:
            await interaction.followup.send(
                embed=info_embed(
                    "No pins to snapshot",
                    f"{channel.mention} has no pinned messages. Pin the canonical message first.",
                ),
                ephemeral=True,
            )
            return
        msg = pins[0]
        content = msg.content or ""
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.bot.db.upsert_policy_snapshot(
            channel_id=str(channel.id),
            channel_name=channel.name,
            message_id=str(msg.id),
            content=content,
            content_hash=h,
        )
        await interaction.followup.send(
            embed=success_embed(
                "Snapshot saved",
                f"{channel.mention} — {len(content):,} characters captured. Drift checks will compare future pins against this baseline.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} snapshotted policy in #{channel.name} "
            f"(msg {msg.id})."
        )

    @app_commands.command(
        name="forget-policy",
        description="Stop tracking a channel for policy drift.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def forget_policy(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.delete_policy_snapshot(str(channel.id))
        await interaction.response.send_message(
            embed=success_embed("Drift tracking disabled", f"No longer monitoring {channel.mention} for policy drift."),
            ephemeral=True,
        )

    @app_commands.command(
        name="run-now",
        description="Manually run one automation routine immediately (testing).",
    )
    @app_commands.choices(routine=[
        app_commands.Choice(name="anniversaries",   value="anniversaries"),
        app_commands.Choice(name="inactivity",      value="inactivity"),
        app_commands.Choice(name="unverified-nudge", value="unverified_nudge"),
        app_commands.Choice(name="unverified-kicks", value="unverified_kicks"),
        app_commands.Choice(name="auto-alumni",     value="auto_alumni"),
        app_commands.Choice(name="inactivity-nudge", value="inactivity_nudge"),
        app_commands.Choice(name="help-ticket-sla", value="help_sla"),
        app_commands.Choice(name="streak-broken",  value="streak_broken"),
        app_commands.Choice(name="officer-digest", value="officer_digest"),
        app_commands.Choice(name="daily-digest",   value="digest"),
        app_commands.Choice(name="nightly-backup", value="backup"),
        app_commands.Choice(name="policy-drift",    value="drift"),
        app_commands.Choice(name="channel-topic",   value="topic"),
        app_commands.Choice(name="recruit-funnel",  value="funnel"),
        app_commands.Choice(name="treasury-prompt", value="treasury"),
        app_commands.Choice(name="unpaid-silver-reminder", value="unpaid_silver"),
        app_commands.Choice(name="event-reminders", value="reminders"),
        app_commands.Choice(name="underfill-alerts", value="underfill"),
        app_commands.Choice(name="reconcile-events", value="reconcile"),
        app_commands.Choice(name="archive-events",  value="archive"),
        app_commands.Choice(name="orphan-cleanup",  value="orphans"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def run_now(
        self, interaction: discord.Interaction,
        routine: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        try:
            if routine.value == "anniversaries":
                await _post_anniversaries(bot)
            elif routine.value == "inactivity":
                await _post_inactivity_sweep(bot)
            elif routine.value == "unverified_nudge":
                await _run_unverified_nudges(bot)
            elif routine.value == "unverified_kicks":
                await _run_unverified_kicks(bot)
            elif routine.value == "auto_alumni":
                await _run_auto_alumni(bot)
            elif routine.value == "inactivity_nudge":
                await _run_inactivity_nudge(bot)
            elif routine.value == "help_sla":
                await _post_help_ticket_sla(bot)
            elif routine.value == "streak_broken":
                await _post_streak_broken_alerts(bot)
            elif routine.value == "officer_digest":
                await _post_officer_ops_digest(bot)
            elif routine.value == "digest":
                await _post_daily_digest(bot)
            elif routine.value == "backup":
                await _run_nightly_backup(bot)
            elif routine.value == "drift":
                await _check_policy_drift(bot)
            elif routine.value == "topic":
                await _update_channel_topic(bot)
            elif routine.value == "funnel":
                await _post_recruitment_funnel(bot)
            elif routine.value == "treasury":
                await _post_treasury_prompt(bot)
            elif routine.value == "unpaid_silver":
                await _post_unpaid_silver_reminder(bot, force=True)
            elif routine.value == "reminders":
                await _send_event_reminders(bot)
            elif routine.value == "underfill":
                await _send_underfill_alerts(bot)
            elif routine.value == "reconcile":
                await _reconcile_finished_events(bot)
            elif routine.value == "archive":
                archive_completed_events(bot)
            elif routine.value == "orphans":
                cleanup_orphan_guilds(bot)
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Routine failed", f"`{routine.value}` raised `{exc!r}`."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed("Routine complete", f"`{routine.value}` finished successfully."),
            ephemeral=True,
        )

    @app_commands.command(
        name="unverified-nudge-config",
        description="Enable/disable gentle DMs to members stuck at Unverified.",
    )
    @app_commands.describe(
        enabled="Turn the daily nudge DM on or off.",
        days="How many days after joining before the first nudge.",
        cooldown_days="Minimum days between nudges to the same member.",
        max_nudges="Maximum nudges per member before the bot stops DMing them.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unverified_nudge_config(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        days: app_commands.Range[int, 0, 30] = _DEFAULT_UNVERIFIED_NUDGE_DAYS,
        cooldown_days: app_commands.Range[int, 1, 30] = _DEFAULT_UNVERIFIED_NUDGE_COOLDOWN_DAYS,
        max_nudges: app_commands.Range[int, 1, 10] = _DEFAULT_UNVERIFIED_NUDGE_MAX,
    ) -> None:
        self.bot.db.set_config(
            "automation_unverified_nudge_enabled", "1" if enabled else "0",
        )
        self.bot.db.set_config(
            "automation_unverified_nudge_days", str(int(days)),
        )
        self.bot.db.set_config(
            "automation_unverified_nudge_cooldown_days", str(int(cooldown_days)),
        )
        self.bot.db.set_config(
            "automation_unverified_nudge_max", str(int(max_nudges)),
        )
        state = "ENABLED" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                "Unverified nudge configured",
                f"Nudge DMs are **{state}**.\n"
                f"• First nudge after **{int(days)}** day(s) unverified\n"
                f"• Cooldown: **{int(cooldown_days)}** day(s)\n"
                f"• Max nudges/member: **{int(max_nudges)}**\n"
                "Runs daily at 00:05 UTC. Use `/automation run-now unverified-nudge` to trigger manually.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} set unverified-nudge: enabled={enabled} "
            f"days={days} cooldown={cooldown_days} max={max_nudges}."
        )

    @app_commands.command(
        name="unverified-nudge-preview",
        description="Show who would get an Unverified registration nudge right now.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unverified_nudge_preview(
        self,
        interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                embed=error_embed("Guild only", "Run this in a server."),
                ephemeral=True,
            )
            return

        enabled = _get_int_config(self.bot.db, "automation_unverified_nudge_enabled", 1)
        min_days = _get_int_config(
            self.bot.db, "automation_unverified_nudge_days",
            _DEFAULT_UNVERIFIED_NUDGE_DAYS,
        )
        cooldown_days = _get_int_config(
            self.bot.db, "automation_unverified_nudge_cooldown_days",
            _DEFAULT_UNVERIFIED_NUDGE_COOLDOWN_DAYS,
        )
        max_count = _get_int_config(
            self.bot.db, "automation_unverified_nudge_max",
            _DEFAULT_UNVERIFIED_NUDGE_MAX,
        )
        now = _now()
        rows = self.bot.db.fetch_unverified_nudge_targets(
            joined_before_iso=(now - datetime.timedelta(days=min_days)).isoformat(),
            cooldown_iso=(now - datetime.timedelta(days=cooldown_days)).date().isoformat(),
            max_count=max_count,
        )
        row_by_id = {str(r["discord_id"]): r for r in rows}
        role = discord.utils.get(guild.roles, name="Unverified")
        targets: list[tuple[discord.Member, int, int]] = []
        if role is not None:
            for member in role.members:
                row = row_by_id.get(str(member.id))
                if row is None or member.bot:
                    continue
                if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
                    continue
                targets.append(
                    (
                        member,
                        _unverified_age_days(member),
                        int(row.get("unverified_nudge_count") or 0),
                    )
                )
        targets.sort(key=lambda item: item[1], reverse=True)
        if not targets:
            await interaction.followup.send(
                embed=info_embed(
                    "No nudges due",
                    f"No one is due for an Unverified nudge right now.\n"
                    f"Nudges are currently **{'ON' if enabled else 'OFF'}**.",
                ),
                ephemeral=True,
            )
            return

        lines = [
            f"• {m.mention} (`{m}`) — {age}d unverified, {count}/{max_count} nudges used"
            for m, age, count in targets[:25]
        ]
        if len(targets) > 25:
            lines.append(f"…and {len(targets) - 25} more.")
        embed = discord.Embed(
            title=f"📝  Unverified-nudge preview — {len(targets)} due",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=(
                f"Age >= {min_days}d · cooldown {cooldown_days}d · "
                f"max {max_count} · Nudges are {'ON' if enabled else 'OFF'}"
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="unverified-kick-config",
        description="Enable/disable auto-kick of long-Unverified members and set the threshold.",
    )
    @app_commands.describe(
        enabled="Turn the daily auto-kick on or off.",
        days="Days a member must stay Unverified before being kicked (min 1).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unverified_kick_config(
        self, interaction: discord.Interaction,
        enabled: bool,
        days: app_commands.Range[int, 1, 90] = _DEFAULT_UNVERIFIED_KICK_DAYS,
    ) -> None:
        self.bot.db.set_config(
            "automation_unverified_kick_enabled", "1" if enabled else "0",
        )
        self.bot.db.set_config(
            "automation_unverified_kick_days", str(int(days)),
        )
        state = "ENABLED" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                "Unverified auto-kick configured",
                f"Auto-kick is **{state}**, threshold **{days} days**.\n"
                "Runs once per day at 00:05 UTC. Use `/automation run-now "
                "unverified-kicks` to trigger manually, or "
                "`/automation unverified-kick-preview` to see who would be "
                "kicked right now.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} set unverified auto-kick: enabled={enabled} "
            f"days={days}."
        )

    @app_commands.command(
        name="unverified-kick-preview",
        description="Show who WOULD be kicked by the unverified-kick sweep right now (dry run).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unverified_kick_preview(
        self, interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        days = _get_int_config(
            self.bot.db, "automation_unverified_kick_days",
            _DEFAULT_UNVERIFIED_KICK_DAYS,
        )
        enabled = _get_int_config(
            self.bot.db, "automation_unverified_kick_enabled", 0,
        )
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                embed=error_embed("Guild only", "Run this in a server."),
                ephemeral=True,
            )
            return
        targets = _collect_unverified_kick_targets(guild, days)
        stale_roles = _collect_stale_unverified_role_members(guild)
        if not targets:
            detail = (
                f"No one has been Unverified > **{days} days**. ✅\n"
                f"Auto-kick is currently **{'ON' if enabled else 'OFF'}**."
            )
            if stale_roles:
                detail += (
                    f"\n\nThe next cleanup will also remove stale **Unverified** "
                    f"from **{len(stale_roles)}** registered/protected member(s)."
                )
            await interaction.followup.send(
                embed=info_embed(
                    "Nothing to kick",
                    detail,
                ),
                ephemeral=True,
            )
            return
        lines = [
            f"• {m.mention} (`{m}`) — {age}d unverified"
            for m, age in targets[:25]
        ]
        if len(targets) > 25:
            lines.append(f"…and {len(targets) - 25} more.")
        embed = discord.Embed(
            title=f"🚪  Unverified-kick preview — {len(targets)} would be removed",
            description="\n".join(lines),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(
            text=(
                f"Threshold: {days}d · Auto-kick is "
                f"{'ON' if enabled else 'OFF'} · "
                f"{len(stale_roles)} stale role(s) will be cleaned"
            ),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="auto-alumni-config",
        description="Enable/disable auto-demote of long-Inactive members to Alumni.",
    )
    @app_commands.describe(
        enabled="Turn the daily Inactive→Alumni demotion on or off.",
        days="Days at Inactive before auto-demotion (min 7).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def auto_alumni_config(
        self, interaction: discord.Interaction,
        enabled: bool,
        days: app_commands.Range[int, 7, 365] = _DEFAULT_AUTO_ALUMNI_DAYS,
    ) -> None:
        self.bot.db.set_config(
            "automation_auto_alumni_enabled", "1" if enabled else "0",
        )
        self.bot.db.set_config(
            "automation_auto_alumni_days", str(int(days)),
        )
        state = "ENABLED" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                "Auto-Alumni configured",
                f"Auto-Alumni is **{state}**, threshold **{days} days**.\n"
                "Demotes Inactive members to Alumni daily at 00:05 UTC.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} set auto-alumni: enabled={enabled} days={days}."
        )

    @app_commands.command(
        name="inactivity-nudge-config",
        description="Enable/disable the friendly DM sent before someone is flagged Inactive.",
    )
    @app_commands.describe(
        enabled="Turn the daily nudge DM on or off.",
        lead_days="How many days BEFORE the inactivity threshold the nudge fires (1-30).",
        cooldown_days="Don't nudge the same person more than once in this many days (1-90).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def inactivity_nudge_config(
        self, interaction: discord.Interaction,
        enabled: bool,
        lead_days: app_commands.Range[int, 1, 30] = _DEFAULT_INACTIVITY_NUDGE_LEAD_DAYS,
        cooldown_days: app_commands.Range[int, 1, 90] = _DEFAULT_INACTIVITY_NUDGE_COOLDOWN_DAYS,
    ) -> None:
        threshold = _get_int_config(
            self.bot.db, "automation_inactivity_threshold_days", _DEFAULT_INACTIVE_DAYS,
        )
        if int(lead_days) >= threshold:
            await interaction.response.send_message(
                embed=error_embed(
                    "Lead too large",
                    f"`lead_days` ({lead_days}) must be smaller than the inactivity "
                    f"threshold ({threshold} days).",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(
            "automation_inactivity_nudge_enabled", "1" if enabled else "0",
        )
        self.bot.db.set_config(
            "automation_inactivity_nudge_lead_days", str(int(lead_days)),
        )
        self.bot.db.set_config(
            "automation_inactivity_nudge_cooldown_days", str(int(cooldown_days)),
        )
        state = "ENABLED" if enabled else "disabled"
        await interaction.response.send_message(
            embed=success_embed(
                "Inactivity nudge configured",
                f"Nudge DMs are **{state}**.\n"
                f"• Window: members idle **{threshold - int(lead_days)}–{threshold}** days\n"
                f"• Cooldown: **{int(cooldown_days)}** days between nudges to the same member\n"
                "Runs daily at 00:05 UTC.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} set inactivity-nudge: enabled={enabled} "
            f"lead={lead_days} cooldown={cooldown_days}."
        )

    @app_commands.command(
        name="inactivity-preview",
        description="Show the same list the daily inactivity sweep would post (dry run).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def inactivity_preview(
        self, interaction: discord.Interaction,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        embed, count = _build_inactivity_embed(self.bot)
        if embed is None:
            await interaction.followup.send(
                embed=info_embed(
                    "Inactivity preview",
                    "Nobody is over the inactivity threshold. ✅",
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(f"{interaction.user} ran inactivity-preview ({count}).")

    @app_commands.command(
        name="vc-inactive-preview",
        description="Show current TU members below the voice activity threshold.",
    )
    @app_commands.describe(
        days="Look back this many days of voice activity.",
        min_minutes="Minimum voice minutes required in that window.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def vc_inactive_preview(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 14,
        min_minutes: app_commands.Range[int, 0, 720] = 30,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                embed=error_embed("Guild only", "Run this in the server."),
                ephemeral=True,
            )
            return
        targets = _collect_vc_inactive_targets(
            self.bot,
            guild,
            days=int(days),
            min_minutes=int(min_minutes),
        )
        embeds = _build_vc_inactive_embeds(
            targets,
            days=int(days),
            min_minutes=int(min_minutes),
        )
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran vc-inactive-preview: "
            f"days={days}, min_minutes={min_minutes}, candidates={len(targets)}."
        )

    @app_commands.command(
        name="vc-inactive-sweep",
        description="Dry-run or apply VC inactivity demotion to Inactive.",
    )
    @app_commands.describe(
        apply="False = preview only. True = remove HomeGuild and apply Inactive.",
        days="Look back this many days of voice activity.",
        min_minutes="Minimum voice minutes required in that window.",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def vc_inactive_sweep(
        self,
        interaction: discord.Interaction,
        apply: bool = False,
        days: app_commands.Range[int, 1, 90] = 14,
        min_minutes: app_commands.Range[int, 0, 720] = 30,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                embed=error_embed("Guild only", "Run this in the server."),
                ephemeral=True,
            )
            return
        targets = _collect_vc_inactive_targets(
            self.bot,
            guild,
            days=int(days),
            min_minutes=int(min_minutes),
        )
        if not apply:
            embeds = _build_vc_inactive_embeds(
                targets,
                days=int(days),
                min_minutes=int(min_minutes),
            )
            await interaction.followup.send(embed=embeds[0], ephemeral=True)
            for embed in embeds[1:]:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return

        applied, failures = await _apply_vc_inactive_targets(
            self.bot,
            guild,
            targets,
            days=int(days),
            min_minutes=int(min_minutes),
            actor=interaction.user,
        )
        embeds = _build_vc_inactive_embeds(
            applied,
            days=int(days),
            min_minutes=int(min_minutes),
            applied=True,
            failures=failures,
        )
        await interaction.followup.send(embed=embeds[0], ephemeral=True)
        for embed in embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=True)
        officer_channel = _channel(self.bot, "automation_officer_channel_id")
        if officer_channel is not None:
            try:
                for embed in embeds:
                    await officer_channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"vc-inactive officer summary failed: {exc!r}")
        info_log(
            f"{interaction.user} applied vc-inactive-sweep: "
            f"days={days}, min_minutes={min_minutes}, "
            f"applied={len(applied)}, failures={len(failures)}."
        )

    # ── Snooze management ────────────────────────────────────────────────
    # Officers can snooze recurring alerts via buttons on the embed itself,
    # but there's no UI to *see* what's currently snoozed or to lift one
    # early. These two commands fix that gap.
    _SNOOZE_SCOPES: tuple[str, ...] = ("inactivity", "policy")

    @app_commands.command(
        name="snoozes",
        description="List which automation alerts are currently snoozed (and until when).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def snoozes(self, interaction: discord.Interaction) -> None:
        lines: list[str] = []
        for scope in self._SNOOZE_SCOPES:
            raw = self.bot.db.get_config(_snooze_key(scope))
            if not raw:
                lines.append(f"• **{scope}** — not snoozed")
                continue
            try:
                until = datetime.datetime.fromisoformat(
                    str(raw).replace("Z", "+00:00"),
                )
            except ValueError:
                lines.append(f"• **{scope}** — ⚠️ unparseable timestamp `{raw}`")
                continue
            if _now() >= until:
                lines.append(f"• **{scope}** — expired (cleared on next post)")
            else:
                lines.append(
                    f"• **{scope}** — 💤 until <t:{int(until.timestamp())}:f> "
                    f"(<t:{int(until.timestamp())}:R>)"
                )
        await interaction.response.send_message(
            embed=info_embed("Active snoozes", "\n".join(lines)),
            ephemeral=True,
        )

    @app_commands.command(
        name="unsnooze",
        description="Lift a snooze early so the next daily run can post the alert again.",
    )
    @app_commands.describe(scope="Which alert to un-snooze.")
    @app_commands.choices(scope=[
        app_commands.Choice(name="Inactivity sweep", value="inactivity"),
        app_commands.Choice(name="Policy drift",     value="policy"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unsnooze(
        self, interaction: discord.Interaction,
        scope: app_commands.Choice[str],
    ) -> None:
        _clear_snooze(self.bot, scope.value)
        await interaction.response.send_message(
            embed=success_embed(
                "Snooze cleared",
                f"`{scope.name}` will fire again on the next daily run.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} cleared {scope.value} snooze.")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Automation(bot))
