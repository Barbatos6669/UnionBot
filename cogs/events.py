from cogs._typing import Bot
import asyncio
import datetime
from pathlib import Path
import discord
from discord.ext import commands, tasks
from debug import info_log, error_log
from config import LIFECYCLE_ROLES, STAFF_ROLES, derive_lifecycle
from cogs._nickname_tags import (
    strip_managed_nickname_tag,
    tagged_nickname_for_profile,
)
from cogs.graphs import update_live_graphs
from cogs.staff import rebalance_staff, refresh_staff_board
from cogs.points import award_fame_points, announce_points
from cogs.automation import (
    check_fame_milestones, check_anti_poach,
    cleanup_orphan_guilds, archive_completed_events,
)
import albion_api
from time_utils import utc_now_naive

# Lifecycle roles that auto-progress by time. Alumni is officer-managed only
# (a graceful "left the guild but still welcome" status). Recruit auto-progresses
# upward — once a Recruit's Discord tenure reaches Member/Veteran thresholds,
# they're promoted automatically. They will not be downgraded to Probationary.
_AUTO_LIFECYCLE = ("Recruit", "Probationary", "Member", "Veteran", "Inactive")
CFG_GOODBYE_CHANNEL = "goodbye_channel_id"
CFG_LIFECYCLE_VC_INACTIVITY_DAYS = "lifecycle_vc_inactivity_days"
CFG_LIFECYCLE_STAT_INACTIVITY_DAYS = "lifecycle_stat_inactivity_days"
CFG_LIFECYCLE_INACTIVITY_MODE = "lifecycle_inactivity_mode"
DEFAULT_LIFECYCLE_VC_INACTIVITY_DAYS = 7
DEFAULT_LIFECYCLE_STAT_INACTIVITY_DAYS = 14
DEFAULT_LIFECYCLE_INACTIVITY_MODE = "either"
_DEFAULT_HOME_GUILD_REQUIRED_STAFF = (
    "Commander",
    *(role for role in STAFF_ROLES if "Shotcaller" not in role),
)


def _parse_lifecycle_activity_dt(value: object) -> datetime.datetime | None:
    """Parse an activity timestamp into a naive UTC datetime.

    ``last_activity_date`` is a timestamp, while ``voice_activity.date_utc`` is
    a YYYY-MM-DD day bucket. Date-only voice buckets count through the end of
    that UTC day so someone who joined VC today is not stale by midnight math.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:
            day = datetime.date.fromisoformat(raw)
            return datetime.datetime.combine(day, datetime.time.max)
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


def _last_voice_activity_dt(db, discord_id: str) -> datetime.datetime | None:
    """Return the member's most recent guild voice day, if recorded."""
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """
            SELECT MAX(date_utc) AS last_voice
              FROM voice_activity
             WHERE discord_id = ?
               AND seconds > 0
            """,
            (str(discord_id),),
        )
        row = db.cursor.fetchone()
        if not row:
            return None
        return _parse_lifecycle_activity_dt(row["last_voice"])
    except Exception as exc:  # noqa: BLE001
        error_log(f"lifecycle voice activity lookup failed for {discord_id}: {exc!r}")
        return None


def _effective_lifecycle_activity_dt(db, profile: dict) -> datetime.datetime | None:
    """Freshest activity signal used by lifecycle inactivity automation."""
    discord_id = str(profile.get("discord_id") or "")
    candidates = [
        _parse_lifecycle_activity_dt(profile.get("last_activity_date")),
        _last_voice_activity_dt(db, discord_id) if discord_id else None,
    ]
    candidates = [candidate for candidate in candidates if candidate is not None]
    return max(candidates) if candidates else None


