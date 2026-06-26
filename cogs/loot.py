"""Loot-split cog.

Divides a silver pool from a finished event evenly among the attendees,
crediting each member's ``silver_balance`` (positive = guild owes them)
and writing a ``silver_ledger`` row. Officers settle the balance
in-game later; until then `/me` and `/dashboard` show the debt.

Commands:
    /loot split event:<id> total:<silver> [silver_total=0]
                            [silver_opt_out=@members] [tax_pct=0]
                            [shotcaller_bonus_pct=0]
                            [include_all_signups=false] [shotcaller_id=auto]
    /loot quick-split members:<mentions> total:<silver> [silver_total=0]
    /loot history event:<id>

Math (all integer silver):
    1. ``total`` is the tradable/sellable loot pool.
    2. ``tax`` is the guild cut: ``total * tax_pct // 100``.
    3. ``payable = total - tax``.
    4. If shotcaller_bonus_pct > 0, the shotcaller pockets
       ``payable * bonus_pct // 100`` off the top.
    5. Remaining silver is split evenly among attendees; the modulo
       (a few silver) goes to the guild bank as rounding.
    6. ``silver_total`` is a separate manual pool for untradable silver bags.
       It is split only among members who did not opt out of that pool.

The whole split runs through one SQLite batch transaction so a crash
or missing profile does not leave half the members credited.
"""

from __future__ import annotations

import datetime as _dt
from time_utils import utc_now_naive
import re
from collections import defaultdict

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
_SILVER_TOKEN_RE = re.compile(
    r"\d+(?:[\d,_ ]*\d)?(?:\.\d+)?\s*(?:million|mil|m|thousand|k)?",
    re.IGNORECASE,
)


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


def _parse_silver_amount(raw: str | None) -> int:
    """Parse officer-friendly silver text like ``4.2m`` or ``750k``."""
    text = (
        str(raw or "")
        .strip()
        .lower()
        .replace(",", "")
        .replace("_", "")
        .replace(" ", "")
    )
    if not text:
        return 0
    text = text.replace("silver", "").strip()
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


def _parse_silver_split_field(raw: str | None) -> tuple[int, list[str]]:
    """Parse modal text like ``5m | optout: @A @B``.

    Slash commands expose these as separate fields, but Discord modals are
    capped at five inputs. This keeps the event-post button useful without
    adding another modal step.
    """
    text = str(raw or "").strip()
    if not text:
        return 0, []
    lower = text.lower()
    opt_idx = lower.find("optout:")
    if opt_idx < 0:
        opt_idx = lower.find("opt-out:")
    amount_text = text if opt_idx < 0 else text[:opt_idx]
    opt_text = "" if opt_idx < 0 else text[opt_idx:]
    amount_match = _SILVER_TOKEN_RE.search(amount_text)
    amount = 0
    if amount_match:
        amount = _parse_silver_amount(amount_match.group(0))
    return amount, _parse_member_ids(opt_text)


def compute_loot_split(
    total: int,
    tax_pct: int,
    shotcaller_bonus_pct: int,
    n_attendees: int,
    *,
    has_shotcaller: bool,
    silver_total: int = 0,
    n_silver_attendees: int | None = None,
) -> dict:
    """Pure integer-silver math for ``/loot split``.

    ``total`` is the tradable/sellable loot pool. ``silver_total`` is for
    untradable silver bags or other manual silver values that need a separate
    equal split. Extracted as a pure function so the math is unit-testable
    without a DB.
    """
    total_silver = max(0, int(total))
    silver_pool = max(0, int(silver_total))
    tax_pct = max(0, int(tax_pct))
    bonus_pct = max(0, int(shotcaller_bonus_pct))
    n = max(0, int(n_attendees))
    silver_n = n if n_silver_attendees is None else max(0, int(n_silver_attendees))

    tax = (total_silver * tax_pct) // 100
    payable = total_silver - tax
    sc_bonus = (payable * bonus_pct) // 100 if has_shotcaller and bonus_pct > 0 else 0
    remainder_pool = payable - sc_bonus
    per_head = remainder_pool // n if n else 0
    rounding = remainder_pool - (per_head * n)
    silver_per_head = silver_pool // silver_n if silver_n else 0
    silver_rounding = silver_pool - (silver_per_head * silver_n)
    return {
        "tax": tax,
        "payable": payable,
        "sc_bonus": sc_bonus,
        "per_head": per_head,
        "rounding": rounding,
        "silver_total": silver_pool,
        "silver_per_head": silver_per_head,
        "silver_rounding": silver_rounding,
        "silver_recipients": silver_n,
    }


