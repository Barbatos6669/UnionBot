"""Embeds, persistent views, modals, and lifecycle for the content-curator cog.

This module owns *all* discord-facing presentation for content curator:
    * Embed builders for weekly poll, quickpoll, dashboard
    * Persistent views (ContentPollView, ContentQuickPollView, ContentBoardView)
    * Modals/selects used by the dashboard suggest flow
    * Lifecycle helpers: open_poll / close_poll / open_quickpoll / close_quickpoll
    * LFG auto-creation helpers

Kept separate from the cog so the cog stays focused on slash commands and the
background tick loop.
"""

from __future__ import annotations

from cogs._typing import Bot
import datetime as dt
import json
import os
from typing import Optional

import discord

from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed
from cogs._lfg_config import (
    EVENT_TYPES_BY_KEY,
    PREP_MINUTES,
    REVIEW_MINUTES,
    CFG_LFG_CHANNEL,
    display_slot_label,
)
from cogs._content_config import (
    CFG_AUTO_LFG,
    CFG_BOARD_CHANNEL,
    CFG_BOARD_MESSAGE,
    CFG_CHANNEL,
    CFG_DURATION_HOURS,
    CFG_EVENT_DURATION,
    CFG_EVENT_HOUR,
    CFG_MAX_PER_USER,
    CFG_QUICKVOTE_DURATION_LFG,
    CFG_QUICKVOTE_DURATION_MIN,
    CFG_QUICKVOTE_LEAD_MIN,
    CFG_TOP_N,
    DEFAULT_QUICKVOTE_KEYS,
    QUICKVOTE_PREFIX,
    SUGGEST_PICK_KEYS,
    activity_min,
    availability_recommendation_keys,
    cfg_int,
    is_officer,
    now_utc,
    utc_dt_for_discord,
)
from cogs._content_db import (
    availability_slot_labels,
    availability_tallies,
    availability_total_voters,
    cast_quickvote,
    count_user_suggestions,
    fetch_open_poll,
    fetch_availability_poll,
    fetch_daily_timer_funnel_by_quickpoll,
    fetch_open_availability_poll,
    fetch_open_quickpoll,
    fetch_pending_suggestions,
    fetch_poll_suggestions,
    fetch_quickpoll,
    quickpoll_option_keys,
    quickpoll_tallies,
    quickpoll_total_votes,
    set_availability_votes,
    set_user_votes,
    update_daily_timer_funnel,
)


# ── embeds ──────────────────────────────────────────────────────────────────
def discord_ts_from_iso(raw: str) -> int:
    return int(utc_dt_for_discord(dt.datetime.fromisoformat(str(raw))).timestamp())


def _apply_announcement_watermark(db, embed: discord.Embed, footer_text: str) -> None:
    """Apply the configured announcement crest to content-planning embeds."""
    crest_url = ""
    try:
        crest_url = (db.get_config("announce_crest_url") or "").strip()
    except Exception:  # noqa: BLE001
        crest_url = ""
    if crest_url:
        embed.set_thumbnail(url=crest_url)
        embed.set_footer(text=footer_text, icon_url=crest_url)
    else:
        embed.set_footer(text=footer_text)


def poll_embed(db, poll: dict) -> discord.Embed:
    suggestions = fetch_poll_suggestions(db, poll["id"])
    total_votes = sum(s["vote_count"] for s in suggestions)
    ts = discord_ts_from_iso(poll["closes_at"])

    color = discord.Color.blurple() if poll["status"] == "open" else discord.Color.dark_gray()
    title = "📊 Content Poll" + (" — open" if poll["status"] == "open" else " — closed")
    embed = discord.Embed(title=title, color=color)
    if poll["status"] == "open":
        embed.description = f"Pick the events you want to play.\nCloses <t:{ts}:R> (<t:{ts}:f>)."
    else:
        embed.description = "This poll is closed. See the winners announcement."

    if not suggestions:
        embed.add_field(name="No suggestions yet", value="Use `/content suggest` to add one.", inline=False)
        return embed

    lines = []
    for i, s in enumerate(suggestions, 1):
        et = EVENT_TYPES_BY_KEY.get(s["event_type"])
        emoji = et.emoji if et else "📌"
        label = et.label if et else s["event_type"]
        bar = "█" * min(int(s["vote_count"]), 12)
        lines.append(
            f"`#{i}` {emoji} **{s['title']}** _(by <@{s['suggester_id']}>)_\n"
            f"   {label} · **{s['vote_count']}** vote(s) {bar}"
        )
    embed.add_field(name=f"Suggestions ({len(suggestions)})", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text=f"Poll #{poll['id']} · {total_votes} total vote(s) cast")
    return embed


def winners_embed(db, poll: dict, winners: list[dict], created_event_ids: list[int]) -> discord.Embed:
    embed = discord.Embed(
        title="🏆 Content Poll Results",
        description=f"Poll #{poll['id']} is closed. Here's what the community picked:",
        color=discord.Color.green(),
    )
    if not winners:
        embed.add_field(name="No winners", value="No suggestions received any votes.", inline=False)
        return embed
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(winners):
        medal = medals[i] if i < len(medals) else f"#{i + 1}"
        et = EVENT_TYPES_BY_KEY.get(s["event_type"])
        emoji = et.emoji if et else "📌"
        label = et.label if et else s["event_type"]
        note = f"\n_{s['notes']}_" if s.get("notes") else ""
        embed.add_field(
            name=f"{medal} {emoji} {s['title']}",
            value=(
                f"{label} · **{s['vote_count']}** vote(s)\n"
                f"Suggested by <@{s['suggester_id']}>{note}"
            ),
            inline=False,
        )
    if created_event_ids:
        embed.add_field(
            name="📅 Auto-scheduled LFG events",
            value=", ".join(f"#{eid}" for eid in created_event_ids),
            inline=False,
        )
    return embed


def quickpoll_embed(db, poll: dict) -> discord.Embed:
    keys = quickpoll_option_keys(poll)
    tallies = quickpoll_tallies(db, poll["id"])
    total = sum(tallies.values())
    ts = discord_ts_from_iso(poll["closes_at"])

    is_open = poll["status"] == "open"
    color = discord.Color.gold() if is_open else discord.Color.dark_gray()
    title = "🎯 Next Activity" + (" — vote!" if is_open else " — closed")
    desc_lines = [
        "**One vote per player.** Your vote = your headcount.",
        "Each activity needs a minimum number of players to run.",
    ]
    if poll.get("target_starts_at"):
        try:
            starts_at = utc_dt_for_discord(dt.datetime.fromisoformat(str(poll["target_starts_at"])))
            ends_at = utc_dt_for_discord(dt.datetime.fromisoformat(str(poll["target_ends_at"])))
            slot_label = str(poll.get("target_slot_label") or "Target timer")
            desc_lines.append(
                f"Timer: **{display_slot_label(slot_label)}** · Your time: "
                f"<t:{int(starts_at.timestamp())}:f> to <t:{int(ends_at.timestamp())}:t>."
            )
        except (TypeError, ValueError):
            pass
    if is_open:
        desc_lines.append(f"Closes <t:{ts}:R> (<t:{ts}:T>).")
    embed = discord.Embed(title=title, description="\n".join(desc_lines), color=color)

    if not keys:
        embed.add_field(name="No options", value="_(misconfigured)_", inline=False)
        return embed

    ranked = sorted(keys, key=lambda k: (-tallies.get(k, 0), k))
    lines = []
    for k in ranked:
        et = EVENT_TYPES_BY_KEY[k]
        n = tallies.get(k, 0)
        need = activity_min(k)
        status = "✅" if n >= need else "⏳"
        bar = "█" * min(n, 12)
        lines.append(f"{et.emoji} **{et.label}** — `{n}/{need}` {status} {bar}")
    embed.add_field(name="Activities", value="\n".join(lines)[:1024], inline=False)
    _apply_announcement_watermark(
        db, embed, f"Quickpoll #{poll['id']} · {total} vote(s) cast",
    )
    return embed


