"""Loot-split cog.

Divides a silver pool from a finished event evenly among the attendees,
crediting each member's ``silver_balance`` (positive = guild owes them)
and writing a ``silver_ledger`` row. Officers settle the balance
in-game later; until then `/me` and `/dashboard` show the debt.

Commands:
    /loot split event:<id> total:<silver> [tax_pct=0] [shotcaller_bonus_pct=0]
                            [include_all_signups=false] [shotcaller_id=auto]
    /loot history event:<id>

Math (all integer silver):
    1. ``tax`` is the guild cut: ``total * tax_pct // 100``.
    2. ``payable = total - tax``.
    3. If shotcaller_bonus_pct > 0, the shotcaller pockets
       ``payable * bonus_pct // 100`` off the top.
    4. Remaining silver is split evenly among attendees; the modulo
       (a few silver) goes to the guild bank as rounding.

The whole split runs inside a single SQLite transaction so a crash
doesn't leave half the members credited. ``adjust_silver_balance``
already wraps each member's update + ledger row atomically; we just
keep calling it inside a confirm-then-execute flow.
"""

from __future__ import annotations

import datetime as _dt
import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from debug import error_log, info_log
from utils import confirm_action, error_embed, info_embed, is_officer


_MAX_TAX_PCT = 50
_MAX_BONUS_PCT = 25

# Matches <@123>, <@!123>, or a bare 17–20 digit snowflake.
_ID_RE = re.compile(r"<@!?(\d{17,20})>|\b(\d{17,20})\b")


def _fmt(amount: int) -> str:
    return f"{amount:,}"


def _parse_member_ids(raw: str) -> list[str]:
    """Pull every Discord user id out of a free-form string. Dedupes while
    preserving the order they were typed in (matches officer expectation
    that the first name listed is the shotcaller if no override is given)."""
    if not raw:
        return []
    seen: list[str] = []
    for m in _ID_RE.finditer(raw):
        uid = m.group(1) or m.group(2)
        if uid and uid not in seen:
            seen.append(uid)
    return seen


def compute_loot_split(
    total: int,
    tax_pct: int,
    shotcaller_bonus_pct: int,
    n_attendees: int,
    *,
    has_shotcaller: bool,
) -> dict:
    """Pure integer-silver math for ``/loot split``.

    Returns a dict with ``tax``, ``payable``, ``sc_bonus``, ``per_head``
    and ``rounding`` (silver that drops to the bank). Extracted as a
    pure function so the math is unit-testable without a DB.
    """
    total_silver = max(0, int(total))
    tax_pct = max(0, int(tax_pct))
    bonus_pct = max(0, int(shotcaller_bonus_pct))
    n = max(0, int(n_attendees))

    tax = (total_silver * tax_pct) // 100
    payable = total_silver - tax
    sc_bonus = (payable * bonus_pct) // 100 if has_shotcaller and bonus_pct > 0 else 0
    remainder_pool = payable - sc_bonus
    per_head = remainder_pool // n if n else 0
    rounding = remainder_pool - (per_head * n)
    return {
        "tax": tax,
        "payable": payable,
        "sc_bonus": sc_bonus,
        "per_head": per_head,
        "rounding": rounding,
    }


