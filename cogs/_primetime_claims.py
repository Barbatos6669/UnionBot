"""Live prime-time claim dashboard helpers.

The source of truth is ``lfg_events``: creating a prime-time event through the
event board is what claims that Albion timer slot. This module renders those
claims as an embed and provides the persistent refresh button used by tracked
dashboards.
"""

from __future__ import annotations

import datetime as _dt
import os

import discord

from cogs._lfg_config import (
    EVENT_TYPES_BY_KEY,
    PRIME_SLOTS,
    prime_first_hour,
    prime_slot_display_label,
    prime_slot_for_label,
    prime_timer_cycle_date,
    prime_timer_cycle_end,
    prime_timer_cycle_start,
)
from cogs._typing import Bot
from debug import error_log
from utils import error_embed

TRACKER_TYPE = "prime-claims"
VALID_WINDOWS = {"today", "week"}


def normalize_claim_window(window: str | None) -> str:
    window = (window or "today").strip().lower()
    return window if window in VALID_WINDOWS else "today"


def _timer_cycle_date(now: _dt.datetime) -> _dt.date:
    return prime_timer_cycle_date(now)


def _cycle_date_for_event_start(
    starts_at: _dt.datetime,
    slot_label: str | None,
) -> _dt.date:
    first_hour = prime_first_hour()
    slot = prime_slot_for_label(slot_label)
    slot_hour = slot.start_hour if slot else starts_at.hour
    if slot_hour < first_hour:
        return starts_at.date() - _dt.timedelta(days=1)
    return starts_at.date()


def _cycle_start(day: _dt.date) -> _dt.datetime:
    return prime_timer_cycle_start(day)


def _cycle_end(day: _dt.date) -> _dt.datetime:
    return prime_timer_cycle_end(day)


def _slot_display_label(_day: _dt.date, slot) -> str:
    return prime_slot_display_label(slot)


def _claim_window_bounds(
    window: str,
    now: _dt.datetime | None = None,
) -> tuple[_dt.datetime, _dt.datetime]:
    """Return UTC [start, end) for Albion prime timer-day dashboards."""
    now = now or discord.utils.utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    now = now.astimezone(_dt.timezone.utc)
    start_day = _timer_cycle_date(now)
    days = 7 if normalize_claim_window(window) == "week" else 1
    end_day = start_day + _dt.timedelta(days=days - 1)
    return _cycle_start(start_day), _cycle_end(end_day)