def quickpoll_result_embed(
    db, poll: dict, winner_key: Optional[str], lfg_event_id: Optional[int],
) -> discord.Embed:
    tallies = quickpoll_tallies(db, poll["id"])
    if winner_key is not None:
        et = EVENT_TYPES_BY_KEY[winner_key]
        embed = discord.Embed(
            title=f"🏆 Next activity: {et.emoji} {et.label}",
            description=(
                f"Won with **{tallies.get(winner_key, 0)}** vote(s) "
                f"(min **{activity_min(winner_key)}**)."
            ),
            color=discord.Color.green(),
        )
        if lfg_event_id:
            embed.add_field(
                name="📅 LFG event",
                value=f"Auto-created as event **#{lfg_event_id}**. Sign up in the LFG channel.",
                inline=False,
            )
        _apply_announcement_watermark(db, embed, f"Quickpoll #{poll['id']} result")
        return embed

    embed = discord.Embed(
        title="🚫 No activity reached its minimum",
        description=(
            "Not enough players signed on for any option. "
            "Try again with a different option set, or run something solo-friendly."
        ),
        color=discord.Color.red(),
    )
    if tallies:
        ordered = sorted(tallies.items(), key=lambda kv: -kv[1])
        lines = []
        for k, n in ordered:
            et = EVENT_TYPES_BY_KEY.get(k)
            label = f"{et.emoji} {et.label}" if et else k
            lines.append(f"{label} — `{n}/{activity_min(k)}`")
        embed.add_field(name="Final tallies", value="\n".join(lines)[:1024], inline=False)
    _apply_announcement_watermark(db, embed, f"Quickpoll #{poll['id']} result")
    return embed


def _availability_timer_window(
    poll: dict, label: str,
) -> tuple[dt.datetime, dt.datetime] | None:
    """Parse generated daily timer labels into UTC start/end datetimes."""
    import re

    match = re.search(
        r"(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})"
        r"(?:\s+·\s+UTC\s+|\s+)"
        r"(?P<start>\d{2})(?::00)?-(?P<end>\d{2})(?::00)?(?:\s+UTC)?",
        label,
    )
    if not match:
        return None
    try:
        closes = utc_dt_for_discord(dt.datetime.fromisoformat(poll["closes_at"]))
        candidates: list[tuple[float, dt.datetime, dt.datetime]] = []
        for year in (closes.year - 1, closes.year, closes.year + 1):
            start = dt.datetime.strptime(
                f"{year} {match.group('mon')} {match.group('day')} "
                f"{match.group('start')}",
                "%Y %b %d %H",
            ).replace(tzinfo=dt.timezone.utc)
            end_hour = int(match.group("end"))
            end = start.replace(hour=end_hour)
            if end <= start:
                end += dt.timedelta(days=1)
            candidates.append((abs((start - closes).total_seconds()), start, end))
        _distance, starts_at, ends_at = min(candidates, key=lambda row: row[0])
    except (KeyError, TypeError, ValueError):
        return None
    return starts_at, ends_at


def _availability_local_time_note(poll: dict, label: str) -> str | None:
    """Return a Discord-local timestamp note for generated daily timer labels."""
    window = _availability_timer_window(poll, label)
    if not window:
        return None
    starts_at, ends_at = window
    return f"<t:{int(starts_at.timestamp())}:f> - <t:{int(ends_at.timestamp())}:t>"


def _availability_slot_heading(label: str) -> str:
    """Format generated daily timer labels into a more scannable heading."""
    import re

    clean = str(label or "").strip()
    match = re.match(
        r"^(?P<emoji>\S+)\s+"
        r"(?P<day>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2})\s+"
        r"(?:·\s+)?(?P<time>UTC\s+\d{2}-\d{2}|\d{2}:00-\d{2}:00 UTC)$",
        clean,
    )
    if match:
        time_label = match.group("time")
        legacy_match = re.match(r"(?P<start>\d{2}):00-(?P<end>\d{2}):00 UTC", time_label)
        if legacy_match:
            time_label = f"UTC {legacy_match.group('start')}-{legacy_match.group('end')}"
        return f"{match.group('emoji')} **{match.group('day')}** · `{time_label}`"
    return f"**{clean or 'Timer'}**"


def _availability_content_summary(headcount: int) -> str:
    keys = availability_recommendation_keys(headcount, limit=3)
    if not keys:
        return "Needs more availability"
    labels: list[str] = []
    for key in keys:
        et = EVENT_TYPES_BY_KEY.get(key)
        if et:
            labels.append(f"{et.emoji} {et.label}")
    return ", ".join(labels) if labels else "Needs more availability"


def _daily_timer_content_guide() -> str:
    return "\n".join([
        "`0` No content vote yet; the timer needs availability.",
        "`1` Solo-flexible: ⚔️ PvP, ⛏️ Gathering, or 💰 Economy planning.",
        "`2` Adds 🔥 Hellgates, 🌫️ Duo Mists, 🕳️ Abyssal Depths, 🐾 Tracking, and 🐂 Transport.",
        "`3-4` Adds 🗡️ Ganking, 🛡️ Small Scale, 🏴 Faction, 🛣️ Roads, and dungeon groups.",
        "`5-9` Adds 🗝️ Avalonian Dungeons, 👹 World Boss, and 🏟️ Crystal Arena.",
        "`10-14` Strong for open-world objective fights and larger faction groups.",
        "`15+` Adds ⚔️ ZvZ; lower-headcount content can still be voted for.",
        "The timer preview shows the top 3 options; the content vote can include up to 10 valid options.",
    ])


def _event_jump_url(db, event: dict) -> str | None:
    channel_id = str(event.get("channel_id") or "").strip()
    message_id = str(event.get("message_id") or "").strip()
    if not channel_id or not message_id:
        return None
    guild_id = (os.getenv("GUILD_DISCORD_ID") or "").strip()
    if not guild_id:
        try:
            db.cursor.execute(
                "SELECT guild_id FROM discord_channels WHERE channel_id = ? LIMIT 1",
                (channel_id,),
            )
            row = db.cursor.fetchone()
            if row:
                guild_id = str(row["guild_id"])
        except Exception:  # noqa: BLE001
            guild_id = ""
    if not guild_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _availability_claim_line(db, poll: dict, label: str) -> str | None:
    window = _availability_timer_window(poll, label)
    if not window:
        return None
    starts_at, ends_at = window
    try:
        overlaps = db.fetch_overlapping_prime_events(
            starts_at.isoformat(),
            ends_at.isoformat(),
        )
    except Exception:  # noqa: BLE001
        overlaps = []
    if not overlaps:
        return None
    event = overlaps[0]
    title = str(event.get("title") or "Claimed LFG").strip()
    url = _event_jump_url(db, event)
    linked = f"**[{title}]({url})**" if url else f"**{title}**"
    try:
        signed = len(db.fetch_lfg_signups(int(event["id"])))
    except Exception:  # noqa: BLE001
        signed = 0
    return f"LFG: {linked} · {signed} signed"


