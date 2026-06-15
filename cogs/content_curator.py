"""Community content curator cog — slash commands + background tick.

Heavy lifting lives in sibling modules (underscored so the auto-loader skips
them):
    cogs._content_config — constants and tiny helpers
    cogs._content_db     — schema + DB-access helpers
    cogs._content_views  — embeds, persistent views, modals, lifecycle

Slash commands (all under ``/content``):
    suggest, pool, show, open, close, config, clear-pool, post-board,
    nextvote, nextvote-close
"""

from __future__ import annotations

from cogs._typing import Bot
import datetime as dt
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import HOME_GUILD_ROLE_NAME
from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed
from cogs._lfg_config import EVENT_TYPES, EVENT_TYPES_BY_KEY
from cogs._content_config import (
    CFG_ANNOUNCE_CHANNEL,
    CFG_AUTO_LFG,
    CFG_BOARD_CHANNEL,
    CFG_BOARD_MESSAGE,
    CFG_CHANNEL,
    CFG_AVAILABILITY_DURATION_MIN,
    CFG_DURATION_HOURS,
    CFG_DAILY_TIMER_CHANNEL,
    CFG_DAILY_TIMER_FUNNEL_ENABLED,
    CFG_DAILY_TIMER_AVAIL_HOUR,
    CFG_DAILY_TIMER_AVAIL_MINUTE,
    CFG_DAILY_TIMER_MIN_AVAILABLE,
    CFG_DAILY_TIMER_PING_ROLE,
    CFG_DAILY_TIMER_VOTE_DURATION,
    CFG_DAILY_TIMER_VOTE_HOUR,
    CFG_DAILY_TIMER_VOTE_MINUTE,
    CFG_EVENT_HOUR,
    CFG_MAX_PER_USER,
    CFG_OPEN_HOUR,
    CFG_OPEN_WEEKDAY,
    CFG_WEEKLY_POLL_ENABLED,
    CFG_QUICKVOTE_DURATION_LFG,
    CFG_QUICKVOTE_DURATION_MIN,
    CFG_QUICKVOTE_LEAD_MIN,
    CFG_QUICKVOTE_OPTIONS,
    CFG_TOP_N,
    DEFAULT_QUICKVOTE_KEYS,
    QUICKVOTE_CATEGORY_KEYS,
    availability_recommendation_keys,
    cfg_int,
    daily_timer_availability_due,
    daily_timer_slot_windows,
    daily_timer_target_date,
    daily_timer_vote_due,
    is_officer,
    now_utc,
    parse_availability_slots,
    ranked_available_timer_indexes,
    utc_dt_for_discord,
)
from cogs._content_db import (
    availability_tallies,
    count_user_suggestions,
    create_daily_timer_funnel,
    ensure_schema,
    availability_slot_labels,
    fetch_availability_poll,
    fetch_daily_timer_funnel,
    fetch_open_availability_poll,
    fetch_open_poll,
    fetch_open_quickpoll,
    fetch_pending_suggestions,
    fetch_poll_suggestions,
    quickpoll_option_keys,
    update_daily_timer_funnel,
)
from cogs._content_views import (
    ContentAvailabilityPollView,
    ContentBoardView,
    ContentPollView,
    ContentQuickPollView,
    close_availability_poll,
    board_embed,
    close_poll,
    close_quickpoll,
    discord_ts_from_iso,
    open_availability_poll,
    open_poll,
    open_quickpoll,
    poll_embed,
    refresh_board_message,
)


def _officer_or_reject(interaction: discord.Interaction, db) -> bool:
    """Return True if user is officer; otherwise schedule an ephemeral reply."""
    if isinstance(interaction.user, discord.Member) and is_officer(interaction.user, db):
        return True
    # Caller is responsible for actually sending the reply — but since we
    # can only send once per interaction, we don't preemptively reply here.
    return False


