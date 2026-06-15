"""Join-to-create voice rooms.

Configured trigger voice channels create temporary rooms in matching
categories. This keeps guest/alliance/faction/content/vibe voice areas tidy:
members join a trigger, get moved into their own room, and the room deletes
when empty.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

import discord
from discord import app_commands
from discord.ext import commands

from config import HOME_GUILD_ROLE_NAME
from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed


LEGACY_GUEST_TRIGGER_ID = "voice_jtc_trigger_id"
LEGACY_GUEST_CATEGORY_ID = "voice_jtc_category_id"
OFFICER_ROLE_NAME = "Officer"


@dataclass(frozen=True)
class JTCProfile:
    key: str
    label: str
    trigger_config: str
    category_config: str
    trigger_name: str
    room_prefix: str
    allowed_roles: tuple[str, ...]
    allow_everyone: bool = False
    legacy_trigger_config: str | None = None
    legacy_category_config: str | None = None


PROFILES: dict[str, JTCProfile] = {
    "guest": JTCProfile(
        key="guest",
        label="Guest",
        trigger_config="guest_jtc_trigger_id",
        category_config="guest_jtc_category_id",
        trigger_name="Join to Create - Guest",
        room_prefix="Guest Room - ",
        allowed_roles=("Guest", "Alliance", HOME_GUILD_ROLE_NAME),
        legacy_trigger_config=LEGACY_GUEST_TRIGGER_ID,
        legacy_category_config=LEGACY_GUEST_CATEGORY_ID,
    ),
    "alliance": JTCProfile(
        key="alliance",
        label="Alliance",
        trigger_config="alliance_jtc_trigger_id",
        category_config="alliance_jtc_category_id",
        trigger_name="Join to Create - Alliance",
        room_prefix="Alliance Room - ",
        allowed_roles=("Alliance", HOME_GUILD_ROLE_NAME),
    ),
    "faction": JTCProfile(
        key="faction",
        label="Faction",
        trigger_config="faction_jtc_trigger_id",
        category_config="faction_jtc_category_id",
        trigger_name="Join to Create - Faction",
        room_prefix="Faction Room - ",
        allowed_roles=("Faction Warfare", "Alliance", HOME_GUILD_ROLE_NAME),
        allow_everyone=True,
    ),
    "content": JTCProfile(
        key="content",
        label="Content",
        trigger_config="content_jtc_trigger_id",
        category_config="content_jtc_category_id",
        trigger_name="Join to Create - Content",
        room_prefix="Content Room - ",
        allowed_roles=(HOME_GUILD_ROLE_NAME,),
    ),
    "vibe": JTCProfile(
        key="vibe",
        label="Vibe",
        trigger_config="vibe_jtc_trigger_id",
        category_config="vibe_jtc_category_id",
        trigger_name="Join to Create - Vibe",
        room_prefix="Vibe Room - ",
        allowed_roles=("Verified", "Guest", "Alliance", HOME_GUILD_ROLE_NAME),
    ),
}


def _int_config(db, *keys: str | None) -> int | None:
    for key in keys:
        if not key:
            continue
        raw = (db.get_config(key) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def _safe_room_name(member: discord.Member, prefix: str) -> str:
    base = re.sub(r"\s+", " ", member.display_name).strip()
    base = re.sub(r"[*_`~|<>@#:&]", "", base) or "Room"
    name = f"{prefix}{base}"
    if len(name) <= 100:
        return name
    return name[:97].rstrip() + "..."


def _role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.get(guild.roles, name=name)


def _voice_overwrites(
    guild: discord.Guild,
    profile: JTCProfile,
    *,
    owner: discord.Member | None = None,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    base_allow = discord.PermissionOverwrite(
        view_channel=True,
        connect=True,
        speak=True,
        stream=True,
        use_voice_activation=True,
    )
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {}
    if profile.allow_everyone:
        overwrites[guild.default_role] = base_allow
    else:
        overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=False,
            connect=False,
        )

    for role_name in profile.allowed_roles:
        role = _role(guild, role_name)
        if role is not None:
            overwrites[role] = base_allow

    officer = _role(guild, OFFICER_ROLE_NAME)
    if officer is not None:
        overwrites[officer] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True,
            manage_channels=True,
            move_members=True,
        )

    if owner is not None:
        overwrites[owner] = base_allow

    me = guild.me
    if me is not None:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            manage_channels=True,
            move_members=True,
        )

    return overwrites


class JoinToCreateVoice(commands.Cog):
    """Create temporary voice rooms from configured trigger channels."""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        self._managed_ids: dict[int, str] = {}
        self.bot.tree.add_command(JoinToCreateVoiceGroup(bot, self))
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:
        try:
            self.bot.tree.remove_command("join-voice")
        except Exception:  # noqa: BLE001
            pass

    def _trigger_id(self, profile: JTCProfile) -> int | None:
        return _int_config(
            self.bot.db,
            profile.trigger_config,
            profile.legacy_trigger_config,
        )

    def _category_id(self, profile: JTCProfile) -> int | None:
        return _int_config(
            self.bot.db,
            profile.category_config,
            profile.legacy_category_config,
        )

    def _profile_for_trigger(self, channel_id: int) -> JTCProfile | None:
        for profile in PROFILES.values():
            if self._trigger_id(profile) == channel_id:
                return profile
        return None

    def _profile_for_room(self, channel: discord.VoiceChannel) -> JTCProfile | None:
        if channel.id in self._managed_ids:
            return PROFILES.get(self._managed_ids[channel.id])
        for profile in PROFILES.values():
            category_id = self._category_id(profile)
            if (
                category_id is not None
                and channel.category_id == category_id
                and channel.name.startswith(profile.room_prefix)
            ):
                return profile
        return None

    async def _cleanup_if_empty(self, channel: discord.abc.GuildChannel | None) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        profile = self._profile_for_room(channel)
        if profile is None or channel.members:
            return
        try:
            await channel.delete(reason=f"{profile.label} join-to-create room emptied")
            self._managed_ids.pop(channel.id, None)
            info_log(
                f"join_voice: deleted empty {profile.key} room "
                f"{channel.id} ({channel.name})."
            )
        except discord.NotFound:
            self._managed_ids.pop(channel.id, None)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"join_voice: failed to delete temp room {channel.id}: {exc!r}")

    async def _create_room_for(
        self,
        member: discord.Member,
        trigger: discord.VoiceChannel,
        profile: JTCProfile,
    ) -> None:
        category_id = self._category_id(profile)
        category = None
        if category_id is not None:
            found = member.guild.get_channel(category_id)
            category = found if isinstance(found, discord.CategoryChannel) else None
        if category is None:
            category = trigger.category
        if category is None:
            error_log(f"join_voice: {profile.key} trigger has no category/config.")
            return

        try:
            room = await category.create_voice_channel(
                name=_safe_room_name(member, profile.room_prefix),
                overwrites=_voice_overwrites(member.guild, profile, owner=member),
                reason=f"{profile.label} join-to-create room for {member}",
            )
            self._managed_ids[room.id] = profile.key
            await member.move_to(room, reason=f"{profile.label} join-to-create room created")
            info_log(
                f"join_voice: created {profile.key} room {room.id} for "
                f"user={member.id} from trigger={trigger.id}."
            )
        except discord.Forbidden as exc:
            error_log(f"join_voice: missing permission creating/moving room: {exc!r}")
        except discord.HTTPException as exc:
            error_log(f"join_voice: Discord rejected room create/move: {exc!r}")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            for profile in PROFILES.values():
                category_id = self._category_id(profile)
                if category_id is None:
                    continue
                category = guild.get_channel(category_id)
                if not isinstance(category, discord.CategoryChannel):
                    continue
                for channel in category.voice_channels:
                    if channel.name.startswith(profile.room_prefix):
                        self._managed_ids[channel.id] = profile.key
                        await self._cleanup_if_empty(channel)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        if isinstance(after.channel, discord.VoiceChannel):
            profile = self._profile_for_trigger(after.channel.id)
            if profile is not None:
                await self._create_room_for(member, after.channel, profile)

        if before.channel and before.channel != after.channel:
            await self._cleanup_if_empty(before.channel)


class JoinToCreateVoiceGroup(
    app_commands.Group,
    name="join-voice",
    description="Join-to-create voice setup.",
):
    def __init__(self, bot: Bot, cog: JoinToCreateVoice):
        super().__init__(default_permissions=discord.Permissions(manage_channels=True))
        self.bot = bot
        self.cog = cog

    async def _require_manage_channels(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms and perms.manage_channels:
            return True
        await interaction.response.send_message(
            embed=error_embed("Permission denied", "Manage Channels is required."),
            ephemeral=True,
        )
        return False

    @app_commands.command(name="status", description="Show join-to-create config.")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._require_manage_channels(interaction):
            return
        lines = []
        for profile in PROFILES.values():
            trigger_id = self.cog._trigger_id(profile)
            category_id = self.cog._category_id(profile)
            managed = sum(1 for key in self.cog._managed_ids.values() if key == profile.key)
            lines.append(
                f"**{profile.label}:** "
                f"{f'<#{trigger_id}>' if trigger_id else '_no trigger_'} -> "
                f"{f'<#{category_id}>' if category_id else '_no category_'} "
                f"({managed} temp)"
            )
        await interaction.response.send_message(
            embed=info_embed("Join-to-create voice", "\n".join(lines)),
            ephemeral=True,
        )

    @app_commands.command(name="set", description="Set a join-to-create trigger and temp-room category.")
    @app_commands.choices(profile=[
        app_commands.Choice(name=p.label, value=p.key)
        for p in PROFILES.values()
    ])
    async def set_config(
        self,
        interaction: discord.Interaction,
        profile: app_commands.Choice[str],
        trigger: discord.VoiceChannel,
        category: discord.CategoryChannel,
    ) -> None:
        if not await self._require_manage_channels(interaction):
            return
        item = PROFILES[profile.value]
        self.bot.db.set_config(item.trigger_config, str(trigger.id))
        self.bot.db.set_config(item.category_config, str(category.id))
        if item.legacy_trigger_config:
            self.bot.db.set_config(item.legacy_trigger_config, str(trigger.id))
        if item.legacy_category_config:
            self.bot.db.set_config(item.legacy_category_config, str(category.id))
        await interaction.response.send_message(
            embed=success_embed(
                f"{item.label} join-to-create set",
                f"Trigger: {trigger.mention}\nTemp rooms category: **{category.name}**",
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(JoinToCreateVoice(bot))
