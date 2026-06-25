"""Discord UI views/modal used by the LFG cog.

* ``CreateEventModal`` — opened when an officer clicks a slot button on
  the event board.
* ``EventSignupView`` — Sign Up / Withdraw buttons attached to every
  posted event message.
* ``EventBoardView`` + the two Button subclasses + ``_board_embed`` —
  the persistent control-panel that lives in the events channel.

Sibling module — leading underscore keeps the cog auto-loader from
loading this as a cog extension.
"""
from __future__ import annotations

import contextlib
import datetime
import re

import discord

from cogs._lfg_config import (
    CANCEL_OVERRIDE_ROLES,
    CFG_ROLE_PREFIX,
    EVENT_TYPES,
    EVENT_TYPES_BY_KEY,
    PREP_MINUTES,
    PRIME_SLOTS,
    REVIEW_MINUTES,
    PrimeSlot,
    display_slot_label,
    prime_slot_window_on_date,
    prime_slot_display_label,
)
from cogs._lfg_helpers import (
    _create_discord_scheduled_event,
    _create_lfg_discussion_thread,
    _format_build_briefing,
    _format_event_embed,
    _grant_event_access_role,
    _get_ping_for_type,
    _get_post_channel_for_type,
    _normalize_ip_requirement,
    _next_occurrence,
    _revoke_event_access_role_if_unneeded,
    _slot_occurrence_on_date,
    _user_can_make_prime,
)
from cogs._typing import Bot
from debug import error_log, info_log, warning_log
from utils import error_embed, info_embed, is_officer, success_embed


def _on_first_event_signup(bot, db, *, discord_id: str, event: dict) -> None:
    """Run cross-cog hooks the first time a member signs up for any event.

    Two things happen, both best-effort:

    1. Recruitment funnel: if this discord_id maps to a recruit row not yet
       at ``first_event`` stage, advance it (and stamp ``first_event_at``).
    2. Auto-promote: if the member is currently Probationary, bump them to
       Recruit so an officer doesn't have to do it manually.

    Failures only log — signing up must always succeed for the user.
    """
    # 1) Recruitment funnel advance
    try:
        profile = db.fetch_user_profile(discord_id) or {}
        albion = (profile.get("albion_name") or "").strip()
        if albion:
            recruit = db.recruit_find_by_name(albion)
            if recruit:
                stage_order = ["contacted", "discord", "registered", "first_event"]
                cur = recruit.get("status") or "contacted"
                try:
                    if stage_order.index(cur) < stage_order.index("first_event"):
                        db.recruit_update(
                            int(recruit["id"]),
                            status="first_event",
                            discord_id=discord_id,
                        )
                        info_log(
                            f"recruit #{recruit['id']} ({albion}) advanced to "
                            f"first_event via LFG signup."
                        )
                except ValueError:
                    pass
    except Exception as exc:  # noqa: BLE001
        error_log(f"first-event recruit advance failed: {exc!r}")

    # 2) Lifecycle auto-promote
    try:
        profile = profile if "profile" in locals() else (
            db.fetch_user_profile(discord_id) or {}
        )
        if (profile.get("lifecycle_role") or "").strip() == "Probationary":
            db.set_lifecycle_role(discord_id, "Recruit")
            info_log(
                f"Auto-promoted {discord_id} Probationary → Recruit "
                f"on first event signup (#{event.get('id')})."
            )
            # Try to flip the Discord role too, if we can find the member.
            for guild in bot.guilds:
                m = guild.get_member(int(discord_id))
                if not m:
                    continue
                try:
                    prob_role = discord.utils.get(guild.roles, name="Probationary")
                    recr_role = discord.utils.get(guild.roles, name="Recruit")
                    coros = []
                    if prob_role and prob_role in m.roles:
                        coros.append(m.remove_roles(prob_role, reason="First event signup"))
                    if recr_role and recr_role not in m.roles:
                        coros.append(m.add_roles(recr_role, reason="First event signup"))
                    if coros:
                        import asyncio
                        loop = asyncio.get_event_loop()
                        for c in coros:
                            loop.create_task(c)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    warning_log(f"role swap failed for {m}: {exc!r}")
                break
    except Exception as exc:  # noqa: BLE001
        error_log(f"first-event auto-promote failed: {exc!r}")


async def _refresh_prime_claim_dashboards(
    bot: Bot,
    event: dict | None,
    reason: str,
    *,
    force: bool = False,
) -> None:
    """Best-effort refresh for live prime claim dashboards after LFG changes."""
    if not event:
        return
    if not force:
        try:
            if int(event.get("is_prime") or 0) != 1:
                return
        except (TypeError, ValueError):
            return

    try:
        from cogs._primetime_claims import refresh_prime_claim_trackers

        updated = await refresh_prime_claim_trackers(bot)
        if updated:
            info_log(
                f"Refreshed {updated} prime claim dashboard(s) after LFG {reason}."
            )
    except Exception as exc:  # noqa: BLE001
        warning_log(f"prime claim dashboard refresh failed after LFG {reason}: {exc!r}")


def _can_manage_lfg_event(member: discord.abc.User, event: dict) -> bool:
    if str(member.id) == str(event.get("creator_id")):
        return True
    if not isinstance(member, discord.Member):
        return False
    return any(r.name in CANCEL_OVERRIDE_ROLES for r in member.roles)


def _parse_general_lfg_schedule(raw: str) -> tuple[datetime.datetime, datetime.datetime]:
    """Parse ``YYYY-MM-DD HH:MM, 60m`` into aware UTC start/end datetimes."""
    text = re.sub(r"\s+", " ", str(raw or "").strip())
    match = re.match(
        r"^(?P<start>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})"
        r"(?:\s*(?:,|for)?\s*)"
        r"(?P<duration>\d{1,4})"
        r"(?:\s*(?:m|min|mins|minute|minutes))?\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("expected `YYYY-MM-DD HH:MM, 60m`")

    starts_at = datetime.datetime.strptime(
        match.group("start"), "%Y-%m-%d %H:%M",
    ).replace(tzinfo=datetime.timezone.utc)
    duration = int(match.group("duration"))
    if duration <= 0 or duration > 24 * 60:
        raise ValueError("duration out of range")
    return starts_at, starts_at + datetime.timedelta(minutes=duration)


def _exact_prime_slot_for_window(
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
) -> PrimeSlot | None:
    """Return the prime slot when an event exactly matches one UTC timer."""
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=datetime.timezone.utc)
    else:
        starts_at = starts_at.astimezone(datetime.timezone.utc)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=datetime.timezone.utc)
    else:
        ends_at = ends_at.astimezone(datetime.timezone.utc)

    for slot in PRIME_SLOTS:
        slot_start, slot_end = prime_slot_window_on_date(starts_at.date(), slot)
        if starts_at == slot_start and ends_at == slot_end:
            return slot
    return None


def _claim_fields_for_schedule(
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
) -> dict[str, str | int]:
    """DB fields that keep the claim board aligned with an event schedule."""
    slot = _exact_prime_slot_for_window(starts_at, ends_at)
    if slot is not None:
        return {"slot_label": f"PRIME {slot.label}", "is_prime": 1}
    return {"slot_label": "GENERAL", "is_prime": 0}


def _overlapping_prime_events(
    db,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    *,
    exclude_event_id: int | None = None,
) -> list[dict]:
    rows = db.fetch_overlapping_prime_events(
        starts_at.isoformat(),
        ends_at.isoformat(),
    )
    if exclude_event_id is None:
        return rows
    return [r for r in rows if int(r.get("id") or 0) != int(exclude_event_id)]