def _availability_window_groups(
    db, poll: dict, *, ranked: bool = False,
) -> tuple[list[str], list[str]]:
    slots = availability_slot_labels(poll)
    tallies = availability_tallies(db, poll["id"])
    rows = [(i, label, tallies.get(i, 0)) for i, label in enumerate(slots)]
    if ranked:
        rows.sort(key=lambda row: (-row[2], row[0]))

    open_lines: list[str] = []
    claimed_lines: list[str] = []
    for i, label, n in rows:
        local_note = _availability_local_time_note(poll, label)
        claim_line = _availability_claim_line(db, poll, label)
        heading = _availability_slot_heading(label)
        if claim_line:
            parts = [f"`#{i + 1}` {heading} · **claimed**"]
            if local_note:
                parts.append(f"Your time: {local_note}")
            parts.append(claim_line)
            claimed_lines.append("\n".join(parts))
            continue

        plan = _availability_content_summary(n)
        parts = [f"`#{i + 1}` {heading} · **{n} available**"]
        if local_note:
            parts.append(f"Your time: {local_note}")
        parts.append(f"Can run: {plan}" if n else plan)
        open_lines.append("\n".join(parts))
    return open_lines, claimed_lines


def _add_availability_fields(embed: discord.Embed, lines: list[str], name: str) -> None:
    if not lines:
        embed.add_field(name=name, value="_(misconfigured)_", inline=False)
        return
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        candidate = "\n\n".join([*current, line])
        if current and len(candidate) > 1000:
            chunks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append(current)
    for i, chunk in enumerate(chunks):
        field_name = name if i == 0 else f"{name} (cont.)"
        embed.add_field(name=field_name, value="\n\n".join(chunk)[:1024], inline=False)


def _add_availability_window_fields(
    embed: discord.Embed,
    open_lines: list[str],
    claimed_lines: list[str],
    *,
    open_name: str,
    claimed_name: str = "Claimed timers",
) -> None:
    if open_lines:
        _add_availability_fields(embed, open_lines, open_name)
    else:
        embed.add_field(name=open_name, value="No unclaimed timers are available.", inline=False)
    if claimed_lines:
        _add_availability_fields(embed, claimed_lines, claimed_name)


def availability_poll_embed(db, poll: dict) -> discord.Embed:
    ts = discord_ts_from_iso(poll["closes_at"])
    total = availability_total_voters(db, poll["id"])
    is_open = poll["status"] == "open"
    color = discord.Color.teal() if is_open else discord.Color.dark_gray()
    title = "🗓️ Availability Poll" + (" — open" if is_open else " — closed")
    poll_title = str(poll.get("title") or "Planned event")
    is_daily_timer_poll = poll_title.startswith("Daily Prime Timer Availability")
    desc = [
        f"**{poll_title}**",
    ]
    if is_daily_timer_poll:
        desc.append(
            "This is the planning step before the LFG. Pick every timer you could make; "
            "the bot will choose the strongest unclaimed timer, run a content vote, "
            "then post one signup."
        )
        desc.append("Claimed timers are skipped and shown separately with their LFG link.")
    else:
        desc.append("Pick every window you can attend. Discord converts **Your time** for each viewer.")
    if is_open:
        desc.append(f"Closes <t:{ts}:R> (<t:{ts}:f>).")
    embed = discord.Embed(title=title, description="\n".join(desc), color=color)
    if is_daily_timer_poll:
        embed.add_field(
            name="How content unlocks",
            value=_daily_timer_content_guide(),
            inline=False,
        )
    open_lines, claimed_lines = _availability_window_groups(db, poll)
    _add_availability_window_fields(
        embed,
        open_lines,
        claimed_lines,
        open_name="Open timers",
    )
    _apply_announcement_watermark(
        db, embed, f"Availability poll #{poll['id']} · {total} member(s) responded",
    )
    return embed


def availability_result_embed(db, poll: dict) -> discord.Embed:
    total = availability_total_voters(db, poll["id"])
    embed = discord.Embed(
        title="🧭 Availability Results",
        description=f"**{poll.get('title') or 'Planned event'}** · {total} member(s) responded.",
        color=discord.Color.green(),
    )
    open_lines, claimed_lines = _availability_window_groups(db, poll, ranked=True)
    _add_availability_window_fields(
        embed,
        open_lines,
        claimed_lines,
        open_name="Best unclaimed timers",
        claimed_name="Claimed timers skipped",
    )
    _apply_announcement_watermark(db, embed, f"Availability poll #{poll['id']} result")
    return embed


def board_embed(db) -> discord.Embed:
    """Persistent dashboard embed posted in the configured board channel."""
    poll = fetch_open_poll(db)
    qp = fetch_open_quickpoll(db)
    ap = fetch_open_availability_poll(db)
    pending = fetch_pending_suggestions(db)
    embed = discord.Embed(
        title="🎯 Content Curator",
        description=(
            "Help pick what the guild plays. **Anyone** can suggest events; "
            "everyone can vote when a poll opens."
        ),
        color=discord.Color.blurple(),
    )
    if poll:
        ts = discord_ts_from_iso(poll["closes_at"])
        sugg_count = len(fetch_poll_suggestions(db, poll["id"]))
        embed.add_field(
            name="📊 Weekly poll — open",
            value=f"Poll #{poll['id']} · **{sugg_count}** option(s)\nCloses <t:{ts}:R>",
            inline=False,
        )
    else:
        embed.add_field(
            name="📊 Weekly poll",
            value=f"Not running. **{len(pending)}** suggestion(s) queued for the next one.",
            inline=False,
        )
    if qp:
        ts = discord_ts_from_iso(qp["closes_at"])
        total = quickpoll_total_votes(db, qp["id"])
        embed.add_field(
            name="🎯 Quick-vote — live",
            value=f"Quickpoll #{qp['id']} · **{total}** vote(s) so far · closes <t:{ts}:R>",
            inline=False,
        )
    if ap:
        ts = discord_ts_from_iso(ap["closes_at"])
        total = availability_total_voters(db, ap["id"])
        embed.add_field(
            name="🗓️ Availability poll — live",
            value=f"Poll #{ap['id']} · **{total}** member(s) responded · closes <t:{ts}:R>",
            inline=False,
        )
    embed.add_field(
        name="How it works",
        value=(
            "• **Suggest event** — propose an activity for the next poll\n"
            "• **View pool** — see what's been suggested\n"
            "• **Live poll** — jump to the running poll embed\n"
            "• Officers can open/close polls and start a quick CoD-style next-activity vote"
        ),
        inline=False,
    )
    embed.set_footer(text="Click a button below to get started")
    return embed


