"""Unverified registration cleanup helpers for the automation cog."""
from __future__ import annotations

import datetime

import discord

from cogs._automation_helpers import (
    _DEFAULT_UNVERIFIED_KICK_DAYS,
    _channel,
    _get_int_config,
    _now,
)
from cogs._typing import Bot
from config import LIFECYCLE_ROLES, STAFF_ROLES
from debug import error_log, info_log


# Members get the "Unverified" role on join. If they never register / verify,
# they sit there forever. This sweep kicks anyone who has been Unverified for
# longer than ``automation_unverified_kick_days`` (default 7), but protects
# anyone who already has a registered/member role and only needs stale-role
# cleanup.
_UNVERIFIED_KICK_PROTECTED_ROLE_NAMES = frozenset(
    {
        "Verified",
        "HomeGuild",
        "Alliance",
        "Guest",
        "Ambassador",
        "Commander",
        "Guild Leader",
        *LIFECYCLE_ROLES,
        *STAFF_ROLES,
    }
)


def _member_has_unverified_cleanup_protection(member: discord.Member) -> bool:
    role_names = {getattr(r, "name", "") for r in getattr(member, "roles", [])}
    return bool(role_names & _UNVERIFIED_KICK_PROTECTED_ROLE_NAMES)


def _collect_stale_unverified_role_members(guild: discord.Guild) -> list[discord.Member]:
    """Members who are registered/protected but still carry Unverified."""
    role = discord.utils.get(guild.roles, name="Unverified")
    if role is None:
        return []
    members: list[discord.Member] = []
    for member in role.members:
        if member.bot or member == guild.owner:
            continue
        if _member_has_unverified_cleanup_protection(member):
            members.append(member)
    members.sort(key=lambda m: str(m).lower())
    return members


def _collect_unverified_kick_targets(
    guild: discord.Guild,
    days: int,
) -> list[tuple[discord.Member, int]]:
    """Return Unverified kick targets sorted oldest-first."""
    role = discord.utils.get(guild.roles, name="Unverified")
    if role is None:
        return []
    cutoff = _now() - datetime.timedelta(days=days)
    targets: list[tuple[discord.Member, int]] = []
    for member in role.members:
        if member.bot:
            continue
        if member == guild.owner:
            continue
        perms = member.guild_permissions
        if perms.manage_guild or perms.administrator:
            continue
        if _member_has_unverified_cleanup_protection(member):
            continue
        if not member.joined_at:
            continue
        if member.joined_at > cutoff:
            continue
        age = (_now() - member.joined_at).days
        targets.append((member, age))
    targets.sort(key=lambda t: t[1], reverse=True)
    return targets


async def _run_unverified_kicks(bot: Bot) -> None:
    """Daily sweep: kick members who have been Unverified for too long."""
    enabled = _get_int_config(bot.db, "automation_unverified_kick_enabled", 0)
    if not enabled:
        return
    days = _get_int_config(
        bot.db,
        "automation_unverified_kick_days",
        _DEFAULT_UNVERIFIED_KICK_DAYS,
    )
    if days < 1:
        info_log(f"Unverified-kick days={days} invalid; skipping.")
        return

    officer_channel = _channel(bot, "automation_officer_channel_id")
    kicked: list[str] = []
    failed: list[str] = []
    cleaned: list[str] = []
    cleanup_failed: list[str] = []
    for guild in bot.guilds:
        unverified_role = discord.utils.get(guild.roles, name="Unverified")
        if unverified_role is not None:
            for member in _collect_stale_unverified_role_members(guild):
                try:
                    await member.remove_roles(
                        unverified_role,
                        reason="Cleanup stale Unverified role on registered/protected member",
                    )
                    cleaned.append(f"{member} ({member.id})")
                    info_log(f"Removed stale Unverified role from {member} ({member.id}).")
                except discord.Forbidden:
                    cleanup_failed.append(f"{member} — missing role permissions")
                    error_log(
                        f"Stale Unverified cleanup blocked for {member} "
                        "(role hierarchy?)."
                    )
                except discord.HTTPException as exc:
                    cleanup_failed.append(f"{member} — {exc}")
                    error_log(f"Stale Unverified cleanup HTTP error for {member}: {exc!r}")

        targets = _collect_unverified_kick_targets(guild, days)
        for member, age in targets:
            reason = (
                f"Auto-kick: Unverified for {age} days "
                f"(threshold {days}d). Re-join and verify any time."
            )
            try:
                await member.send(
                    f"You have been removed from **{guild.name}** because "
                    f"your account stayed Unverified for {age} days. "
                    "You're welcome to rejoin and complete verification."
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"Unverified-kick DM failed for {member}: {exc!r}")
            try:
                await member.kick(reason=reason)
                kicked.append(f"{member} ({member.id}) — {age}d in {guild.name}")
                info_log(
                    f"Auto-kicked unverified {member} ({member.id}) "
                    f"after {age} days."
                )
            except discord.Forbidden:
                failed.append(f"{member} — missing kick perms / role hierarchy")
                error_log(
                    f"Auto-kick blocked: bot can't kick {member} "
                    "(role hierarchy?)."
                )
            except discord.HTTPException as exc:
                failed.append(f"{member} — {exc}")
                error_log(f"Auto-kick HTTP error for {member}: {exc!r}")

    if not kicked and not failed and not cleaned and not cleanup_failed:
        return
    if officer_channel is None:
        info_log(
            f"Unverified-kick: {len(kicked)} kicked, {len(failed)} failed, "
            f"{len(cleaned)} stale roles cleaned, {len(cleanup_failed)} cleanup failed; "
            "no officer channel configured."
        )
        return

    lines: list[str] = []
    if cleaned:
        lines.append(f"**Stale Unverified role cleaned ({len(cleaned)}):**")
        lines.extend(f"• {item}" for item in cleaned[:10])
        if len(cleaned) > 10:
            lines.append(f"…and {len(cleaned) - 10} more.")
    if kicked:
        if lines:
            lines.append("")
        lines.append(f"**Kicked ({len(kicked)}):**")
        lines.extend(f"• {k}" for k in kicked[:25])
        if len(kicked) > 25:
            lines.append(f"…and {len(kicked) - 25} more.")
    if failed:
        lines.append(f"\n**Kick failed ({len(failed)}):**")
        lines.extend(f"• {f}" for f in failed[:10])
    if cleanup_failed:
        lines.append(f"\n**Stale role cleanup failed ({len(cleanup_failed)}):**")
        lines.extend(f"• {f}" for f in cleanup_failed[:10])

    embed = discord.Embed(
        title=(
            f"🚪  Unverified cleanup — {len(kicked)} kicked, "
            f"{len(cleaned)} stale cleaned"
        ),
        description="\n".join(lines),
        color=discord.Color.dark_red(),
    )
    embed.set_footer(text=f"Threshold: {days} days unverified")
    try:
        await officer_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"unverified-kick summary post failed: {exc!r}")
