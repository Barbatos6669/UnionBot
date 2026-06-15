"""
Temp-access fallback cog.

Use case: the bot is offline and a new member joins. They can't be
auto-assigned the "Unverified" role and can't see the registration prompt.
Without intervention they sit in the server with no roles and no access.

Workflow:
  1. Officer manually assigns a configured "Bot Down — Pending" role via the
     normal Discord client UI (works even while the bot is offline). This
     role grants minimal read access to a few welcome / general channels.
  2. When the bot comes back online, it scans for any member wearing that
     role, kicks off the registration flow per-member by DMing them a
     Register button (and pinging them in the welcome channel as a fallback
     if DMs are closed), and posts an officer summary.
  3. Once a member completes registration, the existing verification code
     swaps Unverified → Verified as usual; this cog also strips the temp
     role on success so the role only ever marks "still in catch-up".

Slash commands (officers only):
  /temp-access status               — show configured role + members holding it
  /temp-access set-role <role>      — configure the fallback role
  /temp-access grant <member>       — apply the temp role manually + DM the user
  /temp-access scan                 — re-run the recovery scan immediately
  /temp-access clear <member>       — strip the temp role (post-verify cleanup)
"""
from __future__ import annotations

from cogs._typing import Bot
import discord
from discord import app_commands
from discord.ext import commands

from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed, warning_embed


# ── Config keys ──────────────────────────────────────────────────────────────
CFG_TEMP_ROLE_ID = "temp_access_role_id"
CFG_WELCOME_CHANNEL = "welcome_channel_id"  # reused — already set by /admin set-channel
CFG_OFFICER_CHANNEL = "officer_channel_id"  # reused — already set by /admin set-channel




def _get_temp_role(db, guild: discord.Guild) -> discord.Role | None:
    raw = db.get_config(CFG_TEMP_ROLE_ID)
    if not raw:
        return None
    try:
        return guild.get_role(int(raw))
    except (TypeError, ValueError):
        return None


def _get_channel(db, guild: discord.Guild, key: str) -> discord.TextChannel | None:
    raw = db.get_config(key)
    if not raw:
        return None
    try:
        ch = guild.get_channel(int(raw))
    except (TypeError, ValueError):
        return None
    return ch if isinstance(ch, discord.TextChannel) else None


def _build_register_view(bot):
    """Lazy import to avoid circular load with cogs.users_profile."""
    from cogs.users_profile import RegisterView  # noqa: WPS433
    return RegisterView(bot)


def _registration_embed(member: discord.Member) -> discord.Embed:
    return info_embed(
        "Welcome back — let's finish setup!",
        f"Hey {member.mention}, the bot was offline when you arrived so an "
        f"officer manually granted you temporary access. Now that I'm back, "
        f"please click **Register** below to link your Albion character. "
        f"Once an officer approves, you'll get full server access.\n\n"
        f"_If you don't see a Register button below, please post in the "
        f"welcome / help channel and an officer will assist._",
    )


async def _send_registration_prompt(
    bot: Bot,
    member: discord.Member,
    *,
    welcome_channel: discord.TextChannel | None,
) -> tuple[bool, str]:
    """Try DM first, fall back to welcome channel ping. Returns (sent, where)."""
    embed = _registration_embed(member)
    view = _build_register_view(bot)

    # 1) DM
    try:
        await member.send(embed=embed, view=view)
        return True, "DM"
    except (discord.Forbidden, discord.HTTPException):
        pass

    # 2) Welcome channel mention as fallback
    if welcome_channel is not None:
        try:
            await welcome_channel.send(
                content=member.mention,
                embed=embed,
                view=_build_register_view(bot),  # fresh view — can't reuse one across sends
            )
            return True, f"#{welcome_channel.name}"
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"temp_access: welcome-channel send failed for {member}: {exc!r}")

    return False, "nowhere (DM closed, no welcome channel)"


