"""Collaborative guide editing helpers.

Pinned guide messages are official, so normal members cannot directly rewrite
them. This cog gives members a button-driven way to suggest edits/additions and
lets officers approve those suggestions into the guide channel as addendums.
"""

from __future__ import annotations

import datetime

import discord
from discord.ext import commands

from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, success_embed
from utils import is_officer as _is_officer


GUIDE_AVA_KEY = "t6_avalonian_dungeon"
PUBLIC_SUGGEST_ID = "guide:ava:suggest"
PUBLIC_ADD_ID = "guide:ava:add"

APPROVE_TEMPLATE = r"guide:approve:(?P<sid>[0-9]+)"
REJECT_TEMPLATE = r"guide:reject:(?P<sid>[0-9]+)"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ensure_schema(db) -> None:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS guide_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guide_key TEXT NOT NULL,
            suggestion_type TEXT NOT NULL,
            guild_id TEXT,
            source_channel_id TEXT,
            source_message_id TEXT,
            source_jump_url TEXT,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            section TEXT,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            submitted_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            review_note TEXT,
            review_channel_id TEXT,
            review_message_id TEXT,
            addendum_channel_id TEXT,
            addendum_message_id TEXT
        )
        """
    )
    db.cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_guide_suggestions_status "
        "ON guide_suggestions(status, id DESC)"
    )
    db.connection.commit()


def _crest_url(db) -> str | None:
    try:
        return (db.get_config("announce_crest_url") or "").strip() or None
    except Exception:  # noqa: BLE001
        return None


def _guide_color() -> discord.Color:
    return discord.Color.from_str("#d4af37")


def _type_label(suggestion_type: str) -> str:
    return "Guide addition" if suggestion_type == "add" else "Guide edit"


def _db_insert_suggestion(
    db,
    *,
    guide_key: str,
    suggestion_type: str,
    guild_id: str | None,
    source_channel_id: str | None,
    source_message_id: str | None,
    source_jump_url: str | None,
    author_id: str,
    author_name: str,
    section: str,
    body: str,
) -> int:
    ensure_schema(db)
    db.cursor.execute(
        """
        INSERT INTO guide_suggestions (
            guide_key, suggestion_type, guild_id, source_channel_id,
            source_message_id, source_jump_url, author_id, author_name,
            section, body, submitted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guide_key,
            suggestion_type,
            guild_id,
            source_channel_id,
            source_message_id,
            source_jump_url,
            author_id,
            author_name,
            section,
            body,
            _now_iso(),
        ),
    )
    db.connection.commit()
    return int(db.cursor.lastrowid or 0)


def _db_get(db, suggestion_id: int) -> dict | None:
    ensure_schema(db)
    db.cursor.execute("SELECT * FROM guide_suggestions WHERE id = ?", (suggestion_id,))
    row = db.cursor.fetchone()
    return dict(row) if row else None


def _db_mark_review_posted(
    db,
    suggestion_id: int,
    *,
    channel_id: str,
    message_id: str,
) -> None:
    ensure_schema(db)
    db.cursor.execute(
        """
        UPDATE guide_suggestions
           SET review_channel_id = ?, review_message_id = ?
         WHERE id = ?
        """,
        (channel_id, message_id, suggestion_id),
    )
    db.connection.commit()


def _db_mark_resolved(
    db,
    suggestion_id: int,
    *,
    status: str,
    reviewed_by: str,
    review_note: str | None = None,
    addendum_channel_id: str | None = None,
    addendum_message_id: str | None = None,
) -> None:
    ensure_schema(db)
    db.cursor.execute(
        """
        UPDATE guide_suggestions
           SET status = ?,
               reviewed_at = ?,
               reviewed_by = ?,
               review_note = ?,
               addendum_channel_id = COALESCE(?, addendum_channel_id),
               addendum_message_id = COALESCE(?, addendum_message_id)
         WHERE id = ?
        """,
        (
            status,
            _now_iso(),
            reviewed_by,
            review_note,
            addendum_channel_id,
            addendum_message_id,
            suggestion_id,
        ),
    )
    db.connection.commit()


