"""Per-rank duty checklists.

Each staff rank has a set of standing duties (recurring or one-time). Holders
of a rank can mark duties done for the current period (day/week). Officers
and admins can add/remove duties on the fly.

Slash commands:
    /duty board [rank]      — view a rank's checklist with current-period status
    /duty done <id> [note]  — log that you handled a duty for this period
    /duty add               — add a new duty to a rank
    /duty remove <id>       — remove a duty
    /duty mine              — recent duties YOU've completed
    /duty seed-defaults     — bulk-load the starter duty set (admin only)
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime
import discord
from discord import app_commands
from discord.ext import commands

from config import STAFF_ROLES
from debug import info_log
from utils import error_embed, info_embed, success_embed, warning_embed
from time_utils import utc_now_naive


_CADENCE_CHOICES = [
    app_commands.Choice(name="🔁 Daily",  value="daily"),
    app_commands.Choice(name="📅 Weekly", value="weekly"),
    app_commands.Choice(name="📌 Once",   value="once"),
]
_CADENCE_EMOJI = {"daily": "🔁", "weekly": "📅", "once": "📌"}
_RANK_CHOICES = [app_commands.Choice(name=r, value=r) for r in STAFF_ROLES]


# ── Default starter duties seeded on first run / via /duty seed-defaults ────
# Format: rank → [(title, description, cadence), ...]
_DEFAULT_DUTIES: dict[str, list[tuple[str, str, str]]] = {
    "Captain": [
        ("Department check-in",   "Meet with your officers; review department progress and blockers.", "weekly"),
        ("Commander report",      "Send a written summary of the week's wins/issues to the Commander.", "weekly"),
        ("Conflict resolution",   "Address any pending member disputes or rule violations escalated to you.", "weekly"),
        ("Mentor an officer",     "Spend time training/guiding a junior leader.",                       "weekly"),
    ],
    "Officer": [
        ("Run a guild event",     "Lead at least one event (PvP, PvE, gather, social) this week.",     "weekly"),
        ("Welcome new members",   "DM or greet anyone who joined since your last shift.",               "daily"),
        ("Officer chat sweep",    "Read and respond to anything new in officer channels.",             "daily"),
        ("Member roster audit",   "Spot-check 5 members for activity / role correctness.",             "weekly"),
        ("Enforce a rule",        "Address one rule violation or note one warning if applicable.",      "weekly"),
    ],
    "Steward": [
        ("Treasury reconcile",    "Verify guild bank balance against ledger; note any discrepancies.", "weekly"),
        ("Logistics restock",     "Check consumables/regear stockpile and request top-ups.",           "weekly"),
        ("Economy report",        "Post a brief market/silver-flow summary to officers.",              "weekly"),
        ("Tax review",            "Confirm tax rate is appropriate for current member count.",         "weekly"),
    ],
    "Holdmaster": [
        ("Laborer rotation",      "Feed and rotate every laborer on your assigned island.",            "daily"),
        ("Island walk-through",   "Inspect your island for damaged/expiring buildings and broken loops.","weekly"),
        ("Build progress update", "Post a short progress note (what was built, what's next, what's needed) in officer chat.","weekly"),
        ("Material request",      "Submit any silver/material requests to Officers before placing major builds.","weekly"),
        ("Cross-island sync",     "Check in with the other Holdmasters so your island complements theirs.","weekly"),
    ],
    "Logistician": [
        ("Stockpile audit",       "Count guild bank stock vs the regear / content demand sheet.",      "weekly"),
        ("Issue order list",      "Post this week's gather / refine / craft priorities for the team.",  "weekly"),
        ("Demand forecast",       "Estimate what content the guild will run and what gear will burn.",  "weekly"),
        ("Pipeline check",        "Confirm Gatherer → Refiner → Crafter handoff isn't blocked anywhere.","daily"),
        ("Officer report",        "Tell officers what's short and what's healthy before scheduled content.","weekly"),
    ],
    "Crafter": [
        ("Pick up crafting order","Take at least one open order from the Logistician's list.",          "weekly"),
        ("Use guild focus",       "Use your weekly focus on assigned guild orders, not personal gear.",  "weekly"),
        ("Report finished items", "Post item counts you completed; deposit to guild bank.",             "weekly"),
        ("Flag missing mats",     "Tell the Logistician/Refiners what raw or refined mats are blocking you.","weekly"),
    ],
    "Refiner": [
        ("Refine incoming raws",  "Clear pending raw mats in the guild bank on your cadence.",          "daily"),
        ("Spec-up coordination",  "Respond to any member requests to spec refining with guild rss.",    "weekly"),
        ("Stock organize",        "Sort refined output in the guild bank by tier/enchant.",             "weekly"),
        ("Shortage report",       "Tell the Logistician and Gatherers what raw mats are running low.",  "weekly"),
    ],
    "Gatherer": [
        ("Gather priority list",  "Gather whatever's at the top of the Logistician/Refiner priority list.","daily"),
        ("Donate to guild bank",  "Deposit gathered raws to the guild bank on a regular cadence.",      "weekly"),
        ("Hotspot report",        "Share zones / hotspots / competition info in the gathering channel.","weekly"),
        ("Group gather run",      "Help organize or join at least one group gather run when called.",    "weekly"),
    ],
    "Senior Shotcaller": [
        ("Plan content roster",   "Schedule the week's calls with appropriate content tiers.",         "weekly"),
        ("Train a shotcaller",    "Run one mentoring session or post-content review with a SC.",       "weekly"),
        ("Strat doc update",      "Update one strategy/comp doc based on this week's results.",        "weekly"),
    ],
    "Shotcaller": [
        ("Lead a content run",    "Call at least one organized content event (gank, ZvZ, dungeon).",    "weekly"),
        ("Post-run report",       "Share results, MVPs, and lessons in the SC channel.",                "weekly"),
        ("Comp practice",         "Run drills or VOD review for your usual squad.",                    "weekly"),
    ],
    "Recruiter": [
        ("Screen pending apps",   "Review every guild application currently pending.",                 "daily"),
        ("Welcome new joiners",   "DM each newly-verified member with starter info / channel pointers.","daily"),
        ("Recruitment ad",        "Post the recruitment ad in LFG / partner Discords.",                "weekly"),
        ("Alumni follow-up",      "Reach out to one Alumni to check whether they're returning.",       "weekly"),
        ("Trial interview",       "Voice-interview at least one trial / Probationary member.",         "weekly"),
    ],
}


def _period_key(cadence: str, now: datetime.datetime | None = None) -> str:
    now = now or utc_now_naive()
    if cadence == "daily":
        return f"D-{now.strftime('%Y-%m-%d')}"
    if cadence == "weekly":
        iso_year, iso_week, _ = now.isocalendar()
        return f"W-{iso_year}-{iso_week:02d}"
    return "ONCE"


def _is_admin(member: discord.Member) -> bool:
    return bool(member.guild_permissions.administrator)


def _holds_rank(member: discord.Member, rank_name: str) -> bool:
    return any(r.name == rank_name for r in member.roles)


def _holds_any_staff(member: discord.Member) -> bool:
    if _is_admin(member):
        return True
    names = {r.name for r in member.roles}
    return any(r in names for r in STAFF_ROLES)


def _seed_defaults(db) -> int:
    """Insert any default duty that doesn't already exist. Returns count added."""
    added = 0
    for rank, duties in _DEFAULT_DUTIES.items():
        for order, (title, desc, cadence) in enumerate(duties):
            row_id = db.add_duty(rank, title, desc, cadence, display_order=order)
            if row_id:
                added += 1
    return added