def _get_lifecycle_int_config(db, key: str, default: int, *, minimum: int = 1, maximum: int = 365) -> int:
    raw = db.get_config(key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def _idle_days(now: datetime.datetime, last_seen: datetime.datetime | None) -> int:
    if last_seen is None:
        return 0
    return max(0, (now - last_seen).days)


def _lifecycle_inactivity_state(
    db,
    profile: dict,
    *,
    baseline: datetime.datetime | None,
    now: datetime.datetime,
    vc_days: int,
    stat_days: int,
    mode: str,
) -> tuple[bool, str]:
    """Return whether a profile should be treated as lifecycle-inactive.

    Stat movement and voice presence answer different questions:
    - stat movement proves the Albion character is actively progressing;
    - voice presence proves the Discord member is showing up with the guild.

    Missing history starts from the member's join/verification baseline so a
    brand-new verified player is not marked inactive immediately.
    """
    baseline = baseline or now
    stat_last = _parse_lifecycle_activity_dt(profile.get("last_activity_date")) or baseline
    voice_last = _last_voice_activity_dt(db, str(profile.get("discord_id") or "")) or baseline

    stat_idle = _idle_days(now, stat_last)
    vc_idle = _idle_days(now, voice_last)
    stat_stale = stat_idle >= max(1, int(stat_days))
    vc_stale = vc_idle >= max(1, int(vc_days))
    clean_mode = (mode or DEFAULT_LIFECYCLE_INACTIVITY_MODE).strip().lower()
    if clean_mode == "both":
        inactive = stat_stale and vc_stale
        joiner = " and "
    else:
        inactive = stat_stale or vc_stale
        joiner = " or "

    reasons: list[str] = []
    if vc_stale:
        reasons.append(f"no VC {vc_idle}d/{vc_days}d")
    if stat_stale:
        reasons.append(f"no stat movement {stat_idle}d/{stat_days}d")
    return inactive, joiner.join(reasons)


def _tenure_text(
    joined_at: datetime.datetime | None,
    now: datetime.datetime | None = None,
) -> str:
    if not joined_at:
        return "Unknown"
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    days = max(0, (now.astimezone(datetime.timezone.utc) - joined_at.astimezone(datetime.timezone.utc)).days)
    if days == 0:
        return "Less than 1 day"
    if days == 1:
        return "1 day"
    return f"{days} days"


def _format_goodbye_embed(
    member: discord.Member,
    profile: dict | None,
    *,
    now: datetime.datetime | None = None,
) -> discord.Embed:
    profile = profile or {}
    display_name = member.display_name or str(member)
    embed = discord.Embed(
        title="Member Left",
        description=f"**{display_name}** has left **{member.guild.name}**.",
        color=discord.Color.dark_gray(),
        timestamp=now or datetime.datetime.now(datetime.timezone.utc),
    )

    albion_name = (profile.get("albion_name") or "").strip()
    guild_name = (profile.get("guild_name") or "").strip()
    if albion_name:
        albion_value = f"**{albion_name}**"
        if guild_name:
            albion_value += f"\nGuild: **{guild_name}**"
    else:
        albion_value = "Not registered"

    lifecycle = (profile.get("lifecycle_role") or "").strip() or "Unassigned"
    joined_at = getattr(member, "joined_at", None)
    joined_line = ""
    if joined_at:
        if joined_at.tzinfo is None:
            joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
        joined_line = f"\nJoined: <t:{int(joined_at.timestamp())}:D>"

    embed.add_field(name="Albion", value=albion_value, inline=True)
    embed.add_field(name="Lifecycle", value=lifecycle, inline=True)
    embed.add_field(
        name="Time in server",
        value=f"{_tenure_text(joined_at, now)}{joined_line}",
        inline=False,
    )
    embed.set_footer(text=f"User ID: {member.id}")
    avatar = getattr(member, "display_avatar", None)
    avatar_url = getattr(avatar, "url", None)
    if avatar_url:
        embed.set_thumbnail(url=str(avatar_url))
    return embed


def _activity_dispatch_style(
    *,
    kill_delta: int,
    pve_delta: int,
    gather_delta: int,
    craft_delta: int,
    fish_delta: int,
) -> tuple[str, str, discord.Color]:
    """Return a light RP headline for the dominant activity in this sync."""
    scores = {
        "pvp": max(0, int(kill_delta or 0)) * 10,
        "pve": max(0, int(pve_delta or 0)),
        "gather": max(0, int(gather_delta or 0)) * 2,
        "craft": max(0, int(craft_delta or 0)),
        "fish": max(0, int(fish_delta or 0)),
    }
    kind = max(scores, key=scores.get)
    if scores[kind] <= 0:
        return (
            "📜 Union field report",
            "**Union Dispatch:** the ledger recorded fresh movement.",
            discord.Color.green(),
        )

    if kind == "pvp":
        if kill_delta >= 1_000_000:
            line = "**War Ledger:** a major hostile account was settled in the field."
        elif kill_delta >= 250_000:
            line = "**War Ledger:** the Union took payment in steel."
        else:
            line = "**Field Notice:** blades were drawn and the ledger moved."
        return "⚔️ Union war ledger", line, discord.Color.red()

    if kind == "pve":
        return (
            "🗺️ Union expedition report",
            "**Expedition Report:** the road paid out, and the party brought proof home.",
            discord.Color.green(),
        )

    if kind == "gather":
        return (
            "📦 Quartermaster's notice",
            "**Quartermaster's Notice:** raw goods are moving back into Union hands.",
            discord.Color.dark_gold(),
        )

    if kind == "craft":
        return (
            "🔨 Forge ledger",
            "**Forge Ledger:** bench work turned planning into inventory.",
            discord.Color.orange(),
        )

    return (
        "🎣 Provision ledger",
        "**Provision Ledger:** steady work brought supplies in from the waterline.",
        discord.Color.teal(),
    )


def _activity_feed_config(db) -> dict[str, int]:
    """Runtime knobs for keeping the public activity feed selective."""
    defaults = {
        "activity_feed_min_points": 50,
        "activity_feed_major_pvp_fame": 250_000,
        "activity_feed_major_pve_fame": 1_000_000,
        "activity_feed_major_gather_fame": 500_000,
        "activity_feed_major_craft_fame": 500_000,
        "activity_feed_major_fish_fame": 250_000,
    }
    values: dict[str, int] = {}
    for key, default in defaults.items():
        try:
            values[key] = max(0, int(db.get_config(key) or default))
        except (TypeError, ValueError):
            values[key] = default
    return values


class Events(commands.Cog):
    def __init__(self, bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self):
        self.sync_guilds.start()
        self.prune_history.start()
        self.daily_backup.start()
        self.daily_anniversaries.start()
        self.weekly_recap.start()
        self.daily_recap.start()
        self.refresh_guild_scan.start()

    def cog_unload(self):
        self.sync_guilds.cancel()
        self.prune_history.cancel()
        self.daily_backup.cancel()
        self.daily_anniversaries.cancel()
        self.weekly_recap.cancel()
        self.daily_recap.cancel()
        self.refresh_guild_scan.cancel()

    async def _risk_watch_channel(self) -> discord.TextChannel | discord.Thread | None:
        """Officer-only destination for Albion risk-watch alerts."""
        db = self.bot.db
        chan_id = (
            db.get_config("risk_watch_channel_id")
            or db.get_config("automation_officer_channel_id")
            or db.get_config("officer_channel_id")
        )
        if not chan_id:
            return None
        try:
            channel = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return None
        return channel if isinstance(channel, (discord.TextChannel, discord.Thread)) else None

    def _home_guild_required_staff_roles(self) -> tuple[str, ...]:
        """Staff/leadership roles that require current home-guild membership.

        Override with guild_config ``home_guild_required_staff_roles`` as a
        comma-separated list. Shotcaller roles are intentionally not in the
        default set because alliance/guest content callers may use them.
        """
        raw = (self.bot.db.get_config("home_guild_required_staff_roles") or "").strip()
        if not raw:
            return tuple(_DEFAULT_HOME_GUILD_REQUIRED_STAFF)
        roles = tuple(
            part.strip()
            for part in raw.replace("|", ",").split(",")
            if part.strip()
        )
        return roles or tuple(_DEFAULT_HOME_GUILD_REQUIRED_STAFF)

    async def _strip_home_guild_staff_roles(
        self,
        discord_guild: discord.Guild,
        member: discord.Member,
        profile: dict,
        *,
        current_guild_name: str | None,
        home_guild: str,
    ) -> None:
        """Remove TU-only staff positions from members no longer in TU."""
        required_role_names = self._home_guild_required_staff_roles()
        required_set = set(required_role_names)
        to_remove = [
            role for role in member.roles
            if role.name in required_set and not role.managed
        ]
        removed_names: list[str] = []
        for role in to_remove:
            try:
                await member.remove_roles(
                    role,
                    reason=(
                        "Left home Albion guild "
                        f"({home_guild}); current guild: {current_guild_name or 'none'}"
                    ),
                )
                removed_names.append(role.name)
            except discord.Forbidden:
                error_log(
                    f"Cannot remove staff role {role.name!r} from {member} "
                    f"({member.id}); bot lacks role hierarchy/permission."
                )
            except discord.HTTPException as exc:
                error_log(
                    f"HTTP error removing staff role {role.name!r} from "
                    f"{member} ({member.id}): {exc}"
                )

        grant_rows = 0
        try:
            grant_rows = self.bot.db.revoke_staff_grants(
                str(member.id), required_role_names,
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"revoke_staff_grants failed for {member.id}: {exc!r}")

        if not removed_names and not grant_rows:
            return

        albion_name = profile.get("albion_name") or member.display_name
        current = (current_guild_name or "").strip() or "No guild shown"
        info_log(
            f"Removed TU-only staff positions from {member} ({albion_name}) "
            f"after guild changed to {current}: roles={removed_names}, "
            f"grant_rows={grant_rows}."
        )

        channel = await self._risk_watch_channel()
        if channel is None:
            return
        embed = discord.Embed(
            title="🛂 Staff roles removed — left TU",
            description=(
                f"{member.mention} is no longer showing in **{home_guild}**, "
                "so TU-only staff positions were removed."
            ),
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Albion character", value=f"**{albion_name}**", inline=True)
        embed.add_field(name="Current Albion guild", value=f"**{current}**", inline=True)
        if removed_names:
            embed.add_field(
                name="Discord roles removed",
                value=", ".join(f"`{name}`" for name in removed_names)[:1024],
                inline=False,
            )
        if grant_rows:
            embed.add_field(
                name="Staff tenure rows cleared",
                value=f"Removed **{grant_rows}** staff grant record(s).",
                inline=False,
            )
        embed.set_footer(text=f"Discord ID: {member.id}")
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"staff removal officer alert failed: {exc!r}")

    async def _maybe_alert_risk_watch(self, profile: dict, player_stats: dict) -> None:
        """Alert officers when a watched Albion character changes public guild.

        This is intentionally private and review-oriented. The bot does not
        contact outside guilds or make public accusations automatically.
        """
        player_id = str(profile.get("albion_player_id") or "").strip()
        if not player_id:
            return
        try:
            watch = self.bot.db.fetch_risk_watch(player_id)
        except Exception as exc:  # noqa: BLE001
            error_log(f"risk watch lookup failed for {player_id}: {exc!r}")
            return
        if not watch:
            return

        current = {
            "albion_name": (player_stats.get("albion_name") or profile.get("albion_name") or watch.get("albion_name") or "").strip(),
            "guild_id": (player_stats.get("guild_id") or "").strip(),
            "guild_name": (player_stats.get("guild_name") or "").strip(),
            "alliance_id": (player_stats.get("alliance_id") or "").strip(),
            "alliance_name": (player_stats.get("alliance_name") or "").strip(),
            "alliance_tag": (player_stats.get("alliance_tag") or "").strip(),
        }
        prior = {
            "guild_id": (watch.get("last_guild_id") or "").strip(),
            "guild_name": (watch.get("last_guild_name") or "").strip(),
            "alliance_id": (watch.get("last_alliance_id") or "").strip(),
            "alliance_name": (watch.get("last_alliance_name") or "").strip(),
            "alliance_tag": (watch.get("last_alliance_tag") or "").strip(),
        }

        # First observation establishes a baseline. This avoids an old watch
        # entry firing immediately on bot startup just because it has no memory.
        if not (prior["guild_id"] or prior["guild_name"] or prior["alliance_id"] or prior["alliance_name"]):
            self.bot.db.update_risk_watch_seen(
                albion_player_id=player_id,
                albion_name=current["albion_name"],
                guild_id=current["guild_id"],
                guild_name=current["guild_name"],
                alliance_id=current["alliance_id"],
                alliance_name=current["alliance_name"],
                alliance_tag=current["alliance_tag"],
            )
            return

        def _guild_key(prefix: dict) -> str:
            return (prefix.get("guild_id") or prefix.get("guild_name") or "").lower()

        guild_changed = _guild_key(prior) != _guild_key(current)
        alliance_changed = (
            (prior["alliance_id"] or prior["alliance_name"] or prior["alliance_tag"]).lower()
            != (current["alliance_id"] or current["alliance_name"] or current["alliance_tag"]).lower()
        )
        if not guild_changed and not alliance_changed:
            self.bot.db.update_risk_watch_seen(
                albion_player_id=player_id,
                albion_name=current["albion_name"],
                guild_id=current["guild_id"],
                guild_name=current["guild_name"],
                alliance_id=current["alliance_id"],
                alliance_name=current["alliance_name"],
                alliance_tag=current["alliance_tag"],
            )
            return

        def _guild_label(data: dict) -> str:
            name = data.get("guild_name") or "No guild shown"
            gid = data.get("guild_id") or ""
            return f"**{name}**" + (f"\n`{gid}`" if gid else "")

        def _alliance_label(data: dict) -> str:
            tag = data.get("alliance_tag") or ""
            name = data.get("alliance_name") or ""
            aid = data.get("alliance_id") or ""
            label = " / ".join(part for part in (tag, name) if part) or "No alliance shown"
            return f"**{label}**" + (f"\n`{aid}`" if aid else "")

        channel = await self._risk_watch_channel()
        posted = False
        if channel is not None:
            title = "⚠️ Albion Risk Watch — guild changed" if guild_changed else "⚠️ Albion Risk Watch — alliance changed"
            embed = discord.Embed(
                title=title,
                description=(
                    f"Watched character **{current['albion_name'] or player_id}** changed public Albion affiliation.\n\n"
                    "Review the evidence before contacting anyone outside the server."
                ),
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Previous guild", value=_guild_label(prior), inline=True)
            embed.add_field(name="Current guild", value=_guild_label(current), inline=True)
            embed.add_field(name="Current alliance", value=_alliance_label(current), inline=False)
            if watch.get("reason"):
                embed.add_field(name="Watch reason", value=str(watch["reason"])[:1024], inline=False)
            if watch.get("evidence_note"):
                embed.add_field(name="Evidence note", value=str(watch["evidence_note"])[:1024], inline=False)
            embed.add_field(
                name="Suggested next step",
                value=(
                    "Confirm the player identity and evidence. If leadership chooses to warn the new guild, "
                    "keep it factual: what happened, what proof exists, and who they can contact."
                ),
                inline=False,
            )
            embed.set_footer(text=f"Albion player ID: {player_id}")
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                posted = True
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"risk watch alert failed for {player_id}: {exc!r}")

        self.bot.db.update_risk_watch_seen(
            albion_player_id=player_id,
            albion_name=current["albion_name"],
            guild_id=current["guild_id"],
            guild_name=current["guild_name"],
            alliance_id=current["alliance_id"],
            alliance_name=current["alliance_name"],
            alliance_tag=current["alliance_tag"],
            alerted=posted,
        )
        info_log(
            f"Risk watch change for {current['albion_name'] or player_id}: "
            f"{prior.get('guild_name') or 'none'} -> {current.get('guild_name') or 'none'}"
        )

    @commands.Cog.listener()
    async def on_ready(self):
        # Run-once-per-process: do an initial Discord inventory sync so other
        # cogs (LFG, etc.) can read fresh role/channel data from the DB
        # immediately, instead of waiting up to an hour for the first tick.
        if getattr(self, "_ready_done", False):
            return
        self._ready_done = True
        await self._sync_discord_inventory()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """When a member leaves the Discord server, wipe their Albion link
        and any TU history flag so they no longer appear on leaderboards or
        guild-history reports. Keeps their lifetime stats out of stale
        rankings. Re-registering after rejoining is intentional and required.
        """
        try:
            db = self.bot.db
            profile = db.fetch_user_profile(str(member.id))
            # Always log the leave event, even if the user never registered,
            # so the dashboard's joiners/leavers chart reflects real churn.
            try:
                db.log_member_lifecycle_event(
                    str(member.guild.id), str(member.id),
                    "leave",
                    name=member.display_name or str(member),
                )
            except Exception:  # noqa: BLE001
                pass
            if not member.bot:
                moderated_removal = await self._recent_moderated_removal(member)
                await self._send_goodbye(member, profile)
                if not moderated_removal:
                    await self._send_exit_survey(member, profile)
            if not profile:
                return
            had_albion = bool(profile.get("albion_player_id"))
            db.clear_user_albion_info(str(member.id))
            # Also drop the TU history flag — they're gone from Discord, so
            # if they ever come back they should re-prove guild membership.
            try:
                db.set_was_in_home_guild(str(member.id), False)
            except Exception:  # noqa: BLE001
                pass
            info_log(
                f"on_member_remove: cleared profile for {member} ({member.id}); "
                f"had_albion={had_albion}, prev_lifecycle={profile.get('lifecycle_role')!r}."
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"on_member_remove cleanup failed for {member}: {exc!r}")

    async def _recent_moderated_removal(self, member: discord.Member) -> str | None:
        """Return 'kick'/'ban' when audit logs show staff caused this leave."""
        guild = member.guild
        me = guild.me
        if not me or not me.guild_permissions.view_audit_log:
            return None

        # Audit-log entries can arrive a moment after on_member_remove fires.
        await asyncio.sleep(1.5)
        cutoff = discord.utils.utcnow() - datetime.timedelta(seconds=90)
        actions = (
            (discord.AuditLogAction.kick, "kick"),
            (discord.AuditLogAction.ban, "ban"),
        )
        for action, label in actions:
            try:
                async for entry in guild.audit_logs(limit=8, action=action):
                    target_id = getattr(entry.target, "id", None)
                    created_at = entry.created_at
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=datetime.timezone.utc)
                    if target_id == member.id and created_at >= cutoff:
                        return label
            except (discord.Forbidden, discord.HTTPException):
                return None
        return None

    async def _send_goodbye(
        self,
        member: discord.Member,
        profile: dict | None,
    ) -> None:
        db = self.bot.db
        channel_id = (
            db.get_config(CFG_GOODBYE_CHANNEL)
            or db.get_config("welcome_channel_id")
            or ""
        )
        if not channel_id:
            return
        try:
            channel = member.guild.get_channel(int(channel_id))
        except (TypeError, ValueError):
            channel = None
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.send(
                embed=_format_goodbye_embed(member, profile),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            info_log(f"Posted goodbye message for {member} ({member.id}).")
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"goodbye message failed for {member} ({member.id}): {exc!r}")

    async def _send_exit_survey(
        self,
        member: discord.Member,
        profile: dict | None,
    ) -> None:
        surveys = self.bot.get_cog("Surveys")
        sender = getattr(surveys, "send_exit_survey", None)
        if sender is None:
            return
        try:
            await sender(member, profile)
        except Exception as exc:  # noqa: BLE001
            error_log(f"exit survey dispatch failed for {member} ({member.id}): {exc!r}")

    @tasks.loop(minutes=2)
    async def sync_guilds(self):
        # Wrap the entire body so a single bad row / API hiccup can never kill
        # the loop. tasks.loop stops permanently on an unhandled exception
        # unless we re-raise; we never want that for a periodic sync.
        #
        # Restart cooldown: if the previous run started less than
        # `sync_cooldown_minutes` ago (default 50, ~1 sync per hour-ish), skip
        # this iteration. This keeps dev restart-storms from re-running every
        # player API call. Manual /admin sync-now bypasses this via
        # `force_sync_now()`.
        try:
            import datetime as _dt
            # Default cooldown 0 = run every loop tick. Stored config
            # `sync_cooldown_minutes` still wins if you want to slow it down.
            cooldown_min = 0
            try:
                raw = self.bot.db.get_config("sync_cooldown_minutes")
                if raw is not None:
                    cooldown_min = max(0, int(raw))
            except (TypeError, ValueError):
                pass
            last_iso = self.bot.db.get_config("last_sync_started_at")
            if last_iso and cooldown_min > 0:
                try:
                    last = _dt.datetime.fromisoformat(last_iso)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=_dt.timezone.utc)
                    age = _dt.datetime.now(_dt.timezone.utc) - last
                    if age < _dt.timedelta(minutes=cooldown_min):
                        info_log(
                            f"sync_guilds: skipping — last run {int(age.total_seconds() // 60)}m ago "
                            f"(< {cooldown_min}m cooldown). Use /admin sync-now to force."
                        )
                        return
                except ValueError:
                    pass
            self.bot.db.set_config(
                "last_sync_started_at",
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
            )
            await self._sync_discord_inventory()
            await self._sync_guilds_inner()
        except Exception as exc:  # noqa: BLE001
            error_log(f"sync_guilds: unhandled error, will retry next interval: {exc!r}")

    async def force_sync_now(self) -> None:
        """Bypass the cooldown — runs the full sync immediately. Used by
        /admin sync-now so officers can override on demand."""
        import datetime as _dt
        try:
            self.bot.db.set_config(
                "last_sync_started_at",
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
            )
            await self._sync_discord_inventory()
            await self._sync_guilds_inner()
        except Exception as exc:  # noqa: BLE001
            error_log(f"force_sync_now: {exc!r}")
            raise

    async def _sync_discord_inventory(self) -> None:
        """Refresh the cached snapshot of every connected guild's roles + channels.

        Cheap (no API calls — discord.py already caches these). Done on every
        sync tick so renames / new channels propagate within an hour, and on
        startup via cog_load so other cogs can read fresh data immediately.

        Also writes a human-readable dump to ``data/guild-scan-<id>.txt`` so
        the file is always available for debugging / sharing without needing
        to invoke ``/lfg scan-guild`` interactively.
        """
        # Imported here to avoid a circular import at module load time.
        from cogs.lfg import write_guild_scan_file
        for guild in self.bot.guilds:
            try:
                roles, chans, members = self.bot.db.sync_discord_inventory(guild)
                info_log(
                    f"Synced Discord inventory for {guild.name}: "
                    f"{roles} roles, {chans} channels, {members} members."
                )
                # Seed the lifecycle audit log from joined_at for any
                # historical members we haven't recorded yet. Idempotent —
                # the unique index drops duplicates on subsequent runs.
                try:
                    seeded = self.bot.db.backfill_member_joins(str(guild.id))
                    if seeded:
                        info_log(f"Lifecycle backfill: +{seeded} historical join(s) for {guild.name}.")
                except Exception as exc:  # noqa: BLE001
                    error_log(f"Lifecycle backfill failed for {guild.name}: {exc!r}")
                path = write_guild_scan_file(guild, self.bot.db)
                if path:
                    info_log(f"Wrote guild scan to {path}.")
            except Exception as exc:  # noqa: BLE001
                error_log(f"Discord inventory sync failed for {guild.name}: {exc!r}")

    def _should_snapshot_history(self) -> bool:
        """True at most once per `history_snapshot_minutes` (default 60).

        The sync loop runs every couple of minutes for near-live reconcile +
        graphs, but we only want to record stats-history rows once per hour
        to keep the DB small and graph queries fast.
        """
        import datetime as _dt
        try:
            interval = int(self.bot.db.get_config("history_snapshot_minutes") or 60)
        except (TypeError, ValueError):
            interval = 60
        if interval <= 0:
            return True
        last_iso = self.bot.db.get_config("last_history_snapshot_at")
        now = _dt.datetime.now(_dt.timezone.utc)
        if last_iso:
            try:
                last = _dt.datetime.fromisoformat(last_iso)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=_dt.timezone.utc)
                if (now - last) < _dt.timedelta(minutes=interval):
                    return False
            except ValueError:
                pass
        self.bot.db.set_config("last_history_snapshot_at", now.isoformat())
        return True

    async def _sync_guilds_inner(self):
        guilds = self.bot.db.fetch_all_guilds()
        # Decide once per pass whether this pass also writes history rows.
        # Reconcile / live graphs / activity feed run every pass; history rows
        # only every `history_snapshot_minutes` (default 60).
        snapshot_history = self._should_snapshot_history()
        loop = asyncio.get_running_loop()
        for g_idx, guild in enumerate(guilds):
            if g_idx > 0:
                # Light spacing between guild API calls.
                await asyncio.sleep(2.0)
            try:
                data = await loop.run_in_executor(None, lambda g=guild: albion_api.get_guild_stats(g["guild_id"], timeout=30.0))
                if data:
                    stats = albion_api.parse_guild_stats(data)
                    self.bot.db.upsert_guild(
                        stats["guild_id"], stats["guild_name"], stats["founder_name"], stats["founded"],
                        stats["kill_fame"], stats["death_fame"], stats["member_count"],
                        stats["alliance_id"], stats["alliance_name"], stats["alliance_tag"]
                    )
                    if snapshot_history:
                        self.bot.db.insert_guild_history(
                            stats["guild_id"], stats["kill_fame"], stats["death_fame"], stats["member_count"]
                        )
                    info_log(f"Synced guild: {guild['guild_name']}")
            except Exception as exc:  # noqa: BLE001
                error_log(f"sync_guilds: error syncing guild {guild.get('guild_name')!r}: {exc!r}")

        # Sync registered player stats
        all_profiles = self.bot.db.fetch_all_registered_profiles()
        # Spread API calls under the Albion gameinfo soft limit (~180 req/min).
        # We aim for ~30 req/min (one per 2s) by default — 6× under the ceiling
        # but ~5× faster than the previous 15s cap. Tunable via settings:
        #   sync_player_delay_floor   (default 1.0s)
        #   sync_player_delay_ceiling (default 4.0s)
        #   sync_player_total_budget  (default 1200s = 20 min target for full pass)
        try:
            floor = float(self.bot.db.get_config("sync_player_delay_floor") or 1.0)
        except (TypeError, ValueError):
            floor = 1.0
        try:
            ceiling = float(self.bot.db.get_config("sync_player_delay_ceiling") or 4.0)
        except (TypeError, ValueError):
            ceiling = 4.0
        try:
            budget = float(self.bot.db.get_config("sync_player_total_budget") or 1200.0)
        except (TypeError, ValueError):
            budget = 1200.0
        per_player_delay = 0.0
        if all_profiles:
            per_player_delay = max(floor, min(ceiling, budget / len(all_profiles)))
            info_log(
                f"Player sync: {len(all_profiles)} profile(s); "
                f"spacing {per_player_delay:.1f}s between API calls "
                f"(~{int(60 / per_player_delay)} req/min, safe under 180/min ceiling)."
            )
        # Track discord_ids we've reconciled this run so the final sweep can skip them.
        reconciled_ids: set[str] = set()
        bounty_cog = self.bot.get_cog("Bounties")
        enemy_bounty_active = False
        if bounty_cog and hasattr(bounty_cog, "has_active_enemy_kill_bounties"):
            try:
                enemy_bounty_active = bool(bounty_cog.has_active_enemy_kill_bounties())
            except Exception as exc:  # noqa: BLE001
                error_log(f"enemy bounty active check failed: {exc!r}")
        for idx, profile in enumerate(all_profiles):
          if idx > 0 and per_player_delay > 0:
            await asyncio.sleep(per_player_delay)
          try:
            old_guild_name = profile.get("guild_name")
            old_albion_name = profile.get("albion_name")

            player_data = await loop.run_in_executor(None, lambda p=profile: albion_api.get_player_stats(p["albion_player_id"], timeout=30.0))
            if player_data:
                player_stats = albion_api.parse_stats(player_data)

                # Only award activity points / milestones / activity-feed posts
                # for members currently in the home guild. Ex-members may still
                # be tracked for reconcile (Alumni / Alliance / Guest) but
                # their fame deltas should not feed the points economy.
                home_guild_name, _ = self._resolve_home_guild_and_founder()
                api_guild_name = (player_stats.get("guild_name") or "").strip()
                in_home_guild_now = bool(
                    home_guild_name and api_guild_name
                    and api_guild_name.lower() == home_guild_name.lower()
                )

                # Detect stat activity: any of these increasing means the player was active
                activity_increased = (
                    player_stats.get("kill_fame", 0) > (profile.get("kill_fame") or 0) or
                    player_stats.get("pve_total", 0) > (profile.get("pve_total") or 0) or
                    player_stats.get("gather_all", 0) > (profile.get("gather_all") or 0)
                )
                if activity_increased:
                    self.bot.db.execute(
                        'UPDATE user_profiles SET last_activity_date = CURRENT_TIMESTAMP WHERE discord_id = ?',
                        (profile["discord_id"],)
                    )

                # Award activity points based on positive fame deltas this cycle.
                # IMPORTANT: a NULL prior value (or unsynced profile) means
                # "we've never seen this player before". Treating it as 0 would
                # make every metric look like a record-breaking jump (awarding
                # the player's entire lifetime fame on first sync) and spam
                # hall-of-fame. Fall through with delta=0 in that case.
                first_sync = profile.get("last_updated") is None

                def _delta(metric: str) -> int:
                    if first_sync:
                        return 0
                    prior = profile.get(metric)
                    if prior is None:
                        return 0
                    return max(0, int(player_stats.get(metric, 0)) - int(prior))

                kill_delta   = _delta("kill_fame")
                pve_delta    = _delta("pve_total")
                gather_delta = _delta("gather_all")
                craft_delta  = _delta("crafting_fame")
                fish_delta   = _delta("fishing_fame")
                # Sub-metric deltas — used only to build a richer activity-feed
                # message; the points formula stays on the rolled-up totals so
                # we don't double-count.
                pve_zone_deltas = {
                    "Royal":     _delta("pve_royal"),
                    "Outlands":  _delta("pve_outlands"),
                    "Avalon":    _delta("pve_avalon"),
                    "Hellgate":  _delta("pve_hellgate"),
                    "Corrupted": _delta("pve_corrupted"),
                    "Mists":     _delta("pve_mists"),
                }
                gather_resource_deltas = {
                    "fiber": _delta("gather_fiber"),
                    "hide":  _delta("gather_hide"),
                    "ore":   _delta("gather_ore"),
                    "rock":  _delta("gather_rock"),
                    "wood":  _delta("gather_wood"),
                }
                farm_delta    = _delta("farming_fame")
                crystal_delta = _delta("crystal_league")

                # Enemy-kill bounties need real killboard events, not just
                # lifetime fame deltas. Keep this bounded: only check players
                # whose kill fame moved while at least one configured enemy
                # bounty is open/claimed.
                if kill_delta and enemy_bounty_active and bounty_cog:
                    try:
                        kill_events = await loop.run_in_executor(
                            None,
                            lambda pid=profile.get("albion_player_id"): albion_api.get_player_kills(
                                str(pid), limit=10, timeout=30.0,
                            ),
                        )
                        if kill_events and hasattr(bounty_cog, "maybe_auto_submit_enemy_kill_bounty"):
                            matched = await bounty_cog.maybe_auto_submit_enemy_kill_bounty(
                                profile, kill_events,
                            )
                            if matched and hasattr(bounty_cog, "has_active_enemy_kill_bounties"):
                                enemy_bounty_active = bool(bounty_cog.has_active_enemy_kill_bounties())
                    except Exception as exc:  # noqa: BLE001
                        error_log(
                            f"enemy bounty kill scan failed for "
                            f"{profile.get('albion_name') or profile.get('discord_id')}: {exc!r}"
                        )

                awarded = (
                    award_fame_points(
                        self.bot.db,
                        profile["discord_id"],
                        kill_delta=kill_delta,
                        pve_delta=pve_delta,
                        gather_delta=gather_delta,
                        craft_delta=craft_delta,
                        fish_delta=fish_delta,
                    )
                    if in_home_guild_now
                    else 0
                )
                if awarded:
                    info_log(f"Awarded {awarded} fame-delta points to {profile.get('albion_name') or profile['discord_id']}.")
                    # Build a rich activity-feed reason that breaks fame down
                    # by source: PvP kills, PvE per zone, gathering per
                    # resource, plus crafting/fishing/farming/crystal-league.
                    # If sub-metric deltas don't add up to the parent total
                    # (rare — Albion may aggregate before our sync sees it)
                    # we fall back to the parent value.
                    def _fmt(n: int) -> str:
                        n = int(n)
                        if n >= 1_000_000:
                            return f"{n / 1_000_000:.1f}M"
                        if n >= 1_000:
                            return f"{n / 1_000:.1f}K"
                        return f"{n:,}"

                    main_parts: list[str] = []
                    notables: list[str] = []
                    if kill_delta:
                        tone = "PvP spike" if kill_delta >= 250_000 else "PvP activity"
                        main_parts.append(f"⚔️ **{tone}:** +{_fmt(kill_delta)} kill fame")
                    if pve_delta:
                        zone_values = [(name, v) for name, v in pve_zone_deltas.items() if v]
                        zone_values.sort(key=lambda item: item[1], reverse=True)
                        zone_bits = [f"{name} +{_fmt(v)}" for name, v in zone_values[:3]]
                        if zone_bits:
                            main_parts.append(
                                f"🗺️ **Expedition gains:** +{_fmt(pve_delta)} · top: {zone_bits[0]}"
                            )
                            if len(zone_bits) > 1:
                                notables.append(f"Route notes: {', '.join(zone_bits)}")
                        else:
                            main_parts.append(f"🗺️ **Expedition gains:** +{_fmt(pve_delta)}")
                    if gather_delta:
                        res_values = [
                            (name, v) for name, v in gather_resource_deltas.items() if v
                        ]
                        res_values.sort(key=lambda item: item[1], reverse=True)
                        res_bits = [f"{name} +{_fmt(v)}" for name, v in res_values[:3]]
                        if res_bits:
                            main_parts.append(
                                f"⛏️ **Gathering haul:** +{_fmt(gather_delta)} · top: {res_bits[0]}"
                            )
                            if len(res_bits) > 1:
                                notables.append(f"Stores manifest: {', '.join(res_bits)}")
                        else:
                            main_parts.append(f"⛏️ **Gathering haul:** +{_fmt(gather_delta)}")
                    if craft_delta:
                        main_parts.append(f"🔨 **Crafting push:** +{_fmt(craft_delta)} fame")
                    if fish_delta:
                        main_parts.append(f"🎣 **Fishing gains:** +{_fmt(fish_delta)} fame")
                    if farm_delta >= 10_000:
                        notables.append(f"🌾 Farming +{_fmt(farm_delta)}")
                    if crystal_delta >= 10_000:
                        notables.append(f"💎 Crystal League +{_fmt(crystal_delta)}")
                    # Streak: append "🔥 N-day streak" when this UTC day's
                    # activity extends or starts the run. Resets after a
                    # missed day. Highlights at milestone days (3,7,14,…).
                    try:
                        today_iso = utc_now_naive().strftime("%Y-%m-%d")
                        streak_info = self.bot.db.update_activity_streak(
                            profile["discord_id"], today_iso,
                        )
                        s = int(streak_info.get("streak") or 0)
                        milestone = streak_info.get("milestone")
                        if milestone:
                            notables.append(f"🔥 Banner held for **{milestone} active days**")
                        elif streak_info.get("freeze_used") and s >= 2:
                            notables.append(f"❄️ {s}-day streak, one quiet day covered")
                        elif streak_info.get("extended") and s >= 2:
                            notables.append(f"🔥 Banner held for {s} active days")
                        elif streak_info.get("started"):
                            notables.append("🌱 the banner is moving again")
                    except Exception as exc:  # noqa: BLE001
                        error_log(f"streak update failed: {exc!r}")
                    # Personal bests: per-metric best single-sync delta. Only
                    # announces beats over a prior recorded PB (skips first
                    # PB per metric so new players aren't spammed).
                    try:
                        pb_labels = {
                            "kill":   "PvP",
                            "pve":    "PvE",
                            "gather": "Gather",
                            "craft":  "Craft",
                            "fish":   "Fish",
                        }
                        new_bests = self.bot.db.check_personal_bests(
                            profile["discord_id"],
                            {
                                "kill":   kill_delta,
                                "pve":    pve_delta,
                                "gather": gather_delta,
                                "craft":  craft_delta,
                                "fish":   fish_delta,
                            },
                        )
                        for pb in new_bests:
                            label = pb_labels.get(pb["metric"], pb["metric"])
                            notables.append(
                                f"🏆 New personal mark: {label} {_fmt(pb['current'])} "
                                f"(was {_fmt(pb['prior'])})"
                            )
                    except Exception as exc:  # noqa: BLE001
                        error_log(f"personal-best check failed: {exc!r}")
                    # Item-power milestones: announce the first time the
                    # player's Avg IP crosses each threshold (1100, 1200,
                    # 1300, 1400, 1500). Idempotent because once crossed,
                    # the prior baseline stays above the threshold.
                    try:
                        prior_ip = float(profile.get("average_item_power") or 0.0)
                        cur_ip   = float(player_stats.get("average_item_power") or 0.0)
                        for thr in (1100, 1200, 1300, 1400, 1500):
                            if prior_ip < thr <= cur_ip:
                                notables.append(f"📈 **IP {thr} unlocked**")
                    except Exception as exc:  # noqa: BLE001
                        error_log(f"item-power milestone check failed: {exc!r}")
                    if len(main_parts) > 3:
                        overflow = main_parts[3:]
                        main_parts = main_parts[:3]
                        notables.insert(0, "Side ledger: " + " · ".join(overflow))
                    dispatch_title, dispatch_line, dispatch_color = _activity_dispatch_style(
                        kill_delta=kill_delta,
                        pve_delta=pve_delta,
                        gather_delta=gather_delta,
                        craft_delta=craft_delta,
                        fish_delta=fish_delta,
                    )
                    reason_lines = [dispatch_line]
                    reason_lines.extend(main_parts or ["**Albion activity tracked**"])
                    if notables:
                        reason_lines.append("**Ledger notes:** " + " · ".join(notables[:4]))
                    reason = "\n".join(reason_lines)
                    feed_cfg = _activity_feed_config(self.bot.db)
                    major_fame = (
                        kill_delta >= feed_cfg["activity_feed_major_pvp_fame"]
                        or pve_delta >= feed_cfg["activity_feed_major_pve_fame"]
                        or gather_delta >= feed_cfg["activity_feed_major_gather_fame"]
                        or craft_delta >= feed_cfg["activity_feed_major_craft_fame"]
                        or fish_delta >= feed_cfg["activity_feed_major_fish_fame"]
                    )
                    major_notable = any(
                        token in note
                        for note in notables
                        for token in ("New personal mark", "IP ", "active days")
                    )
                    if awarded >= feed_cfg["activity_feed_min_points"] or major_fame or major_notable:
                        try:
                            await announce_points(
                                self.bot,
                                profile["discord_id"],
                                awarded,
                                reason,
                                title=dispatch_title,
                                color=dispatch_color,
                            )
                        except Exception as exc:  # noqa: BLE001
                            error_log(f"announce_points (fame deltas) failed: {exc!r}")
                    else:
                        info_log(
                            "Suppressed low-signal activity-feed post for "
                            f"{profile.get('albion_name') or profile['discord_id']} "
                            f"(+{awarded} pts; kill={kill_delta}, pve={pve_delta}, "
                            f"gather={gather_delta}, craft={craft_delta}, fish={fish_delta})."
                        )

                # Hall-of-fame milestone embeds for big jumps in any metric.
                # Skip on first observation (no prior baseline = nothing to
                # compare against; otherwise the very first sync posts every
                # member's lifetime totals as if they were earned this cycle).
                death_delta = _delta("death_fame")
                if not first_sync and in_home_guild_now:
                    try:
                        await check_fame_milestones(
                            self.bot, profile,
                            {
                                "kill_fame":     kill_delta,
                                "death_fame":    death_delta,
                                "pve_total":     pve_delta,
                                "gather_all":    gather_delta,
                                "crafting_fame": craft_delta,
                                "fishing_fame":  fish_delta,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        error_log(f"check_fame_milestones failed: {exc!r}")

                # Albion's player API sometimes returns an empty/null guild
                # for a member who is genuinely still in their guild
                # (commonly observed for hours-to-a-day after a fresh
                # join/rejoin while their public profile re-indexes).
                # When that happens, refuse to wipe a previously-known
                # non-empty guild — preserve the last-known value so the
                # lifecycle reconciler doesn't yank them out of Recruit/
                # Member/etc. just because the API is lagging.
                api_guild = (player_stats.get("guild_name") or "").strip()
                old_guild = (profile.get("guild_name") or "").strip()
                if not api_guild and old_guild:
                    player_stats["guild_name"] = old_guild
                    player_stats["guild_id"]   = profile.get("guild_id") or player_stats.get("guild_id")
                    info_log(
                        f"Preserving guild_name={old_guild!r} for "
                        f"{profile.get('albion_name')!r} (API returned empty)."
                    )

                try:
                    await self._maybe_alert_risk_watch(profile, player_stats)
                except Exception as exc:  # noqa: BLE001
                    error_log(
                        f"risk watch check failed for "
                        f"{profile.get('albion_name') or profile.get('discord_id')}: {exc!r}"
                    )

                self.bot.db.update_user_albion_info(
                    profile["discord_id"], profile["albion_player_id"],
                    player_stats.get("albion_name") or profile["albion_name"], player_stats
                )
                # Albion's player API sometimes returns AllianceName="BURNR"
                # (the tag) with empty AllianceTag. If we know the canonical
                # (name, tag) for this alliance from the home-alliance config
                # or a guild row, write it back so user_profiles stays
                # consistent with the guilds table and live data.
                try:
                    aid = (player_stats.get("alliance_id") or "").strip()
                    if aid:
                        canon_name = canon_tag = ""
                        home_id = (self.bot.db.get_config("home_alliance_id") or "").strip()
                        if aid == home_id:
                            canon_name = (self.bot.db.get_config("home_alliance_name") or "").strip()
                            canon_tag = (self.bot.db.get_config("home_alliance_tag") or "").strip()
                        if not (canon_name and canon_tag):
                            gid = (player_stats.get("guild_id") or "").strip()
                            if gid:
                                grow = self.bot.db.fetch_guild(gid)
                                if grow and (grow.get("alliance_id") or "") == aid:
                                    canon_name = (grow.get("alliance_name") or canon_name or "").strip()
                                    canon_tag = (grow.get("alliance_tag") or canon_tag or "").strip()
                        if canon_name and canon_tag:
                            cur_name = (player_stats.get("alliance_name") or "").strip()
                            cur_tag = (player_stats.get("alliance_tag") or "").strip()
                            if cur_name != canon_name or cur_tag != canon_tag:
                                self.bot.db.cursor.execute(
                                    "UPDATE user_profiles SET alliance_name = ?, alliance_tag = ? "
                                    "WHERE discord_id = ?",
                                    (canon_name, canon_tag, profile["discord_id"]),
                                )
                                self.bot.db.connection.commit()
                except Exception as exc:  # noqa: BLE001
                    error_log(f"alliance normalize failed: {exc!r}")
                if snapshot_history:
                    self.bot.db.insert_player_history(
                        profile["discord_id"],
                        player_stats.get("kill_fame", 0),
                        player_stats.get("death_fame", 0),
                        player_stats.get("pve_total", 0),
                        player_stats.get("gather_all", 0),
                        player_stats.get("crafting_fame", 0),
                        player_stats.get("average_item_power", 0.0)
                    )
                info_log(f"Synced player: {profile['albion_name']}")

                new_guild_name = player_stats.get("guild_name")
                new_albion_name = player_stats.get("albion_name", old_albion_name)

                # Refresh the in-memory profile dict with fresh API values so
                # _reconcile_member_state below sees the latest guild_id /
                # alliance_id, instead of the stale snapshot loaded at the
                # top of the loop. Without this, the alliance reconcile keeps
                # comparing against last-tick data and members in newly-joined
                # guilds get parked at Guest until the next sync.
                for k in (
                    "guild_id", "guild_name",
                    "alliance_id", "alliance_name", "alliance_tag",
                    "albion_name",
                ):
                    if k in player_stats and player_stats.get(k) is not None:
                        profile[k] = player_stats[k]
            else:
                # API failed — reconcile against the *stored* DB state so stale
                # nickname tags / TU role / Alumni demotions still get applied.
                new_guild_name = old_guild_name
                new_albion_name = old_albion_name

            await self._reconcile_member_state(profile, new_guild_name, new_albion_name)
            reconciled_ids.add(str(profile["discord_id"]))

            if player_data and (old_guild_name != new_guild_name or old_albion_name != new_albion_name):
                info_log(f"Updated roles/nick for {new_albion_name}: guild {old_guild_name} -> {new_guild_name}")
                # Anti-poach alert when an in-home-guild member moves elsewhere.
                try:
                    await check_anti_poach(
                        self.bot, profile, old_guild_name, new_guild_name,
                    )
                except Exception as exc:  # noqa: BLE001
                    error_log(f"check_anti_poach failed: {exc!r}")
          except Exception as exc:  # noqa: BLE001
            error_log(f"sync_guilds: error syncing profile {profile.get('albion_name') or profile.get('discord_id')!r}: {exc!r}")

        # Final sweep: any Discord member with a managed alliance nickname tag who wasn't
        # covered by a registered profile this run gets cleaned up. This catches
        # unregistered/de-registered users who still wear stale tags.
        await self._sweep_orphan_nickname_tags(reconciled_ids)

        # Auto-promote lifecycle roles based on time in the Discord server + activity
        await self._auto_promote_lifecycle()

        # Automation cleanups: archive past LFG events + drop orphan guilds.
        try:
            archive_completed_events(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"archive_completed_events failed: {exc!r}")
        try:
            cleanup_orphan_guilds(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"cleanup_orphan_guilds failed: {exc!r}")

        # Refresh any live graph messages
        await update_live_graphs(self.bot)

        # Rebalance staff slots in case the guild grew or shrank
        await rebalance_staff(self.bot)

        # Refresh the staff application board (slot counts / open vs full)
        await refresh_staff_board(self.bot)

    async def _send_inactive_status_dm(
        self,
        member: discord.Member,
        *,
        reason: str,
        vc_days: int,
        stat_days: int,
    ) -> None:
        reason_line = reason or "recent activity was below the guild's active-member threshold"
        try:
            await member.send(
                "Hey, this is an automated Home Guild activity notice.\n\n"
                "You were moved to **Inactive** because the bot did not see enough "
                "recent guild activity from you.\n"
                f"Reason: **{reason_line}**.\n\n"
                "Inactive keeps you registered, but lowers your Discord visibility "
                "closer to Guest-level access until you are active again.\n\n"
                "How to recover:\n"
                f"• Join guild voice/content again. The VC threshold is **{vc_days} day(s)**.\n"
                f"• Get Albion stat movement again if your stats are stale. The stat threshold is **{stat_days} day(s)**.\n"
                "• After the next bot sync, your active guild status can be restored automatically.\n\n"
                "If you think this was a mistake, message an officer and they can recheck you."
            )
            info_log(f"Sent Inactive status DM to {member} ({member.id}).")
        except discord.Forbidden:
            info_log(f"Could not DM Inactive status notice to {member} ({member.id}); DMs likely closed.")
        except discord.HTTPException as exc:
            error_log(f"Inactive status DM failed for {member} ({member.id}): {exc!r}")

    async def _auto_promote_lifecycle(self) -> int:
        """Run the time-in-server based lifecycle progression. Returns # changes applied."""
        now = utc_now_naive()
        vc_inactivity_days = _get_lifecycle_int_config(
            self.bot.db,
            CFG_LIFECYCLE_VC_INACTIVITY_DAYS,
            DEFAULT_LIFECYCLE_VC_INACTIVITY_DAYS,
        )
        stat_inactivity_days = _get_lifecycle_int_config(
            self.bot.db,
            CFG_LIFECYCLE_STAT_INACTIVITY_DAYS,
            DEFAULT_LIFECYCLE_STAT_INACTIVITY_DAYS,
        )
        inactivity_mode = (
            self.bot.db.get_config(CFG_LIFECYCLE_INACTIVITY_MODE)
            or DEFAULT_LIFECYCLE_INACTIVITY_MODE
        )
        probationary_days = int(self.bot.db.get_config("probationary_days") or 30)
        member_days       = int(self.bot.db.get_config("member_days")       or 90)
        changed = 0
        for profile in self.bot.db.fetch_all_registered_with_verified_date():
            current_role = profile.get("lifecycle_role")
            if current_role not in _AUTO_LIFECYCLE:
                continue  # Officer-managed role (Alumni) — skip

            # Find this user's Discord member object so we can read joined_at
            discord_member = None
            for dg in self.bot.guilds:
                discord_member = dg.get_member(int(profile["discord_id"]))
                if discord_member:
                    break
            if not discord_member:
                continue  # User left the server

            if discord_member.joined_at:
                since_iso = discord_member.joined_at.replace(tzinfo=None).isoformat()
            else:
                since_iso = profile.get("verified_date")

            baseline = _parse_lifecycle_activity_dt(since_iso)
            inactive, inactive_reason = _lifecycle_inactivity_state(
                self.bot.db,
                profile,
                baseline=baseline,
                now=now,
                vc_days=vc_inactivity_days,
                stat_days=stat_inactivity_days,
                mode=inactivity_mode,
            )

            # Members on an active Leave of Absence are never auto-demoted
            # to Inactive. Their LOA naturally expires by date.
            loa_until_str = profile.get("loa_until") or ""
            on_loa = bool(loa_until_str) and loa_until_str >= now.date().isoformat()
            if on_loa:
                inactive = False

            if inactive and current_role != "Inactive":
                target_role = "Inactive"
            elif not inactive:
                target_role = derive_lifecycle(since_iso, probationary_days, member_days)
                # Recruit (in-game confirmed) should never be downgraded to Probationary.
                if current_role == "Recruit" and target_role == "Probationary":
                    continue
            else:
                continue

            if current_role == target_role:
                continue

            role_swap_ok = True
            for discord_guild in self.bot.guilds:
                member = discord_guild.get_member(int(profile["discord_id"]))
                if not member:
                    continue
                old_role = discord.utils.get(discord_guild.roles, name=current_role)
                new_role = discord.utils.get(discord_guild.roles, name=target_role)
                tu_role = discord.utils.get(discord_guild.roles, name="HomeGuild")
                home_guild = (self.bot.db.get_config("home_guild_name") or "HomeGuild").strip()
                in_home_guild = (profile.get("guild_name") or "").strip().lower() == home_guild.lower()
                try:
                    if old_role and old_role in member.roles:
                        await member.remove_roles(old_role)
                    if target_role in {"Inactive", "Alumni"} and tu_role and tu_role in member.roles:
                        await member.remove_roles(tu_role, reason=f"Lifecycle -> {target_role}")
                    elif in_home_guild and tu_role and tu_role not in member.roles:
                        await member.add_roles(tu_role, reason=f"Lifecycle -> {target_role}")
                    if new_role:
                        await member.add_roles(new_role)
                except discord.Forbidden:
                    role_swap_ok = False
                    error_log(
                        f"Cannot promote {member} ({member.id}): {current_role} -> {target_role}. "
                        f"Bot lacks permission. Move UnionBot's role ABOVE '{target_role}' "
                        f"and '{current_role}' in Server Settings → Roles."
                    )
                except discord.HTTPException as exc:
                    role_swap_ok = False
                    error_log(f"HTTP error promoting {member}: {exc}")
            if not role_swap_ok:
                # Don't update DB — leave it stale so next cycle retries after the
                # admin fixes role hierarchy.
                continue
            self.bot.db.set_lifecycle_role(profile["discord_id"], target_role)
            suffix = f" ({inactive_reason})" if target_role == "Inactive" and inactive_reason else ""
            info_log(f"Lifecycle update for {profile['discord_id']}: {current_role} -> {target_role}{suffix}")
            if target_role == "Inactive" and current_role != "Inactive":
                await self._send_inactive_status_dm(
                    discord_member,
                    reason=inactive_reason,
                    vc_days=vc_inactivity_days,
                    stat_days=stat_inactivity_days,
                )
            changed += 1
        return changed

    # ──────────────────────────────────────────────────────────────────────
    # Reconciliation helpers
    # ──────────────────────────────────────────────────────────────────────

    _TU_EARNED = ("Recruit", "Member", "Veteran")

    # System / managed roles that must NEVER be treated as an "external guild
    # role" by the auto-create-on-the-fly logic, even if a Discord guild
    # happens to have a role with one of these names.
    _PROTECTED_ROLE_NAMES = frozenset({
        "Verified", "Unverified", "Synced", "NotSynced",
        "HomeGuild", "Guild Leader",
        "Recruit", "Probationary", "Member", "Veteran", "Inactive", "Alumni", "Alliance", "Guest",
        "Captain", "Officer", "Steward", "Senior Shotcaller", "Shotcaller", "Recruiter",
        "@everyone",
    })

    def _resolve_home_guild_and_founder(self) -> tuple[str, str]:
        """Return (home_guild_name, founder_name_lower) for the configured home guild."""
        from cogs.users_profile import _resolve_home_guild
        home_guild = _resolve_home_guild(self.bot.db)
        founder = ""
        for g in (self.bot.db.fetch_all_guilds() or []):
            if (g.get("guild_name") or "").strip().lower() == home_guild.lower():
                founder = (g.get("founder_name") or "").strip().lower()
                break
        return home_guild, founder

    def _resolve_home_alliance(self) -> tuple[str | None, str | None]:
        """Return (home_alliance_id, home_alliance_name) auto-detected from the
        home guild's tracked record. Returns (None, None) if the home guild is
        not in any alliance.

        Side-effect: caches the detected alliance in the settings table under
        ``home_alliance_id`` / ``home_alliance_name`` and emits an info log
        whenever the home guild changes alliance affiliation.
        """
        home_guild, _ = self._resolve_home_guild_and_founder()
        for g in (self.bot.db.fetch_all_guilds() or []):
            if (g.get("guild_name") or "").strip().lower() != home_guild.lower():
                continue
            new_id = (g.get("alliance_id") or "").strip() or None
            new_name = (g.get("alliance_name") or "").strip() or None
            new_tag = (g.get("alliance_tag") or "").strip() or None
            prev_id = (self.bot.db.get_config("home_alliance_id") or "").strip() or None
            prev_name = (self.bot.db.get_config("home_alliance_name") or "").strip() or None
            prev_tag = (self.bot.db.get_config("home_alliance_tag") or "").strip() or None
            if new_id != prev_id or new_name != prev_name or new_tag != prev_tag:
                self.bot.db.set_config("home_alliance_id", new_id or "")
                self.bot.db.set_config("home_alliance_name", new_name or "")
                self.bot.db.set_config("home_alliance_tag", new_tag or "")
                if new_id and not prev_id:
                    info_log(
                        f"Home alliance detected: {home_guild} joined "
                        f"alliance {new_name!r} ({new_id})."
                    )
                elif prev_id and not new_id:
                    info_log(
                        f"Home alliance lost: {home_guild} left "
                        f"alliance {prev_name!r} ({prev_id})."
                    )
                elif new_id and prev_id and new_id != prev_id:
                    info_log(
                        f"Home alliance changed: {home_guild} moved from "
                        f"{prev_name!r} ({prev_id}) to {new_name!r} ({new_id})."
                    )
            return new_id, new_name
        return None, None

    def _known_external_guild_names(self) -> set[str]:
        """Set of in-game guild names (lowercased) currently observed across all
        registered profiles, excluding the home guild. Used to decide which
        Discord roles on a member are "guild-tag" roles managed by us.
        """
        home_guild, _ = self._resolve_home_guild_and_founder()
        names: set[str] = set()
        for p in (self.bot.db.fetch_all_registered_profiles() or []):
            g = (p.get("guild_name") or "").strip()
            if g and g.lower() != home_guild.lower():
                names.add(g.lower())
        return names

    async def _sync_external_guild_role(
        self,
        discord_guild: discord.Guild,
        member: discord.Member,
        current_guild_name: str | None,
        home_guild: str,
    ) -> None:
        """Ensure ``member`` has a Discord role matching their in-game guild
        (auto-creating it if needed) and strip any other auto-created guild
        roles. No-op when the member is in the home guild — that's covered by
        the dedicated HomeGuild role.
        """
        gname = (current_guild_name or "").strip()
        in_home = gname and gname.lower() == home_guild.lower()
        # The role we WANT them to have (None if they're guildless or in home).
        desired_role: discord.Role | None = None
        if gname and not in_home and gname not in self._PROTECTED_ROLE_NAMES:
            desired_role = discord.utils.get(discord_guild.roles, name=gname)
            if desired_role is None:
                try:
                    desired_role = await discord_guild.create_role(
                        name=gname,
                        mentionable=False,
                        reason=f"Auto-created for in-game guild {gname}",
                    )
                    info_log(f"Created guild role: {gname}")
                except discord.Forbidden:
                    error_log(
                        f"Cannot create role '{gname}'. Bot needs Manage Roles permission."
                    )
                    return
                except discord.HTTPException as exc:
                    error_log(f"HTTP error creating role '{gname}': {exc}")
                    return

        # Strip any *other* external-guild roles this member has.
        known = self._known_external_guild_names()
        for r in list(member.roles):
            if r.name in self._PROTECTED_ROLE_NAMES:
                continue
            if desired_role and r.id == desired_role.id:
                continue
            if r.name.lower() in known:
                try:
                    await member.remove_roles(r, reason="Reconcile: no longer in this in-game guild")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Add the desired role if missing.
        if desired_role and desired_role not in member.roles:
            try:
                await member.add_roles(desired_role, reason=f"In-game guild: {gname}")
            except discord.Forbidden:
                error_log(
                    f"Cannot assign '{gname}' to {member}. Move UnionBot's role ABOVE '{gname}' "
                    f"in Server Settings → Roles."
                )
            except discord.HTTPException as exc:
                error_log(f"HTTP error assigning '{gname}' to {member}: {exc}")

    async def _hydrate_profile_alliance_from_guild(self, profile: dict) -> None:
        """Fill missing alliance fields from the member's current Albion guild."""
        if (profile.get("alliance_id") or "").strip() and (
            profile.get("alliance_tag") or profile.get("alliance_name")
        ):
            return
        player_guild_id = (profile.get("guild_id") or "").strip()
        if not player_guild_id:
            return

        guild_row = self.bot.db.fetch_guild(player_guild_id)
        if not guild_row:
            try:
                raw = await asyncio.to_thread(
                    albion_api.get_guild_stats,
                    player_guild_id,
                    "americas",
                    30.0,
                )
                if raw:
                    gstats = albion_api.parse_guild_stats(raw)
                    if gstats.get("guild_id"):
                        self.bot.db.upsert_guild(
                            gstats["guild_id"], gstats["guild_name"],
                            gstats["founder_name"], gstats["founded"],
                            gstats["kill_fame"], gstats["death_fame"],
                            gstats["member_count"],
                            gstats["alliance_id"], gstats["alliance_name"],
                            gstats["alliance_tag"],
                        )
                        guild_row = self.bot.db.fetch_guild(player_guild_id)
                        info_log(
                            f"Auto-discovered guild {gstats.get('guild_name')!r} "
                            f"({gstats.get('guild_id')}) alliance="
                            f"{gstats.get('alliance_name')!r} "
                            f"[{gstats.get('alliance_tag')!r}] "
                            f"id={gstats.get('alliance_id')!r}"
                        )
            except Exception as exc:
                error_log(f"Auto-discover guild {player_guild_id} failed: {exc}")
                return

        if not guild_row:
            return

        alliance_id = (guild_row.get("alliance_id") or "").strip()
        alliance_name = (guild_row.get("alliance_name") or "").strip()
        alliance_tag = (guild_row.get("alliance_tag") or "").strip()
        if not (alliance_id or alliance_name or alliance_tag):
            return

        if alliance_id and not (profile.get("alliance_id") or "").strip():
            profile["alliance_id"] = alliance_id
        if alliance_name and not (profile.get("alliance_name") or "").strip():
            profile["alliance_name"] = alliance_name
        if alliance_tag and not (profile.get("alliance_tag") or "").strip():
            profile["alliance_tag"] = alliance_tag
        try:
            self.bot.db.execute(
                "UPDATE user_profiles SET "
                "alliance_id = COALESCE(NULLIF(alliance_id, ''), ?), "
                "alliance_name = COALESCE(NULLIF(alliance_name, ''), ?), "
                "alliance_tag = COALESCE(NULLIF(alliance_tag, ''), ?) "
                "WHERE discord_id = ?",
                (
                    alliance_id,
                    alliance_name,
                    alliance_tag,
                    str(profile.get("discord_id") or ""),
                ),
                quiet=True,
            )
        except Exception:
            pass

    async def _reconcile_member_state(self, profile: dict, current_guild_name: str | None,
                                      current_albion_name: str | None) -> None:
        """Sync TU role, alliance nickname tag, Guild Leader role, and Alumni demotion for one profile.

        Runs every sync cycle (even on API failure, using stored DB state) so stale tags
        get cleaned up promptly.
        """
        home_guild, home_founder = self._resolve_home_guild_and_founder()
        in_home_guild = (current_guild_name or "").strip().lower() == home_guild.lower()
        is_founder = bool(home_founder) and (current_albion_name or "").strip().lower() == home_founder
        effective_in_home = in_home_guild or is_founder
        current_lifecycle = profile.get("lifecycle_role")

        for discord_guild in self.bot.guilds:
            member = discord_guild.get_member(int(profile["discord_id"]))
            if not member:
                continue
            verified_role = discord.utils.get(discord_guild.roles, name="Verified")
            if not verified_role or verified_role not in member.roles:
                continue  # Only update verified (registered) members

            # Guild Leader Discord role — only the in-game founder.
            leader_role = discord.utils.get(discord_guild.roles, name="Guild Leader")
            if leader_role:
                if is_founder and leader_role not in member.roles:
                    try:
                        await member.add_roles(leader_role, reason="In-game guild founder")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                elif not is_founder and leader_role in member.roles:
                    try:
                        await member.remove_roles(leader_role, reason="No longer in-game guild founder")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            # HomeGuild role.
            tu_role = discord.utils.get(discord_guild.roles, name="HomeGuild")
            if tu_role:
                try:
                    dormant_lifecycle = current_lifecycle in {"Inactive", "Alumni"}
                    if effective_in_home and not dormant_lifecycle and tu_role not in member.roles:
                        await member.add_roles(tu_role)
                    elif (not effective_in_home or dormant_lifecycle) and tu_role in member.roles:
                        await member.remove_roles(tu_role)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            # Per-guild role: auto-create a Discord role matching the in-game
            # guild name and assign it to the member (only for non-home guilds —
            # the home guild has its own dedicated HomeGuild role).
            await self._sync_external_guild_role(
                discord_guild, member, current_guild_name, home_guild
            )
            await self._hydrate_profile_alliance_from_guild(profile)

            # Nickname:
            #   in home guild          -> [TU] <name> by default
            #   in home alliance       -> [UOT] <name> / alliance tag
            #   in another alliance    -> [<alliance_tag>] <name>
            #   otherwise              -> <name>
            if current_albion_name:
                self._resolve_home_alliance()
                desired_nick = tagged_nickname_for_profile(
                    self.bot.db,
                    current_albion_name,
                    profile,
                    home_member=effective_in_home,
                )
                if (member.nick or member.name) != desired_nick:
                    try:
                        await member.edit(nick=desired_nick)
                    except (discord.Forbidden, discord.HTTPException):
                        pass

            # Demote based on home-guild membership and history.
            #
            #   was_in_home_guild  + currently in home guild   → handled by _auto_promote_lifecycle
            #   was_in_home_guild  + NOT currently in home     → Alumni
            #   never in home      + NOT currently in home     → Guest
            #   founder                                         → exempt (handled above)
            #
            # `was_in_home_guild` is a sticky flag persisted on the profile.
            # Legacy profiles without the flag are backfilled here: anyone whose
            # current lifecycle is a TU-earned role (or Alumni) clearly used to
            # be in the guild.
            db_was_in_home = bool(profile.get("was_in_home_guild"))
            if not db_was_in_home and (
                current_lifecycle in self._TU_EARNED or current_lifecycle == "Alumni"
            ):
                self.bot.db.set_was_in_home_guild(profile["discord_id"], True)
                db_was_in_home = True

            if in_home_guild and not db_was_in_home:
                self.bot.db.set_was_in_home_guild(profile["discord_id"], True)
                db_was_in_home = True

            # Rescue stuck Guest/Alumni who are now (back) in the home guild.
            # Without this, a member who registered while the Albion API
            # returned GuildName=null (or who rejoined the guild after going
            # Alumni) would stay parked at Guest/Alumni forever, because
            # _auto_promote_lifecycle skips lifecycle roles outside
            # _AUTO_LIFECYCLE. Promote to Recruit; subsequent sync cycles
            # will progress them through Probationary/Member/Veteran via
            # tenure-based auto-promotion.
            if (
                effective_in_home
                and current_lifecycle in ("Guest", "Alliance", "Alumni")
            ):
                recruit_role = discord.utils.get(discord_guild.roles, name="Recruit")
                old_role = discord.utils.get(
                    discord_guild.roles, name=current_lifecycle,
                )
                rescue_ok = True
                try:
                    if old_role and old_role in member.roles:
                        await member.remove_roles(
                            old_role, reason="Rescue: now in home guild",
                        )
                    if recruit_role and recruit_role not in member.roles:
                        await member.add_roles(
                            recruit_role, reason="Rescue: now in home guild",
                        )
                except discord.Forbidden:
                    rescue_ok = False
                    error_log(
                        f"Cannot rescue {member} ({member.id}) from {current_lifecycle} "
                        f"to Recruit. Bot lacks permission. Move UnionBot's role "
                        f"ABOVE 'Recruit' and '{current_lifecycle}' in Server "
                        f"Settings → Roles."
                    )
                except discord.HTTPException as exc:
                    rescue_ok = False
                    error_log(
                        f"HTTP error rescuing {member} from {current_lifecycle}: {exc}"
                    )
                if rescue_ok:
                    self.bot.db.set_lifecycle_role(profile["discord_id"], "Recruit")
                    info_log(
                        f"{profile.get('albion_name')} now in {home_guild} — "
                        f"rescued {current_lifecycle} -> Recruit."
                    )
                    current_lifecycle = "Recruit"
                    try:
                        from cogs.users_profile import post_new_member_shoutout
                        await post_new_member_shoutout(
                            self.bot, member,
                            lifecycle="Recruit", home_guild=home_guild,
                        )
                    except Exception as exc:  # noqa: BLE001
                        error_log(f"shout-out for {member} failed: {exc!r}")

            if not in_home_guild and not is_founder:
                await self._strip_home_guild_staff_roles(
                    discord_guild,
                    member,
                    profile,
                    current_guild_name=current_guild_name,
                    home_guild=home_guild,
                )

                # Decide between Alumni / Alliance / Guest.
                # was_in_home_guild always wins → Alumni (sticky history flag).
                # Otherwise: alliance match → Alliance, no match → Guest.
                if db_was_in_home:
                    target_role = "Alumni"
                else:
                    home_alliance_id, _ = self._resolve_home_alliance()
                    member_alliance_id = (profile.get("alliance_id") or "").strip() or None
                    member_alliance_name = (profile.get("alliance_name") or "").strip() or None
                    member_alliance_tag = (profile.get("alliance_tag") or "").strip() or None
                    # Fallback: Albion's player API sometimes returns an empty
                    # AllianceId even when the player's guild is in an alliance.
                    # Look the player's current guild up in our tracked guilds
                    # table and use that guild's alliance_id instead.
                    player_guild_id = profile.get("guild_id")
                    player_guild_name = profile.get("guild_name")
                    if not member_alliance_id and player_guild_id:
                        g = self.bot.db.fetch_guild(player_guild_id)
                        # Auto-discover: if the player's guild isn't in our DB
                        # yet, fetch it from the Albion API and store it so we
                        # can resolve its alliance now and on future syncs.
                        if not g:
                            try:
                                raw = await asyncio.to_thread(
                                    albion_api.get_guild_stats, player_guild_id, "americas", 30.0,
                                )
                                if raw:
                                    gstats = albion_api.parse_guild_stats(raw)
                                    if gstats.get("guild_id"):
                                        self.bot.db.upsert_guild(
                                            gstats["guild_id"], gstats["guild_name"],
                                            gstats["founder_name"], gstats["founded"],
                                            gstats["kill_fame"], gstats["death_fame"],
                                            gstats["member_count"],
                                            gstats["alliance_id"], gstats["alliance_name"],
                                            gstats["alliance_tag"],
                                        )
                                        info_log(
                                            f"Auto-discovered guild {gstats.get('guild_name')!r} "
                                            f"({gstats.get('guild_id')}) alliance="
                                            f"{gstats.get('alliance_name')!r} "
                                            f"[{gstats.get('alliance_tag')!r}] "
                                            f"id={gstats.get('alliance_id')!r}"
                                        )
                                        g = self.bot.db.fetch_guild(player_guild_id)
                            except Exception as exc:
                                error_log(
                                    f"Auto-discover guild {player_guild_id} failed: {exc}"
                                )
                        if g:
                            member_alliance_id = (g.get("alliance_id") or "").strip() or None
                            if not member_alliance_name:
                                member_alliance_name = (g.get("alliance_name") or "").strip() or None
                            if not member_alliance_tag:
                                member_alliance_tag = (g.get("alliance_tag") or "").strip() or None
                    if home_alliance_id and member_alliance_id == home_alliance_id:
                        target_role = "Alliance"
                    else:
                        target_role = "Guest"
                    info_log(
                        f"Reconcile alliance check {profile.get('albion_name')!r} "
                        f"guild={player_guild_name!r} "
                        f"alliance={member_alliance_name!r} [{member_alliance_tag!r}] "
                        f"member_alliance_id={member_alliance_id!r} "
                        f"home_alliance_id={home_alliance_id!r} -> {target_role}"
                    )
                # Always run the role swap, even if DB lifecycle already
                # matches target_role. This backfills the case where a prior
                # tick set the DB but failed to apply the Discord role (e.g.
                # role hadn't been created yet, transient permission glitch,
                # or out-of-band DB edit). Idempotent: no-op when the role
                # is already present.
                target_role_obj = discord.utils.get(discord_guild.roles, name=target_role)
                lifecycle_changed = current_lifecycle != target_role
                needs_add = bool(target_role_obj and target_role_obj not in member.roles)
                # Find any other lifecycle role that shouldn't be on this
                # member and strip it. This catches stale Recruit/Member/etc.
                # roles when someone is approved as Guest or leaves the home
                # guild/alliance path.
                stale_external = [
                    r for r in member.roles
                    if r.name in LIFECYCLE_ROLES
                    and r.name != target_role
                ]
                if lifecycle_changed or needs_add or stale_external:
                    role_swap_ok = True
                    try:
                        for r in stale_external:
                            await member.remove_roles(r, reason=f"Reconcile → {target_role}")
                        if needs_add and target_role_obj is not None:
                            await member.add_roles(target_role_obj, reason=f"Reconcile → {target_role}")
                    except discord.Forbidden:
                        role_swap_ok = False
                        error_log(
                            f"Cannot move {member} ({member.id}) to {target_role}. Bot lacks permission. "
                            f"Move UnionBot's role ABOVE '{target_role}'"
                            + (f" and '{current_lifecycle}'" if current_lifecycle else "")
                            + " in Server Settings → Roles."
                        )
                    except discord.HTTPException as exc:
                        role_swap_ok = False
                        error_log(f"HTTP error moving {member} to {target_role}: {exc}")
                    if role_swap_ok and lifecycle_changed:
                        self.bot.db.set_lifecycle_role(profile["discord_id"], target_role)
                        info_log(
                            f"{profile.get('albion_name')} not in {home_guild} \u2014 "
                            f"moved {current_lifecycle or 'None'} -> {target_role}."
                        )

    async def _sweep_orphan_nickname_tags(self, already_reconciled: set[str]) -> None:
        """Strip leftover managed nickname tags / TU role from unregistered members.

        Catches members who:
          * have no registered profile but somehow got a managed nickname tag or TU role,
          * had their profile cleared but kept the visual artifacts.
        """
        for discord_guild in self.bot.guilds:
            tu_role = discord.utils.get(discord_guild.roles, name="HomeGuild")
            leader_role = discord.utils.get(discord_guild.roles, name="Guild Leader")
            for member in discord_guild.members:
                if member.bot or str(member.id) in already_reconciled:
                    continue
                # Never touch a Guild Leader — even if they're unregistered.
                if leader_role and leader_role in member.roles:
                    continue
                profile = self.bot.db.fetch_user_profile(str(member.id))
                if profile and profile.get("albion_player_id"):
                    continue
                stripped_nick = strip_managed_nickname_tag(self.bot.db, member.nick or "")
                has_managed_nick = stripped_nick is not None
                has_tu_role = bool(tu_role and tu_role in member.roles)
                if not has_managed_nick and not has_tu_role:
                    continue
                # Member wasn't picked up by the registered-profiles loop, so by
                # definition they are not currently in the home in-game guild.
                if has_tu_role:
                    try:
                        await member.remove_roles(tu_role, reason="Sweep: no active home-guild profile")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                if has_managed_nick:
                    try:
                        await member.edit(nick=stripped_nick, reason="Sweep: no active home-guild profile")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                info_log(f"Orphan nickname-tag sweep cleaned {member} ({member.id}).")

    async def _sweep_orphan_tu_tags(self, already_reconciled: set[str]) -> None:
        """Backward-compatible alias for older admin flows."""
        await self._sweep_orphan_nickname_tags(already_reconciled)

    @sync_guilds.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()

    @sync_guilds.error
    async def _sync_guilds_error(self, exc: BaseException):
        # Last-resort safety net: if the loop ever stops with an unhandled
        # exception (e.g. CancelledError during reconnect, surprise TypeError),
        # log and restart it so periodic syncing resumes after the bot is back.
        error_log(f"sync_guilds task crashed: {exc!r}; restarting loop.")
        try:
            self.sync_guilds.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart sync_guilds: {restart_exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Frequent guild-scan refresh. The scan file is pure local snapshot —
    # it touches the DB and the cached discord.py guild state, but makes no
    # API calls. Refreshing it every 5 minutes keeps debugging context
    # close to live without any rate-limit cost.
    # ──────────────────────────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def refresh_guild_scan(self):
        from cogs.lfg import write_guild_scan_file
        for guild in self.bot.guilds:
            try:
                write_guild_scan_file(guild, self.bot.db)
            except Exception as exc:  # noqa: BLE001
                error_log(f"refresh_guild_scan failed for {guild.name}: {exc!r}")

    @refresh_guild_scan.before_loop
    async def _before_refresh_guild_scan(self):
        await self.bot.wait_until_ready()

    @refresh_guild_scan.error
    async def _refresh_guild_scan_error(self, exc: BaseException):
        error_log(f"refresh_guild_scan task crashed: {exc!r}; restarting.")
        try:
            self.refresh_guild_scan.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart refresh_guild_scan: {restart_exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Daily retention sweep: prune player_stats_history / guild_stats_history
    # rows older than RETENTION_DAYS so the DB stays small and queries fast.
    # The latest row per player/guild is always preserved as a baseline.
    # ──────────────────────────────────────────────────────────────────────
    RETENTION_DAYS = 90

    @tasks.loop(hours=24)
    async def prune_history(self):
        try:
            deleted = self.bot.db.prune_stats_history(days=self.RETENTION_DAYS)
            if deleted["players"] or deleted["guilds"]:
                info_log(
                    f"Retention sweep: pruned {deleted['players']} player + "
                    f"{deleted['guilds']} guild history rows older than {self.RETENTION_DAYS}d."
                )
        except Exception as exc:  # noqa: BLE001
            error_log(f"prune_history loop error: {exc!r}")

    @prune_history.before_loop
    async def _before_prune(self):
        await self.bot.wait_until_ready()
        # Stagger so it doesn't fire at the same moment as the hourly sync.
        await asyncio.sleep(300)

    @prune_history.error
    async def _prune_history_error(self, exc: BaseException):
        error_log(f"prune_history task crashed: {exc!r}; restarting loop.")
        try:
            self.prune_history.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart prune_history: {restart_exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Daily SQLite backup. Copies data/database.db to data/backups/ with a
    # date stamp; keeps the last BACKUP_KEEP files. Uses the SQLite backup
    # API (via connection.iterdump or sqlite3.connect.backup) for a
    # consistent snapshot even if writes are happening concurrently.
    # ──────────────────────────────────────────────────────────────────────
    BACKUP_DIR = Path("data/backups")
    BACKUP_KEEP = 7

    @tasks.loop(hours=24)
    async def daily_backup(self):
        try:
            self.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            stamp = utc_now_naive().strftime("%Y%m%d-%H%M%S")
            dest = self.BACKUP_DIR / f"db-{stamp}.db"
            # Run blocking backup in a thread so the event loop isn't stalled.
            await asyncio.to_thread(self._do_sqlite_backup, dest)
            # Rotate: keep most recent BACKUP_KEEP files.
            backups = sorted(self.BACKUP_DIR.glob("db-*.db"))
            removed = 0
            while len(backups) > self.BACKUP_KEEP:
                old = backups.pop(0)
                try:
                    old.unlink()
                    removed += 1
                except OSError as exc:
                    error_log(f"daily_backup: failed to delete {old}: {exc!r}")
            size_mb = dest.stat().st_size / (1024 * 1024)
            info_log(
                f"DB backup: wrote {dest.name} ({size_mb:.2f} MB); "
                f"rotated {removed} old file(s); {len(backups)} kept."
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_backup error: {exc!r}")

    def _do_sqlite_backup(self, dest: Path) -> None:
        """Copy the live SQLite DB to ``dest`` using the backup API for a
        consistent, online snapshot. Synchronous — call via to_thread.

        Opens its own short-lived source connection inside this worker
        thread because SQLite connections cannot be shared across threads.
        Verifies integrity of the destination file before returning; on
        any failure the partial dest file is removed so a stale/corrupt
        backup is never left lying around.
        """
        import sqlite3
        try:
            with sqlite3.connect("data/database.db") as src_conn, \
                 sqlite3.connect(str(dest)) as bck:
                src_conn.backup(bck)
            # Verify the freshly-written backup. If the integrity check
            # doesn't return 'ok', treat the backup as failed.
            with sqlite3.connect(str(dest)) as verify:
                row = verify.execute("PRAGMA integrity_check").fetchone()
            if not row or (row[0] or "").lower() != "ok":
                raise sqlite3.DatabaseError(
                    f"integrity_check returned {row!r} for {dest.name}"
                )
        except (sqlite3.Error, OSError) as exc:
            # Don't keep half-written/corrupt backup around.
            try:
                if dest.exists():
                    dest.unlink()
            except OSError:
                pass
            raise RuntimeError(f"sqlite backup failed for {dest.name}: {exc!r}") from exc

    @daily_backup.before_loop
    async def _before_backup(self):
        await self.bot.wait_until_ready()
        # Stagger to avoid colliding with sync tick / prune.
        await asyncio.sleep(600)

    @daily_backup.error
    async def _daily_backup_error(self, exc: BaseException):
        error_log(f"daily_backup task crashed: {exc!r}; restarting loop.")
        try:
            self.daily_backup.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart daily_backup: {restart_exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Daily anniversary check. For each registered profile, compute days
    # since their Discord join and announce if today is a milestone
    # (30/90/180/365/yearly). Awards a small point bonus and posts to the
    # points-announce channel via announce_points().
    # ──────────────────────────────────────────────────────────────────────
    ANNIVERSARY_DAYS = (30, 90, 180, 365)
    ANNIVERSARY_BONUS = {30: 25, 90: 50, 180: 75, 365: 200}
    ANNIVERSARY_DEFAULT_BONUS = 100  # for 2nd+ year anniversaries

    @tasks.loop(hours=24)
    async def daily_anniversaries(self):
        try:
            today = utc_now_naive().date()
            announced = 0
            guild = self.bot.guilds[0] if self.bot.guilds else None
            if guild is None:
                return
            for member in guild.members:
                if member.bot or member.joined_at is None:
                    continue
                joined = member.joined_at.date()
                days = (today - joined).days
                if days <= 0:
                    continue
                # Match either an explicit milestone OR an exact-year anniversary (365, 730, 1095, ...)
                is_milestone = days in self.ANNIVERSARY_DAYS
                is_yearly = days % 365 == 0 and days >= 365
                if not (is_milestone or is_yearly):
                    continue
                # Have we already congratulated this member at this milestone?
                key = f"anniv:{member.id}:{days}"
                if self.bot.db.get_config(key):
                    continue
                self.bot.db.set_config(key, today.isoformat())
                # Give bonus points + announce
                bonus = self.ANNIVERSARY_BONUS.get(days, self.ANNIVERSARY_DEFAULT_BONUS)
                try:
                    self.bot.db.add_points(str(member.id), bonus)
                except Exception as exc:  # noqa: BLE001
                    error_log(f"anniversary points add failed for {member}: {exc!r}")
                # Pretty label
                if days >= 365 and days % 365 == 0:
                    years = days // 365
                    reason = f"{years}-year anniversary 🎂"
                else:
                    reason = f"{days}-day anniversary 🎉"
                try:
                    await announce_points(self.bot, member.id, bonus, reason)
                    announced += 1
                except Exception as exc:  # noqa: BLE001
                    error_log(f"anniversary announce failed for {member}: {exc!r}")
            if announced:
                info_log(f"Anniversary sweep: announced {announced} milestone(s).")
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_anniversaries error: {exc!r}")

    @daily_anniversaries.before_loop
    async def _before_anniversaries(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(900)  # stagger

    @daily_anniversaries.error
    async def _daily_anniversaries_error(self, exc: BaseException):
        error_log(f"daily_anniversaries task crashed: {exc!r}; restarting.")
        try:
            self.daily_anniversaries.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart daily_anniversaries: {restart_exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Weekly recap. Every Sunday 20:00 UTC, post a single embed summarizing
    # the past 7 days: top earners (points), biggest fame movers, and new
    # members. Posts to ``automation_announcements_channel_id`` if set.
    # Dedup via config key ``weekly_recap_last_iso_week`` so a restart on
    # the same day doesn't double-post.
    # ──────────────────────────────────────────────────────────────────────
    @tasks.loop(hours=1)
    async def weekly_recap(self):
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            # Sunday = weekday() 6, target hour 20:00 UTC
            if now.weekday() != 6 or now.hour != 20:
                return

            iso_year, iso_week, _ = now.isocalendar()
            stamp = f"{iso_year}-W{iso_week:02d}"
            if self.bot.db.get_config("weekly_recap_last_iso_week") == stamp:
                return  # already posted this week

            channel_id = self.bot.db.get_config("automation_announcements_channel_id")
            if not channel_id:
                return  # silently no-op if unconfigured
            channel = self.bot.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                return

            guild = channel.guild
            since = now - datetime.timedelta(days=7)
            since_iso = since.strftime("%Y-%m-%d %H:%M:%S")

            # ── Top point earners (week) ──────────────────────────────
            try:
                top = self.bot.db.top_points("week", limit=5)
            except Exception as exc:  # noqa: BLE001
                error_log(f"weekly_recap: top_points failed: {exc!r}")
                top = []
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            if top:
                lines = []
                for i, row in enumerate(top):
                    badge = medals.get(i, f"`#{i+1}`")
                    name = row.get("albion_name") or row.get("username") or row.get("discord_id")
                    pts = int(row.get("points") or 0)
                    lines.append(f"{badge} **{name}** — {pts:,} pts")
                top_block = "\n".join(lines)
            else:
                top_block = "_No points awarded this week._"

            # ── Biggest fame movers ──────────────────────────────────
            home_guild_name, _ = self._resolve_home_guild_and_founder()
            try:
                killers = self.bot.db.fetch_top_movers("kill_fame", since_iso, limit=3, home_guild=home_guild_name)
            except Exception as exc:  # noqa: BLE001
                error_log(f"weekly_recap: fetch_top_movers kill failed: {exc!r}")
                killers = []
            try:
                pvers = self.bot.db.fetch_top_movers("pve_total", since_iso, limit=3, home_guild=home_guild_name)
            except Exception as exc:  # noqa: BLE001
                error_log(f"weekly_recap: fetch_top_movers pve failed: {exc!r}")
                pvers = []

            def _fmt_movers(rows: list[dict]) -> str:
                if not rows:
                    return "_no activity_"
                return "\n".join(
                    f"• **{r.get('name') or r.get('discord_id')}** — {int(r.get('delta') or 0):,}"
                    for r in rows
                )

            # ── New members this week ────────────────────────────────
            new_member_lines = []
            if guild is not None:
                cutoff = now - datetime.timedelta(days=7)
                joined = []
                for m in guild.members:
                    if m.bot or m.joined_at is None:
                        continue
                    if m.joined_at >= cutoff:
                        joined.append(m)
                joined.sort(key=lambda x: x.joined_at or now, reverse=True)
                for m in joined[:5]:
                    new_member_lines.append(f"• {m.mention}")
                if len(joined) > 5:
                    new_member_lines.append(f"…and **{len(joined) - 5}** more")
            new_block = "\n".join(new_member_lines) or "_no new members_"

            # Pull announcement branding so the recap matches /announce post.
            from cogs.announcements import (
                CFG_COLOR_HEX, CFG_CREST_URL, CFG_FOOTER_NAME,
                DEFAULT_FOOTER, _parse_color,
            )
            db = self.bot.db
            color = _parse_color(db.get_config(CFG_COLOR_HEX))
            crest_url = (db.get_config(CFG_CREST_URL) or "").strip() or None
            footer_name = (db.get_config(CFG_FOOTER_NAME) or DEFAULT_FOOTER).strip()

            embed = discord.Embed(
                title="\ud83d\udcc5 Weekly Recap",
                description=(
                    f"The past 7 days in HomeGuild \u2014 "
                    f"<t:{int(since.timestamp())}:D> \u2192 <t:{int(now.timestamp())}:D>"
                ),
                color=color,
                timestamp=now,
            )
            if crest_url:
                embed.set_thumbnail(url=crest_url)
            embed.add_field(name="\ud83c\udfc6 Top Point Earners", value=top_block, inline=False)
            embed.add_field(name="\u2694\ufe0f Top Kill Fame", value=_fmt_movers(killers), inline=True)
            embed.add_field(name="\ud83d\udc17 Top PvE Fame",  value=_fmt_movers(pvers),   inline=True)
            embed.add_field(name="\ud83d\udc4b New Members",   value=new_block, inline=False)
            if crest_url:
                embed.set_footer(
                    text=f"{footer_name} \u00b7 Weekly Recap",
                    icon_url=crest_url,
                )
            else:
                embed.set_footer(text=f"{footer_name} \u00b7 Weekly Recap")

            # Ping the HomeGuild role if it exists in the guild so all
            # members see the recap in their notification feed.
            tu_role = (
                discord.utils.get(guild.roles, name="HomeGuild") if guild is not None else None
            )
            content = tu_role.mention if tu_role is not None else None
            allowed = discord.AllowedMentions(
                roles=[tu_role] if tu_role is not None else False,
                users=False, everyone=False,
            )

            try:
                await channel.send(
                    content=content,
                    embed=embed,
                    allowed_mentions=allowed,
                )
                self.bot.db.set_config("weekly_recap_last_iso_week", stamp)
                info_log(f"weekly_recap posted for {stamp}.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"weekly_recap send failed: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            error_log(f"weekly_recap error: {exc!r}")

    @weekly_recap.before_loop
    async def _before_weekly_recap(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)

    @weekly_recap.error
    async def _weekly_recap_error(self, exc: BaseException):
        error_log(f"weekly_recap task crashed: {exc!r}; restarting.")
        try:
            self.weekly_recap.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart weekly_recap: {restart_exc!r}")

    # ──────────────────────────────────────────────────────────────────────
    # Daily recap. Every day at 18:00 UTC, post a branded embed summarizing
    # the past 24h: top kill / PvE fame movers and new members. Skips post
    # entirely if there's no movement to report (no fame deltas + no new
    # members) to avoid spamming the announce channel with empty days.
    # Posts to ``automation_announcements_channel_id``.
    # Dedup via config key ``daily_recap_last_date``.
    # ──────────────────────────────────────────────────────────────────────
    @tasks.loop(hours=1)
    async def daily_recap(self):
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            if now.hour != 18:
                return

            stamp = now.strftime("%Y-%m-%d")
            if self.bot.db.get_config("daily_recap_last_date") == stamp:
                return  # already posted today

            channel_id = self.bot.db.get_config("automation_announcements_channel_id")
            if not channel_id:
                return
            channel = self.bot.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                return

            guild = channel.guild
            since = now - datetime.timedelta(days=1)
            since_iso = since.strftime("%Y-%m-%d %H:%M:%S")

            # Fame movers (24h)
            home_guild_name, _ = self._resolve_home_guild_and_founder()
            try:
                killers = self.bot.db.fetch_top_movers(
                    "kill_fame", since_iso, limit=3, home_guild=home_guild_name,
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"daily_recap: fetch_top_movers kill failed: {exc!r}")
                killers = []
            try:
                pvers = self.bot.db.fetch_top_movers(
                    "pve_total", since_iso, limit=3, home_guild=home_guild_name,
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"daily_recap: fetch_top_movers pve failed: {exc!r}")
                pvers = []

            def _fmt_movers(rows: list[dict]) -> str:
                if not rows:
                    return "_no activity_"
                return "\n".join(
                    f"\u2022 **{r.get('name') or r.get('discord_id')}** \u2014 {int(r.get('delta') or 0):,}"
                    for r in rows
                )

            # New members (24h)
            new_member_lines: list[str] = []
            new_member_count = 0
            if guild is not None:
                cutoff = now - datetime.timedelta(days=1)
                joined = [
                    m for m in guild.members
                    if not m.bot and m.joined_at is not None and m.joined_at >= cutoff
                ]
                joined.sort(key=lambda x: x.joined_at or now, reverse=True)
                new_member_count = len(joined)
                for m in joined[:5]:
                    new_member_lines.append(f"\u2022 {m.mention}")
                if new_member_count > 5:
                    new_member_lines.append(f"\u2026and **{new_member_count - 5}** more")
            new_block = "\n".join(new_member_lines) or "_no new members_"

            # Skip post entirely if nothing happened.
            if not killers and not pvers and new_member_count == 0:
                self.bot.db.set_config("daily_recap_last_date", stamp)
                info_log(f"daily_recap: no activity, skipped post for {stamp}.")
                return

            # Pull announcement branding so the recap matches /announce post.
            from cogs.announcements import (
                CFG_COLOR_HEX, CFG_CREST_URL, CFG_FOOTER_NAME,
                DEFAULT_FOOTER, _parse_color,
            )
            db = self.bot.db
            color = _parse_color(db.get_config(CFG_COLOR_HEX))
            crest_url = (db.get_config(CFG_CREST_URL) or "").strip() or None
            footer_name = (db.get_config(CFG_FOOTER_NAME) or DEFAULT_FOOTER).strip()

            embed = discord.Embed(
                title="\ud83c\udf05 Daily Recap",
                description=(
                    f"The past 24 hours in HomeGuild \u2014 "
                    f"<t:{int(since.timestamp())}:f> \u2192 <t:{int(now.timestamp())}:f>"
                ),
                color=color,
                timestamp=now,
            )
            if crest_url:
                embed.set_thumbnail(url=crest_url)
            embed.add_field(name="\u2694\ufe0f Top Kill Fame", value=_fmt_movers(killers), inline=True)
            embed.add_field(name="\ud83d\udc17 Top PvE Fame",  value=_fmt_movers(pvers),   inline=True)
            embed.add_field(name="\ud83d\udc4b New Members",   value=new_block, inline=False)
            if crest_url:
                embed.set_footer(
                    text=f"{footer_name} \u00b7 Daily Recap",
                    icon_url=crest_url,
                )
            else:
                embed.set_footer(text=f"{footer_name} \u00b7 Daily Recap")

            tu_role = (
                discord.utils.get(guild.roles, name="HomeGuild") if guild is not None else None
            )
            content = tu_role.mention if tu_role is not None else None
            allowed = discord.AllowedMentions(
                roles=[tu_role] if tu_role is not None else False,
                users=False, everyone=False,
            )

            try:
                await channel.send(
                    content=content,
                    embed=embed,
                    allowed_mentions=allowed,
                )
                self.bot.db.set_config("daily_recap_last_date", stamp)
                info_log(f"daily_recap posted for {stamp}.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"daily_recap send failed: {exc!r}")
        except Exception as exc:  # noqa: BLE001
            error_log(f"daily_recap error: {exc!r}")

    @daily_recap.before_loop
    async def _before_daily_recap(self):
        await self.bot.wait_until_ready()

    @daily_recap.error
    async def _daily_recap_error(self, exc: BaseException):
        error_log(f"daily_recap task crashed: {exc!r}; restarting.")
        try:
            self.daily_recap.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart daily_recap: {restart_exc!r}")

async def setup(bot):
    await bot.add_cog(Events(bot))