def perform_event_loot_split(
    bot: Bot,
    event_id: int,
    total: int,
    tax_pct: int,
    shotcaller_bonus_pct: int,
    actor_id: str,
    *,
    include_all_signups: bool = False,
    shotcaller_id_override: str | None = None,
) -> tuple[discord.Embed | None, str | None]:
    """Execute a loot split for an LFG event without going through the
    interactive ``/loot split`` confirm flow. Returns ``(embed, error)``:

    * On success ``embed`` is the public receipt and ``error`` is None.
    * On a recoverable validation failure (no signups, nobody to pay,
      nothing left to distribute) ``embed`` is None and ``error`` is a
      short user-facing message.

    Mirrors the math + ledger behaviour of the ``/loot split`` command so
    button-driven splits feed the same silver_ledger ref_type='loot_split'
    history view (``/loot history``).
    """
    db = bot.db
    ev = db.fetch_lfg_event(int(event_id))
    if not ev:
        return None, f"No LFG event with id `{event_id}`."

    signups = db.fetch_lfg_signups(int(event_id)) or []
    if not signups:
        return None, "That event has nobody signed up."
    if include_all_signups:
        attendee_ids = [str(s["discord_id"]) for s in signups]
    else:
        attendee_ids = [
            str(s["discord_id"]) for s in signups
            if int(s.get("attended") or 0) == 1
        ]
    attendee_ids = list(dict.fromkeys(attendee_ids))
    if not attendee_ids:
        return None, (
            "Nobody is marked **attended** yet. Use `/lfg mark-attended` first, "
            "or choose *Include all signups*."
        )

    total_silver = int(total)
    sc_id: str | None = shotcaller_id_override or ev.get("shotcaller_id") or ev.get("creator_id")
    if sc_id is not None:
        sc_id = str(sc_id)
    math = compute_loot_split(
        total_silver, int(tax_pct), int(shotcaller_bonus_pct),
        len(attendee_ids), has_shotcaller=bool(sc_id),
    )
    tax = math["tax"]
    sc_bonus = math["sc_bonus"]
    per_head = math["per_head"]
    rounding = math["rounding"]
    n = len(attendee_ids)

    if per_head <= 0 and sc_bonus <= 0:
        return None, "After tax and bonus, nothing was left to distribute."

    event_label = ev.get("title") or ev.get("name") or f"event #{event_id}"
    ref_type = "loot_split"
    ref_id = str(event_id)
    reason_base = f"Loot split — {event_label}"
    credited: list[str] = []
    failed: list[str] = []

    # Shotcaller bonus first so a partial failure can't leave them with
    # just the per-head share.
    if sc_id and sc_bonus > 0:
        new_bal = db.adjust_silver_balance(
            sc_id, int(sc_bonus),
            reason=f"{reason_base} (shotcaller bonus)",
            ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
        )
        if new_bal is None:
            failed.append(f"<@{sc_id}> (shotcaller bonus — no profile)")
        else:
            credited.append(f"<@{sc_id}> **+{_fmt(sc_bonus)}** _(bonus)_")

    if per_head > 0:
        for did in attendee_ids:
            new_bal = db.adjust_silver_balance(
                did, int(per_head),
                reason=reason_base,
                ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
            )
            if new_bal is None:
                failed.append(f"<@{did}> (no profile — skipped)")
                continue
            line = f"<@{did}> +{_fmt(per_head)}"
            if did == sc_id and sc_bonus > 0:
                credited = [c for c in credited if did not in c]
                line = (
                    f"<@{did}> **+{_fmt(per_head + sc_bonus)}** "
                    "_(bonus included)_"
                )
            credited.append(line)

    embed = discord.Embed(
        title=f"💰 Loot split — {event_label}",
        description=(
            f"**Pool:** {_fmt(total_silver)} • **Tax:** {_fmt(tax)} "
            f"({int(tax_pct)}%) • **Per head:** {_fmt(per_head)} × {n}"
            + (
                f"\n**Shotcaller bonus:** {_fmt(sc_bonus)} "
                f"({int(shotcaller_bonus_pct)}%)" if sc_bonus else ""
            )
            + (f"\n**Rounding to bank:** {_fmt(rounding)}" if rounding else "")
        ),
        color=discord.Color.gold(),
        timestamp=_dt.datetime.utcnow(),
    )
    if credited:
        embed.add_field(
            name=f"Credited ({len(credited)})",
            value="\n".join(credited)[:1024],
            inline=False,
        )
    if failed:
        embed.add_field(
            name=f"⚠️ Skipped ({len(failed)})",
            value="\n".join(failed)[:1024],
            inline=False,
        )
    embed.set_footer(text=f"By <@{actor_id}> • event #{event_id}")
    info_log(
        f"button loot split actor={actor_id} event={event_id} "
        f"total={total_silver} tax={tax} per_head={per_head} attendees={n} "
        f"sc_bonus={sc_bonus} failed={len(failed)}."
    )
    return embed, None
    return seen


