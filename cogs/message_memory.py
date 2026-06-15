from __future__ import annotations

import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs._typing import Bot
from debug import error_log, info_log
from utils import info_embed


CFG_ENABLED = "message_memory_enabled"
CFG_RETENTION_DAYS = "message_memory_retention_days"
CFG_MAX_CHARS = "message_memory_max_chars"
CFG_EXCLUDED_CHANNEL_IDS = "message_memory_excluded_channel_ids"
CFG_EXCLUDED_CATEGORY_IDS = "message_memory_excluded_category_ids"
CFG_INCLUDE_BOT_MESSAGES = "message_memory_include_bot_messages"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_CHARS = 1200

# Default privacy guardrails. Admins can still fetch context from archived
# channels, so we avoid collecting obvious staff/private zones by default.
EXCLUDED_NAME_KEYWORDS = (
    "admin",
    "application",
    "applications",
    "audit",
    "command",
    "leadership",
    "log",
    "logs",
    "mod",
    "moderation",
    "officer",
    "review",
    "staff",
    "ticket",
    "tickets",
)


def _config_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _config_int(raw: str | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw).strip())
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


def _has_excluded_keyword(*names: str | None) -> bool:
    joined = " ".join(n or "" for n in names).lower()
    return any(keyword in joined for keyword in EXCLUDED_NAME_KEYWORDS)


def _category_for_channel(channel: discord.abc.GuildChannel | discord.Thread) -> discord.CategoryChannel | None:
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return parent.category if isinstance(parent, discord.TextChannel) else None
    return channel.category if isinstance(channel, discord.TextChannel) else None


def _channel_name(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        if isinstance(parent, discord.TextChannel):
            return f"{parent.name} / {channel.name}"
    return getattr(channel, "name", str(channel))


def _time_label(value: str | None) -> str:
    if not value:
        return "??:??"
    try:
        parsed = datetime.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc).strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16]


