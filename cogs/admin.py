from cogs._typing import Bot
import asyncio
import discord
import io
from discord import app_commands
from discord.ext import commands
from debug import error_log, info_log
from cogs._nickname_tags import (
    extract_tagged_nickname_name,
    tagged_nickname_for_profile,
)
from cogs.users_profile import (
    RegisterView,
    _audit_member_roles,
    build_registration_embed,
    sync_member_to_albion,
    _resolve_home_guild,
)
from config import LIFECYCLE_ROLES, STAFF_ROLES
from utils import (
    SERVER_CHOICES,
    confirm_action,
    error_embed,
    info_embed,
    success_embed,
)
from time_utils import utc_now_naive
import albion_api

CFG_LIFECYCLE_VC_INACTIVITY_DAYS = "lifecycle_vc_inactivity_days"
CFG_LIFECYCLE_STAT_INACTIVITY_DAYS = "lifecycle_stat_inactivity_days"
CFG_LIFECYCLE_INACTIVITY_MODE = "lifecycle_inactivity_mode"
DEFAULT_LIFECYCLE_VC_INACTIVITY_DAYS = 7
DEFAULT_LIFECYCLE_STAT_INACTIVITY_DAYS = 14
DEFAULT_LIFECYCLE_INACTIVITY_MODE = "either"

# All roles the bot expects to exist, with colours and whether they're hoisted in the member list
_REQUIRED_ROLES = [
    {"name": "Unverified",        "color": discord.Color.from_str("#747f8d"), "hoist": False},
    {"name": "Verified",          "color": discord.Color.from_str("#43b581"), "hoist": False},
    {"name": "Synced",            "color": discord.Color.from_str("#43b581"), "hoist": False},
    {"name": "NotSynced",         "color": discord.Color.from_str("#747f8d"), "hoist": False},
    {"name": "HomeGuild",    "color": discord.Color.from_str("#f04747"), "hoist": True},
    {"name": "Captain",           "color": discord.Color.from_str("#9b59b6"), "hoist": True},
    {"name": "Officer",           "color": discord.Color.from_str("#3498db"), "hoist": True},
    {"name": "Steward",           "color": discord.Color.from_str("#16a085"), "hoist": True},
    {"name": "Holdmaster",        "color": discord.Color.from_str("#8e44ad"), "hoist": True},
    {"name": "Logistician",       "color": discord.Color.from_str("#f1c40f"), "hoist": True},
    {"name": "Crafter",           "color": discord.Color.from_str("#e67e22"), "hoist": True},
    {"name": "Refiner",           "color": discord.Color.from_str("#95a5a6"), "hoist": True},
    {"name": "Alchemist",         "color": discord.Color.from_str("#9b59b6"), "hoist": True},
    {"name": "Guild Farmer",      "color": discord.Color.from_str("#2ecc71"), "hoist": True},
    {"name": "Gatherer",          "color": discord.Color.from_str("#27ae60"), "hoist": True},
    {"name": "Senior Shotcaller", "color": discord.Color.from_str("#e74c3c"), "hoist": True},
    {"name": "Shotcaller",        "color": discord.Color.from_str("#e67e22"), "hoist": True},
    {"name": "Squad Leader",      "color": discord.Color.from_str("#00b894"), "hoist": True},
    {"name": "Recruiter",         "color": discord.Color.from_str("#2ecc71"), "hoist": True},
    {"name": "Recruit",           "color": discord.Color.from_str("#979c9f"), "hoist": True},
    {"name": "Probationary",      "color": discord.Color.from_str("#f0a500"), "hoist": True},
    {"name": "Member",            "color": discord.Color.from_str("#1abc9c"), "hoist": True},
    {"name": "Veteran",           "color": discord.Color.from_str("#e67e22"), "hoist": True},
    {"name": "Inactive",          "color": discord.Color.from_str("#747f8d"), "hoist": False},
    {"name": "Alumni",            "color": discord.Color.from_str("#9b59b6"), "hoist": True},
    {"name": "Alliance",          "color": discord.Color.from_str("#3498db"), "hoist": True},
    {"name": "Guest",             "color": discord.Color.from_str("#607d8b"), "hoist": True},
]

