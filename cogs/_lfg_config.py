"""Pure-data configuration for the LFG / event board cog.

Everything in this module is static configuration: dataclasses, role-name
sets, layout templates, channel intros, permission overwrites, and constant
keys. No I/O, no Discord API calls, no DB access. Imported by
:mod:`cogs.lfg`.

The file is prefixed with ``_`` so :func:`bot._load_cogs` skips it during
extension auto-discovery — only ``cogs/<name>.py`` (no underscore) gets
loaded as a cog.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import discord


# ── Prime-time slots ────────────────────────────────────────────────────────
# Emoji colors mirror the in-game Albion prime-time palette. Edit the
# :data:`PRIME_SLOTS` list to change the panel layout.
@dataclass(frozen=True)
class PrimeSlot:
    start_hour: int
    end_hour: int
    emoji: str = "\U0001F7E7"  # default orange square

    @property
    def label(self) -> str:
        return f"{self.start_hour:02d}:00-{self.end_hour:02d}:00"

    @property
    def slot_id(self) -> str:
        return f"prime_{self.start_hour:02d}_{self.end_hour:02d}"


PRIME_SLOTS: list[PrimeSlot] = [
    PrimeSlot(18, 19, "\U0001F7E5"),  # red
    PrimeSlot(20, 21, "\U0001F7E7"),  # orange
    PrimeSlot(22, 23, "\U0001F7E8"),  # yellow
    PrimeSlot(0, 1, "\U0001F7E9"),    # green
    PrimeSlot(2, 3, "\U0001F7E6"),    # blue (teal closest)
    PrimeSlot(4, 5, "\U0001F7EA"),    # purple
]


def compact_prime_slot_range(slot: PrimeSlot) -> str:
    return f"{slot.start_hour:02d}-{slot.end_hour:02d}"


def prime_slot_display_label(slot: PrimeSlot) -> str:
    return f"UTC {compact_prime_slot_range(slot)}"


def display_slot_label(slot_label: str | None) -> str:
    text = str(slot_label or "").strip()
    if not text:
        return "Unknown"
    for slot in PRIME_SLOTS:
        if slot.label in text or prime_slot_display_label(slot) in text:
            return prime_slot_display_label(slot)
    if text.upper() == "GENERAL":
        return "General LFG"
    return text


def prime_first_hour() -> int:
    return PRIME_SLOTS[0].start_hour if PRIME_SLOTS else 18


def prime_timer_rollover_hour() -> int:
    first_hour = prime_first_hour()
    after_midnight_ends = [
        slot.end_hour for slot in PRIME_SLOTS if slot.start_hour < first_hour
    ]
    return max(after_midnight_ends, default=0)


def utc_datetime(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def prime_timer_cycle_date(now: dt.datetime) -> dt.date:
    now = utc_datetime(now)
    rollover = prime_timer_rollover_hour()
    if rollover and now.hour < rollover:
        return now.date() - dt.timedelta(days=1)
    return now.date()


def prime_timer_cycle_start(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(
        day,
        dt.time(prime_first_hour(), tzinfo=dt.timezone.utc),
    )


def prime_timer_cycle_end(day: dt.date) -> dt.datetime:
    rollover = prime_timer_rollover_hour()
    return dt.datetime.combine(
        day + dt.timedelta(days=1),
        dt.time(rollover, tzinfo=dt.timezone.utc),
    )


def prime_slot_for_label(slot_label: str | None) -> PrimeSlot | None:
    text = str(slot_label or "")
    for slot in PRIME_SLOTS:
        if slot.label in text or prime_slot_display_label(slot) in text:
            return slot
    return None


def prime_slot_start_for_day(day: dt.date, slot: PrimeSlot) -> dt.datetime:
    slot_day = day + dt.timedelta(days=1 if slot.start_hour < prime_first_hour() else 0)
    return dt.datetime.combine(
        slot_day,
        dt.time(slot.start_hour, tzinfo=dt.timezone.utc),
    )


def prime_slot_window_on_date(
    date_utc: dt.date, slot: PrimeSlot,
) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(
        date_utc,
        dt.time(slot.start_hour, tzinfo=dt.timezone.utc),
    )
    duration_hours = (slot.end_hour - slot.start_hour) % 24 or 1
    return start, start + dt.timedelta(hours=duration_hours)


def next_prime_slot_window(
    slot: PrimeSlot,
    now: dt.datetime | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    now = utc_datetime(now or dt.datetime.now(dt.timezone.utc))
    next_start = now.replace(hour=slot.start_hour, minute=0, second=0, microsecond=0)
    if next_start <= now:
        next_start += dt.timedelta(days=1)
    return prime_slot_window_on_date(next_start.date(), slot)


def prime_slot_window_for_day(
    day: dt.date, slot: PrimeSlot,
) -> tuple[dt.datetime, dt.datetime]:
    start = prime_slot_start_for_day(day, slot)
    end_day = start.date() + dt.timedelta(days=1 if slot.end_hour <= slot.start_hour else 0)
    end = dt.datetime.combine(
        end_day,
        dt.time(slot.end_hour, tzinfo=dt.timezone.utc),
    )
    return start, end


def prime_slot_for_time(now: dt.datetime) -> PrimeSlot | None:
    now = utc_datetime(now)
    clock_minutes = now.hour * 60 + now.minute
    for slot in PRIME_SLOTS:
        start = slot.start_hour * 60
        end = slot.end_hour * 60
        if end <= start:
            in_window = clock_minutes >= start or clock_minutes < end
        else:
            in_window = start <= clock_minutes < end
        if in_window:
            return slot
    return None


def prime_timer_status_emoji(
    now: dt.datetime,
    *,
    between_emoji: str = "⏳",
    off_emoji: str = "💤",
) -> str:
    slot = prime_slot_for_time(now)
    if slot:
        return slot.emoji

    now = utc_datetime(now)
    rollover = prime_timer_rollover_hour()
    if now.hour >= prime_first_hour() or (rollover and now.hour < rollover):
        return between_emoji
    return off_emoji


# Map a slot's emoji to a matching embed color so posted events visually
# match the in-game prime-time palette shown on the board.
PRIME_SLOT_COLORS: dict[str, discord.Color] = {
    "\U0001F7E5": discord.Color.red(),
    "\U0001F7E7": discord.Color.orange(),
    "\U0001F7E8": discord.Color.gold(),
    "\U0001F7E9": discord.Color.green(),
    "\U0001F7E6": discord.Color.blue(),
    "\U0001F7EA": discord.Color.purple(),
}


# ── Role gates ──────────────────────────────────────────────────────────────
# Roles allowed to create a prime-time event.
PRIME_CREATOR_ROLES = {
    "Shotcaller", "Senior Shotcaller", "Officer", "Captain",
    "Commander", "Guild Leader", "Alliance Leader",
}

# Roles allowed to cancel an event they did NOT create.
# Officers and above only — Shotcallers can create prime slots but cannot
# cancel another user's event.
CANCEL_OVERRIDE_ROLES = {
    "Officer", "Captain", "Commander", "Guild Leader", "Alliance Leader",
}


# ── Staff-role permission scheme (applied via /lfg apply-staff-perms) ───────
# Maps a role *name* (case-insensitive match against guild.roles) to a list
# of discord.Permissions flag names. Order is from least → most authority so
# the table reads top-down like a promotion ladder. Roles not listed here
# are left untouched. The bot needs `manage_roles` AND a top role positioned
# above every target role for edits to succeed.
STAFF_PERMISSION_SCHEME: dict[str, tuple[str, ...]] = {
    # Members and veterans — just enough to bring friends in.
    "Member": ("create_instant_invite",),
    "Veteran": ("create_instant_invite",),
    # Recruiter — onboarding ops only (text-side).
    "Recruiter": (
        "create_instant_invite", "manage_nicknames",
    ),
    # Shotcaller — ping authority during content.
    "Shotcaller": (
        "create_instant_invite",
        "mention_everyone",
    ),
    # Senior Shotcaller — adds light text-channel moderation + events.
    "Senior Shotcaller": (
        "create_instant_invite",
        "mention_everyone",
        "manage_messages", "manage_threads", "manage_events",
    ),
    # Squad Leader is an onboarding-duty badge, not a moderation power role.
    "Squad Leader": (),
    # Officer — adds kick + timeout + audit visibility.
    "Officer": (
        "create_instant_invite",
        "mention_everyone",
        "manage_messages", "manage_threads", "manage_events",
        "kick_members", "moderate_members", "view_audit_log",
    ),
    # Captain — adds ban authority and nickname management.
    "Captain": (
        "create_instant_invite",
        "mention_everyone",
        "manage_messages", "manage_threads", "manage_events",
        "kick_members", "moderate_members", "view_audit_log",
        "ban_members", "manage_nicknames",
    ),
    # Commander — top staff below GL: full server-shape control short of admin.
    "Commander": (
        "create_instant_invite",
        "mention_everyone",
        "manage_messages", "manage_threads", "manage_events",
        "kick_members", "moderate_members", "view_audit_log",
        "ban_members", "manage_nicknames",
        "manage_roles", "manage_channels", "manage_webhooks",
    ),
}


# ── Recommended channel layout ──────────────────────────────────────────────
# Drives /lfg propose-layout and /lfg apply-layout. Tuned for the Travelers
# Union guild structure observed in data/guild-scan-*.txt — preserves what
# already works, adds Content Chat + a member-visible bot-commands, and
# splits voice into Social / Content. The layout system *only* creates new
# stuff and moves channels by exact-name match; it never deletes or renames.
# Channels not listed here are left untouched (reported as "not in template").
#
# Format: (category_name, [(channel_name, kind), ...]). ``kind`` is "text"
# or "voice"; categories are inferred from the outer tuple.
DESIRED_LAYOUT: list[tuple[str, list[tuple[str, str]]]] = [
    ("👋 START HERE", [
        ("📜-rules", "text"),
        ("📝-register-here", "text"),
        ("🛡️-apply-to-guild", "text"),
        ("📝-staff-applications", "text"),
        ("❓-help-ticket", "text"),
    ]),
    ("📌 Union Board", [
        ("📢-announcements", "text"),
        ("📅-event-board", "text"),
        ("🔎-looking-for-group", "text"),
        ("🎭-content-roles", "text"),
        ("🗳️-votes", "text"),
    ]),
    ("🌐 UOT Alliance", [
        ("ℹ️-alliance-info", "text"),
        ("📢-alliance-announcements", "text"),
        ("📅-alliance-events", "text"),
        ("💬-alliance-chat", "text"),
        ("👑-guild-leaders", "text"),
        ("🌐 Alliance Lounge", "voice"),
    ]),
    ("🐏 Martlock Faction", [
        ("ℹ️-martlock-info", "text"),
        ("🔎-martlock-lfg", "text"),
        ("💬-faction-chat", "text"),
        ("🧩-martlock-comps", "text"),
        ("⚔️ Faction War Lounge", "voice"),
    ]),
    ("⚔️ Content Ops", [
        ("⚔️-content-planning", "text"),
        ("📣-shotcalling-sop", "text"),
        ("🧩-comps-and-builds", "text"),
        ("💰-regear-policy", "text"),
        ("🧾-regear-request", "text"),
    ]),
    ("Union Hall", [
        ("💪-flex", "text"),
        ("🇬🇧-english-chat", "text"),
        ("🇪🇸-spanish-chat", "text"),
        ("🇵🇹-portuguese-chat", "text"),
        ("🏆-hall-of-fame", "text"),
        ("📖-union-lore", "text"),
    ]),
    ("📚 Resources", [
        ("📰-albion-patch-notes-and-news", "text"),
        ("🐎-sso-routes", "text"),
        ("🛒-union-market", "text"),
        ("🔨-crafting", "text"),
        ("💰-bounty-board", "text"),
        ("🪲-bugs", "text"),
        ("🔥-member-suggestions", "text"),
    ]),
    ("🤝 Guests", [
        ("🚪-welcome", "text"),
        ("✌️-goodbye", "text"),
        ("ℹ️-guest-info", "text"),
        ("💬-guest-chat", "text"),
        ("🤝 Guest Lounge", "voice"),
    ]),
    ("🔊 Voice", [
        ("🛋️ Travelers Lounge", "voice"),
        ("Event Lounge", "voice"),
        ("💤 AFK", "voice"),
    ]),
    ("🛡️ Guild Feed", [
        ("🏹︱kill-bot", "text"),
        ("💀︱death-bot", "text"),
        ("📡︱activity-feed", "text"),
    ]),
    ("👑 Leadership", [
        ("🤖-bot-commands", "text"),
        ("📡｜command-feed", "text"),
        ("📋-officer-tasks", "text"),
        ("⭐-officer-chat", "text"),
        ("🏛-admin-office", "text"),
        ("🧭-officer-help", "text"),
        ("👑 Leadership Lounge", "voice"),
    ]),
]


# ── Channel intro / pinned-message text ─────────────────────────────────────
# Short blurb pinned at the top of each text channel describing what's
# discussed there. Keys are the emoji-prefixed channel names from
# :data:`DESIRED_LAYOUT`. Voice channels are skipped (no message history).
# ``/lfg pin-intros`` posts (or re-posts) and pins one message per channel.

CHANNEL_INTROS: dict[str, str] = {
    # Welcome / Verify
    "📜-rules": (
        "**Guild & server rules.** Read before participating. "
        "Breaking these = warn → mute → kick. Ask an Officer if anything is unclear."
    ),
    "📝-register-here": (
        "**Link your Albion character.** Click the **Register** button below "
        "and enter your in-game name so the bot can track your fame, attendance, "
        "and regear eligibility."
    ),
    "🛡️-apply-to-guild": (
        "**Not in the guild yet?** Post a short intro: IGN, total fame, timezone, "
        "preferred content (ZvZ / ganking / mists / gathering), and what you're looking for."
    ),
    "📝-command-applications": (
        "**Apply for staff ranks** (Shotcaller, Officer, Captain, etc.). "
        "Use `/staff apply` or click the Apply button on the staff board."
    ),
    # Important
    "📢-announcements": (
        "**Official guild announcements.** Read-only for members. "
        "Patch impacts, leadership changes, big wins, and policy updates land here."
    ),
    "📅-weekly-events": (
        "**Scheduled guild events** — ZvZ calls, CTAs, mist nights, gank parties, "
        "fame trains. Sign up via the event embed or `/signup`."
    ),
    "📅-event-board": (
        "**Guild event board.** Claim prime timers, post LFGs, and sign up for "
        "planned content from here."
    ),
    "🔎-looking-for-group": (
        "**LFG board.** Looking for a group or have spots open? Use `/lfg post` "
        "(content type, IP requirement, voice channel, time). Don't ping `@everyone`."
    ),
    "📣-shotcalling-sop": (
        "**Standard Operating Procedures for shotcallers.** Comp templates, "
        "engage rules, retreat triggers, target priority. Read before calling a party."
    ),
    "⚔️-content-planning": (
        "**Content planning hub.** Use this for comp planning, timer ideas, "
        "shotcaller coordination, and follow-up from LFG threads."
    ),
    "🧩-comps-and-builds": (
        "**Comps and builds.** Post group comps, build links, role requirements, "
        "IP floors, and swaps here so LFG posts can stay clean."
    ),
    "💰-regear-policy": (
        "**Who gets regeared and for what.** Eligible content, IP caps, kit limits, "
        "and the proof requirements. Read before requesting a regear."
    ),
    "🧾-regear-request": (
        "**Submit regear requests here.** Death recap screenshot + content type + "
        "shotcaller name. Officers process these weekly."
    ),
    "🎭-content-roles": (
        "**Pick the content roles you want pinged for** (ZvZ, Ganking, Mists, "
        "Hellgates, etc.). React to the role board to opt in/out."
    ),
    "📰-albion-patch-notes-and-news": (
        "**Albion patch notes, dev posts, and meta news.** Drop links + a one-line "
        "summary. Discussion goes in the relevant content channel."
    ),
    # General
    "🗣️-forum": (
        "**Long-form discussion.** Build theorycraft, meta debates, suggestions for "
        "the guild. Off-topic / shitposting → other channels."
    ),
    "🇬🇧-english-chat": "**General chat — English.** Keep it civil. Albion-related or off-topic both fine.",
    "🇪🇸-spanish-chat": "**Chat general — Español.** Mantenlo amistoso. Albion u off-topic, ambos están bien.",
    "🇵🇹-portuguese-chat": "**Bate-papo geral — Português.** Mantenha amigável. Albion ou off-topic, tudo certo.",
    "🐎-sso-routes": (
        "**Saddled Swiftclaw / mount-leveling routes.** Share safe SSO loops, "
        "T8 mount progress, and call out where the gankers are camping."
    ),
    "🏴‍☠️-bandit": (
        "**Bandit Assault tracking.** Report sightings, share spawn timers, "
        "coordinate response parties. Loot splits per shotcaller's call."
    ),
    "🗳️-votes": "**Guild polls and votes.** Officers post; members react. Results are binding unless stated otherwise.",
    "🤖-bot-commands": (
        "**Bot commands channel.** Run `/signup`, `/lfg`, `/leaderboard`, "
        "`/profile`, etc. here so other channels stay readable."
    ),
    # Content Chat
    "⚔️-zvz": (
        "**Zerg vs Zerg.** Comps, callouts, post-fight VODs, ball discipline, "
        "gear loadouts. Sign up for ZvZ CTAs in #📅-weekly-events."
    ),
    "🗡️-ganking": (
        "**Ganking & dive squads.** Spot calls, flagging strats, T8 mount kills, "
        "roads/BZ activity reports. Drop juicy kill screenshots."
    ),
    "🔥-hellgates": (
        "**2v2 and 5v5 Hellgates.** LF2M / LF5M, comp questions, demon shard splits, "
        "best maps for the current meta."
    ),
    "🌫️-mists": (
        "**Mists / Lethal Mists.** Solo and duo content. Build advice, fame-per-hour, "
        "fairy / wisp routes, Wisplighter farm, Knightfall info."
    ),
    "🛣️-ava-roads": (
        "**Avalonian Roads.** Map opens, chest splits, dungeon clears, and roams. "
        "Bring fightable content here for a quick callout."
    ),
    "🌟-fame-farm": (
        "**Fame farming.** Group dungeon spots, T8 mob clears, books, learning-point "
        "farms. Share IP requirements when asking for a group."
    ),
    "💎-crystal-arena": (
        "**Crystal Arena & Crystal League.** Comp practice, scrim partners, "
        "match VODs, ranked queue talk."
    ),
    "🏰-hce": (
        "**Hardcore Expeditions.** Group LFG, role splits, key economy, "
        "best comps for current modifiers."
    ),
    "⛏️-gathering": (
        "**Gathering & refining.** Resource hotspots, T8 node tracking, "
        "pack-mount safety, refining bonus info, market prices."
    ),
    # Guides
    "❓-help": (
        "**Ask anything Albion-related.** New player questions, build help, "
        "market tips, where to find things. No question is too dumb."
    ),
    # Community
    "💪-flex": "**Flex your wins.** Big kills, dye drops, tome stacks, world-boss loot. Screenshot or it didn't happen.",
    "🏆-hall-of-fame": "**Guild Hall of Fame.** Officer-curated highlights — biggest kills, MVP performances, milestone moments.",
    "📸-pics-irl": "**Pics IRL.** Pets, plates, places. Keep it SFW.",
    "🎵-music-time": "**Music sharing.** What you're listening to while you grind. Spotify / YouTube links welcome.",
    "📖-union-lore": "**Guild lore, RP, and history.** Origin stories, member legends, in-character writing.",
    "🛒-union-market": "**Internal guild marketplace.** Members buy/sell/trade gear, mounts, mats. Format: `[WTS/WTB] item — price`.",
    "🔨-crafting": "**Crafting & refining services.** Crafters list specialties + focus rates. Buyers post requests with mats provided.",
    "💰-bounty-board": "**Bounty board.** Guild-funded targets, shopping requests, and active reward contracts.",
    "🪲-bugs": "**Bot and server bug reports.** Include what happened, where, and a screenshot when possible.",
    "🔥-member-suggestions": "**Member suggestions.** Ideas for guild systems, Discord cleanup, events, and bot improvements.",
    "ℹ️-alliance-info": "**UOT alliance information.** Requirements, expectations, contacts, and current direction.",
    "📢-alliance-announcements": "**Alliance announcements.** Important UOT-wide updates and leadership notices.",
    "📅-alliance-events": "**Alliance events.** Cross-guild content planning and event posts.",
    "💬-alliance-chat": "**Alliance chat.** Day-to-day UOT coordination and discussion.",
    "👑-guild-leaders": "**Guild leader coordination.** Alliance leadership planning and escalations.",
    "ℹ️-martlock-info": "**Martlock faction information.** How we organize Martlock faction content from this server.",
    "🔎-martlock-lfg": "**Martlock faction LFG.** Faction Warfare events and signups post here.",
    "💬-faction-chat": "**Faction chat.** Martlock calls, questions, and faction coordination.",
    "🧩-martlock-comps": "**Martlock comps.** Faction Warfare builds, swaps, and IP expectations.",
    # Leadership
    "📋-officer-tasks": (
        "**Officer task board & application notifications.** Pending staff "
        "applications, regear queue, member issues. Officers/Captains only."
    ),
    # Guests
    "🚪-welcome": "**Welcome feed.** New member and visitor joins land here.",
    "✌️-goodbye": "**Goodbye feed.** Departures land here so staff can spot churn patterns.",
    "ℹ️-guest-info": (
        "**Welcome, guest!** This is a public area for friends, allies, and "
        "visitors. Read here for what's available to you on the server."
    ),
    "💬-guest-chat": "**Guest chat.** Hang out, ask questions, get to know the guild. No Albion account required.",
    # Diplomacy
    "ℹ️-diplomat-info": (
        "**Ambassador / diplomat info.** Channel access policy, how to reach "
        "our diplomats, what to escalate where."
    ),
    "💬-diplomat-chat": "**Diplomatic discussion.** Inter-guild coordination, alliance scheduling, conflict de-escalation.",
    "📰-alliance-news": "**Alliance announcements & cross-guild news.** Read-only for non-officers.",
}


# ── Category-level permission overwrites ────────────────────────────────────
# Applied when a category is **created** by /lfg apply-layout, and re-applied
# on existing template categories when ``apply_perms=True`` is passed to the
# command. Channels inherit category overwrites unless they have their own,
# so per-channel overwrites are intentionally rare here — it keeps the
# server permission model auditable from one place.
#
# Format: ``{category_name: [(role_name, allow_perms, deny_perms), ...]}``.
# ``allow_perms`` / ``deny_perms`` are tuples of discord.Permissions flag
# names. Roles that don't exist in the guild are skipped with a warning.
_SENSITIVE_DENY_ROLES = ("Guest", "Alliance", "Inactive", "Alumni")
_DORMANT_DENY_ROLES = ("Inactive", "Alumni")
_CHANNEL_ACCESS_DENIES = ("read_messages", "send_messages", "connect")


def _deny_channel_access(
    role_names: tuple[str, ...],
) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    return [(role_name, (), _CHANNEL_ACCESS_DENIES) for role_name in role_names]


LAYOUT_CATEGORY_OVERWRITES: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "👋 START HERE": [
        ("@everyone", ("read_messages", "read_message_history"), ()),
        ("Officer", ("manage_messages",), ()),
    ],
    # Members-only areas — @everyone hidden, HomeGuild (verified) sees.
    "📌 Union Board": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("read_messages", "send_messages"), ()),
        ("Commander", ("read_messages", "send_messages"), ()),
        ("Guild Leader", ("read_messages", "send_messages"), ()),
    ],
    "📌 Important": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("read_messages", "send_messages"), ()),
        ("Commander", ("read_messages", "send_messages"), ()),
        ("Guild Leader", ("read_messages", "send_messages"), ()),
    ],
    "🌐 UOT Alliance": [
        ("@everyone", (), ("read_messages", "connect")),
        *_deny_channel_access(("Guest", "Inactive", "Alumni")),
        ("Alliance", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Ambassador", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Officer", ("manage_messages",), ()),
    ],
    "🐏 Martlock Faction": [
        ("@everyone", ("read_messages", "read_message_history"), ("connect", "speak")),
        ("Faction Warfare", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Guest", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Alliance", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Officer", ("manage_messages",), ()),
    ],
    "⚔️ Content Ops": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Shotcaller", ("mention_everyone",), ()),
        ("Officer", ("manage_messages",), ()),
    ],
    "Union Hall": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "💬 General": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "📣 Content Chat": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "📚 Resources": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Officer", ("manage_messages",), ()),
    ],
    "🔊 Voice": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "connect", "speak"), ()),
    ],
    "🎉 Community": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "📚 Guides": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("send_messages",), ()),
    ],
    "🔊 Social Voice": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "connect", "speak"), ()),
    ],
    "🎮 Content Voice": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "connect", "speak"), ()),
    ],
    "🛡️ Guild Feed": [
        ("@everyone", (), ("read_messages",)),
        *_deny_channel_access(_SENSITIVE_DENY_ROLES),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("send_messages",), ()),
    ],
    # Staff-only.
    "👑 Leadership": [
        ("@everyone", (), ("read_messages", "connect")),
        *_deny_channel_access(_DORMANT_DENY_ROLES),
        ("Officer", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Commander", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "connect", "speak"), ()),
    ],
    # Public-facing — Guest role can chat, members can see, staff moderate.
    "🤝 Guests": [
        # Visible to everyone (no read deny). Posting limited to verified
        # tiers so randoms can't spam.
        ("@everyone", ("read_messages", "read_message_history"), ("send_messages", "connect", "speak")),
        ("Guest", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Alliance", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("HomeGuild", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Officer", ("manage_messages",), ()),
    ],
    # Old/noisy channels parked here. Keep them available to staff for audit,
    # but hidden from normal member, alliance, and guest browsing.
    "📦 Archive": [
        ("@everyone", (), ("read_messages", "connect")),
        *_deny_channel_access(_DORMANT_DENY_ROLES),
        ("Officer", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Commander", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "read_message_history", "connect", "speak"), ()),
    ],
    # Diplomat zone — hidden from members, visible to Ambassadors + leadership.
    "🌐 Diplomacy": [
        ("@everyone", (), ("read_messages", "connect")),
        *_deny_channel_access(_DORMANT_DENY_ROLES),
        ("Ambassador", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Officer", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Commander", ("read_messages", "send_messages", "connect", "speak"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "connect", "speak"), ()),
    ],
}

# Per-channel exceptions applied after category sync. Keep this list small:
# most channels should inherit from their category so the server remains easy
# to audit.
LAYOUT_CHANNEL_OVERWRITES: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "📝-staff-applications": [
        ("@everyone", (), ("read_messages", "send_messages", "connect")),
        ("Guest", (), ("read_messages", "send_messages", "connect")),
        ("Alliance", (), ("read_messages", "send_messages", "connect")),
        *_deny_channel_access(_DORMANT_DENY_ROLES),
        ("HomeGuild", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Officer", ("read_messages", "send_messages", "read_message_history", "manage_messages"), ()),
        ("Commander", ("read_messages", "send_messages", "read_message_history", "manage_messages"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "read_message_history", "manage_messages"), ()),
    ],
    "ℹ️-alliance-info": [
        ("@everyone", (), ("read_messages", "send_messages", "connect")),
        *_deny_channel_access(("Guest", "Inactive", "Alumni")),
        ("Alliance", ("read_messages", "read_message_history"), ("send_messages",)),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Ambassador", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Commander", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "📢-alliance-announcements": [
        ("@everyone", (), ("read_messages", "send_messages", "connect")),
        *_deny_channel_access(("Guest", "Inactive", "Alumni")),
        ("Alliance", ("read_messages", "read_message_history"), ("send_messages",)),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Ambassador", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Commander", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "👑-guild-leaders": [
        ("@everyone", (), ("read_messages", "send_messages", "connect")),
        ("Alliance", (), ("read_messages", "send_messages", "connect")),
        ("HomeGuild", (), ("read_messages", "send_messages", "connect")),
        *_deny_channel_access(_DORMANT_DENY_ROLES),
        ("Ambassador", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Officer", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Commander", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "🚪-welcome": [
        ("@everyone", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Guest", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Alliance", ("read_messages", "read_message_history"), ("send_messages",)),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "✌️-goodbye": [
        ("@everyone", (), ("read_messages", "send_messages", "connect")),
        ("Guest", (), ("read_messages", "send_messages", "connect")),
        ("Alliance", (), ("read_messages", "send_messages", "connect")),
        ("HomeGuild", (), ("read_messages", "send_messages", "connect")),
        *_deny_channel_access(_DORMANT_DENY_ROLES),
        ("Officer", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Commander", ("read_messages", "send_messages", "read_message_history"), ()),
        ("Guild Leader", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
    "ℹ️-guest-info": [
        ("@everyone", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Guest", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Alliance", ("read_messages", "read_message_history"), ("send_messages",)),
        ("HomeGuild", ("read_messages", "read_message_history"), ("send_messages",)),
        ("Officer", ("read_messages", "send_messages", "read_message_history"), ()),
    ],
}


# ── Event windows ───────────────────────────────────────────────────────────
# Default windows attached to every event.
PREP_MINUTES = 30
REVIEW_MINUTES = 15


# ── Config keys (stored in guild_config table) ──────────────────────────────
CFG_BOARD_CHANNEL = "lfg_board_channel_id"
CFG_LFG_CHANNEL = "lfg_post_channel_id"
# Per-event-type ping role (key = lfg_role_<EVENT_TYPE_KEY>, value = role id as str).
CFG_ROLE_PREFIX = "lfg_role_"
# Per-event-type post channel override (key = lfg_chan_<EVENT_TYPE_KEY>, value = channel id).
# Falls back to CFG_LFG_CHANNEL when no override is set for a given type.
CFG_CHAN_PREFIX = "lfg_chan_"
# Per-event-type temporary voice category override
# (key = lfg_voice_cat_<EVENT_TYPE_KEY>, value = category id).
# Falls back to the global event voice category when no override is set.
CFG_VOICE_CATEGORY_PREFIX = "lfg_voice_cat_"


# ── Event types ─────────────────────────────────────────────────────────────
# Each event type has a key (stable, used for DB and custom_ids), a display
# label, an emoji, a UI category (used to group types in the future two-step
# picker so we can stay under Discord's 25-option select limit), and keyword
# candidates used to auto-detect the matching role / channel from the cached
# Discord inventory on `/lfg auto-config`. Keep keywords specific enough that
# more-specific types don't get swallowed by broader ones.
@dataclass(frozen=True)
class EventType:
    key: str
    label: str
    emoji: str
    category: str
    role_keywords: tuple[str, ...]
    channel_keywords: tuple[str, ...]


# Tuned to the HOME GUILD role/channel layout but the keyword lists are
# generic enough to also match most other Albion guilds. Order = display order
# inside each category.
EVENT_TYPES: tuple[EventType, ...] = (
    # ── Alliance ───────────────────────────────────────────────────────────
    EventType("alliance", "Alliance Content", "🌐", "PvP — Combat",
              role_keywords=("alliance content", "alliance", "uot alliance"),
              channel_keywords=("alliance-events", "alliance events", "alliance")),
    # ── PvP ────────────────────────────────────────────────────────────────
    EventType("pvp", "PvP", "⚔️", "PvP — Combat",
              role_keywords=("pvp", "p v p", "combat"),
              channel_keywords=("zvz", "gank", "content", "pvp")),
    EventType("faction", "Faction Warfare", "🏴", "PvP — Combat",
              role_keywords=("faction warfare", "faction war", "faction"),
              channel_keywords=("faction events", "faction warfare", "faction war", "faction")),
    EventType("gank", "Ganking", "🗡️", "PvP — Small",
              role_keywords=("ganking", "gank"),
              channel_keywords=("gank", "bz roaming", "roaming")),
    EventType("small_scale", "Small Scale", "🛡️", "PvP — Small",
              role_keywords=("small scale", "smallscale", "smallman", "small man"),
              channel_keywords=("small scale", "smallscale", "bz roaming", "roaming")),
    EventType("zvz", "ZvZ", "⚔️", "PvP — Combat",
              role_keywords=("zvz", "massive", "large scale", "ball"),
              channel_keywords=("zvz", "massive", "ball-fight", "zergs")),
    EventType("hellgate", "Hellgates", "🔥", "PvP — Small",
              role_keywords=("hellgates", "hellgate", "hg"),
              channel_keywords=("hellgates", "hellgate", "hg")),
    EventType("crystal_arena", "Crystal Arena", "🏟️", "PvP — Small",
              role_keywords=("crystal arena", "arena"),
              channel_keywords=("arena",)),
    EventType("duo_mists", "Duo Mists", "🌫️", "PvP — Small",
              role_keywords=("duo mists", "mists duo", "duo mist", "mists", "mist"),
              channel_keywords=("mists", "mist")),
    # ── PvE / Roads ────────────────────────────────────────────────────────
    EventType("abyssal_depths", "Abyssal Depths", "🕳️", "PvE — Roads & Dungeons",
              role_keywords=("abyssal depths", "abyssal", "depths"),
              channel_keywords=("depths", "abyssal")),
    EventType("roads", "Roads", "🛣️", "PvE — Roads & Dungeons",
              role_keywords=("roads", "ava roads", "avalonian roads", "roads roaming"),
              channel_keywords=("ava roads", "roads roaming", "avalonian roads", "roads")),
    EventType("group_dungeon", "Group Dungeons", "🏰", "PvE — Roads & Dungeons",
              role_keywords=("group dungeons", "group dungeon", "fame farm", "fame farming"),
              channel_keywords=("group dungeon", "fame farm", "fame")),
    EventType("static_dungeon", "Static Dungeons", "🏯", "PvE — Roads & Dungeons",
              role_keywords=("static dungeons", "static dungeon", "static"),
              channel_keywords=("static", "group dungeon")),
    EventType("ava_dungeon", "Avalonian Dungeons", "🗝️", "PvE — Roads & Dungeons",
              role_keywords=("avalonian dungeons", "avalonian dungeon", "ava dungeon"),
              channel_keywords=("avalonian dungeon", "ava dungeon")),
    EventType("world_boss", "World Boss", "👹", "PvE — Roads & Dungeons",
              role_keywords=("world boss",),
              channel_keywords=("world boss",)),
    EventType("tracking", "Tracking", "🐾", "PvE — Roads & Dungeons",
              role_keywords=("tracking",),
              channel_keywords=("tracking",)),
    # ── Economy / logistics ────────────────────────────────────────────────
    EventType("gathering", "Gathering", "⛏️", "Economy",
              role_keywords=("gathering",),
              channel_keywords=("gathering",)),
    EventType("transport", "Transport", "🐂", "Economy",
              role_keywords=("transport", "hauling", "caravan"),
              channel_keywords=("transport", "hauling", "market")),
    EventType("economy", "Economy", "💰", "Economy",
              role_keywords=("economy", "market", "trade", "trading"),
              channel_keywords=("union market", "market", "economy", "crafting")),
    # ── Catch-all ──────────────────────────────────────────────────────────
    EventType("other", "Other", "📌", "Other",
              role_keywords=(),
              channel_keywords=()),
)

# Legacy keys stay readable so old LFG rows, schedules, and saved quick-vote
# configs keep routing to the closest current ping role.
EVENT_TYPE_ALIASES: dict[str, str] = {
    "ava_roads": "roads",
    "mists": "duo_mists",
    "toa": "roads",
    "ava_dungeon": "ava_dungeon",
    "static_dungeon": "static_dungeon",
    "fame_farm": "group_dungeon",
    "hce": "group_dungeon",
    "dungeon_dive": "group_dungeon",
    "solo_dungeon": "group_dungeon",
    "corrupted": "pvp",
    "crystal_league": "crystal_arena",
    "gvg": "small_scale",
    "castle": "zvz",
    "siege": "zvz",
    "gather_bz": "gathering",
    "gather_yr": "gathering",
    "fishing": "gathering",
    "meeting": "other",
    "training": "other",
    "season": "other",
    "recruiting": "other",
    "social": "other",
}


def canonical_event_type_key(event_type: str | None) -> str | None:
    key = str(event_type or "").strip().lower()
    if not key:
        return None
    if key in {t.key for t in EVENT_TYPES}:
        return key
    return EVENT_TYPE_ALIASES.get(key)


EVENT_TYPES_BY_KEY: dict[str, EventType] = {t.key: t for t in EVENT_TYPES}
EVENT_TYPES_BY_KEY.update({
    old: EVENT_TYPES_BY_KEY[new]
    for old, new in EVENT_TYPE_ALIASES.items()
    if new in EVENT_TYPES_BY_KEY
})


# ── Channel keyword candidates (for /lfg auto-config) ───────────────────────
# Common board / post channel keyword candidates. Tuned so #weekly-events and
# #looking-for-group win on a TRAVELERS-UNION-style server while still
# matching plainer "#events" / "#lfg" channels elsewhere.
BOARD_CHANNEL_KEYWORDS = (
    "weekly-events", "weekly events", "event-board", "events-board",
    "lfg-board", "event_panel", "events",
)
POST_CHANNEL_KEYWORDS = (
    "looking-for-group", "looking for group", "lfg", "events",
    "event-post", "content",
)

# Categories whose channels should NEVER be auto-picked as a per-type LFG
# post target. These are info / guide / staff-only categories — matching one
# of these names (case-insensitive substring) excludes a channel from the
# per-type auto-mapping. The board and general LFG channels are *not* gated
# by this list because some servers (incl. HOME GUILD) put their event
# announcements channel inside an "important" / "announcements" category.
PER_TYPE_EXCLUDE_CATEGORIES = (
    "guide", "guides", "welcome", "verify", "killboard",
    "stats", "leadership", "voice channels",
)


# ── Guild scan: notable permission flags ────────────────────────────────────
# Permission flags worth surfacing in the scan output. "Administrator" is
# handled separately (it's the nuclear one). The rest are grouped into
# moderation, channel-management, voice-management, and "elevated" buckets;
# we report the union as a flat list to keep each line short. Plain chat
# perms (read/send messages, etc.) are intentionally omitted — every active
# role has those and listing them is just noise.
NOTABLE_PERM_FLAGS: tuple[str, ...] = (
    "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "manage_messages", "manage_threads", "manage_events",
    "manage_nicknames", "manage_emojis_and_stickers",
    "kick_members", "ban_members", "moderate_members",
    "view_audit_log", "view_guild_insights",
    "mention_everyone",
    "mute_members", "deafen_members", "move_members", "priority_speaker",
)
