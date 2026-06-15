"""Activity-points system.

Three rolling point windows live on every registered user_profiles row:
    points_weekly, points_monthly, points_season

Sources of points:
    • Chat messages          — award once per cooldown window when long enough
    • Voice presence         — award per tick to non-AFK humans in non-empty channels
    • Albion fame deltas     — awarded by cogs/events.py during the hourly sync
                               (kill / pve / gather / craft / fishing fame increases)

All point values, cooldowns, and fame ratios are runtime-tunable via
guild_config keys (defaults below). Officers manage them through the
``/points config …`` and ``/points admin …`` slash command groups.

Auto-reset:
    weekly  resets every Monday 00:00 UTC
    monthly resets on the 1st of each month 00:00 UTC
    season  is manual only (officer command)
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks

from debug import info_log, error_log, warning_log
from utils import error_embed, success_embed, info_embed, warning_embed
from cogs.users_profile import _resolve_home_guild


# ──────────────────────────────────────────────────────────────────────────────
# Tunable defaults — every key is also stored in `guild_config` once changed
# ──────────────────────────────────────────────────────────────────────────────
POINT_DEFAULTS: dict[str, int] = {
    # Chat
    "points_chat":              1,    # points awarded per qualifying message
    "points_chat_cooldown_sec": 60,   # min seconds between awards per user
    "points_chat_min_chars":    5,    # ignore very short messages

    # Voice
    "points_voice":             1,    # points per voice tick
    "points_voice_min_humans":  2,    # need this many non-bot humans in the channel
    "points_voice_tick_min":    5,    # tick frequency in minutes (changing requires restart)
    "points_voice_announce_min": 60,  # min minutes between public voice reward cards; 0 = silent
    "points_activity_digest_min": 60, # >0 = hourly digest; 0 = immediate legacy posts

    # Albion fame → points (1 point per N fame). 0 disables that source.
    "points_per_kill_fame":     10000,
    "points_per_pve_fame":      100000,
    "points_per_gather_fame":   50000,
    "points_per_craft_fame":    100000,
    "points_per_fish_fame":     100000,

    # Automation hooks (cogs/automation.py + cogs/regear.py call these).
    # Modest values — these compound across many members so keep them small.
    "points_event_attended":    50,
    "points_anniversary":       150,   # +50 per full year tenure
    "points_kill_milestone":    10,   # one-shot bonus when crossing a fame bucket
    "points_recruit_verified":  25,   # awarded when the Register button flow completes
    "points_regear_cost":       0,    # >0 deducts points on approval
}

# Settings keys that are user-tunable through /points config set.
TUNABLE_KEYS: tuple[str, ...] = tuple(POINT_DEFAULTS.keys())

_RESET_KEY_WEEKLY  = "points_last_weekly_reset"
_RESET_KEY_MONTHLY = "points_last_monthly_reset"

# Public channel to announce point awards (e.g. #🎉-rewards). Unset = silent.
POINTS_ANNOUNCE_CHANNEL_KEY = "points_announce_channel_id"
_VOICE_ANNOUNCE_LAST_KEY = "points_voice_last_announce_at"
_ACTIVITY_QUEUE_TABLE = "activity_feed_queue"
_HOF_QUEUE_TABLE = "hall_of_fame_digest_queue"


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _hour_start(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)


def _ensure_activity_queue(db) -> None:
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_ACTIVITY_QUEUE_TABLE} (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            amount     INTEGER NOT NULL,
            reason     TEXT,
            title      TEXT,
            color      INTEGER
        )
        """,
        quiet=True,
    )