def _context_lines(rows: list[dict]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        author = row.get("author_name") or row.get("author_id") or "Unknown"
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            content = "_(no text)_"
        if len(content) > 220:
            content = content[:217].rstrip() + "..."
        if int(row.get("attachment_count") or 0):
            content += f" [attachments: {int(row.get('attachment_count') or 0)}]"
        if row.get("is_bot"):
            author = f"{author} [bot]"
        jump = row.get("jump_url") or ""
        suffix = f" [jump]({jump})" if jump else ""
        lines.append(f"`{_time_label(row.get('created_at'))}` **{author}:** {content}{suffix}")
    return lines


@app_commands.default_permissions(administrator=True)
class MemoryGroup(app_commands.Group, name="memory", description="Recent message memory and context tools."):
    def __init__(self, bot: Bot):
        super().__init__()
        self.bot = bot

    @app_commands.command(name="context", description="Show recent archived messages from a channel.")
    @app_commands.describe(
        channel="Channel to read recent archived context from.",
        limit="How many messages to show. Default 25.",
        member="Optional: only show messages from this member.",
        search="Optional: only show messages containing this text.",
        include_bots="Include archived UnionBot messages too.",
    )
    async def context(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        limit: app_commands.Range[int, 5, 50] = 25,
        member: discord.Member | None = None,
        search: str | None = None,
        include_bots: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        rows = self.bot.db.fetch_message_context(
            channel_id=str(channel.id),
            limit=int(limit),
            author_id=str(member.id) if member else None,
            search=search,
            include_bots=include_bots,
        )
        if not rows:
            await interaction.followup.send(
                embed=info_embed(
                    "No archived context",
                    "No matching messages are in the rolling archive yet. "
                    "The archive only contains messages seen after message memory was enabled.",
                ),
                ephemeral=True,
            )
            return

        title = f"Recent Context: #{channel.name}"
        if member:
            title += f" · {member.display_name}"
        if search:
            title += f" · search: {search[:30]}"

        chunks: list[list[str]] = [[]]
        chunk_chars = 0
        for line in _context_lines(rows):
            if chunk_chars + len(line) > 3500 and chunks[-1]:
                chunks.append([])
                chunk_chars = 0
            chunks[-1].append(line)
            chunk_chars += len(line) + 1

        for index, chunk in enumerate(chunks):
            embed = info_embed(
                f"{title} ({index + 1}/{len(chunks)})" if len(chunks) > 1 else title,
                "\n".join(chunk),
            )
            embed.set_footer(text=f"{len(rows)} archived message(s) shown · UTC timestamps")
            await interaction.followup.send(embed=embed, ephemeral=True)

        info_log(
            f"{interaction.user} ran /memory context channel={channel.id} "
            f"limit={limit} member={getattr(member, 'id', None)} search={search!r} "
            f"include_bots={include_bots}"
        )


class MessageMemory(commands.Cog):
    """Rolling recent-message archive for staff troubleshooting/context."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.bot.tree.add_command(MemoryGroup(bot))
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        self.cleanup_message_archive.start()

    def cog_unload(self) -> None:
        self.cleanup_message_archive.cancel()
        try:
            self.bot.tree.remove_command("memory")
        except Exception:  # noqa: BLE001
            pass

    def _enabled(self) -> bool:
        return _config_bool(self.bot.db.get_config(CFG_ENABLED), default=True)

    def _retention_days(self) -> int:
        return _config_int(
            self.bot.db.get_config(CFG_RETENTION_DAYS),
            default=DEFAULT_RETENTION_DAYS,
            minimum=1,
            maximum=365,
        )

    def _max_chars(self) -> int:
        return _config_int(
            self.bot.db.get_config(CFG_MAX_CHARS),
            default=DEFAULT_MAX_CHARS,
            minimum=120,
            maximum=4000,
        )

    def _include_bot_messages(self) -> bool:
        return _config_bool(self.bot.db.get_config(CFG_INCLUDE_BOT_MESSAGES), default=True)

    def _should_archive(self, message: discord.Message) -> bool:
        if not self._enabled():
            return False
        if message.guild is None or message.webhook_id is not None:
            return False
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False

        if message.author.bot:
            bot_user = self.bot.user
            if not self._include_bot_messages() or not bot_user or message.author.id != bot_user.id:
                return False

        category = _category_for_channel(message.channel)
        channel_ids = _id_set(self.bot.db.get_config(CFG_EXCLUDED_CHANNEL_IDS))
        category_ids = _id_set(self.bot.db.get_config(CFG_EXCLUDED_CATEGORY_IDS))
        if message.channel.id in channel_ids:
            return False
        if category and category.id in category_ids:
            return False

        category_name = category.name if category else None
        if _has_excluded_keyword(_channel_name(message.channel), category_name):
            return False
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._should_archive(message):
            return

        max_chars = self._max_chars()
        content = (message.content or "")[:max_chars]
        category = _category_for_channel(message.channel)
        attachment_names = "\n".join(
            f"{a.filename or 'attachment'} ({a.content_type or 'unknown'})"
            for a in message.attachments[:8]
        ) or None
        created_at = message.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=datetime.timezone.utc)

        try:
            self.bot.db.archive_message(
                guild_id=str(message.guild.id),
                channel_id=str(message.channel.id),
                message_id=str(message.id),
                author_id=str(message.author.id),
                author_name=getattr(message.author, "display_name", str(message.author)),
                channel_name=_channel_name(message.channel),
                category_name=category.name if category else None,
                content=content,
                attachment_count=len(message.attachments),
                attachment_names=attachment_names,
                jump_url=message.jump_url,
                is_bot=message.author.bot,
                created_at=created_at.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"message memory archive failed: {exc!r}")

    @tasks.loop(hours=6)
    async def cleanup_message_archive(self) -> None:
        if not self._enabled():
            return
        deleted = self.bot.db.purge_old_message_archive(self._retention_days())
        if deleted:
            info_log(f"Message memory purged {deleted} old row(s).")

    @cleanup_message_archive.before_loop
    async def _before_cleanup_message_archive(self) -> None:
        await self.bot.wait_until_ready()

    @cleanup_message_archive.error
    async def _cleanup_message_archive_error(self, exc: BaseException) -> None:
        error_log(f"cleanup_message_archive crashed: {exc!r}; restarting loop.")
        try:
            self.cleanup_message_archive.restart()
        except Exception as restart_exc:  # noqa: BLE001
            error_log(f"Failed to restart cleanup_message_archive: {restart_exc!r}")


async def setup(bot: Bot):
    await bot.add_cog(MessageMemory(bot))
