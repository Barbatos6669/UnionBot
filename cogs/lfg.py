"""LFG / Event board.

Posts a persistent control panel listing the guild's prime-time slots and a
General LFG button. Clicking a slot opens a modal so the member can fill in
event specifics; on submit, the bot posts the event in the configured LFG
channel with Sign Up / Withdraw buttons. The roster is tracked in the DB and
the message edits in place to reflect the current signups.

Permissions:
  * Prime-time slots are restricted to PRIME_CREATOR_ROLES.
  * General LFG is open to anyone (you can tighten this in the check).

Stand-in for the C++ comp/item system: each event has a free-text
``comp_notes`` field. When the comp library is bridged later, this can be
replaced with a real comp picker without touching the DB schema.
"""
from __future__ import annotations

import contextlib
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

# All the static configuration tables (slots, role gates, layout, intros,
# perm scheme, event types, keyword lists, perm-flag list) live in
# ``_lfg_config`` to keep this file focused on runtime logic. The
# leading-underscore name keeps :func:`bot._load_cogs` from trying to load
# it as a cog extension.
from cogs._lfg_config import (
    CFG_BOARD_CHANNEL,
    CFG_CHAN_PREFIX,
    CFG_LFG_CHANNEL,
    CFG_ROLE_PREFIX,
    CFG_VOICE_CATEGORY_PREFIX,
    CHANNEL_INTROS,
    DESIRED_LAYOUT,
    EVENT_TYPES,
    EVENT_TYPES_BY_KEY,
    LAYOUT_CATEGORY_OVERWRITES,
    LAYOUT_CHANNEL_OVERWRITES,
    STAFF_PERMISSION_SCHEME,
    canonical_event_type_key,
    display_slot_label,
)

# ── Helpers + UI views + guild-scan dump (extracted to sibling _lfg_*.py) ───
from cogs._lfg_helpers import (
    _create_discord_scheduled_event,
    _create_lfg_discussion_thread,
    _delete_event_access_role,
    _ensure_event_access_role,
    _event_voice_channel_name,
    _event_voice_overwrites,
    _format_event_embed,
    _grant_event_access_role,
    _get_ping_for_type,
    _get_post_channel_for_type,
    _sync_event_access_role_members,
    auto_discover_config,
)
from cogs._lfg_scan import (
    build_guild_scan_text,
    write_guild_scan_file,
)
from cogs._event_reports import (
    batch_embeds_for_send,
    build_event_report_embed,
    build_event_report_view,
)
from cogs._lfg_views import (
    EventBoardView,
    EventSignupView,
    _board_embed,
    _on_first_event_signup,
    _refresh_event_message,
    _refresh_prime_claim_dashboards,
)
from cogs._typing import Bot
from debug import error_log, info_log, warning_log
from utils import error_embed, info_embed, success_embed

CFG_EVENT_VOICE_CATEGORY = "lfg_event_voice_category_id"
CFG_UNTRACKED_CLEANUP_HOURS = "lfg_untracked_cleanup_hours"
CFG_RECURRING_CTA_ENABLED = "lfg_recurring_02_cta_enabled"
CFG_RECURRING_CTA_PING = "lfg_recurring_02_cta_ping_on_create"
RECURRING_CTA_TITLE = "Daily Content - CTA"
RECURRING_CTA_SLOT_LABEL = "PRIME 02:00-03:00"
RECURRING_CTA_HOUR_UTC = 2
RECURRING_CTA_EVENT_TYPE = "alliance"
RECURRING_CTA_FALLBACK_DESCRIPTION = (
    "We are building daily alliance content at 02:00 UTC to create "
    "consistency, activity, and reliable groups.\n\n"
    "Sign up if you expect regear, rewards, payouts, or priority for limited "
    "slots. Be in voice, follow the shotcaller, keep comms clear, and come "
    "ready with food, pots, mount, cape, and repair silver."
)


def _config_enabled_from_db(db, key: str, *, default: bool = False) -> bool:
    raw = db.get_config(key)
    if raw is None or str(raw).strip() == "":
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


