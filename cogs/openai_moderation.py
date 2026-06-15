from __future__ import annotations

import asyncio
import datetime
import os
import re

import discord
from discord import app_commands
from discord.ext import commands

from cogs._openai_moderation import DEFAULT_MODERATION_MODEL, moderate_text
from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, info_embed, is_officer, success_embed, warning_embed


CFG_ENABLED = "openai_moderation_enabled"
CFG_MODEL = "openai_moderation_model"
CFG_REVIEW_CHANNEL_ID = "openai_moderation_review_channel_id"
CFG_SCAN_MODE = "openai_moderation_scan_mode"
CFG_COOLDOWN_SEC = "openai_moderation_cooldown_sec"
CFG_TIMEOUT_SEC = "openai_moderation_timeout_sec"
CFG_EXCLUDED_CHANNEL_IDS = "openai_moderation_excluded_channel_ids"
CFG_EXCLUDED_CATEGORY_IDS = "openai_moderation_excluded_category_ids"
CFG_EXEMPT_OFFICERS = "openai_moderation_exempt_officers"
CFG_TEST_CHANNEL_ID = "openai_moderation_test_channel_id"
CFG_ACTION = "openai_moderation_action"
CFG_ALERT_THRESHOLD = "openai_moderation_alert_threshold"
CFG_SEVERE_ALERT_THRESHOLD = "openai_moderation_severe_alert_threshold"

DEFAULT_SCAN_MODE = "all"
DEFAULT_COOLDOWN_SEC = 300
DEFAULT_TIMEOUT_SEC = 12
DEFAULT_ALERT_THRESHOLD = 0.82
DEFAULT_SEVERE_ALERT_THRESHOLD = 0.35
SCAN_MODES = {"off", "suspect", "all"}
MODERATION_ACTIONS = {"alert", "delete"}
DEFAULT_ACTION = "alert"
MODERATION_EXEMPT_ROLE_NAMES = {"Guild Leader", "Commander", "Captain", "Officer"}

SEVERE_MODERATION_CATEGORIES = {
    "hate/threatening",
    "harassment/threatening",
    "self-harm/intent",
    "self-harm/instructions",
    "sexual/minors",
    "violence/graphic",
}

PRIVATE_CHANNEL_KEYWORDS = (
    "admin",
    "application",
    "audit",
    "command",
    "leadership",
    "log",
    "moderation",
    "officer",
    "review",
    "staff",
    "ticket",
)

NOISY_CHANNEL_KEYWORDS = (
    "activity-feed",
    "announcement",
    "announcements",
    "audit",
    "bot",
    "bounty-board",
    "dashboard",
    "feed",
    "graph",
    "hall-of-fame",
    "kill-bot",
    "leaderboard",
    "log",
    "logs",
    "market",
    "points",
    "prime-time",
    "rules",
    "treasury",
    "utc",
)

SUSPECT_HINTS = re.compile(
    r"\b("
    r"kys|kill yourself|rape|dox|doxx|doxxing|suicide|self[- ]?harm|"
    r"racist|homophobic|slur|nazi|terrorist|threat|threaten|"
    r"shoot|stab|murder|hang|bomb"
    r")\b",
    re.IGNORECASE,
)

GAME_VIOLENCE_CATEGORIES = {"violence", "violence/graphic"}

ALBION_COMBAT_HINTS = re.compile(
    r"\b("
    r"albion|avalon|avalonian|ava|black zone|blackzone|bz|red zone|redzone|rz|yellow zone|yz|"
    r"martlock|fort sterling|lymhurst|bridgewatch|thetford|caerleon|brecilien|"
    r"faction|outpost|castle|territory|hideout|ho|zvz|small scale|hellgate|corrupted|mists|"
    r"roads|gank|ganking|ganked|clap|bomb|bombing|execute|dismount|dismounted|downed|"
    r"fight|pvp|pk|kill fame|death fame|loot|regear|comp|ip|spec|shotcaller|engage|"
    r"clump|pierce|purge|defensives|healer|tank|dps|caller|roam|roaming|static|dungeon"
    r")\b",
    re.IGNORECASE,
)

