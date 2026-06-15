"""Member feedback surveys.

Small persistent survey panels let members submit structured feedback without
needing an external form. Responses are stored locally for leadership export.
"""

from __future__ import annotations

import csv
import datetime
import io

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, success_embed

SURVEY_KEY = "member-health-v1"
EXIT_SURVEY_KEY = "exit-v1"
PUBLIC_BUTTON_ID = "member-survey:start"
ANON_BUTTON_ID = "member-survey:start-anon"
EXIT_BUTTON_PREFIX = "exit-survey:reason:"

EXIT_REASONS: tuple[tuple[str, str], ...] = (
    ("not_enough_content", "Not enough content"),
    ("wrong_content", "Wrong content type"),
    ("discord_confusing", "Discord was confusing"),
    ("joined_other_guild", "Joined another guild"),
    ("taking_break", "Taking a break"),
    ("other", "Other"),
)


def ensure_schema(db) -> None:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS member_survey_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            survey_key TEXT NOT NULL,
            respondent_id TEXT,
            respondent_name TEXT,
            anonymous INTEGER NOT NULL DEFAULT 0,
            how_joined TEXT,
            content_interests TEXT,
            onboarding_feedback TEXT,
            stay_leave_factors TEXT,
            improvement_priority TEXT,
            submitted_at TEXT NOT NULL
        )
        """
    )
    db.cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_member_survey_key "
        "ON member_survey_responses(survey_key)"
    )
    db.cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS exit_survey_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT,
            display_name TEXT,
            albion_name TEXT,
            lifecycle_role TEXT,
            guild_name TEXT,
            joined_at TEXT,
            left_at TEXT NOT NULL,
            dm_message_id TEXT,
            dm_status TEXT NOT NULL DEFAULT 'pending',
            dm_error TEXT,
            responded_at TEXT
        )
        """
    )
    db.cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_exit_survey_requests_user "
        "ON exit_survey_requests(user_id, id DESC)"
    )
    db.cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_exit_survey_requests_left "
        "ON exit_survey_requests(left_at)"
    )
    db.cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS exit_survey_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            survey_key TEXT NOT NULL,
            guild_id TEXT,
            user_id TEXT NOT NULL,
            user_name TEXT,
            reason_key TEXT NOT NULL,
            reason_label TEXT NOT NULL,
            note TEXT,
            requested_at TEXT,
            submitted_at TEXT NOT NULL
        )
        """
    )
    db.cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_exit_survey_responses_submitted "
        "ON exit_survey_responses(submitted_at)"
    )
    db.connection.commit()


def _crest_url(db) -> str | None:
    try:
        return (db.get_config("announce_crest_url") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def build_member_survey_embed(db) -> discord.Embed:
    embed = discord.Embed(
        title="📋 Optional Home-Guild Member Survey",
        description=(
            "Help leadership understand how members find home guild, what keeps people active, "
            "what makes people drift away, and whether our Discord systems are easy to use.\n\n"
            "This is optional. Honest answers are more useful than perfect answers."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="What We’re Trying To Learn",
        value=(
            "• How members found the guild and why they joined\n"
            "• What content people actually want to run\n"
            "• Whether onboarding, Discord, LFG, regear, and comps are clear\n"
            "• What makes members stay active or go inactive\n"
            "• What leadership should improve first"
        ),
        inline=False,
    )
    embed.add_field(
        name="How It Works",
        value=(
            "Click a button below and answer the short form. "
            "You can submit again later if you think of something else."
        ),
        inline=False,
    )
    crest = _crest_url(db)
    if crest:
        embed.set_thumbnail(url=crest)
        embed.set_footer(text="Home Guild member feedback", icon_url=crest)
    else:
        embed.set_footer(text="Home Guild member feedback")
    return embed


def build_exit_survey_embed(db, guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="Quick Exit Survey",
        description=(
            f"You recently left **{guild_name}**. No pressure and no follow-up spam, "
            "but one quick answer helps leadership understand what needs improving."
        ),
        color=discord.Color.dark_gray(),
    )
    embed.add_field(
        name="Why We Ask",
        value=(
            "We use exit feedback to improve content planning, onboarding, "
            "Discord navigation, and guild culture."
        ),
        inline=False,
    )
    embed.add_field(
        name="How It Works",
        value="Pick the closest reason below. You can add a short optional note after clicking.",
        inline=False,
    )
    crest = _crest_url(db)
    if crest:
        embed.set_thumbnail(url=crest)
        embed.set_footer(text="Home Guild exit feedback", icon_url=crest)
    else:
        embed.set_footer(text="Home Guild exit feedback")
    return embed


def _save_response(
    db,
    *,
    user: discord.abc.User,
    anonymous: bool,
    how_joined: str,
    content_interests: str,
    onboarding_feedback: str,
    stay_leave_factors: str,
    improvement_priority: str,
) -> None:
    ensure_schema(db)
    submitted_at = discord.utils.utcnow().isoformat()
    db.cursor.execute(
        """
        INSERT INTO member_survey_responses (
            survey_key, respondent_id, respondent_name, anonymous,
            how_joined, content_interests, onboarding_feedback,
            stay_leave_factors, improvement_priority, submitted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SURVEY_KEY,
            None if anonymous else str(user.id),
            "Anonymous" if anonymous else str(user),
            1 if anonymous else 0,
            how_joined.strip(),
            content_interests.strip(),
            onboarding_feedback.strip(),
            stay_leave_factors.strip(),
            improvement_priority.strip(),
            submitted_at,
        ),
    )
    db.connection.commit()


