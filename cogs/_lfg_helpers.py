"""LFG helpers extracted from cogs/lfg.py.

Pure helper functions used by the LFG cog and the sibling
``cogs/_lfg_views`` module: prime-slot maths, role/perm gates,
auto-discovery of channels/roles, channel/ping resolution per event
type, and the event-embed formatter.

Sibling module — leading underscore keeps the cog auto-loader from
trying to load this as a cog. cogs/lfg.py imports the public names
back at module load.
"""
from __future__ import annotations

import datetime
import re

import discord

from cogs._lfg_config import (
    BOARD_CHANNEL_KEYWORDS,
    CFG_BOARD_CHANNEL,
    CFG_CHAN_PREFIX,
    CFG_LFG_CHANNEL,
    CFG_ROLE_PREFIX,
    EVENT_TYPES,
    PER_TYPE_EXCLUDE_CATEGORIES,
    POST_CHANNEL_KEYWORDS,
    PREP_MINUTES,
    PRIME_CREATOR_ROLES,
    PRIME_SLOT_COLORS,
    REVIEW_MINUTES,
    PrimeSlot,
    canonical_event_type_key,
    display_slot_label,
    next_prime_slot_window,
    prime_slot_for_label,
    prime_slot_window_on_date,
    utc_datetime,
)
from debug import error_log, warning_log


# ── Helpers ─────────────────────────────────────────────────────────────────
def _normalize_ip_requirement(text: str | None, *, allow_bare: bool = False) -> str | None:
    """Return a compact IP requirement like ``1500 IP`` from user text."""
    raw = str(text or "").strip()
    if not raw:
        return None
    patterns = [
        r"\b(?:min(?:imum)?\s*)?ip(?:\s*(?:req(?:uirement)?|min(?:imum)?))?\D{0,12}([1-2]\d{3})\b",
        r"\b([1-2]\d{3})\s*(?:\+|(?:min(?:imum)?\s*)?ip\b)",
    ]
    if allow_bare:
        patterns.append(r"^\D*([1-2]\d{3})\D*$")
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            ip = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 800 <= ip <= 2200:
            return f"{ip} IP"
    return None


def _extract_ip_requirement(*texts: str | None) -> str | None:
    """Return a compact IP requirement like ``1500 IP`` from free text."""
    joined = " ".join(str(t or "") for t in texts if str(t or "").strip())
    if not joined:
        return None
    return _normalize_ip_requirement(joined)


def _event_voice_channel_name(event: dict) -> str:
    """Build a Discord-safe temporary voice-channel name for an LFG event."""
    raw_title = re.sub(r"\s+", " ", str(event.get("title") or "LFG Event")).strip()
    raw_title = re.sub(r"[*_`~|<>]", "", raw_title)
    ip_req = _normalize_ip_requirement(
        event.get("ip_requirement"),
        allow_bare=True,
    ) or _extract_ip_requirement(
        event.get("comp_notes"),
        event.get("description"),
        event.get("title"),
    )
    suffix = f" - {ip_req}" if ip_req and ip_req not in raw_title else ""
    base = f"🎙️ {raw_title}{suffix}"
    if len(base) <= 100:
        return base
    room_for_title = max(10, 100 - len("🎙️ ") - len(suffix) - 3)
    return f"🎙️ {raw_title[:room_for_title].rstrip()}...{suffix}"


def _event_access_role_name(event: dict) -> str:
    """Build the temporary role name used to gate an event voice channel."""
    event_id = str(event.get("id") or "?")
    raw_title = re.sub(r"\s+", " ", str(event.get("title") or "LFG Event")).strip()
    raw_title = re.sub(r"[*_`~|<>@#:&]", "", raw_title)
    base = f"LFG {event_id} - {raw_title or 'Event'} Access"
    if len(base) <= 100:
        return base
    prefix = f"LFG {event_id} - "
    suffix = " Access"
    room = max(10, 100 - len(prefix) - len(suffix) - 3)
    return f"{prefix}{raw_title[:room].rstrip()}...{suffix}"


