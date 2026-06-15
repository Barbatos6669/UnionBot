"""Weekly content schedule.

The guild's published rhythm — Monday fame farm, Tuesday faction
warfare, Wednesday low-cost BZ practice, etc. Each entry is a template
(day-of-week + optional UTC time + event_type + title) that officers can
publish to a channel and optionally use to auto-generate LFG events for
the upcoming week.

Commands:
* ``/schedule add``    — drop a new slot into the rhythm.
* ``/schedule remove`` — delete a slot.
* ``/schedule view``   — show the current week in an embed.
* ``/schedule post``   — pin a public weekly-rhythm board.
* ``/schedule generate`` — create LFG events from each active slot for
  this week's matching days. Officer-only, dry-run by default.
"""

from __future__ import annotations

import datetime as _dt

import discord
from discord import app_commands
from discord.ext import commands

from cogs._lfg_config import CFG_LFG_CHANNEL, EVENT_TYPES_BY_KEY
from cogs._lfg_helpers import _create_lfg_discussion_thread, _format_event_embed
from cogs._lfg_views import EventSignupView
from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, info_embed, is_officer, success_embed

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday"]
DAY_EMOJI = ["🌑", "⚔️", "🏴", "🛣️", "🔥", "🎉", "🌅"]


def _day_choices() -> list[app_commands.Choice[int]]:
    return [app_commands.Choice(name=d, value=i) for i, d in enumerate(DAYS)]


def _event_type_choices() -> list[app_commands.Choice[str]]:
    # Discord caps choices at 25; keep this aligned with the public
    # content-ping roles.
    keep = [
        "alliance", "pvp", "faction", "gank", "small_scale", "zvz", "hellgate",
        "crystal_arena", "duo_mists",
        "abyssal_depths", "roads", "group_dungeon", "static_dungeon",
        "ava_dungeon", "world_boss", "tracking",
        "gathering", "transport", "economy",
    ]
    out: list[app_commands.Choice[str]] = []
    for key in keep:
        t = EVENT_TYPES_BY_KEY.get(key)
        if not t:
            continue
        out.append(app_commands.Choice(name=f"{t.emoji} {t.label}", value=t.key))
    return out[:25]