def _ensure_hof_queue(db) -> None:
    db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_HOF_QUEUE_TABLE} (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            discord_id  TEXT NOT NULL,
            albion_name TEXT,
            metric_key  TEXT NOT NULL,
            label       TEXT NOT NULL,
            emoji       TEXT,
            delta       INTEGER NOT NULL,
            total       INTEGER
        )
        """,
        quiet=True,
    )


def _resolve_points_channel(bot: Bot) -> discord.TextChannel | None:
    db = getattr(bot, "db", None)
    if db is None:
        return None
    chan_id = db.get_config(POINTS_ANNOUNCE_CHANNEL_KEY)
    if not chan_id:
        return None
    try:
        chan_id_int = int(chan_id)
    except (TypeError, ValueError):
        error_log(f"points activity feed: bad channel id {chan_id!r}")
        return None
    guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return None
    channel = guild.get_channel(chan_id_int)
    return channel if isinstance(channel, discord.TextChannel) else None


def _clean_digest_text(text: str, *, limit: int = 180) -> str:
    clean = " ".join(str(text or "").replace("\n", " · ").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def _display_user(bot: Bot, user_id: str | int) -> str:
    uid = str(user_id)
    guild = bot.guilds[0] if bot.guilds else None
    if guild is not None:
        try:
            member = guild.get_member(int(uid))
        except (TypeError, ValueError):
            member = None
        if member is not None:
            return f"<@{uid}>"
    profile = getattr(bot, "db", None).fetch_user_profile(uid) if getattr(bot, "db", None) else None
    if profile:
        return str(profile.get("albion_name") or profile.get("username") or uid)
    return f"<@{uid}>"


def _queue_activity_announcement(
    bot: Bot,
    user_id: str | int,
    amount: int,
    reason: str,
    *,
    title: str,
    color: discord.Color | None,
) -> None:
    db = getattr(bot, "db", None)
    if db is None:
        return
    _ensure_activity_queue(db)
    color_value = int((color or discord.Color.gold()).value)
    db.execute(
        f"""
        INSERT INTO {_ACTIVITY_QUEUE_TABLE}
            (created_at, user_id, amount, reason, title, color)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _utc_now().isoformat(),
            str(user_id),
            int(amount),
            str(reason or ""),
            str(title or "🎉 Points awarded"),
            color_value,
        ),
        quiet=True,
    )


