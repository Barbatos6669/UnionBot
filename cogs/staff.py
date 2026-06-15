"""
Staff application & rank management.

Members apply for ranks via /staff apply or by clicking the Apply button on the
staff board. Officers review with /staff approve|deny. Slots scale automatically
with guild size; when the guild shrinks, the least-active holders are demoted.
"""
from typing import Optional
from cogs._typing import Bot
import asyncio
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

from debug import info_log, warning_log, error_log
from config import STAFF_ROLES, STAFF_TIERS, STAFF_DESCRIPTIONS
from utils import confirm_action, error_embed, info_embed, success_embed, warning_embed

# Lifecycle roles that have the activity standing to apply for staff at all.
# Recruit / Probationary / Inactive / Alumni cannot apply.
_APPLY_BLOCKED_LIFECYCLES = {"Recruit", "Probationary", "Inactive", "Alumni", None, ""}

# Ranks allowed to review applications and run staff config.
_REVIEWER_ROLES = ("Captain", "Officer")


# ── helpers ──────────────────────────────────────────────────────────────────

def _rank_choices() -> list:
    return [app_commands.Choice(name=r, value=r) for r in STAFF_ROLES]


def _tier_settings(db, rank: str) -> dict:
    """Return effective {eligible, per_slot, max_cap, prereq_role, prereq_days} for a rank.
    DB overrides apply to per_slot, max_cap, and prereq_days."""
    base = STAFF_TIERS[rank]
    per_slot = db.get_config(f"staff_{rank}_per_slot")
    max_cap  = db.get_config(f"staff_{rank}_max")
    prereq_days_override = db.get_config(f"staff_{rank}_prereq_days")
    return {
        "eligible": base["eligible"],
        "per_slot": int(per_slot) if per_slot else base["per_slot"],
        "max_cap":  int(max_cap)  if max_cap  else base["max_cap"],
        "prereq_role": base.get("prereq_role"),
        "prereq_days": int(prereq_days_override) if prereq_days_override else base.get("prereq_days", 0),
    }


