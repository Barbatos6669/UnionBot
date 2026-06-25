"""Recruitment funnel cog.

Tracks prospects from first contact through their first event so officers
can see which recruiters and sources actually convert. Stages:

* ``contacted`` — first whisper / scout note.
* ``discord``   — joined our Discord.
* ``registered``— verified Albion identity via the Register button.
* ``first_event`` — attended their first scheduled event.
* ``retained``  — still around 7+ days after first event.
* ``lost``      — dropped out at any stage.

The Recruiter role uses ``/recruit add`` after a successful whisper.
Officers (or automation) move people through stages with
``/recruit update``. ``/recruit leaderboard`` reveals which recruiter is
actually producing reliable members vs just spamming invites.
"""

from __future__ import annotations

import datetime as _dt

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._typing import Bot
from debug import info_log, error_log, warning_log
from utils import error_embed, info_embed, is_officer, success_embed
from time_utils import utc_now_naive


_STAGES = ["contacted", "discord", "registered",
           "first_event", "retained", "lost"]
_STAGE_LABEL = {
    "contacted":   "1. Contacted",
    "discord":     "2. Joined Discord",
    "registered":  "3. Registered",
    "first_event": "4. First event",
    "retained":    "5. Retained (7d)",
    "lost":        "✖ Lost",
}


def _stage_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=_STAGE_LABEL[s], value=s) for s in _STAGES]


def _fmt_recruit_line(r: dict) -> str:
    rid = int(r["id"])
    name = r.get("albion_name") or "?"
    stage = _STAGE_LABEL.get(r.get("status") or "", r.get("status") or "?")
    src = f" · {r['source']}" if r.get("source") else ""
    recruiter = f" by <@{r['recruiter_id']}>" if r.get("recruiter_id") else ""
    return f"`#{rid}` **{name}** — {stage}{src}{recruiter}"


class RecruitmentCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot

    recruit = app_commands.Group(
        name="recruit",
        description="Recruitment funnel — track prospects from contact to retention.",
    )

    # ── /recruit add ────────────────────────────────────────────────────────
    @recruit.command(name="add", description="Log a new prospect.")
    @app_commands.describe(
        albion_name="In-game Albion name of the prospect.",
        source="Where you found them (Faction WB, Mists, friend, ad, etc).",
        notes="Optional context (build preference, timezone, etc).",
    )
    async def recruit_add(
        self, interaction: discord.Interaction,
        albion_name: str, source: str | None = None,
        notes: str | None = None,
    ) -> None:
        existing = self.bot.db.recruit_find_by_name(albion_name)
        if existing:
            await interaction.response.send_message(
                embed=error_embed(
                    "Already tracked",
                    f"`{albion_name}` is already recruit "
                    f"**#{existing['id']}** (status: "
                    f"`{existing.get('status')}`).",
                ),
                ephemeral=True,
            )
            return
        rid = self.bot.db.recruit_add(
            albion_name=albion_name, source=source,
            recruiter_id=str(interaction.user.id), notes=notes,
        )
        if not rid:
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't log recruit."),
                ephemeral=True,
            )
            return
        info_log(
            f"{interaction.user} added recruit #{rid} ({albion_name}) "
            f"from {source or '—'}"
        )
        await interaction.response.send_message(
            embed=success_embed(
                f"Recruit #{rid} added",
                f"**{albion_name}** logged at stage `contacted`.\n"
                f"Use `/recruit update id:{rid} status:discord` once they "
                f"join the server.",
            ),
            ephemeral=True,
        )

    # ── /recruit update ─────────────────────────────────────────────────────
    @recruit.command(name="update", description="Advance a prospect through the funnel.")
    @app_commands.describe(
        recruit_id="The recruit ID (see /recruit status).",
        status="New funnel stage.",
        notes="Optional notes (append, replaces existing).",
        member="If they have a Discord account now, link it.",
    )
    @app_commands.choices(status=_stage_choices())
    async def recruit_update(
        self, interaction: discord.Interaction,
        recruit_id: int,
        status: app_commands.Choice[str] | None = None,
        notes: str | None = None,
        member: discord.Member | None = None,
    ) -> None:
        row = self.bot.db.recruit_get(int(recruit_id))
        if not row:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"No recruit `#{recruit_id}`."),
                ephemeral=True,
            )
            return
        ok = self.bot.db.recruit_update(
            int(recruit_id),
            status=(status.value if status else None),
            notes=notes,
            discord_id=(str(member.id) if member else None),
        )
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("No changes",
                                  "Provide a status, notes, or member."),
                ephemeral=True,
            )
            return
        new_status = status.value if status else row.get("status")
        info_log(
            f"{interaction.user} updated recruit #{recruit_id} → "
            f"status={new_status}"
        )
        await interaction.response.send_message(
            embed=success_embed(
                f"Recruit #{recruit_id} updated",
                f"Stage: `{new_status}`\n"
                + (f"Discord: {member.mention}\n" if member else "")
                + (f"Notes: {notes}" if notes else ""),
            ),
            ephemeral=True,
        )

    # ── /recruit status ─────────────────────────────────────────────────────
    @recruit.command(
        name="status",
        description="Show current funnel (your recruits, or all if officer).",
    )
    @app_commands.describe(
        stage="Filter by stage.",
        recruiter="Filter by recruiter (officers only — your own otherwise).",
    )
    @app_commands.choices(stage=_stage_choices())
    async def recruit_status(
        self, interaction: discord.Interaction,
        stage: app_commands.Choice[str] | None = None,
        recruiter: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        # Non-officers can only see their own list.
        if not is_officer(interaction.user):
            who = str(interaction.user.id)
        else:
            who = str(recruiter.id) if recruiter else None
        rows = self.bot.db.recruit_list(
            status=(stage.value if stage else None),
            recruiter_id=who,
            limit=50,
        )
        if not rows:
            await interaction.followup.send(
                embed=info_embed(
                    "No recruits",
                    "No prospects match those filters.",
                ),
                ephemeral=True,
            )
            return
        # Group by status for readability.
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r.get("status") or "?", []).append(r)
        embed = discord.Embed(
            title="Recruitment funnel",
            colour=discord.Colour.blurple(),
        )
        embed.description = (
            f"**{len(rows)}** prospect(s)"
            + (f" by <@{who}>" if who else "")
            + (f" at `{stage.value}`" if stage else "")
        )
        for s in _STAGES:
            if s not in groups:
                continue
            chunk = "\n".join(_fmt_recruit_line(r) for r in groups[s][:12])
            extra = ""
            if len(groups[s]) > 12:
                extra = f"\n…and {len(groups[s]) - 12} more."
            embed.add_field(
                name=f"{_STAGE_LABEL[s]} ({len(groups[s])})",
                value=chunk + extra,
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /recruit followup ───────────────────────────────────────────────────
    @recruit.command(
        name="followup",
        description="List stale prospects who need a nudge.",
    )
    @app_commands.describe(
        days="How many days of inactivity before a recruit is 'stale'.",
    )
    async def recruit_followup(
        self, interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 3,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cutoff = (
            utc_now_naive() - _dt.timedelta(days=int(days))
        ).isoformat(" ", "seconds")
        rows = self.bot.db.recruit_list(limit=200)
        stale: list[dict] = []
        for r in rows:
            if r.get("status") in ("retained", "lost"):
                continue
            updated = r.get("updated_at") or r.get("created_at") or ""
            if updated and updated < cutoff:
                stale.append(r)
        if not stale:
            await interaction.followup.send(
                embed=success_embed(
                    "No stale recruits",
                    f"Every active prospect has been updated in the last "
                    f"{days} day(s).",
                ),
                ephemeral=True,
            )
            return
        lines = [_fmt_recruit_line(r) for r in stale[:20]]
        await interaction.followup.send(
            embed=info_embed(
                f"Needs follow-up ({len(stale)})",
                "\n".join(lines)[:4000]
                + ("\n…" if len(stale) > 20 else ""),
            ),
            ephemeral=True,
        )

    # ── /recruit leaderboard ────────────────────────────────────────────────
    @recruit.command(
        name="leaderboard",
        description="Per-recruiter funnel stats. Officers only.",
    )
    @app_commands.describe(
        days="Look back this many days (0 = all-time).",
    )
    async def recruit_leaderboard(
        self, interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 365] = 30,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied",
                                  "Officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        since: str | None = None
        if days > 0:
            since = (
                utc_now_naive() - _dt.timedelta(days=int(days))
            ).isoformat(" ", "seconds")
        rows = self.bot.db.recruit_leaderboard(since_iso=since)
        if not rows:
            await interaction.followup.send(
                embed=info_embed("Leaderboard",
                                 "No recruiter activity in this window."),
                ephemeral=True,
            )
            return
        lines = []
        for i, r in enumerate(rows[:20], start=1):
            conv = 0
            if int(r["prospects"] or 0):
                conv = round(100 * int(r["retained"] or 0) / int(r["prospects"]))
            lines.append(
                f"**{i}.** <@{r['recruiter_id']}> — "
                f"{r['prospects']} prospects · "
                f"{r['joined_discord']} discord · "
                f"{r['registered']} reg · "
                f"{r['first_event']} event · "
                f"**{r['retained']} retained** "
                f"({conv}%)"
            )
        title = f"Recruiter leaderboard ({days}d)" if days > 0 else "Recruiter leaderboard (all time)"
        await interaction.followup.send(
            embed=info_embed(title, "\n".join(lines)),
            ephemeral=True,
        )

    # ── persistent leaderboard ──────────────────────────────────────────────

    @recruit.command(
        name="pin-leaderboard",
        description="Pin a self-updating leaderboard embed in this channel (officers).",
    )
    @app_commands.describe(
        days="Look-back window for the pinned board (0 = all-time, default 30)",
    )
    async def pin_leaderboard(
        self, interaction: discord.Interaction,
        days: app_commands.Range[int, 0, 365] = 30,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This is officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = _build_leaderboard_embed(self.bot.db, days)
        try:
            msg = await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(
                f"recruit leaderboard post failed in "
                f"{interaction.channel}: {exc!r}"
            )
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't post",
                    "I couldn't post the leaderboard in this channel. "
                    "Check that I have View Channel, Send Messages and "
                    "Embed Links here.",
                ),
                ephemeral=True,
            )
            return
        db = self.bot.db
        db.set_config("recruit_leaderboard_channel_id", str(interaction.channel.id))
        db.set_config("recruit_leaderboard_message_id", str(msg.id))
        db.set_config("recruit_leaderboard_days", str(int(days)))
        try:
            await msg.pin(reason="Persistent recruitment leaderboard")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.followup.send(
            embed=success_embed(
                "Leaderboard pinned",
                f"Auto-refreshes hourly. Window: {days} day(s).",
            ),
            ephemeral=True,
        )

    @tasks.loop(hours=1)
    async def refresh_pinned_leaderboard(self) -> None:
        db = self.bot.db
        chan_id = db.get_config("recruit_leaderboard_channel_id")
        msg_id = db.get_config("recruit_leaderboard_message_id")
        if not chan_id or not msg_id:
            return
        try:
            days_raw = db.get_config("recruit_leaderboard_days") or "30"
            days = int(days_raw)
        except (TypeError, ValueError):
            days = 30
        try:
            chan = self.bot.get_channel(int(chan_id)) or await self.bot.fetch_channel(int(chan_id))
            msg = await chan.fetch_message(int(msg_id))
            await msg.edit(embed=_build_leaderboard_embed(db, days))
        except discord.NotFound:
            warning_log(
                "recruit leaderboard message missing — clearing pin config."
            )
            db.set_config("recruit_leaderboard_channel_id", "")
            db.set_config("recruit_leaderboard_message_id", "")
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"refresh_pinned_leaderboard failed: {exc!r}")

    @refresh_pinned_leaderboard.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()