def _event_voice_overwrites(
    guild: discord.Guild,
    access_role: discord.Role,
    category: discord.CategoryChannel | None = None,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Return visible-but-roster-gated overwrites for event voice.

    The channel should remain visible to whoever can normally see the target
    voice category, but only the temporary event access role can join. Copying
    category overwrites keeps guest/alliance/faction boundaries intact.
    """
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
    if category is not None:
        for target, overwrite in category.overwrites.items():
            copied = discord.PermissionOverwrite.from_pair(*overwrite.pair())
            copied.connect = False
            overwrites[target] = copied

    if guild.default_role in overwrites:
        overwrites[guild.default_role].connect = False
    else:
        overwrites[guild.default_role] = discord.PermissionOverwrite(connect=False)

    overwrites[access_role] = discord.PermissionOverwrite(
        view_channel=True,
        connect=True,
        speak=True,
        stream=True,
        use_voice_activation=True,
    )
    me = guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            manage_channels=True,
            move_members=True,
        )
    return overwrites


async def _fetch_event_access_role(
    guild: discord.Guild,
    event: dict,
) -> discord.Role | None:
    role_id = str(event.get("access_role_id") or "").strip()
    if not role_id:
        return None
    try:
        role = guild.get_role(int(role_id))
        if role is not None:
            return role
        fetched = await guild.fetch_roles()
        return next((r for r in fetched if r.id == int(role_id)), None)
    except (discord.Forbidden, discord.HTTPException, ValueError):
        return None


async def _ensure_event_access_role(
    db,
    guild: discord.Guild,
    event: dict,
    when_iso: str,
) -> discord.Role | None:
    """Create or fetch the temporary role for this event voice channel."""
    role = await _fetch_event_access_role(guild, event)
    if role is not None:
        return role

    try:
        role = await guild.create_role(
            name=_event_access_role_name(event),
            permissions=discord.Permissions.none(),
            hoist=False,
            mentionable=False,
            reason=f"LFG event #{event.get('id')} voice access",
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        warning_log(
            f"LFG #{event.get('id')} access role create failed: {exc!r}"
        )
        return None

    db.set_lfg_access_role_id(int(event["id"]), str(role.id), when_iso)
    event["access_role_id"] = str(role.id)
    return role


def _event_voice_access_ids(event: dict, signups: list[dict]) -> set[str]:
    ids = {
        str(row.get("discord_id") or "")
        for row in signups
        if str(row.get("discord_id") or "").isdigit()
    }
    creator_id = str(event.get("creator_id") or "")
    if creator_id.isdigit():
        ids.add(creator_id)
    return ids


async def _grant_event_access_role(
    db,
    guild: discord.Guild | None,
    event: dict,
    user_id: int | str,
    *,
    reason: str,
) -> bool:
    """Grant event voice access to one signed-up member, if the role exists."""
    if guild is None:
        return False
    role = await _fetch_event_access_role(guild, event)
    if role is None:
        return False
    try:
        member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        return False
    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        warning_log(
            f"LFG #{event.get('id')} access role grant failed for {user_id}: {exc!r}"
        )
        return False


async def _revoke_event_access_role_if_unneeded(
    db,
    guild: discord.Guild | None,
    event: dict,
    user_id: int | str,
    *,
    reason: str,
) -> bool:
    """Remove event access from a withdrawn member unless they still need it."""
    if guild is None:
        return False
    if str(user_id) == str(event.get("creator_id") or ""):
        return False
    role = await _fetch_event_access_role(guild, event)
    if role is None:
        return False
    still_signed = {
        str(row.get("discord_id") or "")
        for row in db.fetch_lfg_signups(int(event["id"]))
    }
    if str(user_id) in still_signed:
        return False
    try:
        member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        return False
    if role not in member.roles:
        return True
    try:
        await member.remove_roles(role, reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        warning_log(
            f"LFG #{event.get('id')} access role revoke failed for {user_id}: {exc!r}"
        )
        return False


async def _sync_event_access_role_members(
    db,
    guild: discord.Guild,
    event: dict,
    role: discord.Role,
    *,
    reason: str,
) -> None:
    """Make the temp role match the event creator + current signup roster."""
    wanted = _event_voice_access_ids(event, db.fetch_lfg_signups(int(event["id"])))

    for discord_id in sorted(wanted):
        try:
            member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            continue
        if role not in member.roles:
            try:
                await member.add_roles(role, reason=reason)
            except (discord.Forbidden, discord.HTTPException) as exc:
                warning_log(
                    f"LFG #{event.get('id')} access role sync grant failed "
                    f"for {discord_id}: {exc!r}"
                )

    for member in list(role.members):
        if member.bot:
            continue
        if str(member.id) in wanted:
            continue
        try:
            await member.remove_roles(role, reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            warning_log(
                f"LFG #{event.get('id')} access role sync revoke failed "
                f"for {member.id}: {exc!r}"
            )


async def _delete_event_access_role(
    db,
    guild: discord.Guild | None,
    event: dict,
    when_iso: str,
    *,
    reason: str,
) -> bool:
    """Delete the temporary event access role and stamp cleanup."""
    role = await _fetch_event_access_role(guild, event) if guild is not None else None
    if role is not None:
        try:
            await role.delete(reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            warning_log(
                f"LFG #{event.get('id')} access role delete failed: {exc!r}"
            )
            return False
    if event.get("access_role_id"):
        db.mark_lfg_access_role_deleted(int(event["id"]), when_iso)
    return True


def _next_occurrence(slot: PrimeSlot, now: datetime.datetime | None = None) -> tuple[datetime.datetime, datetime.datetime]:
    """Return (start_utc, end_utc) for the next time this slot starts.

    If we're currently inside the slot's hour, schedule the *next* one tomorrow
    so members aren't accidentally creating an event 5 minutes from now.
    """
    return next_prime_slot_window(slot, now)


def _slot_occurrence_on_date(
    slot: PrimeSlot,
    date_utc: datetime.date,
) -> tuple[datetime.datetime, datetime.datetime]:
    """Return (start_utc, end_utc) for ``slot`` on the chosen UTC start date."""
    return prime_slot_window_on_date(date_utc, slot)


def _user_can_make_prime(member: discord.Member) -> bool:
    """True if ``member`` holds any role in :data:`PRIME_CREATOR_ROLES`.

    Uses the live ``Member.roles`` cache. For DB-backed checks (when you
    only have IDs, or you want consistency with stale-gateway scenarios)
    use :func:`_db_user_can_make_prime` instead.
    """
    return any(r.name in PRIME_CREATOR_ROLES for r in member.roles)


def _db_user_can_make_prime(db, guild_id: int | str, user_id: int | str) -> bool:
    """DB-backed twin of :func:`_user_can_make_prime`. Uses the cached
    ``discord_member_roles`` table so it stays correct even if the live
    ``Member.roles`` cache is briefly stale (e.g. just after a role change).
    """
    return db.member_has_role(
        str(guild_id), str(user_id), role_names=list(PRIME_CREATOR_ROLES)
    )


# Categories whose channels should NEVER be auto-picked as a per-type LFG
# post target. (Imported from :mod:`cogs._lfg_config`.)


# ── Auto-config: discover channels + roles from the cached DB inventory ─────
def auto_discover_config(db, guild: discord.Guild, force: bool = False) -> dict:
    """Scan the cached Discord inventory for likely event-board / post / per-type
    channels and roles, persist what we find to ``guild_config``, and return a
    summary for display.

    Reads from the ``discord_roles`` / ``discord_channels`` tables (populated
    by ``cogs/events._sync_discord_inventory``). If the cache is empty we
    refresh it on the spot so this command never silently does nothing on a
    fresh install.

    By default doesn't overwrite a key that already has a value, so
    re-running is safe and respects manual overrides set via
    ``/lfg set-type-role`` etc. Pass ``force=True`` to wipe the per-type
    role + channel mappings first and re-detect from scratch — useful when
    the keyword logic itself has been tightened.
    """
    gid = str(guild.id)

    # Make sure the inventory is populated. Cheap if it already is.
    if not db.fetch_discord_roles(gid) or not db.fetch_discord_channels(gid):
        db.sync_discord_inventory(guild)

    if force:
        # Wipe per-type mappings only; keep board + default post channel.
        for t in EVENT_TYPES:
            db.set_config(f"{CFG_ROLE_PREFIX}{t.key}", "")
            db.set_config(f"{CFG_CHAN_PREFIX}{t.key}", "")

    summary: dict[str, str] = {}

    # ── Board + general post channel ─────────────────────────────────────
    # No category exclusion here: board channels often live in
    # "important" / "announcements", which we DO want to allow.
    if not db.get_config(CFG_BOARD_CHANNEL):
        m = db.find_channel_by_keywords(gid, list(BOARD_CHANNEL_KEYWORDS), kind="text")
        if m:
            db.set_config(CFG_BOARD_CHANNEL, m["channel_id"])
    if not db.get_config(CFG_LFG_CHANNEL):
        m = db.find_channel_by_keywords(gid, list(POST_CHANNEL_KEYWORDS), kind="text")
        if m:
            db.set_config(CFG_LFG_CHANNEL, m["channel_id"])

    summary["Board channel"] = (
        f"<#{db.get_config(CFG_BOARD_CHANNEL)}>" if db.get_config(CFG_BOARD_CHANNEL) else "_not found_"
    )
    summary["Default post channel"] = (
        f"<#{db.get_config(CFG_LFG_CHANNEL)}>" if db.get_config(CFG_LFG_CHANNEL) else "_not found_"
    )

    # ── Per event-type role + channel override ───────────────────────────
    for t in EVENT_TYPES:
        if not t.role_keywords and not t.channel_keywords:
            continue
        role_key = f"{CFG_ROLE_PREFIX}{t.key}"
        chan_key = f"{CFG_CHAN_PREFIX}{t.key}"

        if not db.get_config(role_key):
            m = db.find_role_by_keywords(gid, list(t.role_keywords))
            if m:
                db.set_config(role_key, m["role_id"])
        if not db.get_config(chan_key):
            m = db.find_channel_by_keywords(
                gid,
                list(t.channel_keywords),
                kind="text",
                exclude_categories=list(PER_TYPE_EXCLUDE_CATEGORIES),
            )
            if m:
                db.set_config(chan_key, m["channel_id"])

        rid = db.get_config(role_key)
        cid = db.get_config(chan_key)
        summary[f"{t.emoji} {t.label}"] = (
            f"role: {('<@&' + rid + '>') if rid else '_none_'} · "
            f"channel: {('<#' + cid + '>') if cid else '_default_'}"
        )

    return summary


# PRIME_SLOT_COLORS is imported from :mod:`cogs._lfg_config`.


def _slot_for_label(slot_label: str) -> PrimeSlot | None:
    """Return the :class:`PrimeSlot` matching a stored ``slot_label`` like
    ``"PRIME 18:00-19:00"``, or ``None`` for General LFG / unknown labels.
    """
    return prime_slot_for_label(slot_label)


async def _create_discord_scheduled_event(
    guild: discord.Guild,
    *,
    name: str,
    description: str,
    starts_at: datetime.datetime,
    ends_at: datetime.datetime,
    location: str,
) -> discord.ScheduledEvent | None:
    """Create a native Discord scheduled event (the in-client event tracker)
    so members can hit "Interested" from the server header. Returns the
    created event, or ``None`` on failure (logged, not raised — LFG posting
    must keep working even if the bot lacks ``manage_events``).

    Discord requires ``start_time`` strictly in the future, so we bump it
    forward by 30s if the slot is essentially "now". ``end_time`` is required
    for external-location events.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if starts_at <= now:
        starts_at = now + datetime.timedelta(seconds=30)
    if ends_at <= starts_at:
        ends_at = starts_at + datetime.timedelta(hours=1)
    # Discord caps description at 1000 chars.
    desc = (description or "")[:1000]
    try:
        return await guild.create_scheduled_event(
            name=name[:100],
            description=desc,
            start_time=starts_at,
            end_time=ends_at,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location=location[:100] or "Albion Online",
        )
    except discord.Forbidden:
        warning_log(
            "create_scheduled_event: missing 'Manage Events' permission; "
            "LFG event posted without a Discord scheduled-event entry."
        )
    except (discord.HTTPException, ValueError) as exc:
        error_log(f"create_scheduled_event failed: {exc!r}")
    return None


def _discussion_thread_name(event: dict) -> str:
    title = " ".join(str(event.get("title") or "Event discussion").split())
    prefix = f"LFG #{event.get('id', '?')} - "
    limit = max(1, 100 - len(prefix))
    if len(title) > limit:
        title = title[: limit - 3].rstrip() + "..."
    return prefix + title


async def _create_lfg_discussion_thread(db, event: dict, message: discord.Message) -> discord.Thread | None:
    """Create and remember the discussion thread for a posted LFG event.

    Thread creation is best-effort: the event post should remain live even if
    Discord rejects the thread due to permissions or channel settings.
    """
    if event.get("discussion_thread_id"):
        return None
    event_id = int(event["id"])
    try:
        thread = await message.create_thread(
            name=_discussion_thread_name(event),
            auto_archive_duration=1440,
            reason=f"LFG event #{event_id} discussion",
        )
    except discord.Forbidden:
        warning_log(
            "create_lfg_discussion_thread: missing thread permission; "
            f"LFG event #{event_id} posted without a discussion thread."
        )
        return None
    except discord.HTTPException as exc:
        error_log(f"create_lfg_discussion_thread failed for event #{event_id}: {exc!r}")
        return None

    try:
        db.set_lfg_discussion_thread_id(event_id, str(thread.id))
    except Exception as exc:  # noqa: BLE001
        error_log(f"set_lfg_discussion_thread_id failed for event #{event_id}: {exc!r}")
        return thread

    try:
        refreshed = db.fetch_lfg_event(event_id) or {**event, "discussion_thread_id": str(thread.id)}
        await message.edit(embed=_format_event_embed(db, refreshed))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"LFG discussion thread embed refresh failed for event #{event_id}: {exc!r}")
    return thread


