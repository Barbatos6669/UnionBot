"""Raffles.

Officers run a quick lottery against the attendees of an LFG event (the
v1 entry source) and the bot picks one winner. The prize can be silver
(auto-credited to the winner's ledger), points (auto-added to all three
windows), or a free-text prize the officer hands out manually.

All raffles + entries + winners are persisted in the ``raffles`` and
``raffle_entries`` tables so the history is auditable.

Officer permission: ``utils.is_officer``.
"""

from __future__ import annotations

import secrets

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, info_embed, is_officer, success_embed


_PRIZE_TYPES = ("silver", "points", "text")


def _fmt(n: int) -> str:
    return f"{n:,}"


def pick_winner(discord_ids: list[str]) -> str | None:
    """Cryptographically-random pick from a list. Pure helper, easy to test."""
    if not discord_ids:
        return None
    return secrets.choice(discord_ids)


def gather_event_attendee_ids(db, event_id: int, include_all_signups: bool) -> list[str]:
    """Return deduped Discord ids of attendees for an LFG event.

    Mirrors ``cogs.loot.perform_event_loot_split`` so the same officers get
    raffle entries that get silver-paid.
    """
    signups = db.fetch_lfg_signups(int(event_id)) or []
    if include_all_signups:
        ids = [str(s["discord_id"]) for s in signups]
    else:
        ids = [
            str(s["discord_id"]) for s in signups
            if int(s.get("attended") or 0) == 1
        ]
    return list(dict.fromkeys(ids))


def _prize_summary(prize_type: str, prize_amount: int, prize_text: str | None) -> str:
    if prize_type == "silver":
        return f"**{_fmt(int(prize_amount))} silver**"
    if prize_type == "points":
        return f"**{_fmt(int(prize_amount))} points**"
    return f"**{prize_text or 'Custom prize'}**"


