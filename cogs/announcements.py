"""Officer-facing announcement cog.

Posts branded "official announcement" embeds with the guild crest as the
thumbnail watermark and the posting officer's name in the footer.

Commands (officer / Manage Guild):
    /announce post       — Opens a modal; you fill in title + body; bot posts
                           a branded embed in the channel you pick.
    /announce config     — Set the crest image URL, accent color, and the
                           guild's display name shown in the footer.

Configuration is stored in the existing ``guild_config`` key/value table:
    announce_crest_url      — public image URL used as embed thumbnail
    announce_color_hex      — accent color (e.g. ``#d4af37``); default gold
    announce_footer_name    — guild display name in footer; default
                              "Official Announcement"

Nothing here talks to the Discord gateway during cold-start — it only loads
on bot startup, so it does not affect the rate-limit behavior tightened in
``bot.py``.
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands

from debug import error_log, info_log
from utils import error_embed, success_embed


# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_COLOR_HEX  = "#d4af37"  # classic guild gold
DEFAULT_FOOTER     = "Official Announcement"

# Config keys (kept namespaced so they don't collide with other cogs).
CFG_CREST_URL    = "announce_crest_url"
CFG_COLOR_HEX    = "announce_color_hex"
CFG_FOOTER_NAME  = "announce_footer_name"

# Discord embed limits.
TITLE_MAX = 256
BODY_MAX  = 4000

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def _parse_color(raw: str | None) -> discord.Color:
    """Parse stored hex string into a discord.Color, falling back to gold."""
    candidate = (raw or DEFAULT_COLOR_HEX).strip()
    if not candidate.startswith("#"):
        candidate = "#" + candidate
    if not _HEX_RE.match(candidate):
        candidate = DEFAULT_COLOR_HEX
    return discord.Color.from_str(candidate)


def _build_announcement_embed(
    *,
    title: str,
    body: str,
    color: discord.Color,
    crest_url: str | None,
    footer_text: str,
    officer: discord.Member | discord.User,
) -> discord.Embed:
    """Construct the branded announcement embed."""
    embed = discord.Embed(
        title=title.strip()[:TITLE_MAX],
        description=body.strip()[:BODY_MAX],
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    if crest_url:
        embed.set_thumbnail(url=crest_url)
        # Footer icon = crest watermark; footer text = guild name + officer.
        embed.set_footer(
            text=f"{footer_text} · Posted by {officer.display_name}",
            icon_url=crest_url,
        )
    else:
        embed.set_footer(text=f"{footer_text} · Posted by {officer.display_name}")
    return embed


# ── Modal: title + body ──────────────────────────────────────────────────────
class _AnnouncementModal(discord.ui.Modal, title="Official Announcement"):
    def __init__(
        self,
        *,
        bot: Bot,
        target_channel: discord.TextChannel,
        ping_role: discord.Role | None,
    ) -> None:
        super().__init__()
        self.bot: Bot = bot
        self.target_channel = target_channel
        self.ping_role = ping_role

        self.title_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Title",
            placeholder="e.g. ZvZ tonight at 9 PM EST",
            max_length=TITLE_MAX,
            required=True,
        )
        self.body_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Body",
            placeholder="Markdown supported. Use blank lines for paragraphs.",
            style=discord.TextStyle.paragraph,
            max_length=BODY_MAX,
            required=True,
        )
        self.add_item(self.title_input)
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defer ephemerally so the modal closes promptly even on a slow channel.
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db  # type: ignore[attr-defined]
        crest_url   = db.get_config(CFG_CREST_URL)
        color       = _parse_color(db.get_config(CFG_COLOR_HEX))
        footer_text = db.get_config(CFG_FOOTER_NAME) or DEFAULT_FOOTER

        embed = _build_announcement_embed(
            title=str(self.title_input.value),
            body=str(self.body_input.value),
            color=color,
            crest_url=crest_url,
            footer_text=footer_text,
            officer=interaction.user,
        )

        content: str | None = None
        allowed = discord.AllowedMentions.none()
        if self.ping_role is not None:
            content = self.ping_role.mention
            allowed = discord.AllowedMentions(roles=[self.ping_role])

        try:
            await self.target_channel.send(
                content=content, embed=embed, allowed_mentions=allowed,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=error_embed(
                    "Missing permissions",
                    f"I can't post in {self.target_channel.mention}. "
                    f"Grant me **Send Messages** + **Embed Links** there.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"announce post failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Post failed", f"Discord rejected the message: `{exc}`."),
                ephemeral=True,
            )
            return

        info_log(
            f"{interaction.user} posted announcement "
            f"'{self.title_input.value}' in #{self.target_channel.name}."
        )
        await interaction.followup.send(
            embed=success_embed(
                "Announcement posted",
                f"Sent to {self.target_channel.mention}.",
            ),
            ephemeral=True,
        )


# ── Cog ──────────────────────────────────────────────────────────────────────
class Announcements(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    announce_group = app_commands.Group(
        name="announce",
        description="Post or configure official guild announcements.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    config_group = app_commands.Group(
        name="config",
        description="Configure announcement branding (officer).",
        parent=announce_group,
    )

    # ── /announce post ──────────────────────────────────────────────────────
    @announce_group.command(
        name="post",
        description="Open the announcement editor and post to a channel (officer).",
    )
    @app_commands.describe(
        channel="Channel to post the announcement in.",
        ping_role="Optional role to mention with the announcement.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        ping_role: discord.Role | None = None,
    ) -> None:
        # Sanity-check we can actually post in the target channel before
        # opening the modal — saves the officer typing a long body for nothing.
        me = channel.guild.me
        perms = channel.permissions_for(me) if me else None
        if perms is None or not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post there",
                    f"I need **Send Messages** + **Embed Links** in {channel.mention}.",
                ),
                ephemeral=True,
            )
            return

        modal = _AnnouncementModal(
            bot=self.bot, target_channel=channel, ping_role=ping_role,
        )
        await interaction.response.send_modal(modal)

    # ── /announce config set-crest ──────────────────────────────────────────
    @config_group.command(
        name="set-crest",
        description="Set the crest image URL used as the announcement watermark.",
    )
    @app_commands.describe(url="Public image URL (PNG/JPG/WEBP). Use a Discord CDN link.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_crest(self, interaction: discord.Interaction, url: str) -> None:
        url = url.strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid URL",
                    "Provide a full https:// URL to a public image.",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(CFG_CREST_URL, url)  # type: ignore[attr-defined]
        info_log(f"{interaction.user} set announcement crest URL.")
        await interaction.response.send_message(
            embed=success_embed("Crest updated", f"Watermark image set to:\n{url}"),
            ephemeral=True,
        )

    # ── /announce config set-color ──────────────────────────────────────────
    @config_group.command(
        name="set-color",
        description="Set the announcement embed accent color.",
    )
    @app_commands.describe(hex_color="Hex color, e.g. #d4af37 (gold), #c0392b (red).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_color(self, interaction: discord.Interaction, hex_color: str) -> None:
        candidate = hex_color.strip()
        if not candidate.startswith("#"):
            candidate = "#" + candidate
        if not _HEX_RE.match(candidate):
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid color",
                    "Use a 6-digit hex like `#d4af37`.",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(CFG_COLOR_HEX, candidate)  # type: ignore[attr-defined]
        await interaction.response.send_message(
            embed=success_embed("Color updated", f"Accent color set to `{candidate}`."),
            ephemeral=True,
        )

    # ── /announce config set-footer ─────────────────────────────────────────
    @config_group.command(
        name="set-footer",
        description="Set the guild name shown in the announcement footer.",
    )
    @app_commands.describe(text="e.g. 'HomeGuild · Official Announcement'")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_footer(self, interaction: discord.Interaction, text: str) -> None:
        text = text.strip()[:120]
        if not text:
            await interaction.response.send_message(
                embed=error_embed("Empty footer", "Provide some footer text."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(CFG_FOOTER_NAME, text)  # type: ignore[attr-defined]
        await interaction.response.send_message(
            embed=success_embed("Footer updated", f"Footer set to: `{text}`."),
            ephemeral=True,
        )

    # ── /announce config show ───────────────────────────────────────────────
    @config_group.command(
        name="show",
        description="Show current announcement branding settings.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def show_config(self, interaction: discord.Interaction) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        crest  = db.get_config(CFG_CREST_URL) or "*not set*"
        color  = db.get_config(CFG_COLOR_HEX) or DEFAULT_COLOR_HEX
        footer = db.get_config(CFG_FOOTER_NAME) or DEFAULT_FOOTER

        preview = _build_announcement_embed(
            title="Preview — Official Announcement",
            body=(
                "This is what an announcement looks like with current settings.\n\n"
                "Use **/announce post** to send a real one."
            ),
            color=_parse_color(color),
            crest_url=db.get_config(CFG_CREST_URL),
            footer_text=footer,
            officer=interaction.user,
        )

        settings = discord.Embed(
            title="Announcement settings",
            color=discord.Color.blurple(),
            description=(
                f"**Crest URL:** {crest}\n"
                f"**Accent color:** `{color}`\n"
                f"**Footer text:** `{footer}`"
            ),
        )
        await interaction.response.send_message(
            embeds=[settings, preview], ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(Announcements(bot))
