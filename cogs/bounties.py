"""Public bounty board.

Anyone can propose a bounty; an officer must approve it before it posts to
the public board. Members claim approved bounties, do the work, submit
proof, and an officer approves the payout.

States:
    pending     — proposed by a member; awaiting officer approval to post
    open        — approved & posted; nobody has claimed it yet
    claimed     — a member has called dibs; nobody else can claim
    submitted   — claimer posted proof; awaiting officer review
    completed   — officer approved; points paid (terminal)
    cancelled   — officer cancelled or denied posting (terminal)
    expired     — deadline passed while open/claimed

Slash commands:
    /bounty post              — anyone: propose a new bounty
    /bounty board             — list open / claimed bounties
    /bounty queue             — officer: list bounties awaiting posting approval
    /bounty view <id>         — full details
    /bounty claim <id>        — member: claim an open bounty
    /bounty unclaim <id>      — claimer drops a claim back to open
    /bounty submit <id>       — claimer submits proof for review
    /bounty approve <id>      — officer: publish a pending bounty OR pay out a submission
    /bounty reject <id>       — officer: deny a pending bounty OR send submission back
    /bounty cancel <id>       — officer cancels (no payout)
    /bounty mine              — your active claims
    /bounty config set-channel        — configure the public board channel (officer)
    /bounty config set-review-channel — configure the pending-review channel (officer)

Background loop checks deadlines every 10 minutes and expires overdue ones.
"""
from __future__ import annotations

from cogs._typing import Bot
from time_utils import utc_now_naive
import asyncio
import contextlib
import datetime
import json
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed, warning_embed
from utils import is_officer as _is_officer
from cogs._bounties_roads import (
    ROAD_CORE_EMOJIS,
    ROAD_CORE_REWARDS,
    ROAD_CORE_TITLE_PREFIX,
    RoadsCoreBoardView,
    RoadsCoreColorView,
    image_attachment_url,
    parse_road_core_proof,
    road_core_proof_text,
    road_core_title,
)
from cogs._bounties_sso import SSORouteBoardView, SubmitSSORouteModal

# Constants, formatters, parsers, and the per-bounty embed builder live in
# ``_bounties_config`` (pure presentation, no DB). Schema + queries live in
# ``_bounties_db``. They are re-exported here under their original
# underscore-prefixed names so the cog body / views below don't have to
# change.
from cogs._bounties_config import (
    CFG_BOARD_CHANNEL, CFG_REVIEW_CHANNEL, CFG_FLEX_CHANNEL,
    STATUS_PENDING, STATUS_OPEN, STATUS_CLAIMED, STATUS_SUBMITTED,
    STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED,
    ACTIVE_STATUSES, PUBLIC_STATUSES, STATUS_EMOJI,
    MAX_REWARD,
    SILENT_POINTS_PROPOSER_APPROVED, SILENT_POINTS_CLAIMER_PAID,
    fmt_silver as _fmt_silver,
    now_iso as _now_iso,
    fmt_deadline as _fmt_deadline,
    bounty_to_embed as _bounty_to_embed,
    bounty_needs_payment as _bounty_needs_payment,
)
from cogs._bounties_db import (
    ensure_flex_schema as _ensure_flex_schema,
    player_total_earned as _player_total_earned,
    player_bounty_count as _player_bounty_count,
    top_earners as _top_earners,
    player_rank as _player_rank,
    new_milestone as _new_milestone,
    db_get as _db_get,
    db_list as _db_list,
    db_list_for_user as _db_list_for_user,
    db_update as _db_update,
    db_claim_open as _db_claim_open,
    db_overdue as _db_overdue,
)
from cogs._bounties_tiers import (
    DEFAULT_ENERGY_CORE_TIERS,
    format_tier_scale as _format_tier_scale,
    load_bounty_tier_scale as _load_bounty_tier_scale,
    load_default_tiers as _load_default_tiers,
    save_bounty_tier_scale as _save_bounty_tier_scale,
)



# Discord modals/buttons for bounty posts live in cogs._bounties_views.
from cogs._bounties_views import (
    BountyApproveButton,
    BountyConfirmPaidButton,
    BountyRejectButton,
    _BountyTierPickSelect,
    _PostBountyModal,
    _view_for_bounty,
    register_persistent_bounty_views,
)


# ── Cog ──────────────────────────────────────────────────────────────────────