def _build_board_embed(db, rank_name: str) -> discord.Embed:
    duties = db.fetch_duties_for_rank(rank_name)
    if not duties:
        return info_embed(
            f"{rank_name} — No duties defined",
            f"No duties are configured for **{rank_name}**. "
            "Add some with `/duty add` or run `/duty seed-defaults` for a starter set.",
        )

    lines: list[str] = []
    for duty in duties:
        cadence = duty["cadence"]
        period = _period_key(cadence)
        completions = db.fetch_completions_for_period(duty["id"], period)
        emoji = _CADENCE_EMOJI.get(cadence, "•")
        if completions:
            who = ", ".join(f"<@{c['completed_by']}>" for c in completions[:5])
            if len(completions) > 5:
                who += f" +{len(completions) - 5}"
            status = f"✅ {who}"
        else:
            status = "⬜ pending"
        title = duty["title"]
        desc = duty.get("description") or ""
        lines.append(f"`#{duty['id']:>3}`  {emoji} **{title}** — {status}")
        if desc:
            lines.append(f"     ↳ {desc}")

    e = discord.Embed(
        title=f"📋 {rank_name} Duty Board",
        description="\n".join(lines)[:4000],
        color=discord.Color.blurple(),
    )
    e.set_footer(text="🔁 daily  •  📅 weekly  •  📌 one-time   |   /duty done <id> to log")
    return e


