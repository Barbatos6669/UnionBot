"""Low-noise channel/workflow breadcrumbs.

This cog answers the repeated "where do I..." questions that showed up in
chat history without waking the AI helper for every normal conversation.
It also exposes `/where` for members who want the same links on demand.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from cogs._typing import Bot
from debug import error_log, info_log
from utils import info_embed, is_unionbot_handled, mark_unionbot_handled


AUTO_COOLDOWN_SEC = 300
DELETE_AFTER_SEC = 600


@dataclass(frozen=True)
class BreadcrumbTopic:
    label: str
    config_keys: tuple[str, ...]
    summary: str
    detail: str


TOPICS: dict[str, BreadcrumbTopic] = {
    "register": BreadcrumbTopic(
        label="Registration",
        config_keys=("registration_channel_id",),
        summary="Link your Discord account to an Albion character.",
        detail=(
            "Click **Register**, enter your Albion character name and server, "
            "then upload the character-screen screenshot after the bot asks. "
            "If Albion's API does not show your guild yet, staff can still "
            "review you as a Guest."
        ),
    ),
    "lfg": BreadcrumbTopic(
        label="LFG / Events",
        config_keys=("lfg_board_channel_id", "lfg_channel_id"),
        summary="Create events and sign up for content.",
        detail=(
            "Use the event board to create content. Prime timer buttons are "
            "for Shotcaller+; General LFG is for custom/non-prime events. "
            "Sign up or withdraw from the event post. Some event voice "
            "channels stay hidden until you sign up."
        ),
    ),
    "bounty": BreadcrumbTopic(
        label="Bounties",
        config_keys=("bounty_board_channel_id",),
        summary="Claim guild tasks and submit proof for payout.",
        detail=(
            "Use the bounty board buttons to claim work and submit proof. "
            "Some bounties have line-item buttons so multiple members can "
            "claim separate rows. Completed bounties may still need an "
            "officer to confirm the in-game silver payment."
        ),
    ),
    "sso": BreadcrumbTopic(
        label="SSO Routes",
        config_keys=("sso_routes_channel_id",),
        summary="Post current hideout portal chains.",
        detail=(
            "Use **Add / Update Route** on the SSO route board. Add Portal 1, "
            "optional Portal 2/3, an optional note, and TTL like `2h`, `90m`, "
            "or `1h30m` so the board knows when the route goes stale."
        ),
    ),
    "market": BreadcrumbTopic(
        label="Union Market",
        config_keys=("market_autopost_channel_id",),
        summary="Read trade ideas from the arbitrage reports.",
        detail=(
            "Treat prices as buy-order and sell-order targets. The bot is "
            "not promising that instant listings will still be there when "
            "you arrive. Place orders, wait for fills, then haul only if "
            "the spread still makes sense."
        ),
    ),
}


TOPIC_CHOICES = [
    app_commands.Choice(name="Registration", value="register"),
    app_commands.Choice(name="LFG / Events", value="lfg"),
    app_commands.Choice(name="Bounties", value="bounty"),
    app_commands.Choice(name="SSO Routes", value="sso"),
    app_commands.Choice(name="Union Market", value="market"),
]

QUESTION_RE = re.compile(r"\b(where|how|what|which|sign\s*up|join|find|collect|claim)\b", re.I)
AUTO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("register", re.compile(r"\b(register|registration|verify|verification)\b", re.I)),
    ("lfg", re.compile(r"\b(lfg|event\s*board|events?|sign\s*up|signup|content)\b", re.I)),
    ("bounty", re.compile(r"\b(bounty|bounties|collect|payout|paid)\b", re.I)),
    ("sso", re.compile(r"\b(sso|route\s*board|portal\s*route|roads?\s*route)\b", re.I)),
    ("market", re.compile(r"\b(union\s*market|market|arbitrage|buy\s*order|sell\s*order)\b", re.I)),
)

NO_AUTO_CHANNEL_BITS = {
    "announcement",
    "activity-feed",
    "hall-of-fame",
    "bounty-board",
    "looking-for-group",
    "event-board",
    "rules",
    "welcome",
    "goodbye",
    "officer",
    "staff",
    "ticket",
    "archive",
}


def _config_int(db, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(db.get_config(key) or "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _topic_from_text(content: str) -> str | None:
    text = (content or "").strip()
    if not text:
        return None
    if not QUESTION_RE.search(text) and "?" not in text:
        return None
    for topic, pattern in AUTO_PATTERNS:
        if pattern.search(text):
            return topic
    return None


def _no_auto_channel(channel: discord.TextChannel | discord.Thread) -> bool:
    names = [getattr(channel, "name", "") or ""]
    parent = getattr(channel, "category", None)
    if parent is not None:
        names.append(getattr(parent, "name", "") or "")
    joined = " ".join(names).lower()
    return any(bit in joined for bit in NO_AUTO_CHANNEL_BITS)


class Breadcrumbs(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._cooldowns: dict[tuple[int, int, int, str], datetime.datetime] = {}
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def _channel_mentions(self, topic: BreadcrumbTopic) -> str:
        mentions: list[str] = []
        for key in topic.config_keys:
            raw = (self.bot.db.get_config(key) or "").strip()  # type: ignore[attr-defined]
            if raw:
                mentions.append(f"<#{raw}>")
        return " / ".join(mentions) if mentions else "_not configured yet_"

    def _embed_for_topic(self, topic_key: str) -> discord.Embed:
        topic = TOPICS.get(topic_key)
        if not topic:
            embed = info_embed("Where to go", "Key server workflows and channels.")
            for key in ("register", "lfg", "bounty", "sso", "market"):
                t = TOPICS[key]
                embed.add_field(
                    name=t.label,
                    value=f"{self._channel_mentions(t)}\n{t.summary}",
                    inline=False,
                )
            return embed

        embed = info_embed(topic.label, topic.summary)
        embed.add_field(name="Channel", value=self._channel_mentions(topic), inline=False)
        embed.add_field(name="How it works", value=topic.detail, inline=False)
        return embed

    @app_commands.command(name="where", description="Find the right server channel or workflow.")
    @app_commands.describe(topic="Optional workflow to look up.")
    @app_commands.choices(topic=TOPIC_CHOICES)
    async def where(self, interaction: discord.Interaction, topic: str | None = None) -> None:
        await interaction.response.send_message(
            embed=self._embed_for_topic(topic or ""),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if is_unionbot_handled(self.bot, message):
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        registration_id = (self.bot.db.get_config("registration_channel_id") or "").strip()  # type: ignore[attr-defined]
        if registration_id and str(message.channel.id) == registration_id:
            return
        if _no_auto_channel(message.channel):
            return

        topic = _topic_from_text(message.content or "")
        if not topic:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        cooldown = _config_int(
            self.bot.db,  # type: ignore[attr-defined]
            "breadcrumb_auto_cooldown_sec",
            AUTO_COOLDOWN_SEC,
            60,
            3600,
        )
        key = (message.guild.id, message.channel.id, message.author.id, topic)
        last = self._cooldowns.get(key)
        if last and (now - last).total_seconds() < cooldown:
            return

        delete_after = _config_int(
            self.bot.db,  # type: ignore[attr-defined]
            "breadcrumb_delete_after_sec",
            DELETE_AFTER_SEC,
            0,
            3600,
        )
        try:
            mark_unionbot_handled(self.bot, message)
            await message.reply(
                embed=self._embed_for_topic(topic),
                mention_author=False,
                delete_after=delete_after or None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self._cooldowns[key] = now
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"breadcrumb auto-reply failed: {exc!r}")


async def setup(bot: Bot) -> None:
    await bot.add_cog(Breadcrumbs(bot))