def _get_post_channel_for_type(db, guild: discord.Guild, event_type: str | None) -> discord.TextChannel | None:
    """Resolve which channel a posted event should go in.

    Order: per-type override → general LFG channel → None.
    """
    cid: str | None = None
    if event_type:
        cid = db.get_config(f"{CFG_CHAN_PREFIX}{event_type}")
        if not cid:
            canonical = canonical_event_type_key(event_type)
            if canonical and canonical != event_type:
                cid = db.get_config(f"{CFG_CHAN_PREFIX}{canonical}")
    if not cid:
        cid = db.get_config(CFG_LFG_CHANNEL)
    if not cid:
        return None
    try:
        ch = guild.get_channel(int(cid))
        if isinstance(ch, discord.TextChannel):
            return ch
    except (TypeError, ValueError):
        pass
    return None


def _get_ping_for_type(db, event_type: str | None) -> str | None:
    """Return a role mention string to ping when posting, or None."""
    if not event_type:
        return None
    rid = db.get_config(f"{CFG_ROLE_PREFIX}{event_type}")
    if not rid:
        canonical = canonical_event_type_key(event_type)
        if canonical and canonical != event_type:
            rid = db.get_config(f"{CFG_ROLE_PREFIX}{canonical}")
    return f"<@&{rid}>" if rid else None


