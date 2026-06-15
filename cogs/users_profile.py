from cogs._typing import Bot
import asyncio
import datetime
import re
import discord
from discord import app_commands
from discord.ext import commands
from debug import error_log, info_log
from config import HOME_GUILD_NAME, HOME_GUILD_ROLE_NAME, LIFECYCLE_ROLES, STAFF_ROLES, derive_lifecycle
from cogs._nickname_tags import tagged_nickname_for_profile
from utils import error_embed, info_embed, mark_unionbot_handled, success_embed
import albion_api

# Tracks discord_ids currently in the registration flow (screenshot pending)
_pending_registrations: set = set()
# Screenshots posted while Albion API lookup is still running. The normal
# wait_for listener is not active until after the character lookup finishes,
# so very fast users can otherwise upload into a dead zone.
_pending_registration_uploads: dict[str, discord.Message] = {}
_REGISTRATION_NUDGE_COOLDOWN_KEY = "registration_nudge_cooldown_sec"
_REGISTRATION_NUDGE_DELETE_AFTER_KEY = "registration_nudge_delete_after_sec"
_DEFAULT_REGISTRATION_NUDGE_COOLDOWN_SEC = 300
_DEFAULT_REGISTRATION_NUDGE_DELETE_AFTER_SEC = 180
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif")
_REGISTRATION_TEXT_HELP_RE = re.compile(
    r"\b("
    r"register(?:ed|ing|ation)?|verify|verification|screenshot|screen\s*shot|"
    r"button|stuck|twice|again|not\s+work(?:ing)?|doesn'?t\s+work|didn'?t\s+work|"
    r"help|what\s+now|next\s+step"
    r")\b",
    re.IGNORECASE,
)

_ALL_LIFECYCLE = set(LIFECYCLE_ROLES)

# Albion character names: 2-16 chars, letters/digits/underscore/dash only.
_ALBION_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{2,16}$")


