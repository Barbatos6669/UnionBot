"""Personal reminders / scheduled messages.

Provides three slash commands grouped under ``/reminder``:

* ``/reminder add when:<duration|datetime> message:<text> [channel]``
  Schedule a one-shot reminder. Default destination is DM; pass ``channel``
  to fire it in a channel instead.
* ``/reminder list``  — show your own pending reminders.
* ``/reminder cancel id:<int>`` — cancel one by id.

A background loop (``_dispatch``, every 30s) fires anything due and marks
it ``fired=1`` in the ``reminders`` table. Survives restarts — anything
that came due while the bot was offline fires on the next tick.
"""

from __future__ import annotations

from cogs._typing import Bot
import datetime as dt
import re
import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed


# ── duration / datetime parsing ─────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"^\s*((?P<d>\d+)\s*d)?\s*"
    r"((?P<h>\d+)\s*h)?\s*"
    r"((?P<m>\d+)\s*m)?\s*"
    r"((?P<s>\d+)\s*s)?\s*$",
    re.IGNORECASE,
)


def _parse_when(text: str) -> Optional[dt.datetime]:
    """Parse ``text`` into a future UTC datetime.

    Accepts:
      * Relative durations: ``2d``, ``3h``, ``30m``, ``1d 6h``, ``45s``
      * Absolute UTC: ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM``, ``YYYY-MM-DDTHH:MM``

    Returns ``None`` if the value can't be parsed or resolves to the past.
    """
    text = (text or "").strip()
    if not text:
        return None
    now = dt.datetime.now(dt.timezone.utc)

    # Duration first — only accept if at least one component matched.
    m = _DURATION_RE.match(text)
    if m and any(m.group(k) for k in ("d", "h", "m", "s")):
        days = int(m.group("d") or 0)
        hours = int(m.group("h") or 0)
        mins = int(m.group("m") or 0)
        secs = int(m.group("s") or 0)
        delta = dt.timedelta(days=days, hours=hours, minutes=mins, seconds=secs)
        if delta.total_seconds() < 30:
            return None  # too short to be useful
        return now + delta

    # Absolute timestamps (UTC).
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            when = dt.datetime.strptime(text, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if when <= now:
            return None
        return when

    return None


def _human_delta(target: dt.datetime, *, now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    secs = int((target - now).total_seconds())
    if secs <= 0:
        return "now"
    parts: list[str] = []
    for label, size in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if secs >= size:
            n = secs // size
            secs -= n * size
            parts.append(f"{n}{label}")
        if len(parts) == 2:
            break
    return " ".join(parts) or f"{secs}s"


# ── DB helpers ──────────────────────────────────────────────────────────────


def _ensure_table(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT    NOT NULL,
            channel_id  TEXT,
            message     TEXT    NOT NULL,
            due_at      TEXT    NOT NULL,
            created_at  TEXT    NOT NULL DEFAULT (CURRENT_TIMESTAMP),
            fired       INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_due "
        "ON reminders (fired, due_at)"
    )


def _add_reminder(db, *, user_id: str, channel_id: Optional[str],
                  message: str, due_at_utc: dt.datetime) -> int:
    db.execute(
        "INSERT INTO reminders (user_id, channel_id, message, due_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, channel_id, message, due_at_utc.strftime("%Y-%m-%d %H:%M:%S")),
    )
    return int(db.cursor.lastrowid)


def _fetch_due(db, *, now_utc: dt.datetime) -> list[dict]:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "SELECT id, user_id, channel_id, message, due_at FROM reminders "
        "WHERE fired = 0 AND due_at <= ? "
        "ORDER BY due_at ASC LIMIT 50",
        (now_utc.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _mark_fired(db, reminder_id: int) -> None:
    db.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))


def _fetch_user_pending(db, user_id: str) -> list[dict]:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "SELECT id, channel_id, message, due_at FROM reminders "
        "WHERE user_id = ? AND fired = 0 "
        "ORDER BY due_at ASC LIMIT 25",
        (user_id,),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _cancel(db, reminder_id: int, user_id: str) -> bool:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        "UPDATE reminders SET fired = 1 WHERE id = ? AND user_id = ? AND fired = 0",
        (reminder_id, user_id),
    )
    db.connection.commit()
    return db.cursor.rowcount > 0


# ── Cog ─────────────────────────────────────────────────────────────────────


