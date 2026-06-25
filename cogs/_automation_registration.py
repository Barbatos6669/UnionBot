"""Registration cleanup and Unverified nudge helpers for automation."""
from __future__ import annotations

import asyncio
import datetime

import discord

from cogs._automation_helpers import (
    _DEFAULT_UNVERIFIED_KICK_DAYS,
    _DEFAULT_UNVERIFIED_NUDGE_COOLDOWN_DAYS,
    _DEFAULT_UNVERIFIED_NUDGE_DAYS,
    _DEFAULT_UNVERIFIED_NUDGE_MAX,
    _channel,
    _get_int_config,
    _now,
)
from cogs._typing import Bot
from debug import error_log, info_log
from utils import info_embed


def _unverified_age_days(member: discord.Member) -> int:
    if not member.joined_at:
        return 0
    return max(0, (_now() - member.joined_at).days)


def _registration_channel_mention(guild: discord.Guild, bot: Bot) -> str:
    raw = bot.db.get_config("registration_channel_id")
    if not raw:
        return "the registration channel"
    try:
        channel = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        channel = None
    if isinstance(channel, discord.TextChannel):
        return channel.mention
    return "the registration channel"


def _collect_registration_cleanup_targets(
    guild: discord.Guild,
    *,
    min_days: int = 0,
) -> list[tuple[discord.Member, int]]:
    """Current Unverified members eligible for manual registration cleanup."""
    role = discord.utils.get(guild.roles, name="Unverified")
    if role is None:
        return []
    min_days = max(0, int(min_days or 0))
    targets: list[tuple[discord.Member, int]] = []
    for member in role.members:
        if member.bot:
            continue
        if member == guild.owner:
            continue
        perms = member.guild_permissions
        if perms.manage_guild or perms.administrator:
            continue
        age = _unverified_age_days(member)
        if age < min_days:
            continue
        targets.append((member, age))
    targets.sort(key=lambda t: t[1], reverse=True)
    return targets