def _validate_albion_name(raw: str) -> tuple[str, str | None]:
    """Normalise and validate a player name. Returns (cleaned, error_or_None)."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return cleaned, "Please enter your Albion Online character name."
    if " " in cleaned or "\t" in cleaned:
        return cleaned, "Albion names don\u2019t contain spaces \u2014 just the character name."
    if cleaned.startswith("@") or cleaned.startswith("<"):
        return cleaned, "Enter your Albion character name, not a Discord mention."
    if not _ALBION_NAME_RE.match(cleaned):
        return cleaned, (
            "That doesn\u2019t look like an Albion name. Use 2\u201316 characters: "
            "letters, digits, underscore or dash."
        )
    return cleaned, None


# ── small helpers ────────────────────────────────────────────────────────────

def _is_registered(profile) -> bool:
    """Return whether a profile has completed or is awaiting verification.

    A stale pre-verification profile can still have an Albion player ID from
    an interrupted registration. That should not block the member from
    clicking Register again.
    """
    if not profile or not profile.get("albion_player_id"):
        return False
    if profile.get("verified_date") or profile.get("lifecycle_role"):
        return True
    try:
        return int(profile.get("pending_verification") or 0) == 1
    except (TypeError, ValueError):
        return False


def _api_membership_missing(stats: dict) -> bool:
    return not any(
        str(stats.get(key) or "").strip()
        for key in (
            "guild_id",
            "guild_name",
            "alliance_id",
            "alliance_name",
            "alliance_tag",
        )
    )


def _attachment_is_image(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    filename = (attachment.filename or "").lower()
    return filename.endswith(_IMAGE_EXTENSIONS)


def _config_int(db, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(db.get_config(key) or "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _first_image_attachment(message: discord.Message) -> discord.Attachment | None:
    for attachment in message.attachments:
        if _attachment_is_image(attachment):
            return attachment
    return None


def _pending_officer_review(profile) -> bool:
    if not profile:
        return False
    try:
        return int(profile.get("pending_verification") or 0) == 1
    except (TypeError, ValueError):
        return False


def _registration_upload_nudge(profile) -> tuple[str, str, bool]:
    """Message shown when a screenshot is uploaded outside the active flow.

    Returns (title, body, include_register_button).
    """
    if _pending_officer_review(profile):
        return (
            "Screenshot already submitted",
            "Your registration is already waiting for officer review. You do not need to upload another screenshot. "
            "You will get a DM when an officer approves or denies it.",
            False,
        )
    if _is_registered(profile):
        return (
            "Already registered",
            "Your Albion character is already linked. You do not need to post screenshots here anymore. "
            "Use `/profile view` if you want to check your profile.",
            False,
        )
    if profile and profile.get("albion_player_id"):
        return (
            "Restart registration",
            "That screenshot is not attached to an active registration. Click **Register**, enter your Albion name/server again, "
            "then post the screenshot after the bot asks for it.",
            True,
        )
    return (
        "Click Register first",
        "I can only accept a screenshot after you click **Register** and enter your Albion character name/server. "
        "Uploading the image first does not give the bot enough information to match it to your character.",
        True,
    )


def _registration_text_nudge(profile) -> tuple[str, str, bool]:
    """Message shown when a user types confused registration text."""
    if _pending_officer_review(profile):
        return (
            "Registration is waiting for review",
            "Your screenshot is already in the officer queue. You do not need to register again unless an officer asks you to restart.",
            False,
        )
    if _is_registered(profile):
        return (
            "Already registered",
            "Your Albion character is already linked. If your roles look wrong, ask staff to run a sync instead of starting over.",
            False,
        )
    if profile and profile.get("albion_player_id"):
        return (
            "Restart registration",
            "Your character lookup exists, but the screenshot step is not active anymore. Click **Register**, enter your Albion name/server, then upload the screenshot after the bot asks for it.",
            True,
        )
    return (
        "Start with the Register button",
        "Click **Register**, enter your Albion character name and server, then upload a screenshot of your character screen in this channel when the bot asks.",
        True,
    )


def _get_lifecycle_thresholds(db) -> tuple[int, int]:
    """Return (probationary_days, member_days) with sensible defaults."""
    return (
        int(db.get_config("probationary_days") or 30),
        int(db.get_config("member_days") or 90),
    )


def _member_since_iso(member: discord.Member, profile=None) -> str | None:
    """ISO string used to derive lifecycle.

    Prefers Discord ``member.joined_at`` (time-in-server); falls back to the
    DB ``verified_date`` for legacy profiles where joined_at is missing.
    """
    if member.joined_at:
        return member.joined_at.replace(tzinfo=None).isoformat()
    if profile:
        return profile.get("verified_date")
    return None


def _days_in_server(member: discord.Member) -> int:
    if not member.joined_at:
        return 0
    return (datetime.datetime.utcnow() - member.joined_at.replace(tzinfo=None)).days


def _resolve_home_guild(db) -> str:
    """Return the configured in-game home guild name (auto-resolves if unset).

    Lightweight twin of ``applications._get_home_guild`` to avoid a circular
    import. Falls back to the single tracked guild, then to ``HOME_GUILD_NAME``.
    """
    configured = (db.get_config("home_guild_name") or "").strip()
    if configured:
        return configured
    try:
        tracked = db.fetch_all_guilds() or []
        names = [(g.get("guild_name") or "").strip() for g in tracked if g.get("guild_name")]
        if len(names) == 1:
            return names[0]
    except Exception:
        pass
    return HOME_GUILD_NAME


def _resolve_home_alliance_id(db) -> str | None:
    """Return the configured home-alliance id (or None if not set).

    Written by ``cogs.events._resolve_home_alliance`` on each sync, so by
    the time a member is registering this is reliably populated.
    """
    val = (db.get_config("home_alliance_id") or "").strip()
    return val or None


async def post_new_member_shoutout(
    bot,
    member: discord.Member,
    *,
    lifecycle: str = "Recruit",
    home_guild: str | None = None,
) -> bool:
    """Post a public welcome shout-out for a member who just got confirmed
    in the home guild (lifecycle promoted to Recruit/Member/Veteran).

    Idempotent — first call per ``member.id`` posts; subsequent calls are
    no-ops. Posts to the channel configured under either
    ``shoutout_channel_id`` or, as fallback, ``welcome_channel_id``.

    Returns True if a shout was posted, False otherwise (already-posted,
    channel missing, or send failed).
    """
    db = bot.db
    discord_id = str(member.id)
    if db.has_member_shoutout_been_sent(discord_id):
        return False

    channel_id = (
        db.get_config("shoutout_channel_id")
        or db.get_config("welcome_channel_id")
    )
    if not channel_id:
        # No channel configured — silently mark sent to avoid backlog spam
        # the moment one is set later.
        return False
    channel = member.guild.get_channel(int(channel_id))
    if channel is None:
        return False

    profile = db.fetch_user_profile(discord_id) or {}
    albion_name = profile.get("albion_name") or member.display_name
    guild_name = home_guild or _resolve_home_guild(db)

    embed = discord.Embed(
        title=f"🎉 Welcome to {guild_name}, {albion_name}!",
        description=(
            f"Everyone give a warm welcome to {member.mention} — "
            f"just confirmed as a **{lifecycle}** of **{guild_name}**!\n\n"
            "Say hi, show them around, and help them get into their first content."
        ),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    ip = profile.get("average_item_power")
    if ip:
        embed.add_field(name="Avg IP", value=f"{float(ip):.0f}", inline=True)
    kill_fame = profile.get("kill_fame")
    if kill_fame:
        embed.add_field(name="Kill Fame", value=f"{int(kill_fame):,}", inline=True)
    embed.set_footer(text="Run /profile view to see your stats anytime.")

    try:
        await channel.send(
            content=member.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        db.mark_member_shoutout_sent(discord_id, lifecycle)
        info_log(
            f"Posted new-member shout-out for {member} ({discord_id}) "
            f"as {lifecycle} of {guild_name}."
        )
        return True
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(
            f"new-member shout-out for {member} ({discord_id}) failed in "
            f"{channel}: {exc!r}"
        )
        return False


def _audit_member_roles(member, profile, role_cache, probationary_days, member_days, db):
    """Return (roles_to_add, roles_to_remove) for one member based on their DB profile.

    Does NOT touch staff roles (Officer, Shotcaller, etc.) — those are officer-managed.
    """
    roles_to_add: list = []
    roles_to_remove: list = []

    is_registered = _is_registered(profile)
    unverified_role = role_cache.get("Unverified")
    verified_role   = role_cache.get("Verified")
    tu_role         = role_cache.get(HOME_GUILD_ROLE_NAME)
    synced_role     = role_cache.get("Synced")
    not_synced_role = role_cache.get("NotSynced")

    if is_registered:
        if verified_role and verified_role not in member.roles:
            roles_to_add.append(verified_role)
        if unverified_role and unverified_role in member.roles:
            roles_to_remove.append(unverified_role)
        if synced_role and synced_role not in member.roles:
            roles_to_add.append(synced_role)
        if not_synced_role and not_synced_role in member.roles:
            roles_to_remove.append(not_synced_role)

        stored_lifecycle = profile.get("lifecycle_role")
        if not stored_lifecycle:
            stored_lifecycle = derive_lifecycle(
                _member_since_iso(member, profile), probationary_days, member_days
            )
            db.set_lifecycle_role(str(member.id), stored_lifecycle)

        guild_name = profile.get("guild_name")
        if tu_role:
            home_guild_lc = _resolve_home_guild(db).strip().lower()
            current_guild_lc = (guild_name or "").strip().lower()
            if stored_lifecycle in {"Inactive", "Alumni"}:
                if tu_role in member.roles:
                    roles_to_remove.append(tu_role)
            elif current_guild_lc == home_guild_lc and tu_role not in member.roles:
                roles_to_add.append(tu_role)
            elif current_guild_lc != home_guild_lc and tu_role in member.roles:
                roles_to_remove.append(tu_role)

        for role_name in _ALL_LIFECYCLE:
            role_obj = role_cache.get(role_name)
            if not role_obj:
                continue
            if role_name == stored_lifecycle:
                if role_obj not in member.roles:
                    roles_to_add.append(role_obj)
            else:
                if role_obj in member.roles:
                    roles_to_remove.append(role_obj)
    else:
        if unverified_role and unverified_role not in member.roles:
            roles_to_add.append(unverified_role)
        if verified_role and verified_role in member.roles:
            roles_to_remove.append(verified_role)
        if tu_role and tu_role in member.roles:
            roles_to_remove.append(tu_role)
        if not_synced_role and not_synced_role not in member.roles:
            roles_to_add.append(not_synced_role)
        if synced_role and synced_role in member.roles:
            roles_to_remove.append(synced_role)
        for role_name in _ALL_LIFECYCLE:
            role_obj = role_cache.get(role_name)
            if role_obj and role_obj in member.roles:
                roles_to_remove.append(role_obj)

    return roles_to_add, roles_to_remove

class RegisterModal(discord.ui.Modal, title="Register Your Albion Identity"):
    def __init__(self, bot):
        super().__init__()
        self.bot: Bot = bot
        
    albion_name = discord.ui.TextInput(
        label="Albion Name",
        placeholder="Enter your Albion Online character name....",
        required=True,
        max_length=32,
    )

    server = discord.ui.TextInput(
        label="Server",
        placeholder="Americas, Europe, or Asia",
        required=True,
        max_length=10,
    )

    # Common short forms / aliases users might type.
    _SERVER_ALIASES = {
        "americas": "americas", "america": "americas", "us": "americas", "na": "americas",
        "europe":   "europe",   "eu": "europe",
        "asia":     "asia",     "sea": "asia",
    }

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_server = self.server.value.strip().lower()
        server = self._SERVER_ALIASES.get(raw_server)

        # Validate server input
        if not server:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid server",
                    f"`{raw_server or '(empty)'}` isn’t a valid server.",
                    hint="Enter **Americas**, **Europe**, or **Asia**.",
                ),
                ephemeral=True,
            )
            return

        player_name, name_err = _validate_albion_name(self.albion_name.value)
        if name_err:
            await interaction.response.send_message(
                embed=error_embed("Check the name", name_err),
                ephemeral=True,
            )
            return

        discord_id = str(interaction.user.id)

        # Blacklist gate — block deny-listed Discord IDs up front.
        try:
            bl = self.bot.db.is_blacklisted(discord_id=discord_id)
        except Exception:  # noqa: BLE001
            bl = None
        if bl:
            await interaction.response.send_message(
                embed=error_embed(
                    "Registration not allowed",
                    "You are not eligible to register at this time. "
                    "Reach out to a staff member if you believe this is a mistake.",
                ),
                ephemeral=True,
            )
            return

        # Prevent concurrent registration attempts from the same user
        if discord_id in _pending_registrations:
            await interaction.response.send_message(
                embed=info_embed(
                    "Registration in progress",
                    "You already have a registration in progress. Post your screenshot to continue, or wait for it to time out.",
                ),
                ephemeral=True,
            )
            return

        # Step 2: Check if already registered
        profile = self.bot.db.fetch_user_profile(discord_id)
        if _is_registered(profile):
            await interaction.response.send_message(
                embed=info_embed(
                    "Already registered",
                    "Your Albion character is already linked. Use `/profile view` to see your stats.",
                ),
                ephemeral=True,
            )
            return

        # Step 3: Look up character — defer first since API call takes time
        await interaction.response.defer(ephemeral=True)
        _pending_registrations.add(discord_id)
        _pending_registration_uploads.pop(discord_id, None)
        try:
            loop = asyncio.get_running_loop()
            home_guild = _resolve_home_guild(self.bot.db)
            result = await loop.run_in_executor(
                None,
                lambda: albion_api.get_player_id(player_name, server, prefer_guild_name=home_guild),
            )
            if not result:
                await interaction.followup.send(
                    embed=error_embed(
                        "Character not found",
                        f"`{player_name}` was not found on the **{server.capitalize()}** server.",
                        hint="Check the spelling (capitalization matters) and try again.",
                    ),
                    ephemeral=True,
                )
                return

            # Step 4: Fetch stats and save
            player_id, exact_name = result

            # Reject if this Albion character is already linked to a
            # different Discord account — prevents typos onto someone else's
            # name. Allow re-registration on the SAME discord_id (e.g. user
            # is fixing their own broken state).
            other = self.bot.db.fetch_user_profile_by_player_id(player_id)
            if other and str(other.get("discord_id") or "") not in ("", discord_id):
                await interaction.followup.send(
                    embed=error_embed(
                        "That character is already linked",
                        f"**{exact_name}** is already linked to another Discord account.",
                        hint="If this is your character on a different account, contact a staff member \u2014 don\u2019t register a duplicate.",
                    ),
                    ephemeral=True,
                )
                return

            # Blacklist gate (round 2) — block by Albion player_id so an alt
            # Discord account can't register a deny-listed character.
            try:
                bl_player = self.bot.db.is_blacklisted(albion_player_id=player_id)
            except Exception:  # noqa: BLE001
                bl_player = None
            if bl_player:
                await interaction.followup.send(
                    embed=error_embed(
                        "Registration not allowed",
                        f"`{exact_name}` is not eligible to register at this time. "
                        "Reach out to a staff member if you believe this is a mistake.",
                    ),
                    ephemeral=True,
                )
                return

            data = await loop.run_in_executor(None, lambda: albion_api.get_player_stats(player_id, server))
            if not data:
                await interaction.followup.send(
                    embed=error_embed(
                        "Albion API timed out",
                        f"Found `{exact_name}`, but couldn't fetch stats from Albion right now. "
                        "Please run the registration again in a minute.",
                    ),
                    ephemeral=True,
                )
                return
            stats = albion_api.parse_stats(data)
            self.bot.db.update_user_albion_info(discord_id, player_id, exact_name, stats)

            # Registration is a security check: prove this Discord account
            # controls a real Albion character. Guild/alliance membership only
            # decides which lifecycle role they receive after officer approval.
            home_guild = _resolve_home_guild(self.bot.db)
            home_alliance_id = _resolve_home_alliance_id(self.bot.db)
            stats_guild_lc = (stats.get("guild_name") or "").strip().lower()
            stats_alliance_id = (stats.get("alliance_id") or "").strip() or None
            api_membership_missing = _api_membership_missing(stats)
            in_home_guild = stats_guild_lc == home_guild.strip().lower()
            in_home_alliance = bool(
                home_alliance_id
                and stats_alliance_id
                and stats_alliance_id == home_alliance_id
            )
            self.bot.db.execute(
                "UPDATE user_profiles SET pending_home_guild_until = NULL "
                "WHERE discord_id = ?",
                (discord_id,),
            )

            # Step 5: Prompt for screenshot
            screenshot_intro = (
                f"Character {exact_name} found — home guild"
                if in_home_guild else
                f"Character {exact_name} found — allied guild"
                if in_home_alliance else
                f"Character {exact_name} found"
            )
            screenshot_body = (
                "**Next step:** post a screenshot of your character screen in this channel to complete registration."
            )
            if not in_home_guild and not in_home_alliance:
                guild_name = (stats.get("guild_name") or "").strip()
                guild_line = (
                    "Albion's API is not showing a guild or alliance for this character."
                    if api_membership_missing else
                    f"Albion's API shows this character in **{guild_name or 'no guild'}**."
                )
                screenshot_body = (
                    f"{guild_line} That's fine — registration only verifies "
                    "that you are an Albion player. If approved, you'll be "
                    "registered as **Guest** until you join the guild or an "
                    "allied guild.\n\n"
                    + screenshot_body
                )
            if in_home_alliance and not in_home_guild:
                alliance_tag = (stats.get("alliance_tag") or "").strip()
                tag_part = f" (`{alliance_tag}`)" if alliance_tag else ""
                screenshot_body = (
                    f"You're in an allied guild{tag_part}, not the home guild "
                    f"`{home_guild}`. That's fine — you'll be registered as an "
                    "**Alliance** member.\n\n" + screenshot_body
                )
            await interaction.followup.send(
                embed=info_embed(screenshot_intro, screenshot_body),
                ephemeral=True,
            )

            # Step 6: Wait for screenshot. Start the waiter before consuming
            # the early-upload buffer so there is no gap between "API lookup
            # finished" and "ready to accept screenshot".
            def screenshot_check(m: discord.Message) -> bool:
                return (
                    m.author.id == interaction.user.id
                    and m.channel.id == interaction.channel.id
                    and _first_image_attachment(m) is not None
                )

            wait_task = asyncio.create_task(
                self.bot.wait_for("message", check=screenshot_check, timeout=300)
            )
            await asyncio.sleep(0)
            buffered_message = _pending_registration_uploads.pop(discord_id, None)
            try:
                if buffered_message and screenshot_check(buffered_message):
                    wait_task.cancel()
                    message = buffered_message
                    info_log(
                        f"Registration reused early screenshot from {interaction.user} ({discord_id})."
                    )
                else:
                    message = await wait_task
            except TimeoutError:
                # Skip cleanup if the user got verified out-of-band (admin
                # vouch / manual approve) while we were waiting for a
                # screenshot. Wiping the profile here would strip the role.
                fresh = self.bot.db.fetch_user_profile(discord_id) or {}
                if not fresh.get("verified_date"):
                    self.bot.db.clear_user_albion_info(discord_id)
                await interaction.followup.send(
                    embed=error_embed(
                        "Registration timed out",
                        "You took too long to post a screenshot.",
                        hint="Click **Register** again to restart.",
                    ),
                    ephemeral=True,
                )
                return
            finally:
                if not wait_task.done():
                    wait_task.cancel()

            screenshot = _first_image_attachment(message)
            if screenshot is None:
                await interaction.followup.send(
                    embed=error_embed(
                        "Screenshot missing",
                        "I could not find an image attachment on that message.",
                        hint="Click **Register** again and upload a PNG/JPG screenshot when asked.",
                    ),
                    ephemeral=True,
                )
                return

            screenshot_url = screenshot.url
            screenshot_file = await screenshot.to_file(filename="screenshot.png")
            await message.delete()

            self.bot.db.set_screenshot_url(discord_id, screenshot_url)

            # Post to officer channel for review
            officer_channel_id = self.bot.db.get_config("officer_channel_id")
            officer_channel = interaction.guild.get_channel(int(officer_channel_id)) if officer_channel_id else None
            if not officer_channel:
                fresh = self.bot.db.fetch_user_profile(discord_id) or {}
                if not fresh.get("verified_date"):
                    self.bot.db.clear_user_albion_info(discord_id)
                await interaction.followup.send(
                    embed=error_embed(
                        "Registration failed",
                        "No officer review channel is configured.",
                        hint="Please contact an admin.",
                    ),
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="New Registration Request",
                color=discord.Color.orange()
            )
            embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
            embed.add_field(name="Albion Name", value=exact_name, inline=True)
            embed.add_field(name="Server", value=server.capitalize(), inline=True)
            embed.add_field(name="Guild", value=stats.get("guild_name") or "N/A", inline=True)
            outcome = (
                "Recruit (home guild)"
                if in_home_guild else
                "Alliance"
                if in_home_alliance else
                "Guest"
            )
            embed.add_field(name="Approval role", value=outcome, inline=True)
            embed.add_field(name="Kill Fame", value=f"{stats.get('kill_fame', 0):,}", inline=True)
            embed.add_field(name="Avg IP", value=f"{stats.get('average_item_power', 0.0):.1f}", inline=True)
            if api_membership_missing:
                embed.add_field(
                    name="API note",
                    value=(
                        "Albion API returned no guild/alliance. Approval still verifies "
                        "the Discord user controls this Albion character; they will be "
                        "Guest unless a later sync sees guild/alliance membership."
                    ),
                    inline=False,
                )
            embed.set_image(url="attachment://screenshot.png")
            review_msg = await officer_channel.send(
                embed=embed,
                file=screenshot_file,
                view=VerificationView(self.bot),
            )
            # Persist the message → applicant mapping so the Approve/Deny
            # buttons survive a bot restart.
            try:
                self.bot.db.record_verification_request(
                    review_msg.id, str(interaction.user.id), review_msg.channel.id,
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"record_verification_request failed: {exc!r}")

            await interaction.followup.send(
                embed=success_embed(
                    "Screenshot received",
                    "An officer will review your registration shortly. You’ll get a DM when it’s decided.",
                ),
                ephemeral=True,
            )
            info_log(f"User {interaction.user} ({discord_id}) submitted registration as {exact_name} on {server}.")

        finally:
            _pending_registrations.discard(discord_id)
            _pending_registration_uploads.pop(discord_id, None)

        

class RegisterView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Register · Registrarse · Cadastrar-se", style=discord.ButtonStyle.primary, custom_id="register_button")
    async def register_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegisterModal(self.bot))

class VerificationView(discord.ui.View):
    """Persistent officer-review view for new-member registrations.

    Uses static custom_ids and looks the applicant up via the
    ``verification_requests`` table so the buttons keep working after a
    bot restart (the old design embedded the applicant ID in the
    custom_id and kept it only in memory, which broke on reload).
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    def _resolve_applicant_id(self, interaction: discord.Interaction) -> str | None:
        msg = interaction.message
        if msg is None:
            return None
        row = self.bot.db.fetch_verification_request(msg.id)
        if row:
            return row.get("discord_id")
        return None

    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.success,
        custom_id="verify_approve",
    )
    async def approve_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await self._approve(interaction)

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="verify_deny",
    )
    async def deny_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await self._deny(interaction)

    async def _approve(self, interaction: discord.Interaction) -> None:
        applicant_discord_id = self._resolve_applicant_id(interaction)
        if not applicant_discord_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Verification request not found",
                    "This review entry is no longer linked to an applicant "
                    "(probably already resolved). You can dismiss this message.",
                ),
                ephemeral=True,
            )
            return
        applicant = interaction.guild.get_member(int(applicant_discord_id))
        if applicant:
            unverified_role = discord.utils.get(interaction.guild.roles, name="Unverified")
            verified_role = discord.utils.get(interaction.guild.roles, name="Verified")
            not_synced_role = discord.utils.get(interaction.guild.roles, name="NotSynced")
            synced_role = discord.utils.get(interaction.guild.roles, name="Synced")
            base_removes = [
                role for role in (unverified_role, not_synced_role)
                if role and role in applicant.roles
            ]
            base_adds = [
                role for role in (verified_role, synced_role)
                if role and role not in applicant.roles
            ]
            if base_removes:
                await applicant.remove_roles(
                    *base_removes,
                    reason="Registration approved",
                )
            if base_adds:
                await applicant.add_roles(
                    *base_adds,
                    reason="Registration approved",
                )
            self.bot.db.set_verified_date(applicant_discord_id)

            profile = self.bot.db.fetch_user_profile(applicant_discord_id)
            albion_name = profile.get("albion_name") if profile else None
            guild_name = profile.get("guild_name") if profile else None

            home_guild = _resolve_home_guild(self.bot.db)
            home_alliance_id = _resolve_home_alliance_id(self.bot.db)
            profile_alliance_id = str((profile or {}).get("alliance_id") or "").strip()
            in_home_guild = bool(guild_name) and guild_name.strip().lower() == home_guild.lower()
            in_home_alliance = bool(
                not in_home_guild
                and home_alliance_id
                and profile_alliance_id
                and profile_alliance_id == home_alliance_id
            )
            lifecycle_role_names = set(LIFECYCLE_ROLES)
            lifecycle_by_name = {
                role.name: role
                for role in interaction.guild.roles
                if role.name in lifecycle_role_names
            }

            async def _set_lifecycle_role(target_name: str) -> None:
                target = lifecycle_by_name.get(target_name)
                removes = [
                    role for name, role in lifecycle_by_name.items()
                    if name != target_name and role in applicant.roles
                ]
                if removes:
                    await applicant.remove_roles(
                        *removes,
                        reason=f"Registration lifecycle -> {target_name}",
                    )
                if target and target not in applicant.roles:
                    await applicant.add_roles(
                        target,
                        reason=f"Registration lifecycle -> {target_name}",
                    )

            if in_home_guild:
                # Already in the in-game guild — skip Probationary, go straight to Recruit
                # and auto-resolve any pending guild application.
                await _set_lifecycle_role("Recruit")
                self.bot.db.set_lifecycle_role(applicant_discord_id, "Recruit")
                pending_app = self.bot.db.fetch_pending_guild_application(applicant_discord_id)
                if pending_app:
                    self.bot.db.update_guild_application_status(
                        pending_app["id"], "approved", str(interaction.user.id),
                        f"Auto-approved during registration (already in {home_guild})",
                    )
                try:
                    await applicant.send(
                        f"Your registration has been approved. You're already in **{home_guild}** in-game, "
                        f"so you've been added directly as **Recruit** — welcome!"
                    )
                except discord.Forbidden:
                    pass
                info_log(
                    f"Registration approved for {applicant_discord_id} by {interaction.user}; "
                    f"auto-Recruited (already in {home_guild})."
                )
                try:
                    await post_new_member_shoutout(
                        self.bot, applicant,
                        lifecycle="Recruit", home_guild=home_guild,
                    )
                except Exception as exc:  # noqa: BLE001
                    error_log(f"shout-out for {applicant} failed: {exc!r}")
            elif in_home_alliance:
                await _set_lifecycle_role("Alliance")
                self.bot.db.set_lifecycle_role(applicant_discord_id, "Alliance")
                try:
                    await applicant.send(
                        "Your registration has been approved — welcome! "
                        "You're registered as an **Alliance** member."
                    )
                except discord.Forbidden:
                    pass
                info_log(
                    f"Registration approved for {applicant_discord_id} by {interaction.user}; "
                    "set lifecycle=Alliance."
                )
            else:
                # Verified Albion player, but not in the home guild/alliance.
                # Registration is still valid; park them as Guest until a
                # later guild join/application promotes them.
                await _set_lifecycle_role("Guest")
                self.bot.db.set_lifecycle_role(applicant_discord_id, "Guest")
                try:
                    await applicant.send(
                        "Your registration has been approved \u2014 welcome! You're flagged as **Guest** for now. "
                        f"Once you're in **{home_guild}** in-game, an allied guild, or your guild application "
                        "is approved here, your role will update automatically."
                    )
                except discord.Forbidden:
                    pass
                info_log(
                    f"Registration approved for {applicant_discord_id} by {interaction.user}; "
                    f"set lifecycle=Guest (verified Albion player, not in {home_guild}/alliance)."
                )

            if albion_name:
                home_guild_lc = home_guild.strip().lower()
                if guild_name and guild_name.strip().lower() == home_guild_lc:
                    home_role = discord.utils.get(interaction.guild.roles, name=home_guild)
                    if home_role:
                        await applicant.add_roles(home_role)
                new_nick = tagged_nickname_for_profile(
                    self.bot.db,
                    albion_name,
                    profile,
                    home_member=in_home_guild,
                )
                try:
                    await applicant.edit(nick=new_nick)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        self.bot.db.set_pending_verification(applicant_discord_id, False)
        # Resolve the review request: drop the mapping so a stale message
        # click can't fire the workflow twice.
        if interaction.message is not None:
            self.bot.db.delete_verification_request(interaction.message.id)
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content=f"✅ Approved by {interaction.user.mention}", view=self)

    async def _deny(self, interaction: discord.Interaction) -> None:
        applicant_discord_id = self._resolve_applicant_id(interaction)
        if not applicant_discord_id:
            await interaction.response.send_message(
                embed=error_embed(
                    "Verification request not found",
                    "This review entry is no longer linked to an applicant "
                    "(probably already resolved). You can dismiss this message.",
                ),
                ephemeral=True,
            )
            return
        applicant = interaction.guild.get_member(int(applicant_discord_id))
        if applicant:
            try:
                await applicant.send("Your registration was denied. Please contact an officer if you have questions.")
            except discord.Forbidden:
                pass

        self.bot.db.clear_user_albion_info(applicant_discord_id)
        self.bot.db.set_pending_verification(applicant_discord_id, False)
        if interaction.message is not None:
            self.bot.db.delete_verification_request(interaction.message.id)
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(content=f"❌ Denied by {interaction.user.mention}", view=self)
        info_log(f"Registration denied for {applicant_discord_id} by {interaction.user}.")