# ── weekly-poll vote view ───────────────────────────────────────────────────
class _VoteSelect(discord.ui.Select):
    def __init__(self, bot, poll_id: int, options: list[discord.SelectOption], max_values: int):
        self.bot: Bot = bot
        self.poll_id = poll_id
        super().__init__(
            custom_id=f"content:vote:{poll_id}",
            placeholder="Pick the events you want to play (multi-select)…",
            min_values=0,
            max_values=max(1, max_values),
            options=options or [discord.SelectOption(label="No suggestions yet", value="__none__")],
            disabled=not options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        poll = fetch_open_poll(db)
        if not poll or poll["id"] != self.poll_id:
            await interaction.response.send_message(
                embed=info_embed("Poll closed", "This poll is no longer accepting votes."),
                ephemeral=True,
            )
            return
        try:
            picks = [int(v) for v in self.values if v != "__none__"]
        except (TypeError, ValueError):
            picks = []
        set_user_votes(db, poll["id"], str(interaction.user.id), picks)

        try:
            await interaction.response.edit_message(embed=poll_embed(db, poll))
        except (discord.NotFound, discord.HTTPException):
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
        msg = f"Saved **{len(picks)}** vote(s)." if picks else "Cleared your votes."
        try:
            await interaction.followup.send(embed=success_embed("Voted", msg), ephemeral=True)
        except discord.HTTPException:
            pass


class ContentPollView(discord.ui.View):
    """Persistent view: a single multi-select with one option per suggestion."""

    def __init__(self, bot, poll_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.poll_id = poll_id
        suggestions = fetch_poll_suggestions(bot.db, poll_id)[:25]
        opts: list[discord.SelectOption] = []
        for i, s in enumerate(suggestions, 1):
            et = EVENT_TYPES_BY_KEY.get(s["event_type"])
            emoji = et.emoji if et else None
            label = f"#{i} {s['title']}"[:100]
            desc = (et.label if et else s["event_type"])[:100]
            try:
                opts.append(
                    discord.SelectOption(
                        label=label, description=desc, value=str(s["id"]), emoji=emoji,
                    )
                )
            except (TypeError, ValueError):
                opts.append(discord.SelectOption(label=label, description=desc, value=str(s["id"])))
        self.add_item(_VoteSelect(bot, poll_id, opts, max_values=len(opts) or 1))


# ── weekly-poll lifecycle ───────────────────────────────────────────────────
async def open_poll(bot, channel: discord.abc.Messageable) -> Optional[dict]:
    """Open a new poll using whatever pending suggestions exist."""
    db = bot.db
    if fetch_open_poll(db):
        return None

    duration_h = cfg_int(db, CFG_DURATION_HOURS, 48)
    opened = now_utc()
    closes = opened + dt.timedelta(hours=max(1, duration_h))

    db.cursor.execute(
        "INSERT INTO content_polls (opened_at, closes_at, status, channel_id) "
        "VALUES (?, ?, 'open', ?)",
        (opened.isoformat(), closes.isoformat(), str(getattr(channel, "id", "") or "")),
    )
    db.connection.commit()
    poll_id = int(db.cursor.lastrowid or 0)
    if not poll_id:
        return None

    db.cursor.execute(
        "UPDATE content_suggestions SET poll_id = ? WHERE poll_id IS NULL",
        (poll_id,),
    )
    db.connection.commit()
    poll = {
        "id": poll_id, "opened_at": opened.isoformat(), "closes_at": closes.isoformat(),
        "status": "open", "channel_id": str(getattr(channel, "id", "") or ""),
    }

    try:
        msg = await channel.send(embed=poll_embed(db, poll), view=ContentPollView(bot, poll_id))
    except discord.HTTPException as exc:
        error_log(f"content-curator: couldn't post poll #{poll_id}: {exc!r}")
        return poll

    db.cursor.execute(
        "UPDATE content_polls SET message_id = ? WHERE id = ?", (str(msg.id), poll_id),
    )
    db.connection.commit()
    poll["message_id"] = str(msg.id)
    info_log(f"content-curator: opened poll #{poll_id} (closes {closes.isoformat()} UTC).")
    return poll


def _next_event_starts(base: dt.datetime, event_hour: int, index: int) -> tuple[dt.datetime, dt.datetime]:
    """Return (starts_at, ends_at) for the nth winner.

    Picks ``base.date() + 1``, then increments by 2 days per winner, anchored
    at ``event_hour`` UTC. Keeps things spaced out across the week.
    """
    start_date = (base + dt.timedelta(days=1 + index * 2)).date()
    starts_at = dt.datetime.combine(start_date, dt.time(hour=event_hour, minute=0))
    return starts_at, starts_at + dt.timedelta(minutes=90)


async def _auto_create_lfg(bot, winners: list[dict]) -> list[int]:
    """Create LFG events for each winner. Returns list of created event_ids."""
    db = bot.db
    if not cfg_int(db, CFG_AUTO_LFG, 1):
        return []
    chan_id = db.get_config(CFG_LFG_CHANNEL)
    if not chan_id:
        info_log("content-curator: skipping auto-LFG (no lfg_post_channel_id set).")
        return []
    channel = bot.get_channel(int(chan_id)) if chan_id else None
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, ValueError):
            channel = None
    if channel is None:
        error_log("content-curator: LFG channel id set but channel unreachable.")
        return []

    event_hour = cfg_int(db, CFG_EVENT_HOUR, 20)
    duration_min = cfg_int(db, CFG_EVENT_DURATION, 90)
    base = now_utc()

    # Lazy import to avoid LFG cog import at module load.
    try:
        from cogs._lfg_views import (
            EventSignupView,
            _create_discord_scheduled_event,
            _create_lfg_discussion_thread,
            _format_event_embed,
        )
    except Exception as exc:  # noqa: BLE001
        error_log(f"content-curator: cannot import LFG helpers: {exc!r}")
        return []

    created: list[int] = []
    for i, s in enumerate(winners):
        starts_at, _ends = _next_event_starts(base, event_hour, i)
        ends_at = starts_at + dt.timedelta(minutes=duration_min)
        slot_label = starts_at.strftime("%a %H:%M UTC")
        event_id = db.create_lfg_event(
            slot_label=slot_label,
            is_prime=False,
            title=str(s["title"])[:80],
            description=(
                f"Community-picked event from poll #{s['poll_id']} "
                f"({s['vote_count']} vote(s)).\n"
                + (f"Suggested notes: {s['notes']}\n" if s.get('notes') else "")
            ).strip(),
            comp_notes="",
            starts_at=starts_at.isoformat(),
            ends_at=ends_at.isoformat(),
            prep_minutes=PREP_MINUTES,
            review_minutes=REVIEW_MINUTES,
            creator_id=str(s["suggester_id"]),
            event_type=s["event_type"],
        )
        if not event_id:
            continue
        event = db.fetch_lfg_event(event_id)
        try:
            msg = await channel.send(
                embed=_format_event_embed(db, event), view=EventSignupView(event_id),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            try:
                db.delete_lfg_event(event_id)
            except Exception:  # noqa: BLE001
                pass
            error_log(f"content-curator: failed to post LFG #{event_id}: {exc!r}")
            continue
        db.set_lfg_message(event_id, str(channel.id), str(msg.id))
        await _create_lfg_discussion_thread(db, event, msg)

        if getattr(channel, "guild", None) is not None:
            try:
                scheduled = await _create_discord_scheduled_event(
                    channel.guild,
                    name=event["title"],
                    description=(event.get("description") or "") + f"\n\nSign up: {msg.jump_url}",
                    starts_at=starts_at,
                    ends_at=ends_at,
                    location=msg.jump_url,
                )
                if scheduled is not None:
                    db.set_lfg_scheduled_event_id(event_id, str(scheduled.id))
            except Exception as exc:  # noqa: BLE001
                error_log(f"content-curator: scheduled-event creation failed: {exc!r}")

        created.append(event_id)
    return created


async def close_poll(bot, poll: dict) -> dict:
    """Tally, announce, optionally create LFGs, archive."""
    db = bot.db
    suggestions = fetch_poll_suggestions(db, poll["id"])
    top_n = max(1, cfg_int(db, CFG_TOP_N, 3))
    winners = [s for s in suggestions if s["vote_count"] > 0][:top_n]

    created_ids: list[int] = []
    if winners:
        created_ids = await _auto_create_lfg(bot, winners)

    db.cursor.execute(
        "UPDATE content_polls SET status = 'closed', closed_at = ?, winners = ? WHERE id = ?",
        (
            now_utc().isoformat(),
            ", ".join(f"#{s['id']}({s['vote_count']})" for s in winners),
            poll["id"],
        ),
    )
    db.connection.commit()

    chan_id = poll.get("channel_id")
    msg_id = poll.get("message_id")
    if chan_id and msg_id:
        try:
            ch = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
            msg = await ch.fetch_message(int(msg_id))
            poll_closed = dict(poll, status="closed")
            await msg.edit(embed=poll_embed(db, poll_closed), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass

    from cogs._content_config import CFG_ANNOUNCE_CHANNEL
    announce_id = db.get_config(CFG_ANNOUNCE_CHANNEL) or chan_id
    if announce_id and winners:
        try:
            ch = bot.get_channel(int(announce_id)) or await bot.fetch_channel(int(announce_id))
            await ch.send(embed=winners_embed(db, poll, winners, created_ids))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass

    info_log(
        f"content-curator: closed poll #{poll['id']} "
        f"winners={[s['id'] for s in winners]} lfg_events={created_ids}"
    )
    return {"poll_id": poll["id"], "winners": winners, "created_lfg": created_ids}


# ── quickpoll view + lifecycle ──────────────────────────────────────────────
class _QuickVoteSelect(discord.ui.Select):
    def __init__(self, bot, poll_id: int, keys: list[str]):
        self.bot = bot
        self.poll_id = poll_id
        opts: list[discord.SelectOption] = []
        for k in keys[:25]:
            et = EVENT_TYPES_BY_KEY.get(k)
            if not et:
                continue
            try:
                opts.append(
                    discord.SelectOption(
                        label=et.label[:100],
                        description=f"Needs min {activity_min(k)} player(s)"[:100],
                        value=et.key,
                        emoji=et.emoji,
                    )
                )
            except (TypeError, ValueError):
                opts.append(
                    discord.SelectOption(
                        label=et.label[:100],
                        description=f"Needs min {activity_min(k)} player(s)"[:100],
                        value=et.key,
                    )
                )
        super().__init__(
            custom_id=f"{QUICKVOTE_PREFIX}{poll_id}",
            placeholder="Pick the activity you'd join next…",
            min_values=1,
            max_values=1,
            options=opts or [discord.SelectOption(label="—", value="__none__")],
            disabled=not opts,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        poll = fetch_quickpoll(db, self.poll_id)
        if not poll or poll["status"] != "open":
            await interaction.response.send_message(
                embed=info_embed("Vote closed", "This quickpoll is no longer accepting votes."),
                ephemeral=True,
            )
            return
        pick = self.values[0] if self.values else None
        if not pick or pick == "__none__" or pick not in EVENT_TYPES_BY_KEY:
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
            return
        if pick not in quickpoll_option_keys(poll):
            await interaction.response.send_message(
                embed=error_embed("Invalid option", "That activity isn't on this poll."),
                ephemeral=True,
            )
            return
        cast_quickvote(db, poll["id"], str(interaction.user.id), pick)
        try:
            await interaction.response.edit_message(embed=quickpoll_embed(db, poll))
        except (discord.NotFound, discord.HTTPException):
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
        et = EVENT_TYPES_BY_KEY[pick]
        try:
            await interaction.followup.send(
                embed=success_embed(
                    "Vote in",
                    f"You're counted for {et.emoji} **{et.label}**. "
                    "You can change your pick before the poll closes.",
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            pass


class ContentQuickPollView(discord.ui.View):
    """Persistent CoD-style next-activity vote view."""

    def __init__(self, bot, poll_id: int, keys: list[str]):
        super().__init__(timeout=None)
        self.bot = bot
        self.poll_id = poll_id
        self.add_item(_QuickVoteSelect(bot, poll_id, keys))


async def open_quickpoll(
    bot,
    channel: discord.abc.Messageable,
    option_keys: list[str],
    duration_min: int,
    lead_min: int,
    lfg_duration_min: int,
    creator_id: str,
    target_starts_at: dt.datetime | None = None,
    target_ends_at: dt.datetime | None = None,
    target_slot_label: str | None = None,
    target_is_prime: bool = False,
    content: str | None = None,
) -> Optional[dict]:
    db = bot.db
    if fetch_open_quickpoll(db):
        return None
    keys = [k for k in option_keys if k in EVENT_TYPES_BY_KEY][:25]
    if not keys:
        return None
    opened = now_utc()
    closes = opened + dt.timedelta(minutes=max(1, duration_min))
    target_start_iso = (
        utc_dt_for_discord(target_starts_at).isoformat()
        if target_starts_at is not None else None
    )
    target_end_iso = (
        utc_dt_for_discord(target_ends_at).isoformat()
        if target_ends_at is not None else None
    )
    db.cursor.execute(
        "INSERT INTO content_quickpolls "
        "(opened_at, closes_at, channel_id, status, options, creator_id, "
        " lead_minutes, duration_minutes, target_starts_at, target_ends_at, "
        " target_slot_label, target_is_prime) "
        "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            opened.isoformat(), closes.isoformat(),
            str(getattr(channel, "id", "") or ""),
            ",".join(keys), str(creator_id),
            int(max(0, lead_min)), int(max(15, lfg_duration_min)),
            target_start_iso, target_end_iso,
            (target_slot_label or "")[:80] if target_slot_label else None,
            1 if target_is_prime else 0,
        ),
    )
    db.connection.commit()
    poll_id = int(db.cursor.lastrowid or 0)
    if not poll_id:
        return None
    poll = fetch_quickpoll(db, poll_id) or {}
    try:
        msg = await channel.send(
            content=content if content is not None else "@here 🎯 Vote for the next activity!",
            embed=quickpoll_embed(db, poll),
            view=ContentQuickPollView(bot, poll_id, keys),
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
    except discord.Forbidden:
        try:
            msg = await channel.send(
                embed=quickpoll_embed(db, poll),
                view=ContentQuickPollView(bot, poll_id, keys),
            )
        except discord.HTTPException as exc:
            error_log(f"content-curator: quickpoll #{poll_id} post failed: {exc!r}")
            return poll
    except discord.HTTPException as exc:
        error_log(f"content-curator: quickpoll #{poll_id} post failed: {exc!r}")
        return poll
    db.cursor.execute(
        "UPDATE content_quickpolls SET message_id = ? WHERE id = ?",
        (str(msg.id), poll_id),
    )
    db.connection.commit()
    poll["message_id"] = str(msg.id)
    info_log(
        f"content-curator: opened quickpoll #{poll_id} "
        f"options={keys} closes={closes.isoformat()}"
    )
    return poll


async def _quickpoll_create_lfg(bot, poll: dict, winner_key: str, headcount: int) -> Optional[int]:
    """Create an LFG event for the winning activity. Returns event_id or None."""
    db = bot.db
    if not cfg_int(db, CFG_AUTO_LFG, 1):
        return None
    chan_id = db.get_config(CFG_LFG_CHANNEL)
    if not chan_id:
        info_log("content-curator: quickpoll — no LFG channel configured.")
        return None
    channel = bot.get_channel(int(chan_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, ValueError):
            channel = None
    if channel is None:
        error_log("content-curator: quickpoll — LFG channel unreachable.")
        return None

    try:
        from cogs._lfg_views import (
            EventSignupView,
            _create_discord_scheduled_event,
            _create_lfg_discussion_thread,
            _format_event_embed,
        )
    except Exception as exc:  # noqa: BLE001
        error_log(f"content-curator: quickpoll — cannot import LFG helpers: {exc!r}")
        return None

    et = EVENT_TYPES_BY_KEY[winner_key]
    lead_min = int(poll.get("lead_minutes") or 15)
    dur_min = int(poll.get("duration_minutes") or 90)
    target_start = poll.get("target_starts_at")
    target_end = poll.get("target_ends_at")
    if target_start:
        starts_at = utc_dt_for_discord(dt.datetime.fromisoformat(str(target_start)))
        if target_end:
            ends_at = utc_dt_for_discord(dt.datetime.fromisoformat(str(target_end)))
        else:
            ends_at = starts_at + dt.timedelta(minutes=max(15, dur_min))
        slot_label = str(poll.get("target_slot_label") or starts_at.strftime("%a %H:%M UTC"))
        is_prime = int(poll.get("target_is_prime") or 0) == 1
    else:
        starts_at = utc_dt_for_discord(now_utc() + dt.timedelta(minutes=max(0, lead_min)))
        ends_at = starts_at + dt.timedelta(minutes=max(15, dur_min))
        slot_label = starts_at.strftime("%a %H:%M UTC")
        is_prime = False

    if is_prime:
        overlap = db.fetch_overlapping_prime_events(starts_at.isoformat(), ends_at.isoformat())
        if overlap:
            names = ", ".join(f"#{e['id']} {e['title']!r}" for e in overlap[:3])
            error_log(
                "content-curator: quickpoll winner would double-book prime "
                f"{display_slot_label(slot_label)}: {names}"
            )
            return None

    event_id = db.create_lfg_event(
        slot_label=slot_label,
        is_prime=is_prime,
        title=f"{et.label} ({'daily timer pick' if is_prime else 'community pick'})",
        description=(
            f"Voted in via /content nextvote with **{headcount}** interested player(s).\n"
            f"Minimum needed: {activity_min(winner_key)}."
            + (
                f"\nTimer: **{display_slot_label(slot_label)}**."
                if is_prime else ""
            )
        ),
        comp_notes="",
        starts_at=starts_at.isoformat(),
        ends_at=ends_at.isoformat(),
        prep_minutes=PREP_MINUTES,
        review_minutes=REVIEW_MINUTES,
        creator_id=str(poll.get("creator_id") or ""),
        event_type=winner_key,
    )
    if not event_id:
        return None

    event = db.fetch_lfg_event(event_id)
    try:
        msg = await channel.send(
            embed=_format_event_embed(db, event), view=EventSignupView(event_id),
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        try:
            db.delete_lfg_event(event_id)
        except Exception:  # noqa: BLE001
            pass
        error_log(f"content-curator: quickpoll — failed to post LFG #{event_id}: {exc!r}")
        return None
    db.set_lfg_message(event_id, str(channel.id), str(msg.id))
    await _create_lfg_discussion_thread(db, event, msg)

    if getattr(channel, "guild", None) is not None:
        try:
            scheduled = await _create_discord_scheduled_event(
                channel.guild,
                name=event["title"],
                description=(event.get("description") or "") + f"\n\nSign up: {msg.jump_url}",
                starts_at=starts_at,
                ends_at=ends_at,
                location=msg.jump_url,
            )
            if scheduled is not None:
                db.set_lfg_scheduled_event_id(event_id, str(scheduled.id))
        except Exception as exc:  # noqa: BLE001
            error_log(f"content-curator: quickpoll — scheduled-event creation failed: {exc!r}")
    if is_prime:
        try:
            from cogs._primetime_claims import refresh_prime_claim_trackers

            await refresh_prime_claim_trackers(bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"content-curator: prime dashboard refresh failed: {exc!r}")
    return event_id


async def close_quickpoll(bot, poll: dict) -> dict:
    db = bot.db
    tallies = quickpoll_tallies(db, poll["id"])
    candidates = [
        (k, n) for k, n in tallies.items()
        if n >= activity_min(k) and k in EVENT_TYPES_BY_KEY
    ]
    candidates.sort(key=lambda kv: (-kv[1], kv[0]))
    winner_key: Optional[str] = candidates[0][0] if candidates else None

    lfg_event_id: Optional[int] = None
    if winner_key:
        try:
            lfg_event_id = await _quickpoll_create_lfg(bot, poll, winner_key, tallies[winner_key])
        except Exception as exc:  # noqa: BLE001
            error_log(f"content-curator: quickpoll LFG creation failed: {exc!r}")

    db.cursor.execute(
        "UPDATE content_quickpolls SET status = 'closed', closed_at = ?, "
        "winner_event_type = ?, lfg_event_id = ? WHERE id = ?",
        (now_utc().isoformat(), winner_key, lfg_event_id, poll["id"]),
    )
    db.connection.commit()
    funnel = fetch_daily_timer_funnel_by_quickpoll(db, poll["id"])
    if funnel:
        update_daily_timer_funnel(
            db,
            int(funnel["id"]),
            {
                "lfg_event_id": lfg_event_id,
                "status": "lfg_created" if lfg_event_id else "skipped",
                "closed_at": now_utc().isoformat(),
            },
        )

    chan_id = poll.get("channel_id")
    msg_id = poll.get("message_id")
    if chan_id and msg_id:
        try:
            ch = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
            msg = await ch.fetch_message(int(msg_id))
            closed_poll = dict(poll, status="closed")
            await msg.edit(embed=quickpoll_embed(db, closed_poll), view=None)
            await ch.send(embed=quickpoll_result_embed(db, closed_poll, winner_key, lfg_event_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass

    info_log(
        f"content-curator: closed quickpoll #{poll['id']} "
        f"winner={winner_key} lfg_event_id={lfg_event_id} tallies={tallies}"
    )
    return {
        "poll_id": poll["id"], "winner": winner_key,
        "lfg_event_id": lfg_event_id, "tallies": tallies,
    }


# ── availability-poll view + lifecycle ─────────────────────────────────────
class _AvailabilitySelect(discord.ui.Select):
    def __init__(self, bot, poll_id: int, slots: list[str]):
        self.bot = bot
        self.poll_id = poll_id
        opts = [
            discord.SelectOption(
                label=label[:100],
                value=str(i),
                description="I can make this window",
            )
            for i, label in enumerate(slots[:25])
        ]
        super().__init__(
            custom_id=f"content:availability:{poll_id}",
            placeholder="Pick every time window you can attend…",
            min_values=0,
            max_values=max(1, len(opts)),
            options=opts or [discord.SelectOption(label="—", value="__none__")],
            disabled=not opts,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        poll = fetch_availability_poll(db, self.poll_id)
        if not poll or poll["status"] != "open":
            await interaction.response.send_message(
                embed=info_embed("Poll closed", "This availability poll is no longer accepting responses."),
                ephemeral=True,
            )
            return
        picks: list[int] = []
        for value in self.values:
            try:
                picks.append(int(value))
            except (TypeError, ValueError):
                continue
        set_availability_votes(db, poll["id"], str(interaction.user.id), picks)
        try:
            await interaction.response.edit_message(embed=availability_poll_embed(db, poll))
        except (discord.NotFound, discord.HTTPException):
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
        msg = f"Saved **{len(picks)}** available window(s)." if picks else "Cleared your availability."
        try:
            await interaction.followup.send(embed=success_embed("Availability saved", msg), ephemeral=True)
        except discord.HTTPException:
            pass


class ContentAvailabilityPollView(discord.ui.View):
    """Persistent multi-select availability poll view."""

    def __init__(self, bot, poll_id: int, slots: list[str]):
        super().__init__(timeout=None)
        self.bot = bot
        self.poll_id = poll_id
        self.add_item(_AvailabilitySelect(bot, poll_id, slots))


async def open_availability_poll(
    bot,
    channel: discord.abc.Messageable,
    *,
    title: str,
    slots: list[str],
    duration_min: int,
    creator_id: str,
    content: str | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> Optional[dict]:
    db = bot.db
    if fetch_open_availability_poll(db):
        return None
    clean_slots = [s.strip()[:90] for s in slots if s.strip()][:25]
    if not clean_slots:
        return None

    opened = now_utc()
    closes = opened + dt.timedelta(minutes=max(5, int(duration_min)))
    db.cursor.execute(
        "INSERT INTO content_availability_polls "
        "(title, opened_at, closes_at, channel_id, status, options, creator_id) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (
            title.strip()[:120] or "Special event",
            opened.isoformat(),
            closes.isoformat(),
            str(getattr(channel, "id", "") or ""),
            json.dumps(clean_slots),
            str(creator_id),
        ),
    )
    db.connection.commit()
    poll_id = int(db.cursor.lastrowid or 0)
    if not poll_id:
        return None
    poll = fetch_availability_poll(db, poll_id) or {}
    try:
        msg = await channel.send(
            content=content,
            embed=availability_poll_embed(db, poll),
            view=ContentAvailabilityPollView(bot, poll_id, clean_slots),
            allowed_mentions=allowed_mentions,
        )
    except discord.HTTPException as exc:
        error_log(f"content-curator: availability poll #{poll_id} post failed: {exc!r}")
        return poll
    db.cursor.execute(
        "UPDATE content_availability_polls SET message_id = ? WHERE id = ?",
        (str(msg.id), poll_id),
    )
    db.connection.commit()
    poll["message_id"] = str(msg.id)
    info_log(
        f"content-curator: opened availability poll #{poll_id} "
        f"slots={len(clean_slots)} closes={closes.isoformat()}"
    )
    return poll


async def close_availability_poll(bot, poll: dict) -> dict:
    db = bot.db
    db.cursor.execute(
        "UPDATE content_availability_polls SET status = 'closed', closed_at = ? WHERE id = ?",
        (now_utc().isoformat(), poll["id"]),
    )
    db.connection.commit()

    closed_poll = dict(poll, status="closed")
    chan_id = poll.get("channel_id")
    msg_id = poll.get("message_id")
    if chan_id and msg_id:
        try:
            ch = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
            msg = await ch.fetch_message(int(msg_id))
            await msg.edit(embed=availability_poll_embed(db, closed_poll), view=None)
            await ch.send(embed=availability_result_embed(db, closed_poll))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass

    tallies = availability_tallies(db, poll["id"])
    info_log(f"content-curator: closed availability poll #{poll['id']} tallies={tallies}")
    return {"poll_id": poll["id"], "tallies": tallies}


# ── dashboard suggest flow ──────────────────────────────────────────────────
class _SuggestModal(discord.ui.Modal):
    """Modal opened after the user picks an event type from the suggest menu."""

    title_input = discord.ui.TextInput(
        label="Title",
        placeholder="e.g. Friday night ZvZ in Arthur's Rest",
        min_length=3,
        max_length=80,
        required=True,
    )
    notes_input = discord.ui.TextInput(
        label="Notes (optional)",
        placeholder="Meeting spot, comp, expectations…",
        style=discord.TextStyle.paragraph,
        max_length=300,
        required=False,
    )

    def __init__(self, bot, event_type_key: str):
        self.bot = bot
        self.event_type_key = event_type_key
        et = EVENT_TYPES_BY_KEY.get(event_type_key)
        label = (et.label if et else event_type_key)[:40]
        super().__init__(title=f"Suggest: {label}", timeout=600)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        et = EVENT_TYPES_BY_KEY.get(self.event_type_key)
        if not et:
            await interaction.response.send_message(
                embed=error_embed("Unknown event type", "Try again from the dashboard."),
                ephemeral=True,
            )
            return
        clean_title = str(self.title_input.value or "").strip()
        if not (3 <= len(clean_title) <= 80):
            await interaction.response.send_message(
                embed=error_embed("Check the title", "Title must be 3–80 characters."),
                ephemeral=True,
            )
            return
        clean_notes = (str(self.notes_input.value or "").strip()) or None

        db = self.bot.db
        poll = fetch_open_poll(db)
        poll_id = poll["id"] if poll else None
        max_per = cfg_int(db, CFG_MAX_PER_USER, 3)
        used = count_user_suggestions(db, poll_id, str(interaction.user.id))
        if used >= max_per:
            await interaction.response.send_message(
                embed=info_embed(
                    "Suggestion limit reached",
                    f"You've already submitted **{used}** suggestion(s) "
                    f"({'this poll' if poll else 'for the next poll'}). "
                    f"Limit is **{max_per}**.",
                ),
                ephemeral=True,
            )
            return

        db.cursor.execute(
            "INSERT INTO content_suggestions "
            "(poll_id, suggester_id, event_type, title, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (poll_id, str(interaction.user.id), et.key, clean_title, clean_notes, now_utc().isoformat()),
        )
        db.connection.commit()
        sid = int(db.cursor.lastrowid or 0)

        await interaction.response.send_message(
            embed=success_embed(
                f"Suggestion #{sid} added",
                f"{et.emoji} **{clean_title}** ({et.label}) "
                + ("queued for the next poll." if poll_id is None else "is now live on the current poll."),
            ),
            ephemeral=True,
        )

        if poll and poll.get("channel_id") and poll.get("message_id"):
            try:
                ch = self.bot.get_channel(int(poll["channel_id"])) or await self.bot.fetch_channel(int(poll["channel_id"]))
                msg = await ch.fetch_message(int(poll["message_id"]))
                await msg.edit(embed=poll_embed(db, poll), view=ContentPollView(self.bot, poll["id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass


class _SuggestPickSelect(discord.ui.Select):
    """Ephemeral select shown when a user clicks 'Suggest event'."""

    def __init__(self, bot):
        self.bot = bot
        opts: list[discord.SelectOption] = []
        for k in SUGGEST_PICK_KEYS:
            et = EVENT_TYPES_BY_KEY.get(k)
            if not et:
                continue
            try:
                opts.append(discord.SelectOption(
                    label=et.label[:100], value=et.key, emoji=et.emoji,
                    description=et.category[:100],
                ))
            except (TypeError, ValueError):
                opts.append(discord.SelectOption(
                    label=et.label[:100], value=et.key, description=et.category[:100],
                ))
        super().__init__(
            placeholder="Pick the event type…", min_values=1, max_values=1,
            options=opts[:25],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_SuggestModal(self.bot, self.values[0]))


class _SuggestPickView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.add_item(_SuggestPickSelect(bot))


# ── dashboard view ──────────────────────────────────────────────────────────
class ContentBoardView(discord.ui.View):
    """Persistent dashboard panel for content curator."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _officer_check(self, interaction: discord.Interaction) -> bool:
        if (
            isinstance(interaction.user, discord.Member)
            and is_officer(interaction.user, self.bot.db)
        ):
            return True
        await interaction.response.send_message(
            embed=error_embed("Officer only", "This button is officer/admin only."),
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="Suggest event", emoji="💡",
        style=discord.ButtonStyle.success,
        custom_id="content:board:suggest", row=0,
    )
    async def suggest_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        await interaction.response.send_message(
            content="Pick the event type, then fill in the details:",
            view=_SuggestPickView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="View pool", emoji="📥",
        style=discord.ButtonStyle.secondary,
        custom_id="content:board:pool", row=0,
    )
    async def pool_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        db = self.bot.db
        poll = fetch_open_poll(db)
        rows = fetch_poll_suggestions(db, poll["id"]) if poll else fetch_pending_suggestions(db)
        title = (f"📊 Live Poll #{poll['id']}" if poll else "📥 Suggestion Pool (next poll)")
        desc = (
            f"Closes <t:{discord_ts_from_iso(poll['closes_at'])}:R>"
            if poll else f"{len(rows)} suggestion(s) queued."
        )
        embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
        if not rows:
            embed.add_field(name="Empty", value="Click **Suggest event** to add one.", inline=False)
        else:
            lines = []
            for i, s in enumerate(rows[:25], 1):
                et = EVENT_TYPES_BY_KEY.get(s["event_type"])
                emoji = et.emoji if et else "📌"
                label = et.label if et else s["event_type"]
                votes_part = f" · **{s['vote_count']}** vote(s)" if poll else ""
                lines.append(
                    f"`#{i}` {emoji} **{s['title']}** ({label}){votes_part}\n"
                    f"   by <@{s['suggester_id']}>"
                )
            embed.add_field(name="Suggestions", value="\n".join(lines)[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Live poll", emoji="🔗",
        style=discord.ButtonStyle.secondary,
        custom_id="content:board:live", row=0,
    )
    async def live_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        db = self.bot.db
        poll = fetch_open_poll(db)
        if not poll:
            await interaction.response.send_message(
                embed=info_embed("No active poll", "Nothing to jump to right now."),
                ephemeral=True,
            )
            return
        chan_id = poll.get("channel_id")
        msg_id = poll.get("message_id")
        if not chan_id or not msg_id:
            await interaction.response.send_message(
                embed=info_embed("No poll message", "The poll exists but no message was posted."),
                ephemeral=True,
            )
            return
        guild_id = interaction.guild_id or 0
        url = f"https://discord.com/channels/{guild_id}/{chan_id}/{msg_id}"
        await interaction.response.send_message(
            embed=info_embed("Live poll", f"[Jump to the poll →]({url})"),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Open weekly poll", emoji="📊",
        style=discord.ButtonStyle.primary,
        custom_id="content:board:open", row=1,
    )
    async def open_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._officer_check(interaction):
            return
        db = self.bot.db
        if fetch_open_poll(db):
            await interaction.response.send_message(
                embed=info_embed("Already open", "A poll is already running."),
                ephemeral=True,
            )
            return
        chan_id = db.get_config(CFG_CHANNEL)
        if not chan_id:
            await interaction.response.send_message(
                embed=error_embed("No channel configured", "Run `/content config channel:#x` first."),
                ephemeral=True,
            )
            return
        ch = self.bot.get_channel(int(chan_id))
        if ch is None:
            await interaction.response.send_message(
                embed=error_embed("Channel unreachable", "Bot can't see the configured channel."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        poll = await open_poll(self.bot, ch)
        if poll:
            await interaction.followup.send(
                embed=success_embed("Poll opened", f"Poll #{poll['id']} is live in {ch.mention}."),
                ephemeral=True,
            )
            await refresh_board_message(self.bot)
        else:
            await interaction.followup.send(
                embed=error_embed("Failed", "Couldn't open the poll. Check logs."),
                ephemeral=True,
            )

    @discord.ui.button(
        label="Close poll", emoji="🏁",
        style=discord.ButtonStyle.danger,
        custom_id="content:board:close", row=1,
    )
    async def close_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._officer_check(interaction):
            return
        db = self.bot.db
        poll = fetch_open_poll(db)
        if not poll:
            await interaction.response.send_message(
                embed=info_embed("No active poll", "Nothing to close."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await close_poll(self.bot, poll)
        await interaction.followup.send(
            embed=success_embed(
                "Poll closed",
                f"Poll #{result['poll_id']} closed with **{len(result['winners'])}** winner(s). "
                f"Auto-created **{len(result['created_lfg'])}** LFG event(s).",
            ),
            ephemeral=True,
        )
        await refresh_board_message(self.bot)

    @discord.ui.button(
        label="Next-activity vote", emoji="🎯",
        style=discord.ButtonStyle.primary,
        custom_id="content:board:nextvote", row=1,
    )
    async def nextvote_btn(self, interaction: discord.Interaction, _btn: discord.ui.Button) -> None:
        if not await self._officer_check(interaction):
            return
        db = self.bot.db
        if fetch_open_quickpoll(db):
            await interaction.response.send_message(
                embed=info_embed("Already open", "A quickpoll is already live."),
                ephemeral=True,
            )
            return
        keys = list(DEFAULT_QUICKVOTE_KEYS)
        dur = cfg_int(db, CFG_QUICKVOTE_DURATION_MIN, 10)
        lead = cfg_int(db, CFG_QUICKVOTE_LEAD_MIN, 15)
        lfg_dur = cfg_int(db, CFG_QUICKVOTE_DURATION_LFG, 90)
        ch = interaction.channel
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                embed=error_embed("Wrong channel", "Use this button in a text channel."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        poll = await open_quickpoll(
            self.bot, ch, keys,
            duration_min=dur, lead_min=lead, lfg_duration_min=lfg_dur,
            creator_id=str(interaction.user.id),
        )
        if not poll:
            await interaction.followup.send(
                embed=error_embed("Failed", "Couldn't open the quickpoll."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "Quickpoll started",
                f"Voting open for **{dur}** min in {ch.mention}. "
                f"Winner's LFG starts **{lead}** min later.",
            ),
            ephemeral=True,
        )


async def refresh_board_message(bot) -> None:
    """Update the persistent board embed in place (if posted)."""
    db = bot.db
    chan_id = db.get_config(CFG_BOARD_CHANNEL)
    msg_id = db.get_config(CFG_BOARD_MESSAGE)
    if not chan_id or not msg_id:
        return
    try:
        ch = bot.get_channel(int(chan_id)) or await bot.fetch_channel(int(chan_id))
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(embed=board_embed(db), view=ContentBoardView(bot))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
        pass