# Compact role → emoji mapping for the slot grid. Falls back to ⚔️ for
# anything unmapped so officers can use freeform role names.
_ROLE_EMOJI = {
    "tank": "🛡️",
    "off tank": "🛡️",
    "bruiser": "⚔️",
    "melee dps": "⚔️",
    "ranged dps": "🏹",
    "ranged": "🏹",
    "healer": "💚",
    "main healer": "💚",
    "support": "✨",
    "utility": "✨",
    "scout": "🦅",
    "caller": "📣",
    "shotcaller": "📣",
}


def _emoji_for_role(role: str | None) -> str:
    if not role:
        return "⚔️"
    return _ROLE_EMOJI.get(role.strip().lower(), "⚔️")


def _format_build_briefing(
    db, event: dict, slot: dict,
) -> discord.Embed:
    """Rich ephemeral embed showing the player exactly what to bring for a
    given comp slot: full gear loadout, food/potion, IP floor, the slot's
    role + notes (the 'job description'), and the comp-level strategy
    description so they understand the bigger picture.

    Safe to call with sparse data — empty fields are shown as ``—`` so the
    player still sees the slot exists.
    """
    role = slot.get("role") or "Slot"
    weapon = slot.get("weapon") or "?"
    emoji = _emoji_for_role(role)
    e = discord.Embed(
        title=f"{emoji} Your build — {role} · {weapon}",
        description=(
            f"Event **#{event['id']} — {event.get('title') or ''}**\n"
            "Show up wearing exactly this. Swap parts only with officer approval."
        ),
        color=discord.Color.from_str("#2ecc71"),
    )
    # Gear column 1: weapons & body.
    gear_lines = [
        f"**Weapon:** {slot.get('weapon') or '—'}",
        f"**Offhand:** {slot.get('offhand') or '— (two-handed)'}",
        f"**Head:** {slot.get('head') or '—'}",
        f"**Chest:** {slot.get('chest') or '—'}",
        f"**Boots:** {slot.get('shoes') or '—'}",
        f"**Cape:** {slot.get('cape') or '—'}",
    ]
    e.add_field(name="🛡️ Gear", value="\n".join(gear_lines), inline=True)
    # Column 2: consumables, mount, IP floor.
    consum_lines = [
        f"**Food:** {slot.get('food') or '—'}",
        f"**Potion:** {slot.get('potion') or '—'}",
        f"**Mount:** {slot.get('mount') or '—'}",
        f"**Min IP:** {slot.get('ip_min') or 0}",
        f"**Build type:** {slot.get('build_type') or '—'}",
        f"**Required:** {'✅ Yes' if int(slot.get('required') or 1) else '❌ Optional'}",
    ]
    e.add_field(name="🧪 Consumables & IP", value="\n".join(consum_lines), inline=True)
    # Approved swaps — gear alternates that still fulfil the role.
    if slot.get("swaps"):
        e.add_field(
            name="🔁 Approved swaps",
            value=str(slot["swaps"])[:1024],
            inline=False,
        )
    # Job description: slot.notes is the per-role instructions; comp.description
    # is the overall battle plan / strategy.
    if slot.get("notes"):
        e.add_field(
            name="📋 Your job",
            value=str(slot["notes"])[:1024],
            inline=False,
        )
    comp_id = event.get("comp_id")
    if comp_id:
        try:
            comp = db.fetch_comp(int(comp_id)) or {}
        except Exception:  # noqa: BLE001
            comp = {}
        if comp.get("description"):
            e.add_field(
                name=f"🎯 Strategy — {comp.get('name') or 'comp'}",
                value=str(comp["description"])[:1024],
                inline=False,
            )
    e.set_footer(text=f"Slot #{slot.get('slot_id', '?')} · Event #{event['id']}")
    return e