def _review_embed(db, suggestion: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📚 Guide suggestion #{suggestion['id']} — {_type_label(suggestion['suggestion_type'])}",
        description=str(suggestion.get("body") or "")[:3800],
        color=_guide_color(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="Guide",
        value="T6 Avalonian Dungeon",
        inline=True,
    )
    embed.add_field(
        name="Section",
        value=(str(suggestion.get("section") or "General")[:1024] or "General"),
        inline=True,
    )
    embed.add_field(
        name="Submitted by",
        value=f"<@{suggestion['author_id']}>",
        inline=True,
    )
    if suggestion.get("source_jump_url"):
        embed.add_field(
            name="Guide post",
            value=f"[Open guide]({suggestion['source_jump_url']})",
            inline=False,
        )
    crest = _crest_url(db)
    if crest:
        embed.set_thumbnail(url=crest)
        embed.set_footer(text=f"Status: {suggestion['status']}", icon_url=crest)
    else:
        embed.set_footer(text=f"Status: {suggestion['status']}")
    return embed


def _addendum_embed(db, suggestion: dict, approver: discord.abc.User) -> discord.Embed:
    section = str(suggestion.get("section") or "General").strip() or "General"
    embed = discord.Embed(
        title=f"📌 Guide Addendum — {section[:180]}",
        description=str(suggestion.get("body") or "")[:3900],
        color=_guide_color(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Guide", value="T6 Avalonian Dungeon", inline=True)
    embed.add_field(name="Type", value=_type_label(str(suggestion.get("suggestion_type") or "")), inline=True)
    embed.add_field(name="Submitted by", value=f"<@{suggestion['author_id']}>", inline=True)
    crest = _crest_url(db)
    footer = f"Approved by {getattr(approver, 'display_name', str(approver))}"
    if crest:
        embed.set_thumbnail(url=crest)
        embed.set_footer(text=footer, icon_url=crest)
    else:
        embed.set_footer(text=footer)
    return embed


class GuideSuggestionModal(discord.ui.Modal):
    def __init__(self, bot: Bot, *, suggestion_type: str) -> None:
        title = "Suggest guide edit" if suggestion_type == "edit" else "Add guide tip"
        super().__init__(title=title, timeout=None)
        self.bot = bot
        self.suggestion_type = suggestion_type
        self.section = discord.ui.TextInput(
            label="Section / topic",
            placeholder="e.g. Boss Door Routine, Ironroot main healer, loot split",
            max_length=120,
            required=True,
        )
        self.body = discord.ui.TextInput(
            label="What should be changed or added?",
            placeholder="Write the correction, extra tip, or new note here.",
            style=discord.TextStyle.paragraph,
            max_length=1800,
            required=True,
        )
        self.add_item(self.section)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Guides") if isinstance(interaction.client, commands.Bot) else None
        if not isinstance(cog, Guides):
            await interaction.response.send_message("Guide helper is not loaded.", ephemeral=True)
            return
        await cog.submit_suggestion(
            interaction,
            suggestion_type=self.suggestion_type,
            section=str(self.section.value or "").strip(),
            body=str(self.body.value or "").strip(),
        )


class GuidePublicView(discord.ui.View):
    def __init__(self, bot: Bot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Suggest edit",
        style=discord.ButtonStyle.secondary,
        emoji="✏️",
        custom_id=PUBLIC_SUGGEST_ID,
    )
    async def suggest_edit(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(GuideSuggestionModal(self.bot, suggestion_type="edit"))

    @discord.ui.button(
        label="Add tip",
        style=discord.ButtonStyle.primary,
        emoji="➕",
        custom_id=PUBLIC_ADD_ID,
    )
    async def add_tip(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(GuideSuggestionModal(self.bot, suggestion_type="add"))


class GuideApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=APPROVE_TEMPLATE,
):
    def __init__(self, suggestion_id: int) -> None:
        self.suggestion_id = suggestion_id
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                emoji="✅",
                custom_id=f"guide:approve:{suggestion_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        cog = interaction.client.get_cog("Guides") if isinstance(interaction.client, commands.Bot) else None
        if isinstance(cog, Guides):
            await cog.approve_suggestion(interaction, self.suggestion_id)


class GuideRejectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=REJECT_TEMPLATE,
):
    def __init__(self, suggestion_id: int) -> None:
        self.suggestion_id = suggestion_id
        super().__init__(
            discord.ui.Button(
                label="Reject",
                style=discord.ButtonStyle.danger,
                emoji="🗑️",
                custom_id=f"guide:reject:{suggestion_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["sid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        cog = interaction.client.get_cog("Guides") if isinstance(interaction.client, commands.Bot) else None
        if isinstance(cog, Guides):
            await cog.reject_suggestion(interaction, self.suggestion_id)


def _review_view(suggestion_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(GuideApproveButton(suggestion_id))
    view.add_item(GuideRejectButton(suggestion_id))
    return view


class Guides(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        ensure_schema(self.bot.db)
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        self.bot.add_view(GuidePublicView(self.bot))
        self.bot.add_dynamic_items(GuideApproveButton, GuideRejectButton)

    def _review_channel_id(self) -> str | None:
        db = self.bot.db
        return (
            db.get_config("guide_review_channel_id")
            or db.get_config("automation_officer_channel_id")
            or db.get_config("officer_channel_id")
        )

    async def _fetch_text_channel(self, channel_id: str | None) -> discord.TextChannel | None:
        if not channel_id:
            return None
        try:
            ch = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            return None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def submit_suggestion(
        self,
        interaction: discord.Interaction,
        *,
        suggestion_type: str,
        section: str,
        body: str,
    ) -> None:
        if not body:
            await interaction.response.send_message(
                embed=error_embed("Nothing submitted", "Write the change or addition first."),
                ephemeral=True,
            )
            return
        source_message = interaction.message
        suggestion_id = _db_insert_suggestion(
            self.bot.db,
            guide_key=GUIDE_AVA_KEY,
            suggestion_type=suggestion_type,
            guild_id=str(interaction.guild_id) if interaction.guild_id else None,
            source_channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            source_message_id=str(source_message.id) if source_message else None,
            source_jump_url=getattr(source_message, "jump_url", None),
            author_id=str(interaction.user.id),
            author_name=str(interaction.user),
            section=section[:120],
            body=body[:1800],
        )
        suggestion = _db_get(self.bot.db, suggestion_id)
        review_channel = await self._fetch_text_channel(self._review_channel_id())
        if suggestion and review_channel:
            try:
                msg = await review_channel.send(
                    embed=_review_embed(self.bot.db, suggestion),
                    view=_review_view(suggestion_id),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                _db_mark_review_posted(
                    self.bot.db,
                    suggestion_id,
                    channel_id=str(review_channel.id),
                    message_id=str(msg.id),
                )
            except discord.HTTPException as exc:
                error_log(f"guide suggestion review post failed #{suggestion_id}: {exc!r}")

        await interaction.response.send_message(
            embed=success_embed(
                "Suggestion submitted",
                "Staff will review it. If approved, it will be posted back into this guide channel as an official addendum.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} submitted guide suggestion #{suggestion_id}.")

    async def approve_suggestion(
        self,
        interaction: discord.Interaction,
        suggestion_id: int,
    ) -> None:
        suggestion = _db_get(self.bot.db, suggestion_id)
        if not suggestion:
            await interaction.response.send_message(
                embed=error_embed("Missing suggestion", "That suggestion no longer exists."),
                ephemeral=True,
            )
            return
        if suggestion["status"] != "pending":
            await interaction.response.send_message(
                embed=error_embed("Already resolved", f"Status is `{suggestion['status']}`."),
                ephemeral=True,
            )
            return
        target = await self._fetch_text_channel(suggestion.get("source_channel_id"))
        if target is None:
            await interaction.response.send_message(
                embed=error_embed("Guide channel missing", "I cannot find the original guide channel."),
                ephemeral=True,
            )
            return
        try:
            addendum = await target.send(
                embed=_addendum_embed(self.bot.db, suggestion, interaction.user),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                embed=error_embed("Post failed", f"Discord rejected the addendum: `{exc}`"),
                ephemeral=True,
            )
            return
        _db_mark_resolved(
            self.bot.db,
            suggestion_id,
            status="approved",
            reviewed_by=str(interaction.user.id),
            addendum_channel_id=str(addendum.channel.id),
            addendum_message_id=str(addendum.id),
        )
        updated = _db_get(self.bot.db, suggestion_id) or {**suggestion, "status": "approved"}
        await interaction.response.edit_message(
            embed=_review_embed(self.bot.db, updated),
            view=None,
        )

    async def reject_suggestion(
        self,
        interaction: discord.Interaction,
        suggestion_id: int,
    ) -> None:
        suggestion = _db_get(self.bot.db, suggestion_id)
        if not suggestion:
            await interaction.response.send_message(
                embed=error_embed("Missing suggestion", "That suggestion no longer exists."),
                ephemeral=True,
            )
            return
        if suggestion["status"] != "pending":
            await interaction.response.send_message(
                embed=error_embed("Already resolved", f"Status is `{suggestion['status']}`."),
                ephemeral=True,
            )
            return
        _db_mark_resolved(
            self.bot.db,
            suggestion_id,
            status="rejected",
            reviewed_by=str(interaction.user.id),
        )
        updated = _db_get(self.bot.db, suggestion_id) or {**suggestion, "status": "rejected"}
        await interaction.response.edit_message(
            embed=_review_embed(self.bot.db, updated),
            view=None,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(Guides(bot))