def _slot_count(db, rank: str, guild_size: int) -> int:
    s = _tier_settings(db, rank)
    return min(s["max_cap"], guild_size // s["per_slot"])


def _holders_in_guild(discord_guild: discord.Guild, rank: str) -> list:
    role = discord.utils.get(discord_guild.roles, name=rank)
    if not role:
        return []
    return list(role.members)


def _is_reviewer(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in _REVIEWER_ROLES for r in member.roles)


async def rebalance_staff(bot: Bot) -> None:
    """For each staff rank, if current holders exceed slot capacity, demote the least active.
    Runs after the hourly sync so demotions reflect the latest activity data.

    Gated by the ``staff_rebalance_enabled`` config flag (default off) so freshly-onboarded
    servers don't immediately demote existing officers before activity data is collected.
    """
    if str(bot.db.get_config("staff_rebalance_enabled") or "0").lower() not in ("1", "true", "yes", "on"):
        return
    guild_size = bot.db.count_registered_members()
    for discord_guild in bot.guilds:
        for rank in STAFF_ROLES:
            slots = _slot_count(bot.db, rank, guild_size)
            holders = _holders_in_guild(discord_guild, rank)
            if len(holders) <= slots:
                continue

            holder_ids = [str(m.id) for m in holders]
            ordered = bot.db.fetch_staff_holders_with_activity(rank, holder_ids)
            # Members not in DB still need to be considered; treat as least active.
            known_ids = {row["discord_id"] for row in ordered}
            unknown = [m for m in holders if str(m.id) not in known_ids]
            ordered_ids = [m["discord_id"] for m in unknown] + [row["discord_id"] for row in ordered]

            to_demote = len(holders) - slots
            demote_ids = ordered_ids[:to_demote]
            role = discord.utils.get(discord_guild.roles, name=rank)
            for did in demote_ids:
                member = discord_guild.get_member(int(did))
                if not member or role not in member.roles:
                    continue
                try:
                    await member.remove_roles(role, reason=f"Staff rebalance: {rank} over capacity ({len(holders)}/{slots})")
                    info_log(f"Demoted {member.display_name} from {rank} (rebalance, slots={slots}).")
                    try:
                        await member.send(
                            f"Hey — guild population shrank, so the **{rank}** rank is being downsized to "
                            f"{slots} slot(s). You were the least active holder and have been demoted. "
                            f"You can re-apply once activity recovers."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                except discord.Forbidden:
                    warning_log(f"Missing perms to demote {member.display_name} from {rank}.")


# ── application processing (shared by /staff apply and the Apply button) ────

async def _process_application(bot: Bot, interaction: discord.Interaction, rank_name: str, reason: str) -> None:
    """Validate eligibility/slots, insert the application, notify officers, and reply.
    Caller must NOT have already responded to the interaction."""
    member = interaction.user
    profile = bot.db.fetch_user_profile(str(member.id))

    if not profile or not profile.get("albion_player_id"):
        await interaction.response.send_message(
            embed=error_embed(
                "Registration required",
                "You must register your Albion character before applying.",
                hint="Use the registration button in the welcome channel first.",
            ),
            ephemeral=True,
        )
        return

    lifecycle = profile.get("lifecycle_role")
    tier = _tier_settings(bot.db, rank_name)

    # Shotcaller is open to all lifecycles (call-out ability is independent of tenure),
    # so skip the activity-standing gate for that rank only.
    if rank_name != "Shotcaller" and lifecycle in _APPLY_BLOCKED_LIFECYCLES:
        await interaction.response.send_message(
            embed=error_embed(
                "Not eligible to apply",
                f"Your lifecycle role (**{lifecycle or 'unset'}**) is not eligible.",
                hint="You need to be active and reach **Member** or **Veteran** first.",
            ),
            ephemeral=True,
        )
        return

    if lifecycle not in tier["eligible"]:
        need = " or ".join(tier["eligible"])
        await interaction.response.send_message(
            embed=error_embed(
                "Lifecycle requirement not met",
                f"**{rank_name}** requires lifecycle **{need}**. You are currently **{lifecycle}**.",
            ),
            ephemeral=True,
        )
        return

    # Tenure prerequisite (e.g. Captain requires 30 days served as Officer)
    prereq_role = tier.get("prereq_role")
    prereq_days = tier.get("prereq_days") or 0
    if prereq_role and prereq_days > 0:
        prereq_role_obj = discord.utils.get(interaction.guild.roles, name=prereq_role)
        if not prereq_role_obj or prereq_role_obj not in member.roles:
            await interaction.response.send_message(
                embed=error_embed(
                    "Prerequisite role missing",
                    f"**{rank_name}** requires you to currently hold **{prereq_role}** "
                    f"and have served for at least {prereq_days} days.",
                ),
                ephemeral=True,
            )
            return
        granted_at = bot.db.fetch_first_grant_date(str(member.id), prereq_role)
        if not granted_at:
            await interaction.response.send_message(
                embed=error_embed(
                    "Tenure record missing",
                    f"No tenure record found for **{prereq_role}**.",
                    hint=f"Ask an officer to backfill via `/staff record-grant`, or wait {prereq_days} days from now.",
                ),
                ephemeral=True,
            )
            return
        try:
            granted_dt = datetime.datetime.fromisoformat(granted_at)
        except (TypeError, ValueError):
            granted_dt = datetime.datetime.utcnow()
        served_days = (datetime.datetime.utcnow() - granted_dt).days
        if served_days < prereq_days:
            remaining = prereq_days - served_days
            await interaction.response.send_message(
                embed=error_embed(
                    "Tenure not met",
                    f"**{rank_name}** requires {prereq_days} days as **{prereq_role}**. "
                    f"You have served {served_days} days \u2014 **{remaining}** more to go.",
                ),
                ephemeral=True,
            )
            return

    rank_role = discord.utils.get(interaction.guild.roles, name=rank_name)
    if rank_role and rank_role in member.roles:
        await interaction.response.send_message(
            embed=info_embed("Already held", f"You already hold **{rank_name}**."),
            ephemeral=True,
        )
        return

    if bot.db.fetch_pending_application(str(member.id), rank_name):
        await interaction.response.send_message(
            embed=info_embed("Already pending", f"You already have a pending application for **{rank_name}**."),
            ephemeral=True,
        )
        return

    guild_size = bot.db.count_registered_members()
    slots = _slot_count(bot.db, rank_name, guild_size)
    current = len(_holders_in_guild(interaction.guild, rank_name))
    if slots <= 0:
        await interaction.response.send_message(
            embed=info_embed(
                "No slots available",
                f"**{rank_name}** has no slots at the current guild size ({guild_size}). Slots open up as the guild grows.",
            ),
            ephemeral=True,
        )
        return
    if current >= slots:
        await interaction.response.send_message(
            embed=info_embed(
                "Rank is full",
                f"**{rank_name}** is currently full ({current}/{slots}). Try again when a slot opens.",
            ),
            ephemeral=True,
        )
        return

    reason = reason.strip()
    if len(reason) < 20:
        await interaction.response.send_message(
            embed=warning_embed(
                "Reason too short",
                "Please write a more detailed reason (at least 20 characters).",
            ),
            ephemeral=True,
        )
        return

    app_id = bot.db.insert_staff_application(str(member.id), rank_name, reason)

    # Notify officer channel
    officer_channel_id = bot.db.get_config("officer_channel_id")
    if officer_channel_id:
        channel = interaction.guild.get_channel(int(officer_channel_id))
        if channel:
            app = bot.db.fetch_staff_application(app_id)
            try:
                if app:
                    await _post_or_update_review_message(bot, interaction.guild, channel, app)
            except discord.Forbidden:
                # Silent-fail used to mask config errors. Log so the
                # operator can find out the bot can't post in their
                # configured officer channel.
                from debug import warning_log
                warning_log(
                    f"staff: Forbidden posting application #{app_id} to "
                    f"officer channel #{channel.name} ({channel.id}). "
                    f"Check channel-level overwrites for UnionBot."
                )
            except discord.HTTPException as exc:
                warning_log(
                    f"staff: Discord rejected application #{app_id} review "
                    f"post/update in #{channel.name} ({channel.id}): {exc!r}"
                )
        else:
            from debug import warning_log
            warning_log(
                f"staff: officer_channel_id={officer_channel_id} not found "
                f"in guild {interaction.guild.id}; application #{app_id} "
                f"recorded but no notification sent."
            )
    else:
        from debug import warning_log
        warning_log(
            f"staff: officer_channel_id is unset; application #{app_id} "
            f"recorded but no notification sent. Run /set-officer-channel."
        )

    await interaction.response.send_message(
        embed=success_embed(
            f"Application #{app_id} submitted",
            f"Your application for **{rank_name}** is in. Officers will review it shortly.",
        ),
        ephemeral=True,
    )
    info_log(f"{member} applied for {rank_name} (app #{app_id}).")


# ── persistent Apply button + Modal ─────────────────────────────────────────

class StaffApplyModal(discord.ui.Modal):
    def __init__(self, bot: Bot, rank_name: str):
        super().__init__(title=f"Apply: {rank_name}"[:45])
        self.bot: Bot = bot
        self.rank_name = rank_name
        self.reason = discord.ui.TextInput(
            label="Why are you a good fit?",
            placeholder="Be specific: experience, availability, what you'd contribute...",
            style=discord.TextStyle.paragraph,
            min_length=20,
            max_length=1000,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _process_application(self.bot, interaction, self.rank_name, str(self.reason.value))


class StaffApplyView(discord.ui.View):
    """Persistent view holding one Apply button per rank.
    custom_id encodes the rank so a single view class handles all ranks."""

    def __init__(self, bot: Bot = None):
        super().__init__(timeout=None)
        self.bot = bot

    @staticmethod
    def for_rank(bot: Bot, rank_name: str) -> "StaffApplyView":
        view = StaffApplyView(bot)
        button = discord.ui.Button(
            label=f"Apply for {rank_name}",
            style=discord.ButtonStyle.primary,
            custom_id=f"staff_apply::{rank_name}",
        )
        async def _callback(interaction: discord.Interaction):
            await interaction.response.send_modal(StaffApplyModal(bot, rank_name))
        button.callback = _callback
        view.add_item(button)
        return view


def _board_embed(bot: Bot, discord_guild: discord.Guild, rank_name: str) -> discord.Embed:
    desc = STAFF_DESCRIPTIONS.get(rank_name, {})
    tier = _tier_settings(bot.db, rank_name)
    guild_size = bot.db.count_registered_members()
    slots = _slot_count(bot.db, rank_name, guild_size)
    held = len(_holders_in_guild(discord_guild, rank_name))
    is_open = held < slots and slots > 0

    color = discord.Color.green() if is_open else discord.Color.dark_grey()
    status = f"🟢 **Open** — {held}/{slots} filled" if is_open else f"🔴 **Full** — {held}/{slots} filled"
    if slots == 0:
        status = "⚪ **No slots** at current guild size"

    embed = discord.Embed(
        title=f"{rank_name}",
        description=desc.get("purpose", ""),
        color=color,
    )
    embed.add_field(name="Status", value=status, inline=False)
    if desc.get("responsibilities"):
        embed.add_field(
            name="Responsibilities",
            value="\n".join(f"• {r}" for r in desc["responsibilities"]),
            inline=False,
        )
    if desc.get("expected"):
        embed.add_field(name="Expected behavior", value=desc["expected"], inline=False)
    eligible = " or ".join(tier["eligible"])
    elig_value = f"Lifecycle: **{eligible}** • 1 slot per {tier['per_slot']} members (cap {tier['max_cap']})"
    if tier.get("prereq_role") and tier.get("prereq_days"):
        elig_value += f"\nPrerequisite: **{tier['prereq_days']} days** served as **{tier['prereq_role']}**"
    embed.add_field(name="Eligibility", value=elig_value, inline=False)
    return embed


def _review_embed(bot: Bot, discord_guild: discord.Guild, app: dict) -> discord.Embed:
    rank_name = app["rank"]
    applicant = discord_guild.get_member(int(app["discord_id"]))
    profile = bot.db.fetch_user_profile(str(app["discord_id"])) or {}
    guild_size = bot.db.count_registered_members()
    slots = _slot_count(bot.db, rank_name, guild_size)
    current = len(_holders_in_guild(discord_guild, rank_name))
    embed = discord.Embed(
        title=f"Pending staff application — {rank_name}",
        description=app.get("reason") or "*(no reason given)*",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="Applicant",
        value=applicant.mention if applicant else f"<@{app['discord_id']}> (left server)",
        inline=True,
    )
    embed.add_field(
        name="Lifecycle",
        value=profile.get("lifecycle_role") or "—",
        inline=True,
    )
    embed.add_field(name="Slots", value=f"{current}/{slots}", inline=True)
    embed.add_field(name="Applied", value=app.get("applied_at") or "—", inline=True)
    embed.set_footer(text=f"Application #{app['id']} • Approve or Deny below")
    return embed


async def _post_or_update_review_message(
    bot: Bot,
    discord_guild: discord.Guild,
    channel: discord.TextChannel,
    app: dict,
) -> str:
    """Create or update the officer review post for a pending staff app."""
    embed = _review_embed(bot, discord_guild, app)
    view = build_review_view(int(app["id"]))
    msg = None
    existing_channel_id = app.get("review_channel_id")
    existing_message_id = app.get("review_message_id")

    if existing_channel_id and existing_message_id:
        try:
            old_channel = discord_guild.get_channel(int(existing_channel_id))
            if isinstance(old_channel, discord.TextChannel):
                msg = await old_channel.fetch_message(int(existing_message_id))
                if old_channel.id != channel.id:
                    await msg.delete()
                    msg = None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            msg = None

    if msg is not None:
        await msg.edit(embed=embed, view=view)
        return "updated"

    msg = await channel.send(embed=embed, view=view)
    bot.db.set_staff_application_review_message(
        int(app["id"]),
        str(channel.id),
        str(msg.id),
    )
    return "posted"


async def _mark_review_message_resolved(
    bot: Bot,
    discord_guild: discord.Guild,
    app_id: int,
    status: str,
) -> None:
    app = bot.db.fetch_staff_application(app_id)
    if not app:
        return
    channel_id = app.get("review_channel_id")
    message_id = app.get("review_message_id")
    if not channel_id or not message_id:
        return
    try:
        channel = discord_guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return
        message = await channel.fetch_message(int(message_id))
        await message.edit(view=_resolved_view(status))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
        return


async def _post_or_edit_board_message(
    bot: Bot,
    channel: discord.TextChannel,
    rank_name: str,
    *,
    create_missing: bool = True,
) -> None:
    embed = _board_embed(bot, channel.guild, rank_name)
    view = StaffApplyView.for_rank(bot, rank_name)
    msg_id_str = bot.db.get_config(f"staff_board_msg_{rank_name}")
    msg = None
    if msg_id_str:
        try:
            msg = await channel.fetch_message(int(msg_id_str))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            msg = None
    if msg:
        try:
            await msg.edit(embed=embed, view=view)
            return
        except (discord.Forbidden, discord.HTTPException):
            pass
    if not create_missing:
        return
    new_msg = await channel.send(embed=embed, view=view)
    bot.db.set_config(f"staff_board_msg_{rank_name}", str(new_msg.id))


async def refresh_staff_board(bot: Bot) -> None:
    """Edit each rank's board message in place to reflect current slot status.

    Spaces edits ~1s apart so all PATCHes in the same channel don't trip
    Discord's per-channel anti-burst rate limiter. Used for full board
    rebuilds (startup, /admin setup, etc.). For single-rank changes after
    an approval, prefer ``refresh_staff_board_for_rank``.
    """
    channel_id = bot.db.get_config("staff_board_channel_id")
    if not channel_id:
        return
    for discord_guild in bot.guilds:
        channel = discord_guild.get_channel(int(channel_id))
        if not channel:
            continue
        for idx, rank in enumerate(STAFF_ROLES):
            if idx > 0:
                await asyncio.sleep(1.0)
            try:
                await _post_or_edit_board_message(
                    bot,
                    channel,
                    rank,
                    create_missing=False,
                )
            except Exception as e:
                error_log(f"Failed to refresh staff board for {rank}: {e}")
        return  # Only one server's board


async def refresh_staff_board_for_rank(bot: Bot, rank_name: str) -> None:
    """Edit just one rank's board message. Approving a staff app only changes
    that rank's holder count, so refreshing all 11 (with the 1s spacing) adds
    ~10s of latency to the approve button for no benefit."""
    channel_id = bot.db.get_config("staff_board_channel_id")
    if not channel_id:
        return
    for discord_guild in bot.guilds:
        channel = discord_guild.get_channel(int(channel_id))
        if not channel:
            continue
        try:
            await _post_or_edit_board_message(
                bot,
                channel,
                rank_name,
                create_missing=False,
            )
        except Exception as e:
            error_log(f"Failed to refresh staff board for {rank_name}: {e}")
        return  # Only one server's board


def register_persistent_staff_views(bot: Bot) -> None:
    """Re-register the Apply button views so they keep working after a restart."""
    for rank in STAFF_ROLES:
        bot.add_view(StaffApplyView.for_rank(bot, rank))
    # Approve/Deny buttons on officer notifications survive restarts because
    # they're registered as DynamicItems — discord.py matches the stored
    # custom_id (``staff_review:<verb>:<app_id>``) against the templates and
    # reconstructs the button class on click.
    bot.add_dynamic_items(ApproveAppButton, DenyAppButton)


# ── Shared approve/deny logic ───────────────────────────────────────────────
# Used by both the slash commands (/staff approve, /staff deny) and the
# persistent review buttons attached to officer-channel notifications.
# Returns ``(ok, user_message)`` so callers can surface the result either as
# an ephemeral interaction response or post-hoc edit.

async def _do_approve(
    bot: Bot,
    guild: discord.Guild,
    reviewer: discord.Member,
    app_id: int,
) -> tuple[bool, str]:
    if not _is_reviewer(reviewer):
        return False, "Only Captains and Officers can approve."
    app = bot.db.fetch_staff_application(app_id)
    if not app:
        return False, f"No application #{app_id}."
    if app["status"] != "pending":
        return False, f"Application #{app_id} is **{app['status']}**, not pending."

    rank_name = app["rank"]
    guild_size = bot.db.count_registered_members()
    slots = _slot_count(bot.db, rank_name, guild_size)
    current = len(_holders_in_guild(guild, rank_name))
    if current >= slots:
        return False, (
            f"**{rank_name}** is full ({current}/{slots}). "
            "Demote someone or wait for the guild to grow."
        )

    member = guild.get_member(int(app["discord_id"]))
    if not member:
        bot.db.update_application_status(app_id, "denied", str(reviewer.id), "Applicant left server")
        await _mark_review_message_resolved(bot, guild, app_id, "denied")
        return False, "Applicant is no longer in the server (auto-denied)."

    role = discord.utils.get(guild.roles, name=rank_name)
    if not role:
        return False, f"Role **{rank_name}** does not exist. Run /admin setup-roles."

    try:
        await member.add_roles(role, reason=f"Staff app #{app_id} approved by {reviewer}")
    except discord.Forbidden:
        return False, f"Missing permission to assign **{rank_name}**."

    bot.db.record_staff_grant(str(member.id), rank_name)
    bot.db.update_application_status(app_id, "approved", str(reviewer.id), None)
    await _mark_review_message_resolved(bot, guild, app_id, "approved")
    try:
        await member.send(
            f"Your application for **{rank_name}** was approved. Welcome to the team."
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    info_log(f"{reviewer} approved app #{app_id} ({rank_name}) for {member}.")
    await refresh_staff_board_for_rank(bot, rank_name)
    return True, (
        f"Approved **#{app_id}**. {member.mention} is now **{rank_name}** "
        f"({current + 1}/{slots})."
    )


async def _do_deny(
    bot: Bot,
    guild: discord.Guild,
    reviewer: discord.Member,
    app_id: int,
    reason: str,
) -> tuple[bool, str]:
    if not _is_reviewer(reviewer):
        return False, "Only Captains and Officers can deny."
    if len(reason) < 10:
        return False, "Reason must be at least 10 characters."
    app = bot.db.fetch_staff_application(app_id)
    if not app:
        return False, f"No application #{app_id}."
    if app["status"] != "pending":
        return False, f"Application #{app_id} is **{app['status']}**, not pending."

    bot.db.update_application_status(app_id, "denied", str(reviewer.id), reason)
    await _mark_review_message_resolved(bot, guild, app_id, "denied")
    member = guild.get_member(int(app["discord_id"]))
    if member:
        try:
            await member.send(
                f"Your application for **{app['rank']}** was denied.\nReason: {reason}"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    info_log(f"{reviewer} denied app #{app_id} ({app['rank']}): {reason}")
    return True, f"Denied **#{app_id}** ({app['rank']})."


# ── Persistent review buttons ───────────────────────────────────────────────
# Implemented as ``discord.ui.DynamicItem`` so the per-application id encoded
# in the custom_id can be parsed back out on click — even after a bot
# restart. ``register_persistent_staff_views`` calls ``bot.add_dynamic_items``
# at startup so discord.py routes any stored ``staff_review:*:<id>`` button
# click to these classes.

_APPROVE_TEMPLATE = r"staff_review:approve:(?P<app_id>[0-9]+)"
_DENY_TEMPLATE = r"staff_review:deny:(?P<app_id>[0-9]+)"


class StaffDenyModal(discord.ui.Modal, title="Deny application"):
    def __init__(self, app_id: int) -> None:
        super().__init__(timeout=None)
        self.app_id = app_id
        self.reason = discord.ui.TextInput(
            label="Reason (shown to applicant)",
            placeholder="Be specific — they'll see this verbatim.",
            style=discord.TextStyle.paragraph,
            min_length=10,
            max_length=400,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await _do_deny(
            interaction.client, interaction.guild, interaction.user,
            self.app_id, str(self.reason.value),
        )
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            try:
                await interaction.message.edit(view=_resolved_view("denied"))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class ApproveAppButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_APPROVE_TEMPLATE,
):
    def __init__(self, app_id: int) -> None:
        self.app_id = app_id
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"staff_review:approve:{app_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "ApproveAppButton":
        return cls(int(match["app_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # Approve does role-assignment + board refresh, which can blow past
        # the 3-second interaction deadline on slower hosts. Defer first.
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await _do_approve(
            interaction.client, interaction.guild, interaction.user, self.app_id,
        )
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            try:
                await interaction.message.edit(view=_resolved_view("approved"))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class DenyAppButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=_DENY_TEMPLATE,
):
    def __init__(self, app_id: int) -> None:
        self.app_id = app_id
        super().__init__(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=f"staff_review:deny:{app_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "DenyAppButton":
        return cls(int(match["app_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains and Officers can deny applications."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(StaffDenyModal(self.app_id))


def build_review_view(app_id: int) -> discord.ui.View:
    """Build the Approve/Deny view attached to a fresh notification."""
    view = discord.ui.View(timeout=None)
    view.add_item(ApproveAppButton(app_id))
    view.add_item(DenyAppButton(app_id))
    return view


def _resolved_view(status: str) -> discord.ui.View:
    """Return a disabled-buttons view used to lock a resolved notification."""
    view = discord.ui.View(timeout=None)
    label = {
        "approved": "Approved ✅",
        "withdrawn": "Withdrawn ↩",
    }.get(status, "Denied ❌")
    btn = discord.ui.Button(
        label=label,
        style=discord.ButtonStyle.secondary,
        disabled=True,
        custom_id=f"staff_review:resolved:{status}",
    )
    view.add_item(btn)
    return view


def backfill_staff_grants(bot: Bot) -> None:
    """For every current staff role holder without a grant record, insert one
    with granted_at = NOW. This gives existing officers a tenure clock starting
    today rather than pretending they have unlimited service."""
    inserted = 0
    for discord_guild in bot.guilds:
        for rank in STAFF_ROLES:
            role = discord.utils.get(discord_guild.roles, name=rank)
            if not role:
                continue
            for member in role.members:
                if not bot.db.fetch_first_grant_date(str(member.id), rank):
                    bot.db.record_staff_grant(str(member.id), rank)
                    inserted += 1
    if inserted:
        info_log(f"Backfilled {inserted} staff role grant(s) at startup.")


# ── cog ──────────────────────────────────────────────────────────────────────

class StaffGroup(app_commands.Group, name="staff", description="Staff applications and rank management."):

    def __init__(self, bot: Bot):
        super().__init__()
        self.bot = bot

    # ── apply ───────────────────────────────────────────────────────────────

    @app_commands.command(name="apply", description="Apply for a staff rank.")
    @app_commands.describe(rank="The rank you want to apply for", reason="Why you'd be a good fit (be specific)")
    @app_commands.choices(rank=_rank_choices())
    async def apply(self, interaction: discord.Interaction, rank: app_commands.Choice[str], reason: str) -> None:
        await _process_application(self.bot, interaction, rank.value, reason)

    # ── withdraw ────────────────────────────────────────────────────────────

    @app_commands.command(name="withdraw", description="Withdraw a pending application of yours.")
    @app_commands.choices(rank=_rank_choices())
    async def withdraw(self, interaction: discord.Interaction, rank: app_commands.Choice[str]) -> None:
        pending = self.bot.db.fetch_pending_application(str(interaction.user.id), rank.value)
        if not pending:
            await interaction.response.send_message(
                embed=info_embed("Nothing to withdraw", f"You have no pending application for **{rank.value}**."),
                ephemeral=True,
            )
            return
        self.bot.db.update_application_status(pending["id"], "withdrawn", str(interaction.user.id), "Withdrawn by applicant")
        await _mark_review_message_resolved(
            self.bot,
            interaction.guild,
            int(pending["id"]),
            "withdrawn",
        )
        await interaction.response.send_message(
            embed=success_embed("Application withdrawn", f"Withdrew application **#{pending['id']}** for **{rank.value}**."),
            ephemeral=True,
        )

    # ── slots ───────────────────────────────────────────────────────────────

    @app_commands.command(name="slots", description="Show current capacity and holders for each staff rank.")
    async def slots(self, interaction: discord.Interaction) -> None:
        guild_size = self.bot.db.count_registered_members()
        embed = discord.Embed(
            title="Staff slots",
            description=f"Guild size: **{guild_size}** registered members",
            color=discord.Color.gold()
        )
        for rank in STAFF_ROLES:
            tier = _tier_settings(self.bot.db, rank)
            slots = _slot_count(self.bot.db, rank, guild_size)
            held = len(_holders_in_guild(interaction.guild, rank))
            eligible = ", ".join(tier["eligible"])
            embed.add_field(
                name=rank,
                value=(f"`{held}/{slots}` filled\n"
                       f"1 per {tier['per_slot']} members (cap {tier['max_cap']})\n"
                       f"Eligible: {eligible}"),
                inline=True
            )
        await interaction.response.send_message(embed=embed)

    # ── applications (officer) ──────────────────────────────────────────────

    @app_commands.command(name="applications", description="List pending staff applications.")
    @app_commands.describe(rank="Filter by rank (optional)")
    @app_commands.choices(rank=_rank_choices())
    @app_commands.default_permissions(manage_guild=True)
    async def applications(self, interaction: discord.Interaction, rank: "Optional[app_commands.Choice[str]]" = None) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains and Officers can review applications."),
                ephemeral=True,
            )
            return
        pending = self.bot.db.fetch_pending_applications(rank.value if rank else None)
        if not pending:
            await interaction.response.send_message(
                embed=info_embed("All caught up", "No pending applications."),
                ephemeral=True,
            )
            return
        embed = discord.Embed(title=f"Pending applications ({len(pending)})", color=discord.Color.blue())
        for app in pending[:25]:
            applicant = interaction.guild.get_member(int(app["discord_id"]))
            who = applicant.mention if applicant else f"<@{app['discord_id']}>"
            reason = (app["reason"] or "")[:200]
            embed.add_field(
                name=f"#{app['id']} — {app['rank']}",
                value=f"{who}\n*{reason}*\nApplied: {app['applied_at']}",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── repost-pending (officer) ────────────────────────────────────────────

    @app_commands.command(
        name="repost-pending",
        description="Re-send all pending applications to the officer channel with Approve/Deny buttons.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def repost_pending(self, interaction: discord.Interaction) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains and Officers can repost pending applications."),
                ephemeral=True,
            )
            return
        officer_channel_id = self.bot.db.get_config("officer_channel_id")
        if not officer_channel_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Officer channel not configured",
                    "No officer channel is set.",
                    hint="Run `/set-officer-channel` first.",
                ),
                ephemeral=True,
            )
            return
        channel = interaction.guild.get_channel(int(officer_channel_id))
        if not channel:
            await interaction.response.send_message(
                embed=error_embed("Officer channel missing", "The configured officer channel was not found in this guild."),
                ephemeral=True,
            )
            return

        pending = self.bot.db.fetch_pending_applications(None)
        if not pending:
            await interaction.response.send_message(
                embed=info_embed("All caught up", "No pending applications."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = 0
        updated = 0
        failed = 0
        for app in pending:
            try:
                result = await _post_or_update_review_message(
                    self.bot,
                    interaction.guild,
                    channel,
                    app,
                )
                if result == "updated":
                    updated += 1
                else:
                    posted += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        msg = (
            f"Pending application posts in {channel.mention}: "
            f"**{updated}** updated, **{posted}** newly posted."
        )
        if failed:
            msg += f" {failed} failed (check bot permissions)."
        await interaction.followup.send(msg, ephemeral=True)
        info_log(
            f"{interaction.user} refreshed pending applications in #{channel.name}: "
            f"{updated} updated, {posted} posted, {failed} failed."
        )

    # ── approve / deny (officer) ────────────────────────────────────────────

    @app_commands.command(name="approve", description="Approve a staff application.")
    @app_commands.describe(app_id="The application ID")
    @app_commands.default_permissions(manage_guild=True)
    async def approve(self, interaction: discord.Interaction, app_id: app_commands.Range[int, 1]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await _do_approve(
            self.bot, interaction.guild, interaction.user, int(app_id),
        )
        await interaction.followup.send(msg, ephemeral=not ok)

    @app_commands.command(name="deny", description="Deny a staff application.")
    @app_commands.describe(app_id="The application ID", reason="Reason shown to the applicant (min 10 chars)")
    @app_commands.default_permissions(manage_guild=True)
    async def deny(
        self,
        interaction: discord.Interaction,
        app_id: app_commands.Range[int, 1],
        reason: app_commands.Range[str, 10, 400],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await _do_deny(
            self.bot, interaction.guild, interaction.user, int(app_id), str(reason),
        )
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            await refresh_staff_board(self.bot)

    # ── config (officer) ────────────────────────────────────────────────────

    @app_commands.command(name="config", description="Configure slot scaling for a staff rank.")
    @app_commands.describe(
        rank="The rank to configure",
        per_slot="Members required per 1 slot (1–1000)",
        max_cap="Hard cap on slot count regardless of guild size (0–100)",
    )
    @app_commands.choices(rank=_rank_choices())
    @app_commands.default_permissions(manage_guild=True)
    async def config(
        self,
        interaction: discord.Interaction,
        rank: app_commands.Choice[str],
        per_slot: app_commands.Range[int, 1, 1000],
        max_cap: app_commands.Range[int, 0, 100],
    ) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains and Officers can change staff config."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(f"staff_{rank.value}_per_slot", str(per_slot))
        self.bot.db.set_config(f"staff_{rank.value}_max", str(max_cap))
        guild_size = self.bot.db.count_registered_members()
        slots = _slot_count(self.bot.db, rank.value, guild_size)
        await interaction.response.send_message(
            f"**{rank.value}**: 1 per {per_slot} members, cap {max_cap} → currently {slots} slot(s) at guild size {guild_size}.",
            ephemeral=True
        )

    # ── tenure overrides (officer) ──────────────────────────────────────────

    @app_commands.command(name="record-grant", description="Manually set a staff member's tenure start date for a rank.")
    @app_commands.describe(
        member="The member to credit",
        rank="The staff rank to record tenure for",
        days_ago="How many days ago they earned this rank (0 = today, max 3650).",
    )
    @app_commands.choices(rank=_rank_choices())
    @app_commands.default_permissions(manage_guild=True)
    async def record_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        rank: app_commands.Choice[str],
        days_ago: app_commands.Range[int, 0, 3650],
    ) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains and Officers can edit tenure."),
                ephemeral=True,
            )
            return
        granted_dt = datetime.datetime.utcnow() - datetime.timedelta(days=days_ago)
        # Overwrite any existing record so officers can correct a backfilled date.
        self.bot.db.execute(
            'DELETE FROM staff_role_grants WHERE discord_id = ? AND rank = ?',
            (str(member.id), rank.value)
        )
        self.bot.db.execute(
            'INSERT INTO staff_role_grants (discord_id, rank, granted_at) VALUES (?, ?, ?)',
            (str(member.id), rank.value, granted_dt.isoformat(timespec="seconds"))
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Tenure backfilled",
                f"{member.mention}'s **{rank.value}** tenure now starts {days_ago} day(s) ago "
                f"({granted_dt.date().isoformat()}).",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set {member} {rank.value} tenure to {days_ago}d ago.")

    @app_commands.command(name="tenure", description="Show how long a member has held a staff rank.")
    @app_commands.describe(member="The staff member", rank="The staff rank")
    @app_commands.choices(rank=_rank_choices())
    async def tenure(self, interaction: discord.Interaction, member: discord.Member,
                     rank: app_commands.Choice[str]) -> None:
        granted_at = self.bot.db.fetch_first_grant_date(str(member.id), rank.value)
        if not granted_at:
            await interaction.response.send_message(
                embed=info_embed("No tenure record", f"No tenure record for {member.mention} as **{rank.value}**."),
                ephemeral=True,
            )
            return
        try:
            granted_dt = datetime.datetime.fromisoformat(granted_at)
            days = (datetime.datetime.utcnow() - granted_dt).days
        except (TypeError, ValueError):
            days = 0
        await interaction.response.send_message(
            embed=info_embed(
                f"{rank.value} tenure",
                f"{member.mention} has held **{rank.value}** for **{days} days** (since {granted_at[:10]}).",
            ),
            ephemeral=True,
        )

    # ── manual rebalance (officer) ──────────────────────────────────────────

    @app_commands.command(name="rebalance-toggle", description="Enable or disable automatic hourly staff rebalancing.")
    @app_commands.describe(enabled="True to enable demotions of least-active over-capacity holders")
    @app_commands.default_permissions(manage_guild=True)
    async def rebalance_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains and Officers can toggle rebalancing."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config("staff_rebalance_enabled", "1" if enabled else "0")
        state = "ENABLED" if enabled else "DISABLED"
        detail = (
            "Over-capacity ranks will demote least-active holders on the hourly sync."
            if enabled else "No automatic demotions will occur."
        )
        await interaction.response.send_message(
            embed=success_embed(f"Rebalancing {state}", detail),
            ephemeral=True,
        )

    @app_commands.command(name="rebalance", description="Manually trigger a staff rebalance pass.")
    @app_commands.default_permissions(manage_guild=True)
    async def rebalance_cmd(self, interaction: discord.Interaction) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains and Officers can rebalance."),
                ephemeral=True,
            )
            return
        confirmed = await confirm_action(
            interaction,
            title="Run staff rebalance now?",
            description=(
                "This may **demote the least-active holders** of over-capacity ranks based on "
                "current activity data. The action is logged but cannot be auto-undone."
            ),
            confirm_label="Run rebalance",
        )
        if not confirmed:
            return
        # Manual rebalance bypasses the auto-toggle: temporarily flip it on for this run.
        prior = self.bot.db.get_config("staff_rebalance_enabled")
        self.bot.db.set_config("staff_rebalance_enabled", "1")
        try:
            await rebalance_staff(self.bot)
        finally:
            self.bot.db.set_config("staff_rebalance_enabled", prior or "0")
        await refresh_staff_board(self.bot)
        await interaction.followup.send(
            embed=success_embed("Rebalance complete", "Check the audit log for any demotions."),
            ephemeral=True,
        )

    # ── staff board (officer) ───────────────────────────────────────

    @app_commands.command(name="setup-board", description="Post (or move) the staff application board to a channel.")
    @app_commands.describe(channel="Channel where the board should live (defaults to here)")
    @app_commands.default_permissions(manage_guild=True)
    async def setup_board(self, interaction: discord.Interaction, channel: "Optional[discord.TextChannel]" = None) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains and Officers can set up the board."),
                ephemeral=True,
            )
            return
        dest = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)

        # Permission preflight so we fail loudly instead of silently
        me = dest.guild.me
        perms = dest.permissions_for(me)
        missing = [p for p, ok in (
            ("View Channel", perms.view_channel),
            ("Send Messages", perms.send_messages),
            ("Embed Links", perms.embed_links),
            ("Read Message History", perms.read_message_history),
        ) if not ok]
        if missing:
            await interaction.followup.send(
                embed=error_embed(
                    "Missing permissions",
                    f"Bot is missing **{', '.join(missing)}** in {dest.mention}.",
                    hint="Grant these to the bot role and re-run the command.",
                ),
                ephemeral=True,
            )
            return

        # Always wipe existing board messages and repost in STAFF_ROLES order,
        # so re-running setup-board (e.g. after adding new ranks) produces a
        # clean top-down board instead of leaving old messages in their old
        # positions with new ranks appended at the bottom.
        for rank in STAFF_ROLES:
            old_msg_id = self.bot.db.get_config(f"staff_board_msg_{rank}")
            if old_msg_id:
                try:
                    old_msg = await dest.fetch_message(int(old_msg_id))
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                except (TypeError, ValueError):
                    pass
            self.bot.db.set_config(f"staff_board_msg_{rank}", "")
        self.bot.db.set_config("staff_board_channel_id", str(dest.id))

        # Post directly here (not via refresh_staff_board) so we can surface errors
        errors = []
        for rank in STAFF_ROLES:
            try:
                await _post_or_edit_board_message(self.bot, dest, rank)
            except Exception as e:
                errors.append(f"{rank}: {e}")
                error_log(f"setup-board failed for {rank}: {e}")

        if errors:
            await interaction.followup.send(
                embed=warning_embed(
                    f"Posted with errors in {dest.name}",
                    "\n".join(f"• {e}" for e in errors),
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=success_embed(
                    "Staff board posted",
                    f"Posted in {dest.mention}. It will auto-refresh every hour.",
                ),
                ephemeral=True,
            )

    @app_commands.command(name="refresh-board", description="Manually refresh the staff application board.")
    @app_commands.default_permissions(manage_guild=True)
    async def refresh_board_cmd(self, interaction: discord.Interaction) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains and Officers can refresh the board."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        await refresh_staff_board(self.bot)
        await interaction.followup.send(
            embed=success_embed("Staff board refreshed", "Latest applications and counts are now live."),
            ephemeral=True,
        )


class Staff(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:
        # Reload-safety: drop the manually-added /staff group registered by setup().
        try:
            self.bot.tree.remove_command("staff")
        except Exception:  # noqa: BLE001
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready fires on every reconnect; only do startup work once per process.
        if getattr(self, "_ready_done", False):
            return
        self._ready_done = True
        # Re-register the persistent Apply views so existing board buttons keep working
        register_persistent_staff_views(self.bot)
        # Backfill tenure records for current staff who pre-date the tracking system
        backfill_staff_grants(self.bot)


async def setup(bot: Bot):
    await bot.add_cog(Staff(bot))
    bot.tree.add_command(StaffGroup(bot))