def _parse_event_time(raw: str | None) -> _dt.datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = _dt.datetime.strptime(text[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _slot_key_from_label(slot_label: str | None) -> str | None:
    slot = prime_slot_for_label(slot_label)
    return slot.label if slot else None


def _slot_key_from_event(event: dict) -> str | None:
    key = _slot_key_from_label(event.get("slot_label"))
    if key:
        return key
    start = _parse_event_time(event.get("starts_at"))
    end = _parse_event_time(event.get("ends_at"))
    if not start or not end:
        return None
    return f"{start.hour:02d}:00-{end.hour:02d}:00"


def _fetch_prime_claims(db, start: _dt.datetime, end: _dt.datetime) -> list[dict]:
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """
            SELECT e.*,
                   COUNT(s.id) AS signup_count
            FROM lfg_events e
            LEFT JOIN lfg_signups s ON s.event_id = e.id
            WHERE e.is_prime = 1
              AND e.status != 'cancelled'
              AND datetime(e.starts_at) >= datetime(?)
              AND datetime(e.starts_at) < datetime(?)
            GROUP BY e.id
            ORDER BY datetime(e.starts_at), e.id
            """,
            (
                start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"),
            ),
        )
        return [dict(row) for row in db.cursor.fetchall()]
    except Exception as exc:  # noqa: BLE001
        error_log(f"prime claims fetch failed: {exc!r}")
        return []


def _status_tag(event: dict, now: _dt.datetime) -> str:
    start = _parse_event_time(event.get("starts_at"))
    end = _parse_event_time(event.get("ends_at"))
    status = str(event.get("status") or "open").lower()
    if status == "completed":
        return "✅"
    if start and end and start <= now <= end:
        return "🔥"
    if start and start < now:
        return "⏳"
    return "🟢"


def _event_type_prefix(event_type: str | None) -> str:
    etype = EVENT_TYPES_BY_KEY.get((event_type or "").strip())
    return f"{etype.emoji} " if etype else ""


def _short_title(title: str | None, limit: int = 44) -> str:
    text = " ".join(str(title or "Untitled").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _resolve_guild_id(bot: Bot) -> str | None:
    raw = os.getenv("GUILD_DISCORD_ID")
    if raw and raw.isdigit():
        return raw

    dev_guild_id = getattr(getattr(bot, "dev_guild", None), "id", None)
    if dev_guild_id:
        return str(dev_guild_id)

    guilds = list(getattr(bot, "guilds", []) or [])
    if len(guilds) == 1:
        return str(guilds[0].id)
    return None


def _lfg_message_url(event: dict, guild_id: str | None) -> str | None:
    if event.get("lfg_cleaned_at"):
        return None
    channel_id = str(event.get("channel_id") or "").strip()
    message_id = str(event.get("message_id") or "").strip()
    if not guild_id or not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _linked_event_title(event: dict, guild_id: str | None) -> str:
    title = discord.utils.escape_markdown(_short_title(event.get("title")), as_needed=True)
    url = _lfg_message_url(event, guild_id)
    if not url:
        return f"**{title}**"
    return f"**[{title}]({url})**"


def _format_claim_line(
    event: dict | None,
    slot_label: str,
    now: _dt.datetime,
    guild_id: str | None = None,
    display_label: str | None = None,
) -> str:
    slot = prime_slot_for_label(slot_label)
    emoji = slot.emoji if slot else "⬛"
    label = display_label or slot_label
    if not event:
        return f"{emoji} `{label}` — _open_"

    creator_id = str(event.get("creator_id") or "")
    owner = f"<@{creator_id}>" if creator_id.isdigit() else (creator_id or "unknown")
    start = _parse_event_time(event.get("starts_at"))
    when = f" <t:{int(start.timestamp())}:t>" if start else ""
    signups = int(event.get("signup_count") or 0)
    prefix = _event_type_prefix(event.get("event_type"))
    return (
        f"{emoji} `{label}` {_status_tag(event, now)} {owner}{when} — "
        f"{prefix}{_linked_event_title(event, guild_id)} · {signups} signed"
    )


def _format_day_field_name(day: _dt.date) -> str:
    prime_start = _cycle_start(day)
    return (
        f"{day.strftime('%a %b %d timer day')} • starts "
        f"<t:{int(prime_start.timestamp())}:t> local"
    )


def build_prime_claims_embed(bot: Bot, window: str = "today") -> discord.Embed:
    """Build the live prime-time claim dashboard embed."""
    window = normalize_claim_window(window)
    now = discord.utils.utcnow()
    start, end = _claim_window_bounds(window, now)
    rows = _fetch_prime_claims(bot.db, start, end)

    by_day_slot: dict[tuple[str, str], dict] = {}
    for row in rows:
        dt = _parse_event_time(row.get("starts_at"))
        slot_key = _slot_key_from_event(row)
        if not dt or not slot_key:
            continue
        cycle_day = _cycle_date_for_event_start(dt, slot_key)
        by_day_slot[(cycle_day.isoformat(), slot_key)] = row

    title = "Prime-Time Claims"
    start_day = start.date()
    day_count = 7 if window == "week" else 1
    end_day = start_day + _dt.timedelta(days=day_count - 1)
    if window == "today":
        title += " — Today"
        description = (
            f"Claimed Albion prime slots for **{start_day.isoformat()} timer day**. "
            "A slot is claimed when a prime LFG event is created. "
            "Fields are grouped by Albion timer day: 18/20/22 UTC, then "
            "00/02/04 UTC on the next UTC date."
        )
    else:
        title += " — Next 7 Days"
        description = (
            f"Claimed Albion prime slots from **{start_day.isoformat()}** "
            f"through **{end_day.isoformat()} timer days**. "
            "Each field is one Albion timer day: 18/20/22 UTC, then "
            "00/02/04 UTC on the next UTC date. Discord timestamps render locally."
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.orange(),
        timestamp=now,
    )

    guild_id = _resolve_guild_id(bot)
    for offset in range(day_count):
        day = start_day + _dt.timedelta(days=offset)
        lines = [
            _format_claim_line(
                by_day_slot.get((day.isoformat(), slot.label)),
                slot.label,
                now,
                guild_id,
                _slot_display_label(day, slot),
            )
            for slot in PRIME_SLOTS
        ]
        field_name = _format_day_field_name(day)
        if window == "today":
            embed.add_field(name=field_name, value="\n".join(lines), inline=False)
            break
        embed.add_field(name=field_name, value="\n".join(lines)[:1024], inline=False)

    claimed = len(by_day_slot)
    total = len(PRIME_SLOTS) * day_count
    embed.set_footer(
        text=(
            f"{claimed}/{total} slots claimed • Albion timer days • "
            "Discord timestamps show each viewer's local time"
        ),
    )
    return embed


async def refresh_prime_claim_trackers(bot: Bot) -> int:
    """Refresh all live prime-claim dashboard messages."""
    try:
        trackers = bot.db.fetch_all_live_graphs()
    except Exception as exc:  # noqa: BLE001
        error_log(f"prime claims tracker fetch failed: {exc!r}")
        return 0

    timestamp = int(discord.utils.utcnow().timestamp())
    updated = 0
    for tracker in trackers:
        if tracker["type"] != TRACKER_TYPE:
            continue

        window = normalize_claim_window(tracker["target_id"])
        embed = build_prime_claims_embed(bot, window)
        view = PrimeClaimsRefreshView()
        channel = None
        try:
            channel = bot.get_channel(int(tracker["channel_id"]))
            if channel is None:
                channel = await bot.fetch_channel(int(tracker["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue

            message = await channel.fetch_message(int(tracker["message_id"]))
            await message.edit(
                content=f"-# Last updated: <t:{timestamp}:R>",
                embed=embed,
                attachments=[],
                view=view,
            )
            updated += 1
        except discord.NotFound:
            if not isinstance(channel, discord.TextChannel):
                continue
            try:
                new_msg = await channel.send(
                    content=f"-# Last updated: <t:{timestamp}:R>",
                    embed=embed,
                    view=view,
                )
                bot.db.upsert_live_graph(
                    TRACKER_TYPE,
                    window,
                    str(channel.id),
                    str(new_msg.id),
                )
                updated += 1
            except Exception as exc:  # noqa: BLE001
                error_log(f"prime claims tracker resend failed: {exc!r}")
        except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
            error_log(f"prime claims tracker refresh failed: {exc!r}")

    try:
        from cogs._timer_claims_guide import refresh_timer_claim_guide_trackers

        updated += await refresh_timer_claim_guide_trackers(bot)
    except Exception as exc:  # noqa: BLE001
        error_log(f"timer claim guide refresh from prime claims failed: {exc!r}")

    return updated


class PrimeClaimsRefreshView(discord.ui.View):
    """Refresh button for live prime-time claim dashboards."""

    _COOLDOWN_SECS = 20

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self._last_refresh: dict[int, float] = {}

    @discord.ui.button(
        label="Refresh",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
        custom_id="primetime:claims:refresh",
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        import time

        now = time.monotonic()
        last = self._last_refresh.get(interaction.user.id, 0.0)
        wait = self._COOLDOWN_SECS - (now - last)
        if wait > 0:
            await interaction.response.send_message(
                embed=error_embed("Slow down", f"Try again in {wait:.0f}s."),
                ephemeral=True,
            )
            return
        self._last_refresh[interaction.user.id] = now

        bot = interaction.client  # type: ignore[assignment]
        window = "today"
        try:
            for tracker in bot.db.fetch_all_live_graphs():  # type: ignore[attr-defined]
                if (
                    tracker["type"] == TRACKER_TYPE
                    and str(tracker["message_id"]) == str(interaction.message.id)
                ):
                    window = normalize_claim_window(tracker["target_id"])
                    break
        except Exception:
            pass

        await interaction.response.defer()
        embed = build_prime_claims_embed(bot, window)  # type: ignore[arg-type]
        await interaction.message.edit(
            content=f"-# Last updated: <t:{int(discord.utils.utcnow().timestamp())}:R>",
            embed=embed,
            view=self,
        )