def _now_iso() -> str:
    return discord.utils.utcnow().isoformat()


def _member_joined_iso(member: discord.Member) -> str | None:
    joined_at = getattr(member, "joined_at", None)
    if not joined_at:
        return None
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
    return joined_at.isoformat()


def _record_exit_request(
    db,
    *,
    member: discord.Member,
    profile: dict | None,
) -> int:
    ensure_schema(db)
    profile = profile or {}
    left_at = _now_iso()
    db.cursor.execute(
        """
        INSERT INTO exit_survey_requests (
            guild_id, user_id, user_name, display_name,
            albion_name, lifecycle_role, guild_name,
            joined_at, left_at, dm_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            str(member.guild.id),
            str(member.id),
            str(member),
            member.display_name,
            (profile.get("albion_name") or "").strip() or None,
            (profile.get("lifecycle_role") or "").strip() or None,
            (profile.get("guild_name") or "").strip() or None,
            _member_joined_iso(member),
            left_at,
        ),
    )
    db.connection.commit()
    return int(db.cursor.lastrowid)


def _mark_exit_request_dm(
    db,
    request_id: int,
    *,
    status: str,
    message_id: int | None = None,
    error: str | None = None,
) -> None:
    ensure_schema(db)
    db.cursor.execute(
        """
        UPDATE exit_survey_requests
        SET dm_status = ?, dm_message_id = COALESCE(?, dm_message_id), dm_error = ?
        WHERE id = ?
        """,
        (status, str(message_id) if message_id else None, error, int(request_id)),
    )
    db.connection.commit()


def _latest_exit_request(db, user_id: str) -> dict | None:
    ensure_schema(db)
    db.cursor.execute(
        """
        SELECT *
        FROM exit_survey_requests
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(user_id),),
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def _save_exit_response(
    db,
    *,
    user: discord.abc.User,
    reason_key: str,
    reason_label: str,
    note: str,
) -> None:
    ensure_schema(db)
    submitted_at = _now_iso()
    request = _latest_exit_request(db, str(user.id))
    db.cursor.execute(
        """
        INSERT INTO exit_survey_responses (
            survey_key, guild_id, user_id, user_name,
            reason_key, reason_label, note, requested_at, submitted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            EXIT_SURVEY_KEY,
            request.get("guild_id") if request else None,
            str(user.id),
            str(user),
            reason_key,
            reason_label,
            note.strip(),
            request.get("left_at") if request else None,
            submitted_at,
        ),
    )
    if request:
        db.cursor.execute(
            "UPDATE exit_survey_requests SET responded_at = ? WHERE id = ?",
            (submitted_at, int(request["id"])),
        )
    db.connection.commit()


class MemberSurveyModal(discord.ui.Modal):
    def __init__(self, bot: Bot, *, anonymous: bool) -> None:
        self.bot = bot
        self.anonymous = anonymous
        title = "Anonymous Home-Guild Survey" if anonymous else "Home-Guild Member Survey"
        super().__init__(title=title, timeout=900)

        self.how_joined = discord.ui.TextInput(
            label="How did you find home guild?",
            placeholder="Friend, guild finder, in-game recruit, Discord post... why did you join?",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=False,
        )
        self.content_interests = discord.ui.TextInput(
            label="What content do you like?",
            placeholder="BZ roaming, ganking, Ava roads, ZvZ, gathering, teaching, economy, etc.",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=False,
        )
        self.onboarding_feedback = discord.ui.TextInput(
            label="Discord/onboarding feedback",
            placeholder="Can you find LFG, regear, comps, content roles, voice channels, help?",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=False,
        )
        self.stay_leave_factors = discord.ui.TextInput(
            label="Why stay or leave?",
            placeholder="What keeps you active? What frustrates you or might make you go inactive?",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=False,
        )
        self.improvement_priority = discord.ui.TextInput(
            label="What should improve first?",
            placeholder="Scheduling, training, comps, recruiting, culture, comms, Discord, leadership...",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=False,
        )

        self.add_item(self.how_joined)
        self.add_item(self.content_interests)
        self.add_item(self.onboarding_feedback)
        self.add_item(self.stay_leave_factors)
        self.add_item(self.improvement_priority)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            _save_response(
                self.bot.db,
                user=interaction.user,
                anonymous=self.anonymous,
                how_joined=str(self.how_joined.value),
                content_interests=str(self.content_interests.value),
                onboarding_feedback=str(self.onboarding_feedback.value),
                stay_leave_factors=str(self.stay_leave_factors.value),
                improvement_priority=str(self.improvement_priority.value),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"member survey save failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("Survey failed", "I could not save that response. Try again later."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Survey submitted",
                "Thank you. This helps us understand what is working and what needs fixing.",
            ),
            ephemeral=True,
        )


class MemberSurveyView(discord.ui.View):
    def __init__(self, bot: Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Take Survey",
        style=discord.ButtonStyle.primary,
        custom_id=PUBLIC_BUTTON_ID,
    )
    async def take_survey(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(MemberSurveyModal(self.bot, anonymous=False))

    @discord.ui.button(
        label="Anonymous Survey",
        style=discord.ButtonStyle.secondary,
        custom_id=ANON_BUTTON_ID,
    )
    async def take_anon_survey(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(MemberSurveyModal(self.bot, anonymous=True))


class ExitSurveyNoteModal(discord.ui.Modal):
    def __init__(self, bot: Bot, *, reason_key: str, reason_label: str) -> None:
        self.bot = bot
        self.reason_key = reason_key
        self.reason_label = reason_label
        super().__init__(title="Exit Survey", timeout=900)

        self.note = discord.ui.TextInput(
            label="Optional note",
            placeholder="Anything leadership should know? Leave blank if the button says enough.",
            style=discord.TextStyle.paragraph,
            max_length=900,
            required=False,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            _save_exit_response(
                self.bot.db,
                user=interaction.user,
                reason_key=self.reason_key,
                reason_label=self.reason_label,
                note=str(self.note.value),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"exit survey save failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("Survey failed", "I could not save that response. Try again later."),
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "Feedback received",
                "Thank you. No follow-up reminders will be sent.",
            ),
        )


class ExitSurveyReasonButton(discord.ui.Button):
    def __init__(self, reason_key: str, reason_label: str, *, row: int) -> None:
        super().__init__(
            label=reason_label,
            style=discord.ButtonStyle.secondary,
            custom_id=f"{EXIT_BUTTON_PREFIX}{reason_key}",
            row=row,
        )
        self.reason_key = reason_key
        self.reason_label = reason_label

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        bot = getattr(view, "bot", None)
        if bot is None:
            await interaction.response.send_message(
                embed=error_embed("Survey unavailable", "The survey view is not ready. Try again later."),
            )
            return
        await interaction.response.send_modal(
            ExitSurveyNoteModal(
                bot,
                reason_key=self.reason_key,
                reason_label=self.reason_label,
            )
        )


class ExitSurveyView(discord.ui.View):
    def __init__(self, bot: Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        for idx, (reason_key, reason_label) in enumerate(EXIT_REASONS):
            self.add_item(ExitSurveyReasonButton(reason_key, reason_label, row=idx // 3))


class Surveys(commands.Cog):
    group = app_commands.Group(name="survey", description="Guild survey tools.")

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        ensure_schema(bot.db)
        bot.add_view(MemberSurveyView(bot))
        bot.add_view(ExitSurveyView(bot))

    async def send_exit_survey(
        self,
        member: discord.Member,
        profile: dict | None,
    ) -> None:
        """Send one best-effort exit survey DM after a voluntary server leave."""
        enabled = (self.bot.db.get_config("exit_survey_enabled") or "0").strip()
        if enabled == "0":
            return

        request_id = _record_exit_request(
            self.bot.db,
            member=member,
            profile=profile,
        )
        try:
            msg = await member.send(
                embed=build_exit_survey_embed(self.bot.db, member.guild.name),
                view=ExitSurveyView(self.bot),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden as exc:
            _mark_exit_request_dm(
                self.bot.db,
                request_id,
                status="blocked",
                error=repr(exc)[:500],
            )
            info_log(f"exit survey DM blocked for {member} ({member.id}).")
            return
        except discord.HTTPException as exc:
            _mark_exit_request_dm(
                self.bot.db,
                request_id,
                status="failed",
                error=repr(exc)[:500],
            )
            error_log(f"exit survey DM failed for {member} ({member.id}): {exc!r}")
            return

        _mark_exit_request_dm(
            self.bot.db,
            request_id,
            status="sent",
            message_id=msg.id,
        )
        info_log(f"exit survey DM sent to {member} ({member.id}).")

    @group.command(name="post-member", description="Post the optional member survey panel.")
    @app_commands.default_permissions(manage_guild=True)
    async def post_member(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        dest = channel or interaction.channel
        if not isinstance(dest, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("Wrong channel", "Post the survey in a text channel."),
                ephemeral=True,
            )
            return
        msg = await dest.send(
            embed=build_member_survey_embed(self.bot.db),
            view=MemberSurveyView(self.bot),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await interaction.response.send_message(
            embed=success_embed("Survey posted", f"Survey panel posted: {msg.jump_url}"),
            ephemeral=True,
        )
        info_log(f"member survey posted in #{dest.name} ({msg.id}).")

    @group.command(name="summary", description="Show member survey response counts.")
    @app_commands.default_permissions(manage_guild=True)
    async def summary(self, interaction: discord.Interaction) -> None:
        ensure_schema(self.bot.db)
        self.bot.db.cursor.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN anonymous = 1 THEN 1 ELSE 0 END) AS anon,
                   MAX(submitted_at) AS latest
            FROM member_survey_responses
            WHERE survey_key = ?
            """,
            (SURVEY_KEY,),
        )
        row = self.bot.db.cursor.fetchone()
        total = int(row["total"] or 0) if row else 0
        anon = int(row["anon"] or 0) if row else 0
        latest = row["latest"] if row else None
        latest_line = f"\nLatest: `{latest}`" if latest else ""
        await interaction.response.send_message(
            embed=success_embed(
                "Survey summary",
                f"Responses: **{total}**\nAnonymous: **{anon}**{latest_line}",
            ),
            ephemeral=True,
        )

    @group.command(name="export", description="Export member survey responses as CSV.")
    @app_commands.default_permissions(manage_guild=True)
    async def export(self, interaction: discord.Interaction) -> None:
        ensure_schema(self.bot.db)
        self.bot.db.cursor.execute(
            """
            SELECT id, submitted_at, anonymous, respondent_id, respondent_name,
                   how_joined, content_interests, onboarding_feedback,
                   stay_leave_factors, improvement_priority
            FROM member_survey_responses
            WHERE survey_key = ?
            ORDER BY id
            """,
            (SURVEY_KEY,),
        )
        rows = [dict(row) for row in self.bot.db.cursor.fetchall()]
        if not rows:
            await interaction.response.send_message(
                embed=error_embed("No responses", "No member survey responses have been submitted yet."),
                ephemeral=True,
            )
            return

        text_io = io.StringIO()
        writer = csv.DictWriter(text_io, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        data = io.BytesIO(text_io.getvalue().encode("utf-8"))
        file = discord.File(data, filename="tu-member-survey.csv")
        await interaction.response.send_message(
            content="Member survey export.",
            file=file,
            ephemeral=True,
        )

    @group.command(name="exit-summary", description="Show exit survey response counts.")
    @app_commands.describe(days="How many recent days to include.")
    @app_commands.default_permissions(manage_guild=True)
    async def exit_summary(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = 30,
    ) -> None:
        ensure_schema(self.bot.db)
        cutoff = (discord.utils.utcnow() - datetime.timedelta(days=int(days))).isoformat()
        self.bot.db.cursor.execute(
            """
            SELECT dm_status, COUNT(*) AS total
            FROM exit_survey_requests
            WHERE left_at >= ?
            GROUP BY dm_status
            ORDER BY total DESC
            """,
            (cutoff,),
        )
        request_rows = [dict(row) for row in self.bot.db.cursor.fetchall()]
        request_total = sum(int(row["total"] or 0) for row in request_rows)
        dm_lines = [
            f"{row['dm_status'] or 'unknown'}: **{int(row['total'] or 0)}**"
            for row in request_rows
        ] or ["No exit survey DMs attempted."]

        self.bot.db.cursor.execute(
            """
            SELECT reason_label, COUNT(*) AS total
            FROM exit_survey_responses
            WHERE survey_key = ? AND submitted_at >= ?
            GROUP BY reason_label
            ORDER BY total DESC, reason_label ASC
            """,
            (EXIT_SURVEY_KEY, cutoff),
        )
        reason_rows = [dict(row) for row in self.bot.db.cursor.fetchall()]
        response_total = sum(int(row["total"] or 0) for row in reason_rows)
        reason_lines = [
            f"{row['reason_label']}: **{int(row['total'] or 0)}**"
            for row in reason_rows
        ] or ["No exit survey responses yet."]

        rate = (response_total / request_total * 100.0) if request_total else 0.0
        embed = discord.Embed(
            title=f"Exit Survey Summary · last {int(days)}d",
            description=(
                f"DM attempts: **{request_total}**\n"
                f"Responses: **{response_total}**\n"
                f"Response rate: **{rate:.1f}%**"
            ),
            color=discord.Color.dark_gray(),
        )
        embed.add_field(name="DM Status", value="\n".join(dm_lines), inline=True)
        embed.add_field(name="Reasons", value="\n".join(reason_lines), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(name="exit-export", description="Export exit survey responses as CSV.")
    @app_commands.describe(days="How many recent days to include.")
    @app_commands.default_permissions(manage_guild=True)
    async def exit_export(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365] = 90,
    ) -> None:
        ensure_schema(self.bot.db)
        cutoff = (discord.utils.utcnow() - datetime.timedelta(days=int(days))).isoformat()
        self.bot.db.cursor.execute(
            """
            SELECT r.id, r.submitted_at, r.user_id, r.user_name,
                   r.reason_label, r.note,
                   q.guild_id, q.display_name, q.albion_name,
                   q.lifecycle_role, q.guild_name, q.joined_at,
                   q.left_at, q.dm_status
            FROM exit_survey_responses r
            LEFT JOIN exit_survey_requests q
              ON q.user_id = r.user_id
             AND q.left_at = r.requested_at
            WHERE r.survey_key = ? AND r.submitted_at >= ?
            ORDER BY r.id
            """,
            (EXIT_SURVEY_KEY, cutoff),
        )
        rows = [dict(row) for row in self.bot.db.cursor.fetchall()]
        if not rows:
            await interaction.response.send_message(
                embed=error_embed("No responses", "No exit survey responses have been submitted in that window."),
                ephemeral=True,
            )
            return

        text_io = io.StringIO()
        writer = csv.DictWriter(text_io, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        data = io.BytesIO(text_io.getvalue().encode("utf-8"))
        file = discord.File(data, filename="tu-exit-survey.csv")
        await interaction.response.send_message(
            content=f"Exit survey export for the last {int(days)} day(s).",
            file=file,
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(Surveys(bot))