class LootCog(commands.Cog):
    """Split silver pools from events and post a receipt."""

    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot

    loot = app_commands.Group(
        name="loot",
        description="Split silver pools across event attendees.",
    )

    # ── /loot split ─────────────────────────────────────────────────────────

    @loot.command(
        name="split",
        description="Officers: split a silver pool among attendees of an event.",
    )
    @app_commands.describe(
        event="LFG event ID (run `/lfg list` to find it).",
        total="Total silver to split (positive integer).",
        tax_pct="Guild cut off the top, 0–50%. Default 0.",
        shotcaller_bonus_pct="Bonus paid to shotcaller from the post-tax pool, 0–25%. Default 0.",
        include_all_signups="If true, pay everyone who signed up instead of attended-only.",
        shotcaller="Override shotcaller (defaults to event's shotcaller_id).",
    )
    async def split(
        self,
        interaction: discord.Interaction,
        event: app_commands.Range[int, 1, 1_000_000_000],
        total: app_commands.Range[int, 1, 10_000_000_000],
        tax_pct: app_commands.Range[int, 0, _MAX_TAX_PCT] = 0,
        shotcaller_bonus_pct: app_commands.Range[int, 0, _MAX_BONUS_PCT] = 0,
        include_all_signups: bool = False,
        shotcaller: discord.Member | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Splitting silver is officers only."),
                ephemeral=True,
            )
            return

        db = self.bot.db
        ev = db.fetch_lfg_event(int(event))
        if not ev:
            await interaction.response.send_message(
                embed=error_embed("Unknown event", f"No LFG event with id `{event}`."),
                ephemeral=True,
            )
            return

        # Build attendee list: signups marked attended, OR everyone signed up
        # if include_all_signups.
        signups = db.fetch_lfg_signups(int(event)) or []
        if not signups:
            await interaction.response.send_message(
                embed=error_embed("No signups", "That event has nobody signed up."),
                ephemeral=True,
            )
            return
        if include_all_signups:
            attendee_ids = [str(s["discord_id"]) for s in signups]
        else:
            attendee_ids = [
                str(s["discord_id"]) for s in signups if int(s.get("attended") or 0) == 1
            ]
        attendee_ids = list(dict.fromkeys(attendee_ids))  # dedupe, preserve order
        if not attendee_ids:
            await interaction.response.send_message(
                embed=error_embed(
                    "Nobody to pay",
                    "Mark attendance with `/lfg mark-attended` first, or pass "
                    "`include_all_signups: true` to pay every signup.",
                ),
                ephemeral=True,
            )
            return

        total_silver = int(total)
        tax = (total_silver * int(tax_pct)) // 100
        payable = total_silver - tax
        sc_id: str | None = None
        if shotcaller is not None:
            sc_id = str(shotcaller.id)
        elif ev.get("shotcaller_id"):
            sc_id = str(ev["shotcaller_id"])
        elif ev.get("creator_id"):
            sc_id = str(ev["creator_id"])
        sc_bonus = 0
        if sc_id and int(shotcaller_bonus_pct) > 0:
            sc_bonus = (payable * int(shotcaller_bonus_pct)) // 100
        remainder_pool = payable - sc_bonus
        n = len(attendee_ids)
        per_head = remainder_pool // n if n else 0
        rounding = remainder_pool - (per_head * n)

        event_label = ev.get("title") or ev.get("name") or f"event #{event}"

        # Confirm before mutating.
        sc_line = (
            f"\n• Shotcaller bonus: <@{sc_id}> +**{_fmt(sc_bonus)}** "
            f"({int(shotcaller_bonus_pct)}%)" if sc_bonus and sc_id else ""
        )
        bank_line = (
            f"\n• Guild bank: **{_fmt(tax + rounding)}** "
            f"(tax {_fmt(tax)} + rounding {_fmt(rounding)})"
            if (tax or rounding) else ""
        )
        body = (
            f"**Event:** {event_label} (id `{event}`)\n"
            f"**Pool:** {_fmt(total_silver)} silver\n"
            f"**Attendees:** {n}\n"
            f"**Per head:** **{_fmt(per_head)}** silver"
            f"{sc_line}{bank_line}\n\n"
            "Confirm to credit each member's silver_balance now."
        )
        ok = await confirm_action(
            interaction,
            title=f"Confirm split — {_fmt(total_silver)} silver",
            description=body,
            confirm_label="Pay out",
            cancel_label="Cancel",
            danger=False,
        )
        if not ok:
            return

        if per_head <= 0 and sc_bonus <= 0:
            await interaction.followup.send(
                embed=info_embed(
                    "Nothing to pay",
                    "After tax and bonus, nothing was left to distribute.",
                ),
                ephemeral=True,
            )
            return

        actor_id = str(interaction.user.id)
        ref_type = "loot_split"
        ref_id = str(event)
        reason_base = f"Loot split — {event_label}"
        credited: list[str] = []
        failed: list[str] = []

        # Pay the shotcaller bonus FIRST so any failures later don't strand
        # them with the per-head share but no bonus.
        if sc_id and sc_bonus > 0:
            new_bal = db.adjust_silver_balance(
                sc_id, int(sc_bonus),
                reason=f"{reason_base} (shotcaller bonus)",
                ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
            )
            if new_bal is None:
                failed.append(f"<@{sc_id}> (shotcaller bonus — no profile)")
            else:
                credited.append(f"<@{sc_id}> **+{_fmt(sc_bonus)}** _(bonus)_")

        if per_head > 0:
            for did in attendee_ids:
                new_bal = db.adjust_silver_balance(
                    did, int(per_head),
                    reason=reason_base,
                    ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
                )
                if new_bal is None:
                    failed.append(f"<@{did}> (no profile — skipped)")
                else:
                    line = f"<@{did}> +{_fmt(per_head)}"
                    if did == sc_id and sc_bonus > 0:
                        # Combine totals for the shotcaller in the receipt
                        credited = [c for c in credited if did not in c]
                        line = f"<@{did}> **+{_fmt(per_head + sc_bonus)}** _(bonus included)_"
                    credited.append(line)

        embed = discord.Embed(
            title=f"💰 Loot split — {event_label}",
            description=(
                f"**Pool:** {_fmt(total_silver)} • **Tax:** {_fmt(tax)} "
                f"({int(tax_pct)}%) • **Per head:** {_fmt(per_head)} "
                f"× {n}"
                + (
                    f"\n**Shotcaller bonus:** {_fmt(sc_bonus)} ({int(shotcaller_bonus_pct)}%)"
                    if sc_bonus else ""
                )
                + (
                    f"\n**Rounding to bank:** {_fmt(rounding)}" if rounding else ""
                )
            ),
            color=discord.Color.gold(),
            timestamp=_dt.datetime.utcnow(),
        )
        if credited:
            embed.add_field(
                name=f"Credited ({len(credited)})",
                value="\n".join(credited)[:1024],
                inline=False,
            )
        if failed:
            embed.add_field(
                name=f"⚠️ Skipped ({len(failed)})",
                value="\n".join(failed)[:1024],
                inline=False,
            )
        embed.set_footer(text=f"By {interaction.user} • settle in-game later")
        await interaction.followup.send(embed=embed, ephemeral=False)
        info_log(
            f"{interaction.user} ran loot split event={event} total={total_silver} "
            f"tax={tax} per_head={per_head} attendees={n} "
            f"sc_bonus={sc_bonus} failed={len(failed)}."
        )

    # ── /loot quick-split ───────────────────────────────────────────────────

    @loot.command(
        name="quick-split",
        description="Officers: split silver across a free-form roster (no LFG event needed).",
    )
    @app_commands.describe(
        members="Space- or comma-separated @mentions or user IDs of the people on the run.",
        total="Total silver to split (positive integer).",
        label="Short description for the ledger (e.g. 'Avalonian run 2026-05-14').",
        tax_pct="Guild cut off the top, 0–50%. Default 0.",
        leader_bonus_pct="Bonus paid to the leader from the post-tax pool, 0–25%. Default 0.",
        leader="Who gets the leader bonus (default: first member in the list).",
    )
    async def quick_split(
        self,
        interaction: discord.Interaction,
        members: str,
        total: app_commands.Range[int, 1, 10_000_000_000],
        label: app_commands.Range[str, 1, 80],
        tax_pct: app_commands.Range[int, 0, _MAX_TAX_PCT] = 0,
        leader_bonus_pct: app_commands.Range[int, 0, _MAX_BONUS_PCT] = 0,
        leader: discord.Member | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Splitting silver is officers only."),
                ephemeral=True,
            )
            return

        attendee_ids = _parse_member_ids(members)
        if not attendee_ids:
            await interaction.response.send_message(
                embed=error_embed(
                    "No members parsed",
                    "Couldn't find any @mentions or user IDs in `members`.\n"
                    "Tip: paste the names from voice with @ — e.g. "
                    "`@Alice @Bob @Carol`.",
                ),
                ephemeral=True,
            )
            return

        total_silver = int(total)
        tax = (total_silver * int(tax_pct)) // 100
        payable = total_silver - tax
        leader_id: str | None = None
        if leader is not None:
            leader_id = str(leader.id)
        elif attendee_ids:
            leader_id = attendee_ids[0]
        bonus = 0
        if leader_id and int(leader_bonus_pct) > 0:
            bonus = (payable * int(leader_bonus_pct)) // 100
        remainder_pool = payable - bonus
        n = len(attendee_ids)
        per_head = remainder_pool // n if n else 0
        rounding = remainder_pool - (per_head * n)

        bonus_line = (
            f"\n• Leader bonus: <@{leader_id}> +**{_fmt(bonus)}** "
            f"({int(leader_bonus_pct)}%)" if bonus and leader_id else ""
        )
        bank_line = (
            f"\n• Guild bank: **{_fmt(tax + rounding)}** "
            f"(tax {_fmt(tax)} + rounding {_fmt(rounding)})"
            if (tax or rounding) else ""
        )
        roster_preview = ", ".join(f"<@{a}>" for a in attendee_ids[:15])
        if len(attendee_ids) > 15:
            roster_preview += f", _…+{len(attendee_ids) - 15} more_"
        body = (
            f"**Label:** {label}\n"
            f"**Pool:** {_fmt(total_silver)} silver\n"
            f"**Roster ({n}):** {roster_preview}\n"
            f"**Per head:** **{_fmt(per_head)}** silver"
            f"{bonus_line}{bank_line}\n\n"
            "Confirm to credit each member's silver_balance now."
        )
        ok = await confirm_action(
            interaction,
            title=f"Confirm quick-split — {_fmt(total_silver)} silver",
            description=body,
            confirm_label="Pay out",
            cancel_label="Cancel",
            danger=False,
        )
        if not ok:
            return

        if per_head <= 0 and bonus <= 0:
            await interaction.followup.send(
                embed=info_embed(
                    "Nothing to pay",
                    "After tax and bonus, nothing was left to distribute.",
                ),
                ephemeral=True,
            )
            return

        actor_id = str(interaction.user.id)
        ref_type = "loot_quick_split"
        # Use the current UTC timestamp as a stable ref_id so /loot history
        # has something to query against (no LFG event id available here).
        ref_id = _dt.datetime.utcnow().strftime("adhoc-%Y%m%d-%H%M%S")
        reason_base = f"Loot split — {label}"
        db = self.bot.db
        credited: list[str] = []
        failed: list[str] = []

        if leader_id and bonus > 0:
            new_bal = db.adjust_silver_balance(
                leader_id, int(bonus),
                reason=f"{reason_base} (leader bonus)",
                ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
            )
            if new_bal is None:
                failed.append(f"<@{leader_id}> (leader bonus — no profile)")
            else:
                credited.append(f"<@{leader_id}> **+{_fmt(bonus)}** _(bonus)_")

        if per_head > 0:
            for did in attendee_ids:
                new_bal = db.adjust_silver_balance(
                    did, int(per_head),
                    reason=reason_base,
                    ref_type=ref_type, ref_id=ref_id, actor_id=actor_id,
                )
                if new_bal is None:
                    failed.append(f"<@{did}> (no profile — skipped)")
                else:
                    line = f"<@{did}> +{_fmt(per_head)}"
                    if did == leader_id and bonus > 0:
                        credited = [c for c in credited if did not in c]
                        line = f"<@{did}> **+{_fmt(per_head + bonus)}** _(bonus included)_"
                    credited.append(line)

        embed = discord.Embed(
            title=f"💰 Loot split — {label}",
            description=(
                f"**Pool:** {_fmt(total_silver)} • **Tax:** {_fmt(tax)} "
                f"({int(tax_pct)}%) • **Per head:** {_fmt(per_head)} × {n}"
                + (
                    f"\n**Leader bonus:** {_fmt(bonus)} ({int(leader_bonus_pct)}%)"
                    if bonus else ""
                )
                + (f"\n**Rounding to bank:** {_fmt(rounding)}" if rounding else "")
            ),
            color=discord.Color.gold(),
            timestamp=_dt.datetime.utcnow(),
        )
        if credited:
            embed.add_field(
                name=f"Credited ({len(credited)})",
                value="\n".join(credited)[:1024],
                inline=False,
            )
        if failed:
            embed.add_field(
                name=f"⚠️ Skipped ({len(failed)})",
                value="\n".join(failed)[:1024],
                inline=False,
            )
        embed.set_footer(text=f"By {interaction.user} • ref {ref_id}")
        await interaction.followup.send(embed=embed, ephemeral=False)
        info_log(
            f"{interaction.user} ran loot quick-split label={label!r} ref={ref_id} "
            f"total={total_silver} tax={tax} per_head={per_head} attendees={n} "
            f"bonus={bonus} failed={len(failed)}."
        )

    # ── /loot history ───────────────────────────────────────────────────────

    @loot.command(
        name="history",
        description="Show every loot-split credit recorded for an event.",
    )
    @app_commands.describe(event="LFG event ID.")
    async def history(
        self,
        interaction: discord.Interaction,
        event: app_commands.Range[int, 1, 1_000_000_000],
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This view is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT discord_id, delta, reason, actor_id, created_at "
                "FROM silver_ledger "
                "WHERE ref_type = 'loot_split' AND ref_id = ? "
                "ORDER BY created_at ASC",
                (str(event),),
            )
            rows = [dict(r) for r in db.cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            error_log(f"/loot history failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Query failed", repr(exc)),
                ephemeral=True,
            )
            return
        if not rows:
            await interaction.followup.send(
                embed=info_embed(
                    "No splits",
                    f"No loot splits recorded for event `{event}`.",
                ),
                ephemeral=True,
            )
            return
        total = sum(int(r["delta"] or 0) for r in rows)
        lines = [
            f"`{(r['created_at'] or '')[:16]}` <@{r['discord_id']}> "
            f"**+{_fmt(int(r['delta'] or 0))}** — {r.get('reason') or ''}"
            for r in rows[:25]
        ]
        if len(rows) > 25:
            lines.append(f"_…and {len(rows) - 25} more entries._")
        await interaction.followup.send(
            embed=info_embed(
                f"Loot history — event {event} ({len(rows)} entries, {_fmt(total)} total)",
                "\n".join(lines)[:4000],
            ),
            ephemeral=True,
        )

    # ── /loot recent ────────────────────────────────────────────────────────

    @loot.command(
        name="recent",
        description="Show recent ad-hoc loot splits (no LFG event needed).",
    )
    @app_commands.describe(days="Look-back window in days (default 14, max 90).")
    async def recent(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 14,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This view is for officers."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT ref_id, reason, COUNT(*) AS members, SUM(delta) AS total, "
                "       MIN(created_at) AS at "
                "FROM silver_ledger "
                "WHERE ref_type = 'loot_quick_split' "
                "  AND julianday('now') - julianday(created_at) <= ? "
                "GROUP BY ref_id "
                "ORDER BY at DESC LIMIT 25",
                (int(days),),
            )
            rows = [dict(r) for r in db.cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            error_log(f"/loot recent failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Query failed", repr(exc)),
                ephemeral=True,
            )
            return
        if not rows:
            await interaction.followup.send(
                embed=info_embed(
                    "No ad-hoc splits",
                    f"No quick-splits recorded in the last {days} day(s).",
                ),
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            reason = (r.get("reason") or "").removeprefix("Loot split — ").rsplit(" (leader bonus)", 1)[0]
            lines.append(
                f"`{(r['at'] or '')[:16]}` **{_fmt(int(r['total'] or 0))}** "
                f"silver • {int(r['members'] or 0)} payouts — {reason} "
                f"(`{r['ref_id']}`)"
            )
        await interaction.followup.send(
            embed=info_embed(
                f"Recent quick-splits ({len(rows)})",
                "\n".join(lines)[:4000],
            ),
            ephemeral=True,
        )

    # ── autocomplete for /loot quick-split members ──────────────────────────

    @quick_split.autocomplete("members")
    async def _members_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Suggest guild members based on the trailing token of `current`.

        The picked Choice replaces the trailing token with `<@id>`, so the
        officer can keep appending names one at a time and the field stays
        a valid roster string.
        """
        guild = interaction.guild
        if guild is None:
            return []
        # Split off the trailing search token; everything before it is the
        # confirmed roster prefix we'll preserve in the suggestion's value.
        text = current or ""
        # Last whitespace or comma marks the boundary between confirmed
        # tokens and the live search token.
        idx = max(text.rfind(" "), text.rfind(","), text.rfind("\n"))
        prefix = text[: idx + 1] if idx >= 0 else ""
        needle = text[idx + 1:].lstrip("@<").lower()

        # Skip suggestion entirely if the last token already looks resolved
        # (mention or raw id) — officer is mid-typing the next one.
        if needle and (needle.isdigit() or needle.startswith("!")):
            return []

        already = set(_parse_member_ids(text))
        # Build a pool of candidate members. Cap the work cheap on big guilds.
        seen: list[tuple[str, str]] = []
        scanned = 0
        for m in guild.members:
            scanned += 1
            if scanned > 2000:
                break
            if m.bot or str(m.id) in already:
                continue
            name = m.display_name
            uname = m.name
            if needle and needle not in name.lower() and needle not in uname.lower():
                continue
            seen.append((name, str(m.id)))
            if len(seen) >= 25:
                break

        choices: list[app_commands.Choice[str]] = []
        for name, mid in seen:
            mention = f"<@{mid}> "
            new_value = (prefix + mention).strip()
            if len(new_value) > 100:
                # Discord caps autocomplete value at 100 chars.
                continue
            label = f"{name} ({mid})"[:100]
            choices.append(app_commands.Choice(name=label, value=new_value))
        return choices


async def setup(bot: Bot) -> None:
    await bot.add_cog(LootCog(bot))
    info_log("Initialized Loot cog.")