def _build_leaderboard_embed(db, days: int) -> discord.Embed:
    since: str | None = None
    if days > 0:
        since = (
            utc_now_naive() - _dt.timedelta(days=int(days))
        ).isoformat(" ", "seconds")
    rows = db.recruit_leaderboard(since_iso=since) or []
    if not rows:
        return info_embed(
            f"Recruiter leaderboard ({days}d)" if days > 0 else "Recruiter leaderboard (all time)",
            "_No recruiter activity in this window yet._\n\n"
            "Recruiters log new prospects with `/recruit add`.",
        )
    lines = []
    for i, r in enumerate(rows[:15], start=1):
        conv = 0
        if int(r["prospects"] or 0):
            conv = round(100 * int(r["retained"] or 0) / int(r["prospects"]))
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"**{i}.**")
        lines.append(
            f"{medal} <@{r['recruiter_id']}> — "
            f"**{r['retained']} retained** / {r['prospects']} prospects "
            f"({conv}%)\n"
            f"   {r['joined_discord']} discord · {r['registered']} reg · "
            f"{r['first_event']} event"
        )
    title = (
        f"🏆 Recruiter leaderboard — last {days} day(s)"
        if days > 0 else "🏆 Recruiter leaderboard — all time"
    )
    embed = discord.Embed(
        title=title,
        description="\n\n".join(lines)[:4000],
        color=discord.Color.gold(),
        timestamp=utc_now_naive(),
    )
    embed.set_footer(text="Auto-refreshes hourly • UTC")
    return embed


async def setup(bot: Bot) -> None:
    cog = RecruitmentCog(bot)
    await bot.add_cog(cog)
    cog.refresh_pinned_leaderboard.start()
    info_log("Initialized Recruitment cog.")