class Bounties(commands.Cog):
    KILL_BOUNTY_CFG_PREFIX = "bounty_enemy_target:"
    ROADS_CORE_TITLE_PREFIX = ROAD_CORE_TITLE_PREFIX
    PAYMENT_REMINDER_DEFAULT_HOURS = 24
    PAYMENT_REMINDER_DEFAULT_DAYS = 14
    PAYMENT_REMINDER_BUTTON_LIMIT = 5
    PAYMENT_AUDIT_LIMIT = 25

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        self._startup_refresh_task = None
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        register_persistent_bounty_views(self.bot)
        _ensure_flex_schema(self.bot.db)  # type: ignore[attr-defined]
        self._startup_refresh_task = self.bot.loop.create_task(
            self._startup_refresh_bounty_posts()
        )
        self.deadline_check.start()
        self.payment_reminder.start()
        self.daily_energy_core.start()
        self.daily_sso_route.start()

    def cog_unload(self) -> None:
        if self._startup_refresh_task:
            self._startup_refresh_task.cancel()
        self.deadline_check.cancel()
        self.payment_reminder.cancel()
        self.daily_energy_core.cancel()
        self.daily_sso_route.cancel()

    bounty_group = app_commands.Group(
        name="bounty",
        description="Public bounty board — earn silver for guild tasks.",
    )
    config_group = app_commands.Group(
        name="config",
        description="Officer-only bounty board configuration.",
        parent=bounty_group,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── Internal: post or edit the board embed ──────────────────────────────
    def _target_channel_id(self, status: str) -> str | None:
        db = self.bot.db  # type: ignore[attr-defined]
        if status == STATUS_PENDING:
            return db.get_config(CFG_REVIEW_CHANNEL)
        if status in PUBLIC_STATUSES:
            return db.get_config(CFG_BOARD_CHANNEL)
        return None

    def _is_roads_core_bounty(self, bounty: dict) -> bool:
        return str(bounty.get("title") or "").startswith(self.ROADS_CORE_TITLE_PREFIX)

    async def _post_or_update_board_message(self, bounty: dict) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        if self._is_roads_core_bounty(bounty):
            existing_channel_id = bounty.get("channel_id")
            existing_msg_id = bounty.get("message_id")
            if existing_channel_id and existing_msg_id:
                try:
                    chan = self.bot.get_channel(int(existing_channel_id))
                    if isinstance(chan, discord.TextChannel):
                        msg = await chan.fetch_message(int(existing_msg_id))
                        await msg.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                    pass
                _db_update(db, bounty["id"], channel_id=None, message_id=None)
            await self._refresh_roads_core_board(create=True)
            return
        target_channel_id = self._target_channel_id(bounty["status"])
        if bounty["status"] in (STATUS_CANCELLED, STATUS_EXPIRED):
            target_channel_id = None
        elif bounty["status"] == STATUS_COMPLETED and not _bounty_needs_payment(bounty):
            target_channel_id = None
        existing_channel_id = bounty.get("channel_id")
        existing_msg_id = bounty.get("message_id")

        moving = (
            existing_msg_id and existing_channel_id and target_channel_id
            and str(existing_channel_id) != str(target_channel_id)
        )
        if moving:
            try:
                old_chan = self.bot.get_channel(int(existing_channel_id or 0))
                if isinstance(old_chan, discord.TextChannel):
                    try:
                        old_msg = await old_chan.fetch_message(int(existing_msg_id or 0))
                        await old_msg.delete()
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            except (TypeError, ValueError):
                pass
            existing_msg_id = None

        embed = _bounty_to_embed(bounty)
        view = _view_for_bounty(bounty)

        if not target_channel_id:
            # Terminal, non-actionable bounties leave the live board. The DB
            # row remains available through /bounty view and audit history.
            if existing_msg_id and existing_channel_id:
                try:
                    chan = self.bot.get_channel(int(existing_channel_id))
                    if isinstance(chan, discord.TextChannel):
                        msg = await chan.fetch_message(int(existing_msg_id))
                        await msg.delete()
                        _db_update(db, bounty["id"], channel_id=None, message_id=None)
                except discord.NotFound:
                    _db_update(db, bounty["id"], channel_id=None, message_id=None)
                except (discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                    pass
            return

        try:
            channel = self.bot.get_channel(int(target_channel_id))
        except (TypeError, ValueError):
            return
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            if existing_msg_id and not moving:
                try:
                    msg = await channel.fetch_message(int(existing_msg_id))
                    await msg.edit(embed=embed, view=view or discord.ui.View(timeout=None))
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            send_kwargs: dict = {"embed": embed}
            if view is not None:
                send_kwargs["view"] = view
            msg = await channel.send(**send_kwargs)
            _db_update(db, bounty["id"],
                       channel_id=str(channel.id),
                       message_id=str(msg.id))
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"bounty board post failed for #{bounty['id']}: {exc!r}")

    async def _refresh_board_embed(self, bounty_id: int) -> None:
        b = _db_get(self.bot.db, bounty_id)  # type: ignore[attr-defined]
        if b:
            await self._post_or_update_board_message(b)

    # ── Internal: auto-detected kill bounties ──────────────────────────────
    @staticmethod
    def _parse_utc_datetime(raw: str | None) -> datetime.datetime | None:
        """Parse SQLite UTC strings and Albion's nanosecond Z timestamps."""
        if not raw:
            return None
        s = str(raw).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "." in s:
            head, tail = s.split(".", 1)
            tz = ""
            for marker in ("+", "-"):
                if marker in tail:
                    frac, _, rest = tail.partition(marker)
                    tz = marker + rest
                    break
            else:
                frac = tail
            s = f"{head}.{frac[:6]}{tz}"
        try:
            dt = datetime.datetime.fromisoformat(s.replace("T", " "))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def _active_enemy_kill_bounty_targets(self) -> list[tuple[dict, dict]]:
        """Configured enemy-kill bounties that can still receive proof.

        Targets are stored as JSON in guild_config under
        ``bounty_enemy_target:<bounty_id>``. Example:
        ``{"kind":"alliance","name":"BURNR","alliance_id":"..."}``.
        """
        db = self.bot.db  # type: ignore[attr-defined]
        if not db.connection:
            db.connect()
        try:
            db.cursor.execute(
                "SELECT key, value FROM guild_config WHERE key LIKE ?",
                (f"{self.KILL_BOUNTY_CFG_PREFIX}%",),
            )
            rows = [dict(r) for r in db.cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            error_log(f"enemy bounty target query failed: {exc!r}")
            return []

        targets: list[tuple[dict, dict]] = []
        for row in rows:
            key = str(row.get("key") or "")
            try:
                bounty_id = int(key.rsplit(":", 1)[1])
            except (IndexError, TypeError, ValueError):
                continue
            bounty = _db_get(db, bounty_id)
            if not bounty or bounty.get("status") not in (STATUS_OPEN, STATUS_CLAIMED):
                continue
            try:
                target = json.loads(row.get("value") or "{}")
            except (TypeError, ValueError):
                target = {}
            if isinstance(target, dict) and target:
                targets.append((bounty, target))
        return targets

    def has_active_enemy_kill_bounties(self) -> bool:
        return bool(self._active_enemy_kill_bounty_targets())

    @staticmethod
    def _event_includes_player(event: dict, player_id: str) -> bool:
        wanted = (player_id or "").strip()
        if not wanted:
            return False
        killer = event.get("Killer") or {}
        if str(killer.get("Id") or "").strip() == wanted:
            return True
        for key in ("Participants", "GroupMembers"):
            for member in event.get(key) or []:
                if str((member or {}).get("Id") or "").strip() == wanted:
                    return True
        return False

    @staticmethod
    def _victim_matches_enemy_target(event: dict, target: dict) -> bool:
        victim = event.get("Victim") or {}

        def clean(value) -> str:
            return str(value or "").strip().lower()

        target_alliance_id = clean(target.get("alliance_id"))
        target_guild_id = clean(target.get("guild_id"))
        if target_alliance_id and clean(victim.get("AllianceId")) == target_alliance_id:
            return True
        if target_guild_id and clean(victim.get("GuildId")) == target_guild_id:
            return True

        target_names = {
            clean(target.get("name")),
            clean(target.get("tag")),
            clean(target.get("alliance_name")),
            clean(target.get("alliance_tag")),
        }
        target_names.discard("")
        victim_alliance_names = {
            clean(victim.get("AllianceName")),
            clean(victim.get("AllianceTag")),
        }
        if target_names & victim_alliance_names:
            return True

        target_guild_names = {
            clean(target.get("guild_name")),
            *(clean(v) for v in (target.get("guild_names") or [])),
        }
        target_guild_names.discard("")
        if target_guild_names and clean(victim.get("GuildName")) in target_guild_names:
            return True
        return False

    @staticmethod
    def _killboard_url(event_id: str) -> str:
        return f"https://albiononline.com/en/killboard/kill/{event_id}"

    def _format_enemy_kill_proof(
        self,
        *,
        bounty: dict,
        target: dict,
        profile: dict,
        event: dict,
    ) -> str:
        event_id = str(event.get("EventId") or "").strip()
        victim = event.get("Victim") or {}
        killer = event.get("Killer") or {}
        event_dt = self._parse_utc_datetime(event.get("TimeStamp"))
        when = f"<t:{int(event_dt.timestamp())}:F>" if event_dt else str(event.get("TimeStamp") or "Unknown")
        victim_alliance = (
            victim.get("AllianceTag")
            or victim.get("AllianceName")
            or "No alliance"
        )
        target_name = (
            target.get("name")
            or target.get("tag")
            or target.get("alliance_name")
            or target.get("guild_name")
            or "configured enemy"
        )
        lines = [
            "Auto-detected by UnionBot from Albion killboard.",
            f"**Killboard:** {self._killboard_url(event_id)}",
            f"**Target:** {target_name}",
            (
                "**Union credit:** "
                f"<@{profile.get('discord_id')}> "
                f"({profile.get('albion_name') or 'linked Albion character'})"
            ),
            f"**Killer shown:** {killer.get('Name') or 'Unknown'}",
            (
                "**Victim:** "
                f"{victim.get('Name') or 'Unknown'}"
                f" · Guild: {victim.get('GuildName') or 'None'}"
                f" · Alliance: {victim_alliance}"
            ),
            f"**Kill fame:** {int(event.get('TotalVictimKillFame') or 0):,}",
            f"**Event time:** {when}",
            f"**Bounty:** #{bounty.get('id')} — {bounty.get('title')}",
        ]
        return "\n".join(lines)[:1500]

    def _record_enemy_kill_match(
        self,
        *,
        bounty_id: int,
        profile: dict,
        event: dict,
    ) -> bool:
        """Return True when this bounty/event pair was newly recorded."""
        event_id = str(event.get("EventId") or "").strip()
        if not event_id:
            return False
        db = self.bot.db  # type: ignore[attr-defined]
        if not db.connection:
            db.connect()
        victim = event.get("Victim") or {}
        try:
            db.cursor.execute(
                """
                INSERT OR IGNORE INTO bounty_kill_matches (
                    event_id, bounty_id, killer_discord_id, killer_player_id,
                    killer_name, victim_name, victim_guild, victim_alliance,
                    kill_fame, killboard_url, event_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    int(bounty_id),
                    str(profile.get("discord_id") or ""),
                    str(profile.get("albion_player_id") or ""),
                    str(profile.get("albion_name") or ""),
                    str(victim.get("Name") or ""),
                    str(victim.get("GuildName") or ""),
                    str(victim.get("AllianceTag") or victim.get("AllianceName") or ""),
                    int(event.get("TotalVictimKillFame") or 0),
                    self._killboard_url(event_id),
                    str(event.get("TimeStamp") or ""),
                ),
            )
            db.connection.commit()
            return int(db.cursor.rowcount or 0) > 0
        except Exception as exc:  # noqa: BLE001
            error_log(f"record enemy kill match failed for bounty #{bounty_id}: {exc!r}")
            return False

    async def maybe_auto_submit_enemy_kill_bounty(
        self,
        profile: dict,
        kill_events: list[dict],
    ) -> int:
        """Auto-submit configured enemy-kill bounties from recent kill events.

        This intentionally stops at SUBMITTED. Officers still approve the
        payout and confirm the in-game payment.
        """
        if not kill_events:
            return 0
        discord_id = str(profile.get("discord_id") or "")
        player_id = str(profile.get("albion_player_id") or "")
        if not (discord_id and player_id):
            return 0

        submitted = 0
        for bounty, target in self._active_enemy_kill_bounty_targets():
            bounty_id = int(bounty.get("id") or 0)
            claimed_by = str(bounty.get("claimed_by") or "")
            if claimed_by and claimed_by != discord_id:
                continue

            posted_at = self._parse_utc_datetime(bounty.get("posted_at"))
            for event in kill_events:
                if not self._event_includes_player(event, player_id):
                    continue
                if not self._victim_matches_enemy_target(event, target):
                    continue
                event_at = self._parse_utc_datetime(event.get("TimeStamp"))
                if posted_at and event_at and event_at < posted_at - datetime.timedelta(minutes=10):
                    continue
                if not self._record_enemy_kill_match(
                    bounty_id=bounty_id, profile=profile, event=event,
                ):
                    continue

                proof = self._format_enemy_kill_proof(
                    bounty=bounty, target=target, profile=profile, event=event,
                )
                fields = {
                    "status": STATUS_SUBMITTED,
                    "submitted_at": _now_iso(),
                    "proof": proof,
                }
                if not claimed_by:
                    fields["claimed_by"] = discord_id
                    fields["claimed_at"] = _now_iso()
                _db_update(self.bot.db, bounty_id, **fields)  # type: ignore[attr-defined]
                await self._refresh_board_embed(bounty_id)

                user = self.bot.get_user(int(discord_id))
                if user is None:
                    try:
                        user = await self.bot.fetch_user(int(discord_id))
                    except (discord.Forbidden, discord.HTTPException, ValueError):
                        user = None
                if user is not None:
                    with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                        await user.send(embed=success_embed(
                            f"Bounty #{bounty_id} auto-submitted",
                            "UnionBot detected your matching Albion kill and submitted "
                            "the killboard proof for officer approval.",
                        ))
                    try:
                        await self._notify_officers_submission(bounty_id, user)
                    except Exception as exc:  # noqa: BLE001
                        error_log(f"auto bounty officer notice failed #{bounty_id}: {exc!r}")
                info_log(
                    f"Auto-submitted enemy kill bounty #{bounty_id} for "
                    f"{profile.get('albion_name')} via event {event.get('EventId')}."
                )
                submitted += 1
                break
        return submitted

    async def _startup_refresh_bounty_posts(self) -> None:
        """Refresh existing bounty posts after startup so persistent buttons
        and terminal-message cleanup stay current across deploys."""
        await self.bot.wait_until_ready()
        db = self.bot.db  # type: ignore[attr-defined]
        try:
            msg = await self._refresh_sso_route_board(create=True)
            if msg:
                info_log(f"SSO route board refreshed in #{msg.channel.name}.")
        except Exception as exc:  # noqa: BLE001
            error_log(f"sso route board startup refresh failed: {exc!r}")
        try:
            msg = await self._refresh_roads_core_board(create=True)
            if msg:
                info_log(f"Roads core bounty board refreshed in #{msg.channel.name}.")
        except Exception as exc:  # noqa: BLE001
            error_log(f"roads core board startup refresh failed: {exc!r}")
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                """
                SELECT * FROM bounties
                 WHERE message_id IS NOT NULL
                   AND message_id != ''
                   AND status IN (?, ?, ?, ?, ?, ?, ?)
                 ORDER BY posted_at DESC
                 LIMIT 100
                """,
                (
                    STATUS_PENDING,
                    STATUS_OPEN,
                    STATUS_CLAIMED,
                    STATUS_SUBMITTED,
                    STATUS_COMPLETED,
                    STATUS_CANCELLED,
                    STATUS_EXPIRED,
                ),
            )
            rows = [dict(r) for r in db.cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            error_log(f"bounty startup refresh query failed: {exc!r}")
            return

        refreshed = 0
        for bounty in rows:
            try:
                await self._post_or_update_board_message(bounty)
                refreshed += 1
            except Exception as exc:  # noqa: BLE001
                error_log(f"bounty startup refresh #{bounty.get('id')} failed: {exc!r}")
        if refreshed:
            info_log(f"Bounties: refreshed/cleaned {refreshed} existing post(s) after startup.")

    # ── Internal: SSO route board ───────────────────────────────────────────
    def _fetch_sso_route_bounties(
        self,
        *,
        statuses: tuple[str, ...] = (STATUS_SUBMITTED, STATUS_COMPLETED),
        limit: int = 8,
    ) -> list[dict]:
        db = self.bot.db  # type: ignore[attr-defined]
        if not db.connection:
            db.connect()
        placeholders = ",".join("?" * len(statuses))
        db.cursor.execute(
            f"""
            SELECT * FROM bounties
             WHERE title LIKE ?
               AND status IN ({placeholders})
               AND proof IS NOT NULL
               AND proof != ''
             ORDER BY datetime(COALESCE(completed_at, submitted_at, posted_at)) DESC,
                      id DESC
             LIMIT ?
            """,
            (f"{self.SSO_TITLE_PREFIX}%", *statuses, int(limit)),
        )
        return [dict(r) for r in db.cursor.fetchall()]

    @staticmethod
    def _dt_from_iso(raw: str | None) -> datetime.datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.datetime.fromisoformat(str(raw).replace("T", " "))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def _sso_route_expiry(
        self,
        bounty: dict,
        ttl_min: int | None,
    ) -> datetime.datetime | None:
        completed_at = self._dt_from_iso(bounty.get("completed_at"))
        if completed_at is None:
            completed_at = self._dt_from_iso(bounty.get("submitted_at"))
        if completed_at is None:
            return None
        if ttl_min and ttl_min > 0:
            return completed_at + datetime.timedelta(minutes=ttl_min)
        return (completed_at + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _payment_config_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int = 1,
        maximum: int = 365,
    ) -> int:
        db = self.bot.db  # type: ignore[attr-defined]
        try:
            value = int(db.get_config(key) or default)
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _fetch_unpaid_bounty_payouts(self, *, limit: int = PAYMENT_AUDIT_LIMIT) -> list[dict]:
        db = self.bot.db  # type: ignore[attr-defined]
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """
            SELECT *
              FROM bounties
             WHERE status = ?
               AND COALESCE(reward_points, 0) > 0
               AND claimed_by IS NOT NULL
               AND claimed_by != ''
               AND (paid_at IS NULL OR paid_at = '')
             ORDER BY datetime(COALESCE(completed_at, submitted_at, posted_at)) DESC,
                      id DESC
             LIMIT ?
            """,
            (STATUS_COMPLETED, max(1, int(limit))),
        )
        return [dict(r) for r in db.cursor.fetchall()]

    def _bounty_payment_dt(self, bounty: dict) -> datetime.datetime | None:
        return (
            self._dt_from_iso(bounty.get("completed_at"))
            or self._dt_from_iso(bounty.get("submitted_at"))
            or self._dt_from_iso(bounty.get("posted_at"))
        )

    def _bounty_payment_line(self, bounty: dict) -> str:
        approved_at = self._bounty_payment_dt(bounty)
        when = f"<t:{int(approved_at.timestamp())}:R>" if approved_at else "unknown time"
        title = self._clip(str(bounty.get("title") or "Bounty"), 70)
        return (
            f"`#{bounty['id']}` **{title}**\n"
            f"Pay <@{bounty['claimed_by']}> 🪙 **{_fmt_silver(bounty.get('reward_points') or 0)}** "
            f"· approved {when}"
        )

    def _build_payout_audit_embed(
        self,
        rows: list[dict],
        *,
        title: str,
        hidden_recent: int = 0,
        hidden_old: int = 0,
        old_days: int | None = None,
    ) -> discord.Embed:
        lines = [
            "These bounties were approved in the bot, but silver still has to be paid in Albion.",
            "After paying the member in-game, press that bounty's **Paid #** button so the ledger stops chasing it.",
        ]
        if rows:
            lines.append("")
            lines.extend(self._bounty_payment_line(row) for row in rows)
        if hidden_recent > 0:
            lines.append(f"\n**{hidden_recent}** more recent payout(s) are hidden here. Run `/bounty payouts` for the full audit.")
        if hidden_old > 0:
            age_text = f" older than **{old_days} day(s)**" if old_days else ""
            lines.append(f"\nOlder backlog hidden from auto reminders: **{hidden_old}** payout(s){age_text}. Run `/bounty payouts` to audit or clear them.")
        embed = warning_embed(title, "\n\n".join(lines)[:3900])
        embed.set_footer(text="Buttons settle the bot ledger only after the silver has been paid in-game.")
        return embed

    def _sso_route_line(self, portals: list[str], proof: str) -> str:
        if portals:
            return " → ".join(portals[: self.SSO_MAX_PORTALS])
        return proof.replace("\n", " ").strip()

    def _sso_route_board_embed(self) -> discord.Embed:
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = self._fetch_sso_route_bounties(limit=8)
        embed = discord.Embed(
            title="🐎 SSO Routes — current portals",
            description=(
                "One clean board for hideout portal routes. Use **Add / Update Route** "
                "when scouts find a fresh chain. Submitted routes show here right away "
                "so members can use them; officers still approve the bounty payout."
            ),
            color=discord.Color.dark_teal(),
            timestamp=now,
        )

        active: dict | None = None
        active_expiry: datetime.datetime | None = None
        parsed: list[tuple[dict, list[str], str | None, int | None, datetime.datetime | None]] = []
        closed_id = self.bot.db.get_config("sso_routes_closed_bounty_id")  # type: ignore[attr-defined]
        for row in rows:
            proof = (row.get("proof") or "").strip()
            portals, note, ttl_min = self._parse_sso_route(proof)
            expiry = self._sso_route_expiry(row, ttl_min)
            if closed_id and str(row.get("id")) == str(closed_id):
                expiry = now - datetime.timedelta(seconds=1)
            parsed.append((row, portals, note, ttl_min, expiry))
            if active is None and expiry and expiry > now:
                active = row
                active_expiry = expiry

        if active:
            proof = (active.get("proof") or "").strip()
            portals, note, _ttl_min = self._parse_sso_route(proof)
            route = self._clip(self._sso_route_line(portals, proof), 700)
            scouted = f"<@{active.get('claimed_by')}>" if active.get("claimed_by") else "Unknown scout"
            is_pending = active.get("status") == STATUS_SUBMITTED
            close_text = (
                f"<t:{int(active_expiry.timestamp())}:R> "
                f"(<t:{int(active_expiry.timestamp())}:t>)"
                if active_expiry else "Unknown"
            )
            value = (
                f"**Route:** {route}\n"
                f"**Scouted by:** {scouted}\n"
                f"**Closes:** {close_text}"
            )
            if is_pending:
                value += "\n**Status:** awaiting officer approval for bounty payout"
            if note:
                value += f"\n**Note:** {self._clip(note, 220)}"
            field_name = "🟡 Current Route — pending approval" if is_pending else "✅ Current Route"
            embed.add_field(name=field_name, value=value[:1024], inline=False)
        else:
            embed.add_field(
                name="Current Route",
                value="No active route is posted right now. Scouts can use the button below to submit the next chain.",
                inline=False,
            )

        history_lines: list[str] = []
        for row, portals, _note, _ttl_min, expiry in parsed:
            event_at = self._dt_from_iso(row.get("completed_at")) or self._dt_from_iso(row.get("submitted_at"))
            event_text = f"<t:{int(event_at.timestamp())}:R>" if event_at else "unknown time"
            expiry_text = ""
            if expiry:
                expiry_text = " · active" if expiry > now else " · closed"
            if row.get("status") == STATUS_SUBMITTED:
                expiry_text = " · pending approval" + expiry_text
            route = self._clip(self._sso_route_line(portals, (row.get("proof") or "")), 120)
            scout = f"<@{row.get('claimed_by')}>" if row.get("claimed_by") else "unknown scout"
            history_lines.append(
                f"`#{row.get('id')}` {route}\n{scout} · {event_text}{expiry_text}"
            )
            if len(history_lines) >= 5:
                break
        if history_lines:
            embed.add_field(
                name="Recent Route Reports",
                value="\n\n".join(history_lines)[:1024],
                inline=False,
            )

        embed.add_field(
            name="Submission Format",
            value=(
                "`Portal 1 > Portal 2 > Portal 3 > note: optional details > ttl: 1h30m`\n"
                "Use TTL when you know the connection timer. If no TTL is given, the board treats it as valid until UTC reset."
            ),
            inline=False,
        )
        embed.set_footer(text="SSO route board · route visibility is immediate; bounty payouts require approval")
        return embed

    async def _sso_route_channel(
        self,
        channel: Optional[discord.TextChannel] = None,
    ) -> Optional[discord.TextChannel]:
        if channel is not None:
            return channel
        channel_id = self.bot.db.get_config("sso_routes_channel_id")  # type: ignore[attr-defined]
        if not channel_id:
            return None
        try:
            found = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return found if isinstance(found, discord.TextChannel) else None

    async def _refresh_sso_route_board(
        self,
        *,
        channel: Optional[discord.TextChannel] = None,
        create: bool = True,
    ) -> Optional[discord.Message]:
        db = self.bot.db  # type: ignore[attr-defined]
        target = await self._sso_route_channel(channel)
        if not target:
            return None

        embed = self._sso_route_board_embed()
        view = SSORouteBoardView()
        msg_id = db.get_config("sso_routes_board_message_id")
        if msg_id:
            try:
                msg = await target.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=view)
                return msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                pass
        if not create:
            return None

        msg = await target.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        db.set_config("sso_routes_board_message_id", str(msg.id))
        db.set_config("sso_routes_channel_id", str(target.id))
        return msg

    def _create_claimed_sso_bounty(self, user_id: str) -> int:
        db = self.bot.db  # type: ignore[attr-defined]
        now = datetime.datetime.now(datetime.timezone.utc)
        tomorrow = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        try:
            reward = int(db.get_config("bounty_daily_sso_reward") or self.DAILY_SSO_DEFAULT_REWARD)
        except (TypeError, ValueError):
            reward = self.DAILY_SSO_DEFAULT_REWARD
        reward = max(0, min(reward, MAX_REWARD))
        title = (db.get_config("bounty_daily_sso_title") or self.DAILY_SSO_DEFAULT_TITLE).strip()
        if not title.startswith(self.SSO_TITLE_PREFIX):
            title = f"{self.SSO_TITLE_PREFIX} {title}"
        description = (db.get_config("bounty_daily_sso_description") or self.DAILY_SSO_DEFAULT_DESC).strip()
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """INSERT INTO bounties
               (title, description, reward_points, posted_by, deadline, status,
                claimed_by, claimed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                description,
                reward,
                str(self.bot.user.id) if self.bot.user else "system",
                tomorrow.strftime("%Y-%m-%d %H:%M:%S"),
                STATUS_CLAIMED,
                str(user_id),
                _now_iso(),
            ),
        )
        db.connection.commit()
        bounty_id = int(db.cursor.lastrowid or 0)
        if bounty_id:
            db.set_config("bounty_daily_sso_last_id", str(bounty_id))
            db.set_config("bounty_daily_sso_last_date", now.strftime("%Y-%m-%d"))
        return bounty_id

    async def _open_sso_route_modal(self, interaction: discord.Interaction) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Server only", "Use this from inside the guild server."),
                ephemeral=True,
            )
            return
        profile = db.fetch_user_profile(str(interaction.user.id))
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not registered",
                    "Register your Albion character first so route bounties can pay the right scout.",
                ),
                ephemeral=True,
            )
            return

        bounty_id = 0
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """
            SELECT * FROM bounties
             WHERE title LIKE ?
               AND status IN (?, ?, ?)
             ORDER BY datetime(COALESCE(claimed_at, posted_at)) DESC, id DESC
             LIMIT 1
            """,
            (
                f"{self.SSO_TITLE_PREFIX}%",
                STATUS_OPEN,
                STATUS_CLAIMED,
                STATUS_SUBMITTED,
            ),
        )
        active = dict(db.cursor.fetchone() or {})
        if active and active.get("status") == STATUS_OPEN:
            bounty_id = int(active["id"])
            _db_update(
                db,
                bounty_id,
                status=STATUS_CLAIMED,
                claimed_by=str(interaction.user.id),
                claimed_at=_now_iso(),
            )
            await self._refresh_board_embed(bounty_id)
        elif active and active.get("claimed_by") == str(interaction.user.id):
            bounty_id = int(active["id"])
        else:
            bounty_id = self._create_claimed_sso_bounty(str(interaction.user.id))
            if bounty_id:
                await self._refresh_board_embed(bounty_id)

        if not bounty_id:
            await interaction.response.send_message(
                embed=error_embed("Could not start route", "The bot could not create a route submission."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(SubmitSSORouteModal(bounty_id))

    async def _show_sso_route_format(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=info_embed(
                "SSO route format",
                "**Use the button when possible.** It opens fields for Portal 1, Portal 2, Portal 3, notes, and TTL.\n\n"
                "Manual format:\n"
                "`Portal 1 > Portal 2 > Portal 3 > note: optional details > ttl: 1h30m`\n\n"
                "Examples:\n"
                "`SSO > TA > Longmarch Meadow > Bridgewatch > ttl: 14h`\n"
                "`SSO > Ci > Pen Garn > note: safe so far > ttl: 2h`",
            ),
            ephemeral=True,
        )

    async def _close_current_sso_route(self, interaction: discord.Interaction) -> None:
        rows = self._fetch_sso_route_bounties(limit=1)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No route to close", "There is no approved SSO route on the board yet."),
                ephemeral=True,
            )
            return
        latest = rows[0]
        if (
            not _is_officer(interaction.user)
            and str(latest.get("claimed_by") or "") != str(interaction.user.id)
        ):
            await interaction.response.send_message(
                embed=error_embed(
                    "Permission denied",
                    "Only the scout who submitted the current route or an officer can mark it closed.",
                ),
                ephemeral=True,
            )
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        self.bot.db.set_config("bounty_daily_sso_expires_at", now.isoformat())  # type: ignore[attr-defined]
        self.bot.db.set_config("sso_routes_closed_bounty_id", str(latest.get("id")))  # type: ignore[attr-defined]
        await self._refresh_sso_route_board(create=True)
        await interaction.response.send_message(
            embed=success_embed("Route closed", "The SSO route board has been updated."),
            ephemeral=True,
        )

    # ── Internal: Roads core board ──────────────────────────────────────────
    def _fetch_roads_core_bounties(
        self,
        *,
        statuses: tuple[str, ...] = (STATUS_CLAIMED, STATUS_SUBMITTED, STATUS_COMPLETED),
        limit: int = 12,
    ) -> list[dict]:
        db = self.bot.db  # type: ignore[attr-defined]
        if not db.connection:
            db.connect()
        placeholders = ",".join("?" * len(statuses))
        db.cursor.execute(
            f"""
            SELECT * FROM bounties
             WHERE title LIKE ?
               AND status IN ({placeholders})
             ORDER BY datetime(COALESCE(completed_at, submitted_at, claimed_at, posted_at)) DESC,
                      id DESC
             LIMIT ?
            """,
            (f"{self.ROADS_CORE_TITLE_PREFIX}%", *statuses, int(limit)),
        )
        return [dict(r) for r in db.cursor.fetchall()]

    def _roads_core_deadline(self) -> str:
        db = self.bot.db  # type: ignore[attr-defined]
        configured = str(db.get_config("roads_core_bounty_deadline") or "").strip()
        if configured:
            return configured
        end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=14)
        return end.replace(microsecond=0).isoformat(sep=" ")

    async def _roads_core_channel(
        self,
        channel: Optional[discord.TextChannel] = None,
    ) -> Optional[discord.TextChannel]:
        if channel is not None:
            return channel
        db = self.bot.db  # type: ignore[attr-defined]
        channel_id = (
            db.get_config("roads_core_bounty_channel_id")
            or db.get_config(CFG_BOARD_CHANNEL)
        )
        if not channel_id:
            return None
        try:
            found = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return found if isinstance(found, discord.TextChannel) else None

    def _roads_core_board_embed(self) -> discord.Embed:
        now = datetime.datetime.now(datetime.timezone.utc)
        rows = self._fetch_roads_core_bounties(limit=12)
        deadline = self._dt_from_iso(self._roads_core_deadline())
        deadline_text = (
            f"<t:{int(deadline.timestamp())}:R> · <t:{int(deadline.timestamp())}:D>"
            if deadline else "current two-week push"
        )
        embed = discord.Embed(
            title="⚡ Roads Hideout Core Bounty — live",
            description=(
                "**For the next two weeks, all Roads power cores go to the Roads Hideout.**\n"
                "Hit the Roads, fight for cores, fight for chests, and bring the power home for the Union.\n\n"
                f"**Window:** {deadline_text}\n"
                "**Outworld cores:** use the normal payout policy."
            ),
            color=discord.Color.purple(),
            timestamp=now,
        )
        payout_lines = [
            f"{ROAD_CORE_EMOJIS[color]} **{color.title()} core** — 🪙 **{_fmt_silver(amount)}**"
            for color, amount in ROAD_CORE_REWARDS.items()
        ]
        embed.add_field(name="Roads Hideout Payouts", value="\n".join(payout_lines), inline=False)
        embed.add_field(
            name="How to Claim",
            value=(
                "Click **Submit Core**, choose the core color, then paste/upload the screenshot "
                "directly in this channel. Put party members in the same message if they are not visible in the screenshot. "
                "Officers approve the payout from the review channel."
            ),
            inline=False,
        )

        recent_lines: list[str] = []
        for row in rows:
            proof = parse_road_core_proof(row.get("proof"))
            color = str(proof.get("color") or "").lower()
            emoji = ROAD_CORE_EMOJIS.get(color, "⚡")
            status = str(row.get("status") or "unknown")
            event_at = (
                self._dt_from_iso(row.get("completed_at"))
                or self._dt_from_iso(row.get("submitted_at"))
                or self._dt_from_iso(row.get("claimed_at"))
                or self._dt_from_iso(row.get("posted_at"))
            )
            when = f"<t:{int(event_at.timestamp())}:R>" if event_at else "unknown time"
            recent_lines.append(
                f"`#{row.get('id')}` {emoji} **{color.title() or 'Core'}** "
                f"· {STATUS_EMOJI.get(status, '•')} {status} "
                f"· <@{row.get('claimed_by')}> · {when}"
            )
            if len(recent_lines) >= 6:
                break
        if recent_lines:
            embed.add_field(
                name="Recent Core Claims",
                value="\n".join(recent_lines)[:1024],
                inline=False,
            )
        else:
            embed.add_field(
                name="Recent Core Claims",
                value="No Roads core claims submitted yet. Be first on the board.",
                inline=False,
            )
        embed.set_footer(text="Roads core board · one clean board, officer-approved payouts")
        return embed

    async def _refresh_roads_core_board(
        self,
        *,
        channel: Optional[discord.TextChannel] = None,
        create: bool = True,
    ) -> Optional[discord.Message]:
        db = self.bot.db  # type: ignore[attr-defined]
        target = await self._roads_core_channel(channel)
        if not target:
            return None

        embed = self._roads_core_board_embed()
        view = RoadsCoreBoardView()
        msg_id = db.get_config("roads_core_bounty_board_message_id")
        if msg_id:
            try:
                msg = await target.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=view)
                return msg
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
                pass
        if not create:
            return None

        msg = await target.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        db.set_config("roads_core_bounty_board_message_id", str(msg.id))
        db.set_config("roads_core_bounty_channel_id", str(target.id))
        return msg

    async def _open_roads_core_modal(self, interaction: discord.Interaction) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Server only", "Use this from inside the guild server."),
                ephemeral=True,
            )
            return
        profile = db.fetch_user_profile(str(interaction.user.id))
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not registered",
                    "Register your Albion character first so core bounties can pay the right player.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=info_embed(
                "Pick the core color",
                "Choose the Roads core color first. After that, paste or upload the screenshot directly in this channel.",
            ),
            view=RoadsCoreColorView(),
            ephemeral=True,
        )

    async def _start_roads_core_image_capture(
        self,
        interaction: discord.Interaction,
        *,
        color: str,
    ) -> None:
        if interaction.channel is None:
            await interaction.response.send_message(
                embed=error_embed("No channel", "Use this from the bounty board channel."),
                ephemeral=True,
            )
            return

        reward = int(ROAD_CORE_REWARDS[color])
        await interaction.response.send_message(
            embed=info_embed(
                f"{ROAD_CORE_EMOJIS[color]} {color.title()} core selected",
                "Paste or upload the screenshot **in this channel** within 5 minutes.\n\n"
                "Optional: type party members in the same message. The bot will capture the image and clean up the proof message if it can.",
            ),
            ephemeral=True,
        )

        channel_id = int(interaction.channel.id)
        user_id = int(interaction.user.id)

        def _check(message: discord.Message) -> bool:
            return (
                int(message.author.id) == user_id
                and int(message.channel.id) == channel_id
                and image_attachment_url(message) is not None
            )

        try:
            proof_message = await self.bot.wait_for("message", check=_check, timeout=300)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                embed=warning_embed(
                    "Core submission timed out",
                    "No image was received within 5 minutes. Click **Submit Core** again when you have the screenshot ready.",
                ),
                ephemeral=True,
            )
            return

        screenshot = image_attachment_url(proof_message)
        if not screenshot:
            await interaction.followup.send(
                embed=error_embed("Image missing", "I could not read the screenshot attachment. Try again."),
                ephemeral=True,
            )
            return
        party = (proof_message.content or "").strip()
        if not party:
            party = f"Submitted by <@{interaction.user.id}>; party visible in screenshot."

        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await proof_message.delete()

        await self._submit_roads_core_bounty(
            interaction,
            color=color,
            screenshot=screenshot,
            party=party,
            note=f"Captured from pasted image. Reward: {_fmt_silver(reward)} silver.",
        )

    async def _submit_roads_core_bounty(
        self,
        interaction: discord.Interaction,
        *,
        color: str,
        screenshot: str,
        party: str,
        note: str = "",
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db  # type: ignore[attr-defined]
        reward = int(ROAD_CORE_REWARDS[color])
        proof = road_core_proof_text(
            color=color,
            screenshot=screenshot,
            party=party,
            note=note,
        )
        title = road_core_title(color)
        description = (
            "Roads Hideout power-core bounty. Officers verify screenshot proof "
            "and party list before approving payout."
        )
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """INSERT INTO bounties
               (title, description, reward_points, posted_by, deadline, status,
                claimed_by, claimed_at, submitted_at, proof)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                title,
                description,
                reward,
                str(self.bot.user.id) if self.bot.user else "system",
                self._roads_core_deadline(),
                STATUS_SUBMITTED,
                str(interaction.user.id),
                _now_iso(),
                _now_iso(),
                proof,
            ),
        )
        db.connection.commit()
        bounty_id = int(db.cursor.lastrowid or 0)
        if not bounty_id:
            await interaction.followup.send(
                embed=error_embed("Could not submit core", "The bot could not create the bounty row."),
                ephemeral=True,
            )
            return

        await self._refresh_roads_core_board(create=True)
        try:
            await self._notify_officers_submission(bounty_id, interaction.user)
        except Exception as exc:  # noqa: BLE001
            error_log(f"roads core officer notice failed for #{bounty_id}: {exc!r}")
        await interaction.followup.send(
            embed=success_embed(
                f"Roads core submitted — #{bounty_id}",
                f"{ROAD_CORE_EMOJIS[color]} **{color.title()} core** for "
                f"🪙 **{_fmt_silver(reward)}** is awaiting officer approval.",
            ),
            ephemeral=True,
        )

    # ── Shared action helpers (slash + button entrypoints) ──────────────────
    async def _do_claim(self, interaction: discord.Interaction, bounty_id: int) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True)
            return
        if b["status"] != STATUS_OPEN:
            await interaction.response.send_message(
                embed=warning_embed("Cannot claim",
                    f"This bounty is already **{b['status']}**."),
                ephemeral=True)
            return
        profile = db.fetch_user_profile(str(interaction.user.id))
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed("Not registered",
                    "Link your Albion character first — click the **Register** button in your registration channel."),
                ephemeral=True)
            return
        if not _db_claim_open(
            db,
            bounty_id,
            str(interaction.user.id),
            claimed_at=_now_iso(),
        ):
            current = _db_get(db, bounty_id) or b
            claimed_by = current.get("claimed_by")
            owner = f" by <@{claimed_by}>" if claimed_by else ""
            await self._refresh_board_embed(bounty_id)
            await interaction.response.send_message(
                embed=warning_embed(
                    "Already claimed",
                    f"Someone else claimed bounty #{bounty_id}{owner} before this request finished.",
                ),
                ephemeral=True,
            )
            return
        await self._refresh_board_embed(bounty_id)
        info_log(f"{interaction.user} claimed bounty #{bounty_id}.")
        await interaction.response.send_message(
            embed=success_embed(
                f"Claimed bounty #{bounty_id}",
                f"You're on the hook for **{b['title']}** "
                f"(reward 🪙 **{_fmt_silver(b['reward_points'])}** silver). "
                f"Use **Submit Proof** when done."),
            ephemeral=True)

    async def _do_unclaim(self, interaction: discord.Interaction, bounty_id: int) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True)
            return
        if b.get("claimed_by") != str(interaction.user.id):
            await interaction.response.send_message(
                embed=error_embed("Not yours", "You haven't claimed this bounty."),
                ephemeral=True)
            return
        if b["status"] not in (STATUS_CLAIMED, STATUS_SUBMITTED):
            await interaction.response.send_message(
                embed=warning_embed("Cannot unclaim", f"Status is {b['status']}."),
                ephemeral=True)
            return
        _db_update(db, bounty_id,
                   status=STATUS_OPEN,
                   claimed_by=None, claimed_at=None,
                   submitted_at=None, proof=None)
        await self._refresh_board_embed(bounty_id)
        info_log(f"{interaction.user} unclaimed bounty #{bounty_id}.")
        await interaction.response.send_message(
            embed=success_embed("Released", f"Bounty #{bounty_id} is back on the board."),
            ephemeral=True)

    async def _do_submit(self, interaction: discord.Interaction,
                         bounty_id: int, proof: str) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db  # type: ignore[attr-defined]
        try:
            b = _db_get(db, bounty_id)
            if not b:
                await interaction.followup.send(
                    embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                    ephemeral=True,
                )
                return
            if b.get("claimed_by") != str(interaction.user.id):
                await interaction.followup.send(
                    embed=error_embed("Not yours", "You haven't claimed this bounty."),
                    ephemeral=True,
                )
                return
            if b["status"] not in (STATUS_CLAIMED, STATUS_SUBMITTED):
                await interaction.followup.send(
                    embed=warning_embed("Cannot submit", f"Status is {b['status']}."),
                    ephemeral=True,
                )
                return
            _db_update(db, bounty_id,
                       status=STATUS_SUBMITTED,
                       submitted_at=_now_iso(),
                       proof=proof.strip()[:1500])
            await self._refresh_board_embed(bounty_id)
            if (b.get("title") or "").startswith(self.SSO_TITLE_PREFIX):
                # Route visibility should not wait on payout approval. Scouts need
                # the chain live immediately, while officers can still approve or
                # reject the bounty reward afterward.
                db.set_config("sso_routes_closed_bounty_id", "")
                try:
                    await self._refresh_sso_route_board(create=True)
                except Exception as exc:  # noqa: BLE001
                    error_log(f"sso route board submit refresh failed for #{bounty_id}: {exc!r}")
            # Ping the officer review channel so they don't miss it.
            try:
                await self._notify_officers_submission(bounty_id, interaction.user)
            except Exception as exc:  # noqa: BLE001
                error_log(f"_notify_officers_submission #{bounty_id} failed: {exc!r}")
            info_log(f"{interaction.user} submitted bounty #{bounty_id}.")
            await interaction.followup.send(
                embed=success_embed("Submitted",
                    f"Bounty #{bounty_id} is now awaiting officer approval."),
                ephemeral=True)
        except Exception as exc:  # noqa: BLE001
            error_log(f"bounty submit #{bounty_id} failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Submit failed",
                    "The bot hit an error while saving or refreshing this submission. "
                    "Try again or ask an officer to check the bounty board.",
                ),
                ephemeral=True,
            )

    async def _notify_officers_submission(
        self, bounty_id: int, claimer: discord.abc.User,
    ) -> None:
        """Post a fresh 'proof submitted' embed with Approve/Reject buttons
        into the review channel so officers see it in 📋-officer-tasks."""
        db = self.bot.db  # type: ignore[attr-defined]
        review_id = db.get_config(CFG_REVIEW_CHANNEL)
        if not review_id:
            return
        try:
            channel = self.bot.get_channel(int(review_id)) or await self.bot.fetch_channel(int(review_id))
        except (ValueError, discord.NotFound, discord.Forbidden):
            return
        if not isinstance(channel, discord.TextChannel):
            return
        b = _db_get(db, bounty_id)
        if not b:
            return
        proof_preview = (b.get("proof") or "").strip()
        if len(proof_preview) > 1000:
            proof_preview = proof_preview[:997] + "..."
        embed = discord.Embed(
            title=f"📥 Proof submitted — #{bounty_id}",
            description=(
                f"**{b['title']}**\n"
                f"Claimer: {claimer.mention}\n"
                f"Reward: 🪙 **{_fmt_silver(b['reward_points'])}** silver\n\n"
                f"**Proof:**\n{proof_preview or '_(no proof text)_'}"
            ),
            color=discord.Color.gold(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(BountyApproveButton(bounty_id, label="Approve & Pay"))
        view.add_item(BountyRejectButton(bounty_id, label="Send Back"))
        try:
            await channel.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"Could not post submission notice to review channel: {exc!r}")

    async def _do_approve(self, interaction: discord.Interaction, bounty_id: int) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True)
            return

        if b["status"] == STATUS_PENDING:
            _db_update(db, bounty_id, status=STATUS_OPEN)
            proposer = b.get("posted_by")
            if proposer and SILENT_POINTS_PROPOSER_APPROVED > 0:
                db.add_points(proposer, SILENT_POINTS_PROPOSER_APPROVED)
                info_log(
                    f"Silent +{SILENT_POINTS_PROPOSER_APPROVED} pts to {proposer} "
                    f"for approved bounty #{bounty_id} proposal.")
                from cogs.points import announce_points
                await announce_points(
                    self.bot, proposer, SILENT_POINTS_PROPOSER_APPROVED,
                    f"Bounty #{bounty_id} proposal approved",
                )
            await self._refresh_board_embed(bounty_id)
            info_log(f"{interaction.user} approved posting of bounty #{bounty_id}.")
            if proposer:
                try:
                    user = await self.bot.fetch_user(int(proposer))
                    await user.send(embed=success_embed(
                        f"Bounty #{bounty_id} approved",
                        f"Your bounty **{b['title']}** is now live on the board."))
                except (discord.Forbidden, discord.HTTPException, ValueError):
                    pass
            await interaction.response.send_message(
                embed=success_embed(f"Bounty #{bounty_id} published",
                    "It's now live on the public board."),
                ephemeral=True)
            return

        if b["status"] == STATUS_SUBMITTED:
            # Tiered bounties (e.g. daily Energy Core) need the officer to
            # confirm which tier was delivered before paying out. Look for a
            # saved tier scale; if present, hand off to the picker view and
            # let it call ``_finalize_bounty_payout`` with the chosen amount.
            tiers = _load_bounty_tier_scale(db, bounty_id)
            if tiers:
                view = discord.ui.View(timeout=300)
                view.add_item(_BountyTierPickSelect(self, bounty_id, tiers))
                await interaction.response.send_message(
                    embed=info_embed(
                        "Pick the delivered tier",
                        "This is a **tiered bounty**. Choose which core tier "
                        f"<@{b.get('claimed_by')}> actually delivered — that "
                        "tier's silver value is what gets credited."
                        + "\n\n"
                        + "\n".join(
                            f"{t.get('emoji', '•')} **{t['name']}** — "
                            f"🪙 {_fmt_silver(int(t['silver']))}"
                            for t in tiers
                        ),
                    ),
                    view=view, ephemeral=True,
                )
                return
            await self._finalize_bounty_payout(
                interaction, bounty_id, int(b.get("reward_points") or 0),
                tier_label=None,
            )
            return

        await interaction.response.send_message(
            embed=warning_embed("Nothing to approve",
                f"Bounty is **{b['status']}**."),
            ephemeral=True)

    async def _do_confirm_paid(self, interaction: discord.Interaction, bounty_id: int) -> None:
        """Officer confirms the in-game silver was paid, settling the ledger."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b:
            await interaction.followup.send(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True,
            )
            return
        if b["status"] != STATUS_COMPLETED:
            await interaction.followup.send(
                embed=warning_embed("Not ready", f"Bounty is **{b['status']}**, not completed."),
                ephemeral=True,
            )
            return
        if b.get("paid_at"):
            await interaction.followup.send(
                embed=info_embed(
                    "Already marked paid",
                    f"Bounty #{bounty_id} was already confirmed paid.",
                ),
                ephemeral=True,
            )
            return

        claimer = b.get("claimed_by")
        reward = int(b.get("reward_points") or 0)
        if not claimer or reward <= 0:
            _db_update(
                db,
                bounty_id,
                paid_by=str(interaction.user.id),
                paid_at=_now_iso(),
            )
            await self._refresh_board_embed(bounty_id)
            await interaction.followup.send(
                embed=success_embed(
                    "Marked paid",
                    "No positive payout was attached, so no silver ledger entry was needed.",
                ),
                ephemeral=True,
            )
            return

        balance = int(db.fetch_silver_balance(str(claimer)) or 0)
        settle_amount = min(reward, max(balance, 0))
        new_balance = balance
        if settle_amount > 0:
            maybe_balance = db.adjust_silver_balance(
                str(claimer),
                -settle_amount,
                reason=f"Bounty #{bounty_id} paid in-game — {b['title']}",
                ref_type="bounty_paid",
                ref_id=str(bounty_id),
                actor_id=str(interaction.user.id),
            )
            if maybe_balance is None:
                await interaction.followup.send(
                    embed=error_embed(
                        "Settlement failed",
                        f"Could not write the silver ledger row for <@{claimer}>. "
                        "Make sure they are still registered.",
                    ),
                    ephemeral=True,
                )
                return
            new_balance = int(maybe_balance)

        _db_update(
            db,
            bounty_id,
            paid_by=str(interaction.user.id),
            paid_at=_now_iso(),
        )
        await self._refresh_board_embed(bounty_id)

        if settle_amount < reward:
            note = (
                f"Only **{_fmt_silver(settle_amount)}** was settled because "
                f"<@{claimer}>'s current positive balance was lower than the "
                f"bounty reward. New balance: **{new_balance:+,}** silver."
            )
        else:
            note = (
                f"Settled **{_fmt_silver(settle_amount)}** paid to <@{claimer}>. "
                f"New balance: **{new_balance:+,}** silver."
            )

        with contextlib.suppress(discord.Forbidden, discord.HTTPException, ValueError):
            user = await self.bot.fetch_user(int(claimer))
            await user.send(embed=success_embed(
                f"Bounty #{bounty_id} paid",
                f"An officer marked your 🪙 **{_fmt_silver(settle_amount)}** bounty payout as paid in-game.",
            ))

        await interaction.followup.send(
            embed=success_embed(f"Bounty #{bounty_id} settled", note),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} confirmed bounty #{bounty_id} paid to {claimer}; "
            f"settled={settle_amount:,}, new_balance={new_balance:+,}."
        )

    async def _finalize_bounty_payout(
        self, interaction: discord.Interaction, bounty_id: int,
        silver: int, *, tier_label: str | None = None,
    ) -> None:
        """Complete a SUBMITTED bounty: credit silver, mark completed,
        refresh the board, fire flex shoutout + DM. ``silver`` is the actual
        amount to pay (tier-adjusted for tiered bounties). ``tier_label``
        is appended to log/ledger reasons when present.
        """
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b or b["status"] != STATUS_SUBMITTED:
            await interaction.response.send_message(
                embed=error_embed("Already resolved", "This bounty isn't in submitted state anymore."),
                ephemeral=True,
            )
            return
        claimer = b.get("claimed_by")
        silver = max(0, min(int(silver or 0), MAX_REWARD))
        tier_suffix = f" [{tier_label}]" if tier_label else ""
        if claimer and SILENT_POINTS_CLAIMER_PAID > 0:
            db.add_points(claimer, SILENT_POINTS_CLAIMER_PAID)
            info_log(
                f"Silent +{SILENT_POINTS_CLAIMER_PAID} pts to {claimer} "
                f"for completing bounty #{bounty_id}.")
            from cogs.points import announce_points
            await announce_points(
                self.bot, claimer, SILENT_POINTS_CLAIMER_PAID,
                f"Completed bounty #{bounty_id} — {b['title']}",
            )
        # Snapshot the actual paid amount onto the row for accurate audits,
        # then mark complete.
        _db_update(
            db, bounty_id,
            reward_points=silver,
            status=STATUS_COMPLETED,
            completed_by=str(interaction.user.id),
            completed_at=_now_iso(),
        )
        if claimer and silver > 0:
            try:
                db.adjust_silver_balance(
                    claimer, silver,
                    reason=f"Bounty #{bounty_id} payout{tier_suffix} — {b['title']}",
                    ref_type="bounty", ref_id=str(bounty_id),
                    actor_id=str(interaction.user.id),
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"silver credit failed for bounty #{bounty_id}: {exc!r}")
        await self._refresh_board_embed(bounty_id)
        try:
            await self._maybe_post_sso_route(b, claimer)
        except Exception as exc:  # noqa: BLE001
            error_log(f"sso route repost failed for #{bounty_id}: {exc!r}")
        info_log(f"{interaction.user} approved bounty #{bounty_id}{tier_suffix}; "
                 f"silver payout {silver} owed to {claimer}.")
        if claimer:
            try:
                await self._post_flex_shoutout(b, claimer, silver)
            except Exception as exc:  # noqa: BLE001
                error_log(f"bounty flex shoutout failed for #{bounty_id}: {exc!r}")
        if claimer:
            try:
                user = await self.bot.fetch_user(int(claimer))
                await user.send(embed=success_embed(
                    f"Bounty #{bounty_id} approved",
                    f"You earned 🪙 **{_fmt_silver(silver)}** silver"
                    + (f" ({tier_label} tier)" if tier_label else "")
                    + ". An officer will pay you in-game."))
            except (discord.Forbidden, discord.HTTPException, ValueError):
                pass
        msg_body = (
            f"Pay <@{claimer}> 🪙 **{_fmt_silver(silver)}** silver in-game"
            + (f" — _{tier_label} tier_." if tier_label else ".")
        )
        paid_view = None
        if claimer and silver > 0:
            paid_view = discord.ui.View(timeout=900)
            paid_view.add_item(BountyConfirmPaidButton(bounty_id))
        # The tier-picker callback already responded with the picker; in
        # that flow we edit the picker message. The direct (no-tier) path
        # is the first response.
        if interaction.response.is_done():
            try:
                await interaction.edit_original_response(
                    embed=success_embed(f"Bounty #{bounty_id} approved", msg_body),
                    view=paid_view,
                )
            except discord.HTTPException:
                await interaction.followup.send(
                    embed=success_embed(f"Bounty #{bounty_id} approved", msg_body),
                    view=paid_view,
                    ephemeral=True,
                )
        else:
            await interaction.response.send_message(
                embed=success_embed(f"Bounty #{bounty_id} approved", msg_body),
                view=paid_view,
                ephemeral=True,
            )

    async def _do_reject(self, interaction: discord.Interaction,
                         bounty_id: int, reason: str) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True)
            return

        if b["status"] == STATUS_PENDING:
            _db_update(db, bounty_id, status=STATUS_CANCELLED)
            await self._refresh_board_embed(bounty_id)
            info_log(f"{interaction.user} denied posting of bounty #{bounty_id}: {reason}.")
            proposer = b.get("posted_by")
            if proposer:
                try:
                    user = await self.bot.fetch_user(int(proposer))
                    await user.send(embed=warning_embed(
                        f"Bounty #{bounty_id} denied",
                        f"**{b['title']}** wasn't approved for posting.\n\n"
                        f"**Reason:** {reason}"))
                except (discord.Forbidden, discord.HTTPException, ValueError):
                    pass
            await interaction.response.send_message(
                embed=success_embed(f"Bounty #{bounty_id} denied", "Proposer notified."),
                ephemeral=True)
            return

        if b["status"] == STATUS_SUBMITTED:
            _db_update(db, bounty_id,
                       status=STATUS_CLAIMED,
                       submitted_at=None, proof=None)
            await self._refresh_board_embed(bounty_id)
            if (b.get("title") or "").startswith(self.SSO_TITLE_PREFIX):
                try:
                    await self._refresh_sso_route_board(create=True)
                except Exception as exc:  # noqa: BLE001
                    error_log(f"sso route board reject refresh failed for #{bounty_id}: {exc!r}")
            info_log(f"{interaction.user} rejected submission for bounty #{bounty_id}: {reason}.")
            claimer = b.get("claimed_by")
            if claimer:
                try:
                    user = await self.bot.fetch_user(int(claimer))
                    await user.send(embed=warning_embed(
                        f"Bounty #{bounty_id} rejected",
                        f"**Reason:** {reason}\n\nYou can re-submit with corrected proof, "
                        f"or unclaim it."))
                except (discord.Forbidden, discord.HTTPException, ValueError):
                    pass
            await interaction.response.send_message(
                embed=success_embed(f"Bounty #{bounty_id} sent back",
                    "Claimer notified. They can re-submit."),
                ephemeral=True)
            return

        await interaction.response.send_message(
            embed=warning_embed("Nothing to reject",
                f"Bounty is **{b['status']}**."),
            ephemeral=True)

    # ── Slash commands (thin wrappers) ──────────────────────────────────────
    @bounty_group.command(name="post", description="Propose a new bounty (officer must approve).")
    async def bounty_post(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_PostBountyModal(self))

    @bounty_group.command(name="board", description="List active bounties.")
    async def bounty_board(self, interaction: discord.Interaction) -> None:
        bounties = _db_list(self.bot.db)  # type: ignore[attr-defined]
        if not bounties:
            await interaction.response.send_message(
                embed=info_embed("No active bounties", "Check back later!"),
                ephemeral=True)
            return
        embed = discord.Embed(
            title="🎯 Bounty Board", color=discord.Color.gold(),
            description=f"{len(bounties)} active bounty/bounties.")
        for b in bounties[:15]:
            status = b["status"]
            emoji = STATUS_EMOJI.get(status, "•")
            line = (f"🪙 **{_fmt_silver(b['reward_points'])} silver** • {status}"
                    f" • deadline {_fmt_deadline(b.get('deadline'))}")
            if b.get("claimed_by"):
                line += f" • <@{b['claimed_by']}>"
            embed.add_field(name=f"{emoji} #{b['id']} — {b['title']}",
                            value=line, inline=False)
        embed.set_footer(text="Use /bounty view <id> for full details.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bounty_group.command(name="sso-routes", description="Post or refresh the single SSO route board.")
    @app_commands.describe(channel="Optional channel to place the SSO route board in. Defaults to this channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_sso_routes(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target = channel
        if target is None and isinstance(interaction.channel, discord.TextChannel):
            target = interaction.channel
        if target is None:
            await interaction.response.send_message(
                embed=error_embed("No channel", "Run this from a text channel or provide one."),
                ephemeral=True,
            )
            return
        me = target.guild.me
        perms = target.permissions_for(me) if me else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post there",
                    f"I need **Send Messages** + **Embed Links** in {target.mention}.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.bot.db.set_config("sso_routes_channel_id", str(target.id))  # type: ignore[attr-defined]
        msg = await self._refresh_sso_route_board(channel=target, create=True)
        if not msg:
            await interaction.followup.send(
                embed=error_embed("Board failed", "The route board could not be posted or refreshed."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "SSO route board ready",
                f"The board is live in {target.mention}. Scouts can use **Add / Update Route** from that message.",
            ),
            ephemeral=True,
        )

    @bounty_group.command(name="roads-cores", description="Post or refresh the single Roads core bounty board.")
    @app_commands.describe(channel="Optional channel to place the Roads core board in. Defaults to this channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_roads_cores(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        target = channel
        if target is None and isinstance(interaction.channel, discord.TextChannel):
            target = interaction.channel
        if target is None:
            await interaction.response.send_message(
                embed=error_embed("No channel", "Run this from a text channel or provide one."),
                ephemeral=True,
            )
            return
        me = target.guild.me
        perms = target.permissions_for(me) if me else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post there",
                    f"I need **Send Messages** + **Embed Links** in {target.mention}.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.bot.db.set_config("roads_core_bounty_channel_id", str(target.id))  # type: ignore[attr-defined]
        msg = await self._refresh_roads_core_board(channel=target, create=True)
        if not msg:
            await interaction.followup.send(
                embed=error_embed("Board failed", "The Roads core board could not be posted or refreshed."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "Roads core board ready",
                f"The board is live in {target.mention}. Members can use **Submit Core** from that message.",
            ),
            ephemeral=True,
        )

    @bounty_group.command(name="view", description="View full details of a bounty.")
    @app_commands.describe(bounty_id="Bounty ID number.")
    async def bounty_view(self, interaction: discord.Interaction, bounty_id: int) -> None:
        b = _db_get(self.bot.db, bounty_id)  # type: ignore[attr-defined]
        if not b:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True)
            return
        await interaction.response.send_message(embed=_bounty_to_embed(b), ephemeral=True)

    @bounty_group.command(name="claim", description="Claim an open bounty.")
    @app_commands.describe(bounty_id="Bounty ID to claim.")
    async def bounty_claim(self, interaction: discord.Interaction, bounty_id: int) -> None:
        await self._do_claim(interaction, bounty_id)

    @bounty_group.command(name="unclaim", description="Drop your claim on a bounty.")
    @app_commands.describe(bounty_id="Bounty ID you no longer want.")
    async def bounty_unclaim(self, interaction: discord.Interaction, bounty_id: int) -> None:
        await self._do_unclaim(interaction, bounty_id)

    @bounty_group.command(name="submit", description="Submit proof for a claimed bounty.")
    @app_commands.describe(bounty_id="Bounty ID you completed.",
                           proof="Link to screenshot, in-game receipt, or a short description.")
    async def bounty_submit(self, interaction: discord.Interaction,
                            bounty_id: int, proof: str) -> None:
        await self._do_submit(interaction, bounty_id, proof)

    @bounty_group.command(name="approve",
        description="Officer: publish a pending bounty OR pay out a submitted one.")
    @app_commands.describe(bounty_id="Bounty ID to approve.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_approve(self, interaction: discord.Interaction, bounty_id: int) -> None:
        await self._do_approve(interaction, bounty_id)

    @bounty_group.command(name="reject",
        description="Officer: deny a pending bounty OR send a submission back.")
    @app_commands.describe(bounty_id="Bounty ID to reject.",
                           reason="Why are you rejecting? The proposer/claimer sees this.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_reject(self, interaction: discord.Interaction,
                            bounty_id: int, reason: str) -> None:
        await self._do_reject(interaction, bounty_id, reason)

    @bounty_group.command(name="cancel", description="Cancel a bounty without payout (officer).")
    @app_commands.describe(bounty_id="Bounty ID to cancel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_cancel(self, interaction: discord.Interaction, bounty_id: int) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        b = _db_get(db, bounty_id)
        if not b:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No bounty with ID #{bounty_id}."),
                ephemeral=True)
            return
        if b["status"] in (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_EXPIRED):
            await interaction.response.send_message(
                embed=warning_embed("Already terminal", f"Status is {b['status']}."),
                ephemeral=True)
            return
        _db_update(db, bounty_id, status=STATUS_CANCELLED)
        await self._refresh_board_embed(bounty_id)
        if (b.get("title") or "").startswith(self.SSO_TITLE_PREFIX):
            try:
                await self._refresh_sso_route_board(create=True)
            except Exception as exc:  # noqa: BLE001
                error_log(f"sso route board cancel refresh failed for #{bounty_id}: {exc!r}")
        info_log(f"{interaction.user} cancelled bounty #{bounty_id}.")
        await interaction.response.send_message(
            embed=success_embed(f"Bounty #{bounty_id} cancelled", "No payout."),
            ephemeral=True)

    @bounty_group.command(name="queue",
        description="Officer: list bounties awaiting posting approval.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_queue(self, interaction: discord.Interaction) -> None:
        rows = _db_list(self.bot.db, statuses=(STATUS_PENDING,), limit=50)  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Queue empty", "Nothing waiting for approval."),
                ephemeral=True)
            return
        embed = discord.Embed(title="🟣 Pending Bounties",
                              color=discord.Color.purple(),
                              description=f"{len(rows)} awaiting review.")
        for b in rows[:15]:
            embed.add_field(
                name=f"#{b['id']} — {b['title']}",
                value=(f"🪙 **{_fmt_silver(b['reward_points'])} silver** • by <@{b['posted_by']}>"
                       f" • deadline {_fmt_deadline(b.get('deadline'))}"),
                inline=False)
        embed.set_footer(text="Use the buttons on each pending bounty post to approve or deny.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bounty_group.command(name="payouts",
        description="Officer: audit completed bounties still waiting on in-game silver.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def bounty_payouts(self, interaction: discord.Interaction) -> None:
        rows = self._fetch_unpaid_bounty_payouts(limit=self.PAYMENT_AUDIT_LIMIT)
        if not rows:
            await interaction.response.send_message(
                embed=success_embed(
                    "Payout ledger clear",
                    "No completed bounties are waiting for in-game silver confirmation.",
                ),
                ephemeral=True,
            )
            return
        shown = rows[: self.PAYMENT_REMINDER_BUTTON_LIMIT]
        embed = self._build_payout_audit_embed(
            shown,
            title="Bounty payout audit",
            hidden_recent=max(0, len(rows) - len(shown)),
        )
        view = discord.ui.View(timeout=900)
        for row in shown:
            view.add_item(BountyConfirmPaidButton(int(row["id"])))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @bounty_group.command(name="mine", description="Bounties you have claimed.")
    async def bounty_mine(self, interaction: discord.Interaction) -> None:
        rows = _db_list_for_user(self.bot.db, str(interaction.user.id))  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No active claims", "Browse `/bounty board` to claim one."),
                ephemeral=True)
            return
        embed = discord.Embed(title="🎯 Your Bounties", color=discord.Color.blue(),
                              description=f"{len(rows)} active claim(s).")
        for b in rows:
            embed.add_field(
                name=f"{STATUS_EMOJI.get(b['status'], '•')} #{b['id']} — {b['title']}",
                value=(f"🪙 **{_fmt_silver(b['reward_points'])} silver** • {b['status']}"
                       f" • deadline {_fmt_deadline(b.get('deadline'))}"),
                inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Flex / shoutout ─────────────────────────────────────────────────────
    async def _post_flex_shoutout(self, bounty: dict, claimer_id: str, silver: int) -> None:
        """Celebrate the earner in the flex channel + ping milestone tiers."""
        db = self.bot.db  # type: ignore[attr-defined]
        chan_id = db.get_config(CFG_FLEX_CHANNEL)
        if not chan_id:
            return  # Flex channel not configured — silently skip.
        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, ValueError, discord.HTTPException):
            error_log("bounty flex: configured channel unreachable.")
            return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            error_log("bounty flex: channel is not text-capable.")
            return

        lifetime = _player_total_earned(db, claimer_id)
        bcount = _player_bounty_count(db, claimer_id)
        rank, total_players = _player_rank(db, claimer_id)
        rank_part = f"#{rank}/{total_players}" if rank else "—"

        embed = discord.Embed(
            title="🪙 Bounty Cashout!",
            description=(
                f"<@{claimer_id}> just banked 🪙 **{_fmt_silver(silver)}** silver "
                f"for **{bounty['title']}**!"
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="This payout",
            value=f"🪙 **{_fmt_silver(silver)}** silver",
            inline=True,
        )
        embed.add_field(
            name="Lifetime",
            value=f"🪙 **{_fmt_silver(lifetime)}** silver across **{bcount}** bounty(ies)",
            inline=True,
        )
        embed.add_field(name="All-time rank", value=f"**{rank_part}**", inline=True)
        embed.set_footer(text=f"Bounty #{bounty['id']} · keep stacking that silver")

        try:
            msg = await channel.send(
                content=f"💰 Shoutout to <@{claimer_id}>!",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"bounty flex: send failed: {exc!r}")
            return

        # Milestone bonus shoutout (only once per tier per player).
        try:
            tier = _new_milestone(db, claimer_id, lifetime)
        except Exception as exc:  # noqa: BLE001
            error_log(f"bounty milestone check failed: {exc!r}")
            tier = None
        if tier:
            ms_embed = discord.Embed(
                title="🏆 BOUNTY MILESTONE!",
                description=(
                    f"<@{claimer_id}> just crossed **{_fmt_silver(tier)} silver** "
                    f"lifetime in bounty earnings! 🎉"
                ),
                color=discord.Color.purple(),
            )
            ms_embed.set_footer(text="Hall-of-Fame tier unlocked")
            try:
                await channel.send(
                    content=f"🎊 Big plays from <@{claimer_id}>!",
                    embed=ms_embed,
                    reference=msg,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"bounty milestone post failed: {exc!r}")

    @bounty_group.command(name="top",
        description="Top bounty earners. Show off the silver-makers.")
    @app_commands.describe(
        period="Time window for the leaderboard.",
        limit="How many players to show (1-25). Default 10.",
    )
    @app_commands.choices(period=[
        app_commands.Choice(name="All-time", value="all"),
        app_commands.Choice(name="This month (30d)", value="month"),
        app_commands.Choice(name="This week (7d)", value="week"),
    ])
    async def bounty_top(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, 25] | None = None,
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        period_key = period.value if period else "all"
        period_label = period.name if period else "All-time"
        n = int(limit) if limit is not None else 10

        since_iso: str | None = None
        if period_key == "week":
            since_iso = (utc_now_naive() - datetime.timedelta(days=7)).isoformat(sep=" ")
        elif period_key == "month":
            since_iso = (utc_now_naive() - datetime.timedelta(days=30)).isoformat(sep=" ")

        rows = _top_earners(db, since_iso, limit=n)
        embed = discord.Embed(
            title=f"🏆 Top Bounty Earners — {period_label}",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.description = "_No completed bounties in this window yet._"
            await interaction.response.send_message(embed=embed)
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows, 1):
            medal = medals[i - 1] if i <= 3 else f"`#{i}`"
            uid = r.get("user_id")
            total = int(r.get("total_silver") or 0)
            count = int(r.get("bounty_count") or 0)
            lines.append(
                f"{medal} <@{uid}> — 🪙 **{_fmt_silver(total)}** silver "
                f"_({count} bounty{'ies' if count != 1 else ''})_"
            )
        embed.description = "\n".join(lines)

        # Personal stat-line for the invoker.
        me_total = _player_total_earned(db, str(interaction.user.id))
        if me_total > 0:
            rank, total_players = _player_rank(db, str(interaction.user.id))
            embed.add_field(
                name="Your standing",
                value=(
                    f"🪙 **{_fmt_silver(me_total)}** silver lifetime · "
                    f"all-time rank **#{rank}/{total_players}**"
                ),
                inline=False,
            )
        embed.set_footer(text="Earn more with /bounty board · flex with /bounty top")
        await interaction.response.send_message(embed=embed)

    # ── Config ──────────────────────────────────────────────────────────────
    @config_group.command(name="set-channel",
        description="Configure the public bounty board channel.")
    @app_commands.describe(channel="Channel where new bounties get posted.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(self, interaction: discord.Interaction,
                          channel: discord.TextChannel) -> None:
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=error_embed("Cannot post there",
                    f"I need **Send Messages** + **Embed Links** in {channel.mention}."),
                ephemeral=True)
            return
        self.bot.db.set_config(CFG_BOARD_CHANNEL, str(channel.id))  # type: ignore[attr-defined]
        info_log(f"{interaction.user} set bounty board channel to #{channel.name}.")
        await interaction.response.send_message(
            embed=success_embed("Bounty board configured",
                f"New bounties will post to {channel.mention}."),
            ephemeral=True)

    @config_group.command(name="set-review-channel",
        description="Configure the channel for officers to review pending bounties.")
    @app_commands.describe(channel="Channel where pending bounties post for officer review.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_review_channel(self, interaction: discord.Interaction,
                                 channel: discord.TextChannel) -> None:
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=error_embed("Cannot post there",
                    f"I need **Send Messages** + **Embed Links** in {channel.mention}."),
                ephemeral=True)
            return
        self.bot.db.set_config(CFG_REVIEW_CHANNEL, str(channel.id))  # type: ignore[attr-defined]
        info_log(f"{interaction.user} set bounty review channel to #{channel.name}.")

        backfilled = 0
        for b in _db_list(self.bot.db, statuses=(STATUS_PENDING,), limit=50):  # type: ignore[attr-defined]
            if not b.get("message_id"):
                await self._post_or_update_board_message(b)
                backfilled += 1

        suffix = f"\nPosted {backfilled} pending bounty/bounties." if backfilled else ""
        await interaction.response.send_message(
            embed=success_embed("Review channel configured",
                f"Pending bounties will post to {channel.mention} for officer review.{suffix}"),
            ephemeral=True)

    @config_group.command(name="set-flex-channel",
        description="Configure the channel for bounty payout shoutouts / flex.")
    @app_commands.describe(channel="Channel for celebrating big earners. Use a public hype channel.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_flex_channel(self, interaction: discord.Interaction,
                               channel: discord.TextChannel) -> None:
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=error_embed("Cannot post there",
                    f"I need **Send Messages** + **Embed Links** in {channel.mention}."),
                ephemeral=True)
            return
        self.bot.db.set_config(CFG_FLEX_CHANNEL, str(channel.id))  # type: ignore[attr-defined]
        info_log(f"{interaction.user} set bounty flex channel to #{channel.name}.")
        await interaction.response.send_message(
            embed=success_embed("Flex channel configured",
                f"Bounty payouts will now shout out in {channel.mention}."),
            ephemeral=True)

    # ── Background: deadline expiry ─────────────────────────────────────────
    @tasks.loop(minutes=10)
    async def deadline_check(self) -> None:
        try:
            db = self.bot.db  # type: ignore[attr-defined]
            for b in _db_overdue(db):
                _db_update(db, b["id"], status=STATUS_EXPIRED)
                await self._refresh_board_embed(b["id"])
                if (b.get("title") or "").startswith(self.SSO_TITLE_PREFIX):
                    await self._refresh_sso_route_board(create=True)
                info_log(f"Bounty #{b['id']} expired (deadline {b.get('deadline')}).")
        except Exception as exc:  # noqa: BLE001
            error_log(f"bounty deadline_check error: {exc!r}")

    @deadline_check.before_loop
    async def _before_deadline_check(self) -> None:
        await self.bot.wait_until_ready()

    # ── Background: unpaid bounty reminders ────────────────────────────────
    @tasks.loop(hours=6)
    async def payment_reminder(self) -> None:
        try:
            db = self.bot.db  # type: ignore[attr-defined]
            if (db.get_config("bounty_payment_reminder_enabled") or "1") == "0":
                return
            interval_hours = self._payment_config_int(
                "bounty_payment_reminder_hours",
                self.PAYMENT_REMINDER_DEFAULT_HOURS,
                minimum=1,
                maximum=168,
            )
            last_raw = db.get_config("bounty_payment_reminder_last_at")
            if last_raw:
                try:
                    last = datetime.datetime.fromisoformat(str(last_raw).replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=datetime.timezone.utc)
                    now = datetime.datetime.now(datetime.timezone.utc)
                    if (now - last.astimezone(datetime.timezone.utc)).total_seconds() < interval_hours * 3600:
                        return
                except (TypeError, ValueError):
                    pass

            all_rows = self._fetch_unpaid_bounty_payouts(limit=50)
            if not all_rows:
                return

            recent_days = self._payment_config_int(
                "bounty_payment_reminder_recent_days",
                self.PAYMENT_REMINDER_DEFAULT_DAYS,
                minimum=1,
                maximum=365,
            )
            now = datetime.datetime.now(datetime.timezone.utc)
            recent_cutoff = now - datetime.timedelta(days=recent_days)
            recent_rows: list[dict] = []
            stale_count = 0
            for row in all_rows:
                approved_at = self._bounty_payment_dt(row)
                if approved_at is None or approved_at >= recent_cutoff:
                    recent_rows.append(row)
                else:
                    stale_count += 1

            rows = recent_rows[: self.PAYMENT_REMINDER_BUTTON_LIMIT]
            if not rows:
                info_log(
                    "bounty payment reminder skipped; "
                    f"{stale_count} stale unpaid payout(s) older than {recent_days} day(s)."
                )
                return

            review_id = db.get_config(CFG_REVIEW_CHANNEL)
            if not review_id:
                return
            try:
                channel = self.bot.get_channel(int(review_id)) or await self.bot.fetch_channel(int(review_id))
            except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
            if not isinstance(channel, discord.TextChannel):
                return

            embed = self._build_payout_audit_embed(
                rows,
                title="Bounty payouts ready to settle",
                hidden_recent=max(0, len(recent_rows) - len(rows)),
                hidden_old=stale_count,
                old_days=recent_days,
            )
            view = discord.ui.View(timeout=None)
            for row in rows:
                view.add_item(BountyConfirmPaidButton(int(row["id"])))
            await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            db.set_config(
                "bounty_payment_reminder_last_at",
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            info_log(f"bounty payment reminder posted for {len(rows)} unpaid bounty/bounties.")
        except Exception as exc:  # noqa: BLE001
            error_log(f"bounty payment_reminder error: {exc!r}")

    @payment_reminder.before_loop
    async def _before_payment_reminder(self) -> None:
        await self.bot.wait_until_ready()

    # ── Background: daily Energy Core bounty ────────────────────────────────
    # Auto-creates one open Energy Core bounty per UTC day if none currently
    # exists. Configurable via guild_config:
    #   bounty_daily_energy_reward     (int silver, default 1_000_000)
    #   bounty_daily_energy_title      (str)
    #   bounty_daily_energy_description(str)
    #   bounty_daily_energy_enabled    ("0" to disable, default on)
    #   bounty_daily_energy_last_date  (internal — UTC date stamp YYYY-MM-DD)
    DAILY_ENERGY_DEFAULT_REWARD = 1_000_000
    DAILY_ENERGY_DEFAULT_TITLE  = "Deliver an Energy Core to the HO"
    DAILY_ENERGY_DEFAULT_DESC   = (
        "Bring **one Energy Core** to the guild Hideout. "
        "First member to deliver and submit proof (screenshot of drop-off) "
        "claims the reward. Payout depends on the core's tier — an officer "
        "confirms the tier on approval.\n\n"
        "**Daily auto-bounty** — resets every UTC day."
    )

    @tasks.loop(hours=1)
    async def daily_energy_core(self) -> None:
        try:
            db = self.bot.db  # type: ignore[attr-defined]
            if (db.get_config("bounty_daily_energy_enabled") or "1") == "0":
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            today = now.strftime("%Y-%m-%d")
            last_date = db.get_config("bounty_daily_energy_last_date")

            # Already posted today?
            if last_date == today:
                return

            # If last bounty is still open and posted today, skip (defensive).
            last_id_raw = db.get_config("bounty_daily_energy_last_id")
            if last_id_raw and last_id_raw.isdigit():
                last = _db_get(db, int(last_id_raw))
                if last and last["status"] in (STATUS_OPEN, STATUS_CLAIMED, STATUS_SUBMITTED):
                    posted_at = (last.get("posted_at") or "")[:10]
                    if posted_at == today:
                        db.set_config("bounty_daily_energy_last_date", today)
                        return

            # Read configurable knobs.
            try:
                reward = int(db.get_config("bounty_daily_energy_reward") or self.DAILY_ENERGY_DEFAULT_REWARD)
            except (TypeError, ValueError):
                reward = self.DAILY_ENERGY_DEFAULT_REWARD
            reward = max(0, min(reward, MAX_REWARD))
            if reward <= 0:
                return

            # Tiered payout scale (loaded from config, falls back to the
            # built-in Green/Blue/Purple/Gold scale). The bounty's headline
            # reward is the *highest* tier so leaderboards/audits see the
            # ceiling; the actual payout is selected by the officer at
            # approval time. ``reward`` from above is ignored when tiers are
            # in effect — keep the config key for backwards compat but
            # silently override below.
            tiers = _load_default_tiers(
                db, "daily_energy", DEFAULT_ENERGY_CORE_TIERS,
            )
            headline_reward = max(int(t.get("silver", 0)) for t in tiers)
            headline_reward = max(0, min(headline_reward, MAX_REWARD))

            title = (db.get_config("bounty_daily_energy_title") or self.DAILY_ENERGY_DEFAULT_TITLE).strip()
            base_desc = (db.get_config("bounty_daily_energy_description") or self.DAILY_ENERGY_DEFAULT_DESC).strip()
            description = (
                base_desc
                + "\n\n**💰 Payout by tier delivered:**\n"
                + _format_tier_scale(tiers)
            )

            # Deadline = next UTC midnight (rolls over with the daily reset).
            tomorrow = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            deadline = tomorrow.strftime("%Y-%m-%d %H:%M:%S")

            posted_by = str(self.bot.user.id) if self.bot.user else "system"

            # Create directly as OPEN (skip pending review — it's automated).
            if not db.connection:
                db.connect()
            db.cursor.execute(
                '''INSERT INTO bounties
                   (title, description, reward_points, posted_by, deadline, status)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (title, description, headline_reward, posted_by, deadline, STATUS_OPEN),
            )
            db.connection.commit()
            new_id = int(db.cursor.lastrowid or 0)
            if not new_id:
                error_log("daily_energy_core: failed to get new bounty id.")
                return

            # Snapshot the tier scale onto this bounty so the approve flow
            # can pay out the correct tier and future config edits don't
            # rewrite history.
            _save_bounty_tier_scale(db, new_id, tiers)

            db.set_config("bounty_daily_energy_last_date", today)
            db.set_config("bounty_daily_energy_last_id", str(new_id))

            await self._refresh_board_embed(new_id)
            info_log(
                f"daily_energy_core: posted tiered bounty #{new_id} "
                f"(ceiling {_fmt_silver(headline_reward)} silver, "
                f"{len(tiers)} tiers, deadline {deadline})."
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_energy_core error: {exc!r}")

    @daily_energy_core.before_loop
    async def _before_daily_energy_core(self) -> None:
        await self.bot.wait_until_ready()
        # Stagger to avoid colliding with deadline_check on startup.
        import asyncio
        await asyncio.sleep(60)

    @daily_energy_core.error
    async def _daily_energy_core_error(self, exc: BaseException) -> None:
        error_log(f"daily_energy_core task crashed: {exc!r}; restarting.")
        try:
            self.daily_energy_core.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart daily_energy_core: {restart_exc!r}")

    # ── Background: daily SSO Route bounty ──────────────────────────────────
    # Roads-of-Avalon hideouts have portals that re-roll constantly. Members
    # submit the day's route via the bounty system; on officer approval the
    # proof is auto-reposted to the SSO routes channel as a clean embed.
    #
    # Submission format expected (free text in /bounty submit proof):
    #     Secent-SA-Odesos > Birchcops > note: 30s from Caerleon portal
    # The `>` separator and a trailing `note:` are conventional but not
    # enforced — whatever the claimer writes gets reposted verbatim.
    #
    # Config keys:
    #   sso_routes_channel_id              — destination for approved routes
    #   bounty_daily_sso_reward            (default 250_000)
    #   bounty_daily_sso_title             (override title)
    #   bounty_daily_sso_description       (override flavor text)
    #   bounty_daily_sso_enabled           ("0" to disable)
    #   bounty_daily_sso_last_date         (internal)
    #   bounty_daily_sso_last_id           (internal)
    SSO_TITLE_PREFIX               = "[SSO Route]"
    SSO_MAX_PORTALS                = 3
    DAILY_SSO_DEFAULT_REWARD       = 250_000
    DAILY_SSO_DEFAULT_TITLE        = f"{SSO_TITLE_PREFIX} Submit today's HO portal route"
    DAILY_SSO_DEFAULT_DESC = (
        "Roads-of-Avalon portals have re-rolled. Scout the route from the "
        "guild Hideout and submit it as proof using **/bounty submit**.\n\n"
        "**Format** — up to **3 portals** separated by `>`, optional `note:` "
        "and `ttl:` (time the connection has left, e.g. `1h30m`, `45m`, `2h`):\n"
        "```\n"
        "Sentinel-SA-Odesos > Birchcops > Caerleon > note: 30s from Caerleon portal > ttl: 1h45m\n"
        "```\n"
        "Fewer portals are fine. The TTL lets the bot auto-open a new SSO "
        "bounty as soon as your route closes — keep it accurate. First valid "
        "submission wins."
    )

    @tasks.loop(minutes=10)
    async def daily_sso_route(self) -> None:
        try:
            db = self.bot.db  # type: ignore[attr-defined]
            if (db.get_config("bounty_daily_sso_enabled") or "1") == "0":
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            today = now.strftime("%Y-%m-%d")
            last_date = db.get_config("bounty_daily_sso_last_date")

            # Trigger conditions:
            #   1. New UTC day and we haven't posted yet today.
            #   2. The previous route's TTL has expired (re-roll requested).
            should_post = False
            if last_date != today:
                should_post = True
            else:
                expires_iso = db.get_config("bounty_daily_sso_expires_at")
                if expires_iso:
                    try:
                        expires_at = datetime.datetime.fromisoformat(expires_iso)
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
                        if now >= expires_at:
                            # Only re-roll if the previous bounty already finished
                            # (we don't want two open SSO bounties at once).
                            last_id_raw = db.get_config("bounty_daily_sso_last_id")
                            if last_id_raw and last_id_raw.isdigit():
                                last = _db_get(db, int(last_id_raw))
                                if not last or last["status"] not in ACTIVE_STATUSES:
                                    should_post = True
                            else:
                                should_post = True
                    except (TypeError, ValueError):
                        pass

            if not should_post:
                return

            # If yesterday's daily SSO is still active, skip (keep one open).
            last_id_raw = db.get_config("bounty_daily_sso_last_id")
            if last_id_raw and last_id_raw.isdigit():
                last = _db_get(db, int(last_id_raw))
                if last and last["status"] in ACTIVE_STATUSES:
                    posted_at = (last.get("posted_at") or "")[:10]
                    if posted_at == today:
                        db.set_config("bounty_daily_sso_last_date", today)
                        return

            try:
                reward = int(db.get_config("bounty_daily_sso_reward") or self.DAILY_SSO_DEFAULT_REWARD)
            except (TypeError, ValueError):
                reward = self.DAILY_SSO_DEFAULT_REWARD
            reward = max(0, min(reward, MAX_REWARD))
            if reward <= 0:
                return

            title = (db.get_config("bounty_daily_sso_title") or self.DAILY_SSO_DEFAULT_TITLE).strip()
            # Force the SSO prefix so the approve hook detects it even if
            # an officer overrode the title.
            if not title.startswith(self.SSO_TITLE_PREFIX):
                title = f"{self.SSO_TITLE_PREFIX} {title}"

            description = (db.get_config("bounty_daily_sso_description") or self.DAILY_SSO_DEFAULT_DESC).strip()

            tomorrow = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            deadline = tomorrow.strftime("%Y-%m-%d %H:%M:%S")
            posted_by = str(self.bot.user.id) if self.bot.user else "system"

            if not db.connection:
                db.connect()
            db.cursor.execute(
                '''INSERT INTO bounties
                   (title, description, reward_points, posted_by, deadline, status)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (title, description, reward, posted_by, deadline, STATUS_OPEN),
            )
            db.connection.commit()
            new_id = int(db.cursor.lastrowid or 0)
            if not new_id:
                error_log("daily_sso_route: failed to get new bounty id.")
                return

            db.set_config("bounty_daily_sso_last_date", today)
            db.set_config("bounty_daily_sso_last_id", str(new_id))
            # Clear any stale TTL so we don't immediately re-trigger.
            db.set_config("bounty_daily_sso_expires_at", "")

            await self._refresh_board_embed(new_id)
            info_log(
                f"daily_sso_route: posted bounty #{new_id} "
                f"({_fmt_silver(reward)} silver, deadline {deadline})."
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_sso_route error: {exc!r}")

    @daily_sso_route.before_loop
    async def _before_daily_sso_route(self) -> None:
        await self.bot.wait_until_ready()
        import asyncio
        await asyncio.sleep(120)

    @daily_sso_route.error
    async def _daily_sso_route_error(self, exc: BaseException) -> None:
        error_log(f"daily_sso_route task crashed: {exc!r}; restarting.")
        try:
            self.daily_sso_route.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart daily_sso_route: {restart_exc!r}")

    # ── Helper: parse SSO route proof string ────────────────────────────────
    @staticmethod
    def _parse_sso_route(proof: str) -> tuple[list[str], str | None, int | None]:
        """Parse a route submission into (portals, note, ttl_minutes).

        Accepts segments separated by ``>``. Any segment starting with
        ``note:`` is the note, ``ttl:`` is the time remaining. All other
        segments are portal names (up to ``SSO_MAX_PORTALS``).

        TTL accepts formats: ``90m``, ``1h30m``, ``2h``, ``45``. Returns
        minutes (int) or None if missing/unparseable.
        """
        portals: list[str] = []
        note: str | None = None
        ttl_min: int | None = None

        for raw in proof.split(">"):
            seg = raw.strip()
            if not seg:
                continue
            low = seg.lower()
            if low.startswith("note:"):
                note = seg.split(":", 1)[1].strip() or None
                continue
            if low.startswith("ttl:"):
                ttl_raw = seg.split(":", 1)[1].strip().lower()
                for old, new in (
                    ("hours", "h"),
                    ("hour", "h"),
                    ("hrs", "h"),
                    ("hr", "h"),
                    ("minutes", "m"),
                    ("minute", "m"),
                    ("mins", "m"),
                    ("min", "m"),
                ):
                    ttl_raw = ttl_raw.replace(old, new)
                ttl_raw = ttl_raw.replace(" ", "")
                if not ttl_raw:
                    continue
                m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", ttl_raw)
                if m and (m.group(1) or m.group(2)):
                    h = int(m.group(1) or 0)
                    mn = int(m.group(2) or 0)
                    ttl_min = h * 60 + mn
                elif ttl_raw.isdigit():
                    ttl_min = int(ttl_raw)
                continue
            portals.append(seg)

        return portals[: Bounties.SSO_MAX_PORTALS], note, ttl_min

    # ── Helper: repost approved SSO route to the routes channel ─────────────
    async def _maybe_post_sso_route(self, bounty: dict, claimer: str | None) -> None:
        """If the bounty title is tagged ``[SSO Route]``, post the approved
        proof (route string) to the configured ``sso_routes_channel_id`` as
        a clean embed for guild visibility. No-ops silently otherwise.

        Also stores the route's TTL expiry as ``bounty_daily_sso_expires_at``
        so the daily loop can auto-open a fresh request once portals close.
        """
        title = bounty.get("title") or ""
        if not title.startswith(self.SSO_TITLE_PREFIX):
            return

        db = self.bot.db  # type: ignore[attr-defined]

        proof = (bounty.get("proof") or "").strip()
        if not proof:
            return

        portals, note, ttl_min = self._parse_sso_route(proof)

        now = datetime.datetime.now(datetime.timezone.utc)
        # Record TTL expiry for re-roll scheduling. Falls back to next UTC
        # midnight if the scout didn't supply a TTL.
        if ttl_min and ttl_min > 0:
            expires_at = now + datetime.timedelta(minutes=ttl_min)
        else:
            expires_at = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        db.set_config("bounty_daily_sso_expires_at", expires_at.isoformat())

        db.set_config("sso_routes_closed_bounty_id", "")
        try:
            msg = await self._refresh_sso_route_board(create=True)
            if msg is None:
                info_log(
                    f"sso route board not refreshed from bounty #{bounty.get('id')}: "
                    "sso_routes_channel_id is not configured or reachable."
                )
                return
            info_log(
                f"sso route board refreshed from bounty #{bounty.get('id')} "
                f"(closes in {ttl_min}m)." if ttl_min else
                f"sso route board refreshed from bounty #{bounty.get('id')} (no TTL)."
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"sso route board refresh failed: {exc!r}")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Bounties(bot))