# ── Modal ───────────────────────────────────────────────────────────────────
class CreateEventModal(discord.ui.Modal):
    """Filled in by the event creator after they click a slot button."""

    title_input = discord.ui.TextInput(
        label="Event title",
        placeholder="e.g. ZvZ in Arthur's Rest",
        max_length=100,
        required=True,
    )
    description_input = discord.ui.TextInput(
        label="Description",
        placeholder="What's the plan, meeting spot, expectations…",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
    )
    comp_input = discord.ui.TextInput(
        label="Comp / build requirements",
        placeholder="e.g. 1H/Shield + Holy + DPS, swaps, caller notes",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )
    ip_input = discord.ui.TextInput(
        label="Minimum IP",
        placeholder="e.g. 1500 or 1500 IP",
        max_length=12,
        required=False,
    )

    def __init__(self, bot: Bot, slot: PrimeSlot | None, event_type: str | None = None):
        # slot=None means General LFG (free-form date/time)
        self.bot: Bot = bot
        self.slot = slot
        # event_type is picked on the screen before this modal opens; used
        # to ping the matching content role when the event posts and to
        # stamp the lfg_events.event_type column for analytics.
        self.event_type: str | None = event_type
        super().__init__(
            title=(f"New event — {prime_slot_display_label(slot)}" if slot else "New General LFG"),
            timeout=600,
        )
        if slot is not None:
            next_start, _ = _next_occurrence(slot)
            self.date_input = discord.ui.TextInput(
                label="UTC/Albion date — YYYY-MM-DD",
                default=next_start.strftime("%Y-%m-%d"),
                placeholder="Use the UTC date, even if your local date differs.",
                max_length=10,
                required=True,
            )
            self.add_item(self.date_input)
        else:
            # General LFG needs a start and duration. Keep them together so
            # the modal still fits Discord's 5-input limit with a separate IP
            # field.
            self.schedule_input = discord.ui.TextInput(
                label="Start + duration UTC",
                placeholder=(
                    datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
                    + ", 60m"
                ),
                max_length=32,
                required=True,
            )
            self.add_item(self.schedule_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Resolve start/end
        if self.slot is not None:
            try:
                claim_date = datetime.datetime.strptime(
                    str(self.date_input.value).strip(), "%Y-%m-%d"
                ).date()
            except ValueError as exc:
                await interaction.followup.send(
                    embed=error_embed(
                        "Couldn't parse your prime date",
                        f"`{exc}`. Use the Albion/UTC date format `YYYY-MM-DD`, "
                        "not your local calendar date if they differ.",
                    ),
                    ephemeral=True,
                )
                return
            starts_at, ends_at = _slot_occurrence_on_date(self.slot, claim_date)
            slot_label = f"PRIME {self.slot.label}"
            is_prime = True
            now = datetime.datetime.now(datetime.timezone.utc)
            if starts_at < now - datetime.timedelta(minutes=5):
                await interaction.followup.send(
                    embed=error_embed(
                        "Prime slot is in the past",
                        f"`{starts_at.strftime('%Y-%m-%d %H:%M')} UTC` has already passed. "
                        "Pick today only if that timer has not started yet, or choose a future date.",
                    ),
                    ephemeral=True,
                )
                return
        else:
            try:
                starts_at, ends_at = _parse_general_lfg_schedule(
                    str(self.schedule_input.value).strip()
                )
            except ValueError as exc:
                await interaction.followup.send(
                    embed=error_embed(
                        "Couldn't parse your start time / duration",
                        f"`{exc}`. Use UTC format like `YYYY-MM-DD HH:MM, 60m`.",
                    ),
                    ephemeral=True,
                )
                return
            claim_fields = _claim_fields_for_schedule(starts_at, ends_at)
            slot_label = str(claim_fields["slot_label"])
            is_prime = bool(claim_fields["is_prime"])

            if is_prime and (
                not isinstance(interaction.user, discord.Member)
                or not _user_can_make_prime(interaction.user)
            ):
                await interaction.followup.send(
                    embed=error_embed(
                        "Shotcaller+ only",
                        "That custom time exactly matches a prime timer. "
                        "Prime-time claims are reserved for Shotcaller, "
                        "Senior Shotcaller, Officer, or Captain.",
                    ),
                    ephemeral=True,
                )
                return

            # Reject events scheduled in the past. We allow a small grace
            # window (5 min) for clock skew / user typing latency, but
            # anything older than that is almost certainly a mistake.
            now = datetime.datetime.now(datetime.timezone.utc)
            if starts_at < now - datetime.timedelta(minutes=5):
                await interaction.followup.send(
                    embed=error_embed(
                        "Start time is in the past",
                        f"Your start time `{starts_at.strftime('%Y-%m-%d %H:%M')}` is "
                        "before the current time (UTC). Pick a future time.",
                    ),
                    ephemeral=True,
                )
                return

            # Non-prime custom LFG must not stomp on a booked timer claim.
            if not is_prime:
                overlap = _overlapping_prime_events(
                    interaction.client.db,
                    starts_at,
                    ends_at,
                )
                if overlap:
                    names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
                    await interaction.followup.send(
                        embed=error_embed(
                            "Conflicts with prime-time event(s)",
                            f"Your General LFG would overlap: {names}. "
                            "Pick a different time or coordinate with the prime caller.",
                        ),
                        ephemeral=True,
                    )
                    return

        # Prime-time double-booking guard
        if is_prime:
            overlap = _overlapping_prime_events(
                interaction.client.db,
                starts_at,
                ends_at,
            )
            if overlap:
                names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
                await interaction.followup.send(
                    embed=error_embed(
                        "Slot already booked",
                        f"This prime-time slot already has: {names}. Cancel it first or pick another slot.",
                    ),
                    ephemeral=True,
                )
                return

        # Validate the post channel BEFORE we write a DB row, so a misconfigured
        # channel can't leave us with an orphan event record that was never
        # actually posted anywhere.
        db = interaction.client.db
        channel = _get_post_channel_for_type(db, interaction.guild, self.event_type)
        if channel is None:
            type_label = (
                EVENT_TYPES_BY_KEY[self.event_type].label
                if self.event_type and self.event_type in EVENT_TYPES_BY_KEY
                else None
            )
            await interaction.followup.send(
                embed=error_embed(
                    "LFG channel not configured",
                    "An admin needs to set the post channel before events can be created."
                    + (f"\n\nSelected type: **{type_label}**" if type_label else "")
                    + "\n\n"
                    "• Quick fix: `/lfg auto-config` (auto-detects from your guild)\n"
                    "• Default: `/lfg set-post-channel #channel`\n"
                    "• Per type: `/lfg set-type-channel`",
                ),
                ephemeral=True,
            )
            return

        raw_ip_requirement = str(self.ip_input.value).strip()
        ip_requirement = _normalize_ip_requirement(
            raw_ip_requirement,
            allow_bare=True,
        )
        if raw_ip_requirement and not ip_requirement:
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't parse minimum IP",
                    "Use a plain IP value like `1500` or `1500 IP`, or leave it blank.",
                ),
                ephemeral=True,
            )
            return

        # Create DB row
        event_id = db.create_lfg_event(
            slot_label=slot_label,
            is_prime=is_prime,
            title=str(self.title_input.value).strip(),
            description=str(self.description_input.value).strip(),
            comp_notes=str(self.comp_input.value).strip(),
            ip_requirement=ip_requirement,
            starts_at=starts_at.isoformat(),
            ends_at=ends_at.isoformat(),
            prep_minutes=PREP_MINUTES,
            review_minutes=REVIEW_MINUTES,
            creator_id=str(interaction.user.id),
            event_type=self.event_type,
        )
        if not event_id:
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't save the event",
                    "The database write failed and your event was **not** posted. "
                    "Try again — if it keeps failing, ask an admin to check the bot logs.",
                ),
                ephemeral=True,
            )
            return

        event = db.fetch_lfg_event(event_id)
        # Build the post content. If the officer picked a content type and
        # that type has a ping role mapped via /lfg set-type-role, mention it so
        # subscribers actually get notified — that's the whole point of
        # per-content roles.
        ping = _get_ping_for_type(db, self.event_type)
        try:
            msg = await channel.send(
                content=ping or None,
                embed=_format_event_embed(db, event),
                view=EventSignupView(event_id),
                allowed_mentions=discord.AllowedMentions(
                    roles=True, users=False, everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Roll back the DB row so we don't accumulate orphans on a
            # transient permission/network failure.
            with contextlib.suppress(Exception):
                db.delete_lfg_event(event_id)
            error_log(f"LFG post failed for event #{event_id}: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't post the event",
                    f"Discord rejected the post: `{exc}`. Your event was rolled back.",
                ),
                ephemeral=True,
            )
            return
        db.set_lfg_message(event_id, str(channel.id), str(msg.id))
        await _create_lfg_discussion_thread(db, event, msg)

        # Also create a native Discord scheduled event so it appears in the
        # server's in-client event tracker. Best-effort: if the bot doesn't
        # have ``Manage Events`` we still keep the LFG post.
        scheduled = None
        if interaction.guild is not None:
            location = msg.jump_url
            scheduled = await _create_discord_scheduled_event(
                interaction.guild,
                name=event["title"],
                description=(
                    f"{event.get('description') or ''}\n\n"
                    f"Slot: {display_slot_label(event.get('slot_label'))}\n"
                    f"Sign up: {msg.jump_url}"
                ).strip(),
                starts_at=starts_at,
                ends_at=ends_at,
                location=location,
            )
            if scheduled is not None:
                db.set_lfg_scheduled_event_id(event_id, str(scheduled.id))

        # Tell the officer whether the post pinged anyone and, if not, why.
        # Helps debug "wait, did this notify anybody?" without having to
        # eyeball the channel.
        type_label = EVENT_TYPES_BY_KEY[self.event_type].label if self.event_type and self.event_type in EVENT_TYPES_BY_KEY else None
        if ping:
            ping_note = f"\n🔔 Pinged the **{type_label}** role."
        elif self.event_type is None:
            ping_note = "\n🔕 No content type chosen — nobody was pinged."
        else:
            ping_note = (
                f"\n🔕 No ping role mapped for **{type_label}** — set one "
                f"with `/lfg set-type-role`."
            )
        await interaction.followup.send(
            embed=success_embed(
                "Event posted",
                f"Posted in {channel.mention}: **{event['title']}**\n"
                f"Starts for you: <t:{int(starts_at.timestamp())}:F>\n"
                f"Albion/UTC timer: `{starts_at.strftime('%Y-%m-%d %H:%M')} UTC`"
                f"{ping_note}",
            ),
            ephemeral=True,
        )
        # Offer to link a guild Comp template so members can sign up to
        # specific build slots. Best-effort: skip silently if there are no
        # comps yet or list_comps isn't available.
        try:
            comps = db.list_comps(include_archived=False) or []
        except Exception:  # noqa: BLE001
            comps = []
        if comps:
            await interaction.followup.send(
                embed=info_embed(
                    "🧩 Attach a comp?",
                    "Pick a comp to unlock per-slot build signups on this "
                    "event. Members will see 🎯 **Pick build** to claim a "
                    "specific role/weapon. Skip if this is a casual roster.",
                ),
                view=_EventCompPickerView(event_id, comps),
                ephemeral=True,
            )
        info_log(
            f"LFG event #{event_id} created by {interaction.user} "
            f"[{slot_label}] {starts_at.isoformat()} -> {ends_at.isoformat()} "
            f"scheduled_event={getattr(scheduled, 'id', None)}"
        )
        await _refresh_prime_claim_dashboards(interaction.client, event, "create")


# ── Sign-up view (one per event message) ────────────────────────────────────
class _LootSplitModal(discord.ui.Modal, title="Record loot split"):
    """Officer-driven loot split for the event tied to ``event_id``.

    Re-uses ``cogs.loot.perform_event_loot_split`` so the silver_ledger rows
    have the same ref_type/ref_id as ``/loot split`` — meaning ``/loot
    history`` will surface button-driven splits alongside slash-command ones.
    """

    total = discord.ui.TextInput(
        label="Total silver to split",
        placeholder="e.g. 12500000",
        required=True,
        max_length=15,
    )
    tax_pct = discord.ui.TextInput(
        label="Guild tax % (0–50)",
        placeholder="0",
        required=False,
        max_length=2,
        default="0",
    )
    sc_bonus_pct = discord.ui.TextInput(
        label="Shotcaller bonus % (0–25)",
        placeholder="0",
        required=False,
        max_length=2,
        default="0",
    )
    include_all_signups = discord.ui.TextInput(
        label="Include all signups? (y/n)",
        placeholder="n",
        required=False,
        max_length=3,
        default="n",
    )
    silver_split = discord.ui.TextInput(
        label="Silver bags / opt-outs (optional)",
        placeholder="e.g. 5m | optout: @Alice @Bob",
        required=False,
        max_length=500,
    )

    def __init__(self, event_id: int) -> None:
        super().__init__()
        self._event_id = int(event_id)

    @staticmethod
    def _to_int(raw: str | None, default: int = 0) -> int:
        if raw is None:
            return default
        import re as _re
        cleaned = _re.sub(r"[^0-9-]", "", str(raw))
        try:
            return int(cleaned)
        except ValueError:
            return default

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot: Bot = interaction.client  # type: ignore[assignment]

        total_silver = self._to_int(str(self.total.value))
        tax_pct = max(0, min(50, self._to_int(str(self.tax_pct.value))))
        sc_bonus_pct = max(0, min(25, self._to_int(str(self.sc_bonus_pct.value))))
        include_all_signups = str(self.include_all_signups.value or "").strip().lower() in (
            "y", "yes", "true", "1", "on",
        )
        from cogs.loot import _parse_silver_split_field

        try:
            silver_total, silver_opt_out_ids = _parse_silver_split_field(str(self.silver_split.value or ""))
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed("Bad silver-bag value", str(exc)),
                ephemeral=True,
            )
            return
        if total_silver <= 0 and silver_total <= 0:
            await interaction.response.send_message(
                embed=error_embed(
                    "Bad amount",
                    "Enter a positive tradable loot total, a silver-bag/manual total, or both.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        # Lazy import to avoid circular module load at startup.
        from cogs.loot import perform_event_loot_split
        embed, err = perform_event_loot_split(
            bot,
            self._event_id,
            total_silver,
            tax_pct,
            sc_bonus_pct,
            str(interaction.user.id),
            include_all_signups=include_all_signups,
            silver_total=silver_total,
            silver_opt_out_ids=silver_opt_out_ids,
        )
        if err is not None or embed is None:
            await interaction.followup.send(
                embed=error_embed("Split failed", err or "Unknown error."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=embed)


class EditEventModal(discord.ui.Modal, title="Edit event"):
    """Modal opened from the ✏️ Edit button on an event message.

    Lets the creator (or an officer) tweak the human-facing fields and the
    start time / duration without cancelling + recreating the event. The
    Discord scheduled event (if any) is patched too so the in-client event
    tracker stays in sync.
    """

    def __init__(self, event: dict):
        super().__init__(timeout=600)
        self.event_id = int(event["id"])
        # Pre-fill every text input with the current value so the officer
        # only has to change what they actually want changed.
        self.title_input = discord.ui.TextInput(
            label="Event title",
            default=event.get("title") or "",
            max_length=100,
            required=True,
        )
        self.description_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=event.get("description") or "",
            max_length=1000,
            required=True,
        )
        self.comp_input = discord.ui.TextInput(
            label="Comp / build requirements",
            style=discord.TextStyle.paragraph,
            default=event.get("comp_notes") or "",
            max_length=500,
            required=False,
        )
        # Parse stored ISO into the friendlier "YYYY-MM-DD HH:MM" form so
        # the officer doesn't have to deal with the timezone suffix.
        try:
            starts = datetime.datetime.fromisoformat(event["starts_at"])
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=datetime.timezone.utc)
            ends = datetime.datetime.fromisoformat(event["ends_at"])
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=datetime.timezone.utc)
            duration_min = max(1, int(round((ends - starts).total_seconds() / 60)))
            start_str = starts.strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, KeyError):
            start_str = ""
            duration_min = 60
        self.start_input = discord.ui.TextInput(
            label="Start UTC/Albion — YYYY-MM-DD HH:MM",
            default=start_str,
            max_length=16,
            required=True,
        )
        self.duration_input = discord.ui.TextInput(
            label="Duration in minutes",
            default=str(duration_min),
            max_length=4,
            required=True,
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.comp_input)
        self.add_item(self.start_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.followup.send(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        try:
            starts_at = datetime.datetime.strptime(
                str(self.start_input.value).strip(), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=datetime.timezone.utc)
            duration = int(str(self.duration_input.value).strip())
            if duration <= 0 or duration > 24 * 60:
                raise ValueError("duration out of range")
            ends_at = starts_at + datetime.timedelta(minutes=duration)
        except ValueError as exc:
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't parse start time / duration",
                    f"`{exc}`. Use UTC `YYYY-MM-DD HH:MM` and a positive minutes value.",
                ),
                ephemeral=True,
            )
            return

        claim_fields = _claim_fields_for_schedule(starts_at, ends_at)
        will_be_prime = bool(claim_fields["is_prime"])
        was_prime = bool(int(event.get("is_prime") or 0))
        if will_be_prime and (
            not isinstance(interaction.user, discord.Member)
            or not _user_can_make_prime(interaction.user)
        ):
            await interaction.followup.send(
                embed=error_embed(
                    "Shotcaller+ only",
                    "That start time and duration exactly match a prime timer. "
                    "Prime-time claims are reserved for Shotcaller, Senior "
                    "Shotcaller, Officer, or Captain.",
                ),
                ephemeral=True,
            )
            return

        overlap = _overlapping_prime_events(
            db,
            starts_at,
            ends_at,
            exclude_event_id=self.event_id,
        )
        if will_be_prime and overlap:
            names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
            await interaction.followup.send(
                embed=error_embed(
                    "Slot already booked",
                    f"This prime-time slot already has: {names}. "
                    "Cancel it first or pick another slot.",
                ),
                ephemeral=True,
            )
            return
        if not will_be_prime and overlap:
            names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
            await interaction.followup.send(
                embed=error_embed(
                    "Conflicts with prime-time event(s)",
                    f"That new time would overlap: {names}. "
                    "Pick a different time or coordinate with the prime caller.",
                ),
                ephemeral=True,
            )
            return

        fields = {
            "title": str(self.title_input.value).strip(),
            "description": str(self.description_input.value).strip(),
            "comp_notes": str(self.comp_input.value).strip(),
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            **claim_fields,
        }
        ok = db.update_lfg_event(self.event_id, fields)
        if not ok:
            await interaction.followup.send(
                embed=error_embed("Couldn't save", "DB rejected the update."),
                ephemeral=True,
            )
            return

        # Refresh the posted message in-place.
        new_event = db.fetch_lfg_event(self.event_id) or event
        try:
            chan = interaction.guild.get_channel(int(new_event["channel_id"])) \
                if interaction.guild else None
            if chan is None:
                chan = await interaction.client.fetch_channel(int(new_event["channel_id"]))
            msg = await chan.fetch_message(int(new_event["message_id"]))
            await msg.edit(
                embed=_format_event_embed(db, new_event),
                view=EventSignupView(self.event_id) if new_event["status"] == "open" else None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
            warning_log(f"refresh after edit failed for LFG #{self.event_id}: {exc!r}")

        # Patch the linked Discord scheduled event so the native tracker
        # reflects the new title / description / time. Best-effort.
        sched_id = new_event.get("scheduled_event_id")
        if sched_id and interaction.guild is not None:
            try:
                sched = interaction.guild.get_scheduled_event(int(sched_id)) \
                    or await interaction.guild.fetch_scheduled_event(int(sched_id))
                if sched is not None:
                    await sched.edit(
                        name=fields["title"],
                        description=(
                            f"{fields['description']}\n\n"
                            f"Slot: {display_slot_label(new_event.get('slot_label'))}"
                        ).strip(),
                        start_time=starts_at,
                        end_time=ends_at,
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
                warning_log(
                    f"edit scheduled event for LFG #{self.event_id} failed: {exc!r}"
                )

        await interaction.followup.send(
            embed=success_embed("Event updated", f"Saved changes to **{fields['title']}**."),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} edited LFG #{self.event_id} "
            f"({fields['title']!r} {starts_at.isoformat()} -> {ends_at.isoformat()})"
        )
        await _refresh_prime_claim_dashboards(
            interaction.client,
            new_event,
            "edit",
            force=was_prime or will_be_prime,
        )


class RescheduleEventModal(discord.ui.Modal, title="Reschedule event"):
    """Small modal for changing only an event's start time and duration."""

    def __init__(self, event: dict):
        super().__init__(timeout=600)
        self.event_id = int(event["id"])
        try:
            starts = datetime.datetime.fromisoformat(event["starts_at"])
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=datetime.timezone.utc)
            ends = datetime.datetime.fromisoformat(event["ends_at"])
            if ends.tzinfo is None:
                ends = ends.replace(tzinfo=datetime.timezone.utc)
            duration_min = max(1, int(round((ends - starts).total_seconds() / 60)))
            start_str = starts.strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, KeyError):
            start_str = ""
            duration_min = 60
        self.start_input = discord.ui.TextInput(
            label="Start UTC/Albion — YYYY-MM-DD HH:MM",
            default=start_str,
            placeholder="2026-06-06 04:00",
            max_length=16,
            required=True,
        )
        self.duration_input = discord.ui.TextInput(
            label="Duration in minutes",
            default=str(duration_min),
            placeholder="60",
            max_length=4,
            required=True,
        )
        self.add_item(self.start_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.followup.send(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        try:
            starts_at = datetime.datetime.strptime(
                str(self.start_input.value).strip(), "%Y-%m-%d %H:%M"
            ).replace(tzinfo=datetime.timezone.utc)
            duration = int(str(self.duration_input.value).strip())
            if duration <= 0 or duration > 24 * 60:
                raise ValueError("duration out of range")
            ends_at = starts_at + datetime.timedelta(minutes=duration)
        except ValueError as exc:
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't parse start time / duration",
                    f"`{exc}`. Use UTC `YYYY-MM-DD HH:MM` and a positive minutes value.",
                ),
                ephemeral=True,
            )
            return

        claim_fields = _claim_fields_for_schedule(starts_at, ends_at)
        will_be_prime = bool(claim_fields["is_prime"])
        was_prime = bool(int(event.get("is_prime") or 0))
        if will_be_prime and (
            not isinstance(interaction.user, discord.Member)
            or not _user_can_make_prime(interaction.user)
        ):
            await interaction.followup.send(
                embed=error_embed(
                    "Shotcaller+ only",
                    "That start time and duration exactly match a prime timer. "
                    "Prime-time claims are reserved for Shotcaller, Senior "
                    "Shotcaller, Officer, or Captain.",
                ),
                ephemeral=True,
            )
            return

        overlap = _overlapping_prime_events(
            db,
            starts_at,
            ends_at,
            exclude_event_id=self.event_id,
        )
        if will_be_prime and overlap:
            names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
            await interaction.followup.send(
                embed=error_embed(
                    "Slot already booked",
                    f"This prime-time slot already has: {names}. "
                    "Cancel it first or pick another slot.",
                ),
                ephemeral=True,
            )
            return
        if not will_be_prime and overlap:
            names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
            await interaction.followup.send(
                embed=error_embed(
                    "Conflicts with prime-time event(s)",
                    f"That new time would overlap: {names}. "
                    "Pick a different time or coordinate with the prime caller.",
                ),
                ephemeral=True,
            )
            return

        old_starts = str(event.get("starts_at") or "")
        fields = {
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            **claim_fields,
        }
        ok = db.update_lfg_event(self.event_id, fields)
        if not ok:
            await interaction.followup.send(
                embed=error_embed("Couldn't save", "DB rejected the reschedule."),
                ephemeral=True,
            )
            return
        if old_starts != fields["starts_at"]:
            with contextlib.suppress(Exception):
                db.execute(
                    "UPDATE lfg_events SET reminded_at = NULL WHERE id = ?",
                    (self.event_id,),
                )

        new_event = db.fetch_lfg_event(self.event_id) or event
        try:
            chan = interaction.guild.get_channel(int(new_event["channel_id"])) \
                if interaction.guild else None
            if chan is None:
                chan = await interaction.client.fetch_channel(int(new_event["channel_id"]))
            msg = await chan.fetch_message(int(new_event["message_id"]))
            await msg.edit(
                embed=_format_event_embed(db, new_event),
                view=EventSignupView(self.event_id) if new_event["status"] == "open" else None,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
            warning_log(f"refresh after reschedule failed for LFG #{self.event_id}: {exc!r}")

        sched_id = new_event.get("scheduled_event_id")
        if sched_id and interaction.guild is not None:
            try:
                sched = interaction.guild.get_scheduled_event(int(sched_id)) \
                    or await interaction.guild.fetch_scheduled_event(int(sched_id))
                if sched is not None:
                    await sched.edit(
                        start_time=starts_at,
                        end_time=ends_at,
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
                warning_log(
                    f"reschedule scheduled event for LFG #{self.event_id} failed: {exc!r}"
                )

        await interaction.followup.send(
            embed=success_embed(
                "Event rescheduled",
                f"#{self.event_id} now starts **{starts_at.strftime('%Y-%m-%d %H:%M')} UTC**.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} rescheduled LFG #{self.event_id} "
            f"({starts_at.isoformat()} -> {ends_at.isoformat()})"
        )
        await _refresh_prime_claim_dashboards(
            interaction.client,
            new_event,
            "reschedule",
            force=was_prime or will_be_prime,
        )


class CancelEventModal(discord.ui.Modal):
    """Require a reason before an LFG event can be cancelled."""

    def __init__(self, event: dict, parent_view: "EventSignupView") -> None:
        super().__init__(title="Cancel LFG Event")
        self.event_id = int(event["id"])
        self.parent_view = parent_view
        self.reason_input = discord.ui.TextInput(
            label="Cancellation reason",
            placeholder="e.g. Not enough signups, no healer, rescheduling, real-life issue...",
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=500,
            required=True,
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        if str(event.get("status") or "open").lower() != "open":
            await interaction.response.send_message(
                embed=error_embed("Event closed", "This event is already closed."),
                ephemeral=True,
            )
            return
        if not _can_manage_lfg_event(interaction.user, event):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not allowed",
                    "Only the event creator or an Officer can cancel this event.",
                ),
                ephemeral=True,
            )
            return

        reason = re.sub(r"\s+", " ", str(self.reason_input.value or "")).strip()
        if len(reason) < 5:
            await interaction.response.send_message(
                embed=error_embed("Reason required", "Please give a clear reason before cancelling."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        cancelled_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
        db.cancel_lfg_event(
            self.event_id,
            reason=reason,
            cancelled_by=str(interaction.user.id),
            cancelled_at=cancelled_at,
        )
        updated_event = db.fetch_lfg_event(self.event_id) or event
        await self.parent_view._refresh_message(interaction)
        await _refresh_prime_claim_dashboards(interaction.client, updated_event, "cancel")

        # Also cancel the linked Discord scheduled event, if any.
        sched_id = event.get("scheduled_event_id")
        if sched_id and interaction.guild is not None:
            try:
                sched = interaction.guild.get_scheduled_event(int(sched_id)) \
                    or await interaction.guild.fetch_scheduled_event(int(sched_id))
                if sched is not None:
                    await sched.cancel(
                        reason=f"LFG #{self.event_id} cancelled by {interaction.user}: {reason[:220]}"
                    )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
                warning_log(
                    f"cancel scheduled event for LFG #{self.event_id} failed: {exc!r}"
                )
            db.set_lfg_scheduled_event_id(self.event_id, None)

        cleanup = getattr(interaction.client.get_cog("LFG"), "cleanup_lfg_event_surfaces", None)
        if callable(cleanup):
            await cleanup(self.event_id, reason=f"manual event cancel: {reason[:120]}")

        await interaction.followup.send(
            embed=info_embed(
                "Event cancelled",
                f"#{self.event_id} **{event['title']}** is cancelled.\n\n"
                f"**Reason:** {reason}",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} cancelled LFG #{self.event_id} "
            f"({event.get('title')!r}) reason={reason!r}"
        )


class EventSignupView(discord.ui.View):
    """Persistent view attached to each posted event message.

    custom_id encodes the event_id so we can survive restarts: when the bot
    restarts, the cog re-registers a view per open event by ID.
    """

    def __init__(self, event_id: int):
        super().__init__(timeout=None)
        self.event_id = event_id
        # Set unique custom_ids so the buttons keep working across restarts.
        self.signup.custom_id = f"lfg:signup:{event_id}"
        self.pick_build.custom_id = f"lfg:pickbuild:{event_id}"
        self.drop_build.custom_id = f"lfg:dropbuild:{event_id}"
        self.my_build.custom_id = f"lfg:mybuild:{event_id}"
        self.withdraw.custom_id = f"lfg:withdraw:{event_id}"
        self.change_comp.custom_id = f"lfg:comp:{event_id}"
        self.split_loot.custom_id = f"lfg:lootsplit:{event_id}"
        self.edit.custom_id = f"lfg:edit:{event_id}"
        self.reschedule.custom_id = f"lfg:reschedule:{event_id}"
        self.cancel.custom_id = f"lfg:cancel:{event_id}"

    async def _refresh_message(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            return
        try:
            message = getattr(interaction, "message", None)
            if message is None and event.get("channel_id") and event.get("message_id"):
                channel = interaction.client.get_channel(int(event["channel_id"]))
                if channel is None:
                    channel = await interaction.client.fetch_channel(int(event["channel_id"]))
                if hasattr(channel, "fetch_message"):
                    message = await channel.fetch_message(int(event["message_id"]))
            if message is None:
                return
            await message.edit(
                embed=_format_event_embed(db, event),
                view=None if event["status"] != "open" else self,
            )
        except (discord.NotFound, discord.HTTPException) as exc:
            warning_log(f"Couldn't edit LFG message for event #{self.event_id}: {exc}")

    @discord.ui.button(label="Sign up", style=discord.ButtonStyle.success, emoji="✅", custom_id="lfg:signup:0")
    async def signup(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event or event["status"] != "open":
            await interaction.response.send_message(
                embed=error_embed("Event closed", "This event is no longer accepting signups."),
                ephemeral=True,
            )
            return
        added = db.add_lfg_signup(self.event_id, str(interaction.user.id))
        if added:
            await interaction.response.send_message(
                embed=success_embed("Signed up", f"You're in for **{event['title']}**."),
                ephemeral=True,
            )
            # First-event hook: advance the recruitment funnel and (if member
            # is still Probationary) auto-promote toward Recruit. Best-effort.
            try:
                _on_first_event_signup(
                    interaction.client, db,
                    discord_id=str(interaction.user.id),
                    event=event,
                )
            except Exception as exc:  # noqa: BLE001
                from debug import error_log as _err
                _err(f"first-event hook failed for {interaction.user}: {exc!r}")
            await _grant_event_access_role(
                db,
                interaction.guild,
                event,
                interaction.user.id,
                reason=f"LFG #{self.event_id} signup voice access",
            )
        else:
            await interaction.response.send_message(
                embed=info_embed("Already signed up", "Use **Withdraw** to drop."),
                ephemeral=True,
            )
        await self._refresh_message(interaction)
        if added:
            await _refresh_prime_claim_dashboards(interaction.client, event, "signup")

    @discord.ui.button(label="Pick build", style=discord.ButtonStyle.primary, emoji="🎯", custom_id="lfg:pickbuild:0")
    async def pick_build(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event or event["status"] != "open":
            await interaction.response.send_message(
                embed=error_embed("Event closed", "This event isn't accepting signups."),
                ephemeral=True,
            )
            return
        if not event.get("comp_id"):
            await interaction.response.send_message(
                embed=info_embed(
                    "No comp attached",
                    "This event doesn't have a comp yet. Ask an officer to "
                    "run `/lfg set-comp` or just use **Sign up** to join "
                    "the general roster.",
                ),
                ephemeral=True,
            )
            return
        grid = db.fetch_lfg_slot_grid(self.event_id)
        if not grid:
            await interaction.response.send_message(
                embed=error_embed(
                    "Comp has no slots",
                    "The attached comp has no slots defined. Add slots "
                    "with `/comp add-slot` first.",
                ),
                ephemeral=True,
            )
            return
        view = _BuildPickerView(self.event_id, grid, str(interaction.user.id), self)
        await interaction.response.send_message(
            embed=info_embed(
                "🎯 Pick your build",
                "Pick a slot below. Picking auto-signs you up and replaces "
                "any previous slot you held on this event. Open slots are "
                "marked _open_; taken slots show the current holder.",
            ),
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="Drop build", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="lfg:dropbuild:0")
    async def drop_build(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        db = interaction.client.db
        released = db.release_lfg_slot(self.event_id, str(interaction.user.id))
        if released:
            await interaction.response.send_message(
                embed=info_embed(
                    "Build released",
                    "You're still on the roster, just not locked into a "
                    "build. Use **Pick build** to grab a different one.",
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=info_embed("No build to drop", "You don't have a build claimed on this event."),
                ephemeral=True,
            )
        await self._refresh_message(interaction)

    @discord.ui.button(label="My build", style=discord.ButtonStyle.primary, emoji="📋", custom_id="lfg:mybuild:0")
    async def my_build(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Re-send the build briefing for the slot this user currently
        holds on the event. Anyone who picked a build can come back later
        and check exactly what gear and food they need to bring."""
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        if not event.get("comp_id"):
            await interaction.response.send_message(
                embed=info_embed(
                    "No comp attached",
                    "This event doesn't have a comp, so there are no "
                    "per-build briefings. You're on the general roster.",
                ),
                ephemeral=True,
            )
            return
        user_id = str(interaction.user.id)
        slot = next(
            (s for s in db.fetch_lfg_slot_grid(self.event_id)
             if s.get("claimed_by") and str(s["claimed_by"]) == user_id),
            None,
        )
        if not slot:
            await interaction.response.send_message(
                embed=info_embed(
                    "No build claimed",
                    "Hit 🎯 **Pick build** to grab a slot first.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=_format_build_briefing(db, event, slot),
            ephemeral=True,
        )

    @discord.ui.button(label="Withdraw", style=discord.ButtonStyle.secondary, emoji="↩️", custom_id="lfg:withdraw:0")
    async def withdraw(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        db = interaction.client.db
        removed = db.remove_lfg_signup(self.event_id, str(interaction.user.id))
        if removed:
            await interaction.response.send_message(
                embed=info_embed("Withdrawn", "Removed from the roster."),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=info_embed("Not signed up", "You weren't on the roster."),
                ephemeral=True,
            )
        await self._refresh_message(interaction)
        if removed:
            event = db.fetch_lfg_event(self.event_id)
            if event:
                await _revoke_event_access_role_if_unneeded(
                    db,
                    interaction.guild,
                    event,
                    interaction.user.id,
                    reason=f"LFG #{self.event_id} withdraw voice access",
                )
            await _refresh_prime_claim_dashboards(interaction.client, event, "withdraw")

    @discord.ui.button(label="Comp", style=discord.ButtonStyle.secondary, emoji="🧩", custom_id="lfg:comp:0")
    async def change_comp(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        if event["status"] != "open":
            await interaction.response.send_message(
                embed=error_embed("Event closed", "Only open events can change comp."),
                ephemeral=True,
            )
            return
        if not _can_manage_lfg_event(interaction.user, event):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not allowed",
                    "Only the event creator or an Officer can change this event's comp.",
                ),
                ephemeral=True,
            )
            return
        try:
            comps = db.list_comps(include_archived=False) or []
        except Exception:  # noqa: BLE001
            comps = []
        if not comps:
            await interaction.response.send_message(
                embed=info_embed(
                    "No comps available",
                    "Create a comp first with `/comp create`, then come back here.",
                ),
                ephemeral=True,
            )
            return
        current = "No comp attached"
        if event.get("comp_id"):
            comp = db.fetch_comp(int(event["comp_id"])) or {}
            current = f"Current comp: **{comp.get('name') or event['comp_id']}**"
        await interaction.response.send_message(
            embed=info_embed(
                "Change event comp",
                f"{current}\n\n"
                "Pick a new comp below. Changing comps clears existing "
                "build-slot claims, but keeps the event roster.",
            ),
            view=_EventCompPickerView(
                self.event_id,
                comps,
                allow_clear=bool(event.get("comp_id")),
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Split loot", style=discord.ButtonStyle.primary, emoji="💰", custom_id="lfg:lootsplit:0")
    async def split_loot(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Officer-only: open a modal that records a loot split for this
        event. Mirrors ``/loot split`` so the entries land in the same
        ``silver_ledger`` history."""
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(
                    "Officers only",
                    "Splitting silver is an officer action.",
                ),
                ephemeral=True,
            )
            return
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_LootSplitModal(self.event_id))

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, emoji="✏️", custom_id="lfg:edit:0")
    async def edit(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Open a modal so the event creator (or an officer) can tweak the
        title / description / comp notes / start time / duration without
        having to cancel and recreate the event."""
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        is_creator = str(interaction.user.id) == event["creator_id"]
        is_staff = False
        if isinstance(interaction.user, discord.Member):
            is_staff = any(
                r.name in CANCEL_OVERRIDE_ROLES for r in interaction.user.roles
            )
        if not (is_creator or is_staff):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not allowed",
                    "Only the event creator or an Officer can edit this event.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(EditEventModal(event))

    @discord.ui.button(label="Reschedule", style=discord.ButtonStyle.secondary, emoji="🕒", custom_id="lfg:reschedule:0")
    async def reschedule(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        """Open a focused modal for changing only the event time."""
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event missing", "That event no longer exists."),
                ephemeral=True,
            )
            return
        if event["status"] != "open":
            await interaction.response.send_message(
                embed=error_embed("Event closed", "Only open events can be rescheduled."),
                ephemeral=True,
            )
            return
        if not _can_manage_lfg_event(interaction.user, event):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not allowed",
                    "Only the event creator or an Officer can reschedule this event.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RescheduleEventModal(event))

    @discord.ui.button(label="Cancel event", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="lfg:cancel:0")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        db = interaction.client.db
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            return
        if str(event.get("status") or "open").lower() != "open":
            await interaction.response.send_message(
                embed=error_embed("Event closed", "This event is already closed."),
                ephemeral=True,
            )
            return
        if not _can_manage_lfg_event(interaction.user, event):
            await interaction.response.send_message(
                embed=error_embed("Not allowed", "Only the event creator or an Officer can cancel this event."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(CancelEventModal(event, self))


# ── Control-panel view (the message in the events board) ────────────────────
class EventBoardView(discord.ui.View):
    """The persistent panel with one button per prime slot + General LFG."""

    def __init__(self, bot: Bot):
        super().__init__(timeout=None)
        self.bot = bot

        for slot in PRIME_SLOTS:
            self.add_item(_PrimeSlotButton(slot))
        self.add_item(_GeneralLFGButton())


class _PrimeSlotButton(discord.ui.Button):
    def __init__(self, slot: PrimeSlot):
        super().__init__(
            label=prime_slot_display_label(slot),
            style=discord.ButtonStyle.secondary,
            emoji=slot.emoji,
            custom_id=f"lfg:slot:{slot.slot_id}",
        )
        self.slot = slot

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not _user_can_make_prime(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(
                    "Shotcaller+ only",
                    "Prime-time slots are reserved for Shotcaller, Senior Shotcaller, "
                    "Officer, or Captain. Use **General LFG** for non-prime events.",
                ),
                ephemeral=True,
            )
            return
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        next_start, _ = _next_occurrence(self.slot, now_utc)
        await interaction.response.send_message(
            embed=info_embed(
                "What kind of event is this?",
                "Pick a content type so members with that ping role get notified. "
                "Only roles available on the Content Roles panel are shown here. "
                "Ping mappings are configured with `/lfg set-type-role`. "
                "Choose **Skip** if this one shouldn't ping anyone.\n\n"
                "Next you will enter the **Albion/UTC date** for this timer. "
                f"Current Albion/UTC: `{now_utc.strftime('%Y-%m-%d %H:%M')}`. "
                "If your local day is different, still use the UTC date.\n"
                f"Next `{prime_slot_display_label(self.slot)}` starts for you: "
                f"<t:{int(next_start.timestamp())}:F>. "
                f"Date to enter for that next timer: `{next_start.strftime('%Y-%m-%d')}`.",
            ),
            view=_PickEventTypeView(interaction.client, interaction.guild, self.slot),
            ephemeral=True,
        )


class _GeneralLFGButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="General LFG",
            style=discord.ButtonStyle.success,
            emoji="🟦",
            custom_id="lfg:slot:general",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=info_embed(
                "What kind of event is this?",
                "Pick a content type so members with that ping role get notified. "
                "Only roles available on the Content Roles panel are shown here. "
                "Ping mappings are configured with `/lfg set-type-role`. "
                "Choose **Skip** if this one shouldn't ping anyone.\n\n"
                "General LFG start times are typed as Albion/UTC time; the "
                "posted event will show everyone their local Discord time.",
            ),
            view=_PickEventTypeView(interaction.client, interaction.guild, None),
            ephemeral=True,
        )


# ── Event-type picker (shown before CreateEventModal) ───────────────────────
def _has_configured_ping_role(bot: Bot, guild: discord.Guild | None, event_type) -> bool:
    rid = bot.db.get_config(CFG_ROLE_PREFIX + event_type.key)
    if not rid or guild is None:
        return False
    try:
        return guild.get_role(int(rid)) is not None
    except (TypeError, ValueError):
        return False


def _pingable_event_types(bot: Bot, guild: discord.Guild | None, types: list) -> list:
    return [t for t in types if _has_configured_ping_role(bot, guild, t)]


class _PickEventTypeView(discord.ui.View):
    """One-shot picker shown after the user clicks a slot/General button on
    the event board. The selected event-type key (or None for skip) is
    forwarded into ``CreateEventModal`` so the resulting post can ping the
    role mapped to that content type via ``/lfg set-type-role``.

    The picker only includes event types with a configured Discord role,
    which keeps it in sync with the public Content Roles panel.
    """

    def __init__(self, bot: Bot, guild: discord.Guild | None, slot: PrimeSlot | None):
        super().__init__(timeout=300)
        self.slot = slot

        combat_categories = {"PvP — Combat", "PvP — Small", "Other"}
        combat_types = _pingable_event_types(
            bot,
            guild,
            [t for t in EVENT_TYPES if t.category in combat_categories],
        )
        peace_types = _pingable_event_types(
            bot,
            guild,
            [t for t in EVENT_TYPES if t.category not in combat_categories],
        )

        if combat_types:
            self.add_item(_EventTypeSelect(
                "Combat / PvP content…", combat_types, slot,
            ))
        if peace_types:
            self.add_item(_EventTypeSelect(
                "PvE / Economy / Guild content…", peace_types, slot,
            ))

        skip_btn = discord.ui.Button(
            label="Skip — no ping",
            style=discord.ButtonStyle.secondary,
            emoji="⏭️",
            row=2,
        )

        async def _skip_cb(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(
                CreateEventModal(interaction.client, self.slot, event_type=None),
            )

        skip_btn.callback = _skip_cb  # type: ignore[assignment]
        self.add_item(skip_btn)


class _EventTypeSelect(discord.ui.Select):
    """One of the two selects inside ``_PickEventTypeView``. Holds up to 25
    event types from one half of the EVENT_TYPES list. Picking an option
    immediately opens ``CreateEventModal`` pre-stamped with that type so
    the resulting channel post can include the content-role ping.
    """

    def __init__(self, placeholder: str, types: list, slot: PrimeSlot | None):
        # Defensive: enforce the 25-option Discord cap.
        types = types[:25]
        self._slot = slot
        options = [
            discord.SelectOption(
                label=t.label[:100],
                value=t.key,
                emoji=t.emoji,
                description=t.category[:100],
            )
            for t in types
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        chosen = self.values[0]
        if chosen not in EVENT_TYPES_BY_KEY:
            await interaction.response.send_message(
                embed=error_embed("Unknown type", f"`{chosen}` is no longer valid."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            CreateEventModal(interaction.client, self._slot, event_type=chosen),
        )



def _board_embed() -> discord.Embed:
    e = discord.Embed(
        title="📅 Guild Event Board",
        description=(
            "Create LFG posts from the buttons below.\n"
            "**Prime Time** locks a UTC timer and is limited to Shotcaller+.\n"
            "**General LFG** is for everything outside prime timers.\n"
            "Events include **30 min prep** and **15 min VOD review**."
        ),
        color=discord.Color.from_str("#e67e22"),
    )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    lines: list[str] = []
    for s in PRIME_SLOTS:
        start, end = _next_occurrence(s, now_utc)
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        lines.append(
            f"{s.emoji} **{prime_slot_display_label(s)}** · "
            f"<t:{start_ts}:t>-<t:{end_ts}:t> local · <t:{start_ts}:R>"
        )
    e.add_field(
        name="Prime Timers",
        value="\n".join(lines),
        inline=False,
    )
    e.add_field(
        name="Creation Notes",
        value=(
            f"Current UTC: `{now_utc:%b %d %H:%M}`. "
            "Prime modals pre-fill the next UTC date; use Albion/UTC if editing manually."
        ),
        inline=False,
    )
    e.add_field(
        name="General LFG",
        value="🟦 Any non-prime content or custom start time.",
        inline=False,
    )
    return e


# ── Build picker (ephemeral; one Select per page of up to 25 slots) ─────────
class _BuildPickerView(discord.ui.View):
    """Transient ephemeral view shown after the user clicks 🎯 Pick build.

    Discord caps Selects at 25 options, so a 20-slot ZvZ comp fits in one
    Select; bigger comps get pagination via prev/next buttons.
    """

    PAGE_SIZE = 25

    def __init__(
        self, event_id: int, grid: list[dict], user_id: str,
        parent_view: EventSignupView, page: int = 0,
    ):
        super().__init__(timeout=300)
        self.event_id = event_id
        self.grid = grid
        self.user_id = user_id
        self.parent_view = parent_view
        self.page = page
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        chunk = self.grid[start:start + self.PAGE_SIZE]
        self.add_item(_BuildPickSelect(self, chunk))
        n_pages = (len(self.grid) + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        if n_pages > 1:
            prev_btn = discord.ui.Button(
                label=f"◀ Page {self.page}",
                style=discord.ButtonStyle.secondary,
                disabled=self.page == 0,
            )
            next_btn = discord.ui.Button(
                label=f"Page {self.page + 2} ▶",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= n_pages - 1,
            )

            async def _prev(interaction: discord.Interaction):
                self.page -= 1
                self._rebuild()
                await interaction.response.edit_message(view=self)

            async def _next(interaction: discord.Interaction):
                self.page += 1
                self._rebuild()
                await interaction.response.edit_message(view=self)

            prev_btn.callback = _prev  # type: ignore[assignment]
            next_btn.callback = _next  # type: ignore[assignment]
            self.add_item(prev_btn)
            self.add_item(next_btn)


class _BuildPickSelect(discord.ui.Select):
    def __init__(self, parent: _BuildPickerView, slots: list[dict]):
        self.parent_picker = parent
        options: list[discord.SelectOption] = []
        for s in slots:
            slot_id = int(s["slot_id"])
            taken = bool(s.get("claimed_by"))
            mine = taken and str(s["claimed_by"]) == parent.user_id
            weapon = (s.get("weapon") or "?")[:60]
            role = (s.get("role") or "")[:20]
            label = f"{role} · {weapon}"[:100] if role else weapon[:100]
            if mine:
                desc = "✅ You currently hold this slot"
                emoji = "✅"
            elif taken:
                desc = "🔒 Taken by another member"
                emoji = "🔒"
            else:
                desc_parts = []
                if s.get("chest"):
                    desc_parts.append(str(s["chest"])[:30])
                if s.get("notes"):
                    desc_parts.append(str(s["notes"])[:40])
                desc = " · ".join(desc_parts) or "_open_"
                emoji = "🟢"
            options.append(discord.SelectOption(
                label=label or f"Slot #{slot_id}",
                description=desc[:100],
                value=str(slot_id),
                emoji=emoji,
            ))
        super().__init__(
            placeholder="Pick the build you're bringing…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        slot_id = int(self.values[0])
        event = None
        ok, reason = db.claim_lfg_slot(
            self.parent_picker.event_id,
            str(interaction.user.id),
            slot_id,
        )
        if not ok:
            msg = {
                "taken": (
                    "Someone grabbed that slot first. Pick another or hit "
                    "🔓 **Drop build** later to swap."
                ),
                "not_in_comp": "That slot isn't part of this event's comp anymore.",
                "error": "Database error — try again.",
            }.get(reason, "Couldn't claim that slot.")
            await interaction.response.send_message(
                embed=error_embed("Slot unavailable", msg), ephemeral=True,
            )
            return
        # First-event funnel hook (same as Sign up button).
        try:
            event = db.fetch_lfg_event(self.parent_picker.event_id)
            if event:
                _on_first_event_signup(
                    interaction.client, db,
                    discord_id=str(interaction.user.id),
                    event=event,
                )
                await _grant_event_access_role(
                    db,
                    interaction.guild,
                    event,
                    interaction.user.id,
                    reason=f"LFG #{self.parent_picker.event_id} build signup voice access",
                )
        except Exception as exc:  # noqa: BLE001
            error_log(f"first-event hook (build pick) failed: {exc!r}")
        # Refresh the main event message.
        try:
            event = db.fetch_lfg_event(self.parent_picker.event_id)
            chan = interaction.client.get_channel(int(event["channel_id"])) \
                if event and event.get("channel_id") else None
            msg = None
            if chan and event.get("message_id"):
                try:
                    msg = await chan.fetch_message(int(event["message_id"]))  # type: ignore[attr-defined]
                except (discord.NotFound, discord.Forbidden):
                    msg = None
            if msg is not None:
                await msg.edit(
                    embed=_format_event_embed(db, event),
                    view=self.parent_picker.parent_view,
                )
        except Exception as exc:  # noqa: BLE001
            warning_log(f"build pick refresh failed: {exc!r}")
        # Build the full briefing so the player sees exactly what to bring
        # without having to click "My build" separately.
        slot_row = next(
            (s for s in db.fetch_lfg_slot_grid(self.parent_picker.event_id)
             if int(s.get("slot_id") or 0) == slot_id),
            None,
        )
        if event and slot_row:
            briefing = _format_build_briefing(db, event, slot_row)
            if reason == "already_yours":
                briefing.title = (briefing.title or "") + " — already yours"
            await interaction.response.edit_message(embed=briefing, view=None)
        else:
            flavor = (
                "Already yours — no change." if reason == "already_yours"
                else "Locked in. Show up in the right gear and food. 🪖"
            )
            await interaction.response.edit_message(
                embed=success_embed("🎯 Build claimed", flavor),
                view=None,
            )
        if reason == "claimed":
            await _refresh_prime_claim_dashboards(interaction.client, event, "build pick")



# ── Event comp picker (offered right after event creation) ─────────────────
class _EventCompPickerView(discord.ui.View):
    """Ephemeral view for attaching, changing, or clearing an event comp."""

    def __init__(
        self,
        event_id: int,
        comps: list[dict],
        *,
        allow_clear: bool = False,
    ):
        super().__init__(timeout=300)
        self.event_id = event_id
        # Discord caps Select options at 25; if a guild has more comps,
        # prefer the first 25 (alphabetical from list_comps). Officers can
        # still attach less-common comps later via `/lfg set-comp`.
        options: list[discord.SelectOption] = []
        for c in comps[:25]:
            label = str(c.get("name") or f"Comp #{c.get('id')}")[:100]
            ct = (c.get("content_type") or "").strip() or "comp"
            desc = f"{ct} · {c.get('description') or ''}".strip(" ·")[:100]
            options.append(discord.SelectOption(
                label=label, value=str(c["id"]),
                description=desc or None,
            ))
        select = discord.ui.Select(
            placeholder="Pick a comp for this event…",
            min_values=1, max_values=1, options=options,
        )

        async def _select_cb(interaction: discord.Interaction) -> None:
            db = interaction.client.db
            comp_id = int(select.values[0])
            db.set_lfg_event_comp(self.event_id, comp_id)
            await _refresh_event_message(interaction.client, db, self.event_id)
            comp = db.fetch_comp(comp_id) or {}
            await interaction.response.edit_message(
                embed=success_embed(
                    "🧩 Comp updated",
                    f"**{comp.get('name', comp_id)}** is now the build "
                    "roster for this event. Existing build-slot claims were "
                    "cleared if the comp changed.",
                ),
                view=None,
            )

        select.callback = _select_cb  # type: ignore[assignment]
        self.add_item(select)

        if allow_clear:
            clear_btn = discord.ui.Button(
                label="Clear comp",
                style=discord.ButtonStyle.danger,
                emoji="🧹",
            )

            async def _clear_cb(interaction: discord.Interaction) -> None:
                db = interaction.client.db
                db.set_lfg_event_comp(self.event_id, None)
                await _refresh_event_message(interaction.client, db, self.event_id)
                await interaction.response.edit_message(
                    embed=success_embed(
                        "🧹 Comp cleared",
                        "This event no longer has a comp. Build-slot claims "
                        "were cleared, but the event roster was kept.",
                    ),
                    view=None,
                )

            clear_btn.callback = _clear_cb  # type: ignore[assignment]
            self.add_item(clear_btn)

        skip_btn = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary,
            emoji="⏭️",
        )

        async def _skip_cb(interaction: discord.Interaction) -> None:
            title = "No change" if allow_clear else "No comp attached"
            body = (
                "The event comp was left as-is."
                if allow_clear else
                "You can attach one later with `/lfg set-comp` or the 🧩 "
                "**Comp** button on the event."
            )
            await interaction.response.edit_message(
                embed=info_embed(
                    title,
                    body,
                ),
                view=None,
            )

        skip_btn.callback = _skip_cb  # type: ignore[assignment]
        self.add_item(skip_btn)


async def _refresh_event_message(bot, db, event_id: int) -> None:
    """Edit the posted event message in place with a freshly rendered embed.

    Best-effort: a missing channel/message just logs a warning so command
    flows never explode on stale links.
    """
    try:
        event = db.fetch_lfg_event(event_id)
        if not event or not event.get("channel_id") or not event.get("message_id"):
            return
        chan = bot.get_channel(int(event["channel_id"]))
        if chan is None:
            return
        try:
            msg = await chan.fetch_message(int(event["message_id"]))
        except (discord.NotFound, discord.Forbidden):
            return
        await msg.edit(
            embed=_format_event_embed(db, event),
            view=EventSignupView(event_id),
        )
    except Exception as exc:  # noqa: BLE001
        warning_log(f"_refresh_event_message #{event_id} failed: {exc!r}")