def _build_registration_cleanup_embed(
    bot: Bot,
    guild: discord.Guild,
) -> discord.Embed:
    """Build the officer task embed attached to registration-cleanup buttons."""
    days = _get_int_config(
        bot.db,
        "automation_unverified_kick_days",
        _DEFAULT_UNVERIFIED_KICK_DAYS,
    )
    targets = _collect_registration_cleanup_targets(guild, min_days=0)
    kick_targets = _collect_registration_cleanup_targets(guild, min_days=days)
    register_link = _registration_channel_mention(guild, bot)

    sample = [
        f"• {member.mention} (`{member}`) — {age}d"
        for member, age in targets[:15]
    ]
    if len(targets) > 15:
        sample.append(f"…and {len(targets) - 15} more.")
    sample_text = "\n".join(sample) if sample else "No current Unverified members found."

    embed = discord.Embed(
        title="🛡️ Officer Task — Registration Cleanup",
        description=(
            "Choose an action below for members who still have the **Unverified** role.\n\n"
            f"Registration channel: {register_link}"
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Current status",
        value=(
            f"Unverified members: **{len(targets)}**\n"
            f"Kick-eligible at **{days}d+**: **{len(kick_targets)}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Button actions",
        value=(
            "🔄 **Refresh list** updates this task.\n"
            "📝 **DM register reminder** messages every current Unverified member.\n"
            f"🚪 **Kick eligible** starts a confirmation for Unverified members **{days}d+**."
        ),
        inline=False,
    )
    embed.add_field(name="Current candidates", value=sample_text[:1024], inline=False)
    embed.set_footer(text="Officer-only controls · Kick action requires confirmation")
    return embed


async def _run_registration_cleanup_nudges(
    bot: Bot,
    guild: discord.Guild,
    *,
    actor: discord.abc.User,
) -> discord.Embed:
    """DM registration reminders to all current Unverified members."""
    targets = _collect_registration_cleanup_targets(guild, min_days=0)
    if not targets:
        return info_embed(
            "No unverified members",
            "No current Unverified members were found.",
        )
    register_link = _registration_channel_mention(guild, bot)
    today_iso = _now().date().isoformat()
    sent: list[str] = []
    dm_closed: list[str] = []
    failed: list[str] = []
    for member, age in targets:
        try:
            await member.send(
                f"Hey, please register in **{guild.name}** so staff can confirm "
                f"you are an Albion Online player.\n\n"
                f"Go to {register_link}, click **Register**, enter your Albion "
                "character name, then upload the requested character screenshot. "
                "The bot uses the Americas server automatically.\n\n"
                "If you are stuck, reply in the server or ask an officer for help."
            )
            bot.db.mark_unverified_nudge_sent(str(member.id), today_iso)
            sent.append(f"{member} — {age}d")
        except discord.Forbidden:
            bot.db.mark_unverified_nudge_sent(str(member.id), today_iso)
            dm_closed.append(f"{member} — DMs closed")
        except discord.HTTPException as exc:
            failed.append(f"{member} — {exc}")
        await asyncio.sleep(0.25)

    lines: list[str] = []
    if sent:
        lines.append(f"**DM sent ({len(sent)}):**")
        lines.extend(f"• {item}" for item in sent[:20])
        if len(sent) > 20:
            lines.append(f"…and {len(sent) - 20} more.")
    if dm_closed:
        lines.append(f"\n**DMs closed ({len(dm_closed)}):**")
        lines.extend(f"• {item}" for item in dm_closed[:10])
    if failed:
        lines.append(f"\n**Failed ({len(failed)}):**")
        lines.extend(f"• {item}" for item in failed[:10])
    info_log(
        f"{actor} ran manual registration nudges: "
        f"sent={len(sent)} closed={len(dm_closed)} failed={len(failed)}."
    )
    embed = discord.Embed(
        title=f"📝 Registration reminders — {len(sent)} DM(s) sent",
        description="\n".join(lines) or "No messages were sent.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Triggered by {actor}")
    return embed


async def _run_registration_cleanup_kicks(
    bot: Bot,
    guild: discord.Guild,
    *,
    actor: discord.abc.User,
    days: int,
) -> discord.Embed:
    """Kick current Unverified members over the configured threshold."""
    days = max(1, int(days or _DEFAULT_UNVERIFIED_KICK_DAYS))
    targets = _collect_registration_cleanup_targets(guild, min_days=days)
    if not targets:
        return info_embed(
            "Nothing eligible to kick",
            f"No Unverified members are past the **{days}d** kick threshold.",
        )
    kicked: list[str] = []
    failed: list[str] = []
    for member, age in targets:
        reason = (
            f"Officer registration cleanup by {actor}: "
            f"Unverified for {age} days (threshold {days}d)."
        )
        try:
            await member.send(
                f"You have been removed from **{guild.name}** because your account "
                f"stayed Unverified for {age} days. You may rejoin and complete "
                "registration if you want access."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await member.kick(reason=reason)
            kicked.append(f"{member} ({member.id}) — {age}d")
            info_log(f"{actor} kicked unverified {member} ({member.id}) after {age}d.")
        except discord.Forbidden:
            failed.append(f"{member} — missing kick permissions / role hierarchy")
        except discord.HTTPException as exc:
            failed.append(f"{member} — {exc}")
        await asyncio.sleep(0.25)

    lines: list[str] = []
    if kicked:
        lines.append(f"**Kicked ({len(kicked)}):**")
        lines.extend(f"• {item}" for item in kicked[:25])
        if len(kicked) > 25:
            lines.append(f"…and {len(kicked) - 25} more.")
    if failed:
        lines.append(f"\n**Failed ({len(failed)}):**")
        lines.extend(f"• {item}" for item in failed[:10])
    embed = discord.Embed(
        title=f"🚪 Registration cleanup — {len(kicked)} kicked",
        description="\n".join(lines) or "No members were kicked.",
        color=discord.Color.dark_red(),
    )
    embed.set_footer(text=f"Threshold: {days}d · Triggered by {actor}")
    return embed


async def _run_unverified_nudges(bot: Bot) -> None:
    enabled = _get_int_config(bot.db, "automation_unverified_nudge_enabled", 1)
    if not enabled:
        return

    min_days = _get_int_config(
        bot.db,
        "automation_unverified_nudge_days",
        _DEFAULT_UNVERIFIED_NUDGE_DAYS,
    )
    cooldown_days = _get_int_config(
        bot.db,
        "automation_unverified_nudge_cooldown_days",
        _DEFAULT_UNVERIFIED_NUDGE_COOLDOWN_DAYS,
    )
    max_count = _get_int_config(
        bot.db,
        "automation_unverified_nudge_max",
        _DEFAULT_UNVERIFIED_NUDGE_MAX,
    )
    if min_days < 0 or cooldown_days < 1 or max_count < 1:
        info_log(
            f"unverified-nudge config invalid "
            f"(days={min_days}, cooldown={cooldown_days}, max={max_count}); skipping."
        )
        return

    now = _now()
    today_iso = now.date().isoformat()
    joined_before_iso = (now - datetime.timedelta(days=min_days)).isoformat()
    cooldown_iso = (now - datetime.timedelta(days=cooldown_days)).date().isoformat()
    rows = bot.db.fetch_unverified_nudge_targets(
        joined_before_iso=joined_before_iso,
        cooldown_iso=cooldown_iso,
        max_count=max_count,
    )
    if not rows:
        return

    row_by_id = {str(r["discord_id"]): r for r in rows}
    nudged: list[str] = []
    skipped_dms: list[str] = []
    failed: list[str] = []
    for guild in bot.guilds:
        role = discord.utils.get(guild.roles, name="Unverified")
        if role is None:
            continue
        register_link = _registration_channel_mention(guild, bot)
        for member in list(role.members):
            if member.bot:
                continue
            row = row_by_id.get(str(member.id))
            if row is None:
                continue
            if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
                continue
            age = _unverified_age_days(member)
            prior_count = int(row.get("unverified_nudge_count") or 0)
            try:
                await member.send(
                    f"Hey, quick reminder from **{guild.name}**: you're still **Unverified**.\n\n"
                    f"To unlock the server, head to {register_link} and click **Register**. "
                    "The bot will ask for your Albion character name, then a screenshot so staff can approve you. "
                    "It uses the Americas server automatically.\n\n"
                    "If you're stuck, reply in the server or ping an officer and we'll help."
                )
                bot.db.mark_unverified_nudge_sent(str(member.id), today_iso)
                nudged.append(
                    f"{member.mention} (`{member}`) — {age}d unverified, "
                    f"nudge {prior_count + 1}/{max_count}"
                )
            except discord.Forbidden:
                bot.db.mark_unverified_nudge_sent(str(member.id), today_iso)
                skipped_dms.append(f"{member} ({member.id}) — DMs closed")
            except discord.HTTPException as exc:
                failed.append(f"{member} ({member.id}): {exc}")

    info_log(
        f"unverified-nudge: nudged={len(nudged)} dm-closed={len(skipped_dms)} "
        f"failed={len(failed)} age>={min_days}d cooldown={cooldown_days}d max={max_count}."
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
        title=f"📝  Unverified nudge — {len(nudged)} DM(s) sent",
        description="\n".join(parts),
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=f"Age >= {min_days}d · cooldown {cooldown_days}d · max {max_count} nudges/member",
    )
    try:
        await officer_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"unverified-nudge summary post failed: {exc!r}")
