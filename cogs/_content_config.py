"""Constants and tiny helpers for the content-curator cog.

Kept separate so the views/db/cog modules can all import from one place
without duplicating string keys. No discord imports beyond what's needed
for the role-check helper.
"""

from __future__ import annotations

import datetime as dt
import re

import discord

from cogs._lfg_config import (
    EVENT_TYPES,
    EVENT_TYPES_BY_KEY,
    PRIME_SLOTS,
    prime_slot_window_for_day,
    prime_slot_display_label,
    utc_datetime,
)

# ── config keys ─────────────────────────────────────────────────────────────
CFG_CHANNEL          = "content_curator_channel_id"
CFG_ANNOUNCE_CHANNEL = "content_curator_announce_channel_id"
CFG_WEEKLY_POLL_ENABLED = "content_curator_weekly_poll_enabled"
CFG_OPEN_WEEKDAY     = "content_curator_open_weekday"      # 0=Mon, 4=Fri
CFG_OPEN_HOUR        = "content_curator_open_hour"          # 0-23 UTC
CFG_DURATION_HOURS   = "content_curator_duration_hours"
CFG_TOP_N            = "content_curator_top_n"
CFG_MAX_PER_USER     = "content_curator_max_per_user"
CFG_AUTO_LFG         = "content_curator_auto_create_lfg"
CFG_EVENT_HOUR       = "content_curator_event_hour"
CFG_EVENT_DURATION   = "content_curator_event_duration_min"
CFG_REVIEWER_ROLES   = "content_curator_officer_roles"
CFG_BOARD_CHANNEL    = "content_curator_board_channel_id"
CFG_BOARD_MESSAGE    = "content_curator_board_message_id"
CFG_AVAILABILITY_DURATION_MIN = "content_curator_availability_duration_min"
CFG_DAILY_TIMER_FUNNEL_ENABLED = "content_curator_daily_timer_enabled"
CFG_DAILY_TIMER_CHANNEL = "content_curator_daily_timer_channel_id"
CFG_DAILY_TIMER_AVAIL_HOUR = "content_curator_daily_timer_avail_hour"
CFG_DAILY_TIMER_AVAIL_MINUTE = "content_curator_daily_timer_avail_minute"
CFG_DAILY_TIMER_VOTE_HOUR = "content_curator_daily_timer_vote_hour"
CFG_DAILY_TIMER_VOTE_MINUTE = "content_curator_daily_timer_vote_minute"
CFG_DAILY_TIMER_VOTE_DURATION = "content_curator_daily_timer_vote_duration_min"
CFG_DAILY_TIMER_MIN_AVAILABLE = "content_curator_daily_timer_min_available"
CFG_DAILY_TIMER_PING_ROLE = "content_curator_daily_timer_ping_role_id"
CFG_DAILY_TIMER_SEASON_KEYS = "content_curator_daily_timer_season_keys"

# Quick-vote ("next match" style) config
CFG_QUICKVOTE_DURATION_MIN = "content_curator_quickvote_duration_min"  # default 10
CFG_QUICKVOTE_LEAD_MIN     = "content_curator_quickvote_lead_min"      # default 15
CFG_QUICKVOTE_DURATION_LFG = "content_curator_quickvote_lfg_minutes"   # default 90
CFG_QUICKVOTE_OPTIONS      = "content_curator_quickvote_options"

DEFAULT_OFFICER_ROLES = ("Captain", "Officer", "Steward")

QUICKVOTE_PREFIX = "content:qv:"


# ── activity catalog ────────────────────────────────────────────────────────
# Minimum players required for a quick-vote winner to actually run.
# Keyed by event_type.key from cogs._lfg_config. Unlisted keys default to 1.
ACTIVITY_MIN_PLAYERS: dict[str, int] = {
    "alliance": 5,
    "pvp": 1, "faction": 3, "gank": 3, "small_scale": 3, "zvz": 15,
    "hellgate": 2, "crystal_arena": 5, "duo_mists": 2,
    "abyssal_depths": 2, "roads": 3, "group_dungeon": 3,
    "static_dungeon": 3, "ava_dungeon": 5, "world_boss": 5,
    "tracking": 2,
    "gathering": 1, "transport": 2, "economy": 1,
    "other": 1,
}

