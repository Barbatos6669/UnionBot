"""Guild prime-time analyzer.

Tells you *when* your members are actually active by mining timestamps
the bot already collects:

* ``lfg_signups.signed_at``  — when members engage with events.
* ``lfg_events.starts_at``   — when scheduled events run, weighted by
  attendance / signups.
* ``voice_activity``         — daily voice seconds per member (day-of-
  week patterns).
* ``event_voice_snapshots``  — who was actually in voice during past
  events (joined to ``lfg_events.starts_at`` for hour-of-day info).

All times are stored in UTC. Albion server time is UTC, so the heatmap
is shown in UTC by default. Pass ``tz_offset`` (hours from UTC, e.g.
``-5`` for EST) to shift the labels.
"""
from __future__ import annotations

import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from cogs._primetime_claims import (
    TRACKER_TYPE as PRIME_CLAIMS_TRACKER_TYPE,
    PrimeClaimsRefreshView,
    build_prime_claims_embed,
    normalize_claim_window,
)
from debug import info_log, error_log
from utils import error_embed, info_embed, success_embed

# ── constants ────────────────────────────────────────────────────────────

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Five intensity levels for the heatmap (low → high).
BLOCKS = [" ", "·", "░", "▒", "▓", "█"]

_CLAIM_WINDOW_CHOICES = [
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="Next 7 days", value="week"),
]


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(ts: str) -> Optional[datetime.datetime]:
    """Parse a stored timestamp into a UTC-aware datetime. Returns None
    on failure. Handles both ``YYYY-MM-DD HH:MM:SS`` (CURRENT_TIMESTAMP)
    and ``YYYY-MM-DDTHH:MM:SS`` (ISO).
    """
    if not ts:
        return None
    s = ts.strip().replace("T", " ")
    # Strip fractional seconds / timezone if present.
    if "." in s:
        s = s.split(".", 1)[0]
    if "+" in s:
        s = s.split("+", 1)[0]
    try:
        dt = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        try:
            dt = datetime.datetime.strptime(s[:10], "%Y-%m-%d")
            return dt.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return None


def _shift_hour(hour_utc: int, tz_offset: int) -> int:
    """Shift a UTC hour by ``tz_offset`` and wrap into 0..23."""
    return (hour_utc + tz_offset) % 24


def _shift_dow(dow_utc: int, hour_utc: int, tz_offset: int) -> int:
    """Given a (Mon=0..Sun=6) UTC weekday and UTC hour, return the
    weekday after shifting by ``tz_offset`` hours.
    """
    shifted_hour = hour_utc + tz_offset
    day_delta = 0
    if shifted_hour < 0:
        day_delta = -1
    elif shifted_hour >= 24:
        day_delta = 1
    return (dow_utc + day_delta) % 7


def _intensity_char(value: int, peak: int) -> str:
    if peak <= 0 or value <= 0:
        return BLOCKS[0]
    ratio = value / peak
    idx = min(len(BLOCKS) - 1, max(1, int(ratio * (len(BLOCKS) - 1) + 0.5)))
    return BLOCKS[idx]


def _format_heatmap(
    grid: list[list[int]],
    tz_offset: int,
    title: str,
    total: int,
) -> str:
    """Render a 7×24 grid as a monospace block, with a top-5 hour
    summary underneath.
    """
    peak = max((max(row) for row in grid), default=0)
    tz_label = f"UTC{tz_offset:+d}" if tz_offset else "UTC"
    header = "      " + " ".join(f"{h:02d}" for h in range(24))
    lines = [f"{title}  ({tz_label}, {total} samples)", "", header]
    for d, row in enumerate(grid):
        cells = " ".join(_intensity_char(v, peak) + " " for v in row).rstrip()
        lines.append(f"{DAYS[d]}   {cells}")
    # Top hour-of-week slots.
    flat = [
        (grid[d][h], d, h)
        for d in range(7)
        for h in range(24)
        if grid[d][h] > 0
    ]
    flat.sort(reverse=True)
    if flat:
        lines.append("")
        lines.append("Top slots:")
        for n, (count, d, h) in enumerate(flat[:5], start=1):
            lines.append(f"  {n}. {DAYS[d]} {h:02d}:00 — {count}")
    return "```\n" + "\n".join(lines) + "\n```"