class Reminders(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        _ensure_table(self.bot.db)  # type: ignore[attr-defined]
        self._dispatch.start()
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:  # type: ignore[override]
        self._dispatch.cancel()

    # ── group ──────────────────────────────────────────────────────────────
    group = app_commands.Group(name="reminder", description="Personal reminders.")

    @group.command(name="add", description="Schedule a reminder for yourself.")
    @app_commands.describe(
        when="When to fire: 2d, 3h, 30m, 1d6h, or 2026-05-15 14:00 (UTC).",
        message="What to remind you about.",
        channel="Optional: fire in a channel instead of DM.",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        when: str,
        message: app_commands.Range[str, 1, 500],
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        due = _parse_when(when)
        if due is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Couldn't parse `when`",
                    f"Got `{when!r}`.",
                    hint="Use a duration like `2d`, `3h`, `30m`, `1d6h`, "
                         "or an absolute UTC time like `2026-05-15 14:00`.",
                ),
                ephemeral=True,
            )
            return

        chan_id = str(channel.id) if channel else None
        rid = _add_reminder(
            self.bot.db,  # type: ignore[attr-defined]
            user_id=str(interaction.user.id),
            channel_id=chan_id,
            message=message,
            due_at_utc=due,
        )
        dest = f"in {channel.mention}" if channel else "via DM"
        ts = int(due.timestamp())
        await interaction.response.send_message(
            embed=success_embed(
                "Reminder set",
                f"#{rid} · fires <t:{ts}:R> (<t:{ts}:F>) {dest}.\n"
                f"> {message}",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} scheduled reminder #{rid} at "
            f"{due.isoformat()} dest={chan_id or 'DM'} msg={message!r}"
        )

    @group.command(name="list", description="Show your pending reminders.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = _fetch_user_pending(
            self.bot.db, str(interaction.user.id),  # type: ignore[attr-defined]
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No pending reminders", "You have no reminders queued."),
                ephemeral=True,
            )
            return
        lines: list[str] = []
        now = dt.datetime.now(dt.timezone.utc)
        for r in rows:
            try:
                due = dt.datetime.strptime(r["due_at"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=dt.timezone.utc
                )
            except Exception:
                continue
            ts = int(due.timestamp())
            dest = f"<#{r['channel_id']}>" if r["channel_id"] else "DM"
            preview = r["message"]
            if len(preview) > 80:
                preview = preview[:77] + "…"
            lines.append(
                f"**#{r['id']}** · <t:{ts}:R> ({_human_delta(due, now=now)}) · {dest}\n"
                f"  {preview}"
            )
        await interaction.response.send_message(
            embed=info_embed(
                f"Your reminders ({len(rows)})",
                "\n".join(lines),
            ),
            ephemeral=True,
        )

    @group.command(name="cancel", description="Cancel a pending reminder by id.")
    @app_commands.describe(reminder_id="The id shown in /reminder list.")
    async def cancel(
        self,
        interaction: discord.Interaction,
        reminder_id: app_commands.Range[int, 1, 1_000_000_000],
    ) -> None:
        ok = _cancel(
            self.bot.db, int(reminder_id),  # type: ignore[attr-defined]
            str(interaction.user.id),
        )
        if ok:
            await interaction.response.send_message(
                embed=success_embed(
                    "Reminder cancelled", f"Reminder #{int(reminder_id)} cancelled.",
                ),
                ephemeral=True,
            )
            info_log(f"{interaction.user} cancelled reminder #{int(reminder_id)}.")
        else:
            await interaction.response.send_message(
                embed=error_embed(
                    "Nothing to cancel",
                    f"No pending reminder #{int(reminder_id)} owned by you.",
                ),
                ephemeral=True,
            )

    # ── dispatcher ─────────────────────────────────────────────────────────
    @tasks.loop(seconds=30)
    async def _dispatch(self) -> None:
        try:
            now = dt.datetime.now(dt.timezone.utc)
            try:
                rows = _fetch_due(self.bot.db, now_utc=now)  # type: ignore[attr-defined]
            except sqlite3.Error as exc:
                error_log(f"reminders: fetch_due failed: {exc!r}")
                return
            for r in rows:
                await self._fire(r)
        except Exception as exc:  # noqa: BLE001
            error_log(f"reminders dispatch loop: {exc!r}")

    @_dispatch.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _fire(self, r: dict) -> None:
        rid = int(r["id"])
        user_id = r["user_id"]
        message = r["message"]
        channel_id = r.get("channel_id")
        embed = info_embed("🔔 Reminder", message)
        embed.set_footer(text=f"Reminder #{rid}")
        try:
            if channel_id:
                ch = self.bot.get_channel(int(channel_id))
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    await ch.send(content=f"<@{user_id}>", embed=embed)
                else:
                    # Channel vanished — fall back to DM.
                    user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                    if user:
                        await user.send(embed=embed)
            else:
                user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                if user:
                    await user.send(embed=embed)
        except discord.Forbidden:
            error_log(
                f"reminders: cannot deliver #{rid} to user={user_id} "
                f"channel={channel_id} (DMs closed / missing perms)."
            )
        except discord.HTTPException as exc:
            error_log(f"reminders: HTTP error firing #{rid}: {exc!r}")
        finally:
            # Always mark fired — otherwise a permanently-undeliverable
            # reminder would re-fire every 30s forever.
            try:
                _mark_fired(self.bot.db, rid)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                error_log(f"reminders: mark_fired #{rid} failed: {exc!r}")
            info_log(f"reminders: fired #{rid} for user {user_id}.")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Reminders(bot))