# Default options for /content nextvote and the dashboard's "Next-activity"
# button. Discord caps a select at 25 options — keep this <=24.
DEFAULT_QUICKVOTE_KEYS: tuple[str, ...] = (
    "alliance", "pvp", "faction", "gank", "small_scale", "zvz", "hellgate",
    "crystal_arena", "duo_mists",
    "abyssal_depths", "roads", "group_dungeon", "static_dungeon",
    "ava_dungeon", "world_boss", "tracking",
    "gathering", "transport", "economy",
)

# Curated event types shown in the dashboard "Suggest" modal picker.
SUGGEST_PICK_KEYS: tuple[str, ...] = DEFAULT_QUICKVOTE_KEYS

# Friendly category shortcuts for /content nextvote options:
QUICKVOTE_CATEGORY_KEYS: dict[str, tuple[str, ...]] = {
    "pvp": (
        "alliance", "pvp", "faction", "gank", "small_scale", "zvz", "hellgate",
        "crystal_arena", "duo_mists",
    ),
    "pve": (
        "abyssal_depths", "roads", "group_dungeon", "static_dungeon",
        "ava_dungeon", "world_boss", "tracking",
    ),
    "small": ("gank", "small_scale", "hellgate", "crystal_arena", "duo_mists"),
    "large": ("zvz", "alliance", "faction", "roads", "ava_dungeon", "world_boss"),
    "economy": ("gathering", "transport", "economy"),
    "guild": ("pvp", "faction", "roads", "group_dungeon", "gathering"),
    "all": tuple(t.key for t in EVENT_TYPES if t.key != "other")[:25],
}

AVAILABILITY_RECOMMENDATION_KEYS: tuple[str, ...] = (
    "zvz", "alliance", "faction", "ava_dungeon", "world_boss",
    "crystal_arena", "roads", "small_scale", "static_dungeon",
    "group_dungeon", "gank", "hellgate", "duo_mists",
    "abyssal_depths", "tracking", "transport", "pvp",
    "gathering", "economy",
)

# Daily prime-timer funnel options should push the guild season, not just fill
# time. These are event-board categories that can be aimed at Might/Conqueror
# progress when run in lethal Outlands/Roads context. Casual faction, arena,
# economy, and transport are intentionally excluded from this default set.
SEASON_POINT_FOCUS_KEYS: tuple[str, ...] = (
    "zvz", "alliance", "world_boss", "ava_dungeon", "roads",
    "static_dungeon", "group_dungeon", "small_scale", "gank",
    "hellgate", "duo_mists", "abyssal_depths", "tracking",
    "gathering", "pvp",
)


# ── tiny helpers ────────────────────────────────────────────────────────────
def activity_min(event_type: str) -> int:
    return int(ACTIVITY_MIN_PLAYERS.get(event_type, 1))


def utc_dt_for_discord(value: dt.datetime) -> dt.datetime:
    """Normalize a datetime to aware UTC for Discord API/timestamps."""
    return utc_datetime(value)


def daily_timer_target_date(now: dt.datetime | None = None) -> dt.date:
    """Cycle date for the daily prime-timer funnel.

    A cycle starts with the 18:00 UTC timer on this UTC date and continues
    through the 04:00 UTC timer on the following UTC date.
    """
    now = utc_dt_for_discord(now or now_utc())
    return now.date()


def daily_timer_slot_windows(target_date: dt.date) -> list[dict[str, object]]:
    """Return the 18/20/22/00/02/04 UTC windows for one daily cycle."""
    windows: list[dict[str, object]] = []
    for slot in PRIME_SLOTS:
        start, end = prime_slot_window_for_day(target_date, slot)
        label = f"{slot.emoji} {start:%a %b %d} · {prime_slot_display_label(slot)}"
        windows.append({
            "label": label,
            "slot_label": f"PRIME {slot.label}",
            "starts_at": start,
            "ends_at": end,
            "emoji": slot.emoji,
        })
    return windows