def _parse_time(s: str | None) -> str | None:
    """Accept 'HH:MM' or 'HH', return canonical 'HH:MM' or None."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        if ":" in s:
            h, m = s.split(":", 1)
        else:
            h, m = s, "00"
        hi = int(h)
        mi = int(m)
        if 0 <= hi < 24 and 0 <= mi < 60:
            return f"{hi:02d}:{mi:02d}"
    except (ValueError, AttributeError):
        return None
    return None


def _event_label(key: str | None) -> str:
    t = EVENT_TYPES_BY_KEY.get(key or "")
    if not t:
        return key or "—"
    return f"{t.emoji} {t.label}"


def _format_entry(row: dict) -> str:
    time_bit = row.get("start_time") or "anytime"
    parts = [f"`{time_bit}` UTC", _event_label(row.get("event_type"))]
    if row.get("lead_role"):
        parts.append(f"lead: {row['lead_role']}")
    if row.get("comp"):
        parts.append(f"comp: `{row['comp']}`")
    sub = " · ".join(parts)
    title = row.get("title") or "(untitled)"
    inactive = "" if int(row.get("active") or 1) else " *(disabled)*"
    return f"**#{row['id']}** **{title}**{inactive}\n  {sub}"


def _build_week_embed(rows: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="📅 Weekly content schedule",
        colour=discord.Colour.blurple(),
        description=(
            "Our weekly rhythm. Members should know what's happening every "
            "day without asking. Officers run `/schedule generate` each "
            "Sunday to push the week's LFG events live."
        ),
    )
    by_day: dict[int, list[dict]] = {}
    for r in rows:
        by_day.setdefault(int(r["day_of_week"]), []).append(r)
    for i, day in enumerate(DAYS):
        slots = by_day.get(i, [])
        if not slots:
            value = "_open day_"
        else:
            value = "\n\n".join(_format_entry(s) for s in slots)
        embed.add_field(
            name=f"{DAY_EMOJI[i]} {day}",
            value=value[:1024],
            inline=False,
        )
    return embed


def _next_occurrence(day_of_week: int, start_time: str | None,
                     base: _dt.datetime) -> _dt.datetime:
    """Return the next datetime (UTC) on or after ``base`` whose weekday
    matches ``day_of_week`` and time matches ``start_time`` (defaults to
    20:00 UTC if no time is set)."""
    hh, mm = 20, 0
    if start_time:
        try:
            hh, mm = [int(p) for p in start_time.split(":", 1)]
        except (ValueError, IndexError):
            pass
    today = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    days_ahead = (day_of_week - today.weekday()) % 7
    return today + _dt.timedelta(days=days_ahead)


class WeeklyScheduleCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot

    schedule = app_commands.Group(
        name="schedule",
        description="Weekly content rhythm — the guild's published schedule.",
    )

    # ── /schedule add ───────────────────────────────────────────────────────
    @schedule.command(name="add", description="Add a weekly slot.")
    @app_commands.describe(
        day="Day of week.",
        title="Short slot name (e.g. 'Faction WB Night').",
        event_type="Content type.",
        start_time="UTC start time (HH:MM). Leave blank for 'anytime'.",
        duration_min="Length in minutes (default 120).",
        description="Optional details / what to bring.",
        comp="Optional comp name from /comp.",
        lead_role="Who leads this slot (e.g. 'Shotcaller', 'Mentor').",
    )
    @app_commands.choices(day=_day_choices(), event_type=_event_type_choices())
    async def schedule_add(
        self, interaction: discord.Interaction,
        day: app_commands.Choice[int],
        title: str,
        event_type: app_commands.Choice[str] | None = None,
        start_time: str | None = None,
        duration_min: app_commands.Range[int, 15, 720] = 120,
        description: str | None = None,
        comp: str | None = None,
        lead_role: str | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Only officers can edit the schedule."),
                ephemeral=True,
            )
            return
        canon = _parse_time(start_time)
        if start_time and canon is None:
            await interaction.response.send_message(
                embed=error_embed("Bad time",
                                  "Use UTC `HH:MM` format (e.g. `20:30`)."),
                ephemeral=True,
            )
            return
        new_id = self.bot.db.schedule_add(
            day_of_week=int(day.value),
            start_time=canon,
            duration_min=int(duration_min),
            event_type=(event_type.value if event_type else None),
            title=title, description=description, comp=comp,
            lead_role=lead_role,
            created_by=str(interaction.user.id),
        )
        if not new_id:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't save."),
                ephemeral=True,
            )
            return
        info_log(
            f"{interaction.user} added schedule slot #{new_id}: "
            f"{DAYS[day.value]} {canon or 'anytime'} - {title}"
        )
        await interaction.response.send_message(
            embed=success_embed(
                f"Slot #{new_id} added",
                f"**{DAYS[day.value]}** {canon or 'anytime'} UTC — **{title}**\n"
                f"Use `/schedule view` to see the full week.",
            ),
            ephemeral=True,
        )

    # ── /schedule remove ────────────────────────────────────────────────────
    @schedule.command(name="remove", description="Delete a weekly slot.")
    @app_commands.describe(entry_id="The slot ID (see /schedule view).")
    async def schedule_remove(
        self, interaction: discord.Interaction, entry_id: int,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Officers only."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.schedule_remove(int(entry_id))
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Not found",
                                  f"No schedule slot `#{entry_id}`."),
                ephemeral=True,
            )
            return
        info_log(f"{interaction.user} removed schedule slot #{entry_id}.")
        await interaction.response.send_message(
            embed=success_embed("Removed",
                                f"Schedule slot **#{entry_id}** deleted."),
            ephemeral=True,
        )

    # ── /schedule toggle ────────────────────────────────────────────────────
    @schedule.command(name="toggle", description="Enable / disable a slot.")
    @app_commands.describe(
        entry_id="The slot ID.",
        active="True to enable, False to disable (keeps in DB).",
    )
    async def schedule_toggle(
        self, interaction: discord.Interaction,
        entry_id: int, active: bool,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Officers only."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.schedule_set_active(int(entry_id), bool(active))
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Not found",
                                  f"No schedule slot `#{entry_id}`."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Slot updated",
                f"Slot **#{entry_id}** now "
                f"{'**active**' if active else '**inactive**'}.",
            ),
            ephemeral=True,
        )

    # ── /schedule view ──────────────────────────────────────────────────────
    @schedule.command(name="view", description="Show the current weekly rhythm.")
    @app_commands.describe(
        include_disabled="Also show disabled slots.",
    )
    async def schedule_view(
        self, interaction: discord.Interaction,
        include_disabled: bool = False,
    ) -> None:
        rows = self.bot.db.schedule_list(include_inactive=include_disabled)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    "Schedule is empty",
                    "Use `/schedule add` to start building the week.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=_build_week_embed(rows), ephemeral=True,
        )

    # ── /schedule post ──────────────────────────────────────────────────────
    @schedule.command(
        name="post",
        description="Post a public weekly schedule board in this channel.",
    )
    async def schedule_post(
        self, interaction: discord.Interaction,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Officers only."),
                ephemeral=True,
            )
            return
        rows = self.bot.db.schedule_list()
        embed = _build_week_embed(rows)
        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await interaction.response.send_message(
                embed=error_embed("Bad channel",
                                  "Can't post in this channel."),
                ephemeral=True,
            )
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"weekly schedule post failed in {channel}: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed(
                    "Couldn't post",
                    f"I couldn't post to {channel.mention}. Check that I have "
                    "View Channel, Send Messages and Embed Links there.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed("Schedule posted",
                                "Weekly rhythm is now public."),
            ephemeral=True,
        )
        info_log(f"{interaction.user} posted weekly schedule.")

    # ── /schedule generate ──────────────────────────────────────────────────
    @schedule.command(
        name="generate",
        description="Create LFG events from the schedule for the next 7 days.",
    )
    @app_commands.describe(
        confirm="Set True to actually create events. Default = dry run.",
        skip_existing="Skip slots whose time already has an open LFG event.",
        post_to_channel="Also post each new event to the LFG channel.",
    )
    async def schedule_generate(
        self, interaction: discord.Interaction,
        confirm: bool = False,
        skip_existing: bool = True,
        post_to_channel: bool = True,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        db = self.bot.db
        rows = db.schedule_list()
        if not rows:
            await interaction.followup.send(
                embed=info_embed("Nothing to generate",
                                 "The schedule is empty."),
                ephemeral=True,
            )
            return
        now = _dt.datetime.utcnow()
        # Skip any slot whose next occurrence is sooner than ~10 minutes
        # away so we don't spawn a "starts in 2 minutes" event by accident.
        cutoff = now + _dt.timedelta(minutes=10)

        # If skip_existing, pull all open events in the window and we'll
        # match by (event_type, starts_at within 1h) to avoid duplicates.
        existing_keys: set[tuple[str, str]] = set()
        if skip_existing:
            try:
                for ev in db.fetch_open_lfg_events():
                    k = (
                        (ev.get("event_type") or "").lower(),
                        (ev.get("starts_at") or "")[:16],  # truncate seconds
                    )
                    existing_keys.add(k)
            except Exception:  # noqa: BLE001
                pass

        plan: list[tuple[dict, _dt.datetime, _dt.datetime, str]] = []
        skipped: list[str] = []
        for slot in rows:
            occ = _next_occurrence(int(slot["day_of_week"]),
                                   slot.get("start_time"), now)
            if occ < cutoff:
                # rolled to next week
                occ = occ + _dt.timedelta(days=7)
            ends = occ + _dt.timedelta(
                minutes=int(slot.get("duration_min") or 120),
            )
            key = (
                (slot.get("event_type") or "").lower(),
                occ.isoformat(" ", "minutes"),
            )
            if skip_existing and key in existing_keys:
                skipped.append(
                    f"#{slot['id']} {slot.get('title')} "
                    f"(already on {occ:%a %H:%M})"
                )
                continue
            plan.append((slot, occ, ends, key[1]))

        if not plan and not skipped:
            await interaction.followup.send(
                embed=info_embed("Nothing to do",
                                 "No upcoming slots in the next 7 days."),
                ephemeral=True,
            )
            return

        # Build summary.
        lines = []
        for slot, occ, ends, _ in plan:
            lines.append(
                f"• **{occ:%a %m/%d %H:%M}** UTC — **{slot.get('title')}** "
                f"({_event_label(slot.get('event_type'))})"
            )
        body = "\n".join(lines) if lines else "_nothing new_"
        if skipped:
            body += "\n\n**Skipped (already scheduled):**\n• " + \
                "\n• ".join(skipped[:10])

        if not confirm:
            await interaction.followup.send(
                embed=info_embed(
                    f"Dry run — {len(plan)} event(s) would be created",
                    body
                    + "\n\nRun again with **confirm: True** to actually post.",
                ),
                ephemeral=True,
            )
            return

        # Actually create the events. If ``post_to_channel`` is True we also
        # surface each one in the configured LFG channel using the same
        # embed + signup view that ``/lfg create`` uses, so members can sign
        # up immediately instead of waiting for the next /lfg list call.
        created: list[int] = []
        posted: list[int] = []
        post_channel: discord.TextChannel | None = None
        if post_to_channel:
            chan_id = db.get_config(CFG_LFG_CHANNEL)
            if chan_id:
                try:
                    raw = (
                        interaction.guild.get_channel(int(chan_id))
                        if interaction.guild else None
                    ) or await self.bot.fetch_channel(int(chan_id))
                    if isinstance(raw, discord.TextChannel):
                        post_channel = raw
                except (discord.NotFound, discord.Forbidden, ValueError):
                    post_channel = None
        for slot, occ, ends, _ in plan:
            new_id = db.create_lfg_event(
                slot_label="SCHEDULED",
                is_prime=False,
                title=str(slot.get("title") or "Scheduled event"),
                description=str(slot.get("description") or ""),
                comp_notes=str(slot.get("comp") or ""),
                starts_at=occ.isoformat(" ", "seconds"),
                ends_at=ends.isoformat(" ", "seconds"),
                prep_minutes=30, review_minutes=15,
                creator_id=str(interaction.user.id),
                event_type=slot.get("event_type"),
            )
            if new_id:
                created.append(new_id)
                if post_channel is not None:
                    try:
                        ev = db.fetch_lfg_event(new_id)
                        if ev:
                            msg = await post_channel.send(
                                embed=_format_event_embed(db, ev),
                                view=EventSignupView(int(new_id)),
                            )
                            try:
                                db.set_lfg_message(
                                    int(new_id), str(post_channel.id),
                                    str(msg.id),
                                )
                                await _create_lfg_discussion_thread(db, ev, msg)
                            except Exception:  # noqa: BLE001
                                pass
                            posted.append(new_id)
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        error_log(
                            f"schedule: post failed for event #{new_id}: {exc!r}"
                        )
            else:
                error_log(
                    f"schedule: failed to create event from slot "
                    f"#{slot.get('id')}"
                )
        info_log(
            f"{interaction.user} ran /schedule generate: "
            f"created {len(created)} event(s), posted {len(posted)}, "
            f"skipped {len(skipped)}"
        )
        post_summary = (
            f"\n\nPosted **{len(posted)}** to "
            f"{post_channel.mention if post_channel else 'the LFG channel'}."
            if post_to_channel and created
            else ""
        )
        if post_to_channel and created and post_channel is None:
            post_summary = (
                "\n\n_No LFG channel is configured — run "
                "`/lfg set-post-channel` so future generated events post "
                "automatically._"
            )
        await interaction.followup.send(
            embed=success_embed(
                f"Generated {len(created)} event(s)",
                body
                + "\n\nEvent IDs: "
                + ", ".join(f"#{i}" for i in created)
                + post_summary,
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(WeeklyScheduleCog(bot))
    info_log("Initialized WeeklySchedule cog.")