class TempAccess(commands.Cog):
    """Recover registration flow for members who joined while the bot was down."""

    def __init__(self, bot: Bot):
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    # ── Startup recovery ────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Run a single sweep across every guild the bot is in. Safe to call
        # multiple times because it just re-prompts the same members.
        for guild in self.bot.guilds:
            try:
                await self._scan_guild(guild)
            except Exception as exc:  # noqa: BLE001
                error_log(f"temp_access: on_ready scan failed for {guild!r}: {exc!r}")

    async def _scan_guild(self, guild: discord.Guild) -> dict:
        db = self.bot.db  # type: ignore[attr-defined]
        role = _get_temp_role(db, guild)
        if role is None:
            return {"status": "no temp role configured"}

        verified_role = discord.utils.get(guild.roles, name="Verified")
        welcome_ch = _get_channel(db, guild, CFG_WELCOME_CHANNEL)
        officer_ch = _get_channel(db, guild, CFG_OFFICER_CHANNEL)

        prompted: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for member in list(role.members):
            # Already verified → just clean up the temp role.
            if verified_role is not None and verified_role in member.roles:
                try:
                    await member.remove_roles(role, reason="Already verified — clearing temp-access role")
                    skipped.append(f"{member} (already verified, role cleared)")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    error_log(f"temp_access: failed to clear role from {member}: {exc!r}")
                continue

            sent, where = await _send_registration_prompt(self.bot, member, welcome_channel=welcome_ch)
            if sent:
                prompted.append(f"{member} → {where}")
            else:
                failed.append(f"{member} → {where}")

        info_log(
            f"temp_access scan ({guild.name}): role={role.name} "
            f"prompted={len(prompted)} skipped={len(skipped)} failed={len(failed)}"
        )

        if officer_ch is not None and (prompted or failed):
            lines = []
            if prompted:
                lines.append("**Prompted:**\n" + "\n".join(f"• {p}" for p in prompted))
            if failed:
                lines.append("**Failed (need manual help):**\n" + "\n".join(f"• {f}" for f in failed))
            try:
                await officer_ch.send(
                    embed=info_embed(
                        f"Temp-access recovery — {role.name}",
                        "\n\n".join(lines)[:4000],
                    )
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"temp_access: officer summary send failed: {exc!r}")

        return {
            "role": role.name,
            "prompted": len(prompted),
            "skipped": len(skipped),
            "failed": len(failed),
        }

    # ── Slash commands ──────────────────────────────────────────────────────
    temp_group = app_commands.Group(
        name="temp-access",
        description="Fallback role for new members who joined while the bot was down.",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @temp_group.command(name="status", description="Show temp-access role and current holders.")
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from inside the server."),
                ephemeral=True,
            )
            return
        db = self.bot.db  # type: ignore[attr-defined]
        role = _get_temp_role(db, interaction.guild)
        welcome_ch = _get_channel(db, interaction.guild, CFG_WELCOME_CHANNEL)
        officer_ch = _get_channel(db, interaction.guild, CFG_OFFICER_CHANNEL)

        if role is None:
            body = (
                "_No temp-access role configured._\n\n"
                "Use `/temp-access set-role` to pick the role officers will "
                "assign during bot outages."
            )
        else:
            members = role.members
            preview = ", ".join(m.mention for m in members[:25])
            more = f" _(+{len(members) - 25} more)_" if len(members) > 25 else ""
            body = (
                f"**Role:** {role.mention}\n"
                f"**Welcome channel (fallback):** "
                f"{welcome_ch.mention if welcome_ch else '_not set — `/admin set-channel purpose:welcome`_'}\n"
                f"**Officer channel (summary):** "
                f"{officer_ch.mention if officer_ch else '_not set — `/admin set-channel purpose:officer`_'}\n"
                f"**Currently holding role:** {len(members)}\n"
                f"{preview}{more}"
            )
        await interaction.response.send_message(
            embed=info_embed("Temp-access status", body),
            ephemeral=True,
        )

    @temp_group.command(name="set-role", description="Configure the fallback role officers assign during outages.")
    @app_commands.describe(role="A role with read-only access to a few welcome channels.")
    async def set_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        self.bot.db.set_config(CFG_TEMP_ROLE_ID, str(role.id))  # type: ignore[attr-defined]
        info_log(f"/temp-access set-role {role.name} ({role.id}) by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed(
                "Temp-access role set",
                f"Officers can now assign {role.mention} during bot outages. "
                f"When the bot restarts it will DM each holder a Register prompt.",
            ),
            ephemeral=True,
        )

    @temp_group.command(name="grant", description="Apply the temp role to a member and DM them a Register prompt.")
    @app_commands.describe(member="The member to grant temporary access to.")
    async def grant(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None:
            return
        db = self.bot.db  # type: ignore[attr-defined]
        role = _get_temp_role(db, interaction.guild)
        if role is None:
            await interaction.response.send_message(
                embed=error_embed("Not configured", "Run `/temp-access set-role` first."),
                ephemeral=True,
            )
            return
        try:
            await member.add_roles(role, reason=f"/temp-access grant by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "I can't assign that role (check role hierarchy)."),
                ephemeral=True,
            )
            return
        welcome_ch = _get_channel(db, interaction.guild, CFG_WELCOME_CHANNEL)
        sent, where = await _send_registration_prompt(self.bot, member, welcome_channel=welcome_ch)
        info_log(f"/temp-access grant {member} by {interaction.user} → prompt {where}")
        msg = f"{member.mention} now has {role.mention}.\nRegistration prompt: **{where}**"
        if not sent:
            msg += "\n\n⚠ Couldn't reach them — please ping them manually."
        await interaction.response.send_message(
            embed=(success_embed if sent else warning_embed)("Temp-access granted", msg),
            ephemeral=True,
        )

    @temp_group.command(name="scan", description="Re-run the recovery scan now (re-prompts every role-holder).")
    async def scan(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self._scan_guild(interaction.guild)
        body = "\n".join(f"**{k}:** {v}" for k, v in result.items())
        await interaction.followup.send(
            embed=info_embed("Temp-access scan complete", body),
            ephemeral=True,
        )

    @temp_group.command(name="clear", description="Remove the temp-access role from a member.")
    @app_commands.describe(member="The member to clear.")
    async def clear(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild is None:
            return
        role = _get_temp_role(self.bot.db, interaction.guild)  # type: ignore[attr-defined]
        if role is None:
            await interaction.response.send_message(
                embed=error_embed("Not configured", "Run `/temp-access set-role` first."),
                ephemeral=True,
            )
            return
        if role not in member.roles:
            await interaction.response.send_message(
                embed=info_embed("Nothing to clear", f"{member.mention} doesn't have {role.mention}."),
                ephemeral=True,
            )
            return
        try:
            await member.remove_roles(role, reason=f"/temp-access clear by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "I can't remove that role (role hierarchy)."),
                ephemeral=True,
            )
            return
        info_log(f"/temp-access clear {member} by {interaction.user}")
        await interaction.response.send_message(
            embed=success_embed("Cleared", f"{role.mention} removed from {member.mention}."),
            ephemeral=True,
        )

    # ── Auto-cleanup when a member finishes verification ────────────────────
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Strip the temp role automatically once a member becomes Verified."""
        if before.roles == after.roles:
            return
        verified = discord.utils.get(after.guild.roles, name="Verified")
        if verified is None:
            return
        # Only act on the transition: NOT verified before → verified now.
        if verified in before.roles or verified not in after.roles:
            return
        role = _get_temp_role(self.bot.db, after.guild)  # type: ignore[attr-defined]
        if role is None or role not in after.roles:
            return
        try:
            await after.remove_roles(role, reason="Verified — clearing temp-access role")
            info_log(f"temp_access: auto-cleared {role.name} from {after} after verification")
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"temp_access: auto-clear failed for {after}: {exc!r}")


async def setup(bot: Bot) -> None:
    await bot.add_cog(TempAccess(bot))