async def flush_activity_feed_digest(bot: Bot, *, force: bool = False) -> bool:
    """Post one compact activity-feed card for completed hourly windows.

    This is the one public activity card for the hour: point awards, voice
    rewards, and Hall of Fame milestones all collapse into sections here.
    """
    db = getattr(bot, "db", None)
    if db is None:
        return False
    channel = _resolve_points_channel(bot)
    if channel is None:
        return False
    _ensure_activity_queue(db)
    _ensure_hof_queue(db)
    interval_min = get_point_setting(db, "points_activity_digest_min")
    if interval_min <= 0 and not force:
        return False

    now = _utc_now()
    cutoff = now if force else _hour_start(now)
    try:
        db.cursor.execute(
            f"""
            SELECT id, created_at, user_id, amount, reason, title, color
            FROM {_ACTIVITY_QUEUE_TABLE}
            WHERE created_at < ?
            ORDER BY id ASC
            """,
            (cutoff.isoformat(),),
        )
        activity_rows = [dict(row) for row in db.cursor.fetchall()]
        db.cursor.execute(
            f"""
            SELECT id, created_at, discord_id, albion_name, metric_key, label, emoji, delta, total
            FROM {_HOF_QUEUE_TABLE}
            WHERE created_at < ?
            ORDER BY delta DESC, id ASC
            """,
            (cutoff.isoformat(),),
        )
        hof_rows = [dict(row) for row in db.cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001
        error_log(f"activity digest fetch failed: {exc!r}")
        return False
    if not activity_rows and not hof_rows:
        return False

    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    best_reason: dict[str, tuple[int, str]] = {}
    for row in activity_rows:
        uid = str(row["user_id"])
        amount = int(row["amount"] or 0)
        totals[uid] = totals.get(uid, 0) + amount
        counts[uid] = counts.get(uid, 0) + 1
        reason = _clean_digest_text(row.get("reason") or "Activity tracked.", limit=170)
        if uid not in best_reason or amount > best_reason[uid][0]:
            best_reason[uid] = (amount, reason)

    total_points = sum(totals.values())
    top_users = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:8]
    top_lines = []
    for uid, total in top_users:
        reason = best_reason.get(uid, (0, "Activity tracked."))[1]
        count = counts.get(uid, 0)
        count_text = f" across {count} marks" if count > 1 else ""
        top_lines.append(
            f"**+{total:,}** {_display_user(bot, uid)}{count_text} — {reason}"
        )
    if len(totals) > len(top_users):
        top_lines.append(f"_+{len(totals) - len(top_users)} more traveler(s) in the ledger._")

    hof_lines: list[str] = []
    total_hof_delta = 0
    for row in hof_rows[:8]:
        emoji = str(row.get("emoji") or "🏆")
        uid = str(row.get("discord_id") or "")
        name = str(row.get("albion_name") or uid)
        label = str(row.get("label") or "fame")
        delta = int(row.get("delta") or 0)
        total = int(row.get("total") or 0)
        total_hof_delta += delta
        total_text = f" · total **{total:,}**" if total > 0 else ""
        hof_lines.append(
            f"{emoji} {_display_user(bot, uid)} **{name}** — **+{delta:,}** {label}{total_text}"
        )
    if len(hof_rows) > len(hof_lines):
        total_hof_delta += sum(int(row.get("delta") or 0) for row in hof_rows[len(hof_lines):])
        hof_lines.append(f"_+{len(hof_rows) - len(hof_lines)} more milestone(s) entered the ledger._")

    all_times = [
        str(row.get("created_at") or "")
        for row in [*activity_rows, *hof_rows]
        if row.get("created_at")
    ]
    all_times.sort()
    window_start = (all_times[0] if all_times else cutoff.isoformat())[:16].replace("T", " ")
    window_end = (all_times[-1] if all_times else cutoff.isoformat())[:16].replace("T", " ")
    embed = discord.Embed(
        title="📜 S.I. Union Activity Dispatch",
        description=(
            "The hourly ledger is sealed by Scribe Intelligence. "
            f"**{len(activity_rows)}** activity mark{'s' if len(activity_rows) != 1 else ''}, "
            f"**{len(hof_rows)}** milestone{'s' if len(hof_rows) != 1 else ''}, "
            f"**+{total_points:,}** point{'s' if total_points != 1 else ''} logged."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Activity Ledger",
        value="\n".join(top_lines)[:1024] if top_lines else "No marks this hour.",
        inline=False,
    )
    if hof_lines:
        embed.add_field(
            name="Hall of Fame",
            value="\n".join(hof_lines)[:1024],
            inline=False,
        )
        embed.add_field(
            name="Fame Signal",
            value=f"**{total_hof_delta:,}** fame surged across the hour's milestones.",
            inline=False,
        )
    embed.set_footer(
        text=f"S.I. hourly dispatch · {window_start} to {window_end} UTC"
    )
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"activity digest send failed: {exc!r}")
        return False

    activity_ids = [int(row["id"]) for row in activity_rows]
    hof_ids = [int(row["id"]) for row in hof_rows]
    try:
        if activity_ids:
            placeholders = ",".join("?" for _ in activity_ids)
            db.execute(
                f"DELETE FROM {_ACTIVITY_QUEUE_TABLE} WHERE id IN ({placeholders})",
                tuple(activity_ids),
                quiet=True,
            )
        if hof_ids:
            placeholders = ",".join("?" for _ in hof_ids)
            db.execute(
                f"DELETE FROM {_HOF_QUEUE_TABLE} WHERE id IN ({placeholders})",
                tuple(hof_ids),
                quiet=True,
            )
    except Exception as exc:  # noqa: BLE001
        error_log(f"activity digest cleanup failed: {exc!r}")
    info_log(
        f"Posted hourly activity digest with {len(activity_rows)} activity row(s) "
        f"and {len(hof_rows)} milestone row(s)."
    )
    return True