def daily_timer_availability_due(
    now: dt.datetime,
    *,
    hour: int = 5,
    minute: int = 5,
    catchup_minutes: int = 30,
) -> bool:
    now = utc_dt_for_discord(now)
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return due <= now < due + dt.timedelta(minutes=max(1, catchup_minutes))


def daily_timer_vote_due(now: dt.datetime, *, hour: int = 15, minute: int = 0) -> bool:
    now = utc_dt_for_discord(now)
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= due


def parse_availability_slots(raw: str, limit: int = 25) -> list[str]:
    """Parse officer-entered time windows into Discord select labels."""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[\n;,]+", raw or ""):
        label = re.sub(r"\s+", " ", part).strip()
        if not label:
            continue
        label = label[:90]
        key = label.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(label)
        if len(out) >= limit:
            break
    return out


def _clean_event_key_list(keys: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        clean = str(key or "").strip()
        if not clean or clean in seen or clean not in EVENT_TYPES_BY_KEY:
            continue
        seen.add(clean)
        out.append(clean)
    return tuple(out)


def configured_season_point_focus_keys(db) -> tuple[str, ...]:
    """Return daily-timer focus keys, allowing officers to override in config."""
    raw = ""
    try:
        raw = str(db.get_config(CFG_DAILY_TIMER_SEASON_KEYS) or "").strip()
    except Exception:  # noqa: BLE001 - helper must stay safe in tests/startup
        raw = ""
    if not raw:
        return _clean_event_key_list(SEASON_POINT_FOCUS_KEYS)
    configured = _clean_event_key_list(tuple(re.split(r"[\s,|;]+", raw)))
    return configured or _clean_event_key_list(SEASON_POINT_FOCUS_KEYS)


def availability_recommendation_keys(
    headcount: int,
    limit: int = 8,
    *,
    focus_keys: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """Return event-type keys that are realistic for the available headcount."""
    try:
        count = max(0, int(headcount))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return []

    candidates: list[tuple[int, int, str]] = []
    key_source = _clean_event_key_list(
        tuple(focus_keys)
        if focus_keys is not None
        else AVAILABILITY_RECOMMENDATION_KEYS
    )
    for idx, key in enumerate(key_source):
        if key not in EVENT_TYPES_BY_KEY:
            continue
        need = activity_min(key)
        if need <= count:
            candidates.append((need, idx, key))
    candidates.sort(key=lambda row: (-row[0], row[1]))

    return [key for _need, _idx, key in candidates[: max(1, limit)]]


def season_point_focus_recommendation_keys(
    db, headcount: int, limit: int = 8,
) -> list[str]:
    """Return daily prime-timer choices that scale with headcount and season value."""
    return availability_recommendation_keys(
        headcount,
        limit=limit,
        focus_keys=configured_season_point_focus_keys(db),
    )


def availability_content_recommendations(headcount: int, limit: int = 8) -> list[str]:
    """Return content labels that are realistic for the available headcount."""
    lines: list[str] = []
    for key in availability_recommendation_keys(headcount, limit=limit):
        et = EVENT_TYPES_BY_KEY[key]
        lines.append(f"{et.emoji} {et.label} (min {activity_min(key)})")
    return lines


def ranked_available_timer_indexes(
    tallies: dict[int, int],
    *,
    window_count: int,
    min_available: int = 1,
) -> list[tuple[int, int]]:
    """Rank timer indexes by availability, keeping original order as tie-break."""
    rows = [
        (int(tallies.get(i, 0)), i)
        for i in range(max(0, int(window_count)))
        if int(tallies.get(i, 0)) >= int(min_available)
    ]
    return sorted(rows, key=lambda row: (-row[0], row[1]))


def cfg_int(db, key: str, default: int) -> int:
    try:
        v = db.get_config(key)
        return int(v) if v is not None and str(v).strip() != "" else default
    except (TypeError, ValueError):
        return default


def is_officer(member: discord.Member, db) -> bool:
    if member.guild_permissions.administrator:
        return True
    cfg_roles = (db.get_config(CFG_REVIEWER_ROLES) or "").strip()
    allowed = tuple(r.strip() for r in cfg_roles.split(",") if r.strip()) or DEFAULT_OFFICER_ROLES
    return any(r.name in allowed for r in member.roles)


def now_utc() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)