@app_commands.default_permissions(administrator=True)
class AdminGroup(app_commands.Group, name="admin", description="Admin-only commands."):

    def __init__(self, bot: Bot):
        super().__init__()
        self.bot: Bot = bot

    @app_commands.command(name="setup-roles", description="Create all required roles and audit every member's role assignments.")
    async def setup_roles(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # Step 1: Create any missing roles
        existing = {r.name: r for r in interaction.guild.roles}
        created = []
        skipped = []

        for role_def in _REQUIRED_ROLES:
            if role_def["name"] in existing:
                skipped.append(role_def["name"])
            else:
                new_role = await interaction.guild.create_role(
                    name=role_def["name"],
                    color=role_def["color"],
                    hoist=role_def["hoist"],
                    reason=f"Auto-created by /admin setup-roles ({interaction.user})"
                )
                existing[role_def["name"]] = new_role
                created.append(role_def["name"])

        # Step 2: Audit every member
        probationary_days = int(self.bot.db.get_config("probationary_days") or 30)
        member_days       = int(self.bot.db.get_config("member_days")       or 90)
        updated = 0
        failed: list[str] = []

        # Throttle to avoid Discord per-route rate limits on big guilds. A
        # small sleep between mutating calls keeps us well under the ceiling
        # without making the command noticeably slower on small servers.
        SLEEP_BETWEEN = 0.25

        for member in interaction.guild.members:
            if member.bot:
                continue

            profile = self.bot.db.fetch_user_profile(str(member.id))
            roles_to_add, roles_to_remove = _audit_member_roles(
                member, profile, existing, probationary_days, member_days, self.bot.db
            )

            member_changed = False
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove, reason="setup-roles audit")
                    member_changed = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failed.append(f"{member.display_name} (remove: {exc!r})")
                await asyncio.sleep(SLEEP_BETWEEN)
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="setup-roles audit")
                    member_changed = True
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failed.append(f"{member.display_name} (add: {exc!r})")
                await asyncio.sleep(SLEEP_BETWEEN)
            if member_changed:
                updated += 1

        lines = []
        if created:
            lines.append(f"**Created ({len(created)}):** {', '.join(created)}")
        if skipped:
            lines.append(f"**Already existed ({len(skipped)}):** {', '.join(skipped)}")
        lines.append(f"**Members audited and updated:** {updated}")
        if failed:
            preview = ", ".join(failed[:5])
            extra = f" (+{len(failed) - 5} more)" if len(failed) > 5 else ""
            lines.append(f"**Partial failures ({len(failed)}):** {preview}{extra}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        info_log(
            f"{interaction.user} ran setup-roles. Created: {created}, "
            f"updated {updated} members, {len(failed)} failed."
        )

    @app_commands.command(name="setup-registration", description="Set up the registration message in the current channel.")
    async def setup_registration(self, interaction: discord.Interaction) -> None:
        """Sends a message with the registration button."""
        self.bot.db.set_config("registration_channel_id", str(interaction.channel.id))
        await interaction.channel.send(embed=build_registration_embed(), view=RegisterView(self.bot))
        await interaction.response.send_message(
            embed=success_embed(
                "Registration message posted",
                f"Members can now click the button to register. Screenshot safeguards are watching {interaction.channel.mention}.",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="setup-shotcaller-sop",
        description="Post the branded Shotcaller SOP embed in the current channel.",
    )
    async def setup_shotcaller_sop(self, interaction: discord.Interaction) -> None:
        """Post the Shotcaller Standard Operating Procedures as a branded embed.

        Uses the guild crest configured via ``/announce config`` as the
        thumbnail watermark and footer icon, matching the look of official
        announcements.
        """
        db = self.bot.db
        crest_url = (db.get_config("announce_crest_url") or "").strip() or None
        color_hex = (db.get_config("announce_color_hex") or "#e67e22").strip()
        if not color_hex.startswith("#"):
            color_hex = "#" + color_hex
        try:
            color = discord.Color.from_str(color_hex)
        except ValueError:
            color = discord.Color.from_str("#e67e22")
        footer_name = (db.get_config("announce_footer_name") or "Shotcaller SOP").strip()

        embed = discord.Embed(
            title="\u2694\ufe0f Shotcaller SOP \u2014 One Voice Doctrine",
            description=(
                "**During content: one voice. one direction. follow the call.**\n\n"
                "This is the baseline doctrine for every guild fight. Read it before "
                "you ever join a called party. Officers and Senior Shotcallers may "
                "override on a per-fight basis \u2014 follow their call."
            ),
            color=color,
        )

        # ── Chain of Command ────────────────────────────────────────────
        embed.add_field(
            name="\ud83d\udc51 Primary Shotcaller",
            value=(
                "**Final authority during content.**\n"
                "Duties:\n"
                "\u2022 Calls movement\n"
                "\u2022 Calls engages\n"
                "\u2022 Calls resets\n"
                "\u2022 Calls retreats\n"
                "\u2022 Decides when to fight or leave\n\n"
                "Everyone follows the Primary Shotcaller immediately."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udee1\ufe0f Support Caller",
            value=(
                "**Tracks backline and defensive needs.**\n"
                "Duties:\n"
                "\u2022 Reports healer pressure\n"
                "\u2022 Calls peel needs\n"
                "\u2022 Warns about flankers\n"
                "\u2022 Tracks defensive problems\n\n"
                "Keep calls short and useful."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udde1\ufe0f Bomb / Follow-Up Caller",
            value=(
                "**Coordinates damage timing.**\n"
                "Duties:\n"
                "\u2022 Confirms DPS readiness\n"
                "\u2022 Echoes engage calls\n"
                "\u2022 Calls damage windows\n"
                "\u2022 Calls follow-up pressure"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udc41\ufe0f Scout / Intel",
            value=(
                "**Provides information only.**\n"
                "Duties:\n"
                "\u2022 Reports enemy numbers\n"
                "\u2022 Reports enemy direction\n"
                "\u2022 Reports enemy comp\n"
                "\u2022 Reports third parties or danger\n\n"
                "Scouts do not shotcall unless assigned."
            ),
            inline=False,
        )

        # ── Comms Rules ─────────────────────────────────────────────────
        embed.add_field(
            name="\ud83d\udce2 Clear Comms",
            value=(
                "When **\u201cClear Comms\u201d** is called, only assigned callers speak.\n\n"
                "Allowed voices:\n"
                "\u2022 Primary Shotcaller\n"
                "\u2022 Support Caller\n"
                "\u2022 Bomb Caller\n"
                "\u2022 Scout"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udeab No Backseat Shotcalling",
            value=(
                "**Do not call:** engages, retreats, chases, route changes, target swaps.\n"
                "**Give information only.**\n\n"
                "Good: *\u201cHealers pressured.\u201d* / *\u201cFlank west.\u201d* / *\u201cEnemy clumped.\u201d* / *\u201cThird party south.\u201d*\n"
                "Bad: *\u201cPush them.\u201d* / *\u201cWhy are we running?\u201d* / *\u201cGo east instead.\u201d* / *\u201cChase them.\u201d*"
            ),
            inline=False,
        )

        # ── Fight Flow ──────────────────────────────────────────────────
        embed.add_field(
            name="\ud83d\udd27 Form Up",
            value=(
                "Before step-off:\n"
                "\u2022 Roles confirmed\n"
                "\u2022 Builds checked\n"
                "\u2022 Food/pots ready\n"
                "\u2022 Everyone in comms\n"
                "\u2022 Content goal explained"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udc0e Movement",
            value=(
                "During movement:\n"
                "\u2022 Stay with group\n"
                "\u2022 Stay mounted unless called\n"
                "\u2022 Do not chase\n"
                "\u2022 Do not scout unless assigned\n"
                "\u2022 Keep comms light"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udd0d Contact",
            value=(
                "When enemies are spotted:\n"
                "\u2022 Scout reports numbers/direction\n"
                "\u2022 Shotcaller decides fight, hold, kite, or leave\n"
                "\u2022 DPS waits for damage call\n"
                "\u2022 Tanks and supports prepare"
            ),
            inline=False,
        )
        embed.add_field(
            name="\u2694\ufe0f Engage",
            value=(
                "**Only engage on call.**\n"
                "Basic flow:\n"
                "\u2022 Tanks prepare\n"
                "\u2022 Engage countdown\n"
                "\u2022 CC lands\n"
                "\u2022 Bomb Caller confirms damage\n"
                "\u2022 DPS drops damage"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udd04 Reset",
            value=(
                "After engage:\n"
                "\u2022 Stop chasing\n"
                "\u2022 Reposition\n"
                "\u2022 Heal up\n"
                "\u2022 Wait for cooldowns\n"
                "\u2022 Prepare next call"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83e\udd1d Peel",
            value=(
                "If backline is pressured:\n"
                "\u2022 Tanks peel\n"
                "\u2022 Supports call pressure\n"
                "\u2022 DPS helps if needed\n"
                "\u2022 Protect healers first"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83c\udfc3 Disengage",
            value=(
                "If the retreat call is made:\n"
                "\u2022 Leave immediately\n"
                "\u2022 Do not turn\n"
                "\u2022 Do not chase\n"
                "\u2022 Do not argue"
            ),
            inline=False,
        )
        embed.add_field(
            name="\u2693 Anchor Rule",
            value=(
                "If you get lost:\n"
                "1\ufe0f\u20e3 Follow the shotcaller marker.\n"
                "2\ufe0f\u20e3 If lost, follow the tank line.\n"
                "3\ufe0f\u20e3 If still lost, regroup with the main party.\n"
                "4\ufe0f\u20e3 Call your location once if fully split."
            ),
            inline=False,
        )

        # ── Expectations / Review / Discipline ──────────────────────────
        embed.add_field(
            name="\ud83d\udc65 Player Expectations",
            value=(
                "All members must:\n"
                "\u2022 Be in voice\n"
                "\u2022 Follow calls\n"
                "\u2022 Bring assigned build\n"
                "\u2022 Stay with group\n"
                "\u2022 Keep comms clean\n"
                "\u2022 Save feedback for after content"
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udcdd After-Action Review",
            value=(
                "Feedback happens **after** content.\n"
                "Review topics:\n"
                "\u2022 Good calls\n"
                "\u2022 Bad engages\n"
                "\u2022 Positioning mistakes\n"
                "\u2022 Comms issues\n"
                "\u2022 Comp problems\n"
                "\u2022 Player improvements"
            ),
            inline=False,
        )
        embed.add_field(
            name="\u26a0\ufe0f Discipline",
            value=(
                "Repeated issues may lead to: **warning \u2192 role reassignment \u2192 removal from party "
                "\u2192 removal from future content \u2192 officer review**.\n\n"
                "Examples: backseat shotcalling, ignoring calls, comms clutter, refusing role, "
                "splitting from group, flaming members."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udd14 Final Rule",
            value="**During content: one voice. one direction. follow the call.**",
            inline=False,
        )

        if crest_url:
            embed.set_thumbnail(url=crest_url)
            embed.set_footer(
                text=f"{footer_name} \u00b7 Updated by {interaction.user.display_name}",
                icon_url=crest_url,
            )
        else:
            embed.set_footer(text=f"{footer_name} \u00b7 Updated by {interaction.user.display_name}")

        try:
            await interaction.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post here",
                    "I don't have permission to post in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"setup-shotcaller-sop failed: {exc!s}")
            await interaction.response.send_message(
                embed=error_embed("Failed to post", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                "Shotcaller SOP posted",
                "The branded SOP embed is up. Pin it so new shotcallers can find it.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} posted shotcaller SOP in #{interaction.channel}.")

    @app_commands.command(
        name="setup-timer-claims",
        description="Post the branded Timer Claim System embed in the current channel.",
    )
    async def setup_timer_claims(self, interaction: discord.Interaction) -> None:
        """Post the guild's Timer Claim System doctrine as a branded embed.

        Lists the actual prime-time slots configured in ``_lfg_config.PRIME_SLOTS``
        so the message stays in sync with what `/lfg post-board` actually shows.
        Uses the crest configured via `/announce config` for watermark + footer.
        """
        from cogs._timer_claims_guide import (
            CFG_POSTED_BY,
            TRACKER_ID,
            TRACKER_TYPE,
            build_timer_claim_guide_embed,
        )

        db = self.bot.db
        db.set_config(CFG_POSTED_BY, interaction.user.display_name)
        embed = build_timer_claim_guide_embed(db, interaction.user.display_name)

        try:
            msg = await interaction.channel.send(embed=embed)
            db.upsert_live_graph(TRACKER_TYPE, TRACKER_ID, str(msg.channel.id), str(msg.id))
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post here",
                    "I don't have permission to post in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"setup-timer-claims failed: {exc!s}")
            await interaction.response.send_message(
                embed=error_embed("Failed to post", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                "Timer Claim System posted",
                "Pin it in the events channel so shotcallers see it.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} posted timer-claim doctrine in #{interaction.channel}.")

    @app_commands.command(
        name="setup-officer-lifecycle-guide",
        description="Post officer SOP: how lifecycle roles (Recruit/Veteran/Inactive/Alumni) work + manual fallback.",
    )
    async def setup_officer_lifecycle_guide(self, interaction: discord.Interaction) -> None:
        """Officer-facing reference embed for the **lifecycle / tenure** role system.

        Posts a branded embed in the current channel (intended for an
        officer-only channel) documenting:
          1. What each lifecycle role means and how the bot derives it.
          2. The auto-assign cadence (sync_guilds, inactivity scan).
          3. **Manual fallback** \u2014 exactly how an officer assigns the
             correct rank role by hand when the bot is offline so new joiners
             and rank promotions don't stall.
        """
        db = self.bot.db
        crest_url = (db.get_config("announce_crest_url") or "").strip() or None
        color_hex = (db.get_config("announce_color_hex") or "#d4af37").strip()
        if not color_hex.startswith("#"):
            color_hex = "#" + color_hex
        try:
            color = discord.Color.from_str(color_hex)
        except ValueError:
            color = discord.Color.from_str("#d4af37")
        footer_name = (db.get_config("announce_footer_name") or "Officer SOP").strip()

        # Pull the current lifecycle inactivity thresholds so the SOP shows the
        # real numbers officers will see during sync.
        try:
            vc_inactive_days = int(
                db.get_config(CFG_LIFECYCLE_VC_INACTIVITY_DAYS)
                or DEFAULT_LIFECYCLE_VC_INACTIVITY_DAYS
            )
        except (TypeError, ValueError):
            vc_inactive_days = DEFAULT_LIFECYCLE_VC_INACTIVITY_DAYS
        try:
            stat_inactive_days = int(
                db.get_config(CFG_LIFECYCLE_STAT_INACTIVITY_DAYS)
                or DEFAULT_LIFECYCLE_STAT_INACTIVITY_DAYS
            )
        except (TypeError, ValueError):
            stat_inactive_days = DEFAULT_LIFECYCLE_STAT_INACTIVITY_DAYS
        inactive_mode = (
            db.get_config(CFG_LIFECYCLE_INACTIVITY_MODE)
            or DEFAULT_LIFECYCLE_INACTIVITY_MODE
        ).strip().lower()
        inactive_joiner = "and" if inactive_mode == "both" else "or"

        embed = discord.Embed(
            title="\ud83c\udfdb\ufe0f Officer SOP \u2014 Lifecycle Roles",
            description=(
                "**Officer-only reference.** Lifecycle roles track each member's "
                "**tenure and status** in the guild. The bot assigns them "
                "automatically based on Discord join date, Albion guild "
                "membership, and recent activity \u2014 but if the bot is down, "
                "officers must be able to set the right role by hand so the "
                "roster doesn't break."
            ),
            color=color,
        )

        embed.add_field(
            name="\ud83d\udccb The Six Lifecycle Roles",
            value=(
                "\u2022 **Recruit** \u2014 just joined Discord, **not yet "
                "verified** in-game. Earliest stage, before Probationary.\n"
                "\u2022 **Probationary** \u2014 verified and in the home guild, "
                "but **< 30 days** in the server. Still proving fit.\n"
                "\u2022 **Member** \u2014 verified, in the home guild, "
                "**30\u201389 days** of tenure. Full member.\n"
                "\u2022 **Veteran** \u2014 verified, in the home guild, "
                f"**90+ days** of tenure. Eligible for senior staff applications.\n"
                "\u2022 **Inactive** \u2014 no guild VC for "
                f"**{vc_inactive_days}+ days** {inactive_joiner} no stat "
                "movement (kill fame, PvE, gathering, crafting) for "
                f"**{stat_inactive_days}+ days**. "
                "Replaces Probationary/Member/Veteran while quiet.\n"
                "\u2022 **Alumni** \u2014 was in the home guild, **no longer is**. "
                "Kept in Discord as a former member.\n\n"
                "Two related tags the bot also manages: **Alliance** (in a "
                "different guild within our alliance) and **Guest** (verified "
                "but neither in the home guild nor the alliance)."
            ),
            inline=False,
        )

        embed.add_field(
            name="\u2705 Normal Path (bot online)",
            value=(
                "The guild-scan task runs on a schedule and:\n"
                "\u2022 Adds **Recruit** the moment a new Discord join is detected.\n"
                "\u2022 Promotes **Recruit \u2192 Probationary** once verified + in the home guild.\n"
                "\u2022 Promotes **Probationary \u2192 Member \u2192 Veteran** by tenure (30 / 90 days).\n"
                "\u2022 Swaps the active rank to **Inactive** after the configured VC/stat inactivity thresholds.\n"
                "\u2022 Swaps to **Alumni** when a member leaves the home guild.\n"
                "Officers should **not** touch lifecycle roles while the bot is "
                "online \u2014 manual edits get overwritten on the next sync."
            ),
            inline=False,
        )

        embed.add_field(
            name="\ud83d\udee0\ufe0f Manual Fallback \u2014 Bot Is Down",
            value=(
                "If the bot is offline and a member needs the correct lifecycle "
                "role *now*, any officer with **Manage Roles** can set it.\n\n"
                "**Decision tree** (pick exactly one of these per member):\n"
                "\u2022 New join, not verified yet \u2192 **Recruit**\n"
                "\u2022 Verified, in home guild, **< 30 days** since Discord join \u2192 **Probationary**\n"
                "\u2022 Verified, in home guild, **30\u201389 days** \u2192 **Member**\n"
                "\u2022 Verified, in home guild, **90+ days** \u2192 **Veteran**\n"
                f"\u2022 Was Probationary/Member/Veteran but silent {inactive_days}+ days \u2192 **Inactive**\n"
                "\u2022 Left the home guild but still in Discord \u2192 **Alumni**\n\n"
                "**How to assign on Desktop / Web:**\n"
                "\u2022 1\ufe0f\u20e3 Right-click the member \u2192 **Roles** "
                "\u2192 **uncheck** their old lifecycle role.\n"
                "\u2022 2\ufe0f\u20e3 Right-click again \u2192 **Roles** "
                "\u2192 **check** the correct new lifecycle role.\n\n"
                "**On Mobile:** tap avatar \u2192 **Manage User** \u2192 "
                "**Roles** \u2192 toggle off the old, toggle on the new.\n\n"
                "\u26a0\ufe0f A member should hold **exactly one** lifecycle "
                "role at a time. Always remove the old one before adding the new."
            ),
            inline=False,
        )

        embed.add_field(
            name="\ud83d\udd04 What Happens When the Bot Comes Back",
            value=(
                "On the next guild-scan, the bot **re-derives** the correct "
                "lifecycle role from Discord join date + in-game data and will "
                "**overwrite** your manual assignment if it disagrees. That's "
                "fine \u2014 it just means the bot caught up. If you believe "
                "the bot picked wrong (e.g. someone is incorrectly flagged "
                "Inactive), use `/admin recheck-member` or escalate to the "
                "Guild Leader rather than re-editing the role by hand."
            ),
            inline=False,
        )

        embed.add_field(
            name="\ud83d\udcdd Log the Manual Assign",
            value=(
                "Drop a one-liner in the officer channel so the change is "
                "auditable, e.g.:\n"
                "`Manual lifecycle: @Player Probationary \u2192 Member (bot down)`"
            ),
            inline=False,
        )

        embed.add_field(
            name="\u26d4 Never Manually Assign",
            value=(
                "These are bot-managed *and* tied to other systems \u2014 "
                "hand-editing them breaks state:\n"
                "\u2022 **Staff / command roles** (Captain, Officer, Shotcaller, "
                "Recruiter, etc.) \u2014 use `/staff apply` and the application flow.\n"
                "\u2022 **HomeGuild** alliance tag \u2014 set by guild sync.\n"
                "If urgent and the bot is down, ping the Guild Leader first."
            ),
            inline=False,
        )

        embed.add_field(
            name="\ud83d\udd14 Rule of Thumb",
            value=(
                "**One lifecycle role per member, set by tenure + status.** "
                "Touch them only when the bot is offline, and log every change. "
                "Everything else, let the bot handle it."
            ),
            inline=False,
        )

        if crest_url:
            embed.set_thumbnail(url=crest_url)
            embed.set_footer(
                text=f"{footer_name} \u00b7 Lifecycle Roles \u00b7 Posted by {interaction.user.display_name}",
                icon_url=crest_url,
            )
        else:
            embed.set_footer(
                text=f"{footer_name} \u00b7 Lifecycle Roles \u00b7 Posted by {interaction.user.display_name}"
            )

        try:
            await interaction.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post here",
                    "I don't have permission to post in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"setup-officer-lifecycle-guide failed: {exc!s}")
            await interaction.response.send_message(
                embed=error_embed("Failed to post", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                "Officer lifecycle-roles SOP posted",
                "Pin it in the officer channel so every officer can find it.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} posted officer lifecycle-roles SOP in #{interaction.channel}."
        )

    # ── Channel setters: one command, purpose choice ─────────────────────
    # Single source of truth for which guild_config key each purpose maps to.
    # Add a new purpose by appending one row.
    _CHANNEL_PURPOSES: dict[str, tuple[str, str]] = {
        # purpose -> (config_key, friendly label for the success embed)
        "registration":  ("registration_channel_id",             "📝 Registration"),
        "officer":       ("officer_channel_id",                  "🛡️ Officer review"),
        "welcome":       ("welcome_channel_id",                  "👋 Welcome"),
        "goodbye":       ("goodbye_channel_id",                  "👋 Goodbye"),
        "points":        ("points_announce_channel_id",          "🏆 Points announce"),
        "hof":           ("automation_hall_of_fame_channel_id",  "🏅 Hall of Fame"),
        "announcements": ("automation_announcements_channel_id", "📢 Announcements"),
        "sso-routes":    ("sso_routes_channel_id",               "🗺️ SSO routes"),
    }

    @app_commands.command(
        name="set-channel",
        description="Set one of the bot's notification channels (officer, welcome, points, HOF, etc.).",
    )
    @app_commands.describe(
        purpose="Which notification stream to route.",
        channel="Text channel that will receive these posts.",
    )
    @app_commands.choices(purpose=[
        app_commands.Choice(name="Registration (register button)",       value="registration"),
        app_commands.Choice(name="Officer review (registrations)",     value="officer"),
        app_commands.Choice(name="Welcome (new-member shouts)",        value="welcome"),
        app_commands.Choice(name="Goodbye (member leave shouts)",      value="goodbye"),
        app_commands.Choice(name="Points announce (award shouts)",     value="points"),
        app_commands.Choice(name="Hall of Fame (fame milestones)",     value="hof"),
        app_commands.Choice(name="Announcements (weekly recap)",       value="announcements"),
        app_commands.Choice(name="SSO routes (approved portal posts)", value="sso-routes"),
    ])
    async def set_channel(
        self,
        interaction: discord.Interaction,
        purpose: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        config_key, label = self._CHANNEL_PURPOSES[purpose.value]
        self.bot.db.set_config(config_key, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                f"{label} channel updated",
                f"Posts will now go to {channel.mention}.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set {config_key}={channel.id} ({label})")

    @app_commands.command(
        name="show-channels",
        description="Show every configured notification channel side-by-side.",
    )
    async def show_channels(self, interaction: discord.Interaction) -> None:
        lines: list[str] = []
        for purpose, (config_key, label) in self._CHANNEL_PURPOSES.items():
            raw = self.bot.db.get_config(config_key)
            if raw:
                ch = self.bot.get_channel(int(raw))
                target = ch.mention if ch else f"⚠️ unknown id `{raw}`"
            else:
                target = "— not set —"
            lines.append(f"**{label}** (`{purpose}`): {target}")
        await interaction.response.send_message(
            embed=info_embed("Notification channels", "\n".join(lines)),
            ephemeral=True,
        )

    @app_commands.command(
        name="set-milestone-threshold",
        description="Set the Hall of Fame threshold for a fame metric (per sync window).",
    )
    @app_commands.describe(
        metric="Which fame metric to tune.",
        threshold="Minimum fame gain in one sync window to fire a shoutout. 0 = disable this metric.",
    )
    @app_commands.choices(metric=[
        app_commands.Choice(name="Kill fame",     value="kill_fame"),
        app_commands.Choice(name="Death fame",    value="death_fame"),
        app_commands.Choice(name="PvE fame",      value="pve_total"),
        app_commands.Choice(name="Gather fame",   value="gather_all"),
        app_commands.Choice(name="Crafting fame", value="crafting_fame"),
        app_commands.Choice(name="Fishing fame",  value="fishing_fame"),
        app_commands.Choice(name="ALL (global override)", value="__all__"),
    ])
    async def set_milestone_threshold(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        threshold: app_commands.Range[int, 0, 100_000_000],
    ) -> None:
        if metric.value == "__all__":
            key = "automation_kill_milestone_threshold"
            label = "global override (all metrics)"
        else:
            key = f"automation_milestone_threshold_{metric.value}"
            label = metric.name
        self.bot.db.set_config(key, str(int(threshold)))
        msg = (
            f"**{label}** threshold set to **{int(threshold):,}** fame per sync window."
            if threshold > 0 else
            f"**{label}** disabled (threshold=0)."
        )
        await interaction.response.send_message(
            embed=success_embed("Milestone threshold updated", msg),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set {key}={int(threshold)}")

    @app_commands.command(name="set-roles", description="Set the roles for unverified and verified users.")
    @app_commands.describe(
        unverified_role="Role given to brand-new joins before they register.",
        verified_role="Role granted once an officer approves their registration.",
    )
    async def set_roles(self, interaction: discord.Interaction, unverified_role: discord.Role, verified_role: discord.Role) -> None:
        self.bot.db.set_config("unverified_role_id", str(unverified_role.id))
        self.bot.db.set_config("verified_role_id", str(verified_role.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Roles configured",
                f"Unverified → {unverified_role.mention}\nVerified → {verified_role.mention}",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="add-guild", description="Add or update a guild in the database.")
    @app_commands.describe(
        guild_name="Exact in-game guild name (case-insensitive).",
        server="Albion server region the guild plays on.",
    )
    @app_commands.choices(server=SERVER_CHOICES)
    async def add_guild(
        self,
        interaction: discord.Interaction,
        guild_name: str,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_name = guild_name.strip()
        if not guild_name:
            await interaction.followup.send(
                embed=error_embed("Guild name required", "Please provide a non-empty guild name."),
                ephemeral=True,
            )
            return
        server_value = server.value if server else "americas"

        # Network call — dispatch to a thread so we don't block the event loop
        # if the Albion API stalls (it has a 30s timeout).
        result = await asyncio.to_thread(
            albion_api.get_guild_id, guild_name, server_value, 30.0,
        )
        if not result:
            await interaction.followup.send(
                embed=error_embed(
                    "Guild not found",
                    f"`{guild_name}` could not be found on **{server_value.capitalize()}**.",
                    hint="Double-check the spelling or pick a different server.",
                ),
                ephemeral=True,
            )
            return

        guild_id, exact_name = result
        data = await asyncio.to_thread(
            albion_api.get_guild_stats, guild_id, server_value, 30.0,
        )
        if not data:
            await interaction.followup.send(
                embed=error_embed(
                    "Failed to fetch guild stats",
                    f"Found `{exact_name}` but the Albion API did not return its stats.",
                    hint="Try again in a minute — the Albion API may be rate-limiting.",
                ),
                ephemeral=True,
            )
            return
        
        stats = albion_api.parse_guild_stats(data)
        self.bot.db.upsert_guild(
            stats["guild_id"], stats["guild_name"], stats["founder_name"], stats["founded"],
            stats["kill_fame"], stats["death_fame"], stats["member_count"],
            stats["alliance_id"], stats["alliance_name"], stats["alliance_tag"]
        )
        
        embed = discord.Embed(title=exact_name, color=discord.Color.green())
        embed.add_field(name="Members", value=stats["member_count"], inline=True)
        embed.add_field(name="Kill Fame", value=f"{stats['kill_fame']:,}", inline=True)
        embed.add_field(name="Alliance", value=stats["alliance_name"] or "None", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="deregister", description="Remove a user's registration and reset their profile.")
    @app_commands.describe(member="Member whose registration should be cleared.")
    async def deregister(self, interaction: discord.Interaction, member: discord.Member) -> None:
        profile = self.bot.db.fetch_user_profile(str(member.id))
        old_lifecycle = profile.get("lifecycle_role") if profile else None

        confirmed = await confirm_action(
            interaction,
            title="Deregister this member?",
            description=(
                f"This will clear {member.mention}'s linked Albion character, remove their\n"
                "Verified / HomeGuild / lifecycle roles, and reset their nickname."
            ),
            confirm_label="Deregister",
        )
        if not confirmed:
            return

        self.bot.db.clear_user_albion_info(str(member.id))

        verified_role = discord.utils.get(interaction.guild.roles, name="Verified")
        unverified_role = discord.utils.get(interaction.guild.roles, name="Unverified")
        tu_role = discord.utils.get(interaction.guild.roles, name="HomeGuild")
        lifecycle_role = discord.utils.get(interaction.guild.roles, name=old_lifecycle) if old_lifecycle else None

        roles_to_remove = [r for r in [verified_role, tu_role, lifecycle_role] if r and r in member.roles]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        if unverified_role and unverified_role not in member.roles:
            await member.add_roles(unverified_role)

        try:
            await member.edit(nick=None)
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.followup.send(
            embed=success_embed(
                "Deregistered",
                f"{member.mention}'s profile has been reset. They can register again at any time.",
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="register-for",
        description="Manually link a member to an Albion character (skips screenshot review).",
    )
    @app_commands.describe(
        member="Discord member to register on behalf of.",
        albion_name="Their Albion Online character name (exact spelling).",
        server="Albion server. Default Americas.",
    )
    @app_commands.choices(server=SERVER_CHOICES)
    async def register_for(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        albion_name: str,
        server: app_commands.Choice[str] | None = None,
    ) -> None:
        """Officer/admin override for members who can't navigate the
        self-service Register button flow. Performs the same Albion API lookup
        + profile link, marks the registration verified (no screenshot
        round-trip), and assigns the correct lifecycle role.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        server_value = server.value if server else "americas"
        discord_id = str(member.id)
        approver = interaction.user

        if member.bot:
            await interaction.followup.send(
                embed=error_embed("Can't register a bot", f"{member.mention} is a bot account."),
                ephemeral=True,
            )
            return

        # Reject if this Discord account is already linked.
        existing = self.bot.db.fetch_user_profile(discord_id) or {}
        if existing.get("albion_player_id"):
            await interaction.followup.send(
                embed=error_embed(
                    "Already registered",
                    f"{member.mention} is already linked to **{existing.get('albion_name') or 'a character'}**.",
                    hint="Run `/admin deregister` first if you want to relink them.",
                ),
                ephemeral=True,
            )
            return

        # Blacklist gates — staff override would defeat the purpose of the
        # deny list, so we honor it. Use /admin (blacklist remove) to lift.
        try:
            if self.bot.db.is_blacklisted(discord_id=discord_id):
                await interaction.followup.send(
                    embed=error_embed(
                        "Blocked by blacklist",
                        f"{member.mention} is on the deny list. Remove them from the blacklist first.",
                    ),
                    ephemeral=True,
                )
                return
        except Exception:  # noqa: BLE001
            pass

        # Validate the name input the officer typed.
        cleaned = (albion_name or "").strip()
        if not cleaned or " " in cleaned or len(cleaned) < 2 or len(cleaned) > 16:
            await interaction.followup.send(
                embed=error_embed(
                    "Check the Albion name",
                    f"`{cleaned or '(empty)'}` doesn't look like a valid Albion character name.",
                    hint="Albion names are 2-16 characters, no spaces.",
                ),
                ephemeral=True,
            )
            return

        # Albion API lookup. Bias to the home guild so 'John' resolves to
        # *our* John when multiple characters share the name.
        loop = asyncio.get_running_loop()
        home_guild = _resolve_home_guild(self.bot.db)
        try:
            result = await loop.run_in_executor(
                None,
                lambda: albion_api.get_player_id(cleaned, server_value, prefer_guild_name=home_guild),
            )
        except Exception as exc:  # noqa: BLE001
            info_log(f"register-for: API error for {cleaned}: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Albion API error",
                    f"Couldn't reach the Albion API: `{exc}`. Try again in a minute.",
                ),
                ephemeral=True,
            )
            return
        if not result:
            await interaction.followup.send(
                embed=error_embed(
                    "Character not found",
                    f"`{cleaned}` was not found on the **{server_value.capitalize()}** server.",
                    hint="Capitalization matters. Double-check with `/profile lookup` or in-game.",
                ),
                ephemeral=True,
            )
            return
        player_id, exact_name = result

        # Reject duplicate links: this Albion character must not already be
        # owned by another Discord account.
        other = self.bot.db.fetch_user_profile_by_player_id(player_id)
        if other and str(other.get("discord_id") or "") != discord_id:
            other_did = other.get("discord_id")
            other_mention = f"<@{other_did}>" if other_did else "another user"
            await interaction.followup.send(
                embed=error_embed(
                    "Character already linked",
                    f"**{exact_name}** is already linked to {other_mention}.",
                    hint="Deregister the other account first if this is a transfer.",
                ),
                ephemeral=True,
            )
            return
        try:
            if self.bot.db.is_blacklisted(albion_player_id=player_id):
                await interaction.followup.send(
                    embed=error_embed(
                        "Blocked by blacklist",
                        f"`{exact_name}` is on the deny list.",
                    ),
                    ephemeral=True,
                )
                return
        except Exception:  # noqa: BLE001
            pass

        # Fetch stats so reviewers in audit log can see what they linked.
        try:
            data = await loop.run_in_executor(
                None, lambda: albion_api.get_player_stats(player_id, server_value),
            )
        except Exception:  # noqa: BLE001
            data = None
        stats = albion_api.parse_stats(data) if data else {}

        # Confirm with the officer before mutating anything. Shows the
        # exact character we matched and current guild so a typo doesn't
        # silently link the wrong person.
        guild_name = stats.get("guild_name") or "(none)"
        kf = int(stats.get("kill_fame") or 0)
        ip = float(stats.get("average_item_power") or 0.0)
        confirmed = await confirm_action(
            interaction,
            title="Manual register — confirm",
            description=(
                f"Link {member.mention} to:\n\n"
                f"• **Name:** `{exact_name}`\n"
                f"• **Server:** {server_value.capitalize()}\n"
                f"• **Guild:** {guild_name}\n"
                f"• **Kill Fame:** {kf:,}\n"
                f"• **Avg IP:** {ip:.1f}\n\n"
                "Verification will be marked complete (no screenshot needed). "
                "Lifecycle will be set to **Recruit** if they're in the home guild, "
                "otherwise **Guest**."
            ),
            confirm_label="Link",
        )
        if not confirmed:
            return

        # Mutations from here on. Mirror what RegisterModal +
        # VerificationView._approve do, minus the screenshot dance.
        self.bot.db.update_user_albion_info(discord_id, player_id, exact_name, stats)
        self.bot.db.set_verified_date(discord_id)
        self.bot.db.set_pending_verification(discord_id, False)

        # Roles + lifecycle. Logic mirrors VerificationView._approve.
        unverified_role = discord.utils.get(interaction.guild.roles, name="Unverified")
        verified_role = discord.utils.get(interaction.guild.roles, name="Verified")
        recruit_role = discord.utils.get(interaction.guild.roles, name="Recruit")
        guest_role = discord.utils.get(interaction.guild.roles, name="Guest")
        tu_role = discord.utils.get(interaction.guild.roles, name="HomeGuild")

        try:
            if unverified_role and unverified_role in member.roles:
                await member.remove_roles(unverified_role, reason=f"Manual register by {approver}")
            if verified_role and verified_role not in member.roles:
                await member.add_roles(verified_role, reason=f"Manual register by {approver}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            info_log(f"register-for: verified/unverified swap failed for {member}: {exc!r}")

        in_home_guild = bool(guild_name) and guild_name.strip().lower() == home_guild.lower()
        if in_home_guild and recruit_role:
            try:
                await member.add_roles(recruit_role, reason=f"Manual register by {approver}")
            except (discord.Forbidden, discord.HTTPException):
                pass
            self.bot.db.set_lifecycle_role(discord_id, "Recruit")
            # Auto-resolve any pending application — they're already in.
            try:
                pending_app = self.bot.db.fetch_pending_guild_application(discord_id)
                if pending_app:
                    self.bot.db.update_guild_application_status(
                        pending_app["id"], "approved", str(approver.id),
                        f"Auto-approved via /admin register-for (already in {home_guild})",
                    )
            except Exception:  # noqa: BLE001
                pass
            # Tie into the recruitment funnel so /recruit list reflects this.
            try:
                from cogs.applications import _sync_recruit_row
                _sync_recruit_row(
                    self.bot.db, cleaned,
                    discord_id=discord_id, status="registered",
                    source="manual_register",
                )
            except Exception as exc:  # noqa: BLE001
                info_log(f"register-for: recruit funnel sync failed: {exc!r}")
            assigned_lifecycle = "Recruit"
        else:
            if guest_role:
                try:
                    await member.add_roles(guest_role, reason=f"Manual register by {approver}")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            self.bot.db.set_lifecycle_role(discord_id, "Guest")
            assigned_lifecycle = "Guest"

        # Home-guild role + alliance nickname tag.
        if in_home_guild and tu_role:
            try:
                await member.add_roles(tu_role, reason=f"Manual register by {approver}")
            except (discord.Forbidden, discord.HTTPException):
                pass
        new_nick = tagged_nickname_for_profile(
            self.bot.db,
            exact_name,
            stats,
            home_member=in_home_guild,
        )
        try:
            await member.edit(nick=new_nick, reason=f"Manual register by {approver}")
        except (discord.Forbidden, discord.HTTPException):
            pass

        info_log(
            f"Manual registration by {approver} ({approver.id}): "
            f"{member} -> {exact_name} (player_id={player_id}, "
            f"server={server_value}, lifecycle={assigned_lifecycle})."
        )

        # DM the member so they know it happened.
        try:
            await member.send(
                f"An officer linked your Discord account to **{exact_name}** in Albion Online. "
                f"You've been set to **{assigned_lifecycle}**. Use `/profile view` to see your stats."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.followup.send(
            embed=success_embed(
                "Registered",
                f"{member.mention} is now linked to **{exact_name}** "
                f"({server_value.capitalize()}) as **{assigned_lifecycle}**.",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="set-lifecycle-role", description="Manually set a member's lifecycle role (overrides automation).")
    @app_commands.describe(member="The member to update", role="The lifecycle role to assign")
    @app_commands.choices(role=[app_commands.Choice(name=r, value=r) for r in LIFECYCLE_ROLES])
    async def set_lifecycle_role(self, interaction: discord.Interaction, member: discord.Member, role: app_commands.Choice[str]) -> None:
        profile = self.bot.db.fetch_user_profile(str(member.id))
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{member.mention} hasn’t linked an Albion character yet."),
                ephemeral=True,
            )
            return

        # Remove any existing lifecycle role
        old_role_name = profile.get("lifecycle_role")
        if old_role_name:
            old_role = discord.utils.get(interaction.guild.roles, name=old_role_name)
            if old_role and old_role in member.roles:
                await member.remove_roles(old_role)

        # Assign the new lifecycle role
        new_role = discord.utils.get(interaction.guild.roles, name=role.value)
        if new_role:
            await member.add_roles(new_role)
        if role.value in ("Inactive", "Alumni"):
            tu_role = discord.utils.get(interaction.guild.roles, name="HomeGuild")
            if tu_role and tu_role in member.roles:
                await member.remove_roles(
                    tu_role,
                    reason=f"Manual lifecycle -> {role.value}",
                )

        self.bot.db.set_lifecycle_role(str(member.id), role.value)
        # If admin assigns a TU-earned role (or Alumni), mark them as having
        # been in the home guild so future reconciles demote to Alumni instead
        # of Guest.
        if role.value in ("Recruit", "Member", "Veteran", "Alumni"):
            self.bot.db.set_was_in_home_guild(str(member.id), True)

        await interaction.response.send_message(
            embed=success_embed(
                "Lifecycle role updated",
                f"Set {member.mention}'s lifecycle role to **{role.value}**.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set lifecycle role for {member} to {role.value}.")

    @app_commands.command(
        name="relink-character",
        description="Re-link a member's Discord account to a different Albion character (officer fix).",
    )
    @app_commands.describe(
        member="Discord member whose linked Albion character is wrong.",
        albion_name="Exact in-game character name to link instead.",
    )
    async def relink_character(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        albion_name: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        albion_name = albion_name.strip()
        if not albion_name:
            await interaction.followup.send(
                embed=error_embed("Bad input", "Albion name cannot be empty."),
                ephemeral=True,
            )
            return

        # Look up the character on the live API. Off-loop because
        # albion_api uses blocking `requests`.
        lookup = await asyncio.to_thread(albion_api.get_player_id, albion_name)
        if not lookup:
            await interaction.followup.send(
                embed=error_embed(
                    "Character not found",
                    f"The Albion API has no player named **{albion_name}**. "
                    "Check the spelling exactly.",
                ),
                ephemeral=True,
            )
            return
        player_id, resolved_name = lookup
        raw = await asyncio.to_thread(albion_api.get_player_stats, player_id)
        stats = albion_api.parse_stats(raw) if raw else {}

        # Make sure this player_id isn't already linked to a different Discord user.
        db = self.bot.db  # type: ignore[attr-defined]
        existing = db.cursor.execute(
            "SELECT discord_id, username FROM user_profiles "
            "WHERE albion_player_id = ? AND discord_id <> ?",
            (player_id, str(member.id)),
        ).fetchone()
        if existing:
            other_id = existing[0]
            other_name = existing[1] or other_id
            await interaction.followup.send(
                embed=error_embed(
                    "Already linked",
                    f"Character **{resolved_name}** is already linked to "
                    f"<@{other_id}> ({other_name}). Unlink that profile first.",
                ),
                ephemeral=True,
            )
            return

        # Make sure the target Discord user actually has a profile.
        profile = db.fetch_user_profile(str(member.id))
        if not profile:
            await interaction.followup.send(
                embed=error_embed(
                    "No profile",
                    f"{member.mention} has no profile yet. They need to click the "
                    "**Register** button in the registration channel at least once before officers can re-link.",
                ),
                ephemeral=True,
            )
            return

        old_albion_name = profile.get("albion_name") or "(none)"
        old_player_id = profile.get("albion_player_id") or "(none)"

        # Apply the swap. update_user_albion_info also overwrites guild/alliance/fame.
        db.update_user_albion_info(str(member.id), player_id, resolved_name, stats)

        # Trigger an immediate per-profile reconcile so roles/nicks update without
        # waiting for the next sync loop tick.
        events_cog = self.bot.get_cog("Events")
        try:
            if events_cog is not None:
                refreshed = db.fetch_user_profile(str(member.id))
                if refreshed:
                    await events_cog._reconcile_member_state(
                        refreshed,
                        stats.get("guild_name"),
                        stats.get("albion_name") or resolved_name,
                    )
        except Exception as exc:  # noqa: BLE001
            error_log(f"relink_character: post-relink reconcile failed: {exc!r}")

        # Auto-enqueue a link audit when the API gave us nothing for the
        # new character. Often happens for fresh joiners — the public
        # endpoint can lag hours-to-a-day after a guild change. The audit
        # loop will recheck daily and confirm/alert once the API catches
        # up so the officer doesn't have to remember.
        api_guild_empty = not (stats.get("guild_name") or "").strip()
        if api_guild_empty:
            try:
                from cogs.link_audit import enqueue_link_audit
                home_guild = (db.get_config("home_guild_name") or "").strip()
                if home_guild:
                    enqueue_link_audit(
                        db,
                        discord_id=str(member.id),
                        expected_guild=home_guild,
                        expected_player_id=player_id,
                        reason=f"relink-character by {interaction.user}",
                        requested_by=str(interaction.user.id),
                        max_checks=7,
                    )
            except Exception as exc:  # noqa: BLE001
                error_log(f"relink_character: enqueue_link_audit failed: {exc!r}")

        guild_name = stats.get("guild_name") or "(no guild)"
        await interaction.followup.send(
            embed=success_embed(
                "Character relinked",
                (
                    f"**{member.mention}**\n"
                    f"• Old: `{old_albion_name}` (`{old_player_id}`)\n"
                    f"• New: `{resolved_name}` (`{player_id}`)\n"
                    f"• Current guild per API: **{guild_name}**\n\n"
                    "Roles & nickname have been reconciled. If they're still in the wrong "
                    "lifecycle, run **/admin sync-now** or wait for the next loop tick."
                ),
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} relinked {member} ({member.id}): "
            f"{old_albion_name} ({old_player_id}) -> {resolved_name} ({player_id}); "
            f"API guild: {guild_name!r}."
        )

    @app_commands.command(
        name="set-tu-history",
        description="Mark whether a member has ever been in the home guild (controls Alumni vs Guest demotion).",
    )
    @app_commands.describe(
        member="The member to update",
        was_in_home_guild="True = treat as ex-member (demote to Alumni). False = never was a member (demote to Guest).",
    )
    async def set_tu_history(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        was_in_home_guild: bool,
    ) -> None:
        profile = self.bot.db.fetch_user_profile(str(member.id))
        if not profile or not profile.get("albion_player_id"):
            await interaction.response.send_message(
                embed=error_embed("Not registered", f"{member.mention} is not registered."),
                ephemeral=True,
            )
            return
        self.bot.db.set_was_in_home_guild(str(member.id), was_in_home_guild)
        label = "ex-member (Alumni track)" if was_in_home_guild else "guest (Guest track)"
        await interaction.response.send_message(
            embed=success_embed(
                "TU history updated",
                f"{member.mention} is now flagged as **{label}**. "
                f"Run `/admin reconcile-now` to apply lifecycle changes.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set was_in_home_guild={was_in_home_guild} for {member}.")

    @app_commands.command(
        name="tu-history",
        description="Show every profile flagged as having been in the home guild (members + Alumni).",
    )
    async def tu_history(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = self.bot.db.fetch_tu_history()
        if not rows:
            await interaction.followup.send(
                embed=error_embed("Nothing tracked yet", "No profiles are flagged as having been in the home guild."),
                ephemeral=True,
            )
            return

        # Bucket by lifecycle role for readability.
        from collections import defaultdict
        buckets: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            bucket = r.get("lifecycle_role") or "(none)"
            display = f"<@{r['discord_id']}> — {r.get('albion_name') or r.get('username') or '?'}"
            buckets[bucket].append(display)

        order = ["Veteran", "Member", "Recruit", "Probationary", "Inactive", "Alumni", "Guest", "(none)"]
        lines = [f"**{len(rows)} profiles** have been in the home guild:\n"]
        for role_name in order:
            if role_name not in buckets:
                continue
            entries = buckets[role_name]
            lines.append(f"__**{role_name}** ({len(entries)})__")
            # Cap each bucket so we don't blow past Discord's 2000-char limit.
            for entry in entries[:25]:
                lines.append(f"• {entry}")
            if len(entries) > 25:
                lines.append(f"• …and {len(entries) - 25} more")
            lines.append("")

        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "\n…(truncated)"
        await interaction.followup.send(body, ephemeral=True)

    @app_commands.command(name="set-inactivity-days", description="Set inactivity thresholds before a member is marked Inactive.")
    @app_commands.describe(
        days="Days without Albion stat movement before Inactive can apply (1–365).",
        vc_days="Optional: days without joining guild VC before Inactive can apply.",
        mode="Optional: either stale signal demotes, or both signals must be stale.",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Either VC or stat stale", value="either"),
        app_commands.Choice(name="Both VC and stat stale", value="both"),
    ])
    async def set_inactivity_days(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 365],
        vc_days: app_commands.Range[int, 1, 365] | None = None,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        self.bot.db.set_config("inactivity_days", str(days))
        self.bot.db.set_config(CFG_LIFECYCLE_STAT_INACTIVITY_DAYS, str(days))
        if vc_days is not None:
            self.bot.db.set_config(CFG_LIFECYCLE_VC_INACTIVITY_DAYS, str(int(vc_days)))
        clean_mode = (mode.value if mode else "").strip().lower()
        if clean_mode in {"either", "both"}:
            self.bot.db.set_config(CFG_LIFECYCLE_INACTIVITY_MODE, clean_mode)
        current_vc_days = (
            str(int(vc_days))
            if vc_days is not None else
            str(self.bot.db.get_config(CFG_LIFECYCLE_VC_INACTIVITY_DAYS) or DEFAULT_LIFECYCLE_VC_INACTIVITY_DAYS)
        )
        current_mode = (
            clean_mode
            if clean_mode in {"either", "both"} else
            str(self.bot.db.get_config(CFG_LIFECYCLE_INACTIVITY_MODE) or DEFAULT_LIFECYCLE_INACTIVITY_MODE)
        )
        mode_text = (
            "either stale signal can demote"
            if current_mode == "either" else
            "both signals must be stale"
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Lifecycle inactivity updated",
                (
                    f"VC threshold: **{current_vc_days} day(s)** without joining voice.\n"
                    f"Stat threshold: **{int(days)} day(s)** without Albion stat movement.\n"
                    f"Mode: **{current_mode}** ({mode_text}).\n"
                    "Takes effect on the next hourly lifecycle sync."
                ),
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} set lifecycle inactivity: "
            f"stat={days} vc={current_vc_days} mode={current_mode}"
        )

    @app_commands.command(name="set-lifecycle-thresholds", description="Set how many days in each lifecycle phase before auto-promotion.")
    @app_commands.describe(
        probationary_days="Days as Probationary before becoming Member (default: 30, range 1–365).",
        member_days="Days as Member before becoming Veteran (default: 90, range 1–730).",
    )
    async def set_lifecycle_thresholds(
        self,
        interaction: discord.Interaction,
        probationary_days: app_commands.Range[int, 1, 365],
        member_days: app_commands.Range[int, 1, 730],
    ) -> None:
        if probationary_days >= member_days:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid thresholds",
                    f"Probationary days (**{probationary_days}**) must be less than Member days (**{member_days}**).",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_config("probationary_days", str(probationary_days))
        self.bot.db.set_config("member_days", str(member_days))
        await interaction.response.send_message(
            embed=success_embed(
                "Lifecycle thresholds updated",
                f"• **Probationary → Member:** {probationary_days} days\n"
                f"• **Member → Veteran:** {member_days} days\n\n"
                f"Takes effect on the next hourly sync.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set lifecycle thresholds: probationary={probationary_days}, member={member_days}.")

    @app_commands.command(name="assign-role", description="Grant or revoke a staff role for a member.")
    @app_commands.describe(member="The member to update", role="The staff role to grant or revoke", action="Grant or revoke the role")
    @app_commands.choices(
        role=[app_commands.Choice(name=r, value=r) for r in STAFF_ROLES],
        action=[
            app_commands.Choice(name="Grant", value="grant"),
            app_commands.Choice(name="Revoke", value="revoke"),
        ]
    )
    async def assign_role(self, interaction: discord.Interaction, member: discord.Member, role: app_commands.Choice[str], action: app_commands.Choice[str]) -> None:
        role_obj = discord.utils.get(interaction.guild.roles, name=role.value)
        if not role_obj:
            await interaction.response.send_message(
                embed=error_embed(
                    "Role not found",
                    f"Role **{role.value}** does not exist in this guild.",
                    hint="Run `/admin setup-roles` to create the standard role set.",
                ),
                ephemeral=True,
            )
            return

        if action.value == "grant":
            if role_obj in member.roles:
                await interaction.response.send_message(
                    embed=info_embed("Already granted", f"{member.mention} already has **{role.value}**."),
                    ephemeral=True,
                )
                return
            await member.add_roles(role_obj, reason=f"Assigned by {interaction.user}")
            await interaction.response.send_message(
                embed=success_embed("Role granted", f"Granted **{role.value}** to {member.mention}."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} granted {role.value} to {member}.")
        else:
            if role_obj not in member.roles:
                await interaction.response.send_message(
                    embed=info_embed("Already revoked", f"{member.mention} does not have **{role.value}**."),
                    ephemeral=True,
                )
                return
            await member.remove_roles(role_obj, reason=f"Revoked by {interaction.user}")
            await interaction.response.send_message(
                embed=success_embed("Role revoked", f"Revoked **{role.value}** from {member.mention}."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} revoked {role.value} from {member}.")

    @app_commands.command(name="sync-now", description="Manually trigger the hourly sync loop right now.")
    async def sync_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        events_cog = self.bot.get_cog("Events")
        if events_cog is None:
            await interaction.followup.send(
                embed=error_embed("Events cog not loaded", "The sync routine is unavailable."),
                ephemeral=True,
            )
            return
        try:
            await events_cog.force_sync_now()
            await interaction.followup.send(
                embed=success_embed("Sync complete", "All registered members and guilds were refreshed."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} triggered manual sync-now.")
        except Exception as e:
            await interaction.followup.send(
                embed=error_embed("Sync failed", f"`{e}`"),
                ephemeral=True,
            )

    @app_commands.command(name="auto-sync", description="Auto-sync all members whose nickname starts with an alliance tag.")
    async def auto_sync(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        confirmed = await confirm_action(
            interaction,
            title="Run auto-sync now?",
            description=(
                "This will scan every member with a `[TAG] <name>` nickname who isn't already "
                "linked, and try to register them via the Albion API.\n\n"
                "Existing registrations will not be touched."
            ),
            confirm_label="Run auto-sync",
            danger=False,
        )
        if not confirmed:
            return
        role_cache = {r.name: r for r in guild.roles}

        candidates = []
        for member in guild.members:
            if member.bot:
                continue
            existing = self.bot.db.fetch_user_profile(str(member.id))
            if existing and existing.get("albion_player_id"):
                continue  # already synced
            display = member.nick or member.display_name or ""
            albion_name = extract_tagged_nickname_name(display)
            if not albion_name:
                continue
            candidates.append((member, albion_name))

        if not candidates:
            await interaction.followup.send(
                embed=info_embed(
                    "Nothing to sync",
                    "No unsynced members with `[TAG] <name>` nicknames were found.",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Found **{len(candidates)}** member(s) to sync. Working...", ephemeral=True
        )

        synced, failed = 0, []
        for member, albion_name in candidates:
            try:
                ok, msg = await sync_member_to_albion(self.bot, member, albion_name, role_cache)
                if ok:
                    synced += 1
                else:
                    failed.append(f"{member.display_name}: {msg}")
            except Exception as e:
                failed.append(f"{member.display_name}: {e}")

        report = f"✅ Synced **{synced}/{len(candidates)}** members."
        if failed:
            report += f"\n❌ **{len(failed)}** failed — see attached `auto_sync_failures.txt` for the full list."
            report += "\n\n**First few failures:**\n" + "\n".join(f"• {f}" for f in failed[:10])
            buf = io.StringIO("\n".join(failed))
            file = discord.File(fp=io.BytesIO(buf.getvalue().encode("utf-8")), filename="auto_sync_failures.txt")
            await interaction.followup.send(report, file=file, ephemeral=True)
        else:
            await interaction.followup.send(report, ephemeral=True)
        info_log(f"{interaction.user} ran auto-sync: {synced} synced, {len(failed)} failed.")

    @app_commands.command(
        name="reconcile-now",
        description="Force-run lifecycle/role reconciliation immediately.",
    )
    @app_commands.describe(
        refresh_api="Also refresh each player's guild info from the Albion API first (slower but catches recent leavers).",
    )
    async def reconcile_now(self, interaction: discord.Interaction, refresh_api: bool = True):
        await interaction.response.defer(ephemeral=True, thinking=True)
        events_cog = self.bot.get_cog("Events")
        if not events_cog:
            await interaction.followup.send(
                embed=error_embed("Events cog not loaded", "Cannot run reconciliation."),
                ephemeral=True,
            )
            return

        import asyncio as _asyncio
        import albion_api as _albion_api

        try:
            profiles = self.bot.db.fetch_all_registered_profiles()

            # Optionally refresh stored guild_name from the Albion API in parallel.
            # This catches members who left the in-game guild since the last hourly sync.
            refreshed = 0
            if refresh_api:
                loop = _asyncio.get_running_loop()
                async def _refresh_one(p):
                    try:
                        data = await loop.run_in_executor(
                            None, lambda: _albion_api.get_player_stats(p["albion_player_id"], timeout=10.0)
                        )
                        if data:
                            stats = _albion_api.parse_stats(data)
                            self.bot.db.update_user_albion_info(
                                p["discord_id"], p["albion_player_id"], p["albion_name"], stats
                            )
                            p["guild_name"] = stats.get("guild_name")
                            p["albion_name"] = stats.get("albion_name", p["albion_name"])
                            return 1
                    except Exception:
                        pass
                    return 0
                # Run up to 10 API calls concurrently to keep total time bounded.
                semaphore = _asyncio.Semaphore(10)
                async def _bounded(p):
                    async with semaphore:
                        return await _refresh_one(p)
                results = await _asyncio.gather(*(_bounded(p) for p in profiles))
                refreshed = sum(results)

            # Run reconciliation against (now-fresh) DB state.
            reconciled: set[str] = set()
            for profile in profiles:
                await events_cog._reconcile_member_state(
                    profile, profile.get("guild_name"), profile.get("albion_name")
                )
                reconciled.add(str(profile["discord_id"]))
            await events_cog._sweep_orphan_nickname_tags(reconciled)
            promoted = await events_cog._auto_promote_lifecycle()
        except Exception as exc:  # noqa: BLE001
            error_log(f"reconcile-members failed: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Reconciliation failed",
                    "Something went wrong while reconciling members. "
                    "Check the bot log for details.",
                ),
                ephemeral=True,
            )
            return

        msg = (
            f"✅ Reconciled **{len(reconciled)}** registered profiles + swept orphan nickname tags.\n"
            f"Lifecycle promotions applied: **{promoted}**."
        )
        if refresh_api:
            msg += f"\nRefreshed **{refreshed}/{len(profiles)}** from Albion API first."
        await interaction.followup.send(msg, ephemeral=True)
        info_log(f"{interaction.user} ran /admin reconcile-now (refresh_api={refresh_api}).")

    @app_commands.command(name="config-list", description="Show every key/value in guild_config.")
    @app_commands.describe(filter="Optional substring filter on key names (case-insensitive).")
    async def config_list(
        self,
        interaction: discord.Interaction,
        filter: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        db = self.bot.db
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT key, value FROM guild_config ORDER BY key ASC"
            )
            rows = [dict(r) for r in db.cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("DB error", f"Could not read guild_config: `{exc}`"),
                ephemeral=True,
            )
            return

        if filter:
            needle = filter.lower()
            rows = [r for r in rows if needle in r["key"].lower()]

        if not rows:
            await interaction.followup.send(
                embed=info_embed("No config entries", "Nothing matches that filter." if filter else "guild_config is empty."),
                ephemeral=True,
            )
            return

        # Mask anything that *looks* like a secret/token in case someone ever stuffs one in.
        def _fmt_value(key: str, value: str) -> str:
            lk = key.lower()
            if any(w in lk for w in ("token", "secret", "key", "password")):
                return "•" * 8 + " (masked)"
            # Channel/role IDs (numeric and ends with _id) — render as mention if possible.
            if lk.endswith("_channel_id") and value.isdigit():
                return f"<#{value}>"
            if lk.endswith("_role_id") and value.isdigit():
                return f"<@&{value}>"
            # Truncate huge values.
            if len(value) > 80:
                return f"{value[:77]}…"
            return f"`{value}`"

        # Chunk into multiple embeds if needed (Discord 6000-char field cap).
        chunks: list[list[str]] = [[]]
        chunk_chars = 0
        CHUNK_LIMIT = 3500  # safe under field/embed limits
        for r in rows:
            line = f"**{r['key']}** — {_fmt_value(r['key'], r['value'] or '')}"
            if chunk_chars + len(line) > CHUNK_LIMIT and chunks[-1]:
                chunks.append([])
                chunk_chars = 0
            chunks[-1].append(line)
            chunk_chars += len(line) + 1

        title = "Guild config" + (f" (filter: {filter!r})" if filter else "")
        for i, lines in enumerate(chunks):
            embed = info_embed(
                f"{title} ({i+1}/{len(chunks)})" if len(chunks) > 1 else title,
                "\n".join(lines) or "_(empty)_",
            )
            embed.set_footer(text=f"{len(rows)} entr{'y' if len(rows)==1 else 'ies'}")
            await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(f"{interaction.user} ran /admin config-list (filter={filter!r}, n={len(rows)}).")

    @app_commands.command(name="health", description="One-glance bot/system health dashboard.")
    async def health(self, interaction: discord.Interaction) -> None:
        from pathlib import Path

        await interaction.response.defer(ephemeral=True)
        db = self.bot.db
        guild = interaction.guild

        # ── DB stats ─────────────────────────────────────────────────────
        db_path = Path("data/database.db")
        db_size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0
        try:
            db.cursor.execute("SELECT COUNT(*) AS n FROM user_profiles")
            profile_count = int(db.cursor.fetchone()["n"])
            db.cursor.execute("SELECT COUNT(*) AS n FROM player_stats_history")
            psh_count = int(db.cursor.fetchone()["n"])
            db.cursor.execute("SELECT MIN(recorded_at) AS oldest, MAX(recorded_at) AS newest FROM player_stats_history")
            row = db.cursor.fetchone()
            oldest_stat = row["oldest"] or "—"
            newest_stat = row["newest"] or "—"
        except Exception:  # noqa: BLE001
            profile_count = psh_count = 0
            oldest_stat = newest_stat = "error"

        # ── Active workload counts ───────────────────────────────────────
        def _count(sql: str, params=()) -> int:
            try:
                db.cursor.execute(sql, params)
                row = db.cursor.fetchone()
                return int(row[0] if row else 0)
            except Exception:  # noqa: BLE001
                return -1

        bounties_active = _count(
            "SELECT COUNT(*) FROM bounties WHERE status IN ('open','claimed','submitted')"
        )
        bounties_pending = _count(
            "SELECT COUNT(*) FROM bounties WHERE status='pending'"
        )
        help_tickets_active = _count(
            "SELECT COUNT(*) FROM help_tickets WHERE status IN ('open','claimed')"
        )
        lfg_upcoming = _count(
            "SELECT COUNT(*) FROM lfg_events WHERE status='scheduled'"
        )
        regear_pending = _count(
            "SELECT COUNT(*) FROM regear_requests WHERE status='pending'"
        )

        # ── Backups ──────────────────────────────────────────────────────
        backup_dir = Path("data/backups")
        if backup_dir.exists():
            backups = sorted(backup_dir.glob("db-*.db"))
            backup_count = len(backups)
            latest_backup = backups[-1].name if backups else "—"
        else:
            backup_count = 0
            latest_backup = "(none yet)"

        # ── Configured channels ──────────────────────────────────────────
        channel_keys = [
            ("Welcome",        "welcome_channel_id"),
            ("Goodbye",        "goodbye_channel_id"),
            ("Officer review", "officer_channel_id"),
            ("Points feed",    "points_announce_channel_id"),
            ("Hall of Fame",   "automation_hall_of_fame_channel_id"),
            ("Announcements",  "automation_announcements_channel_id"),
            ("Help",           "help_channel_id"),
            ("Help review",    "help_review_channel_id"),
            ("Bounty board",   "bounty_board_channel_id"),
            ("Bounty review",  "bounty_review_channel_id"),
        ]
        channel_lines: list[str] = []
        for label, key in channel_keys:
            cid = db.get_config(key)
            if not cid:
                channel_lines.append(f"❌ **{label}:** unset")
                continue
            ch = guild.get_channel(int(cid)) if guild else None
            if isinstance(ch, discord.TextChannel):
                me = guild.me if guild else None
                perms = ch.permissions_for(me) if me else None
                ok = perms and perms.send_messages and perms.embed_links
                channel_lines.append(f"{'✅' if ok else '⚠️'} **{label}:** {ch.mention}")
            else:
                channel_lines.append(f"⚠️ **{label}:** id `{cid}` not found")

        # ── System ───────────────────────────────────────────────────────
        latency_ms = int(self.bot.latency * 1000) if self.bot.latency else -1
        guild_member_count = guild.member_count if guild else 0

        embed = discord.Embed(
            title="🩺 Bot Health Dashboard",
            color=discord.Color.green() if latency_ms < 300 else discord.Color.orange(),
        )
        embed.add_field(
            name="System",
            value=(
                f"Latency: **{latency_ms} ms**\n"
                f"Members: **{guild_member_count}**\n"
                f"Registered profiles: **{profile_count}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Workload",
            value=(
                f"Active bounties: **{bounties_active}**\n"
                f"Pending bounties: **{bounties_pending}**\n"
                f"Open help tickets: **{help_tickets_active}**\n"
                f"Upcoming LFG: **{lfg_upcoming}**\n"
                f"Pending regears: **{regear_pending}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Database",
            value=(
                f"Size: **{db_size_mb:.2f} MB**\n"
                f"Stat rows: **{psh_count:,}**\n"
                f"Oldest: `{oldest_stat}`\n"
                f"Newest: `{newest_stat}`\n"
                f"Backups: **{backup_count}** (latest: `{latest_backup}`)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Channels",
            value="\n".join(channel_lines) or "No channels configured.",
            inline=False,
        )
        embed.set_footer(text=f"Requested by {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=True)
        info_log(f"{interaction.user} ran /admin health.")

    @app_commands.command(name="db-backup", description="Manually trigger a database backup right now.")
    async def db_backup(self, interaction: discord.Interaction) -> None:
        import datetime
        from pathlib import Path
        import sqlite3

        await interaction.response.defer(ephemeral=True)
        backup_dir = Path("data/backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now_naive().strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"db-{stamp}-manual.db"
        try:
            src_conn = self.bot.db.connection
            if src_conn is None:
                await interaction.followup.send(
                    embed=error_embed("Backup failed", "Database connection is closed."),
                    ephemeral=True,
                )
                return
            with sqlite3.connect(str(dest)) as bck:
                src_conn.backup(bck)
            size_mb = dest.stat().st_size / (1024 * 1024)
            await interaction.followup.send(
                embed=success_embed(
                    "Backup written",
                    f"Saved `{dest.name}` ({size_mb:.2f} MB) to `{backup_dir}`.",
                ),
                ephemeral=True,
            )
            info_log(f"{interaction.user} triggered manual DB backup → {dest.name}.")
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(
                embed=error_embed("Backup failed", f"`{exc!r}`"),
                ephemeral=True,
            )

# /audit group lives in a sibling module to keep this file under control.
from cogs._admin_audit import AuditGroup


class Admin(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.bot.tree.add_command(AdminGroup(bot))
        self.bot.tree.add_command(AuditGroup(bot))
        info_log(f"Initialized {self.__class__.__name__} cog.")

    @app_commands.command(
        name="ping-shotcallers",
        description="Ping Shotcallers + Senior Shotcallers to claim an open timer.",
    )
    @app_commands.describe(
        reason="Short note shown with the ping (e.g. 'open 20:00 slot, who's leading?').",
        slot="Optional: which prime-time slot needs a claimer.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def ping_shotcallers(
        self,
        interaction: discord.Interaction,
        reason: app_commands.Range[str, 1, 400],
        slot: str | None = None,
    ) -> None:
        """Ping the Shotcaller and Senior Shotcaller roles asking them to claim a slot."""
        import datetime as _dt

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this in a guild."),
                ephemeral=True,
            )
            return

        sc_role = discord.utils.get(guild.roles, name="Shotcaller")
        ssc_role = discord.utils.get(guild.roles, name="Senior Shotcaller")
        targets = [r for r in (sc_role, ssc_role) if r is not None]
        if not targets:
            await interaction.response.send_message(
                embed=error_embed(
                    "Roles missing",
                    "Neither **Shotcaller** nor **Senior Shotcaller** roles exist. "
                    "Run `/admin setup-roles` first.",
                ),
                ephemeral=True,
            )
            return

        mentions = " ".join(r.mention for r in targets)
        embed = discord.Embed(
            title="\ud83d\udce3 Shotcallers \u2014 timer needs a claimer",
            description=reason.strip(),
            color=discord.Color.orange(),
            timestamp=_dt.datetime.now(_dt.timezone.utc),
        )
        if slot:
            embed.add_field(name="Slot", value=slot.strip()[:200], inline=False)
        embed.add_field(
            name="How to claim",
            value=(
                "Open the event board (`/lfg post-board` if missing) and click the "
                "prime-time slot button to create the event. Post the comp + IP floor, "
                "then ping content roles for signups."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Requested by {interaction.user.display_name}")

        allowed = discord.AllowedMentions(roles=targets, everyone=False, users=False)
        try:
            await interaction.channel.send(
                content=mentions,
                embed=embed,
                allowed_mentions=allowed,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post here",
                    "I don't have permission to post or mention roles in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"ping-shotcallers failed: {exc!s}")
            await interaction.response.send_message(
                embed=error_embed("Failed to post", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                "Shotcallers pinged",
                f"Pinged {', '.join(r.mention for r in targets)}.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} pinged shotcallers in #{interaction.channel} "
            f"(slot={slot!r}, reason={reason[:60]!r})."
        )

    @app_commands.command(
        name="setup-regear-policy",
        description="Post the branded Regear Policy embed in the current channel.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def setup_regear_policy(self, interaction: discord.Interaction) -> None:
        """Top-level slash command. Lives outside `/admin` because that group
        is already at Discord's 25-subcommand cap. Posts the public regear
        policy as a branded embed that can be pinned beside the regear board.
        """
        db = self.bot.db
        from cogs.announcements import (
            CFG_COLOR_HEX,
            CFG_CREST_URL,
            CFG_FOOTER_NAME,
            DEFAULT_FOOTER,
            _build_announcement_embed,
            _parse_color,
        )

        body = (
            "**Lost approved gear during guild content? Submit a regear request.**\n\n"
            "Regear exists to keep members showing up to content without one bad death "
            "knocking them out for the week. It is not a blank cheque: bring the right "
            "build, follow calls, and include proof so officers can process requests quickly.\n\n"
            "**Covered Losses**\n"
            "Eligible losses usually include:\n"
            "• Guild-called LFG events\n"
            "• CTA / objective fights\n"
            "• Officer-approved faction warfare or guild content\n"
            "• Assigned comp roles where the member followed the shotcaller\n\n"
            "Officers may approve edge cases when the loss clearly helped the guild.\n\n"
            "**Not Covered**\n"
            "Regear can be denied for:\n"
            "• Solo deaths or personal farming\n"
            "• Unapproved expensive swaps\n"
            "• Ignoring calls, splitting, or chasing\n"
            "• Missing proof or unclear death context\n"
            "• Meme builds, troll gear, or avoidable transport losses\n\n"
            "**What To Submit**\n"
            "Use the regear board and include:\n"
            "• Content type / event context\n"
            "• Shotcaller or officer present\n"
            "• What happened\n"
            "• Estimated silver value\n"
            "• Screenshot or killboard proof\n\n"
            "Use **Regear from Death** when available so the bot can pre-fill the loss.\n\n"
            "**Review Flow**\n"
            "1. Member submits from the regear board.\n"
            "2. Officers review the request in the regear review channel.\n"
            "3. Approved requests are credited or supplied from stockpile when possible.\n"
            "4. Denied requests receive a reason by DM.\n\n"
            "**Stockpile First**\n"
            "If the guild chest has the needed items, officers may satisfy the regear from "
            "stock instead of silver. Low-stock warnings may be posted after approvals so "
            "crafters and logistics can refill the missing pieces.\n\n"
            "**Payout Standard**\n"
            "Payouts are based on officer review, available funds, stockpile state, and "
            "whether the submitted build matched the event. Officers can reduce or deny "
            "payouts for overgearing, wrong builds, missing consumables, or preventable deaths.\n\n"
            "**Member Expectations**\n"
            "To stay eligible:\n"
            "• Be in voice for called content\n"
            "• Bring the assigned build, food, and potions\n"
            "• Follow the shotcaller\n"
            "• Keep proof ready after death\n"
            "• Submit honestly and without duplicate claims\n\n"
            "**Final Rule**\n"
            "**Ask before you risk something unusual.** If you are unsure whether a set, "
            "swap, or activity is covered, get officer approval before the fight."
        )

        embed = _build_announcement_embed(
            title="🛡️ Regear Policy — Guild Loss Support",
            body=body,
            color=_parse_color(db.get_config(CFG_COLOR_HEX)),
            crest_url=(db.get_config(CFG_CREST_URL) or "").strip() or None,
            footer_text=(db.get_config(CFG_FOOTER_NAME) or DEFAULT_FOOTER).strip(),
            officer=interaction.user,
        )

        try:
            await interaction.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post here",
                    "I don't have permission to post in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"setup-regear-policy failed: {exc!s}")
            await interaction.response.send_message(
                embed=error_embed("Failed to post", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                "Regear policy posted",
                "The branded policy embed is up. Pin it near the regear board.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} posted regear policy in #{interaction.channel}.")

    @app_commands.command(
        name="officer-cheatsheet",
        description="Post the officer quick-reference: every officer-only command grouped by purpose.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def officer_cheatsheet(self, interaction: discord.Interaction) -> None:
        """Top-level slash command. Lives outside `/admin` because that
        group is already at Discord's 25-subcommand cap. Posts a branded
        cheatsheet of officer-facing commands in the current channel.
        """
        db = self.bot.db
        crest_url = (db.get_config("announce_crest_url") or "").strip() or None
        color_hex = (db.get_config("announce_color_hex") or "#d4af37").strip()
        if not color_hex.startswith("#"):
            color_hex = "#" + color_hex
        try:
            color = discord.Color.from_str(color_hex)
        except ValueError:
            color = discord.Color.from_str("#d4af37")
        footer_name = (db.get_config("announce_footer_name") or "Officer SOP").strip()

        embed = discord.Embed(
            title="\ud83d\udcd8 Officer Cheatsheet \u2014 Quick Command Reference",
            description=(
                "**Officer-only index.** Every command an officer regularly "
                "needs, grouped by purpose. Pin this so you never have to "
                "hunt through `/` menus mid-incident.\n"
                "Most of these require **Manage Guild** or higher; if you "
                "don't see one, your role doesn't have access \u2014 escalate."
            ),
            color=color,
        )

        embed.add_field(
            name="\ud83d\udc65 Members & Lifecycle",
            value=(
                "\u2022 `/admin set-lifecycle-role` \u2014 force a member to a specific lifecycle role.\n"
                "\u2022 `/admin set-lifecycle-thresholds` \u2014 change the Probationary / Member / Veteran day cutoffs.\n"
                "\u2022 `/admin set-inactivity-days` \u2014 change the no-activity window before **Inactive** triggers.\n"
                "\u2022 `/admin assign-role` \u2014 grant or revoke a staff role for a member.\n"
                "\u2022 `/admin deregister` \u2014 wipe a user's registration and reset their profile (use carefully)."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udd04 Sync & Verification",
            value=(
                "\u2022 `/admin sync-now` \u2014 force the hourly guild-sync loop to run immediately.\n"
                "\u2022 `/admin auto-sync` \u2014 sync everyone whose nickname matches `[TAG] <name>` in one pass.\n"
                "\u2022 `/admin add-guild` \u2014 register / update a guild in the database."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udce2 Announcements & Posts",
            value=(
                "\u2022 `/announce post` \u2014 post a branded guild announcement.\n"
                "\u2022 `/announce config` \u2014 set crest, color, footer used everywhere.\n"
                "\u2022 `/admin setup-registration` \u2014 (re)post the trilingual register panel.\n"
                "\u2022 `/admin setup-shotcaller-sop` \u2014 post the One Voice Doctrine.\n"
                "\u2022 `/setup-regear-policy` \u2014 post the public regear policy.\n"
                "\u2022 `/admin setup-timer-claims` \u2014 post the Timer Claim System + live slots.\n"
                "\u2022 `/admin setup-officer-lifecycle-guide` \u2014 post the lifecycle-roles SOP."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udce3 Pings & Coordination",
            value=(
                "\u2022 `/ping-shotcallers` \u2014 ping every shotcaller (optional slot + reason).\n"
                "\u2022 `/lfg post-board` \u2014 post the event board with claimable prime-time buttons.\n"
                "\u2022 `/event close` \u2014 close out a claimed event so attendance + payout lock in."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83c\udfc6 Points, Loot, Regear",
            value=(
                "\u2022 `/points award` / `/points revoke` \u2014 adjust a member's points with an audit reason.\n"
                "\u2022 `/points history` \u2014 review recent point activity (use before any reversal).\n"
                "\u2022 `/setup-regear-policy` \u2014 post the policy beside the regear board.\n"
                "\u2022 `/regear approve` / `/regear deny` \u2014 process regear requests from the queue."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83e\udd16 Automation & Health",
            value=(
                "\u2022 `/automation inactive-list` \u2014 current members over the inactivity threshold.\n"
                "\u2022 `/automation dashboard` \u2014 snapshot of recruitment, retention, roster health.\n"
                "\u2022 `/graph dashboard variant:recruitment` \u2014 chart view of the same data."
            ),
            inline=False,
        )
        embed.add_field(
            name="\u2699\ufe0f Channel & Config",
            value=(
                "\u2022 `/admin set-channel purpose:` \u2014 wire `officer`, `welcome`, `goodbye`, `points`, `hof`, `announcements`, `sso-routes`.\n"
                "\u2022 `/admin setup-roles` \u2014 create all required roles and audit every member.\n"
                "\u2022 `/admin set-roles` \u2014 set the unverified / verified base roles."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83e\uddef Staff Applications",
            value=(
                "\u2022 `/staff apply` \u2014 (members) submit an application; lands in the officer review channel.\n"
                "\u2022 `/staff config` \u2014 override per-rank caps / ratios / prereqs at runtime.\n"
                "\u2022 `/staff board` \u2014 post / refresh the public openings board."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83d\udd14 House Rules",
            value=(
                "\u2022 Anything that mutates roles or points leaves an audit log \u2014 use a real reason string.\n"
                "\u2022 If you don't know what a command does, run it on yourself or a test account first.\n"
                "\u2022 When the bot is down, see `/admin setup-officer-lifecycle-guide` for the manual fallback."
            ),
            inline=False,
        )

        if crest_url:
            embed.set_thumbnail(url=crest_url)
            embed.set_footer(
                text=f"{footer_name} \u00b7 Officer Cheatsheet \u00b7 Posted by {interaction.user.display_name}",
                icon_url=crest_url,
            )
        else:
            embed.set_footer(
                text=f"{footer_name} \u00b7 Officer Cheatsheet \u00b7 Posted by {interaction.user.display_name}"
            )

        try:
            await interaction.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed(
                    "Cannot post here",
                    "I don't have permission to post in this channel.",
                ),
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            error_log(f"officer-cheatsheet failed: {exc!s}")
            await interaction.response.send_message(
                embed=error_embed("Failed to post", f"`{exc!s}`"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=success_embed(
                "Officer cheatsheet posted",
                "Pin it in the officer channel so every officer can find it.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} posted officer cheatsheet in #{interaction.channel}."
        )

    def cog_unload(self) -> None:
        # Reload-safety: drop manually-added groups so the next setup() can
        # re-register them without colliding.
        for name in ("admin", "audit"):
            try:
                self.bot.tree.remove_command(name)
            except Exception:  # noqa: BLE001
                pass

async def setup(bot: Bot):
    await bot.add_cog(Admin(bot))