# ── cog ──────────────────────────────────────────────────────────────────

class PrimeTime(commands.Cog):
    """Tells officers *when* the guild is actually online."""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    group = app_commands.Group(
        name="primetime",
        description="Find the guild's actual prime time from real data.",
    )

    # ── /primetime heatmap ─────────────────────────────────────────────

    @group.command(
        name="heatmap",
        description="7×24 heatmap of guild activity from signup timestamps.",
    )
    @app_commands.describe(
        days="Look back this many days (default 60).",
        tz_offset="Hours from UTC for label shift (e.g. -5 for EST).",
        source="Which timestamps to use.",
    )
    @app_commands.choices(source=[
        app_commands.Choice(name="LFG signups (engagement)", value="signups"),
        app_commands.Choice(name="Voice presence at events", value="voice"),
        app_commands.Choice(name="Scheduled event starts",   value="starts"),
    ])
    async def heatmap(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 7, 365] = 60,
        tz_offset: app_commands.Range[int, -12, 14] = 0,
        source: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        src = source.value if source else "signups"
        since = (_now_utc() - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            rows, label = self._fetch_timestamps(src, since)
        except Exception as exc:  # noqa: BLE001
            error_log(f"primetime heatmap fetch failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Heatmap", f"Query failed: {exc}"),
            )
            return

        grid: list[list[int]] = [[0] * 24 for _ in range(7)]
        total = 0
        for ts_raw, weight in rows:
            dt = _parse_iso(ts_raw)
            if not dt:
                continue
            dow_utc = dt.weekday()        # Mon=0
            hour_utc = dt.hour
            dow = _shift_dow(dow_utc, hour_utc, tz_offset)
            hour = _shift_hour(hour_utc, tz_offset)
            grid[dow][hour] += int(weight)
            total += int(weight)

        if total == 0:
            await interaction.followup.send(
                embed=info_embed(
                    "Prime-Time Heatmap",
                    f"No {label.lower()} data in the last {days} days yet. "
                    f"Try a different source or wait for more activity.",
                ),
            )
            return

        body = _format_heatmap(
            grid,
            tz_offset,
            title=f"{label} — last {days}d",
            total=total,
        )
        await interaction.followup.send(body)

    def _fetch_timestamps(
        self,
        source: str,
        since_iso: str,
    ) -> tuple[list[tuple[str, int]], str]:
        """Return ``(rows, label)`` where rows = list of (timestamp, weight)."""
        db = self.bot.db
        if not db.connection:
            db.connect()
        cur = db.cursor
        if source == "signups":
            cur.execute(
                "SELECT signed_at FROM lfg_signups WHERE signed_at >= ?",
                (since_iso,),
            )
            return ([(r["signed_at"], 1) for r in cur.fetchall()],
                    "LFG signups")
        if source == "starts":
            # Weight by signup count: a 12-signup event matters more
            # than a 2-signup event.
            cur.execute(
                """
                SELECT e.starts_at AS ts,
                       COUNT(s.id) AS n
                FROM lfg_events e
                LEFT JOIN lfg_signups s ON s.event_id = e.id
                WHERE e.starts_at >= ?
                  AND e.status != 'cancelled'
                GROUP BY e.id
                """,
                (since_iso,),
            )
            return ([(r["ts"], max(1, int(r["n"]))) for r in cur.fetchall()],
                    "Event start times")
        if source == "voice":
            # event_voice_snapshots has no timestamp of its own — use
            # the parent event's starts_at. Each snapshot row = one
            # person seen in voice, so weight 1 each.
            cur.execute(
                """
                SELECT e.starts_at AS ts,
                       COUNT(*) AS n
                FROM event_voice_snapshots v
                JOIN lfg_events e ON e.id = v.event_id
                WHERE e.starts_at >= ?
                GROUP BY e.id
                """,
                (since_iso,),
            )
            return ([(r["ts"], int(r["n"])) for r in cur.fetchall()],
                    "Voice presence at events")
        raise ValueError(f"unknown source {source!r}")

    # ── /primetime weekday ─────────────────────────────────────────────

    @group.command(
        name="weekday",
        description="Day-of-week voice activity bars (from voice_activity).",
    )
    @app_commands.describe(
        days="Look back this many days (default 60).",
    )
    async def weekday(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 7, 365] = 60,
    ) -> None:
        await interaction.response.defer(thinking=True)
        since = (_now_utc() - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%d"
        )
        try:
            db = self.bot.db
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT date_utc, SUM(seconds) AS s "
                "FROM voice_activity WHERE date_utc >= ? "
                "GROUP BY date_utc",
                (since,),
            )
            rows = db.cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            error_log(f"primetime weekday failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Weekday", f"Query failed: {exc}"),
            )
            return

        totals = [0] * 7
        for r in rows:
            try:
                d = datetime.datetime.strptime(r["date_utc"], "%Y-%m-%d")
            except ValueError:
                continue
            totals[d.weekday()] += int(r["s"] or 0)

        if not any(totals):
            await interaction.followup.send(
                embed=info_embed(
                    "Weekday Activity",
                    f"No voice activity recorded in the last {days} days.",
                ),
            )
            return

        peak = max(totals)
        bar_width = 30
        lines = [f"Voice activity by weekday — last {days}d (UTC)", ""]
        for i, t in enumerate(totals):
            hours = t / 3600
            n = int(bar_width * (t / peak)) if peak else 0
            bar = "█" * n + "·" * (bar_width - n)
            lines.append(f"{DAYS[i]}  {bar}  {hours:6.1f}h")
        best = totals.index(peak)
        lines.append("")
        lines.append(f"Strongest day: {DAYS[best]} ({peak / 3600:.1f}h total)")
        await interaction.followup.send("```\n" + "\n".join(lines) + "\n```")

    # ── /primetime events ──────────────────────────────────────────────

    @group.command(
        name="events",
        description="Which event slots actually filled. Ranked by signups.",
    )
    @app_commands.describe(
        days="Look back this many days (default 90).",
        tz_offset="Hours from UTC for label shift (e.g. -5 for EST).",
        limit="Show top N slots.",
    )
    async def events(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 7, 365] = 90,
        tz_offset: app_commands.Range[int, -12, 14] = 0,
        limit: app_commands.Range[int, 5, 30] = 15,
    ) -> None:
        await interaction.response.defer(thinking=True)
        since = (_now_utc() - datetime.timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            db = self.bot.db
            if not db.connection:
                db.connect()
            db.cursor.execute(
                """
                SELECT e.starts_at AS ts,
                       e.title     AS title,
                       e.event_type AS etype,
                       COUNT(s.id) AS signups
                FROM lfg_events e
                LEFT JOIN lfg_signups s ON s.event_id = e.id
                WHERE e.starts_at >= ?
                  AND e.status != 'cancelled'
                GROUP BY e.id
                """,
                (since,),
            )
            raw = db.cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            error_log(f"primetime events failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Events", f"Query failed: {exc}"),
            )
            return

        # Group by (weekday, hour) after tz shift.
        buckets: dict[tuple[int, int], dict] = {}
        for r in raw:
            dt = _parse_iso(r["ts"])
            if not dt:
                continue
            dow = _shift_dow(dt.weekday(), dt.hour, tz_offset)
            hour = _shift_hour(dt.hour, tz_offset)
            key = (dow, hour)
            b = buckets.setdefault(key, {"signups": 0, "events": 0, "etypes": {}})
            b["signups"] += int(r["signups"] or 0)
            b["events"] += 1
            et = (r["etype"] or "other").lower()
            b["etypes"][et] = b["etypes"].get(et, 0) + 1

        if not buckets:
            await interaction.followup.send(
                embed=info_embed(
                    "Top Event Slots",
                    f"No scheduled events in the last {days} days.",
                ),
            )
            return

        ranked = sorted(
            buckets.items(),
            key=lambda kv: (kv[1]["signups"], kv[1]["events"]),
            reverse=True,
        )[:limit]

        tz_label = f"UTC{tz_offset:+d}" if tz_offset else "UTC"
        lines = [
            f"Top {len(ranked)} event slots — last {days}d ({tz_label})",
            "",
            f"{'Slot':<14} {'Events':>7} {'Signups':>8} {'Avg':>6}  Top type",
            "─" * 60,
        ]
        for (dow, hour), b in ranked:
            avg = b["signups"] / b["events"] if b["events"] else 0
            top_etype = max(b["etypes"].items(), key=lambda kv: kv[1])[0]
            slot = f"{DAYS[dow]} {hour:02d}:00"
            lines.append(
                f"{slot:<14} {b['events']:>7} {b['signups']:>8} {avg:>6.1f}  "
                f"{top_etype}"
            )
        await interaction.followup.send("```\n" + "\n".join(lines) + "\n```")

    # ── /primetime claims ──────────────────────────────────────────────

    @group.command(
        name="claims",
        description="Show who has claimed today's or this week's prime-time event slots.",
    )
    @app_commands.describe(window="Which prime-time claim window to show.")
    @app_commands.choices(window=_CLAIM_WINDOW_CHOICES)
    async def claims(
        self,
        interaction: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer()
        window_key = normalize_claim_window(window.value if window else "today")
        embed = build_prime_claims_embed(self.bot, window_key)
        await interaction.followup.send(embed=embed)
        info_log(f"{interaction.user} requested primetime claims ({window_key}).")

    # ── /primetime track-claims ────────────────────────────────────────

    @group.command(
        name="track-claims",
        description="Post a live prime-time claim dashboard that auto-updates.",
    )
    @app_commands.describe(
        window="Which prime-time claim window to track.",
        channel="Channel to post in (defaults to here).",
    )
    @app_commands.choices(window=_CLAIM_WINDOW_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def track_claims(
        self,
        interaction: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        window_key = normalize_claim_window(window.value if window else "today")
        dest = channel or interaction.channel
        if not isinstance(dest, discord.TextChannel):
            await interaction.followup.send(
                embed=error_embed("Bad channel", "Pick a text channel."),
                ephemeral=True,
            )
            return
        embed = build_prime_claims_embed(self.bot, window_key)
        msg = await dest.send(
            content=f"-# Last updated: <t:{int(discord.utils.utcnow().timestamp())}:R>",
            embed=embed,
            view=PrimeClaimsRefreshView(),
        )
        self.bot.db.upsert_live_graph(
            PRIME_CLAIMS_TRACKER_TYPE,
            window_key,
            str(dest.id),
            str(msg.id),
        )
        await interaction.followup.send(
            embed=success_embed(
                "Prime-time claim dashboard posted",
                f"Tracking **{window_key}** claims in {dest.mention}.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} started live primetime claims "
            f"({window_key}) in #{dest.name}."
        )

    # ── /primetime untrack-claims ──────────────────────────────────────

    @group.command(
        name="untrack-claims",
        description="Stop auto-updating a live prime-time claim dashboard.",
    )
    @app_commands.describe(window="Which claim dashboard to stop tracking.")
    @app_commands.choices(window=_CLAIM_WINDOW_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def untrack_claims(
        self,
        interaction: discord.Interaction,
        window: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        window_key = normalize_claim_window(window.value if window else "today")
        self.bot.db.delete_live_graph(PRIME_CLAIMS_TRACKER_TYPE, window_key)
        await interaction.response.send_message(
            embed=success_embed(
                "Tracking stopped",
                f"Live prime-time claims for **{window_key}** will no longer update.",
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(PrimeTime(bot))
    bot.add_view(PrimeClaimsRefreshView())