class ProfileGroup(app_commands.Group, name="profile", description="Commands related to user profiles."):

    def __init__(self, bot: Bot):
        super().__init__()
        self.bot = bot

    @app_commands.command(name="view", description="View your profile information.")
    async def view(self, interaction: discord.Interaction) -> None:
        """Fetch and display the user's profile information from the database."""
        discord_id = str(interaction.user.id)
        profile = self.bot.db.fetch_user_profile(discord_id)
        if not _is_registered(profile):
            await interaction.response.send_message(
                embed=info_embed(
                    "Not registered yet",
                    "You haven’t linked an Albion character yet.",
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"{profile.get('albion_name', 'Unknown')}",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        # Identity
        embed.add_field(name="Guild", value=profile.get("guild_name") or "N/A", inline=True)
        embed.add_field(name="Alliance", value=f"{profile.get('alliance_name') or 'N/A'} [{profile.get('alliance_tag') or 'N/A'}]", inline=True)
        embed.add_field(name="Avg Item Power", value=f"{profile.get('average_item_power', 0.0):.1f}", inline=True)

        # Combat
        embed.add_field(name="Kill Fame", value=f"{profile.get('kill_fame', 0):,}", inline=True)
        embed.add_field(name="Death Fame", value=f"{profile.get('death_fame', 0):,}", inline=True)
        embed.add_field(name="Fame Ratio", value=f"{profile.get('fame_ratio', 0.0):.2f}", inline=True)

        # PvE
        embed.add_field(name="PvE Total", value=f"{profile.get('pve_total', 0):,}", inline=True)
        embed.add_field(name="Outlands", value=f"{profile.get('pve_outlands', 0):,}", inline=True)
        embed.add_field(name="Hellgate", value=f"{profile.get('pve_hellgate', 0):,}", inline=True)
        embed.add_field(name="Corrupted", value=f"{profile.get('pve_corrupted', 0):,}", inline=True)
        embed.add_field(name="Mists", value=f"{profile.get('pve_mists', 0):,}", inline=True)
        embed.add_field(name="Avalon", value=f"{profile.get('pve_avalon', 0):,}", inline=True)

        # Gathering
        embed.add_field(name="Gathering Total", value=f"{profile.get('gather_all', 0):,}", inline=True)
        embed.add_field(name="Ore", value=f"{profile.get('gather_ore', 0):,}", inline=True)
        embed.add_field(name="Wood", value=f"{profile.get('gather_wood', 0):,}", inline=True)
        embed.add_field(name="Fiber", value=f"{profile.get('gather_fiber', 0):,}", inline=True)
        embed.add_field(name="Hide", value=f"{profile.get('gather_hide', 0):,}", inline=True)
        embed.add_field(name="Rock", value=f"{profile.get('gather_rock', 0):,}", inline=True)

        # Other
        embed.add_field(name="Crafting Fame", value=f"{profile.get('crafting_fame', 0):,}", inline=True)
        embed.add_field(name="Fishing Fame", value=f"{profile.get('fishing_fame', 0):,}", inline=True)
        embed.add_field(name="Farming Fame", value=f"{profile.get('farming_fame', 0):,}", inline=True)
        embed.add_field(name="Crystal League", value=f"{profile.get('crystal_league', 0):,}", inline=True)

        # Engagement: activity streak + per-metric personal bests.
        cur_streak = int(profile.get("activity_streak_days") or 0)
        best_streak = int(profile.get("activity_streak_best") or 0)
        if cur_streak or best_streak:
            streak_val = f"🔥 {cur_streak}d (best {best_streak}d)" if cur_streak else f"best {best_streak}d"
            embed.add_field(name="Activity Streak", value=streak_val, inline=True)

        def _fmt_pb(n: int) -> str:
            n = int(n)
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}K"
            return f"{n:,}"

        pb_pairs = [
            ("PvP PB",    profile.get("pb_kill_delta")),
            ("PvE PB",    profile.get("pb_pve_delta")),
            ("Gather PB", profile.get("pb_gather_delta")),
            ("Craft PB",  profile.get("pb_craft_delta")),
            ("Fish PB",   profile.get("pb_fish_delta")),
        ]
        pb_str = " · ".join(
            f"{label} {_fmt_pb(v)}" for label, v in pb_pairs if v and int(v) > 0
        )
        if pb_str:
            embed.add_field(name="🏆 Personal Bests", value=pb_str, inline=False)

        # Voice activity (total + last 7 days)
        try:
            import datetime as _dt
            total_v = int(self.bot.db.fetch_voice_seconds_total(discord_id) or 0)
            since = (_dt.datetime.utcnow() - _dt.timedelta(days=7)).strftime("%Y-%m-%d")
            week_v = int(self.bot.db.fetch_voice_seconds_window(discord_id, since) or 0)
            if total_v > 0:
                def _fmt_dur(s: int) -> str:
                    h, rem = divmod(int(s), 3600)
                    m, _sec = divmod(rem, 60)
                    if h:
                        return f"{h}h {m}m"
                    return f"{m}m"
                embed.add_field(
                    name="🎤 Voice Time",
                    value=f"{_fmt_dur(week_v)} (7d) · {_fmt_dur(total_v)} total",
                    inline=False,
                )
        except Exception:  # noqa: BLE001
            pass

        embed.set_footer(text=f"Last updated: {profile.get('last_updated', 'Never')}")
        await interaction.response.send_message(embed=embed, ephemeral=True)   

    @app_commands.command(
        name="balance",
        description="Show your silver balance with the guild and recent ledger activity.",
    )
    async def balance(self, interaction: discord.Interaction) -> None:
        db = self.bot.db
        discord_id = str(interaction.user.id)
        bal = db.fetch_silver_balance(discord_id)
        ledger = db.fetch_silver_ledger(discord_id, limit=10)
        if bal > 0:
            headline = f"💰 The guild owes you **{bal:,}** silver."
        elif bal < 0:
            headline = f"⚠️ You owe the guild **{abs(bal):,}** silver."
        else:
            headline = "✅ Your balance with the guild is settled (0 silver)."
        if ledger:
            lines = []
            for r in ledger:
                d = int(r["delta"])
                sign = "+" if d > 0 else ""
                ts = (r["created_at"] or "")[:10]
                reason = r["reason"] or "?"
                lines.append(f"`{ts}` `{sign}{d:,}` — {reason}")
            history = "\n".join(lines)
        else:
            history = "_No ledger entries yet._"
        embed = info_embed("Silver balance", headline)
        embed.add_field(name="Recent activity", value=history, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /profile timezone ───────────────────────────────────────────────────

    @app_commands.command(
        name="timezone",
        description="Set your IANA timezone (e.g. America/Chicago). Used for reminders.",
    )
    @app_commands.describe(
        tz="IANA timezone name (e.g. 'America/Chicago', 'Europe/London'). Leave blank to clear.",
    )
    async def timezone_cmd(
        self, interaction: discord.Interaction, tz: str | None = None,
    ) -> None:
        discord_id = str(interaction.user.id)
        profile = self.bot.db.fetch_user_profile(discord_id)
        if not profile:
            await interaction.response.send_message(
                embed=error_embed(
                    "Not registered",
                    "Click the **Register** button in your registration channel first so the bot can save your timezone.",
                ),
                ephemeral=True,
            )
            return
        cleaned = (tz or "").strip()
        if not cleaned:
            self.bot.db.set_timezone(discord_id, None)
            await interaction.response.send_message(
                embed=success_embed(
                    "Timezone cleared",
                    "Your timezone is no longer stored.",
                ),
                ephemeral=True,
            )
            return
        try:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            ZoneInfo(cleaned)  # raises if invalid
        except (ImportError, ZoneInfoNotFoundError):
            await interaction.response.send_message(
                embed=error_embed(
                    "Unknown timezone",
                    f"`{cleaned}` isn't a valid IANA zone.\n"
                    "Examples: `America/Chicago`, `America/New_York`, "
                    "`Europe/London`, `Europe/Berlin`, `Asia/Tokyo`, "
                    "`Australia/Sydney`, `UTC`.\n"
                    "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_timezone(discord_id, cleaned)
        try:
            from zoneinfo import ZoneInfo
            now_local = datetime.datetime.now(ZoneInfo(cleaned)).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:  # noqa: BLE001
            now_local = "?"
        await interaction.response.send_message(
            embed=success_embed(
                "Timezone saved",
                f"Set to **{cleaned}** (currently `{now_local}`).\n"
                "Future reminders and scheduling will respect this.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set timezone to {cleaned}.")

    @timezone_cmd.autocomplete("tz")
    async def _tz_autocomplete(
        self, interaction: discord.Interaction, current: str,  # noqa: ARG002
    ) -> list[app_commands.Choice[str]]:
        common = [
            "UTC",
            "America/New_York", "America/Chicago", "America/Denver",
            "America/Los_Angeles", "America/Phoenix", "America/Anchorage",
            "America/Toronto", "America/Mexico_City", "America/Sao_Paulo",
            "America/Argentina/Buenos_Aires",
            "Europe/London", "Europe/Dublin", "Europe/Lisbon",
            "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
            "Europe/Rome", "Europe/Amsterdam", "Europe/Stockholm",
            "Europe/Warsaw", "Europe/Athens", "Europe/Moscow",
            "Africa/Cairo", "Africa/Johannesburg",
            "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok",
            "Asia/Singapore", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Seoul",
            "Australia/Perth", "Australia/Sydney", "Pacific/Auckland",
        ]
        needle = (current or "").lower()
        picks = [z for z in common if needle in z.lower()][:25]
        return [app_commands.Choice(name=z, value=z) for z in picks]

    @app_commands.command(name="sync", description="Re-sync an Albion link you previously registered (officer-verified accounts only).")
    @app_commands.describe(albion_name="Your exact Albion Online character name on the Americas server (1–32 chars)")
    async def sync(self, interaction: discord.Interaction, albion_name: app_commands.Range[str, 1, 32]) -> None:
        """Re-sync flow.

        Originally this was a "quick-sync" that let any member self-link to
        any Albion character by name with no verification. That's a real
        spoofing vector: it feeds was_in_home_guild + fame leaderboards +
        the silver ledger. It is now restricted to:

          * users who already have a profile (they went through the
            officer-reviewed registration flow at least once); we let them
            re-link the same or a different character on demand, OR
          * staff (Officer / Captain / Commander / Guild Leader / Admin),
            who can self-serve.

        Brand-new members get redirected to the screenshot-reviewed
        :class:`RegisterView` flow.
        """
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Use this inside the server."),
                ephemeral=True,
            )
            return

        existing = self.bot.db.fetch_user_profile(str(member.id))
        if _is_registered(existing):
            await interaction.response.send_message(
                embed=info_embed(
                    "Already synced",
                    "Your Albion character is already linked. Use `/profile view` to see your stats.",
                ),
                ephemeral=True,
            )
            return

        # Authorisation: must already have a profile (i.e. went through the
        # vetted flow at least once) OR be staff.
        is_staff = any(r.name in STAFF_ROLES for r in member.roles)
        had_profile = existing is not None
        if not (is_staff or had_profile):
            db = self.bot.db
            register_chan_id = db.get_config("registration_channel_id")
            register_link = ""
            if register_chan_id:
                try:
                    ch = member.guild.get_channel(int(register_chan_id))
                    if isinstance(ch, discord.TextChannel):
                        register_link = f"\n\nHead to {ch.mention} and click **Register**."
                except (TypeError, ValueError):
                    pass
            await interaction.response.send_message(
                embed=error_embed(
                    "Registration required",
                    "First-time linking goes through the officer-reviewed registration "
                    "flow (you'll be asked for a screenshot of your character screen).",
                    hint=f"Use the Register button in your registration channel.{register_link}",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        success, msg = await sync_member_to_albion(self.bot, member, albion_name.strip())
        await interaction.followup.send(msg, ephemeral=True)


async def sync_member_to_albion(bot, member: discord.Member, albion_name: str, role_cache: dict | None = None):
    """Look up `albion_name` on Americas, fill the profile, assign Verified + lifecycle + Synced.

    Returns a tuple (success: bool, message: str). Used by both /profile sync and /admin auto-sync.
    """
    discord_id = str(member.id)
    existing = bot.db.fetch_user_profile(discord_id)
    if _is_registered(existing):
        return True, "Already synced"

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: albion_api.get_player_id(albion_name.strip(), "americas"))
    if not result:
        return False, f"Character `{albion_name}` not found on Americas"

    player_id, exact_name = result
    data = await loop.run_in_executor(None, lambda: albion_api.get_player_stats(player_id, "americas"))
    if not data:
        return False, (
            f"Albion API didn't return stats for `{exact_name}` (timeout or rate limit). "
            "Please try again in a minute."
        )
    stats = albion_api.parse_stats(data)

    if not existing:
        bot.db.insert_user_basic_info(
            discord_id=discord_id,
            username=str(member),
            join_date=str(member.joined_at) if member.joined_at else None,
        )

    bot.db.update_user_albion_info(discord_id, player_id, exact_name, stats)
    bot.db.set_verified_date(discord_id)
    bot.db.set_pending_verification(discord_id, False)

    # Award the recruit-verified bonus (configurable; 0 disables).
    try:
        from cogs.points import get_point_setting
        bonus = get_point_setting(bot.db, "points_recruit_verified")
        if bonus > 0:
            bot.db.add_points(discord_id, bonus)
            info_log(
                f"Awarded {bonus} recruit-verified point(s) to {discord_id}."
            )
    except Exception:  # noqa: BLE001
        pass

    probationary_days, member_days = _get_lifecycle_thresholds(bot.db)
    joined_iso = _member_since_iso(member)
    lifecycle = derive_lifecycle(joined_iso, probationary_days, member_days)

    # Auto-promote when the player is already in the home in-game guild.
    #
    # Promotion is *tenure-aware* so a returning Veteran/Member who re-applies after
    # going Inactive/Alumni isn't demoted to Recruit:
    #   • derive_lifecycle returned "Probationary" → still new to Discord → use **Recruit**
    #     (Recruit = "in-game confirmed but still proving out").
    #   • derive_lifecycle returned "Member" or "Veteran" → keep the tenure-earned role.
    #
    # Members verified in a different guild inside the home alliance are
    # **Alliance**. Everyone else outside the home guild/alliance is **Guest**.
    # Neither group enters the home-guild tenure pipeline.
    new_guild_name = stats.get("guild_name")
    new_alliance_id = (stats.get("alliance_id") or "").strip()
    home_guild = _resolve_home_guild(bot.db)
    home_alliance_id = _resolve_home_alliance_id(bot.db)
    in_home_guild = bool(new_guild_name) and new_guild_name.strip().lower() == home_guild.lower()
    in_home_alliance = bool(
        not in_home_guild
        and home_alliance_id
        and new_alliance_id
        and new_alliance_id == home_alliance_id
    )
    if in_home_guild:
        if lifecycle == "Probationary":
            lifecycle = "Recruit"
        bot.db.set_was_in_home_guild(discord_id, True)
        pending_app = bot.db.fetch_pending_guild_application(discord_id)
        if pending_app:
            bot.db.update_guild_application_status(
                pending_app["id"], "approved", "system",
                f"Auto-approved during sync (already in {home_guild}, lifecycle={lifecycle})",
            )
    elif in_home_alliance:
        lifecycle = "Alliance"
    else:
        lifecycle = "Guest"

    bot.db.set_lifecycle_role(discord_id, lifecycle)

    if role_cache is None:
        role_cache = {r.name: r for r in member.guild.roles}

    adds, removes = [], []
    if role_cache.get("Verified"):
        adds.append(role_cache["Verified"])
    if role_cache.get("Synced"):
        adds.append(role_cache["Synced"])
    if role_cache.get("NotSynced") and role_cache["NotSynced"] in member.roles:
        removes.append(role_cache["NotSynced"])
    if role_cache.get("Unverified") and role_cache["Unverified"] in member.roles:
        removes.append(role_cache["Unverified"])
    if role_cache.get(lifecycle):
        adds.append(role_cache[lifecycle])
    for role_name in _ALL_LIFECYCLE:
        if role_name == lifecycle:
            continue
        r = role_cache.get(role_name)
        if r and r in member.roles:
            removes.append(r)
    home_guild_lc = _resolve_home_guild(bot.db).strip().lower()
    in_home_guild_now = bool(new_guild_name) and new_guild_name.strip().lower() == home_guild_lc
    if in_home_guild_now and lifecycle not in {"Inactive", "Alumni"} and role_cache.get(HOME_GUILD_ROLE_NAME):
        adds.append(role_cache[HOME_GUILD_ROLE_NAME])
    elif role_cache.get(HOME_GUILD_ROLE_NAME) and role_cache[HOME_GUILD_ROLE_NAME] in member.roles:
        removes.append(role_cache[HOME_GUILD_ROLE_NAME])

    try:
        if removes:
            await member.remove_roles(*removes, reason="Albion account sync")
        if adds:
            await member.add_roles(*adds, reason="Albion account sync")
    except discord.Forbidden:
        return True, f"Synced as {exact_name} but missing role permissions"

    if exact_name:
        new_nick = tagged_nickname_for_profile(
            bot.db,
            exact_name,
            stats,
            home_member=in_home_guild_now,
        )
        try:
            await member.edit(nick=new_nick)
        except (discord.Forbidden, discord.HTTPException):
            pass

    info_log(f"Synced {member} as {exact_name} → {lifecycle}.")
    return True, f"Synced as **{exact_name}** ({lifecycle}, {_days_in_server(member)} day(s) in server)"


class Users(commands.Cog):
    def __init__(self, bot: Bot):
        self.bot = bot
        self.bot.tree.add_command(ProfileGroup(bot))
        self._registration_nudge_last: dict[str, datetime.datetime] = {}
        info_log(f"Initialized {self.__class__.__name__} cog.")

    def cog_unload(self) -> None:
        # Reload-safety: remove the manually-added /profile group.
        try:
            self.bot.tree.remove_command("profile")
        except Exception:  # noqa: BLE001
            pass

    # On start up loop through all members in the guild and add their basic info to the database
    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready fires on every gateway reconnect; only do the heavy audit once
        # per process so a network blip doesn't trigger a full role re-audit.
        if getattr(self, "_ready_done", False):
            return
        self._ready_done = True

        self.bot.add_view(RegisterView(self.bot))

        # Persistent verification-review view. One instance covers every past
        # and future review message because the buttons use static custom_ids
        # and look up the applicant via `verification_requests`.
        self.bot.add_view(VerificationView(self.bot))

        probationary_days, member_days = _get_lifecycle_thresholds(self.bot.db)

        for guild in self.bot.guilds:
            role_cache = {r.name: r for r in guild.roles}

            for member in guild.members:
                if member.bot:
                    continue

                self.bot.db.insert_user_basic_info(
                    discord_id=str(member.id),
                    username=str(member),
                    join_date=str(member.joined_at)
                )

                profile = self.bot.db.fetch_user_profile(str(member.id))
                roles_to_add, roles_to_remove = _audit_member_roles(
                    member, profile, role_cache, probationary_days, member_days, self.bot.db
                )

                if roles_to_remove:
                    try:
                        await member.remove_roles(*roles_to_remove, reason="on_ready audit")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                if roles_to_add:
                    try:
                        await member.add_roles(*roles_to_add, reason="on_ready audit")
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        info_log("on_ready audit complete.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        raw_channel_id = self.bot.db.get_config("registration_channel_id")
        if not raw_channel_id:
            return
        try:
            registration_channel_id = int(raw_channel_id)
        except (TypeError, ValueError):
            return
        if message.channel.id != registration_channel_id:
            return
        has_image = _first_image_attachment(message) is not None
        if not has_image:
            # Deterministic rescue for common "I registered twice / it is
            # stuck / where do I upload" messages. The AI helper may also
            # watch this channel, so mark the message as handled when we reply.
            content = (message.content or "").strip()
            if not content or not _REGISTRATION_TEXT_HELP_RE.search(content):
                return
            discord_id = str(message.author.id)
            now = datetime.datetime.now(datetime.timezone.utc)
            last = self._registration_nudge_last.get(discord_id)
            cooldown = datetime.timedelta(seconds=_config_int(
                self.bot.db,
                _REGISTRATION_NUDGE_COOLDOWN_KEY,
                _DEFAULT_REGISTRATION_NUDGE_COOLDOWN_SEC,
                30,
                3600,
            ))
            if last and now - last < cooldown:
                return
            profile = self.bot.db.fetch_user_profile(discord_id)
            title, body, include_button = _registration_text_nudge(profile)
            view = RegisterView(self.bot) if include_button else None
            delete_after = _config_int(
                self.bot.db,
                _REGISTRATION_NUDGE_DELETE_AFTER_KEY,
                _DEFAULT_REGISTRATION_NUDGE_DELETE_AFTER_SEC,
                0,
                1800,
            )
            try:
                mark_unionbot_handled(self.bot, message)
                await message.reply(
                    embed=info_embed(title, body),
                    view=view,
                    mention_author=True,
                    delete_after=delete_after or None,
                    allowed_mentions=discord.AllowedMentions(
                        users=[message.author],
                        replied_user=True,
                    ),
                )
                self._registration_nudge_last[discord_id] = now
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"registration text nudge failed for {message.author}: {exc!r}")
            return

        discord_id = str(message.author.id)
        # If the modal flow is actively waiting, let the wait_for handler consume
        # this screenshot. During the Albion API lookup, wait_for is not active
        # yet, so keep a reference and let the modal flow consume it after the
        # lookup finishes.
        if discord_id in _pending_registrations:
            _pending_registration_uploads[discord_id] = message
            info_log(f"Buffered registration screenshot from {message.author} ({discord_id}).")
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        last = self._registration_nudge_last.get(discord_id)
        cooldown = datetime.timedelta(seconds=_config_int(
            self.bot.db,
            _REGISTRATION_NUDGE_COOLDOWN_KEY,
            _DEFAULT_REGISTRATION_NUDGE_COOLDOWN_SEC,
            30,
            3600,
        ))
        recently_nudged = bool(last and now - last < cooldown)

        if not recently_nudged:
            profile = self.bot.db.fetch_user_profile(discord_id)
            title, body, include_button = _registration_upload_nudge(profile)
            view = RegisterView(self.bot) if include_button else None
            embed = info_embed(title, body + "\n\nI will try to remove that screenshot so the channel stays clean.")
            delete_after = _config_int(
                self.bot.db,
                _REGISTRATION_NUDGE_DELETE_AFTER_KEY,
                _DEFAULT_REGISTRATION_NUDGE_DELETE_AFTER_SEC,
                0,
                1800,
            )
            try:
                await message.reply(
                    embed=embed,
                    view=view,
                    mention_author=True,
                    delete_after=delete_after or None,
                    allowed_mentions=discord.AllowedMentions(
                        users=[message.author],
                        replied_user=True,
                    ),
                )
                self._registration_nudge_last[discord_id] = now
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(f"registration upload nudge failed for {message.author}: {exc!r}")

        try:
            await message.delete(reason="Registration screenshot uploaded outside active flow")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return

        self.bot.db.insert_user_basic_info(
            discord_id=str(member.id),
            username=str(member),
            join_date=str(member.joined_at)
        )

        # Append a 'join' event to the lifecycle audit log so dashboards can
        # chart joiners vs leavers over time. Use the actual joined_at when
        # available so charts reflect the true moment, not on_member_join's
        # delivery time.
        try:
            self.bot.db.log_member_lifecycle_event(
                str(member.guild.id), str(member.id),
                "join",
                name=member.display_name or str(member),
                occurred_at=member.joined_at.isoformat() if member.joined_at else None,
            )
        except Exception:  # noqa: BLE001 — never let logging break onboarding
            pass

        unverified_role = discord.utils.get(member.guild.roles, name="Unverified")
        if unverified_role:
            try:
                await member.add_roles(unverified_role)
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Build and send the welcome message.
        await self._send_welcome(member)

    async def _send_welcome(self, member: discord.Member) -> None:
        """DM a welcome message and optionally post in the configured welcome channel."""
        db = self.bot.db
        home_guild = _resolve_home_guild(db)

        # Try to mention the configured registration / application channels so
        # newcomers can click straight through.
        register_chan_id = db.get_config("registration_channel_id")
        apply_chan_id    = db.get_config("application_channel_id")

        def _chan_link(cid: str | None) -> str:
            if not cid:
                return ""
            try:
                ch = member.guild.get_channel(int(cid))
                if isinstance(ch, discord.TextChannel):
                    return ch.mention
            except (TypeError, ValueError):
                pass
            return ""

        register_link = _chan_link(register_chan_id)
        apply_link    = _chan_link(apply_chan_id)

        embed = discord.Embed(
            title=f"Welcome to {member.guild.name}, {member.display_name}! 👋",
            description=(
                "Glad to have you. Here's how to get set up — pick the path "
                "that matches your situation."
            ),
            color=discord.Color.gold(),
        )

        # Path 1: already in the in-game guild
        already_in_value = (
            "Use the **Register** button"
            + (f" in {register_link}" if register_link else "")
            + ".\n"
            "You'll be promoted straight to **Recruit** once an officer "
            "verifies your character screenshot."
        )
        embed.add_field(
            name=f"🟢 Already in **{home_guild}** in-game?",
            value=already_in_value,
            inline=False,
        )

        # Path 2: plays Albion but not in the guild yet
        not_in_guild_value = (
            "Use the **Apply** button"
            + (f" in {apply_link}" if apply_link else "")
            + ".\n"
            "We'll pull your stats from the Albion API and an officer will "
            "review your application."
        )
        embed.add_field(
            name=f"🟡 Play Albion but not in **{home_guild}**?",
            value=not_in_guild_value,
            inline=False,
        )

        # Path 3: doesn't play yet
        embed.add_field(
            name="🔵 New to Albion Online?",
            value=(
                "No problem — install the game ([albiononline.com]"
                "(https://albiononline.com/)), make a character on the "
                "**Americas** server, then come back and **Apply** above. "
                "Ask in chat if you need help getting started!"
            ),
            inline=False,
        )

        embed.add_field(
            name="📌 Quick tips",
            value=(
                "• Read the rules / pinned messages in the welcome channels.\n"
                "• Once verified, check `/leaderboard`, `/profile view`, and "
                "the bounty / event boards.\n"
                "• Questions? Ping an officer — we're happy to help."
            ),
            inline=False,
        )
        embed.set_footer(text=f"You joined {member.guild.name}")

        # Try DM first (most personal).
        try:
            await member.send(embed=embed)
            info_log(f"Sent welcome DM to {member} ({member.id}).")
        except (discord.Forbidden, discord.HTTPException):
            info_log(f"Could not DM welcome to {member} ({member.id}); DMs likely closed.")

        # Optional: also post a brief shout in a configured welcome channel.
        welcome_chan_id = db.get_config("welcome_channel_id")
        if not welcome_chan_id:
            return
        try:
            ch = member.guild.get_channel(int(welcome_chan_id))
        except (TypeError, ValueError):
            ch = None
        if not isinstance(ch, discord.TextChannel):
            return

        public_embed = discord.Embed(
            description=(
                f"👋 Welcome **{member.mention}** to **{member.guild.name}**!\n\n"
                f"Check your DMs for setup steps, or "
                + (f"head to {register_link} to register" if register_link else "click the **Register** button in your registration channel")
                + (f" / {apply_link} to apply." if apply_link else " / **/apply** to apply to the guild.")
            ),
            color=discord.Color.gold(),
        )
        try:
            await ch.send(
                content=member.mention,
                embed=public_embed,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

async def setup(bot: Bot):
    await bot.add_cog(Users(bot))
