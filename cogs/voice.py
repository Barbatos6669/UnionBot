"""Voice activity tracker.

Records per-player time spent in any voice channel and aggregates it per
UTC day in the ``voice_activity`` table.

Design:
* On ``on_voice_state_update``: maintain an in-memory ``_sessions`` dict
  mapping ``discord_id -> session_start_utc``. A session starts when a
  member joins any voice channel and ends when they leave or disconnect.
* On session end: compute elapsed seconds, split across UTC-day boundaries
  if needed, and write to the DB.
* Periodic flush (every 5 min): for every still-open session, persist the
  elapsed seconds since the last flush. This way we don't lose long calls
  if the bot restarts mid-session.

Bots are ignored. Stage channels and AFK channels count if Discord reports
the member as in-voice — keep simple, don't second-guess.
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime

import discord
from discord.ext import commands, tasks

from config import LIFECYCLE_ROLES, STAFF_ROLES
from debug import info_log, error_log


REGISTERED_VOICE_ROLES = frozenset(
    {
        "Verified",
        "HomeGuild",
        "Alliance",
        "Guest",
        "Ambassador",
        "Commander",
        "Guild Leader",
        *(role for role in LIFECYCLE_ROLES if role not in {"Inactive", "Alumni"}),
        *STAFF_ROLES,
    }
)
VOICE_GUARD_NOTIFY_COOLDOWN = datetime.timedelta(hours=6)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iter_day_buckets(start: datetime.datetime, end: datetime.datetime):
    """Yield ``(date_iso, seconds)`` pairs splitting [start, end] across UTC
    day boundaries. ``end`` exclusive in seconds calc."""
    if end <= start:
        return
    cur = start
    while cur < end:
        next_midnight = (cur + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        bucket_end = min(next_midnight, end)
        seconds = int((bucket_end - cur).total_seconds())
        if seconds > 0:
            yield cur.strftime("%Y-%m-%d"), seconds
        cur = bucket_end


def _parse_configured_voice_roles(raw: str | None) -> set[str]:
    """Officer-configurable extra role names allowed into voice."""
    if not raw:
        return set()
    return {
        part.strip()
        for part in str(raw).replace("\n", ",").split(",")
        if part.strip()
    }


def _member_has_registered_voice_access(member: discord.Member, db=None) -> bool:
    """Voice is for registered members or explicitly approved temporary guests."""
    perms = getattr(member, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_guild or perms.move_members):
        return True

    role_names = {getattr(role, "name", "") for role in getattr(member, "roles", [])}
    if role_names & REGISTERED_VOICE_ROLES:
        return True

    if db is not None:
        extra_names = _parse_configured_voice_roles(
            db.get_config("voice_extra_access_roles")
        )
        if role_names & extra_names:
            return True

    return False


def _channel_mention_by_name(guild: discord.Guild, *needles: str) -> str | None:
    lowered = tuple(needle.lower() for needle in needles if needle)
    for channel in guild.text_channels:
        name = channel.name.lower()
        if all(needle in name for needle in lowered):
            return channel.mention
    return None


def _is_voice_like(channel: discord.abc.Connectable | None) -> bool:
    return isinstance(channel, (discord.VoiceChannel, discord.StageChannel))


class Voice(commands.Cog):
    """Track per-member voice-channel time."""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        # discord_id -> last accounted-for timestamp (UTC)
        self._sessions: dict[str, datetime.datetime] = {}
        self._voice_guard_notified_at: dict[str, datetime.datetime] = {}
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        self.flush_loop.start()

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        # Persist whatever is currently open so a clean reload doesn't drop time.
        self._flush_open_sessions()

    # ── persistence helpers ────────────────────────────────────────────────

    def _persist_span(self, discord_id: str,
                      start: datetime.datetime,
                      end: datetime.datetime) -> None:
        for date_iso, seconds in _iter_day_buckets(start, end):
            try:
                self.bot.db.add_voice_seconds(discord_id, date_iso, seconds)
            except Exception as exc:  # noqa: BLE001
                error_log(f"voice add_voice_seconds failed for {discord_id}: {exc!r}")

    def _flush_open_sessions(self) -> None:
        now = _now()
        for did, started in list(self._sessions.items()):
            if now > started:
                self._persist_span(did, started, now)
                self._sessions[did] = now

    async def _notify_voice_restricted(self, member: discord.Member) -> None:
        did = str(member.id)
        now = _now()
        last = self._voice_guard_notified_at.get(did)
        if last is not None and now - last < VOICE_GUARD_NOTIFY_COOLDOWN:
            return
        self._voice_guard_notified_at[did] = now

        register = _channel_mention_by_name(member.guild, "register")
        help_channel = (
            _channel_mention_by_name(member.guild, "help")
            or _channel_mention_by_name(member.guild, "ticket")
        )
        route = []
        if register:
            route.append(f"register in {register}")
        if help_channel:
            route.append(f"ask for help in {help_channel}")
        route_text = " or ".join(route) if route else "register or ask an officer for help"

        try:
            await member.send(
                "Voice channels are restricted to registered members and approved "
                f"temporary guests. Please {route_text} before joining voice."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _disconnect_unregistered_voice(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        if _member_has_registered_voice_access(member, self.bot.db):
            return False
        try:
            await member.move_to(
                None,
                reason="Voice restricted to registered members / approved guests",
            )
            info_log(
                f"voice_guard: disconnected unregistered member {member.id} "
                f"from {channel.id} ({channel.name})."
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(
                f"voice_guard: failed to disconnect {member.id} "
                f"from {channel.id}: {exc!r}"
            )
        await self._notify_voice_restricted(member)
        return True

    # ── listeners ──────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        was_in = before.channel is not None
        is_in = after.channel is not None
        did = str(member.id)
        now = _now()
        if _is_voice_like(after.channel):
            if await self._disconnect_unregistered_voice(member, after.channel):
                started = self._sessions.pop(did, None)
                if started:
                    self._persist_span(did, started, now)
                return
        # Joined voice (or moved between channels we don't track separately).
        if not was_in and is_in:
            self._sessions[did] = now
            return
        # Left voice — flush the span and clear.
        if was_in and not is_in:
            started = self._sessions.pop(did, None)
            if started:
                self._persist_span(did, started, now)
            return
        # Same-channel state change (mute/deafen/etc.) — nothing to do.

    # ── periodic flush ─────────────────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def flush_loop(self) -> None:
        try:
            self._flush_open_sessions()
        except Exception as exc:  # noqa: BLE001
            error_log(f"voice flush_loop failed: {exc!r}")

    @flush_loop.before_loop
    async def _before_flush(self) -> None:
        await self.bot.wait_until_ready()
        # Seed sessions for anyone already in voice when we boot, so we start
        # accruing time from now (we don't know how long they've been in).
        try:
            now = _now()
            for guild in self.bot.guilds:
                for vc in [*guild.voice_channels, *guild.stage_channels]:
                    for m in vc.members:
                        if m.bot:
                            continue
                        if await self._disconnect_unregistered_voice(m, vc):
                            continue
                        self._sessions[str(m.id)] = now
        except Exception as exc:  # noqa: BLE001
            error_log(f"voice startup seed failed: {exc!r}")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Voice(bot))