class Raffle(commands.Cog):
    """Officer-only raffles tied to event attendance."""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    group = app_commands.Group(
        name="raffle",
        description="Officers: run a raffle for event attendees.",
    )

    # ── /raffle create-from-event ───────────────────────────────────────
    @group.command(
        name="create-from-event",
        description="Create a raffle whose entrants are the attendees of an LFG event.",
    )
    @app_commands.describe(
        event_id="LFG event id (the same id /loot split uses).",
        prize_type="silver = auto-credit ledger, points = auto-add, text = announce only.",
        prize_amount="Required for silver/points. Ignored for text.",
        prize_text="Required for text prizes. Optional flavour text for silver/points.",
        include_all_signups="If true, every signup is entered instead of attended-only.",
    )
    @app_commands.choices(prize_type=[
        app_commands.Choice(name="silver", value="silver"),
        app_commands.Choice(name="points", value="points"),
        app_commands.Choice(name="text",   value="text"),
    ])
    async def create_from_event(
        self,
        interaction: discord.Interaction,
        event_id: int,
        prize_type: app_commands.Choice[str],
        prize_amount: int | None = None,
        prize_text: str | None = None,
        include_all_signups: bool = False,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Creating raffles is officers only."),
                ephemeral=True,
            )
            return

        ptype = prize_type.value
        amount = int(prize_amount or 0)
        text = (prize_text or "").strip() or None

        if ptype in ("silver", "points") and amount <= 0:
            await interaction.response.send_message(
                embed=error_embed(
                    "Missing prize amount",
                    f"{ptype.title()} raffles need a positive `prize_amount`.",
                ),
                ephemeral=True,
            )
            return
        if ptype == "text" and not text:
            await interaction.response.send_message(
                embed=error_embed(
                    "Missing prize",
                    "Text raffles need a `prize_text` describing the prize.",
                ),
                ephemeral=True,
            )
            return

        db = self.bot.db
        ev = db.fetch_lfg_event(int(event_id))
        if not ev:
            await interaction.response.send_message(
                embed=error_embed("Unknown event", f"No LFG event with id `{event_id}`."),
                ephemeral=True,
            )
            return

        attendee_ids = gather_event_attendee_ids(db, int(event_id), include_all_signups)
        if not attendee_ids:
            hint = (
                "Use `/lfg mark-attended` first, or re-run with "
                "`include_all_signups: true`."
            )
            await interaction.response.send_message(
                embed=error_embed("No entrants", "Nobody is marked **attended** yet.", hint=hint),
                ephemeral=True,
            )
            return

        raffle_id = db.create_raffle(
            guild_id=str(interaction.guild_id) if interaction.guild_id else None,
            creator_id=str(interaction.user.id),
            source_type="event",
            source_ref=str(event_id),
            prize_type=ptype,
            prize_amount=amount,
            prize_text=text,
        )
        if not raffle_id:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Could not create raffle. See logs."),
                ephemeral=True,
            )
            return

        inserted = db.add_raffle_entries(raffle_id, attendee_ids)

        event_label = ev.get("title") or ev.get("name") or f"Event #{event_id}"
        embed = info_embed(
            f"🎟️ Raffle #{raffle_id} — {event_label}",
            (
                f"**Prize:** {_prize_summary(ptype, amount, text)}\n"
                f"**Entries:** {inserted}\n"
                f"**Source:** event attendees "
                f"({'all signups' if include_all_signups else 'attended only'})\n\n"
                f"Use `/raffle draw raffle_id:{raffle_id}` when ready."
            ),
        )
        await interaction.response.send_message(embed=embed)
        try:
            msg = await interaction.original_response()
            db.set_raffle_message(raffle_id, str(msg.channel.id), str(msg.id))
        except (discord.HTTPException, AttributeError):
            pass
        info_log(
            f"raffle.create: id={raffle_id} event={event_id} prize={ptype}:{amount} "
            f"entries={inserted} creator={interaction.user.id}"
        )

    # ── /raffle draw ─────────────────────────────────────────────────────
    @group.command(name="draw", description="Pick a winner for a raffle.")
    @app_commands.describe(raffle_id="ID returned by /raffle create-from-event.")
    async def draw(self, interaction: discord.Interaction, raffle_id: int) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Drawing raffles is officers only."),
                ephemeral=True,
            )
            return

        db = self.bot.db
        raffle = db.fetch_raffle(int(raffle_id))
        if not raffle:
            await interaction.response.send_message(
                embed=error_embed("Unknown raffle", f"No raffle with id `{raffle_id}`."),
                ephemeral=True,
            )
            return
        if raffle["status"] != "open":
            await interaction.response.send_message(
                embed=error_embed(
                    "Already resolved",
                    f"Raffle #{raffle_id} is **{raffle['status']}** "
                    f"(winner: <@{raffle.get('winner_id') or '?'}>).",
                ),
                ephemeral=True,
            )
            return

        entries = db.fetch_raffle_entries(int(raffle_id))
        ids = [e["discord_id"] for e in entries]
        winner_id = pick_winner(ids)
        if not winner_id:
            await interaction.response.send_message(
                embed=error_embed("Empty raffle", "Nobody is entered. Cancel and recreate."),
                ephemeral=True,
            )
            return

        # Mark drawn first so a concurrent /draw can't double-award.
        if not db.mark_raffle_drawn(int(raffle_id), winner_id):
            await interaction.response.send_message(
                embed=error_embed("Race", "Raffle was already drawn. Refresh and retry."),
                ephemeral=True,
            )
            return

        # Auto-pay where applicable. Failures are reported but don't roll back
        # the draw — officers can settle manually from the audit log.
        payout_note = ""
        prize_type = raffle["prize_type"]
        amount = int(raffle["prize_amount"] or 0)
        if prize_type == "silver" and amount > 0:
            new_bal = db.adjust_silver_balance(
                winner_id, amount,
                reason=f"Raffle #{raffle_id} prize",
                ref_type="raffle",
                ref_id=str(raffle_id),
                actor_id=str(interaction.user.id),
            )
            if new_bal is None:
                payout_note = (
                    "\n⚠️ Winner has no profile row — silver **not** credited. "
                    "Have them register, then settle manually."
                )
            else:
                payout_note = f"\n✅ Credited **{_fmt(amount)}** silver (new balance: {_fmt(new_bal)})."
        elif prize_type == "points" and amount > 0:
            db.add_points(winner_id, amount)
            payout_note = f"\n✅ Added **{_fmt(amount)}** points to all windows."
        elif prize_type == "text":
            payout_note = "\nℹ️ Hand out the prize manually."

        embed = success_embed(
            f"🎉 Raffle #{raffle_id} — Winner!",
            (
                f"**Winner:** <@{winner_id}>\n"
                f"**Prize:** {_prize_summary(prize_type, amount, raffle.get('prize_text'))}\n"
                f"**Entrants:** {len(ids)}"
                f"{payout_note}"
            ),
        )
        await interaction.response.send_message(embed=embed)
        info_log(
            f"raffle.draw: id={raffle_id} winner={winner_id} "
            f"prize={prize_type}:{amount} entrants={len(ids)} "
            f"actor={interaction.user.id}"
        )

    # ── /raffle list ────────────────────────────────────────────────────
    @group.command(name="list", description="List open raffles.")
    async def list_raffles(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Raffle admin is officers only."),
                ephemeral=True,
            )
            return
        raffles = self.bot.db.fetch_open_raffles()
        if not raffles:
            await interaction.response.send_message(
                embed=info_embed("No open raffles", "Create one with `/raffle create-from-event`."),
                ephemeral=True,
            )
            return
        lines = []
        for r in raffles[:25]:
            lines.append(
                f"• **#{r['id']}** — {_prize_summary(r['prize_type'], int(r['prize_amount'] or 0), r.get('prize_text'))}"
                f" · src `{r['source_type']}:{r.get('source_ref') or '-'}`"
                f" · by <@{r['creator_id']}>"
            )
        await interaction.response.send_message(
            embed=info_embed("Open raffles", "\n".join(lines)),
            ephemeral=True,
        )

    # ── /raffle show ────────────────────────────────────────────────────
    @group.command(name="show", description="Show a raffle's entries and status.")
    @app_commands.describe(raffle_id="ID of the raffle to inspect.")
    async def show(self, interaction: discord.Interaction, raffle_id: int) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Raffle admin is officers only."),
                ephemeral=True,
            )
            return
        db = self.bot.db
        raffle = db.fetch_raffle(int(raffle_id))
        if not raffle:
            await interaction.response.send_message(
                embed=error_embed("Unknown raffle", f"No raffle with id `{raffle_id}`."),
                ephemeral=True,
            )
            return
        entries = db.fetch_raffle_entries(int(raffle_id))
        preview = ", ".join(f"<@{e['discord_id']}>" for e in entries[:20])
        if len(entries) > 20:
            preview += f" … (+{len(entries) - 20} more)"
        winner_line = (
            f"\n**Winner:** <@{raffle['winner_id']}>" if raffle.get("winner_id") else ""
        )
        body = (
            f"**Status:** {raffle['status']}\n"
            f"**Prize:** {_prize_summary(raffle['prize_type'], int(raffle['prize_amount'] or 0), raffle.get('prize_text'))}\n"
            f"**Source:** {raffle['source_type']}:{raffle.get('source_ref') or '-'}\n"
            f"**Created by:** <@{raffle['creator_id']}>\n"
            f"**Entries ({len(entries)}):** {preview or '_none_'}"
            f"{winner_line}"
        )
        await interaction.response.send_message(
            embed=info_embed(f"Raffle #{raffle_id}", body),
            ephemeral=True,
        )

    # ── /raffle cancel ──────────────────────────────────────────────────
    @group.command(name="cancel", description="Cancel an open raffle (does not refund).")
    @app_commands.describe(raffle_id="ID of the raffle to cancel.")
    async def cancel(self, interaction: discord.Interaction, raffle_id: int) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Raffle admin is officers only."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.cancel_raffle(int(raffle_id))
        if not ok:
            await interaction.response.send_message(
                embed=error_embed(
                    "Could not cancel",
                    f"Raffle `{raffle_id}` is not open (already drawn or doesn't exist).",
                ),
                ephemeral=True,
            )
            return
        info_log(f"raffle.cancel: id={raffle_id} actor={interaction.user.id}")
        await interaction.response.send_message(
            embed=info_embed("Raffle cancelled", f"Raffle #{raffle_id} is closed."),
            ephemeral=True,
        )

    async def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command(self.group.name)
        except (AttributeError, ValueError) as exc:
            error_log(f"raffle cog_unload: {exc}")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Raffle(bot))