async def announce_points(
    bot: Bot, user_id: str | int, amount: int, reason: str,
    *,
    title: str = "🎉 Points awarded",
    color: discord.Color | None = None,
) -> None:
    """Post a public 'X received N points' message to the configured channel.

    Silently no-ops if no channel is configured, the channel is missing, or
    Discord rejects the send. Safe to call from anywhere points are awarded.
    Skips zero / negative amounts (don't announce deductions).
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return
    if amount <= 0:
        return
    db = getattr(bot, "db", None)
    if db is None:
        return
    if not db.get_config(POINTS_ANNOUNCE_CHANNEL_KEY):
        info_log(f"announce_points: skipped (no channel set) user={user_id} +{amount} {reason!r}")
        return

    digest_min = get_point_setting(db, "points_activity_digest_min")
    if digest_min > 0:
        _queue_activity_announcement(
            bot,
            user_id,
            amount,
            reason,
            title=title,
            color=color or discord.Color.gold(),
        )
        info_log(
            f"announce_points: queued +{amount} for {user_id} "
            f"({reason!r}) for hourly digest."
        )
        return

    channel = _resolve_points_channel(bot)
    if channel is None:
        info_log(f"announce_points: skipped (no channel set) user={user_id} +{amount} {reason!r}")
        return
    clean_reason = str(reason or "").strip() or "Activity tracked."
    if "\n" in clean_reason or clean_reason.startswith("**"):
        description = f"<@{user_id}> received **+{amount:,}** points\n{clean_reason}"
    else:
        description = f"<@{user_id}> received **+{amount:,}** points\n*{clean_reason}*"
    embed = discord.Embed(
        title=title,
        description=description,
        color=color or discord.Color.gold(),
    )
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        info_log(f"announce_points: posted +{amount} for {user_id} ({reason!r}) in #{channel.name}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"announce_points failed: {exc!r}")


async def announce_points_bulk(
    bot: Bot, user_ids: list, amount: int, reason: str,
) -> None:
    """Post a single aggregated 'N members each received X points' card to the
    configured channel. Use for periodic high-volume awards (voice tick, etc.)
    where individual announcements would spam the feed.

    No-ops on empty list, zero amount, or unconfigured channel.
    """
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return
    if amount <= 0 or not user_ids:
        return
    db = getattr(bot, "db", None)
    if db is None:
        return
    if not db.get_config(POINTS_ANNOUNCE_CHANNEL_KEY):
        return
    digest_min = get_point_setting(db, "points_activity_digest_min")
    if digest_min > 0:
        for user_id in user_ids:
            _queue_activity_announcement(
                bot,
                user_id,
                amount,
                f"🔊 Voice activity reward · {reason}",
                title="🔊 Voice activity reward",
                color=discord.Color.blurple(),
            )
        info_log(
            f"announce_points_bulk: queued +{amount} for {len(user_ids)} member(s) "
            "for hourly activity digest."
        )
        return
    chan_id = db.get_config(POINTS_ANNOUNCE_CHANNEL_KEY)
    if not chan_id:
        return
    try:
        chan_id_int = int(chan_id)
    except (TypeError, ValueError):
        return
    guild = bot.guilds[0] if bot.guilds else None
    if guild is None:
        return
    channel = guild.get_channel(chan_id_int)
    if not isinstance(channel, discord.TextChannel):
        return
    # Mention up to 10; summarise the rest as "+N more".
    mention_cap = 10
    mentions = [f"<@{uid}>" for uid in user_ids[:mention_cap]]
    extra = len(user_ids) - len(mentions)
    member_list = ", ".join(mentions)
    if extra > 0:
        member_list += f" *and {extra} more*"
    embed = discord.Embed(
        title="🔊 Voice activity reward",
        description=(
            f"**+{amount:,}** point{'s' if amount != 1 else ''} each to "
            f"**{len(user_ids)}** member{'s' if len(user_ids) != 1 else ''}\n"
            f"*{reason}*\n\n{member_list}"
        ),
        color=discord.Color.blurple(),
    )
    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
        info_log(
            f"announce_points_bulk: posted +{amount} for {len(user_ids)} member(s) "
            f"({reason!r}) in #{channel.name}"
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"announce_points_bulk failed: {exc!r}")


def get_point_setting(db, key: str) -> int:
    """Read a points setting, falling back to POINT_DEFAULTS if unset/invalid."""
    if key not in POINT_DEFAULTS:
        raise KeyError(f"Unknown points setting: {key}")
    raw = db.get_config(key)
    if raw is None:
        return POINT_DEFAULTS[key]
    try:
        return int(raw)
    except (TypeError, ValueError):
        warning_log(f"Bad value for {key}: {raw!r}; using default {POINT_DEFAULTS[key]}.")
        return POINT_DEFAULTS[key]


def _voice_announcement_due(db, now: datetime.datetime) -> bool:
    """Throttle high-volume voice reward posts while still awarding every tick."""
    interval_min = get_point_setting(db, "points_voice_announce_min")
    if interval_min <= 0:
        return False
    raw = db.get_config(_VOICE_ANNOUNCE_LAST_KEY)
    if raw:
        try:
            last = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=datetime.timezone.utc)
            if (now - last.astimezone(datetime.timezone.utc)).total_seconds() < interval_min * 60:
                return False
        except ValueError:
            pass
    db.set_config(_VOICE_ANNOUNCE_LAST_KEY, now.isoformat())
    return True


def award_fame_points(db, discord_id: str, *, kill_delta: int = 0, pve_delta: int = 0,
                      gather_delta: int = 0, craft_delta: int = 0,
                      fish_delta: int = 0) -> int:
    """Convert positive Albion fame deltas into activity points and apply them.

    Returns the total points awarded for this profile this call. Negative or
    zero deltas are ignored. Designed to be called from cogs/events.py.
    """
    pairs = (
        (kill_delta,   "points_per_kill_fame"),
        (pve_delta,    "points_per_pve_fame"),
        (gather_delta, "points_per_gather_fame"),
        (craft_delta,  "points_per_craft_fame"),
        (fish_delta,   "points_per_fish_fame"),
    )
    total = 0
    for delta, key in pairs:
        if delta <= 0:
            continue
        ratio = get_point_setting(db, key)
        if ratio <= 0:
            continue
        total += delta // ratio
    if total > 0:
        db.add_points(discord_id, total)
    return total


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────
class Points(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        # Per-user last-message-award timestamp (in-memory; resets on restart).
        self._chat_cooldowns: dict[int, datetime.datetime] = {}
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        self.voice_tick.start()
        self.reset_check.start()
        self.activity_digest_tick.start()

    def cog_unload(self) -> None:
        self.voice_tick.cancel()
        self.reset_check.cancel()
        self.activity_digest_tick.cancel()

    # ── Chat awards ──────────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if message.webhook_id is not None:
            return
        # Ignore slash-command interactions and pure-attachment uploads.
        content = (message.content or "").strip()
        min_chars = get_point_setting(self.bot.db, "points_chat_min_chars")
        if len(content) < min_chars:
            return

        # Only fully-registered users (linked Albion character) earn points,
        # so the leaderboard's in-game names are always populated.
        profile = self.bot.db.fetch_user_profile(str(message.author.id))
        if not profile or not profile.get("albion_player_id"):
            return

        cooldown = get_point_setting(self.bot.db, "points_chat_cooldown_sec")
        now = datetime.datetime.utcnow()
        last = self._chat_cooldowns.get(message.author.id)
        if last and (now - last).total_seconds() < cooldown:
            return
        self._chat_cooldowns[message.author.id] = now

        amount = get_point_setting(self.bot.db, "points_chat")
        if amount > 0:
            self.bot.db.add_points(str(message.author.id), amount)
            info_log(
                f"Awarded {amount} chat point(s) to "
                f"{profile.get('albion_name') or message.author.name}."
            )

    # ── Voice awards ─────────────────────────────────────────────────────────
    @tasks.loop(minutes=POINT_DEFAULTS["points_voice_tick_min"])
    async def voice_tick(self) -> None:
        try:
            amount     = get_point_setting(self.bot.db, "points_voice")
            min_humans = get_point_setting(self.bot.db, "points_voice_min_humans")
            if amount <= 0:
                return
            # Prefetch every registered Discord ID once per tick rather than
            # hitting SQLite per-member-per-channel-per-guild. With 200+ voice
            # users this drops hundreds of queries to one.
            registered_ids = {
                str(p["discord_id"])
                for p in self.bot.db.fetch_all_registered_profiles()
                if p.get("albion_player_id")
            }
            awarded_ids: list[str] = []
            for guild in self.bot.guilds:
                afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
                for vc in guild.voice_channels:
                    if afk_channel_id and vc.id == afk_channel_id:
                        continue
                    humans = [
                        m for m in vc.members
                        if not m.bot
                        and not (m.voice and (m.voice.self_deaf or m.voice.deaf or m.voice.afk))
                    ]
                    if len(humans) < min_humans:
                        continue
                    for member in humans:
                        if str(member.id) not in registered_ids:
                            continue
                        self.bot.db.add_points(str(member.id), amount)
                        awarded_ids.append(str(member.id))
            if awarded_ids:
                info_log(
                    f"Voice tick: awarded {amount} point(s) each to "
                    f"{len(awarded_ids)} member(s)."
                )
                now = datetime.datetime.now(datetime.timezone.utc)
                if _voice_announcement_due(self.bot.db, now):
                    announce_min = get_point_setting(self.bot.db, "points_voice_announce_min")
                    await announce_points_bulk(
                        self.bot, awarded_ids, amount,
                        f"Voice rewards are still awarded every tick; this public card posts about every {announce_min} min.",
                    )
        except Exception as exc:
            error_log(f"voice_tick error: {exc}")

    @voice_tick.before_loop
    async def _before_voice_tick(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def activity_digest_tick(self) -> None:
        try:
            await flush_activity_feed_digest(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"activity_digest_tick error: {exc!r}")

    @activity_digest_tick.before_loop
    async def _before_activity_digest_tick(self) -> None:
        await self.bot.wait_until_ready()

    # ── Auto reset (weekly / monthly) ────────────────────────────────────────
    @tasks.loop(hours=1)
    async def reset_check(self) -> None:
        try:
            now = datetime.datetime.utcnow()

            # Weekly: track the ISO year-week we last reset for. If the bot was
            # offline on Monday, this still fires the first time it boots in a
            # new week (any day, not just Monday).
            iso_year, iso_week, _ = now.isocalendar()
            current_week_tag = f"{iso_year}-W{iso_week:02d}"
            last_w = self.bot.db.get_config(_RESET_KEY_WEEKLY)
            # Migrate legacy date-only values (YYYY-MM-DD) to their ISO week tag
            # so the upgrade doesn't trigger an immediate mid-week wipe.
            if last_w and len(last_w) == 10 and last_w[4] == "-" and last_w[7] == "-":
                try:
                    d = datetime.date.fromisoformat(last_w)
                    ly, lw, _ = d.isocalendar()
                    last_w = f"{ly}-W{lw:02d}"
                except ValueError:
                    pass
            if last_w != current_week_tag:
                affected = self.bot.db.reset_points_window("weekly")
                self.bot.db.set_config(_RESET_KEY_WEEKLY, current_week_tag)
                info_log(f"Auto-reset weekly points for {affected} profile(s) [{current_week_tag}].")

            # Monthly: track the year-month we last reset for. Resets on the
            # first boot of a new month, even if the bot was offline on the 1st.
            current_month_tag = f"{now.year}-{now.month:02d}"
            last_m = self.bot.db.get_config(_RESET_KEY_MONTHLY)
            # Migrate legacy YYYY-MM-DD values to YYYY-MM.
            if last_m and len(last_m) == 10 and last_m[4] == "-" and last_m[7] == "-":
                last_m = last_m[:7]
            if last_m != current_month_tag:
                affected = self.bot.db.reset_points_window("monthly")
                self.bot.db.set_config(_RESET_KEY_MONTHLY, current_month_tag)
                info_log(f"Auto-reset monthly points for {affected} profile(s) [{current_month_tag}].")
        except Exception as exc:
            error_log(f"reset_check error: {exc}")

    @reset_check.before_loop
    async def _before_reset_check(self) -> None:
        await self.bot.wait_until_ready()

    # ──────────────────────────────────────────────────────────────────────
    # Slash commands  —  /points …
    # ──────────────────────────────────────────────────────────────────────
    points_group = app_commands.Group(
        name="points", description="Activity points commands."
    )

    @points_group.command(name="show", description="Show your (or another member's) activity points.")
    @app_commands.describe(member="Whose points to show. Defaults to yourself.")
    async def points_show(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        pts = self.bot.db.get_points(str(target.id))
        embed = info_embed(
            f"Activity points — {target.display_name}",
            (
                f"**Weekly:**  {pts['weekly']:,}\n"
                f"**Monthly:** {pts['monthly']:,}\n"
                f"**Season:**  {pts['season']:,}"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=(member is None))

    @points_group.command(name="leaderboard", description="Top point earners for a window.")
    @app_commands.describe(window="Which window to rank by.", limit="How many to show (1-25, default 10).")
    @app_commands.choices(window=[
        app_commands.Choice(name="Weekly",  value="weekly"),
        app_commands.Choice(name="Monthly", value="monthly"),
        app_commands.Choice(name="Season",  value="season"),
    ])
    async def points_leaderboard(
        self,
        interaction: discord.Interaction,
        window: app_commands.Choice[str],
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        rows = self.bot.db.top_points(window.value, limit=int(limit), home_guild=_resolve_home_guild(self.bot.db))
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(f"{window.name} leaderboard", "No points awarded yet."),
                ephemeral=True,
            )
            return
        lines = []
        for i, row in enumerate(rows, start=1):
            name = row.get("albion_name") or row.get("username") or row.get("discord_id")
            lines.append(f"`#{i:>2}`  **{name}** — {int(row['points']):,}")
        embed = info_embed(f"{window.name} leaderboard — top {len(rows)}", "\n".join(lines))
        await interaction.response.send_message(embed=embed)

    @points_group.command(
        name="rank",
        description="Show your (or another member's) rank across weekly / monthly / season.",
    )
    @app_commands.describe(member="Whose rank to look up. Defaults to yourself.")
    async def points_rank(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        target_id = str(target.id)
        db = self.bot.db
        profile = db.fetch_user_profile(target_id)
        if not profile:
            await interaction.response.send_message(
                embed=warning_embed(
                    "No profile",
                    f"{target.mention} has no profile yet — they need to click the **Register** button in your registration channel first."
                ),
                ephemeral=True,
            )
            return

        windows = [("Weekly", "points_weekly"), ("Monthly", "points_monthly"), ("Season", "points_season")]
        lines = []
        for label, col in windows:
            try:
                if not db.connection:
                    db.connect()
                row = db.cursor.execute(
                    f"SELECT {col} AS pts FROM user_profiles WHERE discord_id = ?",
                    (target_id,),
                ).fetchone()
                pts = int(row["pts"]) if row and row["pts"] is not None else 0
                # Rank = 1 + count of profiles with strictly more points (only
                # registered, non-zero) — ties share the higher rank.
                rank_row = db.cursor.execute(
                    f'''SELECT COUNT(*) + 1 AS rank
                        FROM user_profiles
                        WHERE {col} > ?
                          AND albion_player_id IS NOT NULL''',
                    (pts,),
                ).fetchone()
                rank = int(rank_row["rank"]) if rank_row else 0
                total_row = db.cursor.execute(
                    f'''SELECT COUNT(*) AS total
                        FROM user_profiles
                        WHERE {col} > 0
                          AND albion_player_id IS NOT NULL'''
                ).fetchone()
                total = int(total_row["total"]) if total_row else 0
                if pts <= 0:
                    lines.append(f"**{label}** — _no points this window_")
                else:
                    lines.append(f"**{label}** — `#{rank}` of `{total}` · **{pts:,}** pts")
            except Exception as exc:  # noqa: BLE001
                error_log(f"points_rank {label.lower()} query failed: {exc!r}")
                lines.append(f"**{label}** — _query failed_")

        name = profile.get("albion_name") or target.display_name
        embed = info_embed(f"📊 Rank — {name}", "\n".join(lines))
        await interaction.response.send_message(embed=embed, ephemeral=(member is None))

    @points_group.command(name="config-show", description="Show every tunable points setting.")
    async def points_config_show(self, interaction: discord.Interaction) -> None:
        lines = []
        for key in TUNABLE_KEYS:
            current = get_point_setting(self.bot.db, key)
            default = POINT_DEFAULTS[key]
            tag = "" if current == default else "  *(custom)*"
            lines.append(f"`{key}` = **{current:,}** *(default {default:,})*{tag}")
        await interaction.response.send_message(
            embed=info_embed("Points configuration", "\n".join(lines)),
            ephemeral=True,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Officer / admin subcommands
    # ──────────────────────────────────────────────────────────────────────
    @points_group.command(name="config-set", description="Change a points setting (officer).")
    @app_commands.describe(key="The setting key.", value="The new integer value (>= 0).")
    @app_commands.choices(key=[app_commands.Choice(name=k, value=k) for k in TUNABLE_KEYS])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def points_config_set(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: app_commands.Range[int, 0, 10_000_000],
    ) -> None:
        self.bot.db.set_config(key.value, str(int(value)))
        info_log(f"{interaction.user} set {key.value} = {value}.")
        await interaction.response.send_message(
            embed=success_embed(
                "Setting updated",
                f"`{key.value}` is now **{int(value):,}** (default `{POINT_DEFAULTS[key.value]:,}`).",
            ),
            ephemeral=True,
        )

    @points_group.command(name="config-reset", description="Reset a points setting to its default (officer).")
    @app_commands.describe(key="The setting key to reset.")
    @app_commands.choices(key=[app_commands.Choice(name=k, value=k) for k in TUNABLE_KEYS])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def points_config_reset(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
    ) -> None:
        # Easiest: write the default explicitly so reads are deterministic.
        self.bot.db.set_config(key.value, str(POINT_DEFAULTS[key.value]))
        info_log(f"{interaction.user} reset {key.value} to default.")
        await interaction.response.send_message(
            embed=success_embed(
                "Setting reset",
                f"`{key.value}` reset to default **{POINT_DEFAULTS[key.value]:,}**.",
            ),
            ephemeral=True,
        )

    @points_group.command(name="add", description="Manually award points to a member (officer).")
    @app_commands.describe(member="Member to award.", amount="Points to add (1–100,000).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def points_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
    ) -> None:
        if not self.bot.db.fetch_user_profile(str(member.id)):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{member.mention} has no profile yet."),
                ephemeral=True,
            )
            return
        self.bot.db.add_points(str(member.id), int(amount))
        info_log(f"{interaction.user} added {amount} points to {member}.")
        await announce_points(
            self.bot, member.id, int(amount),
            f"Awarded by {interaction.user.mention}",
        )
        await interaction.response.send_message(
            embed=success_embed("Points added",
                                f"Added **{int(amount):,}** points to {member.mention}."),
            ephemeral=True,
        )

    @points_group.command(name="subtract", description="Manually subtract points from a member (officer).")
    @app_commands.describe(member="Member to debit.", amount="Points to remove (1–100,000).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def points_subtract(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, 100_000],
    ) -> None:
        if not self.bot.db.fetch_user_profile(str(member.id)):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{member.mention} has no profile yet."),
                ephemeral=True,
            )
            return
        self.bot.db.add_points(str(member.id), -int(amount))
        info_log(f"{interaction.user} subtracted {amount} points from {member}.")
        await interaction.response.send_message(
            embed=success_embed("Points removed",
                                f"Subtracted **{int(amount):,}** points from {member.mention}."),
            ephemeral=True,
        )

    @points_group.command(name="reset", description="Reset a points window for everyone (officer).")
    @app_commands.describe(window="Which window to wipe.")
    @app_commands.choices(window=[
        app_commands.Choice(name="Weekly",  value="weekly"),
        app_commands.Choice(name="Monthly", value="monthly"),
        app_commands.Choice(name="Season",  value="season"),
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def points_reset(
        self,
        interaction: discord.Interaction,
        window: app_commands.Choice[str],
    ) -> None:
        affected = self.bot.db.reset_points_window(window.value)
        # Keep auto-reset state in sync for weekly/monthly so it doesn't immediately re-fire.
        today_iso = datetime.datetime.utcnow().date().isoformat()
        if window.value == "weekly":
            self.bot.db.set_config(_RESET_KEY_WEEKLY, today_iso)
        elif window.value == "monthly":
            self.bot.db.set_config(_RESET_KEY_MONTHLY, today_iso)
        info_log(f"{interaction.user} reset {window.value} points ({affected} rows).")
        await interaction.response.send_message(
            embed=success_embed(
                f"{window.name} points reset",
                f"Cleared **{window.name.lower()}** points for **{affected:,}** profile(s).",
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(Points(bot))