def _preview_mentions(ids: list[str], *, limit: int = 15) -> str:
    if not ids:
        return "_none_"
    preview = ", ".join(f"<@{did}>" for did in ids[:limit])
    if len(ids) > limit:
        preview += f", _…+{len(ids) - limit} more_"
    return preview


def _credit_loot_split(
    db,
    *,
    normal_recipient_ids: list[str],
    silver_recipient_ids: list[str],
    bonus_recipient_id: str | None,
    math: dict,
    reason_base: str,
    ref_type: str,
    ref_id: str,
    actor_id: str,
    bonus_label: str,
) -> tuple[list[str], list[str]]:
    credited_totals: dict[str, int] = defaultdict(int)
    credited_parts: dict[str, list[str]] = defaultdict(list)
    entries: list[dict] = []
    failed: list[str] = []

    def _queue_credit(discord_id: str, amount: int, reason: str, part_label: str) -> None:
        if amount <= 0:
            return
        entries.append(
            {
                "discord_id": discord_id,
                "delta": int(amount),
                "reason": reason,
                "ref_type": ref_type,
                "ref_id": ref_id,
                "actor_id": actor_id,
                "part_label": part_label,
            }
        )

    sc_bonus = int(math.get("sc_bonus") or 0)
    per_head = int(math.get("per_head") or 0)
    silver_per_head = int(math.get("silver_per_head") or 0)

    if bonus_recipient_id and sc_bonus > 0:
        _queue_credit(
            bonus_recipient_id,
            sc_bonus,
            f"{reason_base} ({bonus_label} bonus)",
            f"{bonus_label} bonus",
        )

    for did in normal_recipient_ids:
        _queue_credit(did, per_head, reason_base, "loot")

    for did in silver_recipient_ids:
        _queue_credit(did, silver_per_head, f"{reason_base} (silver bags)", "silver bags")

    if not entries:
        return [], []

    result = db.adjust_silver_balances_batch(entries)
    if result is None:
        return [], ["Transaction failed; no balances were changed."]

    _balances, missing = result
    if missing:
        parts_by_missing: dict[str, list[str]] = defaultdict(list)
        for entry in entries:
            if entry["discord_id"] in missing:
                parts_by_missing[entry["discord_id"]].append(entry["part_label"])
        failed.extend(
            f"<@{did}> ({', '.join(parts_by_missing.get(did) or ['loot'])} — no profile)"
            for did in missing
        )
        failed.append("Split rolled back; fix missing profiles and run it again.")
        return [], failed

    for entry in entries:
        did = entry["discord_id"]
        credited_totals[did] += int(entry["delta"])
        credited_parts[did].append(entry["part_label"])

    credited = [
        f"<@{did}> **+{_fmt(total)}** _({', '.join(credited_parts[did])})_"
        for did, total in credited_totals.items()
    ]
    return credited, failed