class DutyGroup(app_commands.Group, name="duty", description="Per-rank duty checklists."):
    def __init__(self, bot: Bot):
        super().__init__()
        self.bot: Bot = bot

    # ── board ─────────────────────────────────────────────────────────────
    @app_commands.command(name="board", description="View the duty board for a staff rank.")
    @app_commands.describe(rank="Which rank's board to show. Leave empty to show your own ranks.")
    @app_commands.choices(rank=_RANK_CHOICES)
    async def board(
        self,
        interaction: discord.Interaction,
        rank: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if rank is not None:
            embed = _build_board_embed(self.bot.db, rank.value)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # No rank specified → show every rank the user holds (or all if admin).
        user_ranks = [r.name for r in interaction.user.roles if r.name in STAFF_ROLES]
        if not user_ranks and _is_admin(interaction.user):
            user_ranks = STAFF_ROLES
        if not user_ranks:
            await interaction.followup.send(
                embed=info_embed(
                    "No staff ranks",
                    "You don't hold any staff ranks. Pick one with the `rank` option to view its board.",
                ),
                ephemeral=True,
            )
            return

        for rank_name in user_ranks:
            await interaction.followup.send(
                embed=_build_board_embed(self.bot.db, rank_name), ephemeral=True
            )

    # ── done ──────────────────────────────────────────────────────────────
    @app_commands.command(name="done", description="Log that you completed a duty for this period.")
    @app_commands.describe(duty_id="Duty ID (see /duty board).", note="Optional note about how it went.")
    async def done(
        self,
        interaction: discord.Interaction,
        duty_id: app_commands.Range[int, 1, 999999],
        note: str | None = None,
    ) -> None:
        duty = self.bot.db.fetch_duty(duty_id)
        if not duty:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Duty #{duty_id} does not exist."), ephemeral=True
            )
            return
        if not (_holds_rank(interaction.user, duty["rank_name"]) or _is_admin(interaction.user)):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not eligible",
                    f"Only **{duty['rank_name']}** holders can complete this duty.",
                ),
                ephemeral=True,
            )
            return

        period = _period_key(duty["cadence"])
        new = self.bot.db.record_duty_completion(duty_id, str(interaction.user.id), period, note)
        if not new:
            await interaction.response.send_message(
                embed=warning_embed(
                    "Already logged",
                    f"You already completed **{duty['title']}** for this period (`{period}`).",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Duty logged",
                f"✅ **{duty['title']}** marked done for `{period}`."
                + (f"\n\nNote: {note}" if note else ""),
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} completed duty #{duty_id} ({duty['title']!r}).")

    # ── add ───────────────────────────────────────────────────────────────
    @app_commands.command(name="add", description="Add a new duty to a rank's checklist.")
    @app_commands.describe(
        rank="Which rank this duty belongs to.",
        title="Short duty title.",
        description="Details / how to do it.",
        cadence="How often it repeats (daily, weekly, or one-time).",
    )
    @app_commands.choices(rank=_RANK_CHOICES, cadence=_CADENCE_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    async def add(
        self,
        interaction: discord.Interaction,
        rank: app_commands.Choice[str],
        title: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 1, 500],
        cadence: app_commands.Choice[str],
    ) -> None:
        if not _holds_any_staff(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Not allowed", "Only staff or admins can add duties."),
                ephemeral=True,
            )
            return
        new_id = self.bot.db.add_duty(rank.value, title.strip(), description.strip(), cadence.value)
        if not new_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Could not add",
                    "A duty with that exact title already exists on this rank, or the database errored.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Duty added",
                f"📋 Duty **#{new_id}** added to **{rank.value}** ({_CADENCE_EMOJI[cadence.value]} {cadence.value}).",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} added duty #{new_id} ({title!r}) to {rank.value}.")

    # ── remove ────────────────────────────────────────────────────────────
    @app_commands.command(name="remove", description="Delete a duty from a rank's checklist (admin only).")
    @app_commands.describe(duty_id="Duty ID to delete.")
    @app_commands.default_permissions(administrator=True)
    async def remove(self, interaction: discord.Interaction,
                     duty_id: app_commands.Range[int, 1, 999999]) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Admin only", "Only admins can delete duties."),
                ephemeral=True,
            )
            return
        duty = self.bot.db.fetch_duty(duty_id)
        if not duty:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Duty #{duty_id} does not exist."), ephemeral=True
            )
            return
        self.bot.db.remove_duty(duty_id)
        await interaction.response.send_message(
            embed=success_embed(
                "Duty removed",
                f"🗑️ Removed duty **#{duty_id}** ({duty['title']}) from **{duty['rank_name']}**.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} removed duty #{duty_id} ({duty['title']!r}).")

    # ── mine ──────────────────────────────────────────────────────────────
    @app_commands.command(name="mine", description="Show your recent duty completions.")
    async def mine(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = self.bot.db.fetch_user_recent_completions(str(interaction.user.id), limit=20)
        if not rows:
            await interaction.followup.send(
                embed=info_embed("No completions yet", "You haven't logged any duties."),
                ephemeral=True,
            )
            return
        lines = [
            f"`{r['completed_at'][:16]}`  {_CADENCE_EMOJI.get(r['cadence'], '•')} "
            f"**{r['title']}** ({r['rank_name']})"
            + (f" — _{r['note']}_" if r.get("note") else "")
            for r in rows
        ]
        e = discord.Embed(
            title="📋 Your recent duty completions",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.followup.send(embed=e, ephemeral=True)

    # ── seed defaults ─────────────────────────────────────────────────────
    @app_commands.command(
        name="seed-defaults",
        description="Bulk-load the starter set of duties for every rank (admin only).",
    )
    @app_commands.default_permissions(administrator=True)
    async def seed_defaults(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Admin only", "Only admins can seed default duties."),
                ephemeral=True,
            )
            return
        added = _seed_defaults(self.bot.db)
        await interaction.response.send_message(
            embed=success_embed(
                "Seeded duties",
                f"Added **{added}** new duties across all ranks. "
                f"Existing duties were left untouched. Use `/duty board` to view.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} seeded {added} default duties.")


class Duties(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.bot.tree.add_command(DutyGroup(bot))
        # Auto-seed on first ever load if the table is empty.
        try:
            existing = bot.db.fetch_all_duties()
            if not existing:
                added = _seed_defaults(bot.db)
                if added:
                    info_log(f"Auto-seeded {added} starter duties on first run.")
        except Exception as exc:  # noqa: BLE001
            info_log(f"Duty auto-seed skipped: {exc}")
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:
        # Reload-safety: remove the manually-added /duty group.
        try:
            self.bot.tree.remove_command("duty")
        except Exception:  # noqa: BLE001
            pass


async def setup(bot: Bot):
    await bot.add_cog(Duties(bot))