# ── Cog ─────────────────────────────────────────────────────────────────────
class LFG(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")
        self.dispatch_reminders.start()
        self.manage_event_voice_channels.start()
        self.cleanup_finished_lfg_posts.start()
        self.ensure_daily_02_cta.start()

    def cog_unload(self) -> None:
        self.dispatch_reminders.cancel()
        self.manage_event_voice_channels.cancel()
        self.cleanup_finished_lfg_posts.cancel()
        self.ensure_daily_02_cta.cancel()

    # ── Pre-event reminder dispatcher ──────────────────────────────────
    # Runs every minute. For each open event whose start time falls within
    # the configured lead window (default 30 min) and that we haven't
    # already pinged, DM each signup a short heads-up and stamp the
    # event's ``reminded_at`` so we never double-fire.
    @tasks.loop(minutes=1)
    async def dispatch_reminders(self) -> None:
        db = self.bot.db
        # Lead time is officer-tunable via /lfg set-reminder-lead, falls back
        # to 30 minutes. Stored as a string in ``guild_config``.
        try:
            raw = db.get_config("lfg_reminder_minutes") or "30"
            window = max(1, int(raw))
        except (TypeError, ValueError):
            window = 30
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now_dt.isoformat()
        events = db.fetch_lfg_events_to_remind(now_iso, window)
        if not events:
            return
        for ev in events:
            event_id = int(ev["id"])
            # Mark first so any error below can't get us into a retry loop
            # that spams members. One-shot semantics > delivery completeness.
            db.mark_lfg_event_reminded(event_id, now_iso)
            start_dt = self._event_dt(ev.get("starts_at"))
            if start_dt is None:
                start_dt = now_dt + datetime.timedelta(minutes=window)
            minutes_out = max(0, int((start_dt - now_dt).total_seconds() // 60))
            signups = db.fetch_lfg_signups(event_id) or []
            if not signups:
                continue
            embed = discord.Embed(
                title=f"⏰ Starting in ~{minutes_out} min: {ev['title']}",
                description=(
                    f"You're signed up for **{ev['title']}**.\n"
                    f"Kickoff: <t:{int(start_dt.timestamp())}:t> "
                    f"(<t:{int(start_dt.timestamp())}:R>)."
                ),
                color=discord.Color.gold(),
            )
            if ev.get("description"):
                embed.add_field(
                    name="Details",
                    value=str(ev["description"])[:1000],
                    inline=False,
                )
            chan_id = ev.get("channel_id")
            msg_id = ev.get("message_id")
            if chan_id and msg_id:
                guild_id = None
                try:
                    ch = self.bot.get_channel(int(chan_id))
                    if isinstance(ch, discord.TextChannel):
                        guild_id = str(ch.guild.id)
                except ValueError:
                    guild_id = None
                if guild_id is None and self.bot.guilds:
                    guild_id = str(self.bot.guilds[0].id)
                guild_part = guild_id or "@me"
                embed.add_field(
                    name="Event post",
                    value=f"https://discord.com/channels/{guild_part}/{chan_id}/{msg_id}",
                    inline=False,
                )
            sent = 0
            for row in signups:
                did = row.get("discord_id")
                if not did:
                    continue
                try:
                    user = self.bot.get_user(int(did)) \
                        or await self.bot.fetch_user(int(did))
                    if user is None:
                        continue
                    await user.send(embed=embed)
                    sent += 1
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
                    warning_log(
                        f"LFG #{event_id} reminder DM to {did} failed: {exc!r}"
                    )
            info_log(
                f"LFG #{event_id} '{ev['title']}': sent {sent}/{len(signups)} "
                f"reminder DMs ({minutes_out} min lead)."
            )

    @dispatch_reminders.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    @dispatch_reminders.error
    async def _reminders_error(self, exc: BaseException) -> None:
        error_log(f"dispatch_reminders crashed: {exc!r}; restarting loop.")
        try:
            self.dispatch_reminders.restart()
        except Exception as restart_exc:  # pragma: no cover - defensive
            error_log(f"Failed to restart dispatch_reminders: {restart_exc!r}")

    # ── Daily 02:00 UTC CTA keeper ─────────────────────────────────────
    def _config_enabled(self, key: str, *, default: bool = False) -> bool:
        return _config_enabled_from_db(self.bot.db, key, default=default)

    def _next_daily_cta_window(
        self,
        now: datetime.datetime | None = None,
    ) -> tuple[datetime.datetime, datetime.datetime]:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
        now = now.astimezone(datetime.timezone.utc)
        starts_at = now.replace(
            hour=RECURRING_CTA_HOUR_UTC,
            minute=0,
            second=0,
            microsecond=0,
        )
        if starts_at <= now:
            starts_at += datetime.timedelta(days=1)
        return starts_at, starts_at + datetime.timedelta(hours=1)

    def _fetch_active_daily_cta(self, now: datetime.datetime) -> dict | None:
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                """
                SELECT * FROM lfg_events
                WHERE status = 'open'
                  AND is_prime = 1
                  AND title = ?
                  AND slot_label = ?
                  AND datetime(ends_at) >= datetime(?)
                ORDER BY datetime(starts_at) ASC
                LIMIT 1
                """,
                (
                    RECURRING_CTA_TITLE,
                    RECURRING_CTA_SLOT_LABEL,
                    now.isoformat(),
                ),
            )
            row = db.cursor.fetchone()
            return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily CTA lookup failed: {exc!r}")
            return None

    def _fetch_daily_cta_template(self) -> dict:
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                """
                SELECT * FROM lfg_events
                WHERE title = ?
                  AND slot_label = ?
                  AND is_prime = 1
                ORDER BY datetime(starts_at) DESC, id DESC
                LIMIT 1
                """,
                (RECURRING_CTA_TITLE, RECURRING_CTA_SLOT_LABEL),
            )
            row = db.cursor.fetchone()
            if row:
                return dict(row)
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily CTA template lookup failed: {exc!r}")
        return {
            "title": RECURRING_CTA_TITLE,
            "description": RECURRING_CTA_FALLBACK_DESCRIPTION,
            "comp_notes": "",
            "ip_requirement": "1200 IP",
            "prep_minutes": 30,
            "review_minutes": 15,
            "creator_id": "",
            "event_type": RECURRING_CTA_EVENT_TYPE,
        }

    async def _post_daily_cta_from_template(
        self,
        *,
        template: dict,
        starts_at: datetime.datetime,
        ends_at: datetime.datetime,
    ) -> int | None:
        db = self.bot.db
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            warning_log("daily CTA keeper skipped: bot is not in a guild.")
            return None

        event_type = str(template.get("event_type") or RECURRING_CTA_EVENT_TYPE)
        channel = _get_post_channel_for_type(db, guild, event_type)
        if channel is None:
            warning_log(
                "daily CTA keeper skipped: alliance/default LFG channel is not configured."
            )
            return None

        overlap = db.fetch_overlapping_prime_events(
            starts_at.isoformat(),
            ends_at.isoformat(),
        )
        if overlap:
            names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
            info_log(
                f"daily CTA keeper skipped {starts_at.isoformat()}: "
                f"prime timer already booked by {names}."
            )
            return None

        event_id = db.create_lfg_event(
            slot_label=RECURRING_CTA_SLOT_LABEL,
            is_prime=True,
            title=str(template.get("title") or RECURRING_CTA_TITLE),
            description=str(
                template.get("description") or RECURRING_CTA_FALLBACK_DESCRIPTION
            ),
            comp_notes=str(template.get("comp_notes") or ""),
            ip_requirement=str(template.get("ip_requirement") or "1200 IP"),
            starts_at=starts_at.isoformat(),
            ends_at=ends_at.isoformat(),
            prep_minutes=int(template.get("prep_minutes") or 30),
            review_minutes=int(template.get("review_minutes") or 15),
            creator_id=str(template.get("creator_id") or ""),
            event_type=event_type,
        )
        if not event_id:
            error_log("daily CTA keeper failed: create_lfg_event returned 0.")
            return None

        event = db.fetch_lfg_event(event_id)
        if not event:
            db.delete_lfg_event(event_id)
            error_log(f"daily CTA keeper failed: event #{event_id} missing after create.")
            return None

        ping = (
            _get_ping_for_type(db, event_type)
            if self._config_enabled(CFG_RECURRING_CTA_PING, default=False)
            else None
        )
        try:
            msg = await channel.send(
                content=ping or None,
                embed=_format_event_embed(db, event),
                view=EventSignupView(event_id),
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                    users=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            with contextlib.suppress(Exception):
                db.delete_lfg_event(event_id)
            error_log(f"daily CTA keeper post failed for #{event_id}: {exc!r}")
            return None

        db.set_lfg_message(event_id, str(channel.id), str(msg.id))
        event = db.fetch_lfg_event(event_id) or event
        await _create_lfg_discussion_thread(db, event, msg)

        scheduled = await _create_discord_scheduled_event(
            guild,
            name=event["title"],
            description=(
                f"{event.get('description') or ''}\n\n"
                f"Slot: {display_slot_label(event.get('slot_label'))}\n"
                f"Sign up: {msg.jump_url}"
            ).strip(),
            starts_at=starts_at,
            ends_at=ends_at,
            location=msg.jump_url,
        )
        if scheduled is not None:
            db.set_lfg_scheduled_event_id(event_id, str(scheduled.id))

        event = db.fetch_lfg_event(event_id) or event
        await _refresh_prime_claim_dashboards(self.bot, event, "daily CTA create")
        info_log(
            f"daily CTA keeper created event #{event_id} "
            f"for {starts_at.isoformat()} in #{channel.name}."
        )
        return event_id

    @tasks.loop(minutes=10)
    async def ensure_daily_02_cta(self) -> None:
        if not self._config_enabled(CFG_RECURRING_CTA_ENABLED, default=False):
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        if self._fetch_active_daily_cta(now):
            return

        starts_at, ends_at = self._next_daily_cta_window(now)
        template = self._fetch_daily_cta_template()
        await self._post_daily_cta_from_template(
            template=template,
            starts_at=starts_at,
            ends_at=ends_at,
        )

    @ensure_daily_02_cta.before_loop
    async def _before_ensure_daily_02_cta(self) -> None:
        await self.bot.wait_until_ready()

    @ensure_daily_02_cta.error
    async def _ensure_daily_02_cta_error(self, exc: BaseException) -> None:
        error_log(f"ensure_daily_02_cta crashed: {exc!r}; restarting loop.")
        try:
            self.ensure_daily_02_cta.restart()
        except Exception as restart_exc:  # pragma: no cover - defensive
            error_log(f"Failed to restart ensure_daily_02_cta: {restart_exc!r}")

    # ── Temporary event voice channels ─────────────────────────────────
    def _event_dt(self, raw: str | None) -> datetime.datetime | None:
        if not raw:
            return None
        try:
            value = datetime.datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return value.astimezone(datetime.timezone.utc)

    async def _fetch_category(self, raw: str | None) -> discord.CategoryChannel | None:
        raw = str(raw or "").strip()
        if raw:
            try:
                ch = self.bot.get_channel(int(raw)) or await self.bot.fetch_channel(int(raw))
                if isinstance(ch, discord.CategoryChannel):
                    return ch
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass
        return None

    async def _category_for_text_channel(self, raw: str | None) -> discord.CategoryChannel | None:
        raw = str(raw or "").strip()
        if not raw:
            return None
        try:
            ch = self.bot.get_channel(int(raw)) or await self.bot.fetch_channel(int(raw))
            if isinstance(ch, discord.TextChannel) and ch.category:
                return ch.category
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return None
        return None

    async def _event_voice_category(self) -> discord.CategoryChannel | None:
        category = await self._fetch_category(self.bot.db.get_config(CFG_EVENT_VOICE_CATEGORY))
        if category is not None:
            return category

        voice_id = (self.bot.db.get_config("automation_voice_channel_id") or "").strip()
        if voice_id:
            try:
                ch = self.bot.get_channel(int(voice_id)) or await self.bot.fetch_channel(int(voice_id))
                if isinstance(ch, discord.VoiceChannel) and ch.category:
                    return ch.category
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

        for guild in self.bot.guilds:
            for category in guild.categories:
                if "content voice" in category.name.lower():
                    return category
        return None

    async def _event_voice_category_for_event(self, event: dict) -> discord.CategoryChannel | None:
        event_type = str(event.get("event_type") or "").strip()
        if event_type:
            type_keys = [event_type]
            canonical = canonical_event_type_key(event_type)
            if canonical and canonical not in type_keys:
                type_keys.append(canonical)
            for type_key in type_keys:
                category = await self._fetch_category(
                    self.bot.db.get_config(f"{CFG_VOICE_CATEGORY_PREFIX}{type_key}")
                )
                if category is not None:
                    return category

            # If a type posts in its own channel, putting its temporary voice
            # in that channel's category keeps specialized content together.
            type_channel = ""
            for type_key in type_keys:
                type_channel = (self.bot.db.get_config(f"{CFG_CHAN_PREFIX}{type_key}") or "").strip()
                if type_channel:
                    break
            event_channel = str(event.get("channel_id") or "").strip()
            if type_channel and type_channel == event_channel:
                category = await self._category_for_text_channel(type_channel)
                if category is not None:
                    return category

        return await self._event_voice_category()

    async def _fetch_event_voice_channel(self, event: dict) -> discord.VoiceChannel | None:
        channel_id = str(event.get("voice_channel_id") or "").strip()
        if not channel_id:
            return None
        try:
            ch = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
            return ch if isinstance(ch, discord.VoiceChannel) else None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return None

    async def _event_guild_for_event(
        self,
        event: dict,
        voice: discord.VoiceChannel | None = None,
    ) -> discord.Guild | None:
        if voice is not None:
            return voice.guild
        chan_id = str(event.get("channel_id") or "").strip()
        if chan_id:
            try:
                ch = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
                guild = getattr(ch, "guild", None)
                if isinstance(guild, discord.Guild):
                    return guild
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass
        category = await self._event_voice_category_for_event(event)
        if category is not None:
            return category.guild
        return self.bot.guilds[0] if self.bot.guilds else None

    async def _post_event_voice_ping(self, event: dict, voice: discord.VoiceChannel) -> None:
        if event.get("voice_channel_pinged_at"):
            return
        event_id = int(event["id"])
        mention_ids: list[str] = []
        creator_id = str(event.get("creator_id") or "")
        if creator_id.isdigit():
            mention_ids.append(creator_id)
        try:
            for signup in self.bot.db.fetch_lfg_signups(event_id):
                did = str(signup.get("discord_id") or "")
                if did.isdigit() and did not in mention_ids:
                    mention_ids.append(did)
        except Exception as exc:  # noqa: BLE001
            warning_log(f"event voice ping signup fetch failed for #{event_id}: {exc!r}")

        mentions = " ".join(f"<@{did}>" for did in mention_ids[:35])
        more = f" _+{len(mention_ids) - 35} more_" if len(mention_ids) > 35 else ""
        content = (
            f"{mentions}{more}\n" if mentions else ""
        ) + (
            f"🔊 Voice is open for **{event.get('title', 'this LFG')}**: {voice.mention}\n"
            "Join voice for prep, comms, and attendance tracking."
        )
        targets: list[discord.abc.Messageable] = []
        thread_id = str(event.get("discussion_thread_id") or "").strip()
        if thread_id:
            try:
                thread = self.bot.get_channel(int(thread_id)) or await self.bot.fetch_channel(int(thread_id))
                if isinstance(thread, discord.Thread):
                    targets.append(thread)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass
        chan_id = str(event.get("channel_id") or "").strip()
        if chan_id:
            try:
                ch = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
                if isinstance(ch, discord.TextChannel):
                    targets.append(ch)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

        for target in targets:
            try:
                await target.send(
                    content=content,
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False,
                    ),
                )
                self.bot.db.mark_lfg_voice_pinged(
                    event_id,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                return
            except (discord.Forbidden, discord.HTTPException):
                continue

    @tasks.loop(minutes=1)
    async def manage_event_voice_channels(self) -> None:
        db = self.bot.db
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = db.fetch_lfg_events_for_voice_lifecycle(now.isoformat())
        for ev in rows:
            event_id = int(ev["id"])
            starts_at = self._event_dt(ev.get("starts_at"))
            ends_at = self._event_dt(ev.get("ends_at"))
            if not starts_at or not ends_at:
                continue
            review_until = ends_at + datetime.timedelta(
                minutes=int(ev.get("review_minutes") or 15),
            )
            voice = await self._fetch_event_voice_channel(ev)

            if voice is None and ev.get("voice_channel_id"):
                db.mark_lfg_voice_deleted(event_id, now.isoformat())
                await _delete_event_access_role(
                    db,
                    await self._event_guild_for_event(ev),
                    ev,
                    now.isoformat(),
                    reason=f"LFG event #{event_id} voice missing",
                )
                continue

            if str(ev.get("status") or "open").lower() == "cancelled":
                if voice is not None:
                    try:
                        await voice.delete(reason=f"LFG event #{event_id} cancelled")
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        warning_log(f"event voice delete failed for #{event_id}: {exc!r}")
                        continue
                db.mark_lfg_voice_deleted(event_id, now.isoformat())
                await _delete_event_access_role(
                    db,
                    await self._event_guild_for_event(ev, voice),
                    ev,
                    now.isoformat(),
                    reason=f"LFG event #{event_id} cancelled",
                )
                continue

            if voice is None:
                category = await self._event_voice_category_for_event(ev)
                if category is None:
                    continue
                access_role = await _ensure_event_access_role(
                    db,
                    category.guild,
                    ev,
                    now.isoformat(),
                )
                if access_role is None:
                    warning_log(
                        f"event voice create blocked for #{event_id}: "
                        "could not create secure access role."
                    )
                    continue
                try:
                    voice = await category.create_voice_channel(
                        name=_event_voice_channel_name(ev),
                        overwrites=_event_voice_overwrites(category.guild, access_role, category),
                        reason=f"LFG event #{event_id} voice",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    warning_log(f"event voice create failed for #{event_id}: {exc!r}")
                    continue
                db.set_lfg_voice_channel_id(event_id, str(voice.id), now.isoformat())
                ev = db.fetch_lfg_event(event_id) or {**ev, "voice_channel_id": str(voice.id)}
                await _sync_event_access_role_members(
                    db,
                    category.guild,
                    ev,
                    access_role,
                    reason=f"LFG event #{event_id} voice access sync",
                )
                await _refresh_event_message(self.bot, db, event_id)
                await self._post_event_voice_ping(ev, voice)
                continue

            access_role = await _ensure_event_access_role(
                db,
                voice.guild,
                ev,
                now.isoformat(),
            )
            if access_role is not None:
                try:
                    await voice.edit(
                        overwrites=_event_voice_overwrites(voice.guild, access_role, voice.category),
                        reason=f"LFG event #{event_id} secure voice access",
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    warning_log(f"event voice lock failed for #{event_id}: {exc!r}")
                await _sync_event_access_role_members(
                    db,
                    voice.guild,
                    ev,
                    access_role,
                    reason=f"LFG event #{event_id} voice access sync",
                )

            if not ev.get("voice_channel_pinged_at"):
                await self._post_event_voice_ping(ev, voice)

            if now >= review_until and not [m for m in voice.members if not m.bot]:
                try:
                    await voice.delete(reason=f"LFG event #{event_id} voice ended")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    warning_log(f"event voice delete failed for #{event_id}: {exc!r}")
                    continue
                db.mark_lfg_voice_deleted(event_id, now.isoformat())
                await _delete_event_access_role(
                    db,
                    voice.guild,
                    ev,
                    now.isoformat(),
                    reason=f"LFG event #{event_id} voice ended",
                )

    @manage_event_voice_channels.before_loop
    async def _before_manage_event_voice_channels(self) -> None:
        await self.bot.wait_until_ready()

    @manage_event_voice_channels.error
    async def _manage_event_voice_channels_error(self, exc: BaseException) -> None:
        error_log(f"manage_event_voice_channels crashed: {exc!r}; restarting loop.")
        try:
            self.manage_event_voice_channels.restart()
        except Exception as restart_exc:  # pragma: no cover - defensive
            error_log(f"Failed to restart manage_event_voice_channels: {restart_exc!r}")

    async def _delete_event_voice_channel(
        self,
        event: dict,
        now_iso: str,
        *,
        reason: str,
    ) -> bool:
        event_id = int(event["id"])
        voice = await self._fetch_event_voice_channel(event)
        if voice is None:
            if event.get("voice_channel_id"):
                self.bot.db.mark_lfg_voice_deleted(event_id, now_iso)
            await _delete_event_access_role(
                self.bot.db,
                await self._event_guild_for_event(event),
                event,
                now_iso,
                reason=reason,
            )
            return True
        try:
            await voice.delete(reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            warning_log(f"event voice delete failed for #{event_id}: {exc!r}")
            return False
        self.bot.db.mark_lfg_voice_deleted(event_id, now_iso)
        await _delete_event_access_role(
            self.bot.db,
            voice.guild,
            event,
            now_iso,
            reason=reason,
        )
        return True

    async def _archive_lfg_thread(self, event: dict) -> None:
        event_id = int(event["id"])
        thread_id = str(event.get("discussion_thread_id") or "").strip()
        if not thread_id:
            return
        try:
            thread = self.bot.get_channel(int(thread_id)) or await self.bot.fetch_channel(int(thread_id))
            if isinstance(thread, discord.Thread):
                await thread.edit(archived=True, locked=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
            warning_log(f"LFG cleanup: archive thread for event #{event_id} failed: {exc!r}")

    async def _delete_lfg_post(self, event: dict, now_iso: str) -> bool:
        event_id = int(event["id"])
        chan_id = str(event.get("channel_id") or "").strip()
        msg_id = str(event.get("message_id") or "").strip()
        if not chan_id or not msg_id:
            return True
        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
            if not isinstance(channel, discord.TextChannel):
                self.bot.db.mark_lfg_event_cleaned(event_id, now_iso)
                return True
            message = await channel.fetch_message(int(msg_id))
            await message.delete()
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
            warning_log(f"LFG cleanup: delete post for event #{event_id} failed: {exc!r}")
            return False

        self.bot.db.mark_lfg_event_cleaned(event_id, now_iso)
        return True

    async def cleanup_lfg_event_surfaces(
        self,
        event_id: int,
        *,
        reason: str = "LFG cleanup",
    ) -> bool:
        """Clean Discord surfaces for one LFG while keeping DB history."""
        event = self.bot.db.fetch_lfg_event(event_id)
        if not event:
            return False

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ok = True
        if str(event.get("status") or "").lower() == "cancelled":
            ok = await self._delete_event_voice_channel(
                event,
                now_iso,
                reason=f"{reason}: event #{event_id} cancelled",
            )

        await self._archive_lfg_thread(event)
        post_ok = await self._delete_lfg_post(event, now_iso)
        return ok and post_ok

    # ── Finished-event channel cleanup ─────────────────────────────────
    # LFG messages are useful before and during an event, but become noise
    # after the event + review window. This task removes those posts from the
    # configured/event post channel and archives the attached discussion thread
    # when possible. The DB row and signup history stay intact.
    @tasks.loop(minutes=5)
    async def cleanup_finished_lfg_posts(self) -> None:
        db = self.bot.db
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now_dt.isoformat()

        completed = db.archive_completed_events()
        if completed:
            info_log(f"LFG cleanup: archived {completed} completed event(s).")

        rows = db.fetch_lfg_events_to_cleanup(now_iso)
        if not rows:
            await self._cleanup_stale_untracked_lfg_messages(now_dt)
            return

        cleaned = 0
        for ev in rows:
            if await self.cleanup_lfg_event_surfaces(int(ev["id"]), reason="scheduled LFG cleanup"):
                cleaned += 1

        if cleaned:
            info_log(f"LFG cleanup: removed {cleaned} finished event post(s).")

        await self._cleanup_stale_untracked_lfg_messages(now_dt)

    async def _cleanup_stale_untracked_lfg_messages(
        self,
        now_dt: datetime.datetime,
    ) -> None:
        """Remove stale manual chatter from the LFG post channel.

        DB-backed bot LFG posts are handled above. This sweep only touches old,
        unpinned, non-bot messages so ad-hoc pings do not clog the channel.
        """
        raw_hours = self.bot.db.get_config(CFG_UNTRACKED_CLEANUP_HOURS) or "12"
        try:
            hours = max(1, int(raw_hours))
        except (TypeError, ValueError):
            hours = 12
        channel_id = (self.bot.db.get_config(CFG_LFG_CHANNEL) or "").strip()
        if not channel_id:
            return
        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return
        if not isinstance(channel, discord.TextChannel):
            return

        cutoff = now_dt - datetime.timedelta(hours=hours)
        tracked_message_ids: set[int] = set()
        try:
            self.bot.db.cursor.execute(
                """
                SELECT message_id FROM lfg_events
                WHERE message_id IS NOT NULL AND message_id != ''
                  AND lfg_cleaned_at IS NULL
                """
            )
            tracked_message_ids = {
                int(row["message_id"])
                for row in self.bot.db.cursor.fetchall()
                if str(row["message_id"]).isdigit()
            }
        except Exception as exc:  # noqa: BLE001
            warning_log(f"LFG untracked cleanup: tracked-message fetch failed: {exc!r}")
            return

        deleted = 0
        try:
            async for msg in channel.history(limit=100, before=cutoff):
                if msg.pinned:
                    continue
                if msg.id in tracked_message_ids:
                    continue
                if self.bot.user and msg.author.id == self.bot.user.id:
                    continue
                try:
                    await msg.delete()
                    deleted += 1
                except (discord.Forbidden, discord.HTTPException):
                    continue
        except (discord.Forbidden, discord.HTTPException) as exc:
            warning_log(f"LFG untracked cleanup: history scan failed: {exc!r}")
            return
        if deleted:
            info_log(
                f"LFG cleanup: removed {deleted} stale untracked message(s) "
                f"older than {hours}h."
            )

    @cleanup_finished_lfg_posts.before_loop
    async def _before_cleanup_finished_lfg_posts(self) -> None:
        await self.bot.wait_until_ready()

    @cleanup_finished_lfg_posts.error
    async def _cleanup_finished_lfg_posts_error(self, exc: BaseException) -> None:
        error_log(f"cleanup_finished_lfg_posts crashed: {exc!r}; restarting loop.")
        try:
            self.cleanup_finished_lfg_posts.restart()
        except Exception as restart_exc:  # pragma: no cover - defensive
            error_log(
                f"Failed to restart cleanup_finished_lfg_posts: {restart_exc!r}"
            )

    @commands.Cog.listener()
    async def on_ready(self):
        # Run-once per process.
        if getattr(self, "_ready_done", False):
            return
        self._ready_done = True

        # Re-register the persistent control panel view.
        self.bot.add_view(EventBoardView(self.bot))

        # Re-register one signup view per still-open event so existing posted
        # messages keep working after a restart.
        open_events = self.bot.db.fetch_open_lfg_events()
        for ev in open_events:
            self.bot.add_view(EventSignupView(int(ev["id"])))
        if open_events:
            self.bot.loop.create_task(self._refresh_open_lfg_messages(open_events))

    async def _refresh_open_lfg_messages(self, events: list[dict]) -> None:
        """Refresh open LFG posts after startup so component changes deploy."""
        refreshed = 0
        for ev in events:
            if not ev.get("channel_id") or not ev.get("message_id"):
                continue
            await _refresh_event_message(self.bot, self.bot.db, int(ev["id"]))
            refreshed += 1
        if refreshed:
            info_log(f"LFG: refreshed {refreshed} open event post(s) after startup.")

    # ── Scheduled-event ↔ LFG signup sync ──────────────────────────────
    # Members hitting "Interested" on the in-client scheduled event get
    # added to the LFG roster, and removing Interest withdraws them.
    # Requires GUILD_SCHEDULED_EVENTS intent (default for discord.py).

    async def _refresh_lfg_message(self, event_row: dict) -> None:
        """Edit the posted LFG message in place to reflect current signups.

        Used by gateway listeners (where there's no Interaction). Best-effort:
        any failure (deleted message, missing channel, perms) is logged but
        does not raise.
        """
        chan_id = event_row.get("channel_id")
        msg_id = event_row.get("message_id")
        if not chan_id or not msg_id:
            return
        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
            if not isinstance(channel, discord.TextChannel):
                return
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(embed=_format_event_embed(self.bot.db, event_row))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as exc:
            warning_log(f"refresh LFG message #{event_row.get('id')} failed: {exc!r}")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-sign members when they are moved into an event voice channel.

        Officers often drag people into a secure event VC after form-up. That
        should count as LFG participation intent too, otherwise analytics,
        access-role sync, loot splits, and regear context undercount the run.
        """
        if member.bot:
            return
        if after.channel is None:
            return
        if before.channel and before.channel.id == after.channel.id:
            return
        if not isinstance(after.channel, discord.VoiceChannel):
            return

        event = self.bot.db.fetch_lfg_event_by_voice_channel_id(str(after.channel.id))
        if not event:
            return

        event_id = int(event["id"])
        added = self.bot.db.add_lfg_signup(event_id, str(member.id))
        if not added:
            return

        info_log(
            f"LFG #{event_id}: auto-signed {member} via event voice join "
            f"({after.channel.name})."
        )

        try:
            _on_first_event_signup(
                self.bot,
                self.bot.db,
                discord_id=str(member.id),
                event=event,
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"first-event hook failed for voice auto-signup {member}: {exc!r}")

        await _grant_event_access_role(
            self.bot.db,
            after.channel.guild,
            event,
            member.id,
            reason=f"LFG #{event_id} event voice auto-signup",
        )
        event = self.bot.db.fetch_lfg_event(event_id) or event
        await self._refresh_lfg_message(event)
        await _refresh_prime_claim_dashboards(self.bot, event, "signup")

    @commands.Cog.listener()
    async def on_scheduled_event_user_add(
        self,
        event: discord.ScheduledEvent,
        user: discord.User,
    ) -> None:
        if user.bot:
            return
        row = self.bot.db.fetch_lfg_event_by_scheduled_id(str(event.id))
        if not row or row.get("status") != "open":
            return
        added = self.bot.db.add_lfg_signup(int(row["id"]), str(user.id))
        if added:
            info_log(
                f"LFG #{row['id']}: signed up {user} via scheduled-event Interested"
            )
            row = self.bot.db.fetch_lfg_event(int(row["id"])) or row
            await self._refresh_lfg_message(row)

    @commands.Cog.listener()
    async def on_scheduled_event_user_remove(
        self,
        event: discord.ScheduledEvent,
        user: discord.User,
    ) -> None:
        if user.bot:
            return
        row = self.bot.db.fetch_lfg_event_by_scheduled_id(str(event.id))
        if not row or row.get("status") != "open":
            return
        removed = self.bot.db.remove_lfg_signup(int(row["id"]), str(user.id))
        if removed:
            info_log(
                f"LFG #{row['id']}: withdrew {user} via scheduled-event un-Interest"
            )
            row = self.bot.db.fetch_lfg_event(int(row["id"])) or row
            await self._refresh_lfg_message(row)

    # ── Slash commands ──────────────────────────────────────────────────────
    lfg_group = app_commands.Group(name="lfg", description="LFG / event board commands.")

    @lfg_group.command(name="post-board", description="Post the event board control panel in this channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def post_board(self, interaction: discord.Interaction) -> None:
        msg = await interaction.channel.send(embed=_board_embed(), view=EventBoardView(self.bot))
        self.bot.db.set_config(CFG_BOARD_CHANNEL, str(msg.channel.id))
        await interaction.response.send_message(
            embed=success_embed("Board posted", f"Control panel posted in {msg.channel.mention}."),
            ephemeral=True,
        )

    @lfg_group.command(name="set-post-channel", description="Set the channel where created events get posted.")
    @app_commands.default_permissions(manage_guild=True)
    async def set_post_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config(CFG_LFG_CHANNEL, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed("LFG post channel set", f"Created events will be posted in {channel.mention}."),
            ephemeral=True,
        )

    @lfg_group.command(
        name="set-reminder-lead",
        description="Minutes before an event starts to DM signups (1–180).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(minutes="Lead time in minutes (1–180). Default is 30.")
    async def set_reminder_lead(
        self,
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 1, 180],
    ) -> None:
        self.bot.db.set_config("lfg_reminder_minutes", str(int(minutes)))
        await interaction.response.send_message(
            embed=success_embed(
                "Reminder lead set",
                f"Signups will be DM'd **{int(minutes)} min** before kickoff.",
            ),
            ephemeral=True,
        )

    @lfg_group.command(
        name="my-events",
        description="Show the upcoming LFG events you're signed up for.",
    )
    async def my_events(self, interaction: discord.Interaction) -> None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rows = self.bot.db.fetch_user_upcoming_lfg_events(
            str(interaction.user.id), now_iso, limit=10,
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    "No upcoming events",
                    "You're not signed up for anything. Browse the event board to find a slot.",
                ),
                ephemeral=True,
            )
            return
        lines: list[str] = []
        for r in rows:
            try:
                start_dt = datetime.datetime.fromisoformat(r["starts_at"])
                ts = int(start_dt.timestamp())
                when = f"<t:{ts}:F> (<t:{ts}:R>)"
            except (TypeError, ValueError):
                when = r.get("starts_at") or "?"
            chan_id = r.get("channel_id")
            msg_id = r.get("message_id")
            jump = ""
            if chan_id and msg_id:
                jump = f"  ·  [jump](https://discord.com/channels/{interaction.guild_id}/{chan_id}/{msg_id})"
            lines.append(f"**#{r['id']} {r['title']}**\n{when}{jump}")
        embed = discord.Embed(
            title=f"🗓️ Your upcoming events ({len(rows)})",
            description="\n\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} viewed /lfg my-events ({len(rows)} events)."
        )

    @lfg_group.command(
        name="stats",
        description="Show your (or another member's) LFG attendance stats.",
    )
    @app_commands.describe(
        member="Whose stats to show. Defaults to yourself.",
        days="Lookback window in days (default 30, max 365).",
    )
    async def lfg_stats(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        days: app_commands.Range[int, 1, 365] = 30,
    ) -> None:
        target = member or interaction.user
        since = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=int(days))
        ).isoformat()
        stats = self.bot.db.fetch_user_lfg_attendance(str(target.id), since)
        signups = stats["signups"]
        attended = stats["attended"]
        not_marked_attended = max(0, signups - attended)
        rate = (attended / signups * 100.0) if signups else 0.0

        if signups == 0:
            await interaction.response.send_message(
                embed=info_embed(
                    f"LFG stats — {target.display_name}",
                    f"No event signups in the last **{int(days)} days**.",
                ),
                ephemeral=(member is None),
            )
            return

        body = (
            f"**Window:** last {int(days)} days\n"
            f"**Signed up for:** {signups} event"
            f"{'s' if signups != 1 else ''}\n"
            f"**Marked attended:** {attended}\n"
            f"**Not marked attended:** {not_marked_attended}\n\n"
            f"**Attendance captured:** {rate:.0f}% ({attended}/{signups} signups)"
        )

        color = (
            discord.Color.green() if rate >= 80
            else discord.Color.gold() if attended
            else discord.Color.greyple()
        )
        embed = discord.Embed(
            title=f"📊 LFG stats · {target.display_name}",
            description=body,
            color=color,
        )
        # Self-lookups stay private; officer peeks at someone else go public
        # so the member can see them too if it lands in a shared channel.
        ephemeral = member is None
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        info_log(
            f"{interaction.user} viewed /lfg stats for {target} "
            f"({days}d: signups={signups} attended={attended})."
        )

    # ── /lfg set-comp ──────────────────────────────────────────────────
    async def _comp_name_ac(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete comp names. ``clear`` is offered as a sentinel to
        detach the current comp from an event."""
        try:
            rows = self.bot.db.list_comps(include_archived=False)
        except Exception:
            rows = []
        q = (current or "").lower().strip()
        choices: list[app_commands.Choice[str]] = []
        if not q or "clear".startswith(q):
            choices.append(app_commands.Choice(name="🧹 clear (detach comp)", value="__clear__"))
        for c in rows:
            name = str(c.get("name") or "")
            if q and q not in name.lower():
                continue
            ct = c.get("content_type") or "Other"
            choices.append(app_commands.Choice(
                name=f"{name} · {ct}"[:100], value=name,
            ))
            if len(choices) >= 25:
                break
        return choices[:25]

    @lfg_group.command(
        name="set-comp",
        description="Attach (or clear) a comp template on an event for per-slot signups.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        event_id="Event ID (see footer of the event message)",
        comp="Comp name, or 'clear' to detach the current comp",
    )
    @app_commands.autocomplete(comp=_comp_name_ac)
    async def set_comp(
        self, interaction: discord.Interaction,
        event_id: int, comp: str,
    ) -> None:
        db = self.bot.db
        event = db.fetch_lfg_event(event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed("No such event", f"No event with ID `{event_id}`."),
                ephemeral=True,
            )
            return
        if comp == "__clear__" or comp.strip().lower() == "clear":
            ok = db.set_lfg_event_comp(event_id, None)
            if not ok:
                await interaction.response.send_message(
                    embed=error_embed("Couldn't detach", "Database rejected the update."),
                    ephemeral=True,
                )
                return
            ev2 = db.fetch_lfg_event(event_id) or event
            await self._refresh_lfg_message(ev2)
            await interaction.response.send_message(
                embed=success_embed(
                    "Comp detached",
                    f"Event #{event_id} no longer has a comp. Existing "
                    "build claims have been cleared.",
                ),
                ephemeral=True,
            )
            return
        row = db.fetch_comp(comp)
        if not row:
            await interaction.response.send_message(
                embed=error_embed(
                    "Comp not found",
                    f"No comp named **{comp}**. Pick from the autocomplete "
                    "or create one with `/comp create`.",
                ),
                ephemeral=True,
            )
            return
        ok = db.set_lfg_event_comp(event_id, int(row["id"]))
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Couldn't attach", "Database rejected the update."),
                ephemeral=True,
            )
            return
        ev2 = db.fetch_lfg_event(event_id) or event
        await self._refresh_lfg_message(ev2)
        await interaction.response.send_message(
            embed=success_embed(
                "Comp attached",
                f"**{row['name']}** is now the build roster for event "
                f"#{event_id}. Members will see 🎯 **Pick build** on the "
                "event message. Existing build claims were cleared if the "
                "comp changed.",
            ),
            ephemeral=True,
        )

    # Apply autocomplete after method definition (decorator-style apply).
    set_comp.autocomplete("comp")(_comp_name_ac)  # type: ignore[arg-type]

    @lfg_group.command(name="show-config", description="Show the current LFG channel configuration.")
    @app_commands.default_permissions(manage_guild=True)
    async def show_config(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        board = db.get_config(CFG_BOARD_CHANNEL)
        post = db.get_config(CFG_LFG_CHANNEL)

        def _fmt_chan(cid: str | None) -> str:
            return f"<#{cid}>" if cid else "_not set_"

        def _fmt_role(rid: str | None) -> str:
            return f"<@&{rid}>" if rid else "_none_"

        type_lines = []
        for t in EVENT_TYPES:
            rid = db.get_config(f"{CFG_ROLE_PREFIX}{t.key}")
            cid = db.get_config(f"{CFG_CHAN_PREFIX}{t.key}")
            type_lines.append(
                f"{t.emoji} **{t.label}** — role: {_fmt_role(rid)} · channel: {_fmt_chan(cid) if cid else '_default_'}"
            )

        embed = info_embed(
            "LFG configuration",
            f"**Board channel:** {_fmt_chan(board)}\n"
            f"**Default post channel:** {_fmt_chan(post)}\n\n"
            "**Per event type:**\n" + "\n".join(type_lines),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @lfg_group.command(
        name="auto-config",
        description="Auto-detect event-type roles and channels by scanning the guild.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        force="Wipe all per-type role/channel mappings first, then re-detect. "
              "Use after the keyword logic has been tightened.",
    )
    async def auto_config(self, interaction: discord.Interaction, force: bool = False) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = auto_discover_config(self.bot.db, interaction.guild, force=force)
        body = "\n".join(f"**{k}:** {v}" for k, v in summary.items())
        await interaction.followup.send(
            embed=info_embed(
                "Auto-config complete" + (" (forced re-detect)" if force else ""),
                body or "_Nothing matched. Run `/lfg scan-guild` and share the output._",
            ),
            ephemeral=True,
        )
        info_log(f"/lfg auto-config run by {interaction.user} (force={force}): {summary}")

    async def _event_type_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        # Discord caps autocomplete responses at 25 entries. We have ~29
        # event types, so substring-filter the user's typed query and slice.
        q = current.lower().strip()
        results: list[app_commands.Choice[str]] = []
        for t in EVENT_TYPES:
            if q and q not in t.label.lower() and q not in t.key.lower() and q not in t.category.lower():
                continue
            results.append(app_commands.Choice(
                name=f"{t.emoji} {t.label}  — {t.category}",
                value=t.key,
            ))
            if len(results) >= 25:
                break
        return results

    @lfg_group.command(
        name="set-type-role",
        description="Manually set the ping role for an event type.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(event_type=_event_type_autocomplete)
    async def set_type_role(
        self,
        interaction: discord.Interaction,
        event_type: str,
        role: discord.Role,
    ) -> None:
        t = EVENT_TYPES_BY_KEY.get(event_type)
        if t is None:
            await interaction.response.send_message(
                embed=error_embed("Unknown type", f"`{event_type}` is not a valid event type."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(f"{CFG_ROLE_PREFIX}{t.key}", str(role.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Type role set",
                f"{t.emoji} {t.label} → {role.mention}",
            ),
            ephemeral=True,
        )

    @lfg_group.command(
        name="set-type-channel",
        description="Manually set the post channel for an event type.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(event_type=_event_type_autocomplete)
    async def set_type_channel(
        self,
        interaction: discord.Interaction,
        event_type: str,
        channel: discord.TextChannel,
    ) -> None:
        t = EVENT_TYPES_BY_KEY.get(event_type)
        if t is None:
            await interaction.response.send_message(
                embed=error_embed("Unknown type", f"`{event_type}` is not a valid event type."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(f"{CFG_CHAN_PREFIX}{t.key}", str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Type channel set",
                f"{t.emoji} {t.label} → {channel.mention}",
            ),
            ephemeral=True,
        )

    @lfg_group.command(
        name="unset-type-channel",
        description="Clear the per-event-type post channel override (falls back to default).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(event_type=_event_type_autocomplete)
    async def unset_type_channel(
        self,
        interaction: discord.Interaction,
        event_type: str,
    ) -> None:
        t = EVENT_TYPES_BY_KEY.get(event_type)
        if t is None:
            await interaction.response.send_message(
                embed=error_embed("Unknown type", f"`{event_type}` is not a valid event type."),
                ephemeral=True,
            )
            return
        # set_config to empty string is the documented "clear" idiom in this
        # codebase; get_config(...) returns it as falsy in the auto-config
        # check, so events of this type will fall back to CFG_LFG_CHANNEL.
        self.bot.db.set_config(f"{CFG_CHAN_PREFIX}{t.key}", "")
        await interaction.response.send_message(
            embed=success_embed(
                "Type channel cleared",
                f"{t.emoji} {t.label} will now post to the default LFG channel.",
            ),
            ephemeral=True,
        )

    @lfg_group.command(
        name="unset-type-role",
        description="Clear the per-event-type ping role (no role pinged on post).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.autocomplete(event_type=_event_type_autocomplete)
    async def unset_type_role(
        self,
        interaction: discord.Interaction,
        event_type: str,
    ) -> None:
        t = EVENT_TYPES_BY_KEY.get(event_type)
        if t is None:
            await interaction.response.send_message(
                embed=error_embed("Unknown type", f"`{event_type}` is not a valid event type."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(f"{CFG_ROLE_PREFIX}{t.key}", "")
        await interaction.response.send_message(
            embed=success_embed(
                "Type role cleared",
                f"{t.emoji} {t.label} will no longer ping any role on post.",
            ),
            ephemeral=True,
        )

    @lfg_group.command(
        name="scan-guild",
        description="Dump all visible channels + roles to a file (for debugging / configuration).",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def scan_guild(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        text = build_guild_scan_text(guild, self.bot.db)
        # Also refresh the on-disk copy so it's available without invoking the command.
        write_guild_scan_file(guild, self.bot.db)

        import io
        buf = io.BytesIO(text.encode("utf-8"))
        file = discord.File(buf, filename=f"guild-scan-{guild.id}.txt")
        await interaction.followup.send(
            embed=info_embed(
                "Guild scan complete",
                f"{len(guild.channels)} channels · {len(guild.roles)} roles · "
                "see attached file. A copy is also kept at `data/guild-scan-<id>.txt` "
                "(gitignored) and refreshed automatically on every hourly inventory sync.",
            ),
            file=file,
            ephemeral=True,
        )
        info_log(f"/lfg scan-guild run by {interaction.user}")

    @lfg_group.command(
        name="dump-channel",
        description="Export the last N messages from a channel to data/channel-dump-<id>.txt (gitignored).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        channel="The text channel to read from.",
        limit="How many recent messages to grab (default 100, max 1000).",
    )
    async def dump_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        limit: int = 100,
    ) -> None:
        """Pull recent message history from a channel for offline review.

        Writes a Markdown-ish text file (newest-first) into ``data/`` so the
        assistant can read it directly from the workspace. The path matches
        the ``data/channel-dump-*.txt`` glob in ``.gitignore`` and never gets
        committed. Skips bot/system messages by default for noise reduction;
        embeds are summarized to title + description so we don't lose the
        information content.
        """
        # Hard-cap to 1000 to avoid pinning the bot for minutes on huge dumps.
        # discord.py paginates internally; 1000 is ~10 round trips.
        limit = max(1, min(int(limit), 1000))

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            messages: list[discord.Message] = [
                m async for m in channel.history(limit=limit, oldest_first=False)
            ]
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Missing permission",
                    f"I can't read message history in {channel.mention}. "
                    "Grant me **View Channel** + **Read Message History** there.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            # Some 403s (e.g. error code 50001 "Missing Access") arrive as a
            # plain HTTPException depending on discord.py version. Treat any
            # 403 here as a permission issue with a friendly message; surface
            # other HTTP errors as-is.
            if getattr(exc, "status", None) == 403:
                await interaction.followup.send(
                    embed=error_embed(
                        "Missing access",
                        f"Discord refused access to {channel.mention} "
                        "(error 50001). Grant me **View Channel** there.",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                embed=error_embed("Discord error", f"Couldn't fetch history: {exc!s}"),
                ephemeral=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            # Catch-all so a transient gateway hiccup doesn't bubble up to
            # the global app-command error handler — the operator just sees
            # a clean ephemeral message instead of an interaction-failed.
            error_log(f"dump-channel: unexpected error reading {channel}: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Unexpected error", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        # Render newest-first so the most recent activity is at the top of
        # the file — easier to skim for "what's been happening lately".
        lines: list[str] = []
        lines.append(f"# Channel dump: #{channel.name} (id={channel.id})")
        if interaction.guild:
            lines.append(f"# Guild: {interaction.guild.name} (id={interaction.guild.id})")
        lines.append(f"# Generated: {datetime.datetime.now(datetime.timezone.utc).isoformat()}")
        lines.append(f"# Messages: {len(messages)} (newest first)")
        lines.append("")

        for m in messages:
            ts = m.created_at.strftime("%Y-%m-%d %H:%M UTC")
            author = f"{m.author.display_name} ({m.author.name})"
            edited = " [edited]" if m.edited_at else ""
            pinned = " 📌" if m.pinned else ""
            lines.append(f"## [{ts}] {author}{pinned}{edited}")

            body = (m.content or "").strip()
            if body:
                # Quote-style indent so the message body is visually distinct
                # from headers when scanning.
                for ln in body.splitlines():
                    lines.append(f"> {ln}")

            # Embeds — keep the text we'd care about; drop the chrome.
            for i, emb in enumerate(m.embeds, 1):
                title = emb.title or ""
                desc = (emb.description or "").strip()
                if title or desc:
                    lines.append(f"[embed {i}] **{title}**" if title else f"[embed {i}]")
                    if desc:
                        for ln in desc.splitlines():
                            lines.append(f"> {ln}")
                for f in emb.fields:
                    lines.append(f"  • _{f.name}_: {f.value}")

            # Attachments — record filename + URL only (we don't download).
            for a in m.attachments:
                lines.append(f"[attachment] {a.filename}  ({a.url})")

            # Reactions — useful signal for "popular event posts".
            if m.reactions:
                tally = " ".join(f"{r.emoji}×{r.count}" for r in m.reactions)
                lines.append(f"[reactions] {tally}")

            lines.append("")  # blank line between messages

        text = "\n".join(lines)

        # Persist to disk for the assistant. Best-effort; never block the
        # interaction response on the file write.
        path = ""
        try:
            import os
            os.makedirs("data", exist_ok=True)
            path = os.path.join("data", f"channel-dump-{channel.id}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            error_log(f"dump-channel: failed to write file: {exc!r}")

        # Also attach the file to the ephemeral reply so the operator can
        # download it directly without SSHing into the Pi.
        import io
        buf = io.BytesIO(text.encode("utf-8"))
        file = discord.File(buf, filename=f"channel-dump-{channel.id}.txt")

        await interaction.followup.send(
            embed=info_embed(
                "Channel dump complete",
                f"Pulled **{len(messages)}** messages from {channel.mention}." +
                (f"\nSaved to `{path}` (gitignored)." if path else ""),
            ),
            file=file,
            ephemeral=True,
        )
        info_log(
            f"/lfg dump-channel run by {interaction.user}: "
            f"#{channel.name} ({len(messages)} msgs)"
        )

    # ── Staff perms application ────────────────────────────────────────
    async def _apply_perm_scheme(
        self,
        interaction: discord.Interaction,
        scheme: dict[str, tuple[str, ...]],
        *,
        action_label: str,
    ) -> None:
        """Shared body for apply / reset commands.

        ``scheme`` maps role name → tuple of perm-flag names. An empty tuple
        means "clear all perms on this role". Skips managed roles (integrations
        own those) and skips roles positioned at or above the bot's top role
        (Discord forbids editing them).
        """
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this in a guild."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        me = guild.me or guild.get_member(self.bot.user.id)  # type: ignore[union-attr]
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.followup.send(
                embed=error_embed(
                    "Missing permission",
                    "I need the **Manage Roles** permission to edit role permissions.",
                ),
                ephemeral=True,
            )
            return
        my_top = me.top_role.position

        # Lower-case index for friendly matching.
        targets = {name.lower(): flags for name, flags in scheme.items()}

        applied: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        missing: list[str] = list(scheme.keys())

        for role in guild.roles:
            flags = targets.get(role.name.lower())
            if flags is None:
                continue
            # Found one — drop it from the missing list.
            with contextlib.suppress(ValueError):
                missing.remove(role.name)

            if role.managed:
                skipped.append(f"{role.name} (managed by integration)")
                continue
            if role.is_default():
                skipped.append(f"{role.name} (@everyone)")
                continue
            if role.position >= my_top:
                skipped.append(
                    f"{role.name} (position {role.position} ≥ my top role {my_top})"
                )
                continue

            # Build the new Permissions object explicitly. Starting from a
            # zeroed Permissions object means we *replace* whatever was there
            # — desirable here because the scheme is the source of truth.
            new_perms = discord.Permissions.none()
            unknown_flags: list[str] = []
            for flag in flags:
                if not hasattr(new_perms, flag):
                    unknown_flags.append(flag)
                    continue
                setattr(new_perms, flag, True)
            if unknown_flags:
                failed.append(f"{role.name}: unknown perm flag(s) {unknown_flags}")
                continue

            try:
                await role.edit(
                    permissions=new_perms,
                    reason=f"{action_label} by {interaction.user}",
                )
                applied.append(f"{role.name} → {len(flags)} perms")
            except discord.Forbidden:
                failed.append(f"{role.name} (Forbidden — check role hierarchy)")
            except discord.HTTPException as exc:
                failed.append(f"{role.name} ({exc!s})")

        # Build a compact result embed.
        parts: list[str] = []
        if applied:
            parts.append("**Applied**\n" + "\n".join(f"• {x}" for x in applied))
        if skipped:
            parts.append("**Skipped**\n" + "\n".join(f"• {x}" for x in skipped))
        if failed:
            parts.append("**Failed**\n" + "\n".join(f"• {x}" for x in failed))
        if missing:
            parts.append(
                "**Not found in guild**\n" + "\n".join(f"• {x}" for x in missing)
            )

        await interaction.followup.send(
            embed=success_embed(
                f"{action_label} complete",
                "\n\n".join(parts) if parts else "No changes.",
            ),
            ephemeral=True,
        )
        info_log(
            f"/lfg {action_label.lower()} by {interaction.user}: "
            f"applied={len(applied)} skipped={len(skipped)} "
            f"failed={len(failed)} missing={len(missing)}"
        )

    @lfg_group.command(
        name="apply-staff-perms",
        description="Apply the canonical staff-role permission scheme to this guild.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def apply_staff_perms(self, interaction: discord.Interaction) -> None:
        """Apply :data:`STAFF_PERMISSION_SCHEME` to matching roles in the guild.

        The scheme is the single source of truth — running this command will
        *replace* the current permissions on each matched role with exactly
        the flags listed in the scheme. To revert, run ``/lfg reset-staff-perms``.
        """
        await self._apply_perm_scheme(
            interaction, STAFF_PERMISSION_SCHEME, action_label="Apply staff perms"
        )

    @lfg_group.command(
        name="reset-staff-perms",
        description="Clear all permissions from the staff roles managed by the scheme.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def reset_staff_perms(self, interaction: discord.Interaction) -> None:
        """Strip every permission from each role listed in :data:`STAFF_PERMISSION_SCHEME`.

        This restores the "decorative-only" state so you can audit the change
        from a known baseline. Members keep the role for hoisting/ping purposes
        but lose all permissions tied to it.
        """
        cleared = {name: () for name in STAFF_PERMISSION_SCHEME}
        await self._apply_perm_scheme(
            interaction, cleared, action_label="Reset staff perms"
        )

    # ── Channel layout proposal/application ────────────────────────────
    @lfg_group.command(
        name="propose-layout",
        description="Show a dry-run diff of the recommended channel layout vs. the current one.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def propose_layout(self, interaction: discord.Interaction) -> None:
        """Print the diff for :data:`DESIRED_LAYOUT`. Safe — touches nothing."""
        await self._run_layout(interaction, dry_run=True, apply_perms=False)

    @lfg_group.command(
        name="apply-layout",
        description="Apply the recommended channel layout (creates + moves only, never deletes).",
    )
    @app_commands.describe(
        apply_perms="Also (re)apply the category permission overwrites for template categories.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def apply_layout(
        self, interaction: discord.Interaction, apply_perms: bool = True,
    ) -> None:
        """Execute the layout. Creates missing categories/channels and moves
        listed channels to the right category. Optionally applies the
        category-level permission overwrites defined in
        :data:`LAYOUT_CATEGORY_OVERWRITES`. Never deletes channels.
        """
        await self._run_layout(interaction, dry_run=False, apply_perms=apply_perms)

    @lfg_group.command(
        name="cleanup-duplicates",
        description="Delete plain-named channels/categories that have an emoji-prefixed twin in the template.",
    )
    @app_commands.describe(
        confirm="Set to True to actually delete. Default is a dry run that lists what would be deleted.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def cleanup_duplicates(
        self, interaction: discord.Interaction, confirm: bool = False,
    ) -> None:
        """Find channels/categories whose name is the "plain version" of a
        template entry (e.g. ``announcements`` while the template has
        ``📢-announcements``) and delete them.

        A guild item is a duplicate when:
        * its name doesn't match any template entry exactly, AND
        * stripping leading non-alphanumeric chars from a template name
          produces this item's name (case-insensitive).

        Dry run by default. Pass ``confirm:True`` to actually delete. The
        deletion is permanent — Discord does not soft-delete channels.
        """
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this in a guild."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Build the set of "plain versions" of template names. The plain
        # version is what's left after stripping all leading non-alphanumeric
        # characters (emoji + space/dash) from the template name. We map
        # each plain version back to its template name so the report can
        # show what the duplicate corresponds to.
        def _plainify(name: str) -> str:
            i = 0
            while i < len(name) and not name[i].isalnum():
                i += 1
            return name[i:].strip().lower()

        template_chan_names = {
            ch.lower() for _, chans in DESIRED_LAYOUT for ch, _ in chans
        }
        template_cat_names = {c.lower() for c, _ in DESIRED_LAYOUT}

        # plain_lookup: plainified-name → original template name (for report)
        plain_chan_lookup: dict[str, str] = {}
        for _, chans in DESIRED_LAYOUT:
            for ch_name, _kind in chans:
                plain_chan_lookup[_plainify(ch_name)] = ch_name
        plain_cat_lookup: dict[str, str] = {
            _plainify(c): c for c, _ in DESIRED_LAYOUT
        }

        # Find duplicates. We skip anything that already matches a template
        # name exactly — those are the real ones we want to keep.
        dup_categories: list[tuple[discord.CategoryChannel, str]] = []
        dup_channels: list[tuple[discord.abc.GuildChannel, str]] = []

        for cat in guild.categories:
            if cat.name.lower() in template_cat_names:
                continue  # this IS a template category — keep it
            twin = plain_cat_lookup.get(cat.name.lower())
            if twin:
                dup_categories.append((cat, twin))

        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                continue
            if ch.name.lower() in template_chan_names:
                continue  # template channel — keep
            twin = plain_chan_lookup.get(ch.name.lower())
            if twin:
                dup_channels.append((ch, twin))

        # ── Render report ──────────────────────────────────────────────
        header = "🔍 Cleanup duplicates (dry run)" if not confirm else "🗑️ Deleting duplicates"
        lines: list[str] = []

        if not dup_categories and not dup_channels:
            lines.append("✅ No duplicates found. Server is clean.")
        else:
            if dup_channels:
                lines.append(f"**Channels ({len(dup_channels)})**")
                for ch, twin in dup_channels[:25]:
                    cat_label = ch.category.name if ch.category else "(no category)"
                    lines.append(f"• #{ch.name} _(in {cat_label})_ ↔ template `{twin}`")
                if len(dup_channels) > 25:
                    lines.append(f"… and {len(dup_channels) - 25} more")
            if dup_categories:
                lines.append(f"\n**Categories ({len(dup_categories)})**")
                for cat, twin in dup_categories:
                    n = len(cat.channels)
                    lines.append(
                        f"• {cat.name} (contains {n} channels) ↔ template `{twin}`"
                    )
                lines.append(
                    "_Deleting a category also deletes every channel inside it._"
                )

        deleted: list[str] = []
        failed: list[str] = []

        if confirm and (dup_categories or dup_channels):
            reason = f"cleanup-duplicates by {interaction.user}"
            # Delete duplicate channels first so categories are emptier when
            # we get to them (less surprising in the audit log).
            for ch, _twin in dup_channels:
                try:
                    await ch.delete(reason=reason)
                    deleted.append(f"#{ch.name}")
                except discord.Forbidden:
                    failed.append(f"#{ch.name} (Forbidden)")
                except discord.HTTPException as exc:
                    failed.append(f"#{ch.name} ({exc!s})")
            for cat, _twin in dup_categories:
                try:
                    # Discord requires the category be empty to delete it
                    # cleanly — delete remaining children first.
                    for child in list(cat.channels):
                        try:
                            await child.delete(reason=reason)
                            deleted.append(f"#{child.name}")
                        except discord.Forbidden:
                            failed.append(f"#{child.name} (Forbidden)")
                        except discord.HTTPException as exc:
                            failed.append(f"#{child.name} ({exc!s})")
                    await cat.delete(reason=reason)
                    deleted.append(f"category {cat.name}")
                except discord.Forbidden:
                    failed.append(f"category {cat.name} (Forbidden)")
                except discord.HTTPException as exc:
                    failed.append(f"category {cat.name} ({exc!s})")

            lines.append("")
            lines.append(f"**Deleted:** {len(deleted)}")
            for d in deleted[:25]:
                lines.append(f"• {d}")
            if len(deleted) > 25:
                lines.append(f"… and {len(deleted) - 25} more")
            if failed:
                lines.append(f"\n**Failed:** {len(failed)}")
                for d in failed[:10]:
                    lines.append(f"• {d}")
        elif not confirm and (dup_categories or dup_channels):
            lines.append(
                "\n_Re-run with `confirm:True` to actually delete. "
                "**This cannot be undone.**_"
            )

        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n…(truncated)"
        await interaction.followup.send(
            embed=info_embed(header, text), ephemeral=True
        )
        info_log(
            f"/lfg cleanup-duplicates by {interaction.user}: "
            f"channels={len(dup_channels)} categories={len(dup_categories)} "
            f"confirm={confirm} deleted={len(deleted)} failed={len(failed)}"
        )

    @lfg_group.command(
        name="pin-intros",
        description="Post and pin a one-line intro in each text channel from the layout.",
    )
    @app_commands.describe(
        replace="If True, unpin any existing bot-authored intro before posting a fresh one.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def pin_intros(
        self, interaction: discord.Interaction, replace: bool = False,
    ) -> None:
        """Post a short purpose blurb in every text channel listed in
        :data:`CHANNEL_INTROS` and pin it.

        Skips channels not present in the guild. Skips channels that already
        have a pinned bot-authored message starting with the marker
        ``__INTRO__`` unless ``replace=True``.
        """
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this in a guild."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        marker = "__INTRO__"
        posted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        # Build a name → channel map once. Channel names in DESIRED_LAYOUT
        # are emoji-prefixed; we look them up case-insensitively.
        channels_by_name = {c.name.lower(): c for c in guild.text_channels}

        for ch_name, blurb in CHANNEL_INTROS.items():
            channel = channels_by_name.get(ch_name.lower())
            if channel is None:
                skipped.append(f"#{ch_name} (not found)")
                continue
            try:
                pins = await channel.pins()
            except discord.Forbidden:
                failed.append(f"#{channel.name} (can't read pins)")
                continue

            existing = next(
                (m for m in pins if m.author.id == self.bot.user.id and marker in m.content),
                None,
            )
            if existing and not replace:
                skipped.append(f"#{channel.name} (already pinned)")
                continue
            if existing and replace:
                try:
                    await existing.unpin(reason=f"pin-intros replace by {interaction.user}")
                    await existing.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass

            content = f"{marker}\n{blurb}"
            try:
                msg = await channel.send(content)
                await msg.pin(reason=f"pin-intros by {interaction.user}")
                posted.append(f"#{channel.name}")
            except discord.Forbidden:
                failed.append(f"#{channel.name} (Forbidden)")
            except discord.HTTPException as exc:
                failed.append(f"#{channel.name} ({exc!s})")

        lines = [f"**Posted:** {len(posted)}  **Skipped:** {len(skipped)}  **Failed:** {len(failed)}"]
        if posted:
            lines.append("\n__Posted__")
            lines.extend(posted[:50])
        if skipped:
            lines.append("\n__Skipped__")
            lines.extend(skipped[:50])
        if failed:
            lines.append("\n__Failed__")
            lines.extend(failed[:50])
        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n…(truncated)"
        await interaction.followup.send(
            embed=info_embed("Pin intros", text), ephemeral=True,
        )
        info_log(
            f"/lfg pin-intros by {interaction.user}: "
            f"posted={len(posted)} skipped={len(skipped)} failed={len(failed)} "
            f"replace={replace}"
        )

    # ── Attendance ──────────────────────────────────────────────────────────

    @lfg_group.command(
        name="mark-attended",
        description="Mark a member as having attended an LFG event.",
    )
    @app_commands.describe(
        event_id="The LFG event ID.",
        user="The member who attended.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def mark_attended(
        self,
        interaction: discord.Interaction,
        event_id: int,
        user: discord.Member,
    ) -> None:
        event = self.bot.db.fetch_lfg_event(int(event_id))
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No LFG event with id {event_id}."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.set_signup_attendance(int(event_id), str(user.id), True)
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Could not record attendance."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=info_embed(
                "Attendance recorded",
                f"{user.mention} marked **attended** for event #{event_id}.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} marked {user} attended for event #{event_id}."
        )

    @lfg_group.command(
        name="mark-all-attended",
        description="Mark every signup for an event as attended (bulk).",
    )
    @app_commands.describe(
        event_id="The LFG event ID.",
        confirm="Set True to actually apply the change.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def mark_all_attended(
        self,
        interaction: discord.Interaction,
        event_id: int,
        confirm: bool = False,
    ) -> None:
        event = self.bot.db.fetch_lfg_event(int(event_id))
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No LFG event with id {event_id}."),
                ephemeral=True,
            )
            return
        signups = self.bot.db.fetch_lfg_signups(int(event_id))
        if not signups:
            await interaction.response.send_message(
                embed=info_embed("No signups", "Nothing to mark."),
                ephemeral=True,
            )
            return
        if not confirm:
            await interaction.response.send_message(
                embed=info_embed(
                    "Confirm",
                    f"This would mark **{len(signups)}** signups as attended for event "
                    f"#{event_id} (**{event.get('title')}**). Re-run with `confirm: True`.",
                ),
                ephemeral=True,
            )
            return
        n = 0
        for s in signups:
            if self.bot.db.set_signup_attendance(
                int(event_id), str(s["discord_id"]), True,
            ):
                n += 1
        await interaction.response.send_message(
            embed=info_embed(
                "Bulk attendance recorded",
                f"Marked **{n}/{len(signups)}** signups attended for event #{event_id}.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} bulk-marked {n}/{len(signups)} attended "
            f"for event #{event_id}."
        )

    @lfg_group.command(
        name="recap",
        description="Quick attendance summary for a past LFG event.",
    )
    @app_commands.describe(event_id="The LFG event ID to summarise.")
    @app_commands.default_permissions(manage_guild=True)
    async def recap(
        self,
        interaction: discord.Interaction,
        event_id: int,
    ) -> None:
        event = self.bot.db.fetch_lfg_event(int(event_id))
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No LFG event with id {event_id}."),
                ephemeral=True,
            )
            return
        counts = self.bot.db.fetch_event_attendance(int(event_id))
        signed = counts["signed"]
        attended = counts["attended"]
        not_marked_attended = max(0, signed - attended)
        rate = (attended / signed * 100.0) if signed else 0.0

        try:
            start_dt = datetime.datetime.fromisoformat(event["starts_at"])
            when = f"<t:{int(start_dt.timestamp())}:F>"
        except (TypeError, ValueError):
            when = event.get("starts_at") or "?"

        status = (event.get("status") or "open").lower()
        status_emoji = {"open": "🟢", "cancelled": "🛑", "completed": "✅"}.get(status, "❓")

        lines = [
            f"**When:** {when}",
            f"**Status:** {status_emoji} {status}",
            "",
            f"**Signed up:** {signed}",
            f"**Attended:** {attended}",
            f"**Not marked attended:** {not_marked_attended}",
        ]
        if signed:
            lines.append(f"**Attendance captured:** {rate:.0f}% ({attended}/{signed} signups)")
        if not_marked_attended:
            lines.append(
                f"\n_Tip:_ run `/lfg mark-all-attended event_id:{event_id}` "
                "to bulk-mark remaining signups."
            )
        chan_id = event.get("channel_id")
        msg_id = event.get("message_id")
        if chan_id and msg_id and interaction.guild_id:
            lines.append(
                f"\n[Jump to original post]"
                f"(https://discord.com/channels/{interaction.guild_id}/{chan_id}/{msg_id})"
            )

        color = (
            discord.Color.green() if rate >= 80
            else discord.Color.gold() if attended
            else discord.Color.greyple()
        )
        embed = discord.Embed(
            title=f"📋 Recap · #{event_id} {event.get('title', '?')}",
            description="\n".join(lines),
            color=color,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} viewed /lfg recap #{event_id} "
            f"(signed={signed}, attended={attended}, not_marked={not_marked_attended})."
        )

    @lfg_group.command(
        name="event-report",
        description="Build a detailed attendance, stats, value, and regear report.",
    )
    @app_commands.describe(
        event_id="The LFG event ID to report on.",
        include_killboard="Fetch Albion kill/death events for this report.",
        public="Post visibly in this channel instead of only to you.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def event_report(
        self,
        interaction: discord.Interaction,
        event_id: int,
        include_killboard: bool = True,
        public: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=not public, thinking=True)
        event = self.bot.db.fetch_lfg_event(int(event_id))
        if not event:
            await interaction.followup.send(
                embed=error_embed("Not found", f"No LFG event with id {event_id}."),
                ephemeral=not public,
            )
            return
        try:
            threshold_pct = int(
                self.bot.db.get_config("automation_voice_attendance_min_pct") or "50"
            )
        except (TypeError, ValueError):
            threshold_pct = 50
        try:
            graph_files: list[discord.File] = []
            extra_embeds: list[discord.Embed] = []
            embed = await build_event_report_embed(
                self.bot,
                event,
                threshold_pct=threshold_pct,
                fetch_killboard=include_killboard,
                include_graph=True,
                graph_files=graph_files,
                extra_embeds=extra_embeds,
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"/lfg event-report #{event_id} failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Report failed",
                    "I could not build that report. Check the bot logs for details.",
                ),
                ephemeral=True,
            )
            return
        report_embeds = [embed, *extra_embeds]
        for idx, embed_batch in enumerate(batch_embeds_for_send(report_embeds)):
            kwargs: dict = {
                "embeds": embed_batch,
                "ephemeral": not public,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if idx == 0:
                if public:
                    kwargs["view"] = build_event_report_view(event_id)
                if graph_files:
                    kwargs["file"] = graph_files[0]
            await interaction.followup.send(**kwargs)
        info_log(
            f"{interaction.user} generated /lfg event-report #{event_id} "
            f"killboard={include_killboard} public={public}."
        )

    async def _run_layout(
        self,
        interaction: discord.Interaction,
        *,
        dry_run: bool,
        apply_perms: bool,
    ) -> None:
        """Shared body for propose-layout / apply-layout.

        Builds an ordered action list from :data:`DESIRED_LAYOUT`, prints it,
        and (when ``dry_run=False``) executes the safe subset: create category,
        create text channel, move channel-to-category. Renames and overwrite
        edits are *suggested* in the diff but never applied automatically.
        """
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this in a guild."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Index existing state by lowercase name for friendly matching.
        cats_by_name: dict[str, discord.CategoryChannel] = {
            c.name.lower(): c for c in guild.categories
        }
        chans_by_name: dict[str, discord.abc.GuildChannel] = {
            c.name.lower(): c
            for c in guild.channels
            if not isinstance(c, discord.CategoryChannel)
        }

        actions: list[tuple[str, str]] = []  # (verb, description)

        # Walk the desired layout. ``DESIRED_LAYOUT`` is a list of
        # (category_name, [(channel_name, kind), ...]) so we can keep a
        # stable order for the report.
        for cat_name, channels in DESIRED_LAYOUT:
            cat_obj = cats_by_name.get(cat_name.lower())
            if cat_obj is None:
                actions.append(("CREATE_CATEGORY", cat_name))

            for ch_name, kind in channels:
                existing = chans_by_name.get(ch_name.lower())
                if existing is None:
                    actions.append(
                        ("CREATE_CHANNEL", f"{ch_name} ({kind}) → {cat_name}")
                    )
                else:
                    cur_cat = existing.category.name if existing.category else "(none)"
                    if cur_cat.lower() != cat_name.lower():
                        actions.append((
                            "MOVE_CHANNEL",
                            f"#{existing.name}: {cur_cat} → {cat_name}",
                        ))
                    # Same category — nothing to do; quiet success.

        # Anything in the guild that isn't in DESIRED_LAYOUT? Just list it
        # as informational so the operator can decide manually whether to
        # archive/delete. We never touch these.
        wanted_chan_names = {
            ch.lower() for _, chans in DESIRED_LAYOUT for ch, _ in chans
        }
        wanted_cat_names = {c.lower() for c, _ in DESIRED_LAYOUT}
        unmanaged: list[str] = []
        for ch in guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                if ch.name.lower() not in wanted_cat_names:
                    unmanaged.append(f"category #{ch.name}")
            elif ch.name.lower() not in wanted_chan_names:
                cat = ch.category.name if ch.category else "(none)"
                unmanaged.append(f"#{ch.name} ({cat})")

        # ── Render the report ─────────────────────────────────────────
        header = "🔍 Layout proposal" if dry_run else "🛠 Applying layout"
        report_lines: list[str] = []
        if not actions:
            report_lines.append("✅ No changes needed — guild already matches the desired layout.")
        else:
            grouped: dict[str, list[str]] = {}
            for verb, desc in actions:
                grouped.setdefault(verb, []).append(desc)
            for verb in ("CREATE_CATEGORY", "CREATE_CHANNEL", "MOVE_CHANNEL"):
                items = grouped.get(verb)
                if not items:
                    continue
                report_lines.append(f"**{verb}** ({len(items)})")
                for d in items[:25]:  # keep the embed short
                    report_lines.append(f"• {d}")
                if len(items) > 25:
                    report_lines.append(f"… and {len(items) - 25} more")

        if unmanaged:
            report_lines.append(
                f"\n*Not in template ({len(unmanaged)} items — left alone):*"
            )
            for d in unmanaged[:10]:
                report_lines.append(f"• {d}")
            if len(unmanaged) > 10:
                report_lines.append(f"… and {len(unmanaged) - 10} more")

        # ── Apply phase ───────────────────────────────────────────────
        applied_log: list[str] = []
        failed_log: list[str] = []
        if not dry_run and (actions or apply_perms):
            # Re-resolve categories as we go so newly created ones are usable
            # by subsequent CREATE_CHANNEL actions.
            live_cats: dict[str, discord.CategoryChannel] = {
                c.name.lower(): c for c in guild.categories
            }
            live_chans: dict[str, discord.abc.GuildChannel] = {
                c.name.lower(): c
                for c in guild.channels
                if not isinstance(c, discord.CategoryChannel)
            }
            roles_by_name: dict[str, discord.Role] = {
                r.name.lower(): r for r in guild.roles
            }
            reason = f"layout apply by {interaction.user}"

            # 1. Create categories first so channels have a target.
            for cat_name, _ in DESIRED_LAYOUT:
                if cat_name.lower() in live_cats:
                    continue
                try:
                    new_cat = await guild.create_category(cat_name, reason=reason)
                    live_cats[cat_name.lower()] = new_cat
                    applied_log.append(f"created category {cat_name}")
                except discord.Forbidden:
                    failed_log.append(f"category {cat_name} (Forbidden)")
                except discord.HTTPException as exc:
                    failed_log.append(f"category {cat_name} ({exc!s})")

            # 2. Create + move channels.
            for cat_name, channels in DESIRED_LAYOUT:
                target_cat = live_cats.get(cat_name.lower())
                if target_cat is None:
                    continue  # category create failed; skip its channels
                for ch_name, kind in channels:
                    existing = live_chans.get(ch_name.lower())
                    try:
                        if existing is None:
                            if kind == "voice":
                                new_ch = await guild.create_voice_channel(
                                    ch_name, category=target_cat, reason=reason
                                )
                            else:
                                new_ch = await guild.create_text_channel(
                                    ch_name, category=target_cat, reason=reason
                                )
                            live_chans[ch_name.lower()] = new_ch
                            applied_log.append(f"created #{ch_name} in {cat_name}")
                        else:
                            cur_cat_id = existing.category.id if existing.category else None
                            if cur_cat_id != target_cat.id:
                                await existing.edit(category=target_cat, reason=reason)
                                applied_log.append(
                                    f"moved #{existing.name} → {cat_name}"
                                )
                    except discord.Forbidden:
                        failed_log.append(f"#{ch_name} (Forbidden)")
                    except discord.HTTPException as exc:
                        failed_log.append(f"#{ch_name} ({exc!s})")

            # 3. (Optional) apply category-level permission overwrites.
            #    Channels inherit category overwrites unless they have their
            #    own, so setting at the category is the cleanest, most
            #    auditable approach. We *replace* the category's overwrites
            #    with exactly what's in the scheme — predictable and easy
            #    to revert if needed.
            if apply_perms:
                def build_overwrites(
                    scheme: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
                ) -> tuple[dict[discord.Role, discord.PermissionOverwrite], list[str]]:
                    new_overwrites: dict[discord.Role, discord.PermissionOverwrite] = {}
                    skipped_roles: list[str] = []
                    for role_name, allow_flags, deny_flags in scheme:
                        if role_name == "@everyone":
                            role_obj = guild.default_role
                        else:
                            role_obj = roles_by_name.get(role_name.lower())
                        if role_obj is None:
                            skipped_roles.append(role_name)
                            continue
                        ow = discord.PermissionOverwrite()
                        for flag in allow_flags:
                            if hasattr(ow, flag):
                                setattr(ow, flag, True)
                        for flag in deny_flags:
                            if hasattr(ow, flag):
                                setattr(ow, flag, False)
                        new_overwrites[role_obj] = ow
                    return new_overwrites, skipped_roles

                for cat_name, scheme in LAYOUT_CATEGORY_OVERWRITES.items():
                    target_cat = live_cats.get(cat_name.lower())
                    if target_cat is None:
                        failed_log.append(
                            f"perms for {cat_name} (category not found)"
                        )
                        continue

                    new_overwrites, skipped_roles = build_overwrites(scheme)

                    try:
                        await target_cat.edit(
                            overwrites=new_overwrites, reason=reason
                        )
                        applied_log.append(
                            f"perms on {cat_name} ({len(new_overwrites)} roles)"
                            + (
                                f" — skipped missing: {', '.join(skipped_roles)}"
                                if skipped_roles else ""
                            )
                        )
                        # 4. Sync each child channel's overwrites to the
                        #    category. This is what the Discord UI offers as
                        #    "Sync Now" — channels start cleanly inheriting.
                        for child in target_cat.channels:
                            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                                await child.edit(
                                    sync_permissions=True, reason=reason
                                )
                    except discord.Forbidden:
                        failed_log.append(f"perms on {cat_name} (Forbidden)")
                    except discord.HTTPException as exc:
                        failed_log.append(f"perms on {cat_name} ({exc!s})")

                # Apply the small set of intentionally unsynced channels
                # after category sync so future layout runs preserve them.
                for ch_name, scheme in LAYOUT_CHANNEL_OVERWRITES.items():
                    target_ch = live_chans.get(ch_name.lower())
                    if target_ch is None:
                        failed_log.append(
                            f"perms for #{ch_name} (channel not found)"
                        )
                        continue
                    new_overwrites, skipped_roles = build_overwrites(scheme)
                    try:
                        await target_ch.edit(
                            overwrites=new_overwrites, reason=reason
                        )
                        applied_log.append(
                            f"perms on #{ch_name} ({len(new_overwrites)} roles)"
                            + (
                                f" — skipped missing: {', '.join(skipped_roles)}"
                                if skipped_roles else ""
                            )
                        )
                    except discord.Forbidden:
                        failed_log.append(f"perms on #{ch_name} (Forbidden)")
                    except discord.HTTPException as exc:
                        failed_log.append(f"perms on #{ch_name} ({exc!s})")

            report_lines.append("")
            report_lines.append(f"**Applied:** {len(applied_log)}")
            for d in applied_log[:25]:
                report_lines.append(f"• {d}")
            if len(applied_log) > 25:
                report_lines.append(f"… and {len(applied_log) - 25} more")
            if failed_log:
                report_lines.append(f"\n**Failed:** {len(failed_log)}")
                for d in failed_log[:10]:
                    report_lines.append(f"• {d}")

        text = "\n".join(report_lines)
        # Discord embed description hard caps at 4096; truncate defensively.
        if len(text) > 3900:
            text = text[:3900] + "\n…(truncated)"

        await interaction.followup.send(
            embed=info_embed(header, text), ephemeral=True
        )
        info_log(
            f"/lfg {'apply' if not dry_run else 'propose'}-layout by "
            f"{interaction.user}: actions={len(actions)} unmanaged={len(unmanaged)} "
            f"applied={len(applied_log)} failed={len(failed_log)}"
        )

    # ── /lfg readycheck ────────────────────────────────────────────────────
    @lfg_group.command(
        name="readycheck",
        description="Pre-event readiness report: signups, IP, comp coverage.",
    )
    @app_commands.describe(
        event_id="LFG event ID (see the event embed footer).",
        comp="Optional comp name to compare signups against.",
        min_ip="Override the IP floor (defaults to lowest comp slot IP or 1100).",
    )
    async def readycheck(
        self, interaction: discord.Interaction,
        event_id: int,
        comp: str | None = None,
        min_ip: app_commands.Range[int, 0, 2000] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        db = self.bot.db
        event = db.fetch_lfg_event(int(event_id))
        if not event:
            await interaction.followup.send(
                embed=error_embed("Not found", f"No LFG event with id `{event_id}`."),
                ephemeral=True,
            )
            return
        signups = db.fetch_lfg_signups(int(event_id))
        signed_ids = [s["discord_id"] for s in signups]

        # Pull IP / albion name from profiles for each signup.
        rows: list[dict] = []
        for did in signed_ids:
            prof = db.fetch_user_profile(did) or {}
            rows.append({
                "discord_id": did,
                "albion_name": prof.get("albion_name"),
                "ip": float(prof.get("average_item_power") or 0),
                "registered": bool(prof.get("albion_player_id")),
            })

        # Comp lookup (explicit or inferred from event.comp_notes).
        comp_row: dict | None = None
        if comp:
            comp_row = db.fetch_comp(comp)
        elif event.get("comp_notes"):
            comp_row = db.fetch_comp(event["comp_notes"].strip())

        # Floor IP: explicit override, else min required IP in comp, else 1100.
        floor = int(min_ip or 0)
        slots: list[dict] = []
        if comp_row:
            slots = db.list_comp_slots(int(comp_row["id"]))
            if not floor and slots:
                ips = [int(s.get("ip_min") or 0) for s in slots if s.get("required")]
                ips = [i for i in ips if i > 0]
                if ips:
                    floor = min(ips)
        if not floor:
            floor = 1100

        # Build voice-presence snapshot via the event's voice snapshot table
        # (populated by the automation cog as the event runs). Pre-event it
        # may be empty — that's fine, we just show 0 confirmed.
        voice_ids: set[str] = set()
        try:
            # Use a direct query — there's no helper for this read.
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT DISTINCT discord_id FROM event_voice_snapshots "
                "WHERE event_id = ?", (int(event_id),),
            )
            voice_ids = {str(r["discord_id"]) for r in db.cursor.fetchall()}
        except Exception:  # noqa: BLE001
            pass

        # Counters.
        n_total = len(rows)
        n_reg = sum(1 for r in rows if r["registered"])
        n_ip_ok = sum(1 for r in rows if r["ip"] >= floor)
        n_voice = sum(1 for r in rows if r["discord_id"] in voice_ids)
        below_ip = [r for r in rows if r["ip"] < floor and r["registered"]]
        unregistered = [r for r in rows if not r["registered"]]

        # Role coverage if we have a comp.
        coverage_lines: list[str] = []
        gaps: list[str] = []
        if slots:
            from collections import Counter
            need = Counter()
            for s in slots:
                if int(s.get("required") or 0):
                    need[(s.get("build_type") or "any").lower()] += 1
            # We don't know which player will fill which role, so just
            # report needs as "still uncovered" relative to signups count.
            need_total = sum(need.values())
            covered = min(n_total, need_total)
            coverage_lines.append(
                f"Required slots filled by headcount: **{covered} / {need_total}**"
            )
            for role, n in need.items():
                coverage_lines.append(f"  • {role}: {n} needed")
            if need_total > n_total:
                gaps.append(
                    f"Need **{need_total - n_total}** more signups to fill required slots."
                )

        # Verdict colour.
        if n_total == 0:
            colour = discord.Colour.red()
        elif gaps or below_ip or unregistered:
            colour = discord.Colour.orange()
        else:
            colour = discord.Colour.green()

        embed = discord.Embed(
            title=f"Ready check — {event['title']}",
            colour=colour,
        )
        embed.description = (
            f"**Event:** #{event_id} · {event.get('event_type') or '—'} · "
            f"{display_slot_label(event.get('slot_label'))}\n"
            f"**Starts:** {event.get('starts_at')}\n"
            f"**Comp:** {comp_row['name'] if comp_row else '_(none)_'}\n"
            f"**IP floor:** {floor}+"
        )

        # Top-line stats.
        embed.add_field(
            name="Roster",
            value=(
                f"✅ **{n_total}** signed up\n"
                f"{'✅' if n_voice >= max(1, n_total // 2) else '⚠️'} "
                f"**{n_voice}** confirmed in voice\n"
                f"{'✅' if n_ip_ok == n_total and n_total > 0 else '⚠️'} "
                f"**{n_ip_ok}/{n_total}** meet {floor}+ IP\n"
                f"{'✅' if n_reg == n_total and n_total > 0 else '⚠️'} "
                f"**{n_reg}/{n_total}** fully registered"
            ),
            inline=False,
        )
        if coverage_lines:
            embed.add_field(
                name="Comp coverage",
                value="\n".join(coverage_lines)[:1024],
                inline=False,
            )
        problems: list[str] = []
        if unregistered:
            problems.append(
                "❌ **Unregistered:** "
                + ", ".join(f"<@{r['discord_id']}>" for r in unregistered[:8])
            )
        if below_ip:
            problems.append(
                "⚠️ **Below IP floor:** "
                + ", ".join(
                    f"<@{r['discord_id']}> ({int(r['ip'])} IP)"
                    for r in below_ip[:8]
                )
            )
        if gaps:
            problems.extend(gaps)
        if problems:
            embed.add_field(
                name="Issues",
                value="\n".join(problems)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Issues",
                value="None detected. Good to launch.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(
            f"{interaction.user} ran /lfg readycheck #{event_id}: "
            f"{n_total} signed, {n_voice} voice, {n_ip_ok} IP-ok, {n_reg} reg, "
            f"comp={comp_row['name'] if comp_row else 'none'}"
        )


async def setup(bot: Bot):
    await bot.add_cog(LFG(bot))