def _format_event_embed(db, event: dict) -> discord.Embed:
    """Build the embed shown on a posted event message. Reads live signups from DB."""
    starts = datetime.datetime.fromisoformat(event["starts_at"])
    ends = datetime.datetime.fromisoformat(event["ends_at"])
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=datetime.timezone.utc)
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=datetime.timezone.utc)
    prep_min = int(event.get("prep_minutes") or PREP_MINUTES)
    review_min = int(event.get("review_minutes") or REVIEW_MINUTES)
    prep_at = starts - datetime.timedelta(minutes=prep_min)
    review_until = ends + datetime.timedelta(minutes=review_min)

    color = discord.Color.from_str("#e67e22") if event["is_prime"] else discord.Color.from_str("#3498db")
    tag = "🟧 PRIME" if event["is_prime"] else "🟦 GENERAL"
    if event["is_prime"]:
        slot = _slot_for_label(event.get("slot_label") or "")
        if slot is not None:
            color = PRIME_SLOT_COLORS.get(slot.emoji, color)
            tag = f"{slot.emoji} PRIME"
    title = f"{tag} — {event['title']}"

    e = discord.Embed(title=title, description=event.get("description") or "—", color=color)
    e.add_field(
        name="When",
        value=(
            "**Your local time:**\n"
            f"Prep: <t:{int(prep_at.timestamp())}:t>\n"
            f"Start: <t:{int(starts.timestamp())}:F>\n"
            f"End: <t:{int(ends.timestamp())}:t>\n"
            f"VOD review until: <t:{int(review_until.timestamp())}:t>\n\n"
            "**Albion/UTC:** "
            f"`{starts.astimezone(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M')}`"
            f" to `{ends.astimezone(datetime.timezone.utc).strftime('%H:%M')} UTC`"
        ),
        inline=False,
    )
    e.add_field(name="Slot", value=display_slot_label(event.get("slot_label")), inline=True)
    e.add_field(name="Created by", value=f"<@{event['creator_id']}>", inline=True)
    ip_req = _normalize_ip_requirement(
        event.get("ip_requirement"),
        allow_bare=True,
    ) or _extract_ip_requirement(
        event.get("comp_notes"),
        event.get("description"),
        event.get("title"),
    )
    if ip_req:
        e.add_field(name="Minimum IP", value=ip_req, inline=True)
    if event.get("comp_notes"):
        e.add_field(name="Comp / requirements", value=event["comp_notes"], inline=False)
    if event.get("discussion_thread_id"):
        e.add_field(
            name="Discussion",
            value=f"<#{event['discussion_thread_id']}>",
            inline=True,
        )
    if event.get("voice_channel_id") and not event.get("voice_channel_deleted_at"):
        e.add_field(
            name="Voice",
            value=f"<#{event['voice_channel_id']}>",
            inline=True,
        )

    # ── Comp slot grid (when a comp is attached) ─────────────────────────
    comp_id = event.get("comp_id")
    comp_name = None
    grid: list[dict] = []
    if comp_id:
        try:
            comp = db.fetch_comp(int(comp_id))
            comp_name = (comp or {}).get("name")
            grid = db.fetch_lfg_slot_grid(event["id"])
        except Exception:  # noqa: BLE001
            grid = []

    signups = db.fetch_lfg_signups(event["id"])
    claimed_ids = {str(s.get("discord_id")) for s in signups if s.get("slot_id")}
    unassigned = [s for s in signups
                  if str(s.get("discord_id")) not in claimed_ids]

    if grid:
        # Group by role to keep the field readable; cap at ~1000 chars.
        from collections import defaultdict
        by_role: dict[str, list[str]] = defaultdict(list)
        filled = 0
        total = len(grid)
        for r in grid:
            who = r.get("claimed_by")
            weapon = r.get("weapon") or "?"
            if who:
                filled += 1
                line = f"• {weapon} → <@{who}>"
            else:
                line = f"• {weapon} — _open_"
            by_role[r.get("role") or "Other"].append(line)

        chunks: list[str] = []
        for role, lines in by_role.items():
            emoji = _emoji_for_role(role)
            chunks.append(f"**{emoji} {role}** ({len(lines)})")
            chunks.extend(lines)
            chunks.append("")
        body = "\n".join(chunks).rstrip()
        if len(body) > 1000:
            body = body[:980].rsplit("\n", 1)[0] + "\n…(truncated)"
        e.add_field(
            name=(
                f"🧩 Build assignments — {comp_name or 'comp'}  "
                f"({filled}/{total} filled)"
            ),
            value=body or "_No slots in this comp._",
            inline=False,
        )

    # Roster (people without a specific build claim, or everyone if no comp).
    list_to_show = unassigned if grid else signups
    label = "Reserves / no build claimed" if grid else "Signed up"
    if list_to_show:
        lines = [f"• <@{s['discord_id']}>" for s in list_to_show]
        roster = "\n".join(lines)
        if len(roster) > 1000:
            head = lines[:35]
            roster = "\n".join(head) + f"\n…and **{len(lines) - len(head)}** more"
    else:
        roster = "_No one yet_" if grid else "_No signups yet_"
    e.add_field(
        name=f"{label} ({len(list_to_show)})",
        value=roster, inline=False,
    )

    e.set_footer(text=f"Event #{event['id']} — Status: {event['status']}")
    return e