REAL_WORLD_THREAT_HINTS = re.compile(
    r"\b("
    r"irl|real life|your house|your home|home address|address|dox|doxx|doxxing|"
    r"school|workplace|family|wife|husband|kid|kids|children|"
    r"find you|come to you|come over|pull up|show up|"
    r"kill yourself|kys|suicide|self[- ]?harm|shoot up|stab you|murder you|hang you"
    r")\b",
    re.IGNORECASE,
)


def _bool_config(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _int_config(raw: str | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _float_config(raw: str | None, *, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _id_set(raw: str | None) -> set[int]:
    ids: set[int] = set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


def _clean_text(value: str, *, limit: int) -> str:
    value = " ".join(str(value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _category_for(channel: discord.abc.GuildChannel | discord.Thread) -> discord.CategoryChannel | None:
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return parent.category if isinstance(parent, discord.TextChannel) else None
    return channel.category if isinstance(channel, discord.TextChannel) else None


def _name_blob(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    names = [getattr(channel, "name", "")]
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        names.append(getattr(parent, "name", ""))
        if isinstance(parent, discord.TextChannel) and parent.category:
            names.append(parent.category.name)
    elif isinstance(channel, discord.TextChannel) and channel.category:
        names.append(channel.category.name)
    return " ".join(names).lower()


def _is_privateish(channel: discord.abc.GuildChannel | discord.Thread) -> bool:
    blob = _name_blob(channel)
    return any(keyword in blob for keyword in PRIVATE_CHANNEL_KEYWORDS)


def _is_noisy(channel: discord.abc.GuildChannel | discord.Thread) -> bool:
    blob = _name_blob(channel)
    return any(keyword in blob for keyword in NOISY_CHANNEL_KEYWORDS)


def _is_moderation_exempt(member: discord.Member) -> bool:
    """Only core moderation leadership bypasses public moderation scanning.

    The shared `is_officer` helper includes operational jobs like Recruiter,
    Logistician, Crafter, Refiner, and Gatherer. Those roles should still be
    scanned in normal public channels, otherwise too many members bypass the
    moderation safety net.
    """
    if member.guild_permissions.manage_guild:
        return True
    role_names = {role.name for role in member.roles}
    return bool(role_names & MODERATION_EXEMPT_ROLE_NAMES)


def _is_albion_game_violence(
    *,
    content: str,
    channel: discord.abc.GuildChannel | discord.Thread,
    categories: list[str],
) -> bool:
    """Return True for moderation flags that are only Albion combat language.

    Open-world Albion chat naturally contains words like "kill", "gank",
    "bomb", "execute", and "clap". Those should not be deleted just because
    the moderation endpoint marks the text as violent. We only suppress flags
    when every flagged category is violence-related and the text/channel clearly
    looks like game context. Anything involving harassment, self-harm, sexual
    content, hate, or a real-world threat still goes to officers.
    """
    if not categories:
        return False
    if any(category not in GAME_VIOLENCE_CATEGORIES for category in categories):
        return False
    if REAL_WORLD_THREAT_HINTS.search(content or ""):
        return False
    blob = f"{content or ''} {_name_blob(channel)}"
    return bool(ALBION_COMBAT_HINTS.search(blob))


def _category_score(category_scores: dict[str, float], category: str) -> float:
    return float(category_scores.get(category, 0.0) or 0.0)


def _moderation_threshold_decision(
    *,
    categories: list[str],
    category_scores: dict[str, float],
    alert_threshold: float,
    severe_alert_threshold: float,
) -> tuple[bool, str]:
    """Return whether a flagged result is strong enough to alert officers.

    OpenAI's `flagged` boolean is intentionally conservative. In Albion chat,
    that catches too much ordinary profanity and combat language, so the public
    moderation cog applies a second score gate. Severe categories keep a lower
    gate so true threats/self-harm/minors/threatening hate are still escalated.
    """
    if not categories:
        return False, "no flagged categories"
    severe_scores = {
        category: _category_score(category_scores, category)
        for category in categories
        if category in SEVERE_MODERATION_CATEGORIES
    }
    if severe_scores:
        category, score = max(severe_scores.items(), key=lambda item: item[1])
        if score >= severe_alert_threshold:
            return True, f"severe category `{category}` score {score:.3f} >= {severe_alert_threshold:.3f}"
    scored = {
        category: _category_score(category_scores, category)
        for category in categories
    }
    category, score = max(scored.items(), key=lambda item: item[1])
    if score >= alert_threshold:
        return True, f"category `{category}` score {score:.3f} >= {alert_threshold:.3f}"
    return False, f"top flagged score {score:.3f} below threshold {alert_threshold:.3f}"


def _alert_field(embed: discord.Embed, name: str) -> str | None:
    target = name.strip().lower()
    for field in embed.fields:
        if str(field.name).strip().lower() == target:
            return str(field.value)
    return None


def _with_decision(
    embed: discord.Embed,
    *,
    title: str,
    body: str,
) -> discord.Embed:
    updated = embed.copy()
    field_name = "Officer decision"
    value = _clean_text(f"**{title}**\n{body}", limit=1024)
    for index, field in enumerate(updated.fields):
        if str(field.name).strip().lower() == field_name.lower():
            updated.set_field_at(index, name=field_name, value=value, inline=False)
            return updated
    updated.add_field(name=field_name, value=value, inline=False)
    return updated


def _moderation_button_id(
    action: str,
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    user_id: int,
) -> str:
    return f"modai:{action}:{guild_id}:{channel_id}:{message_id}:{user_id}"


async def _fetch_reviewed_message(
    bot: Bot,
    *,
    channel_id: int,
    message_id: int,
) -> tuple[discord.Message | None, str]:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None, "channel_missing"
        except discord.Forbidden:
            return None, "channel_forbidden"
        except discord.HTTPException:
            return None, "channel_fetch_failed"
    if not hasattr(channel, "fetch_message"):
        return None, "channel_not_readable"
    try:
        message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        return message, "found"
    except discord.NotFound:
        return None, "already_deleted"
    except discord.Forbidden:
        return None, "message_forbidden"
    except discord.HTTPException:
        return None, "message_fetch_failed"


async def _warn_reviewed_user(
    bot: Bot,
    *,
    user_id: int,
    source_channel_id: int,
    alert_embed: discord.Embed | None,
) -> tuple[bool, str]:
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException):
        return False, "user_missing"

    excerpt = _alert_field(alert_embed, "Message") if alert_embed else None
    embed = warning_embed(
        "Officer warning",
        (
            "One of your recent messages was reviewed by staff. Keep chat inside "
            "server rules and avoid real-world threats, slurs, harassment, or "
            "anything that makes the server unsafe."
        ),
    )
    embed.add_field(name="Channel", value=f"<#{source_channel_id}>", inline=True)
    if excerpt:
        embed.add_field(name="Message reviewed", value=excerpt[:900], inline=False)
    embed.set_footer(text="If this was a misunderstanding, please ask an officer.")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        return False, "dm_closed"
    except discord.HTTPException:
        return False, "dm_failed"
    return True, "sent"


async def _finish_moderation_action(
    interaction: discord.Interaction,
    *,
    title: str,
    body: str,
) -> None:
    embed = None
    if interaction.message and interaction.message.embeds:
        embed = _with_decision(interaction.message.embeds[0], title=title, body=body)
    if interaction.message:
        try:
            await interaction.message.edit(embed=embed, view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"openai_moderation: alert resolve edit failed: {exc!r}")


class ModerationActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=(
        r"modai:(?P<action>ignore|cleanup|warn):"
        r"(?P<guild_id>\d+):(?P<channel_id>\d+):"
        r"(?P<message_id>\d+):(?P<user_id>\d+)"
    ),
):
    """Restart-safe officer action button for one moderation alert."""

    _LABELS = {
        "ignore": "Ignore",
        "cleanup": "Clean up",
        "warn": "Warn",
    }
    _STYLES = {
        "ignore": discord.ButtonStyle.secondary,
        "cleanup": discord.ButtonStyle.danger,
        "warn": discord.ButtonStyle.primary,
    }

    def __init__(
        self,
        *,
        action: str,
        guild_id: int,
        channel_id: int,
        message_id: int,
        user_id: int,
    ) -> None:
        self.action = action
        self.guild_id = int(guild_id)
        self.channel_id = int(channel_id)
        self.message_id = int(message_id)
        self.user_id = int(user_id)
        super().__init__(
            discord.ui.Button(
                label=self._LABELS[action],
                style=self._STYLES[action],
                custom_id=_moderation_button_id(
                    action,
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    message_id=self.message_id,
                    user_id=self.user_id,
                ),
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> ModerationActionButton:
        return cls(
            action=match.group("action"),
            guild_id=int(match.group("guild_id")),
            channel_id=int(match.group("channel_id")),
            message_id=int(match.group("message_id")),
            user_id=int(match.group("user_id")),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officer-only", "Only staff can use moderation actions."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=False)
        bot: Bot = interaction.client  # type: ignore[assignment]
        alert_embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None

        if self.action == "ignore":
            body = f"Ignored by {interaction.user.mention}."
            await _finish_moderation_action(interaction, title="Ignored", body=body)
            await interaction.followup.send(
                embed=success_embed("Ignored", "Moderation alert closed with no action."),
                ephemeral=True,
            )
            info_log(
                f"openai_moderation: alert ignored by={interaction.user.id} "
                f"message={self.message_id} user={self.user_id}"
            )
            return

        if self.action == "cleanup":
            source_message, status = await _fetch_reviewed_message(
                bot,
                channel_id=self.channel_id,
                message_id=self.message_id,
            )
            if source_message is not None:
                try:
                    await source_message.delete(
                        reason=f"AI moderation cleanup by {interaction.user}",
                    )
                    status = "deleted"
                except discord.NotFound:
                    status = "already_deleted"
                except discord.Forbidden:
                    await interaction.followup.send(
                        embed=error_embed(
                            "Missing permission",
                            "I cannot delete that message in the source channel.",
                        ),
                        ephemeral=True,
                    )
                    return
                except discord.HTTPException as exc:
                    error_log(f"openai_moderation cleanup button failed: {exc!r}")
                    await interaction.followup.send(
                        embed=error_embed("Cleanup failed", "Discord rejected the delete request."),
                        ephemeral=True,
                    )
                    return
            title = "Cleaned up" if status == "deleted" else "Already cleaned up"
            body = f"{title} by {interaction.user.mention}. Source status: `{status}`."
            await _finish_moderation_action(interaction, title=title, body=body)
            await interaction.followup.send(
                embed=success_embed(title, f"Source message status: `{status}`."),
                ephemeral=True,
            )
            info_log(
                f"openai_moderation: cleanup action by={interaction.user.id} "
                f"message={self.message_id} user={self.user_id} status={status}"
            )
            return

        ok, status = await _warn_reviewed_user(
            bot,
            user_id=self.user_id,
            source_channel_id=self.channel_id,
            alert_embed=alert_embed,
        )
        if not ok:
            await interaction.followup.send(
                embed=error_embed(
                    "Warning not sent",
                    f"I could not DM the member. Status: `{status}`.",
                ),
                ephemeral=True,
            )
            return
        body = f"Warned by {interaction.user.mention}. DM status: `{status}`."
        await _finish_moderation_action(interaction, title="Warned", body=body)
        await interaction.followup.send(
            embed=success_embed("Warned", "The member was sent a private warning."),
            ephemeral=True,
        )
        info_log(
            f"openai_moderation: warning action by={interaction.user.id} "
            f"message={self.message_id} user={self.user_id}"
        )


class ModerationAlertView(discord.ui.View):
    """Buttons attached to each moderation alert in the officer channel."""

    def __init__(self, message: discord.Message) -> None:
        super().__init__(timeout=None)
        guild_id = message.guild.id if message.guild else 0
        for action in ("ignore", "cleanup", "warn"):
            self.add_item(
                ModerationActionButton(
                    action=action,
                    guild_id=guild_id,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    user_id=message.author.id,
                )
            )


class OpenAIModeration(commands.Cog):
    """Quiet OpenAI moderation alerts for public guild chat."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._cooldowns: dict[tuple[int, str], datetime.datetime] = {}
        self._last_missing_key_log: datetime.datetime | None = None
        self._semaphore = asyncio.Semaphore(3)
        self.bot.add_dynamic_items(ModerationActionButton)
        self.bot.tree.add_command(ModerationGroup(bot, self))
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command("mod-ai")
        except Exception:  # noqa: BLE001
            pass

    def _enabled(self) -> bool:
        return _bool_config(self.bot.db.get_config(CFG_ENABLED), default=True)

    def _exempt_officers(self) -> bool:
        return _bool_config(self.bot.db.get_config(CFG_EXEMPT_OFFICERS), default=True)

    def _model(self) -> str:
        return (self.bot.db.get_config(CFG_MODEL) or DEFAULT_MODERATION_MODEL).strip()

    def _scan_mode(self) -> str:
        mode = (self.bot.db.get_config(CFG_SCAN_MODE) or DEFAULT_SCAN_MODE).strip().lower()
        return mode if mode in SCAN_MODES else DEFAULT_SCAN_MODE

    def _action(self) -> str:
        action = (self.bot.db.get_config(CFG_ACTION) or DEFAULT_ACTION).strip().lower()
        return action if action in MODERATION_ACTIONS else DEFAULT_ACTION

    def _cooldown_sec(self) -> int:
        return _int_config(
            self.bot.db.get_config(CFG_COOLDOWN_SEC),
            default=DEFAULT_COOLDOWN_SEC,
            minimum=30,
            maximum=86400,
        )

    def _timeout_sec(self) -> int:
        return _int_config(
            self.bot.db.get_config(CFG_TIMEOUT_SEC),
            default=DEFAULT_TIMEOUT_SEC,
            minimum=5,
            maximum=60,
        )

    def _alert_threshold(self) -> float:
        return _float_config(
            self.bot.db.get_config(CFG_ALERT_THRESHOLD),
            default=DEFAULT_ALERT_THRESHOLD,
            minimum=0.05,
            maximum=0.99,
        )

    def _severe_alert_threshold(self) -> float:
        return _float_config(
            self.bot.db.get_config(CFG_SEVERE_ALERT_THRESHOLD),
            default=DEFAULT_SEVERE_ALERT_THRESHOLD,
            minimum=0.01,
            maximum=0.95,
        )

    def _api_key(self) -> str:
        return (os.getenv("OPENAI_API_KEY") or "").strip()

    def _base_url(self) -> str:
        return (self.bot.db.get_config("ai_assistant_openai_base_url") or "https://api.openai.com/v1").strip()

    def _review_channel_id(self) -> int | None:
        raw = (
            self.bot.db.get_config(CFG_REVIEW_CHANNEL_ID)
            or self.bot.db.get_config("automation_officer_channel_id")
            or self.bot.db.get_config("officer_channel_id")
            or ""
        ).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _test_channel_id(self) -> int | None:
        raw = (self.bot.db.get_config(CFG_TEST_CHANNEL_ID) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    async def _review_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self._review_channel_id()
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.Forbidden, discord.HTTPException):
                return None
        return channel if isinstance(channel, discord.TextChannel) else None

    def _should_scan(self, message: discord.Message) -> bool:
        if not self._enabled() or self._scan_mode() == "off":
            return False
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return False
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False
        if not isinstance(message.author, discord.Member):
            return False
        in_test_channel = bool(self._test_channel_id() and message.channel.id == self._test_channel_id())
        if _is_moderation_exempt(message.author) and self._exempt_officers() and not in_test_channel:
            return False
        if not in_test_channel and (_is_privateish(message.channel) or _is_noisy(message.channel)):
            return False

        category = _category_for(message.channel)
        excluded_channels = _id_set(self.bot.db.get_config(CFG_EXCLUDED_CHANNEL_IDS))
        excluded_categories = _id_set(self.bot.db.get_config(CFG_EXCLUDED_CATEGORY_IDS))
        if message.channel.id in excluded_channels:
            return False
        if category and category.id in excluded_categories:
            return False

        content = _clean_text(message.content or "", limit=2000)
        if len(content) < 4:
            return False
        if self._scan_mode() == "suspect" and not SUSPECT_HINTS.search(content):
            return False
        return True

    def _cooldown_key(self, user_id: int, categories: list[str]) -> tuple[int, str]:
        label = ",".join(categories[:3]) if categories else "flagged"
        return user_id, label

    def _cooldown_active(self, key: tuple[int, str]) -> bool:
        last = self._cooldowns.get(key)
        if not last:
            return False
        age = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
        return age < self._cooldown_sec()

    def _mark_cooldown(self, key: tuple[int, str]) -> None:
        self._cooldowns[key] = datetime.datetime.now(datetime.timezone.utc)

    async def _send_alert(
        self,
        message: discord.Message,
        categories: list[str],
        top_scores: list[tuple[str, float]],
        *,
        action: str,
    ) -> None:
        if message.guild is None:
            return
        channel = await self._review_channel(message.guild)
        if channel is None:
            error_log("openai_moderation: flagged message but no review channel is configured.")
            return

        score_lines = "\n".join(
            f"`{name}`: {score:.3f}"
            for name, score in top_scores[:5]
        ) or "No category scores returned."
        category_text = ", ".join(f"`{name}`" for name in categories) or "`flagged`"
        content = _clean_text(message.content or "", limit=900)

        embed = warning_embed(
            "OpenAI moderation flag",
            "A public message was flagged for officer review.",
        )
        embed.add_field(name="Member", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
        embed.add_field(name="Channel", value=getattr(message.channel, "mention", f"`{message.channel.id}`"), inline=True)
        embed.add_field(name="Categories", value=category_text, inline=True)
        embed.add_field(
            name="Action",
            value=(
                "`delete` - message will be removed from the public channel."
                if action == "delete"
                else "`alert` - no automatic cleanup was applied."
            ),
            inline=False,
        )
        embed.add_field(name="Top scores", value=score_lines, inline=False)
        embed.add_field(name="Message", value=content or "_(no text)_", inline=False)
        embed.add_field(name="Jump", value=f"[Open message]({message.jump_url})", inline=False)
        embed.set_footer(text=f"OpenAI moderation endpoint · action={action}")

        alert_text = (
            f"⚠️ AI moderation flag in {getattr(message.channel, 'mention', f'`{message.channel.id}`')} "
            f"from **{discord.utils.escape_markdown(message.author.display_name)}**"
            f"{' · cleaning up public message' if action == 'delete' else ''}."
        )
        try:
            await channel.send(
                content=alert_text,
                embed=embed,
                view=ModerationAlertView(message),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(
                f"openai_moderation: failed to send alert to channel={channel.id}: {exc!r}"
            )
            return
        info_log(
            f"openai_moderation: flagged user={message.author.id} "
            f"channel={message.channel.id} categories={categories}"
        )

    async def _delete_flagged_message(self, message: discord.Message, categories: list[str]) -> str:
        reason = _clean_text(
            f"OpenAI moderation flag: {', '.join(categories[:3]) if categories else 'flagged'}",
            limit=120,
        )
        try:
            await message.delete(reason=reason)
            info_log(
                f"openai_moderation: deleted flagged message user={message.author.id} "
                f"channel={message.channel.id} message={message.id}"
            )
            return "deleted"
        except discord.NotFound:
            return "already_deleted"
        except discord.Forbidden as exc:
            error_log(
                f"openai_moderation: cannot delete flagged message channel={message.channel.id} "
                f"message={message.id}: {exc!r}"
            )
            return "missing_permission"
        except discord.HTTPException as exc:
            error_log(
                f"openai_moderation: failed to delete flagged message channel={message.channel.id} "
                f"message={message.id}: {exc!r}"
            )
            return "failed"

    def _log_missing_api_key_once(self) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        if (
            self._last_missing_key_log
            and (now - self._last_missing_key_log).total_seconds() < 600
        ):
            return
        self._last_missing_key_log = now
        error_log("openai_moderation: OPENAI_API_KEY is missing; skipping moderation scans.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._should_scan(message):
            return
        api_key = self._api_key()
        if not api_key:
            self._log_missing_api_key_once()
            return
        try:
            async with self._semaphore:
                result = await moderate_text(
                    api_key=api_key,
                    text=_clean_text(message.content or "", limit=2000),
                    model=self._model(),
                    base_url=self._base_url(),
                    timeout_sec=self._timeout_sec(),
                )
        except Exception as exc:  # noqa: BLE001
            error_log(f"openai_moderation: moderation request failed: {exc!r}")
            return

        if not result.flagged:
            return
        categories = result.flagged_categories
        content = _clean_text(message.content or "", limit=2000)
        if _is_albion_game_violence(
            content=content,
            channel=message.channel,
            categories=categories,
        ):
            info_log(
                f"openai_moderation: ignored Albion combat language user={message.author.id} "
                f"channel={message.channel.id} categories={categories}"
            )
            return
        should_alert, threshold_reason = _moderation_threshold_decision(
            categories=categories,
            category_scores=result.category_scores,
            alert_threshold=self._alert_threshold(),
            severe_alert_threshold=self._severe_alert_threshold(),
        )
        if not should_alert:
            info_log(
                f"openai_moderation: ignored low-confidence flag user={message.author.id} "
                f"channel={message.channel.id} categories={categories} reason={threshold_reason}"
            )
            return
        action = self._action()
        key = self._cooldown_key(message.author.id, categories)
        if self._cooldown_active(key):
            cleanup_status = "not_configured"
            if action == "delete":
                cleanup_status = await self._delete_flagged_message(message, categories)
            info_log(
                f"openai_moderation: suppressed duplicate alert user={message.author.id} "
                f"channel={message.channel.id} categories={categories} cooldown={self._cooldown_sec()}s "
                f"cleanup={cleanup_status}"
            )
            return
        self._mark_cooldown(key)
        await self._send_alert(message, categories, result.top_scores, action=action)
        if action == "delete":
            await self._delete_flagged_message(message, categories)


class ModerationGroup(app_commands.Group, name="mod-ai", description="OpenAI moderation controls."):
    def __init__(self, bot: Bot, cog: OpenAIModeration):
        super().__init__(default_permissions=discord.Permissions(manage_guild=True))
        self.bot = bot
        self.cog = cog

    async def _require_officer(self, interaction: discord.Interaction) -> bool:
        if is_officer(interaction.user):
            return True
        await interaction.response.send_message(
            embed=error_embed("Officers only", "Only staff can configure AI moderation."),
            ephemeral=True,
        )
        return False

    @app_commands.command(name="status", description="Show OpenAI moderation status.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._require_officer(interaction):
            return
        channel_id = self.cog._review_channel_id()
        review_channel = f"<#{channel_id}>" if channel_id else "not configured"
        test_channel_id = self.cog._test_channel_id()
        test_channel = f"<#{test_channel_id}>" if test_channel_id else "not configured"
        key_state = "set" if self.cog._api_key() else "missing"
        embed = info_embed(
            "OpenAI moderation status",
            "\n".join([
                f"Enabled: **{'yes' if self.cog._enabled() else 'no'}**",
                f"Scan mode: **{self.cog._scan_mode()}**",
                f"Model: `{self.cog._model()}`",
                f"OpenAI key: **{key_state}**",
                f"Review channel: {review_channel}",
                f"Test channel: {test_channel}",
                f"Officer exemption: **{'on' if self.cog._exempt_officers() else 'off'}**",
                f"Alert cooldown: **{self.cog._cooldown_sec()}s/user/category**",
                f"Normal alert threshold: **{self.cog._alert_threshold():.2f}**",
                f"Severe alert threshold: **{self.cog._severe_alert_threshold():.2f}**",
                f"Action: **{self.cog._action()}**",
            ]),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="enable", description="Enable AI moderation alerts.")
    async def enable(self, interaction: discord.Interaction) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_ENABLED, "1")
        await interaction.response.send_message(
            embed=success_embed("AI moderation enabled", "Flagged public messages will alert the review channel."),
            ephemeral=True,
        )

    @app_commands.command(name="disable", description="Disable AI moderation alerts.")
    async def disable(self, interaction: discord.Interaction) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_ENABLED, "0")
        await interaction.response.send_message(
            embed=success_embed("AI moderation disabled", "No messages will be sent to OpenAI moderation."),
            ephemeral=True,
        )

    @app_commands.command(name="set-action", description="Choose what happens after a public message is flagged.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Alert only", value="alert"),
        app_commands.Choice(name="Alert and delete public message", value="delete"),
    ])
    async def set_action(self, interaction: discord.Interaction, action: app_commands.Choice[str]) -> None:
        if not await self._require_officer(interaction):
            return
        clean = action.value if action.value in MODERATION_ACTIONS else DEFAULT_ACTION
        self.bot.db.set_config(CFG_ACTION, clean)
        await interaction.response.send_message(
            embed=success_embed(
                "AI moderation action set",
                "Flagged messages will be removed after the officer alert."
                if clean == "delete"
                else "Flagged messages will only alert officers.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set-review-channel", description="Set where AI moderation alerts go.")
    async def set_review_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_REVIEW_CHANNEL_ID, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed("AI moderation review channel set", f"Alerts will go to {channel.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="set-test-channel", description="Set a private channel where officers can test live moderation.")
    async def set_test_channel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_TEST_CHANNEL_ID, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "AI moderation test channel set",
                f"Officer messages in {channel.mention} can trigger moderation alerts for testing.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="clear-test-channel", description="Clear the private moderation test channel.")
    async def clear_test_channel(self, interaction: discord.Interaction) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_TEST_CHANNEL_ID, "")
        await interaction.response.send_message(
            embed=success_embed("AI moderation test channel cleared", "Officer messages are exempt everywhere again."),
            ephemeral=True,
        )

    @app_commands.command(name="set-officer-exempt", description="Choose whether officer messages are ignored by AI moderation.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="On - exempt officers", value="1"),
        app_commands.Choice(name="Off - scan officers too", value="0"),
    ])
    async def set_officer_exempt(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_EXEMPT_OFFICERS, mode.value)
        await interaction.response.send_message(
            embed=success_embed(
                "Officer exemption updated",
                "Officer messages are exempt from public moderation."
                if mode.value == "1"
                else "Officer messages can now trigger moderation alerts too.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set-scan-mode", description="Choose how much public chat AI moderation scans.")
    @app_commands.describe(mode="all: scan eligible public text; suspect: only obvious risk hints; off: no scanning.")
    @app_commands.choices(mode=[
        app_commands.Choice(name="All eligible public messages", value="all"),
        app_commands.Choice(name="Suspect hints only", value="suspect"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def set_scan_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        if not await self._require_officer(interaction):
            return
        clean = mode.value if mode.value in SCAN_MODES else DEFAULT_SCAN_MODE
        self.bot.db.set_config(CFG_SCAN_MODE, clean)
        await interaction.response.send_message(
            embed=success_embed("AI moderation scan mode set", f"Scan mode is now **{clean}**."),
            ephemeral=True,
        )

    @app_commands.command(name="set-thresholds", description="Tune how confident AI moderation must be before alerting.")
    @app_commands.describe(
        normal="Normal category score needed for an alert. Higher = fewer cussing/soft alerts. Suggested 0.75-0.90.",
        severe="Severe category score needed for threats/self-harm/minors. Suggested 0.25-0.45.",
    )
    async def set_thresholds(
        self,
        interaction: discord.Interaction,
        normal: app_commands.Range[float, 0.05, 0.99],
        severe: app_commands.Range[float, 0.01, 0.95],
    ) -> None:
        if not await self._require_officer(interaction):
            return
        self.bot.db.set_config(CFG_ALERT_THRESHOLD, f"{float(normal):.3f}")
        self.bot.db.set_config(CFG_SEVERE_ALERT_THRESHOLD, f"{float(severe):.3f}")
        await interaction.response.send_message(
            embed=success_embed(
                "AI moderation thresholds updated",
                "\n".join([
                    f"Normal alerts now require score **{float(normal):.2f}**.",
                    f"Severe alerts now require score **{float(severe):.2f}**.",
                    "Higher normal threshold means less noise from cussing and low-confidence flags.",
                ]),
            ),
            ephemeral=True,
        )

    @app_commands.command(name="test", description="Privately test text against OpenAI moderation.")
    async def test(self, interaction: discord.Interaction, text: str) -> None:
        if not await self._require_officer(interaction):
            return
        api_key = self.cog._api_key()
        if not api_key:
            await interaction.response.send_message(
                embed=error_embed("OpenAI key missing", "`OPENAI_API_KEY` is not set on the bot host."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await moderate_text(
                api_key=api_key,
                text=text,
                model=self.cog._model(),
                base_url=self.cog._base_url(),
                timeout_sec=self.cog._timeout_sec(),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"/mod-ai test failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed("Moderation failed", "OpenAI moderation did not answer. Check logs/API key."),
                ephemeral=True,
            )
            return
        categories = ", ".join(result.flagged_categories) or "none"
        scores = "\n".join(f"`{name}`: {score:.3f}" for name, score in result.top_scores[:8])
        await interaction.followup.send(
            embed=info_embed(
                "Moderation test",
                f"Flagged: **{'yes' if result.flagged else 'no'}**\n"
                f"Categories: `{categories}`\n\n"
                f"{scores or 'No scores returned.'}",
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(OpenAIModeration(bot))
