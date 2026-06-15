"""Shared helpers + tunable defaults for the Automation cog.

Kept under a leading-underscore filename so ``bot.py``'s cog auto-loader
skips this file (it globs ``cogs/*.py`` and ignores ``_*.py``). It is
imported as a regular module by ``cogs/automation.py`` and the other
``cogs/_automation_*.py`` sibling modules.
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime

import discord


# ── Tunable defaults ────────────────────────────────────────────────────────

_DEFAULT_REMINDER_MIN     = 30
_DEFAULT_INACTIVE_DAYS    = 21
_DEFAULT_UNVERIFIED_KICK_DAYS = 7
_DEFAULT_UNVERIFIED_NUDGE_DAYS = 1
_DEFAULT_UNVERIFIED_NUDGE_COOLDOWN_DAYS = 2
_DEFAULT_UNVERIFIED_NUDGE_MAX = 3
_DEFAULT_AUTO_ALUMNI_DAYS = 60
_DEFAULT_INACTIVITY_NUDGE_LEAD_DAYS = 7
_DEFAULT_INACTIVITY_NUDGE_COOLDOWN_DAYS = 14
_DEFAULT_HELP_TICKET_SLA_HOURS = 24
_DEFAULT_MILESTONE_FAME   = 250_000  # global fallback
_DEFAULT_VOICE_PCT        = 50
# Under-filled comp alert defaults.
_DEFAULT_UNDERFILL_LEAD_MIN   = 120  # alert when event starts in ≤2h
_DEFAULT_UNDERFILL_THRESHOLD  = 60   # alert if <60% of comp slots claimed
# (metric_key, label, emoji, default_threshold_per_sync_window)
_DEFAULT_FAME_METRICS     = (
    ("kill_fame",     "kill fame",     "⚔️",  100_000),
    ("pve_total",     "PvE fame",      "🐗",  500_000),
    ("gather_all",    "gather fame",   "⛏️",  250_000),
    ("crafting_fame", "crafting fame", "🔨",  250_000),
    ("fishing_fame",  "fishing fame",  "🎣",  100_000),
)


# ── Time / config helpers ───────────────────────────────────────────────────

def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _get_int_config(db, key: str, default: int) -> int:
    raw = db.get_config(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _channel(bot: Bot, key: str) -> discord.TextChannel | None:
    raw = bot.db.get_config(key)
    if not raw:
        return None
    ch = bot.get_channel(int(raw))
    return ch if isinstance(ch, discord.TextChannel) else None


# ── Snooze helpers ──────────────────────────────────────────────────────────
#
# Officers can suppress a recurring daily alert (inactivity sweep, policy
# drift) for a window via a button on the embed. Snoozes are stored as a
# UTC ISO timestamp in guild_config under ``automation_snooze_<scope>_until``.

def _snooze_key(scope: str) -> str:
    return f"automation_snooze_{scope}_until"


def _is_snoozed(bot: Bot, scope: str) -> bool:
    raw = bot.db.get_config(_snooze_key(scope))
    if not raw:
        return False
    try:
        until = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    return _now() < until


def _set_snooze(bot: Bot, scope: str, hours: int) -> datetime.datetime:
    until = _now() + datetime.timedelta(hours=hours)
    bot.db.set_config(_snooze_key(scope), until.isoformat())
    return until


def _clear_snooze(bot: Bot, scope: str) -> None:
    bot.db.set_config(_snooze_key(scope), "")
