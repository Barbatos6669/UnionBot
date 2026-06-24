"""Post-event analytics for LFG content.

The report intentionally keeps automated conclusions conservative:
attendance comes from LFG signups plus event-voice snapshots, Albion stats are
snapshot deltas, and regear rows are "needs officer review" rather than
automatic payouts.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import traceback
from collections import defaultdict
from typing import Any

import discord

import albion_api
import albionbb_api
from cogs._graphs_primitives import _empty_panel, _fig_to_file, _fmt_compact, _style_axes
from cogs._graphs_theme import ACCENT, PALETTE, TEXT_COLOR
from cogs.regear import (
    create_regear_review_from_death_summary,
    enrich_death_summaries_with_estimates,
)
from debug import error_log, info_log
from utils import error_embed, success_embed, is_officer


UTC = dt.timezone.utc
STAT_METRICS = (
    ("kill_fame", "PvP fame"),
    ("death_fame", "Death fame"),
    ("pve_total", "PvE fame"),
    ("gather_all", "Gathering fame"),
    ("crafting_fame", "Crafting fame"),
)
KILLBOARD_LIMIT = 50
KILLBOARD_MAX_PLAYERS = 25
KILLBOARD_REQUEST_TIMEOUT_SECONDS = 8
KILLBOARD_LOOKUP_TIMEOUT_SECONDS = 25
ALBIONBB_MAX_PLAYERS = 25
ALBIONBB_MAX_BATTLES = 30
ALBIONBB_MIN_PLAYERS = 1
GEAR_PRICING_TIMEOUT_SECONDS = 25
EMBED_FIELD_LIMIT = 1024
EMBED_TOTAL_SAFE_LIMIT = 5600
LOOT_BUTTON_TEMPLATE = r"eventreport:loot:(?P<eid>[0-9]+)"


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _discord_ts(value: dt.datetime | None, style: str = "f") -> str:
    if value is None:
        return "unknown"
    return f"<t:{int(value.timestamp())}:{style}>"


def _fmt_num(value: int | float | None) -> str:
    n = int(value or 0)
    sign = "-" if n < 0 else ""
    n_abs = abs(n)
    if n_abs >= 1_000_000:
        return f"{sign}{n_abs / 1_000_000:.1f}M"
    if n_abs >= 1_000:
        return f"{sign}{n_abs / 1_000:.1f}K"
    return f"{n:,}"


def _clamp(text: str, limit: int = 1024) -> str:
    text = str(text or "").strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n...(truncated)"


def _chunk_lines(lines: list[str], *, limit: int = 1000) -> list[str]:
    """Split lines into Discord-field-safe chunks without dropping rows."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for raw in lines:
        line = str(raw or "-").strip() or "-"
        line_len = len(line) + (1 if current else 0)
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if len(line) > limit:
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            if line:
                current = [line]
                current_len = len(line)
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or ["-"]


def embed_text_size(embed: discord.Embed) -> int:
    """Best-effort count for Discord's per-message embed text budget."""
    data = embed.to_dict()
    total = (
        len(data.get("title") or "")
        + len(data.get("description") or "")
        + len((data.get("footer") or {}).get("text") or "")
        + len((data.get("author") or {}).get("name") or "")
    )
    for field in data.get("fields") or []:
        total += len(field.get("name") or "") + len(field.get("value") or "")
    return total


def batch_embeds_for_send(
    embeds: list[discord.Embed],
    *,
    max_count: int = 10,
    max_text: int = 5800,
) -> list[list[discord.Embed]]:
    """Batch embeds within Discord's count and combined text limits."""
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_size = 0
    for embed in embeds:
        size = embed_text_size(embed)
        if current and (
            len(current) >= max_count
            or current_size + size > max_text
        ):
            batches.append(current)
            current = []
            current_size = 0
        current.append(embed)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _parse_silver_amount(raw: str | None) -> int:
    """Parse officer-friendly silver text like ``4.2m`` or ``750k``."""
    text = str(raw or "").strip().lower().replace(",", "").replace("_", "")
    if not text:
        return 0
    text = text.replace("silver", "").replace("s", "").strip()
    multiplier = 1
    suffixes = (
        ("million", 1_000_000),
        ("mil", 1_000_000),
        ("m", 1_000_000),
        ("thousand", 1_000),
        ("k", 1_000),
    )
    for suffix, value in suffixes:
        if text.endswith(suffix):
            multiplier = value
            text = text[: -len(suffix)].strip()
            break
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise ValueError("Use a number like `4200000`, `4.2m`, or `750k`.")
    return max(0, int(float(text) * multiplier))


def build_event_report_view(event_id: int) -> discord.ui.View:
    """Officer tools attached to event scorecards."""
    view = discord.ui.View(timeout=None)
    view.add_item(EventReportLootButton(int(event_id)))
    return view