def _build_loot_split_embed(
    *,
    title_label: str,
    total_silver: int,
    tax_pct: int,
    shotcaller_bonus_pct: int,
    math: dict,
    n_normal: int,
    silver_opt_out_ids: list[str],
    credited: list[str],
    failed: list[str],
    actor_text: str,
    ref_text: str,
    bonus_label: str,
) -> discord.Embed:
    tax = int(math.get("tax") or 0)
    sc_bonus = int(math.get("sc_bonus") or 0)
    per_head = int(math.get("per_head") or 0)
    rounding = int(math.get("rounding") or 0)
    silver_total = int(math.get("silver_total") or 0)
    silver_per_head = int(math.get("silver_per_head") or 0)
    silver_rounding = int(math.get("silver_rounding") or 0)
    silver_recipients = int(math.get("silver_recipients") or 0)

    desc_lines = [
        f"**Tradable loot pool:** {_fmt(total_silver)}",
        f"Tax/guild cut: **{_fmt(tax)}** ({int(tax_pct)}%)",
        f"Loot split: **{_fmt(per_head)}** × {n_normal}",
    ]
    if sc_bonus:
        desc_lines.append(f"{bonus_label.title()} bonus: **{_fmt(sc_bonus)}** ({int(shotcaller_bonus_pct)}%)")
    if rounding:
        desc_lines.append(f"Loot rounding to bank: **{_fmt(rounding)}**")
    if silver_total:
        desc_lines.extend(
            [
                "",
                f"**Silver-bag/manual pool:** {_fmt(silver_total)}",
                f"Silver split: **{_fmt(silver_per_head)}** × {silver_recipients}",
            ]
        )
        if silver_rounding:
            desc_lines.append(f"Silver rounding to bank: **{_fmt(silver_rounding)}**")
        if silver_opt_out_ids:
            desc_lines.append(f"Silver opt-outs: {_preview_mentions(silver_opt_out_ids, limit=8)}")

    bank_total = tax + rounding + silver_rounding
    if bank_total:
        desc_lines.append(f"\n**Total left to guild bank/rounding:** {_fmt(bank_total)}")

    embed = discord.Embed(
        title=f"💰 Loot split — {title_label}",
        description="\n".join(desc_lines),
        color=discord.Color.gold(),
        timestamp=utc_now_naive(),
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
    embed.set_footer(text=f"By {actor_text} • {ref_text}")
    return embed


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
    silver_total: int = 0,
    silver_opt_out_ids: list[str] | None = None,
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
    silver_pool = max(0, int(silver_total or 0))
    attendee_set = set(attendee_ids)
    silver_opt_out_ids = [
        did for did in dict.fromkeys(str(did) for did in (silver_opt_out_ids or []) if str(did))
        if did in attendee_set
    ]
    silver_recipient_ids = [did for did in attendee_ids if did not in set(silver_opt_out_ids)]
    sc_id: str | None = shotcaller_id_override or ev.get("shotcaller_id") or ev.get("creator_id")
    if sc_id is not None:
        sc_id = str(sc_id)
    math = compute_loot_split(
        total_silver, int(tax_pct), int(shotcaller_bonus_pct),
        len(attendee_ids), has_shotcaller=bool(sc_id),
        silver_total=silver_pool,
        n_silver_attendees=len(silver_recipient_ids),
    )
    tax = math["tax"]
    sc_bonus = math["sc_bonus"]
    per_head = math["per_head"]
    silver_per_head = math["silver_per_head"]

    if per_head <= 0 and sc_bonus <= 0 and silver_per_head <= 0:
        return None, "After tax and bonus, nothing was left to distribute."

    event_label = ev.get("title") or ev.get("name") or f"event #{event_id}"
    ref_type = "loot_split"
    ref_id = str(event_id)
    reason_base = f"Loot split — {event_label}"
    credited, failed = _credit_loot_split(
        db,
        normal_recipient_ids=attendee_ids,
        silver_recipient_ids=silver_recipient_ids,
        bonus_recipient_id=sc_id,
        math=math,
        reason_base=reason_base,
        ref_type=ref_type,
        ref_id=ref_id,
        actor_id=actor_id,
        bonus_label="shotcaller",
    )
    embed = _build_loot_split_embed(
        title_label=event_label,
        total_silver=total_silver,
        tax_pct=int(tax_pct),
        shotcaller_bonus_pct=int(shotcaller_bonus_pct),
        math=math,
        n_normal=len(attendee_ids),
        silver_opt_out_ids=silver_opt_out_ids,
        credited=credited,
        failed=failed,
        actor_text=f"<@{actor_id}>",
        ref_text=f"event #{event_id}",
        bonus_label="shotcaller",
    )
    info_log(
        f"button loot split actor={actor_id} event={event_id} "
        f"total={total_silver} tax={tax} per_head={per_head} attendees={len(attendee_ids)} "
        f"silver_total={silver_pool} silver_per_head={silver_per_head} "
        f"sc_bonus={sc_bonus} failed={len(failed)}."
    )
    return embed, None


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
        total="Tradable/sellable loot value. Use 0 if only splitting silver bags.",
        tax_pct="Guild cut off the top, 0–50%. Default 0.",
        shotcaller_bonus_pct="Bonus paid to shotcaller from the post-tax pool, 0–25%. Default 0.",
        include_all_signups="If true, pay everyone who signed up instead of attended-only.",
        shotcaller="Override shotcaller (defaults to event's shotcaller_id).",
        silver_total="Untradable silver bags/manual silver value to split separately.",
        silver_opt_out="Members opting out of the silver-bag split (@mentions or IDs).",
    )
    async def split(
        self,
        interaction: discord.Interaction,
        event: app_commands.Range[int, 1, 1_000_000_000],
        total: app_commands.Range[int, 0, 10_000_000_000],
        tax_pct: app_commands.Range[int, 0, _MAX_TAX_PCT] = 0,
        shotcaller_bonus_pct: app_commands.Range[int, 0, _MAX_BONUS_PCT] = 0,
        include_all_signups: bool = False,
        shotcaller: discord.Member | None = None,
        silver_total: app_commands.Range[int, 0, 10_000_000_000] = 0,
        silver_opt_out: str | None = None,
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
        silver_pool = int(silver_total or 0)
        if total_silver <= 0 and silver_pool <= 0:
            await interaction.response.send_message(
                embed=error_embed(
                    "No silver entered",
                    "Enter a tradable loot total, a silver-bag/manual total, or both.",
                ),
                ephemeral=True,
            )
            return
        attendee_set = set(attendee_ids)
        silver_opt_out_ids = [did for did in _parse_member_ids(silver_opt_out or "") if did in attendee_set]
        silver_recipient_ids = [did for did in attendee_ids if did not in set(silver_opt_out_ids)]
        sc_id: str | None = None
        if shotcaller is not None:
            sc_id = str(shotcaller.id)
        elif ev.get("shotcaller_id"):
            sc_id = str(ev["shotcaller_id"])
        elif ev.get("creator_id"):
            sc_id = str(ev["creator_id"])
        math = compute_loot_split(
            total_silver,
            int(tax_pct),
            int(shotcaller_bonus_pct),
            len(attendee_ids),
            has_shotcaller=bool(sc_id),
            silver_total=silver_pool,
            n_silver_attendees=len(silver_recipient_ids),
        )
        tax = int(math["tax"])
        sc_bonus = int(math["sc_bonus"])
        per_head = int(math["per_head"])
        rounding = int(math["rounding"])
        silver_per_head = int(math["silver_per_head"])
        silver_rounding = int(math["silver_rounding"])
        n = len(attendee_ids)

        event_label = ev.get("title") or ev.get("name") or f"event #{event}"

        # Confirm before mutating.
        sc_line = (
            f"\n• Shotcaller bonus: <@{sc_id}> +**{_fmt(sc_bonus)}** "
            f"({int(shotcaller_bonus_pct)}%)" if sc_bonus and sc_id else ""
        )
        bank_line = (
            f"\n• Guild bank/rounding: **{_fmt(tax + rounding + silver_rounding)}** "
            f"(tax {_fmt(tax)} + loot rounding {_fmt(rounding)}"
            f" + silver rounding {_fmt(silver_rounding)})"
            if (tax or rounding or silver_rounding) else ""
        )
        silver_line = (
            f"\n• Silver bags/manual: **{_fmt(silver_pool)}** → "
            f"**{_fmt(silver_per_head)}** × {len(silver_recipient_ids)}"
            + (
                f"\n• Silver opt-outs: {_preview_mentions(silver_opt_out_ids, limit=8)}"
                if silver_opt_out_ids else ""
            )
            if silver_pool else ""
        )
        body = (
            f"**Event:** {event_label} (id `{event}`)\n"
            f"**Tradable loot pool:** {_fmt(total_silver)} silver\n"
            f"**Loot recipients:** {n}\n"
            f"**Loot per head:** **{_fmt(per_head)}** silver"
            f"{silver_line}{sc_line}{bank_line}\n\n"
            "Confirm to credit each member's silver_balance now."
        )
        ok = await confirm_action(
            interaction,
            title=f"Confirm split — {_fmt(total_silver + silver_pool)} silver",
            description=body,
            confirm_label="Pay out",
            cancel_label="Cancel",
            danger=False,
        )
        if not ok:
            return

        if per_head <= 0 and sc_bonus <= 0 and silver_per_head <= 0:
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
        credited, failed = _credit_loot_split(
            db,
            normal_recipient_ids=attendee_ids,
            silver_recipient_ids=silver_recipient_ids,
            bonus_recipient_id=sc_id,
            math=math,
            reason_base=reason_base,
            ref_type=ref_type,
            ref_id=ref_id,
            actor_id=actor_id,
            bonus_label="shotcaller",
        )
        embed = _build_loot_split_embed(
            title_label=event_label,
            total_silver=total_silver,
            tax_pct=int(tax_pct),
            shotcaller_bonus_pct=int(shotcaller_bonus_pct),
            math=math,
            n_normal=n,
            silver_opt_out_ids=silver_opt_out_ids,
            credited=credited,
            failed=failed,
            actor_text=str(interaction.user),
            ref_text="settle in-game later",
            bonus_label="shotcaller",
        )
        await interaction.followup.send(embed=embed, ephemeral=False)
        info_log(
            f"{interaction.user} ran loot split event={event} total={total_silver} "
            f"tax={tax} per_head={per_head} attendees={n} "
            f"silver_total={silver_pool} silver_per_head={silver_per_head} "
            f"silver_opt_outs={len(silver_opt_out_ids)} sc_bonus={sc_bonus} failed={len(failed)}."
        )

    # ── /loot quick-split ───────────────────────────────────────────────────

    @loot.command(
        name="quick-split",
        description="Officers: split silver across a free-form roster (no LFG event needed).",
    )
    @app_commands.describe(
        members="Space- or comma-separated @mentions or user IDs of the people on the run.",
        total="Tradable/sellable loot value. Use 0 if only splitting silver bags.",
        label="Short description for the ledger (e.g. 'Avalonian run 2026-05-14').",
        tax_pct="Guild cut off the top, 0–50%. Default 0.",
        leader_bonus_pct="Bonus paid to the leader from the post-tax pool, 0–25%. Default 0.",
        leader="Who gets the leader bonus (default: first member in the list).",
        silver_total="Untradable silver bags/manual silver value to split separately.",
        silver_opt_out="Members opting out of the silver-bag split (@mentions or IDs).",
    )
    async def quick_split(
        self,
        interaction: discord.Interaction,
        members: str,
        total: app_commands.Range[int, 0, 10_000_000_000],
        label: app_commands.Range[str, 1, 80],
        tax_pct: app_commands.Range[int, 0, _MAX_TAX_PCT] = 0,
        leader_bonus_pct: app_commands.Range[int, 0, _MAX_BONUS_PCT] = 0,
        leader: discord.Member | None = None,
        silver_total: app_commands.Range[int, 0, 10_000_000_000] = 0,
        silver_opt_out: str | None = None,
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
        silver_pool = int(silver_total or 0)
        if total_silver <= 0 and silver_pool <= 0:
            await interaction.response.send_message(
                embed=error_embed(
                    "No silver entered",
                    "Enter a tradable loot total, a silver-bag/manual total, or both.",
                ),
                ephemeral=True,
            )
            return
        attendee_set = set(attendee_ids)
        silver_opt_out_ids = [did for did in _parse_member_ids(silver_opt_out or "") if did in attendee_set]
        silver_recipient_ids = [did for did in attendee_ids if did not in set(silver_opt_out_ids)]
        leader_id: str | None = None
        if leader is not None:
            leader_id = str(leader.id)
        elif attendee_ids:
            leader_id = attendee_ids[0]
        n = len(attendee_ids)
        math = compute_loot_split(
            total_silver,
            int(tax_pct),
            int(leader_bonus_pct),
            n,
            has_shotcaller=bool(leader_id),
            silver_total=silver_pool,
            n_silver_attendees=len(silver_recipient_ids),
        )
        tax = int(math["tax"])
        bonus = int(math["sc_bonus"])
        per_head = int(math["per_head"])
        rounding = int(math["rounding"])
        silver_per_head = int(math["silver_per_head"])
        silver_rounding = int(math["silver_rounding"])

        bonus_line = (
            f"\n• Leader bonus: <@{leader_id}> +**{_fmt(bonus)}** "
            f"({int(leader_bonus_pct)}%)" if bonus and leader_id else ""
        )
        bank_line = (
            f"\n• Guild bank/rounding: **{_fmt(tax + rounding + silver_rounding)}** "
            f"(tax {_fmt(tax)} + loot rounding {_fmt(rounding)}"
            f" + silver rounding {_fmt(silver_rounding)})"
            if (tax or rounding or silver_rounding) else ""
        )
        silver_line = (
            f"\n• Silver bags/manual: **{_fmt(silver_pool)}** → "
            f"**{_fmt(silver_per_head)}** × {len(silver_recipient_ids)}"
            + (
                f"\n• Silver opt-outs: {_preview_mentions(silver_opt_out_ids, limit=8)}"
                if silver_opt_out_ids else ""
            )
            if silver_pool else ""
        )
        body = (
            f"**Label:** {label}\n"
            f"**Tradable loot pool:** {_fmt(total_silver)} silver\n"
            f"**Roster ({n}):** {_preview_mentions(attendee_ids)}\n"
            f"**Loot per head:** **{_fmt(per_head)}** silver"
            f"{silver_line}{bonus_line}{bank_line}\n\n"
            "Confirm to credit each member's silver_balance now."
        )
        ok = await confirm_action(
            interaction,
            title=f"Confirm quick-split — {_fmt(total_silver + silver_pool)} silver",
            description=body,
            confirm_label="Pay out",
            cancel_label="Cancel",
            danger=False,
        )
        if not ok:
            return

        if per_head <= 0 and bonus <= 0 and silver_per_head <= 0:
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
        ref_id = utc_now_naive().strftime("adhoc-%Y%m%d-%H%M%S")
        reason_base = f"Loot split — {label}"
        db = self.bot.db
        credited, failed = _credit_loot_split(
            db,
            normal_recipient_ids=attendee_ids,
            silver_recipient_ids=silver_recipient_ids,
            bonus_recipient_id=leader_id,
            math=math,
            reason_base=reason_base,
            ref_type=ref_type,
            ref_id=ref_id,
            actor_id=actor_id,
            bonus_label="leader",
        )
        embed = _build_loot_split_embed(
            title_label=str(label),
            total_silver=total_silver,
            tax_pct=int(tax_pct),
            shotcaller_bonus_pct=int(leader_bonus_pct),
            math=math,
            n_normal=n,
            silver_opt_out_ids=silver_opt_out_ids,
            credited=credited,
            failed=failed,
            actor_text=str(interaction.user),
            ref_text=f"ref {ref_id}",
            bonus_label="leader",
        )
        await interaction.followup.send(embed=embed, ephemeral=False)
        info_log(
            f"{interaction.user} ran loot quick-split label={label!r} ref={ref_id} "
            f"total={total_silver} tax={tax} per_head={per_head} attendees={n} "
            f"silver_total={silver_pool} silver_per_head={silver_per_head} "
            f"silver_opt_outs={len(silver_opt_out_ids)} bonus={bonus} failed={len(failed)}."
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