class ContentCurator(commands.Cog):
    def __init__(self, bot):
        self.bot: Bot = bot
        ensure_schema(bot.db)

        # Re-register persistent views so existing messages stay interactive
        # across restarts.
        live = fetch_open_poll(bot.db)
        if live:
            try:
                bot.add_view(ContentPollView(bot, live["id"]))
            except Exception as exc:  # noqa: BLE001
                error_log(f"content-curator: could not re-register live view: {exc!r}")

        live_q = fetch_open_quickpoll(bot.db)
        if live_q:
            try:
                bot.add_view(
                    ContentQuickPollView(bot, live_q["id"], quickpoll_option_keys(live_q))
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"content-curator: could not re-register live quickvote view: {exc!r}")

        live_a = fetch_open_availability_poll(bot.db)
        if live_a:
            try:
                bot.add_view(
                    ContentAvailabilityPollView(
                        bot, live_a["id"], availability_slot_labels(live_a),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"content-curator: could not re-register live availability view: {exc!r}")

        try:
            bot.add_view(ContentBoardView(bot))
        except Exception as exc:  # noqa: BLE001
            error_log(f"content-curator: could not re-register board view: {exc!r}")

        self._tick.start()
        info_log("Initialized ContentCurator cog.")

    def cog_unload(self) -> None:
        self._tick.cancel()

    def _configured_daily_timer_channel_id(self) -> str | None:
        db = self.bot.db
        return (
            db.get_config(CFG_DAILY_TIMER_CHANNEL)
            or db.get_config(CFG_BOARD_CHANNEL)
            or db.get_config(CFG_CHANNEL)
        )

    def _configured_daily_timer_ping_role_id(self) -> str | None:
        db = self.bot.db
        role_id = (db.get_config(CFG_DAILY_TIMER_PING_ROLE) or "").strip()
        if role_id:
            return role_id
        try:
            db.cursor.execute(
                "SELECT role_id FROM discord_roles WHERE name = ? LIMIT 1",
                (HOME_GUILD_ROLE_NAME,),
            )
            row = db.cursor.fetchone()
            if row:
                return str(row["role_id"])
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _daily_timer_channel(self) -> discord.TextChannel | discord.Thread | None:
        chan_id = self._configured_daily_timer_channel_id()
        if not chan_id:
            return None
        try:
            ch = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
        except (discord.NotFound, discord.Forbidden, ValueError):
            return None
        return ch if isinstance(ch, (discord.TextChannel, discord.Thread)) else None

    async def _open_daily_timer_availability(
        self, now: dt.datetime, target_date: dt.date,
    ) -> None:
        db = self.bot.db
        target_key = target_date.isoformat()
        if fetch_daily_timer_funnel(db, target_key):
            return
        if fetch_open_availability_poll(db):
            info_log("content-curator: daily timer availability skipped; another availability poll is open.")
            return
        ch = await self._daily_timer_channel()
        if ch is None:
            error_log("content-curator: daily timer channel unreachable; skipping availability post.")
            return

        windows = daily_timer_slot_windows(target_date)
        open_windows = [
            w for w in windows
            if not db.fetch_overlapping_prime_events(
                w["starts_at"].isoformat(),  # type: ignore[union-attr]
                w["ends_at"].isoformat(),    # type: ignore[union-attr]
            )
        ]
        if not open_windows:
            update_daily_timer_funnel(
                db,
                create_daily_timer_funnel(db, target_date=target_key, availability_poll_id=0),
                {"status": "skipped", "closed_at": now_utc().isoformat()},
            )
            info_log(
                f"content-curator: daily timer availability skipped for {target_key}; "
                "all prime timers are already booked."
            )
            return

        vote_hour = cfg_int(db, CFG_DAILY_TIMER_VOTE_HOUR, 15)
        vote_min = cfg_int(db, CFG_DAILY_TIMER_VOTE_MINUTE, 0)
        vote_at = now.replace(hour=vote_hour, minute=vote_min, second=0, microsecond=0)
        if vote_at <= now:
            return
        duration_min = max(5, int((vote_at - now).total_seconds() // 60))
        title = f"Daily Prime Timer Availability - {target_date:%a %b %d} UTC"
        role_id = self._configured_daily_timer_ping_role_id()
        ping = f"<@&{role_id}> " if role_id else ""
        announcement = (
            f"{ping}**Daily prime timer planning is open.**\n"
            "Pick every timer you can realistically attend. The bot uses this to find "
            "the strongest unclaimed timer, starts a content vote, then creates one "
            "real LFG/signup with a discussion thread.\n"
            "The embed explains what content unlocks at each headcount; claimed timers "
            "are skipped so we do not double-book the same prime slot."
        )
        poll = await open_availability_poll(
            self.bot,
            ch,
            title=title,
            slots=[str(w["label"]) for w in windows],
            duration_min=duration_min,
            creator_id=str(self.bot.user.id if self.bot.user else ""),
            content=announcement,
            allowed_mentions=(
                discord.AllowedMentions(roles=[discord.Object(id=int(role_id))])
                if role_id else discord.AllowedMentions.none()
            ),
        )
        if not poll or not poll.get("message_id"):
            error_log("content-curator: daily timer availability poll failed to post.")
            return
        funnel_id = create_daily_timer_funnel(
            db,
            target_date=target_key,
            availability_poll_id=int(poll["id"]),
        )
        info_log(
            f"content-curator: opened daily timer availability funnel #{funnel_id} "
            f"for {target_key}."
        )
        await refresh_board_message(self.bot)

    async def _open_daily_timer_content_vote(self, target_date: dt.date) -> None:
        db = self.bot.db
        target_key = target_date.isoformat()
        funnel = fetch_daily_timer_funnel(db, target_key)
        if not funnel or funnel.get("status") != "availability":
            return
        availability_poll_id = funnel.get("availability_poll_id")
        if not availability_poll_id:
            return
        poll = fetch_availability_poll(db, int(availability_poll_id))
        if not poll:
            update_daily_timer_funnel(
                db, int(funnel["id"]),
                {"status": "skipped", "closed_at": now_utc().isoformat()},
            )
            return
        if poll.get("status") == "open":
            await close_availability_poll(self.bot, poll)

        tallies = availability_tallies(db, int(availability_poll_id))
        windows = daily_timer_slot_windows(target_date)
        min_available = cfg_int(db, CFG_DAILY_TIMER_MIN_AVAILABLE, 1)
        candidates = ranked_available_timer_indexes(
            tallies,
            window_count=len(windows),
            min_available=min_available,
        )
        if not candidates:
            update_daily_timer_funnel(
                db, int(funnel["id"]),
                {"status": "skipped", "closed_at": now_utc().isoformat()},
            )
            info_log(f"content-curator: daily timer funnel {target_key} skipped; no available timer.")
            return

        picked: tuple[int, int] | None = None
        blocked: list[str] = []
        for headcount, slot_index in candidates:
            window = windows[slot_index]
            overlaps = db.fetch_overlapping_prime_events(
                window["starts_at"].isoformat(),  # type: ignore[union-attr]
                window["ends_at"].isoformat(),    # type: ignore[union-attr]
            )
            if not overlaps:
                picked = (headcount, slot_index)
                break
            blocked.append(
                f"{window['slot_label']} ({headcount} available; "
                f"booked by #{overlaps[0].get('id')})"
            )
        if picked is None:
            update_daily_timer_funnel(
                db, int(funnel["id"]),
                {"status": "skipped", "closed_at": now_utc().isoformat()},
            )
            info_log(
                f"content-curator: daily timer funnel {target_key} skipped; "
                f"all available timers booked: {', '.join(blocked)}"
            )
            return

        if blocked:
            info_log(
                f"content-curator: daily timer funnel {target_key} skipped booked "
                f"timer(s): {', '.join(blocked)}"
            )

        headcount, slot_index = picked
        window = windows[slot_index]
        option_keys = availability_recommendation_keys(headcount, limit=10)
        if not option_keys:
            update_daily_timer_funnel(
                db, int(funnel["id"]),
                {"status": "skipped", "closed_at": now_utc().isoformat()},
            )
            return
        if fetch_open_quickpoll(db):
            info_log("content-curator: daily timer content vote waiting; another quickpoll is open.")
            return

        ch = await self._daily_timer_channel()
        if ch is None:
            error_log("content-curator: daily timer channel unreachable; skipping content vote.")
            return
        vote_duration = cfg_int(db, CFG_DAILY_TIMER_VOTE_DURATION, 60)
        poll_q = await open_quickpoll(
            self.bot,
            ch,
            option_keys,
            duration_min=vote_duration,
            lead_min=0,
            lfg_duration_min=max(
                15,
                int((
                    window["ends_at"] - window["starts_at"]  # type: ignore[operator]
                ).total_seconds() // 60),
            ),
            creator_id=str(self.bot.user.id if self.bot.user else ""),
            target_starts_at=window["starts_at"],  # type: ignore[arg-type]
            target_ends_at=window["ends_at"],      # type: ignore[arg-type]
            target_slot_label=str(window["slot_label"]),
            target_is_prime=True,
            content=f"@here Vote what to run for **{window['label']}**.",
        )
        if not poll_q or not poll_q.get("id"):
            error_log("content-curator: daily timer content vote failed to post.")
            return
        update_daily_timer_funnel(
            db,
            int(funnel["id"]),
            {
                "quickpoll_id": int(poll_q["id"]),
                "status": "content_vote",
                "selected_slot_index": slot_index,
                "selected_slot_label": str(window["slot_label"]),
                "selected_starts_at": window["starts_at"].isoformat(),  # type: ignore[union-attr]
                "selected_ends_at": window["ends_at"].isoformat(),      # type: ignore[union-attr]
                "selected_headcount": headcount,
                "vote_opened_at": now_utc().isoformat(),
            },
        )
        info_log(
            f"content-curator: opened daily timer content vote for {target_key} "
            f"slot={window['slot_label']} headcount={headcount}."
        )
        await refresh_board_message(self.bot)

    async def _auto_daily_timer_funnel(self, now: dt.datetime) -> None:
        db = self.bot.db
        if not cfg_int(db, CFG_DAILY_TIMER_FUNNEL_ENABLED, 0):
            return
        target_date = daily_timer_target_date(now)
        avail_hour = cfg_int(db, CFG_DAILY_TIMER_AVAIL_HOUR, 5)
        avail_min = cfg_int(db, CFG_DAILY_TIMER_AVAIL_MINUTE, 5)
        if daily_timer_availability_due(now, hour=avail_hour, minute=avail_min):
            await self._open_daily_timer_availability(now, target_date)

        vote_hour = cfg_int(db, CFG_DAILY_TIMER_VOTE_HOUR, 15)
        vote_min = cfg_int(db, CFG_DAILY_TIMER_VOTE_MINUTE, 0)
        if daily_timer_vote_due(now, hour=vote_hour, minute=vote_min):
            await self._open_daily_timer_content_vote(target_date)

    # ── slash group ────────────────────────────────────────────────────────
    group = app_commands.Group(name="content", description="Community content curator / weekly poll.")

    async def _event_type_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        cur = (current or "").lower()
        out: list[app_commands.Choice[str]] = []
        for t in EVENT_TYPES:
            if cur in t.label.lower() or cur in t.key.lower():
                out.append(app_commands.Choice(name=f"{t.emoji} {t.label}", value=t.key))
            if len(out) >= 25:
                break
        return out

    @group.command(name="suggest", description="Suggest an event for the next weekly content poll.")
    @app_commands.describe(
        event_type="Pick the event category.",
        title="Short title (what's the event).",
        notes="Optional extra notes for voters.",
    )
    @app_commands.autocomplete(event_type=_event_type_autocomplete)
    async def suggest(
        self,
        interaction: discord.Interaction,
        event_type: str,
        title: str,
        notes: Optional[str] = None,
    ) -> None:
        et = EVENT_TYPES_BY_KEY.get(event_type)
        if not et:
            await interaction.response.send_message(
                embed=error_embed("Unknown event type", f"`{event_type}` isn't a valid event type."),
                ephemeral=True,
            )
            return
        clean_title = (title or "").strip()
        if not (3 <= len(clean_title) <= 80):
            await interaction.response.send_message(
                embed=error_embed("Check the title", "Title must be 3–80 characters."),
                ephemeral=True,
            )
            return
        clean_notes = ((notes or "").strip()) or None
        if clean_notes and len(clean_notes) > 300:
            clean_notes = clean_notes[:300]

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
                    f"({'this poll' if poll else 'for the next poll'}). Limit is **{max_per}**.\n"
                    f"_Wait for the next poll or ask an officer to raise the limit._",
                ),
                ephemeral=True,
            )
            return

        db.cursor.execute(
            "INSERT INTO content_suggestions (poll_id, suggester_id, event_type, title, notes, created_at) "
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

        # Refresh live poll embed/view so the new option appears.
        if poll and poll.get("channel_id") and poll.get("message_id"):
            try:
                ch = self.bot.get_channel(int(poll["channel_id"]))
                if ch is None:
                    ch = await self.bot.fetch_channel(int(poll["channel_id"]))
                msg = await ch.fetch_message(int(poll["message_id"]))
                await msg.edit(embed=poll_embed(db, poll), view=ContentPollView(self.bot, poll["id"]))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

    @group.command(name="pool", description="Show the suggestion pool for the current/next poll.")
    async def pool(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        poll = fetch_open_poll(db)
        if poll:
            rows = fetch_poll_suggestions(db, poll["id"])
            title = f"📊 Live Poll #{poll['id']}"
            ts = discord_ts_from_iso(poll["closes_at"])
            desc = f"Closes <t:{ts}:R>"
        else:
            rows = fetch_pending_suggestions(db)
            title = "📥 Suggestion Pool (next poll)"
            desc = f"{len(rows)} suggestion(s) queued."

        embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
        if not rows:
            embed.add_field(name="Empty", value="No suggestions yet. Use `/content suggest`.", inline=False)
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

    @group.command(name="show", description="Re-post the live poll in the configured channel (officer).")
    async def show(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        poll = fetch_open_poll(db)
        if not poll:
            await interaction.response.send_message(
                embed=info_embed("No active poll", "Use `/content open` to start one."),
                ephemeral=True,
            )
            return
        chan_id = db.get_config(CFG_CHANNEL) or poll.get("channel_id")
        if not chan_id:
            await interaction.response.send_message(
                embed=error_embed("No channel", "Set one with `/content config channel:#x` first."),
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
        msg = await ch.send(embed=poll_embed(db, poll), view=ContentPollView(self.bot, poll["id"]))
        db.cursor.execute(
            "UPDATE content_polls SET channel_id = ?, message_id = ? WHERE id = ?",
            (str(ch.id), str(msg.id), poll["id"]),
        )
        db.connection.commit()
        await interaction.followup.send(
            embed=success_embed("Poll re-posted", f"New copy at {msg.jump_url}"),
            ephemeral=True,
        )

    @group.command(name="open", description="Force-open a content poll now (officer).")
    async def open_cmd(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        if fetch_open_poll(db):
            await interaction.response.send_message(
                embed=info_embed("Already open", "A poll is already running. Use `/content close` first."),
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
        else:
            await interaction.followup.send(
                embed=error_embed("Failed", "Couldn't open the poll. Check logs."),
                ephemeral=True,
            )

    @group.command(name="close", description="Force-close the active poll and curate winners (officer).")
    async def close_cmd(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
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

    @group.command(name="config", description="Configure the content curator (officer).")
    @app_commands.describe(
        channel="Channel where polls are posted.",
        announce_channel="Channel for winner announcements (defaults to poll channel).",
        open_weekday="Day to auto-open the poll (0=Mon … 6=Sun). Default 4 (Friday).",
        open_hour="Hour (UTC) to auto-open the poll. Default 18.",
        duration_hours="How long the poll stays open. Default 48h.",
        top_n="How many winners to pick. Default 3.",
        max_per_user="Max suggestions per user per poll. Default 3.",
        auto_create_lfg="Auto-post winners as LFG events.",
        event_hour="Hour (UTC) auto-created LFG events start. Default 20.",
    )
    async def config(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        announce_channel: Optional[discord.TextChannel] = None,
        open_weekday: Optional[app_commands.Range[int, 0, 6]] = None,
        open_hour: Optional[app_commands.Range[int, 0, 23]] = None,
        duration_hours: Optional[app_commands.Range[int, 1, 168]] = None,
        top_n: Optional[app_commands.Range[int, 1, 10]] = None,
        max_per_user: Optional[app_commands.Range[int, 1, 25]] = None,
        auto_create_lfg: Optional[bool] = None,
        event_hour: Optional[app_commands.Range[int, 0, 23]] = None,
    ) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        changes: list[str] = []
        if channel:
            db.set_config(CFG_CHANNEL, str(channel.id)); changes.append(f"channel={channel.mention}")
        if announce_channel:
            db.set_config(CFG_ANNOUNCE_CHANNEL, str(announce_channel.id))
            changes.append(f"announce={announce_channel.mention}")
        if open_weekday is not None:
            db.set_config(CFG_OPEN_WEEKDAY, str(int(open_weekday))); changes.append(f"weekday={int(open_weekday)}")
        if open_hour is not None:
            db.set_config(CFG_OPEN_HOUR, str(int(open_hour))); changes.append(f"open_hour={int(open_hour)}")
        if duration_hours is not None:
            db.set_config(CFG_DURATION_HOURS, str(int(duration_hours)))
            changes.append(f"duration_h={int(duration_hours)}")
        if top_n is not None:
            db.set_config(CFG_TOP_N, str(int(top_n))); changes.append(f"top_n={int(top_n)}")
        if max_per_user is not None:
            db.set_config(CFG_MAX_PER_USER, str(int(max_per_user)))
            changes.append(f"max_per_user={int(max_per_user)}")
        if auto_create_lfg is not None:
            db.set_config(CFG_AUTO_LFG, "1" if auto_create_lfg else "0")
            changes.append(f"auto_lfg={'on' if auto_create_lfg else 'off'}")
        if event_hour is not None:
            db.set_config(CFG_EVENT_HOUR, str(int(event_hour))); changes.append(f"event_hour={int(event_hour)}")

        cur_weekday = cfg_int(db, CFG_OPEN_WEEKDAY, 4)
        cur_hour = cfg_int(db, CFG_OPEN_HOUR, 18)
        cur_dur = cfg_int(db, CFG_DURATION_HOURS, 48)
        cur_top = cfg_int(db, CFG_TOP_N, 3)
        cur_max = cfg_int(db, CFG_MAX_PER_USER, 3)
        cur_auto = cfg_int(db, CFG_AUTO_LFG, 1)
        cur_eh = cfg_int(db, CFG_EVENT_HOUR, 20)
        weekday_name = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[cur_weekday % 7]

        embed = discord.Embed(
            title="Content curator config",
            color=discord.Color.blurple(),
            description=(
                f"Updated: {', '.join(changes) if changes else 'no changes'}\n\n"
                f"**Auto-open:** {weekday_name} {cur_hour:02d}:00 UTC, "
                f"open for **{cur_dur}h**\n"
                f"**Winners:** top **{cur_top}**, max **{cur_max}** suggestion(s) per user\n"
                f"**Auto-LFG:** {'on' if cur_auto else 'off'} "
                f"(events start at **{cur_eh:02d}:00 UTC**)\n"
                f"**Channel:** "
                + (f"<#{db.get_config(CFG_CHANNEL)}>" if db.get_config(CFG_CHANNEL) else "_(unset)_")
                + "\n**Announce:** "
                + (f"<#{db.get_config(CFG_ANNOUNCE_CHANNEL)}>" if db.get_config(CFG_ANNOUNCE_CHANNEL) else "_(falls back to poll channel)_")
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="clear-pool", description="Delete all pending (un-polled) suggestions (officer).")
    async def clear_pool(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        db.cursor.execute("DELETE FROM content_suggestions WHERE poll_id IS NULL")
        n = db.cursor.rowcount
        db.connection.commit()
        await interaction.response.send_message(
            embed=success_embed("Pool cleared", f"Removed **{n}** pending suggestion(s)."),
            ephemeral=True,
        )

    @group.command(
        name="post-board",
        description="Post the persistent content-curator dashboard in this channel (officer).",
    )
    async def post_board(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        ch = interaction.channel
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                embed=error_embed("Wrong channel", "Run this in a text channel."),
                ephemeral=True,
            )
            return
        db = self.bot.db
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Best-effort: delete the previous board message so we don't litter.
        old_chan = db.get_config(CFG_BOARD_CHANNEL)
        old_msg = db.get_config(CFG_BOARD_MESSAGE)
        if old_chan and old_msg:
            try:
                old_ch = self.bot.get_channel(int(old_chan)) or await self.bot.fetch_channel(int(old_chan))
                if isinstance(old_ch, (discord.TextChannel, discord.Thread)):
                    old = await old_ch.fetch_message(int(old_msg))
                    await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
                pass

        try:
            msg = await ch.send(embed=board_embed(db), view=ContentBoardView(self.bot))
        except (discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(
                embed=error_embed("Failed to post", f"`{exc!r}`"),
                ephemeral=True,
            )
            return
        db.set_config(CFG_BOARD_CHANNEL, str(ch.id))
        db.set_config(CFG_BOARD_MESSAGE, str(msg.id))
        info_log(f"content-curator: posted dashboard in #{getattr(ch, 'name', ch.id)} ({msg.id}).")
        await interaction.followup.send(
            embed=success_embed("Dashboard posted", f"Panel is live at {msg.jump_url}"),
            ephemeral=True,
        )

    # ── availability poll (special event planning) ─────────────────────────
    @group.command(
        name="availability",
        description="Start a time-window availability poll for a planned event (officer).",
    )
    @app_commands.describe(
        title="What you are trying to plan, e.g. Guild 5v5 tournament.",
        slots="Time windows separated by commas, semicolons, or new lines.",
        duration_minutes="How long responses stay open. Default 24h.",
        channel="Override where to post (defaults to current channel).",
    )
    async def availability(
        self,
        interaction: discord.Interaction,
        title: str,
        slots: str,
        duration_minutes: Optional[app_commands.Range[int, 5, 10080]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        if fetch_open_availability_poll(db):
            await interaction.response.send_message(
                embed=info_embed(
                    "Availability poll already open",
                    "Close the current one with `/content availability-close` first.",
                ),
                ephemeral=True,
            )
            return
        parsed_slots = parse_availability_slots(slots)
        if len(parsed_slots) < 2:
            await interaction.response.send_message(
                embed=error_embed(
                    "Need more time windows",
                    "Add at least **2** windows, separated by commas, semicolons, or new lines.",
                ),
                ephemeral=True,
            )
            return
        target_ch = channel or (
            interaction.channel
            if isinstance(interaction.channel, (discord.TextChannel, discord.Thread))
            else None
        )
        if target_ch is None:
            await interaction.response.send_message(
                embed=error_embed("No channel", "Pick a channel with the `channel:` option."),
                ephemeral=True,
            )
            return

        dur = (
            int(duration_minutes)
            if duration_minutes is not None
            else cfg_int(db, CFG_AVAILABILITY_DURATION_MIN, 1440)
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        poll = await open_availability_poll(
            self.bot,
            target_ch,
            title=title,
            slots=parsed_slots,
            duration_min=dur,
            creator_id=str(interaction.user.id),
        )
        if not poll:
            await interaction.followup.send(
                embed=error_embed("Failed", "Could not open the availability poll. Check logs."),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                "Availability poll started",
                f"Poll #{poll['id']} is live in {target_ch.mention} for **{dur}** minute(s).",
            ),
            ephemeral=True,
        )
        await refresh_board_message(self.bot)

    @group.command(
        name="availability-close",
        description="Force-close the active availability poll (officer).",
    )
    async def availability_close(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        poll = fetch_open_availability_poll(self.bot.db)
        if not poll:
            await interaction.response.send_message(
                embed=info_embed("No active availability poll", "Nothing to close."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await close_availability_poll(self.bot, poll)
        await interaction.followup.send(
            embed=success_embed(
                "Availability poll closed",
                f"Poll #{result['poll_id']} closed. Results were posted in the poll channel.",
            ),
            ephemeral=True,
        )
        await refresh_board_message(self.bot)

    # ── quickvote (next-activity, CoD-style) ───────────────────────────────
    async def _quickvote_options_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        cur = (current or "").lower().strip()
        out: list[app_commands.Choice[str]] = []
        for shortcut in ("all", "pvp", "pve", "small", "large", "economy", "guild"):
            if cur in shortcut:
                count = len(QUICKVOTE_CATEGORY_KEYS[shortcut])
                out.append(app_commands.Choice(
                    name=f"{shortcut} ({count} activities)", value=shortcut,
                ))
        return out[:25]

    @group.command(name="nextvote", description="Start a quick vote for the next activity (officer).")
    @app_commands.describe(
        options="Category shortcut (all/pvp/pve/small/large/economy/guild) or comma list of event_type keys.",
        duration_minutes="How long voting stays open. Default 10.",
        lead_minutes="Minutes from now until the winning event starts. Default 15.",
        event_duration_minutes="Duration of the auto-created LFG event. Default 90.",
        channel="Override where to post (defaults to current channel).",
    )
    @app_commands.autocomplete(options=_quickvote_options_autocomplete)
    async def nextvote(
        self,
        interaction: discord.Interaction,
        options: Optional[str] = None,
        duration_minutes: Optional[app_commands.Range[int, 1, 120]] = None,
        lead_minutes: Optional[app_commands.Range[int, 0, 240]] = None,
        event_duration_minutes: Optional[app_commands.Range[int, 15, 360]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        if fetch_open_quickpoll(db):
            await interaction.response.send_message(
                embed=info_embed(
                    "Quickpoll already open",
                    "Close the current one with `/content nextvote-close` first.",
                ),
                ephemeral=True,
            )
            return

        # Resolve option set.
        if options:
            raw = options.strip().lower()
            if raw in QUICKVOTE_CATEGORY_KEYS:
                keys = list(QUICKVOTE_CATEGORY_KEYS[raw])
            else:
                keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
        else:
            cfg = (db.get_config(CFG_QUICKVOTE_OPTIONS) or "").strip()
            keys = (
                [k.strip().lower() for k in cfg.split(",") if k.strip()]
                if cfg else list(DEFAULT_QUICKVOTE_KEYS)
            )
        bad = [k for k in keys if k not in EVENT_TYPES_BY_KEY]
        keys = [k for k in keys if k in EVENT_TYPES_BY_KEY][:25]
        if not keys:
            await interaction.response.send_message(
                embed=error_embed(
                    "No valid options",
                    "All option keys were unknown. Use a category shortcut "
                    "(`all`, `pvp`, `pve`, `small`, `large`, `economy`, `guild`) "
                    "or a comma-separated list like `zvz, gank, roads`.",
                ),
                ephemeral=True,
            )
            return

        dur = int(duration_minutes) if duration_minutes is not None else cfg_int(db, CFG_QUICKVOTE_DURATION_MIN, 10)
        lead = int(lead_minutes) if lead_minutes is not None else cfg_int(db, CFG_QUICKVOTE_LEAD_MIN, 15)
        lfg_dur = int(event_duration_minutes) if event_duration_minutes is not None else cfg_int(db, CFG_QUICKVOTE_DURATION_LFG, 90)

        target_ch = channel or (
            interaction.channel
            if isinstance(interaction.channel, (discord.TextChannel, discord.Thread))
            else None
        )
        if target_ch is None:
            await interaction.response.send_message(
                embed=error_embed("No channel", "Pick a channel with the `channel:` option."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        poll = await open_quickpoll(
            self.bot, target_ch, keys,
            duration_min=dur, lead_min=lead, lfg_duration_min=lfg_dur,
            creator_id=str(interaction.user.id),
        )
        if not poll:
            await interaction.followup.send(
                embed=error_embed("Failed", "Could not open the quickpoll. Check logs."),
                ephemeral=True,
            )
            return

        warn = f"\nIgnored unknown option keys: `{', '.join(bad)}`" if bad else ""
        await interaction.followup.send(
            embed=success_embed(
                "Quickpoll started",
                f"Voting open for **{dur}** minute(s) in {target_ch.mention}. "
                f"Winner's LFG will start **{lead}** min later, lasting **{lfg_dur}** min." + warn,
            ),
            ephemeral=True,
        )

    @group.command(name="nextvote-close", description="Force-close the active quick vote (officer).")
    async def nextvote_close(self, interaction: discord.Interaction) -> None:
        if not _officer_or_reject(interaction, self.bot.db):
            await interaction.response.send_message(
                embed=error_embed("No permission", "Officer-only command."), ephemeral=True,
            )
            return
        db = self.bot.db
        poll = fetch_open_quickpoll(db)
        if not poll:
            await interaction.response.send_message(
                embed=info_embed("No active quickpoll", "Nothing to close."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await close_quickpoll(self.bot, poll)
        winner_key = result.get("winner")
        if winner_key:
            et = EVENT_TYPES_BY_KEY[winner_key]
            lfg_id = result.get("lfg_event_id")
            lfg_part = f"#{lfg_id}" if lfg_id else "—"
            msg = f"Winner: {et.emoji} **{et.label}**. LFG event {lfg_part}."
        else:
            msg = "No option met its minimum player count."
        await interaction.followup.send(
            embed=success_embed("Quickpoll closed", msg),
            ephemeral=True,
        )

    # ── background loop ────────────────────────────────────────────────────
    @tasks.loop(minutes=1)
    async def _tick(self) -> None:
        try:
            await self._auto_open_or_close()
            await refresh_board_message(self.bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"content-curator _tick error: {exc!r}")

    @_tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _auto_open_or_close(self) -> None:
        db = self.bot.db
        # Auto-close any expired quickpoll first — short-lived so it's the
        # most time-sensitive thing in this loop.
        qp = fetch_open_quickpoll(db)
        if qp:
            try:
                if utc_dt_for_discord(now_utc()) >= utc_dt_for_discord(
                    dt.datetime.fromisoformat(qp["closes_at"])
                ):
                    info_log(f"content-curator: auto-closing quickpoll #{qp['id']}.")
                    await close_quickpoll(self.bot, qp)
            except (TypeError, ValueError) as exc:
                error_log(f"content-curator: bad quickpoll closes_at: {exc!r}")

        ap = fetch_open_availability_poll(db)
        if ap:
            try:
                if utc_dt_for_discord(now_utc()) >= utc_dt_for_discord(
                    dt.datetime.fromisoformat(ap["closes_at"])
                ):
                    info_log(f"content-curator: auto-closing availability poll #{ap['id']}.")
                    await close_availability_poll(self.bot, ap)
            except (TypeError, ValueError) as exc:
                error_log(f"content-curator: bad availability closes_at: {exc!r}")

        now = utc_dt_for_discord(now_utc())
        await self._auto_daily_timer_funnel(now)

        poll = fetch_open_poll(db)
        if poll:
            closes = utc_dt_for_discord(dt.datetime.fromisoformat(poll["closes_at"]))
            if now >= closes:
                info_log(f"content-curator: auto-closing poll #{poll['id']} (closes_at past).")
                await close_poll(self.bot, poll)
            return

        if cfg_int(db, CFG_WEEKLY_POLL_ENABLED, 0) <= 0:
            return

        weekday = cfg_int(db, CFG_OPEN_WEEKDAY, 4)
        hour = cfg_int(db, CFG_OPEN_HOUR, 18)
        chan_id = db.get_config(CFG_CHANNEL)
        if not chan_id:
            return

        # Open if we're on the right weekday/hour and no poll has opened today.
        if now.weekday() != weekday or now.hour < hour:
            return
        db.cursor.execute(
            "SELECT id FROM content_polls WHERE date(opened_at) = ? ORDER BY id DESC LIMIT 1",
            (now.date().isoformat(),),
        )
        if db.cursor.fetchone():
            return

        ch = self.bot.get_channel(int(chan_id))
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(int(chan_id))
            except (discord.NotFound, discord.Forbidden, ValueError):
                error_log("content-curator: configured channel unreachable; skipping auto-open.")
                return
        await open_poll(self.bot, ch)


async def setup(bot):
    await bot.add_cog(ContentCurator(bot))