class EventLootInputModal(discord.ui.Modal, title="Input Event Loot"):
    def __init__(self, event_id: int, existing: dict | None = None) -> None:
        super().__init__(timeout=300)
        self.event_id = int(event_id)
        existing = existing or {}
        self.gross_loot = discord.ui.TextInput(
            label="Total loot value",
            placeholder="e.g. 4.2m or 4200000",
            required=True,
            max_length=32,
            default=str(existing.get("gross_loot") or ""),
        )
        self.guild_cut = discord.ui.TextInput(
            label="Guild cut / reserve",
            placeholder="optional, e.g. 500k",
            required=False,
            max_length=32,
            default=str(existing.get("guild_cut") or ""),
        )
        self.notes = discord.ui.TextInput(
            label="Notes",
            placeholder="optional: loot sold, still holding items, tax reason, etc.",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=700,
            default=str(existing.get("notes") or ""),
        )
        self.add_item(self.gross_loot)
        self.add_item(self.guild_cut)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Only staff can update event loot analytics."),
                ephemeral=True,
            )
            return

        try:
            gross = _parse_silver_amount(str(self.gross_loot.value))
            guild_cut = _parse_silver_amount(str(self.guild_cut.value))
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed("Bad silver value", str(exc)),
                ephemeral=True,
            )
            return
        if gross <= 0:
            await interaction.response.send_message(
                embed=error_embed("Loot required", "Enter the total loot value brought home from the event."),
                ephemeral=True,
            )
            return
        if guild_cut > gross:
            await interaction.response.send_message(
                embed=error_embed("Cut too high", "Guild cut/reserve cannot be higher than total loot."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        db = getattr(bot, "db", None)
        if db is None:
            await interaction.followup.send(
                embed=error_embed("Bot DB unavailable", "I could not save that loot summary."),
                ephemeral=True,
            )
            return
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.followup.send(
                embed=error_embed("Event not found", f"No LFG event with id `{self.event_id}`."),
                ephemeral=True,
            )
            return

        db.upsert_event_loot_summary(
            self.event_id,
            gross_loot=gross,
            guild_cut=guild_cut,
            notes=str(self.notes.value or "").strip() or None,
            updated_by=str(interaction.user.id),
        )
        info_log(
            f"{interaction.user} updated event loot summary #{self.event_id}: "
            f"gross={gross} guild_cut={guild_cut}."
        )

        channel = interaction.channel
        if not hasattr(channel, "send"):
            await interaction.followup.send(
                embed=success_embed(
                    "Loot saved",
                    "Saved the loot summary, but I could not repost the scorecard in this channel.",
                ),
                ephemeral=True,
            )
            return

        try:
            graph_files: list[discord.File] = []
            extra_embeds: list[discord.Embed] = []
            embed = await build_event_report_embed(
                bot,
                event,
                threshold_pct=int(db.get_config("automation_voice_attendance_min_pct") or "50"),
                fetch_killboard=True,
                include_graph=True,
                graph_files=graph_files,
                extra_embeds=extra_embeds,
            )
            report_embeds = [embed, *extra_embeds]
            for idx, embed_batch in enumerate(batch_embeds_for_send(report_embeds)):
                kwargs: dict[str, Any] = {
                    "embeds": embed_batch,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if idx == 0:
                    kwargs["view"] = build_event_report_view(self.event_id)
                    if graph_files:
                        kwargs["file"] = graph_files[0]
                await channel.send(**kwargs)
        except Exception as exc:  # noqa: BLE001
            error_log(f"event loot scorecard repost failed for #{self.event_id}: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Loot saved, report failed",
                    "The loot value was saved, but I could not repost the scorecard. Check the bot logs.",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=success_embed(
                "Loot saved",
                (
                    f"Saved **{_fmt_num(gross)}** loot for event **#{self.event_id}** "
                    f"with **{_fmt_num(guild_cut)}** guild cut/reserve. "
                    "I posted an updated scorecard below."
                ),
            ),
            ephemeral=True,
        )


class EventReportLootButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=LOOT_BUTTON_TEMPLATE,
):
    def __init__(self, event_id: int) -> None:
        self.event_id = int(event_id)
        super().__init__(
            discord.ui.Button(
                label="Input Event Loot",
                style=discord.ButtonStyle.success,
                custom_id=f"eventreport:loot:{self.event_id}",
                emoji="💰",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "EventReportLootButton":
        return cls(int(match["eid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Only staff can update event loot analytics."),
                ephemeral=True,
            )
            return
        db = getattr(interaction.client, "db", None)
        event = db.fetch_lfg_event(self.event_id) if db is not None else None
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event not found", f"No LFG event with id `{self.event_id}`."),
                ephemeral=True,
            )
            return
        existing = db.fetch_event_loot_summary(self.event_id)
        await interaction.response.send_modal(EventLootInputModal(self.event_id, existing))


def register_persistent_event_report_views(bot) -> None:
    """Wake up event-report DynamicItem buttons after bot restart."""
    bot.add_dynamic_items(EventReportLootButton)


def _append_paged_fields(
    embeds: list[discord.Embed],
    *,
    title: str,
    color: discord.Color,
    lines: list[str],
    field_base: str,
    description: str | None = None,
) -> None:
    """Append continuation embeds with all lines preserved.

    Discord limits one field to 1024 characters and one embed to 6000 total
    characters. We stay under that by packing a few safe chunks per embed.
    """
    chunks = _chunk_lines(lines, limit=1000)
    page = 0
    idx = 0
    while idx < len(chunks):
        page += 1
        embed = discord.Embed(
            title=title,
            description=description if page == 1 else None,
            color=color,
            timestamp=dt.datetime.now(UTC),
        )
        used = len(embed.title or "") + len(embed.description or "")
        fields_on_page = 0
        while idx < len(chunks) and fields_on_page < 5:
            chunk = chunks[idx]
            name = f"{field_base} {idx + 1}/{len(chunks)}"
            if used + len(name) + len(chunk) > EMBED_TOTAL_SAFE_LIMIT and fields_on_page:
                break
            embed.add_field(name=name, value=chunk, inline=False)
            used += len(name) + len(chunk)
            fields_on_page += 1
            idx += 1
        embed.set_footer(text=f"Continuation page {page} - no regear rows omitted.")
        embeds.append(embed)


def _regear_death_line(
    death: dict,
    *,
    profiles: dict[str, dict],
    signup_ids: set[str],
) -> str:
    did = str(death.get("discord_id") or "")
    name = _member_name(profiles.get(did), did)
    url = death.get("killboard_url") or ""
    linked = f"[{name}]({url})" if url else name
    loc = death.get("location") or "unknown zone"
    killer = death.get("killer_name") or "Unknown"
    signed = "yes" if did in signup_ids else "no"
    est_value = int(death.get("estimated_value") or 0)
    value_text = (
        f"Est gear: **{_fmt_num(est_value)}**"
        if est_value > 0 else
        "Est gear: **manual pricing needed**"
    )
    return (
        f"{linked} - {_fmt_num(death.get('fame'))} fame, {loc}, "
        f"killed by {killer}. Signup: {signed}; VC: yes. {value_text}."
    )


def _event_window(event: dict) -> tuple[dt.datetime | None, dt.datetime | None, dt.datetime | None, dt.datetime | None]:
    starts_at = _parse_dt(event.get("starts_at"))
    ends_at = _parse_dt(event.get("ends_at"))
    if not starts_at or not ends_at:
        return starts_at, ends_at, starts_at, ends_at
    prep = max(0, int(event.get("prep_minutes") or 30))
    review = max(0, int(event.get("review_minutes") or 15))
    report_end = ends_at + dt.timedelta(minutes=review)
    voice_deleted_at = _parse_dt(event.get("voice_channel_deleted_at"))
    if voice_deleted_at and voice_deleted_at > report_end:
        report_end = voice_deleted_at
    return (
        starts_at,
        ends_at,
        starts_at - dt.timedelta(minutes=prep),
        report_end,
    )


def _row_before(db, discord_id: str, when: dt.datetime) -> dict | None:
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            """
            SELECT kill_fame, death_fame, pve_total, gather_all,
                   crafting_fame, average_item_power, recorded_at
              FROM player_stats_history
             WHERE discord_id = ?
               AND datetime(recorded_at) <= datetime(?)
             ORDER BY datetime(recorded_at) DESC, id DESC
             LIMIT 1
            """,
            (str(discord_id), when.isoformat()),
        )
        row = db.cursor.fetchone()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        error_log(f"event report history lookup failed for {discord_id}: {exc!r}")
        return None


def _profile_current_row(profile: dict | None) -> dict | None:
    if not profile:
        return None
    return {
        "kill_fame": int(profile.get("kill_fame") or 0),
        "death_fame": int(profile.get("death_fame") or 0),
        "pve_total": int(profile.get("pve_total") or 0),
        "gather_all": int(profile.get("gather_all") or 0),
        "crafting_fame": int(profile.get("crafting_fame") or 0),
        "average_item_power": float(profile.get("average_item_power") or 0),
        "recorded_at": profile.get("last_updated") or "",
    }


def _event_stat_deltas(
    db,
    event: dict,
    profiles: dict[str, dict],
    attendee_ids: set[str],
) -> tuple[dict[str, int], list[dict], int]:
    starts_at, ends_at, report_start, report_end = _event_window(event)
    if not report_start or not report_end:
        return {key: 0 for key, _ in STAT_METRICS}, [], 0

    # Let the next hourly profile sync count if it lands shortly after review.
    now = dt.datetime.now(UTC)
    stat_end = min(now, report_end + dt.timedelta(hours=2))
    totals = {key: 0 for key, _ in STAT_METRICS}
    player_rows: list[dict] = []
    usable = 0

    for did in sorted(attendee_ids):
        profile = profiles.get(did) or {}
        before = _row_before(db, did, report_start)
        after = _row_before(db, did, stat_end) or _profile_current_row(profile)
        if not before or not after:
            continue
        usable += 1
        deltas: dict[str, int] = {}
        for key, _label in STAT_METRICS:
            delta = max(0, int(after.get(key) or 0) - int(before.get(key) or 0))
            deltas[key] = delta
            totals[key] += delta
        activity = (
            deltas["kill_fame"]
            + deltas["pve_total"]
            + deltas["gather_all"]
            + deltas["crafting_fame"]
        )
        if activity:
            player_rows.append({
                "discord_id": did,
                "name": profile.get("albion_name") or profile.get("username") or did,
                "activity": activity,
                **deltas,
            })
    player_rows.sort(key=lambda row: row["activity"], reverse=True)
    return totals, player_rows, usable


def _in_window(event_dt: dt.datetime | None, start: dt.datetime | None, end: dt.datetime | None) -> bool:
    if not event_dt or not start or not end:
        return False
    return start <= event_dt <= end


def _kill_summary(event: dict) -> dict:
    event_id = int(event.get("EventId") or 0)
    killer = event.get("Killer") or {}
    victim = event.get("Victim") or {}
    return {
        "event_id": event_id,
        "timestamp": event.get("TimeStamp") or "",
        "killer_name": killer.get("Name") or "Unknown",
        "killer_guild": killer.get("GuildName") or "",
        "victim_name": victim.get("Name") or "Unknown",
        "victim_guild": victim.get("GuildName") or "",
        "victim_ip": float(victim.get("AverageItemPower") or 0),
        "fame": int(event.get("TotalVictimKillFame") or 0),
        "location": str(event.get("Location") or ""),
        "killboard_url": f"https://albiononline.com/en/killboard/kill/{event_id}" if event_id else "",
    }


async def _fetch_killboard_window(
    profiles: dict[str, dict],
    attendee_ids: set[str],
    report_start: dt.datetime | None,
    report_end: dt.datetime | None,
) -> tuple[list[dict], list[dict], int, int]:
    kills_by_id: dict[int, dict] = {}
    deaths_by_id: dict[int, dict] = {}
    scanned = 0
    errors = 0
    registered = [
        (did, profiles.get(did) or {})
        for did in sorted(attendee_ids)
        if (profiles.get(did) or {}).get("albion_player_id")
    ][:KILLBOARD_MAX_PLAYERS]

    for did, profile in registered:
        player_id = str(profile.get("albion_player_id"))
        try:
            deaths = await asyncio.to_thread(
                albion_api.get_player_deaths,
                player_id,
                limit=KILLBOARD_LIMIT,
                timeout=KILLBOARD_REQUEST_TIMEOUT_SECONDS,
            )
            kills = await asyncio.to_thread(
                albion_api.get_player_kills,
                player_id,
                limit=KILLBOARD_LIMIT,
                timeout=KILLBOARD_REQUEST_TIMEOUT_SECONDS,
            )
            scanned += 1
        except Exception as exc:  # noqa: BLE001
            errors += 1
            error_log(f"event report killboard fetch failed for {did}: {exc!r}")
            continue

        for raw in deaths:
            ts = _parse_dt(raw.get("TimeStamp"))
            if not _in_window(ts, report_start, report_end):
                continue
            summary = albion_api.format_death_event(raw)
            summary["discord_id"] = did
            summary["victim_name"] = profile.get("albion_name") or summary.get("victim_name") or did
            deaths_by_id[int(summary.get("event_id") or 0)] = summary

        for raw in kills:
            ts = _parse_dt(raw.get("TimeStamp"))
            if not _in_window(ts, report_start, report_end):
                continue
            summary = _kill_summary(raw)
            kills_by_id[int(summary.get("event_id") or 0)] = summary

    kills = sorted(kills_by_id.values(), key=lambda row: row.get("fame", 0), reverse=True)
    deaths = sorted(deaths_by_id.values(), key=lambda row: row.get("fame", 0), reverse=True)
    return kills, deaths, scanned, errors


def _profile_server(profile: dict | None) -> str:
    value = str((profile or {}).get("server") or "").strip().lower()
    if value in {"europe", "eu"}:
        return "europe"
    if value in {"asia", "east"}:
        return "asia"
    return "americas"


def _dominant_role(role_counts: dict[str, int]) -> str:
    if not role_counts:
        return "unknown"
    return max(role_counts.items(), key=lambda item: (item[1], item[0]))[0]


def _role_label(role: str | None) -> str:
    role = str(role or "").strip().lower()
    return role or "unknown"


async def _fetch_albionbb_event_window(
    profiles: dict[str, dict],
    attendee_ids: set[str],
    report_start: dt.datetime | None,
    report_end: dt.datetime | None,
) -> dict:
    """Fetch AlbionBB player/battle rows for the event window.

    This is enrichment data only. It gives the scorecard richer battle
    context without replacing VC attendance or official killboard regear
    evidence.
    """
    if not report_start or not report_end or not attendee_ids:
        return {"enabled": False, "reason": "missing event window"}

    candidates: list[tuple[str, dict]] = []
    for did in sorted(attendee_ids):
        profile = profiles.get(did) or {}
        name = str(profile.get("albion_name") or "").strip()
        if name:
            candidates.append((did, profile))
    candidates = candidates[:ALBIONBB_MAX_PLAYERS]
    if not candidates:
        return {"enabled": False, "reason": "no registered attendee names"}

    start_date = report_start.date().isoformat()
    end_date = report_end.date().isoformat()
    semaphore = asyncio.Semaphore(6)
    errors = 0

    async def _fetch_player(did: str, profile: dict) -> tuple[str, dict, list[dict]]:
        nonlocal errors
        async with semaphore:
            name = str(profile.get("albion_name") or "").strip()
            server = _profile_server(profile)
            try:
                rows = await asyncio.to_thread(
                    albionbb_api.get_player_battle_stats,
                    name,
                    server=server,
                    min_players=ALBIONBB_MIN_PLAYERS,
                    start=start_date,
                    end=end_date,
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                error_log(f"AlbionBB player fetch failed for {did}/{name}: {exc!r}")
                rows = []
            return did, profile, rows

    fetched = await asyncio.gather(*(_fetch_player(did, profile) for did, profile in candidates))
    rows_by_battle_player: dict[tuple[int, str], dict] = {}
    player_totals: dict[str, dict] = {}
    friendly_guilds: set[str] = set()
    friendly_alliances: set[str] = set()
    server_counts: defaultdict[str, int] = defaultdict(int)

    for did, profile, raw_rows in fetched:
        name = str(profile.get("albion_name") or profile.get("username") or did)
        server = _profile_server(profile)
        server_counts[server] += 1
        if profile.get("guild_name"):
            friendly_guilds.add(str(profile["guild_name"]))
        if profile.get("alliance_name"):
            friendly_alliances.add(str(profile["alliance_name"]))
        total = player_totals.setdefault(
            did,
            {
                "discord_id": did,
                "name": name,
                "guild": profile.get("guild_name") or "",
                "alliance": profile.get("alliance_name") or "",
                "attendance": 0,
                "kills": 0,
                "deaths": 0,
                "kill_fame": 0,
                "death_fame": 0,
                "damage": 0,
                "heal": 0,
                "ip_values": [],
                "roles": defaultdict(int),
                "battle_ids": set(),
                "server": server,
            },
        )
        for row in raw_rows:
            battle_id = int(row.get("albionId") or 0)
            started = _parse_dt(row.get("startedAt"))
            if not battle_id or not _in_window(started, report_start, report_end):
                continue
            key = (battle_id, did)
            if key in rows_by_battle_player:
                continue
            role = _role_label(row.get("role"))
            ip = int(float(row.get("ip") or 0))
            normalized = {
                **row,
                "discord_id": did,
                "attendee_name": name,
                "started_at": started,
                "role": role,
                "ip": ip,
                "server": server,
            }
            rows_by_battle_player[key] = normalized
            total["attendance"] += 1
            total["kills"] += int(row.get("kills") or 0)
            total["deaths"] += int(row.get("deaths") or 0)
            total["kill_fame"] += int(row.get("killFame") or 0)
            total["death_fame"] += int(row.get("deathFame") or 0)
            total["damage"] += int(row.get("damage") or 0)
            total["heal"] += int(row.get("heal") or 0)
            if ip > 0:
                total["ip_values"].append(ip)
            total["roles"][role] += 1
            total["battle_ids"].add(battle_id)

    rows = sorted(
        rows_by_battle_player.values(),
        key=lambda row: row.get("started_at") or dt.datetime.min.replace(tzinfo=UTC),
    )
    battle_ids = sorted({int(row.get("albionId") or 0) for row in rows if row.get("albionId")})
    primary_server = max(server_counts.items(), key=lambda item: item[1])[0] if server_counts else "americas"

    battle_details: list[dict] = []
    battle_errors = 0
    for battle_id in battle_ids[:ALBIONBB_MAX_BATTLES]:
        try:
            battle = await asyncio.to_thread(
                albionbb_api.get_battle,
                battle_id,
                server=primary_server,
            )
        except Exception as exc:  # noqa: BLE001
            battle_errors += 1
            error_log(f"AlbionBB battle fetch failed for {battle_id}: {exc!r}")
            continue
        if battle:
            battle_details.append(battle)

    role_counts: defaultdict[str, int] = defaultdict(int)
    role_ip_values: defaultdict[str, list[int]] = defaultdict(list)
    role_player_sets: defaultdict[str, set[str]] = defaultdict(set)
    for row in rows:
        role = _role_label(row.get("role"))
        role_counts[role] += 1
        role_player_sets[role].add(str(row.get("discord_id") or row.get("attendee_name") or ""))
        if int(row.get("ip") or 0) > 0:
            role_ip_values[role].append(int(row["ip"]))

    enemy_guilds: defaultdict[str, dict] = defaultdict(lambda: {
        "name": "",
        "alliance": "",
        "kill_fame": 0,
        "kills": 0,
        "deaths": 0,
        "players": 0,
        "battles": set(),
    })
    friendly_guilds_norm = {g.strip().lower() for g in friendly_guilds if g}
    friendly_alliances_norm = {a.strip().lower() for a in friendly_alliances if a}
    for battle in battle_details:
        battle_id = int(battle.get("albionId") or 0)
        for guild in battle.get("guilds") or []:
            name = str(guild.get("name") or "").strip()
            if not name:
                continue
            alliance = str(guild.get("alliance") or "").strip()
            if name.lower() in friendly_guilds_norm:
                continue
            if alliance and alliance.lower() in friendly_alliances_norm:
                continue
            bucket = enemy_guilds[name]
            bucket["name"] = name
            bucket["alliance"] = alliance
            bucket["kill_fame"] += int(guild.get("killFame") or 0)
            bucket["kills"] += int(guild.get("kills") or 0)
            bucket["deaths"] += int(guild.get("deaths") or 0)
            bucket["players"] = max(int(bucket["players"] or 0), int(guild.get("players") or 0))
            if battle_id:
                bucket["battles"].add(battle_id)

    player_rows: list[dict] = []
    for total in player_totals.values():
        if not total["attendance"]:
            continue
        ip_values = total.pop("ip_values")
        roles = dict(total.pop("roles"))
        battle_id_set = total.pop("battle_ids")
        total["avg_ip"] = int(sum(ip_values) / len(ip_values)) if ip_values else 0
        total["dominant_role"] = _dominant_role(roles)
        total["roles"] = roles
        total["battle_ids"] = sorted(battle_id_set)
        total["impact"] = (
            int(total["kill_fame"])
            + int(total["damage"])
            + int(total["heal"]) // 2
        )
        player_rows.append(total)
    player_rows.sort(key=lambda row: row["impact"], reverse=True)

    totals = {
        "attendance_rows": len(rows),
        "battles": len(battle_ids),
        "battle_details": len(battle_details),
        "kills": sum(int(row.get("kills") or 0) for row in rows),
        "deaths": sum(int(row.get("deaths") or 0) for row in rows),
        "kill_fame": sum(int(row.get("killFame") or 0) for row in rows),
        "death_fame": sum(int(row.get("deathFame") or 0) for row in rows),
        "damage": sum(int(row.get("damage") or 0) for row in rows),
        "heal": sum(int(row.get("heal") or 0) for row in rows),
        "avg_ip": int(
            sum(int(row.get("ip") or 0) for row in rows if int(row.get("ip") or 0) > 0)
            / max(1, sum(1 for row in rows if int(row.get("ip") or 0) > 0))
        ) if rows else 0,
    }
    return {
        "enabled": True,
        "source": "AlbionBB",
        "server": primary_server,
        "players_scanned": len(candidates),
        "players_with_rows": len(player_rows),
        "errors": errors + battle_errors,
        "rows": rows,
        "battle_ids": battle_ids,
        "battles": battle_details,
        "player_totals": player_rows,
        "role_counts": dict(role_counts),
        "role_unique_players": {
            role: len({player for player in players if player})
            for role, players in role_player_sets.items()
        },
        "role_avg_ip": {
            role: int(sum(values) / len(values))
            for role, values in role_ip_values.items()
            if values
        },
        "enemy_guilds": sorted(
            (
                {
                    **value,
                    "battles": sorted(value["battles"]),
                }
                for value in enemy_guilds.values()
            ),
            key=lambda row: int(row.get("kill_fame") or 0),
            reverse=True,
        ),
        "friendly_guilds": sorted(friendly_guilds),
        "friendly_alliances": sorted(friendly_alliances),
        "totals": totals,
    }


def _member_name(profile: dict | None, discord_id: str) -> str:
    profile = profile or {}
    return str(profile.get("albion_name") or profile.get("username") or f"<@{discord_id}>")


def _attendance_sets(db, event_id: int, threshold_pct: int) -> dict:
    signups = db.fetch_lfg_signups(event_id)
    snapshots = db.fetch_voice_snapshot_summary(event_id)
    signup_ids = {str(s["discord_id"]) for s in signups}
    max_count = max(snapshots.values()) if snapshots else 0
    threshold = max(1, max_count * max(1, int(threshold_pct or 50)) // 100)
    confirmed = {
        str(s["discord_id"])
        for s in signups
        if int(s.get("attended") or 0) == 1
    }
    confirmed.update(
        did for did, seen in snapshots.items()
        if int(seen or 0) >= threshold
    )
    return {
        "signups": signups,
        "snapshots": snapshots,
        "signup_ids": signup_ids,
        "confirmed_ids": confirmed,
        "all_ids": signup_ids | confirmed,
        "threshold": threshold,
        "threshold_pct": int(threshold_pct or 50),
    }


def _short_label(value: str, limit: int = 18) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _annotate_vertical_bars(ax, values: list[int | float]) -> None:
    if not values:
        return
    span = max(abs(float(v)) for v in values) or 1.0
    for idx, raw in enumerate(values):
        value = float(raw or 0)
        pad = span * 0.03
        va = "bottom" if value >= 0 else "top"
        y = value + pad if value >= 0 else value - pad
        ax.text(
            idx,
            y,
            _fmt_compact(value),
            ha="center",
            va=va,
            color=TEXT_COLOR,
            fontsize=8,
            fontweight="700",
        )


def _draw_stat_card(ax, x: float, y: float, w: float, h: float, *, label: str, value: str, color: str, sub: str = "") -> None:
    import matplotlib.patches as patches

    ax.add_patch(
        patches.FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.015,rounding_size=0.025",
            facecolor="#ffffff",
            edgecolor="#dde1e6",
            linewidth=0.8,
            zorder=2,
        )
    )
    ax.text(
        x + 0.04,
        y + h - 0.09,
        label,
        color="#6b7280",
        fontsize=8,
        fontweight="700",
        transform=ax.transAxes,
        zorder=3,
    )
    ax.text(
        x + 0.04,
        y + h - 0.20,
        value,
        color=color,
        fontsize=13,
        fontweight="900",
        transform=ax.transAxes,
        zorder=3,
    )
    if sub:
        ax.text(
            x + 0.04,
            y + 0.06,
            sub,
            color="#6b7280",
            fontsize=7,
            fontweight="600",
            transform=ax.transAxes,
            zorder=3,
        )


def _build_event_scorecard_graph(
    event: dict,
    *,
    attendance_counts: dict[str, int],
    snapshot_flow: list[dict],
    stat_totals: dict[str, int],
    player_deltas: list[dict],
    kills: list[dict],
    deaths: list[dict],
    kill_fame_value: int,
    death_fame_value: int,
    net_fame_value: int,
    killboard_lookup_enabled: bool = True,
    loot_summary: dict | None = None,
    albionbb_summary: dict | None = None,
) -> discord.File | None:
    """Build a compact officer-facing event scorecard.

    The graph is intentionally a summary, not a source of truth. The embed
    keeps the exact notes/details while the image lets officers scan the event
    outcome quickly.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker
    except Exception as exc:  # noqa: BLE001
        error_log(f"event report graph import failed: {exc!r}")
        return None

    try:
        event_id = int(event.get("id") or 0)
        title = _short_label(str(event.get("title") or "LFG"), 58)
        fig, axes = plt.subplots(2, 3, figsize=(17, 8.6), constrained_layout=True)
        fig.patch.set_facecolor("#eef1f5")
        fig.suptitle(
            f"Event Scorecard  •  #{event_id} {title}",
            fontsize=16,
            fontweight="800",
            color=TEXT_COLOR,
            x=0.02,
            ha="left",
        )

        ax_attendance, ax_battles, ax_stats, ax_roles, ax_value, ax_players = axes.flat

        # Voice attendance over time. This shows the real VC population curve,
        # which is more useful than a static funnel when officers want to know
        # whether people stayed for the run or dropped after form-up.
        flow_points: list[tuple[dt.datetime, int]] = []
        for row in snapshot_flow or []:
            ts = _parse_dt(row.get("snapshot_at"))
            if not ts:
                continue
            flow_points.append((ts, int(row.get("members") or 0)))
        if not flow_points:
            _empty_panel(ax_attendance, "No VC flow captured")
        else:
            starts_at, ends_at, report_start, report_end = _event_window(event)
            ref = report_start or flow_points[0][0]
            xs = [(ts - ref).total_seconds() / 60.0 for ts, _count in flow_points]
            ys = [count for _ts, count in flow_points]
            peak = max(ys) if ys else 0
            first = ys[0] if ys else 0
            final = ys[-1] if ys else 0
            avg = sum(ys) / len(ys) if ys else 0.0
            retention = (100.0 * final / peak) if peak else 0.0
            drop = max(0, peak - final)

            ax_attendance.plot(xs, ys, color=ACCENT, linewidth=2.2, marker="o", markersize=3.8, zorder=4)
            ax_attendance.fill_between(xs, ys, 0, color=ACCENT, alpha=0.18, linewidth=0, zorder=2)
            marker_top = max(peak * 1.15, peak + 2)
            visible_marker_xs: list[float] = []
            for marker, label, color in (
                (starts_at, "start", "#9b7bd4"),
                (ends_at, "end", "#8d99ae"),
                (report_end, "close", "#e6b54a"),
            ):
                if not marker:
                    continue
                mx = (marker - ref).total_seconds() / 60.0
                if min(xs) - 8 <= mx <= max(xs) + 8:
                    visible_marker_xs.append(mx)
                    ax_attendance.axvline(mx, color=color, linestyle="--", linewidth=1.0, alpha=0.75, zorder=3)
                    ax_attendance.text(
                        mx,
                        marker_top,
                        label,
                        color=color,
                        fontsize=7,
                        fontweight="800",
                        ha="center",
                        va="bottom",
                    )
            for x, y in zip(xs, ys):
                if y in (peak, final) or len(xs) <= 6:
                    ax_attendance.text(
                        x,
                        y + max(peak * 0.04, 0.35),
                        str(y),
                        color=TEXT_COLOR,
                        fontsize=7,
                        fontweight="700",
                        ha="center",
                        va="bottom",
                    )
            ax_attendance.set_ylim(0, marker_top * 1.08)
            x_bounds = xs + visible_marker_xs
            if min(x_bounds) == max(x_bounds):
                ax_attendance.set_xlim(xs[0] - 5, xs[0] + 5)
            else:
                ax_attendance.set_xlim(min(x_bounds) - 4, max(x_bounds) + 4)
            ax_attendance.set_title("VC Attendance Flow", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
            ax_attendance.xaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda value, _pos=None: f"{int(value)}m")
            )
            _style_axes(ax_attendance)
            ax_attendance.grid(axis="x", linestyle="-", linewidth=0.6, color="#dde1e6", zorder=0)
            ax_attendance.text(
                0.98,
                0.05,
                f"Peak {peak} • Final {final} • Retention {retention:.0f}%\n"
                f"Avg {avg:.1f} • Drop-off {drop} • First {first}",
                transform=ax_attendance.transAxes,
                ha="right",
                va="bottom",
                color=TEXT_COLOR,
                fontsize=8,
                fontweight="800",
            )
            signed = int(attendance_counts.get("signups") or 0)
            confirmed = int(attendance_counts.get("confirmed") or 0)
            if signed or confirmed:
                ax_attendance.text(
                    0.02,
                    0.05,
                    f"Signed {signed} • VC confirmed {confirmed}",
                    transform=ax_attendance.transAxes,
                    ha="left",
                    va="bottom",
                    color=TEXT_COLOR,
                    fontsize=8,
                    fontweight="700",
                )

        # AlbionBB battle timeline. This shows how many battleboard events the
        # attendees touched during the run and whether those moments were tiny
        # skirmishes or meaningful fame swings.
        bb = albionbb_summary or {}
        bb_battles = [
            battle for battle in (bb.get("battles") or [])
            if _parse_dt(battle.get("startedAt"))
        ]
        if not bb.get("enabled") or not bb_battles:
            _empty_panel(ax_battles, "No AlbionBB battle matches")
        else:
            starts_at, _ends_at, report_start, _report_end = _event_window(event)
            ref = report_start or starts_at or _parse_dt(bb_battles[0].get("startedAt"))
            xs = [
                (_parse_dt(battle.get("startedAt")) - ref).total_seconds() / 60.0
                for battle in bb_battles
                if _parse_dt(battle.get("startedAt")) and ref
            ]
            fame_values = [int(battle.get("totalFame") or 0) for battle in bb_battles[: len(xs)]]
            kill_values = [int(battle.get("totalKills") or 0) for battle in bb_battles[: len(xs)]]
            player_values = [int(battle.get("totalPlayers") or 0) for battle in bb_battles[: len(xs)]]
            colors = [
                PALETTE["kill"] if fame >= 250_000 else PALETTE["members"]
                for fame in fame_values
            ]
            if xs and fame_values:
                ax_battles.bar(xs, fame_values, color=colors, width=3.5, alpha=0.78, zorder=3)
                ax_battles.set_ylim(0, max(fame_values) * 1.22)
                ax_battles.set_title("AlbionBB Battle Timeline", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
                ax_battles.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
                ax_battles.xaxis.set_major_formatter(
                    matplotlib.ticker.FuncFormatter(lambda value, _pos=None: f"{int(value)}m")
                )
                _style_axes(ax_battles)
                for x, fame, battle_kills, players in zip(xs, fame_values, kill_values, player_values):
                    if fame <= 0:
                        continue
                    ax_battles.text(
                        x,
                        fame + max(fame_values) * 0.04,
                        f"{_fmt_compact(fame)} fame\n{battle_kills} kills • {players} players",
                        ha="center",
                        va="bottom",
                        color=TEXT_COLOR,
                        fontsize=6,
                        fontweight="700",
                    )
                ax_battles.text(
                    0.02,
                    0.95,
                    f"{len(bb.get('battle_ids') or [])} battle(s) • "
                    f"{_fmt_compact((bb.get('totals') or {}).get('kill_fame'))} attendee kill fame",
                    transform=ax_battles.transAxes,
                    ha="left",
                    va="top",
                    color=TEXT_COLOR,
                    fontsize=8,
                    fontweight="800",
                    bbox={
                        "facecolor": "#f7f8fa",
                        "edgecolor": "#dde1e6",
                        "boxstyle": "round,pad=0.25",
                        "alpha": 0.88,
                    },
                )
                ax_battles.set_xlabel("Minutes after report window start", color="#6b7280", fontsize=7)
            else:
                _empty_panel(ax_battles, "No AlbionBB battle timeline")

        # Stat growth from stored profile snapshots.
        stat_labels = ["PvP", "Deaths", "PvE", "Gather", "Craft"]
        stat_keys = ["kill_fame", "death_fame", "pve_total", "gather_all", "crafting_fame"]
        stat_colors = [
            PALETTE["kill"],
            PALETTE["death"],
            PALETTE["pve"],
            PALETTE["gather"],
            PALETTE["craft"],
        ]
        stat_values = [int(stat_totals.get(key) or 0) for key in stat_keys]
        if max(stat_values or [0]) <= 0:
            _empty_panel(ax_stats, "No stat movement captured yet")
        else:
            ax_stats.bar(stat_labels, stat_values, color=stat_colors, width=0.62, zorder=3)
            ax_stats.set_title("Fame / Stat Growth", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
            ax_stats.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
            _style_axes(ax_stats)
            _annotate_vertical_bars(ax_stats, stat_values)

        # Role/IP mix from AlbionBB rows. The bar length uses unique players
        # because raw AlbionBB player-battle rows are too easy to misread as
        # headcount. Battle appearances remain visible as sample context.
        role_unique = dict((bb.get("role_unique_players") or {})) if bb.get("enabled") else {}
        role_appearances = dict((bb.get("role_counts") or {})) if bb.get("enabled") else {}
        if not role_unique:
            _empty_panel(ax_roles, "No AlbionBB role/IP data")
        else:
            role_items = sorted(
                role_unique.items(),
                key=lambda item: (int(item[1] or 0), int(role_appearances.get(item[0]) or 0)),
                reverse=True,
            )[:8]
            roles = [_short_label(role.title(), 12) for role, _count in role_items]
            counts = [int(count or 0) for _role, count in role_items]
            y = list(range(len(roles)))
            ax_roles.barh(y, counts, color=PALETTE["members"], height=0.58, zorder=3)
            ax_roles.set_yticks(y)
            ax_roles.set_yticklabels(roles, color=TEXT_COLOR, fontsize=8)
            ax_roles.invert_yaxis()
            ax_roles.set_title("Roles by Unique Players", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
            _style_axes(ax_roles)
            ax_roles.grid(axis="x", linestyle="-", linewidth=0.6, color="#dde1e6", zorder=0)
            max_count = max(counts) if counts else 1
            ax_roles.set_xlim(0, max_count * 1.75)
            avg_by_role = bb.get("role_avg_ip") or {}
            for idx, ((role, _count), count) in enumerate(zip(role_items, counts)):
                appearances = int(role_appearances.get(role) or 0)
                ip_text = f" • {int(avg_by_role.get(role) or 0)} avg IP" if avg_by_role.get(role) else ""
                ax_roles.text(
                    count + max_count * 0.03,
                    idx,
                    f"{count} player{'s' if count != 1 else ''}"
                    f" • {appearances} appearance{'s' if appearances != 1 else ''}"
                    f"{ip_text}",
                    va="center",
                    color=TEXT_COLOR,
                    fontsize=7,
                    fontweight="700",
                )
            avg_ip = int((bb.get("totals") or {}).get("avg_ip") or 0)
            ax_roles.text(
                0.98,
                0.05,
                f"Avg attendee IP {avg_ip} • appearances = player-battle rows"
                if avg_ip else "Appearances = player-battle rows",
                transform=ax_roles.transAxes,
                ha="right",
                va="bottom",
                color=TEXT_COLOR,
                fontsize=8,
                fontweight="800",
            )

        # Combat/regear summary. The old "pressure" chart mixed positive and
        # negative fame values, which looked dramatic but was hard to act on.
        # Cards keep the useful run stats explicit.
        ax_value.set_facecolor("#f7f8fa")
        ax_value.set_xlim(0, 1)
        ax_value.set_ylim(0, 1)
        ax_value.axis("off")
        ax_value.text(
            0,
            1.02,
            "Combat / Regear Recap",
            color=TEXT_COLOR,
            fontsize=11,
            fontweight="700",
            transform=ax_value.transAxes,
        )
        if not killboard_lookup_enabled:
            cards = [
                ("Kills found", "skipped", "#8d99ae", "killboard lookup off"),
                ("Deaths found", "skipped", "#8d99ae", "killboard lookup off"),
                ("K:D", "n/a", "#8d99ae", "not calculated"),
                ("Loot value", "not entered", "#8d99ae", "click Input Event Loot"),
                ("Est. gear loss", "n/a", "#8d99ae", "lookup skipped"),
                ("Net silver", "n/a", "#8d99ae", "needs loss data"),
            ]
        else:
            gear_loss = sum(int(d.get("estimated_value") or 0) for d in deaths)
            priced_deaths = sum(1 for d in deaths if int(d.get("estimated_value") or 0) > 0)
            manual = max(0, len(deaths) - priced_deaths)
            bb_totals = (albionbb_summary or {}).get("totals") or {}
            bb_kills = int(bb_totals.get("kills") or 0)
            bb_deaths = int(bb_totals.get("deaths") or 0)
            display_kills = len(kills) if kills else bb_kills
            display_deaths = len(deaths) if deaths else bb_deaths
            kd_ratio = (display_kills / display_deaths) if display_deaths else float(display_kills)
            kd_label = f"{display_kills}:{display_deaths}"
            kd_sub = (
                f"{kd_ratio:.2f} kills/death"
                if display_deaths
                else ("no deaths found" if display_kills else "no events found")
            )
            avg_loss = int(gear_loss / priced_deaths) if priced_deaths else 0
            net_color = "#27ae60" if net_fame_value >= 0 else "#e67e22"
            gross_loot = int((loot_summary or {}).get("gross_loot") or 0)
            guild_cut = int((loot_summary or {}).get("guild_cut") or 0)
            distributable = max(0, gross_loot - guild_cut)
            net_silver = distributable - gear_loss if gross_loot else 0
            net_silver_color = "#27ae60" if net_silver >= 0 else "#c0392b"
            cards = [
                (
                    "Kills found",
                    str(display_kills),
                    "#27ae60",
                    "official kill events" if kills else "AlbionBB player rows",
                ),
                (
                    "Deaths found",
                    str(display_deaths),
                    "#c0392b",
                    "official regear details" if deaths else "AlbionBB row deaths",
                ),
                ("K:D", kd_label, net_color, kd_sub),
                (
                    "Loot value",
                    _fmt_compact(gross_loot) if gross_loot else "not entered",
                    "#27ae60" if gross_loot else "#8d99ae",
                    f"guild cut {_fmt_compact(guild_cut)}" if guild_cut else "click Input Event Loot",
                ),
                (
                    "Est. gear loss",
                    _fmt_compact(gear_loss),
                    "#e67e22" if gear_loss else TEXT_COLOR,
                    f"avg {_fmt_compact(avg_loss)}" + (f" • {manual} manual" if manual else ""),
                ),
                (
                    "Net silver",
                    _fmt_compact(net_silver) if gross_loot else "n/a",
                    net_silver_color if gross_loot else "#8d99ae",
                    "loot after cut minus gear loss" if gross_loot else "enter loot first",
                ),
            ]
        positions = [
            (0.02, 0.58),
            (0.35, 0.58),
            (0.68, 0.58),
            (0.02, 0.14),
            (0.35, 0.14),
            (0.68, 0.14),
        ]
        for (label, value, color, sub), (x, y) in zip(cards, positions):
            _draw_stat_card(ax_value, x, y, 0.28, 0.28, label=label, value=value, color=color, sub=sub)
        ax_value.text(
            0.02,
            0.03,
            "Net silver uses officer-entered loot minus estimated attendee gear loss."
            if killboard_lookup_enabled
            else "Preview only: run reconcile with killboard lookup for combat/regear data.",
            color="#6b7280",
            fontsize=7,
            fontweight="600",
            transform=ax_value.transAxes,
        )

        # Top contributor movement from stat deltas.
        bb_players = [
            row for row in (bb.get("player_totals") or [])
            if int(row.get("impact") or 0) > 0
        ][:6] if bb.get("enabled") else []
        movers = bb_players or [row for row in player_deltas if int(row.get("activity") or 0) > 0][:6]
        if not movers:
            _empty_panel(ax_players, "No contributor movement captured")
        else:
            names = [_short_label(str(row.get("name") or "Unknown"), 18) for row in movers]
            values = [
                int(row.get("impact") or row.get("activity") or 0)
                for row in movers
            ]
            y = list(range(len(names)))
            ax_players.barh(y, values, color=PALETTE["members"], height=0.58, zorder=3)
            for idx, value in enumerate(values):
                ax_players.text(
                    value + max(values) * 0.02,
                    idx,
                    _fmt_compact(value),
                    va="center",
                    color=TEXT_COLOR,
                    fontsize=8,
                    fontweight="700",
                )
            ax_players.set_yticks(y)
            ax_players.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
            ax_players.invert_yaxis()
            ax_players.set_xlim(0, max(values) * 1.25)
            ax_players.set_title(
                "Top AlbionBB Impact" if bb_players else "Top Attendee Movement",
                color=TEXT_COLOR,
                fontsize=11,
                fontweight="700",
                loc="left",
            )
            ax_players.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
            _style_axes(ax_players)
            ax_players.grid(axis="x", linestyle="-", linewidth=0.6, color="#dde1e6", zorder=0)
            ax_players.grid(axis="y", visible=False)

        fig.text(
            0.02,
            0.01,
            "Best-effort analytics: VC flow uses event voice snapshots; AlbionBB enriches battle/role/IP context; regear still uses official killboard evidence.",
            color="#6b7280",
            fontsize=8,
        )
        return _fig_to_file(fig, f"event_report_{event_id}.png")
    except Exception as exc:  # noqa: BLE001
        error_log(f"event report graph build failed: {exc!r}\n{traceback.format_exc()}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


async def build_event_report_embed(
    bot,
    event: dict,
    *,
    threshold_pct: int = 50,
    fetch_killboard: bool = True,
    create_regear_tasks: bool = False,
    include_graph: bool = False,
    graph_files: list[discord.File] | None = None,
    extra_embeds: list[discord.Embed] | None = None,
) -> discord.Embed:
    db = bot.db
    event_id = int(event["id"])
    starts_at, ends_at, report_start, report_end = _event_window(event)
    attendance = _attendance_sets(db, event_id, threshold_pct)
    snapshot_flow = db.fetch_voice_snapshot_flow(event_id)
    all_ids: set[str] = attendance["all_ids"]
    confirmed_ids: set[str] = attendance["confirmed_ids"]
    signup_ids: set[str] = attendance["signup_ids"]

    profiles = {did: (db.fetch_user_profile(did) or {}) for did in sorted(all_ids)}
    stat_totals, player_deltas, usable_stat_players = _event_stat_deltas(
        db,
        event,
        profiles,
        confirmed_ids,
    )

    kills: list[dict] = []
    deaths: list[dict] = []
    albionbb_summary: dict = {"enabled": False, "reason": "not fetched"}
    scanned = 0
    errors = 0
    pricing_note = ""
    if fetch_killboard and confirmed_ids:
        try:
            albionbb_summary = await _fetch_albionbb_event_window(
                profiles,
                confirmed_ids,
                report_start,
                report_end,
            )
        except Exception as exc:  # noqa: BLE001
            albionbb_summary = {"enabled": False, "reason": "fetch failed", "errors": 1}
            error_log(f"AlbionBB event enrichment failed for #{event_id}: {exc!r}")

        try:
            kills, deaths, scanned, errors = await asyncio.wait_for(
                _fetch_killboard_window(
                    profiles,
                    confirmed_ids,
                    report_start,
                    report_end,
                ),
                timeout=KILLBOARD_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            pricing_note = (
                "Official killboard lookup timed out; AlbionBB battle intel is still "
                "shown, but regear detail may need manual review."
            )
            errors += 1
            error_log(
                f"event report official killboard lookup timed out for event #{event_id} "
                f"after {KILLBOARD_LOOKUP_TIMEOUT_SECONDS}s"
            )
        except Exception as exc:  # noqa: BLE001
            pricing_note = (
                "Official killboard lookup failed; AlbionBB battle intel is still "
                "shown, but regear detail may need manual review."
            )
            errors += 1
            error_log(
                f"event report official killboard lookup failed for event #{event_id}: {exc!r}"
            )
        if deaths:
            try:
                deaths = await asyncio.wait_for(
                    enrich_death_summaries_with_estimates(deaths),
                    timeout=GEAR_PRICING_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                pricing_note = (
                    "Gear pricing timed out; death rows are still listed, "
                    "but value needs manual review."
                )
                error_log(
                    f"event report gear pricing timed out for event #{event_id} "
                    f"after {GEAR_PRICING_TIMEOUT_SECONDS}s"
                )
            except Exception as exc:  # noqa: BLE001
                pricing_note = (
                    "Gear pricing failed; death rows are still listed, "
                    "but value needs manual review."
                )
                error_log(f"event report gear pricing failed for event #{event_id}: {exc!r}")

    kill_fame_value = sum(int(k.get("fame") or 0) for k in kills)
    death_fame_value = sum(int(d.get("fame") or 0) for d in deaths)
    net_fame_value = kill_fame_value - death_fame_value
    est_gear_loss = sum(int(d.get("estimated_value") or 0) for d in deaths)
    manual_death_values = sum(1 for d in deaths if int(d.get("estimated_value") or 0) <= 0)
    loot_summary = db.fetch_event_loot_summary(event_id)
    gross_loot = int((loot_summary or {}).get("gross_loot") or 0)
    guild_cut = int((loot_summary or {}).get("guild_cut") or 0)
    distributable_loot = max(0, gross_loot - guild_cut)
    net_after_loss = distributable_loot - est_gear_loss if gross_loot else 0
    signups_count = len(signup_ids)
    confirmed_count = len(confirmed_ids)
    not_confirmed_count = max(0, signups_count - len(signup_ids & confirmed_ids))
    voice_only_count = len(confirmed_ids - signup_ids)
    registered_count = sum(
        1 for did in confirmed_ids
        if (profiles.get(did) or {}).get("albion_player_id")
    )

    color = discord.Color.green() if confirmed_count else discord.Color.gold()
    embed = discord.Embed(
        title=f"Event Report - #{event_id} {event.get('title') or 'LFG'}",
        description=(
            f"Window: {_discord_ts(starts_at, 'f')} to {_discord_ts(ends_at, 't')}\n"
            "Use this for attendance, performance review, raffle eligibility, "
            "and regear evidence."
        ),
        color=color,
        timestamp=dt.datetime.now(UTC),
    )

    attendance_lines = [
        f"Signed up: **{signups_count}**",
        f"Confirmed in event VC: **{confirmed_count}**",
        f"Signed but not VC-confirmed: **{not_confirmed_count}**",
    ]
    if voice_only_count:
        attendance_lines.append(f"Voice-only attendees: **{voice_only_count}**")
    attendance_lines.append(
        f"Voice threshold: **{attendance['threshold_pct']}%** of strongest snapshot "
        f"({attendance['threshold']} snapshot(s))"
    )
    embed.add_field(name="Attendance", value="\n".join(attendance_lines), inline=False)

    stat_lines = [
        f"{label}: **{_fmt_num(stat_totals[key])}**"
        for key, label in STAT_METRICS
        if int(stat_totals.get(key) or 0) > 0
    ]
    if not stat_lines:
        stat_lines.append("No stat movement captured in stored snapshots yet.")
    stat_lines.append(f"Players with usable stat snapshots: **{usable_stat_players}/{confirmed_count}**")
    embed.add_field(name="Stat Growth", value=_clamp("\n".join(stat_lines)), inline=False)

    bb = albionbb_summary or {}
    if bb.get("enabled"):
        bb_totals = bb.get("totals") or {}
        enemy_lines: list[str] = []
        for enemy in (bb.get("enemy_guilds") or [])[:4]:
            alliance = f" [{enemy.get('alliance')}]" if enemy.get("alliance") else ""
            enemy_lines.append(
                f"{enemy.get('name')}{alliance}: "
                f"{_fmt_num(enemy.get('kill_fame'))} fame, "
                f"{len(enemy.get('battles') or [])} battle(s)"
            )
        battle_links: list[str] = []
        for battle_id in (bb.get("battle_ids") or [])[:5]:
            battle_links.append(f"[{battle_id}]({albionbb_api.battle_url(battle_id, server=bb.get('server') or 'americas')})")
        bb_lines = [
            f"Battles matched: **{bb_totals.get('battles', 0)}**",
            f"Attendee battle rows: **{bb_totals.get('attendance_rows', 0)}**",
            f"Avg IP: **{bb_totals.get('avg_ip') or 'n/a'}**",
            f"Player kills/deaths in rows: **{bb_totals.get('kills', 0)} / {bb_totals.get('deaths', 0)}**",
            f"Damage / healing: **{_fmt_num(bb_totals.get('damage'))} / {_fmt_num(bb_totals.get('heal'))}**",
            f"Attendee kill/death fame: **{_fmt_num(bb_totals.get('kill_fame'))} / {_fmt_num(bb_totals.get('death_fame'))}**",
        ]
        if battle_links:
            bb_lines.append("Battle links: " + ", ".join(battle_links))
        if enemy_lines:
            bb_lines.append("Top opposing guilds:\n" + "\n".join(enemy_lines))
        if bb.get("errors"):
            bb_lines.append(f"AlbionBB lookup errors: **{bb['errors']}**")
        embed.add_field(
            name="AlbionBB Battle Intel",
            value=_clamp("\n".join(bb_lines)),
            inline=False,
        )
    elif fetch_killboard:
        reason = str(bb.get("reason") or "no matching battle rows")
        embed.add_field(
            name="AlbionBB Battle Intel",
            value=f"No AlbionBB enrichment for this run yet: **{reason}**.",
            inline=False,
        )

    value_lines = [
        f"Kills found: **{len(kills)}**",
        f"Deaths found: **{len(deaths)}**",
        f"Kill fame destroyed: **{_fmt_num(kill_fame_value)}**",
        f"Death fame lost: **{_fmt_num(death_fame_value)}**",
        f"Net killboard fame: **{_fmt_num(net_fame_value)}**",
    ]
    if deaths:
        value_lines.append(f"Estimated gear loss: **{_fmt_num(est_gear_loss)}**")
        if manual_death_values:
            value_lines.append(f"Deaths needing manual price check: **{manual_death_values}**")
        if pricing_note:
            value_lines.append(pricing_note)
    if not fetch_killboard:
        value_lines.append("Killboard lookup skipped for this report.")
    elif errors:
        value_lines.append(f"Killboard lookup errors: **{errors}**")
    bb_kills = int(((albionbb_summary or {}).get("totals") or {}).get("kills") or 0)
    bb_deaths = int(((albionbb_summary or {}).get("totals") or {}).get("deaths") or 0)
    if bb_kills and bb_kills != len(kills):
        value_lines.append(
            f"AlbionBB attendee kill rows: **{bb_kills}** "
            f"(official kill events found: **{len(kills)}**)."
        )
    if bb_deaths and bb_deaths != len(deaths):
        value_lines.append(
            f"AlbionBB attendee death rows: **{bb_deaths}** "
            f"(official regear-detail deaths found: **{len(deaths)}**)."
        )
    value_lines.append(
        "Note: Albion killboard fame is not silver loot value; gear loss is best-effort market pricing."
    )
    embed.add_field(name="Combat / Value", value=_clamp("\n".join(value_lines)), inline=False)

    if gross_loot:
        loot_lines = [
            f"Gross loot entered: **{_fmt_num(gross_loot)}**",
            f"Guild cut / reserve: **{_fmt_num(guild_cut)}**",
            f"Distributable loot: **{_fmt_num(distributable_loot)}**",
            f"Estimated gear loss: **{_fmt_num(est_gear_loss)}**",
            f"Net after losses: **{_fmt_num(net_after_loss)}**",
        ]
        if loot_summary and loot_summary.get("updated_by"):
            loot_lines.append(f"Updated by: <@{loot_summary['updated_by']}>")
        if loot_summary and loot_summary.get("updated_at"):
            updated_at = _parse_dt(loot_summary.get("updated_at"))
            loot_lines.append(f"Updated: {_discord_ts(updated_at, 'R')}")
        if loot_summary and loot_summary.get("notes"):
            loot_lines.append(f"Notes: {_clamp(loot_summary.get('notes'), 350)}")
    else:
        loot_lines = [
            "No loot value entered yet.",
            "Click **Input Event Loot** to compare loot value against estimated gear losses.",
        ]
    loot_lines.append("This is analytics-only; actual payouts still use `/loot split`.")
    embed.add_field(name="Loot / Profit-Loss", value=_clamp("\n".join(loot_lines)), inline=False)

    if deaths:
        regear_lines = [
            _regear_death_line(death, profiles=profiles, signup_ids=signup_ids)
            for death in deaths
        ]
        est_total = sum(int(death.get("estimated_value") or 0) for death in deaths)
        manual_count = sum(1 for death in deaths if int(death.get("estimated_value") or 0) <= 0)
        summary_lines = [
            f"VC-confirmed attendee deaths: **{len(deaths)}**",
            f"Estimated gear value found: **{_fmt_num(est_total)}**",
        ]
        if manual_count:
            summary_lines.append(f"Manual pricing needed: **{manual_count}** death(s)")
        if extra_embeds is not None:
            summary_lines.append("Full death-by-death review is posted in the continuation embed(s) below.")
            detail_lines = [
                *regear_lines,
                "Officer review still decides approval and payout value.",
            ]
            _append_paged_fields(
                extra_embeds,
                title=f"Regear Review - #{event_id} {event.get('title') or 'LFG'}",
                description=(
                    "All VC-confirmed attendee deaths found in the event window. "
                    "Rows are paged instead of truncated."
                ),
                color=discord.Color.orange(),
                lines=detail_lines,
                field_base="Deaths",
            )
        else:
            summary_lines.extend(regear_lines)
            summary_lines.append("Officer review still decides approval and payout value.")
    else:
        summary_lines = ["No VC-confirmed attendee deaths found in the event window."]
    embed.add_field(name="Regear Review", value=_clamp("\n".join(summary_lines)), inline=False)

    if create_regear_tasks and deaths:
        task_counts: defaultdict[str, int] = defaultdict(int)
        created_ids: list[int] = []
        for death in deaths:
            did = str(death.get("discord_id") or "")
            if not did:
                continue
            try:
                request_id, status = await create_regear_review_from_death_summary(
                    bot,
                    discord_id=did,
                    summary=death,
                    lfg_event=event,
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"auto event regear task failed for event #{event_id}: {exc!r}")
                task_counts["failed"] += 1
                continue
            task_counts[status] += 1
            if request_id:
                created_ids.append(int(request_id))

        task_lines: list[str] = []
        if created_ids:
            task_lines.append(
                "Created: "
                + ", ".join(f"**#{rid}**" for rid in created_ids)
            )
        if task_counts.get("no_value"):
            task_lines.append(f"Needs manual pricing: **{task_counts['no_value']}**")
        if task_counts.get("duplicate"):
            task_lines.append(f"Already had regear task: **{task_counts['duplicate']}**")
        if task_counts.get("not_configured"):
            task_lines.append("Regear review channel is not configured.")
        if task_counts.get("failed"):
            task_lines.append(f"Failed: **{task_counts['failed']}**")
        if not task_lines:
            task_lines.append("No automatic regear tasks created.")
        embed.add_field(
            name="Auto Regear Tasks",
            value=_clamp("\n".join(task_lines)),
            inline=False,
        )

    top_lines = []
    for row in player_deltas[:5]:
        top_lines.append(
            f"{row['name']} - activity {_fmt_num(row['activity'])} "
            f"(PvP {_fmt_num(row['kill_fame'])}, PvE {_fmt_num(row['pve_total'])})"
        )
    if kills:
        best = kills[0]
        victim = best.get("victim_name") or "Unknown"
        url = best.get("killboard_url") or ""
        victim_text = f"[{victim}]({url})" if url else victim
        top_lines.append(f"Best kill: {victim_text} - {_fmt_num(best.get('fame'))} fame")
    if deaths:
        worst = deaths[0]
        top_lines.append(
            f"Largest death: {worst.get('victim_name') or 'Unknown'} - "
            f"{_fmt_num(worst.get('fame'))} fame"
        )
    embed.add_field(
        name="Highlights",
        value=_clamp("\n".join(top_lines) or "No highlights captured yet."),
        inline=False,
    )

    notes = [
        f"Registered attendees scanned: **{min(registered_count, KILLBOARD_MAX_PLAYERS)}/{registered_count}**",
        f"Report window includes prep and review: {_discord_ts(report_start, 't')} - {_discord_ts(report_end, 't')}.",
    ]
    if registered_count > KILLBOARD_MAX_PLAYERS:
        notes.append(f"Killboard scan capped at {KILLBOARD_MAX_PLAYERS} players to avoid API spam.")
    embed.add_field(name="Data Notes", value=_clamp("\n".join(notes)), inline=False)
    if include_graph and graph_files is not None:
        graph = _build_event_scorecard_graph(
            event,
            attendance_counts={
                "signups": signups_count,
                "confirmed": confirmed_count,
                "not_confirmed": not_confirmed_count,
                "voice_only": voice_only_count,
            },
            snapshot_flow=snapshot_flow,
            stat_totals=stat_totals,
            player_deltas=player_deltas,
            kills=kills,
            deaths=deaths,
            kill_fame_value=kill_fame_value,
            death_fame_value=death_fame_value,
            net_fame_value=net_fame_value,
            killboard_lookup_enabled=fetch_killboard,
            loot_summary=loot_summary,
            albionbb_summary=albionbb_summary,
        )
        if graph is not None:
            embed.set_image(url=f"attachment://{graph.filename}")
            graph_files.append(graph)
    embed.set_footer(text="Attendance = signup intent + event VC proof. Regear is review-only.")
    return embed
