"""Guild application flow.

Outsiders click an "Apply" button on a posted embed, fill in their Albion name,
and the application is forwarded to a review channel where Officer/Captain/
Steward staff approve or deny via buttons. Approval grants the Recruit
lifecycle role, DMs the applicant, and triggers the Albion sync to fill in
their profile.
"""

from cogs._typing import Bot
import asyncio
import datetime
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from debug import info_log, error_log
from cogs.users_profile import sync_member_to_albion
from utils import (
    autocomplete_guild_name,
    error_embed,
    info_embed,
    is_officer,
    success_embed,
)
import albion_api
from config import HOME_GUILD_NAME


# In-flight application submits (discord_id strings). Prevents the same user
# from double-submitting while the Albion API lookup is still running.
_pending_applications: set[str] = set()

# Albion character names: 2-16 chars, letters/digits/underscore/dash only.
# Mirrors the in-game name rules. Stricter than the modal’s 32-char cap so
# we reject obvious garbage before hitting the API.
_ALBION_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{2,16}$")


def _validate_albion_name(raw: str) -> tuple[str, str | None]:
    """Normalise and validate an applicant's name input.

    Returns ``(cleaned, error)``. ``error`` is None on success, otherwise a
    short human-readable reason. ``cleaned`` is the trimmed input.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return cleaned, "Your Albion character name can\u2019t be blank."
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


REVIEWER_ROLES = ("Captain", "Officer", "Steward")
APPLY_CHANNEL_KEY = "application_channel_id"
REVIEW_CHANNEL_KEY = "application_review_channel_id"
MIN_TOTAL_FAME_KEY = "application_min_total_fame"
MIN_COMBAT_FAME_KEY = "application_min_combat_fame"
MIN_MEMBERSHIP_HOURS_KEY = "application_min_membership_hours"
DEFAULT_MIN_MEMBERSHIP_HOURS = 72
HOME_GUILD_KEY = "home_guild_name"
DEFAULT_HOME_GUILD = HOME_GUILD_NAME
POLL_INTERVAL_MINUTES = 10

# Application status values:
#   pending              — awaiting reviewer
#   approved_pending_join — reviewer approved; waiting for in-game guild join
#   approved             — confirmed in guild, Recruit role granted
#   denied / errored
STATUS_PENDING = "pending"
STATUS_AWAITING_JOIN = "approved_pending_join"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"


def _get_home_guild(db) -> str:
    """Resolve the in-game home guild applicants must join.

    Resolution order:
      1. If ``home_guild_name`` config is set AND matches a guild row in the
         ``guilds`` table (case-insensitive), return the canonical name from
         that row (auto-corrects casing/whitespace drift).
      2. If exactly one guild is tracked in the database, return its name and
         persist it as the home guild for future calls.
      3. If a config value is set but doesn't match any tracked guild, fall
         back to the configured value as-is (manual override).
      4. Otherwise return ``DEFAULT_HOME_GUILD``.
    """
    configured = (db.get_config(HOME_GUILD_KEY) or "").strip()
    try:
        tracked = db.fetch_all_guilds() or []
    except Exception:  # noqa: BLE001
        tracked = []

    tracked_names = [(g.get("guild_name") or "").strip() for g in tracked if g.get("guild_name")]

    if configured:
        for name in tracked_names:
            if name.lower() == configured.lower():
                if name != configured:
                    # Persist the canonical casing so logs/embeds match.
                    db.set_config(HOME_GUILD_KEY, name)
                return name
        return configured

    if len(tracked_names) == 1:
        canonical = tracked_names[0]
        db.set_config(HOME_GUILD_KEY, canonical)
        return canonical

    return DEFAULT_HOME_GUILD


def _sync_recruit_row(
    db, albion_name: str, *,
    discord_id: str | None = None,
    status: str | None = None,
    source: str = "application",
) -> None:
    """Seed/advance the recruitment funnel for an applicant.

    On first submit: inserts a recruit row (status='contacted') if the name
    isn't already tracked. On approval: bumps the existing row to the given
    ``status`` and stores the discord id. Best-effort — never raises into
    the calling flow; the application flow must not fail if the recruits
    table is unavailable.
    """
    try:
        if not albion_name:
            return
        existing = db.recruit_find_by_name(albion_name)
        if existing is None:
            new_id = db.recruit_add(
                albion_name=albion_name,
                source=source,
                recruiter_id=None,
            )
            if new_id and (status or discord_id):
                db.recruit_update(
                    int(new_id), status=status, discord_id=discord_id,
                )
            return
        # Already tracked — bump status/discord_id if changed.
        kwargs: dict = {}
        if status and existing.get("status") != status:
            # Don't regress a recruit who's already past this stage.
            current = existing.get("status") or "contacted"
            try:
                current_idx = db._RECRUIT_STAGES.index(current)
                new_idx = db._RECRUIT_STAGES.index(status)
                if new_idx > current_idx:
                    kwargs["status"] = status
            except ValueError:
                kwargs["status"] = status
        if discord_id and existing.get("discord_id") != str(discord_id):
            kwargs["discord_id"] = str(discord_id)
        if kwargs:
            db.recruit_update(int(existing["id"]), **kwargs)
    except Exception as exc:  # noqa: BLE001
        error_log(f"_sync_recruit_row({albion_name!r}) failed: {exc!r}")


def _is_reviewer(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name in REVIEWER_ROLES for r in member.roles)


def _compute_fame(stats: dict) -> tuple[int, int]:
    """Return (combat_fame, total_fame) from a parse_stats() dict.

    combat_fame = lifetime KillFame (PvP).
    total_fame  = combat + PvE + gathering + crafting + fishing + farming.
    """
    combat = int(stats.get("kill_fame") or 0)
    total = combat + sum(int(stats.get(k) or 0) for k in (
        "pve_total", "gather_all", "crafting_fame", "fishing_fame", "farming_fame",
    ))
    return combat, total


def _get_fame_thresholds(db) -> tuple[int, int]:
    """Return (min_total_fame, min_combat_fame). 0 means no gate."""
    return (
        int(db.get_config(MIN_TOTAL_FAME_KEY) or 0),
        int(db.get_config(MIN_COMBAT_FAME_KEY) or 0),
    )


def _get_min_membership_hours(db) -> int:
    """Return required server-membership hours before /apply works.

    0 = no gate. Defaults to ``DEFAULT_MIN_MEMBERSHIP_HOURS`` (72) until
    explicitly overridden via ``/apply set-wait``.
    """
    raw = db.get_config(MIN_MEMBERSHIP_HOURS_KEY)
    if raw is None or raw == "":
        return DEFAULT_MIN_MEMBERSHIP_HOURS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_MEMBERSHIP_HOURS


def _profile_is_home_alliance(db, profile: dict | None, *, home_guild: str | None = None) -> bool:
    if not profile:
        return False
    guild_name = (profile.get("guild_name") or "").strip()
    resolved_home_guild = (home_guild or _get_home_guild(db)).strip()
    if guild_name and guild_name.lower() == resolved_home_guild.lower():
        return False
    home_alliance_id = (db.get_config("home_alliance_id") or "").strip()
    profile_alliance_id = (profile.get("alliance_id") or "").strip()
    return bool(home_alliance_id and profile_alliance_id and profile_alliance_id == home_alliance_id)


def _stats_in_home_alliance(db, stats: dict, *, home_guild: str) -> bool:
    guild_name = (stats.get("guild_name") or "").strip()
    if guild_name and guild_name.lower() == home_guild.strip().lower():
        return False
    home_alliance_id = (db.get_config("home_alliance_id") or "").strip()
    stats_alliance_id = (stats.get("alliance_id") or "").strip()
    return bool(home_alliance_id and stats_alliance_id and stats_alliance_id == home_alliance_id)


# ── persistent views ─────────────────────────────────────────────────────────

class ApplyView(discord.ui.View):
    """Persistent view containing the public Apply button."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot: Bot = bot

    @discord.ui.button(label="Apply · Postular · Candidatar-se", style=discord.ButtonStyle.primary, custom_id="guild_apply_button")
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = self.bot.db.fetch_pending_guild_application(str(interaction.user.id))
        if existing:
            await interaction.response.send_message(
                embed=info_embed(
                    "Application already pending",
                    f"You already have a pending application (**#{existing['id']}**). Wait for staff to review it.",
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ApplyModal(self.bot))


class ApplyModal(discord.ui.Modal, title="Guild Application"):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    albion_name = discord.ui.TextInput(
        label="In-game name (Albion)",
        placeholder="Your Albion Online character name",
        required=True,
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Discord gives us 3 seconds to acknowledge a modal submit. The
        # checks below include a Discord API call (fetch_channel) and a
        # blocking Albion API lookup, both of which can easily exceed
        # that. Defer FIRST so the interaction stays alive; use
        # followup.send for everything downstream. Reply must be
        # ephemeral so the applicant's typed name isn't broadcast.
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException as exc:
            error_log(f"Application defer failed for {interaction.user.id}: {exc!r}")
            return

        raw_name = self.albion_name.value
        name, name_err = _validate_albion_name(raw_name)
        if name_err:
            await interaction.followup.send(
                embed=error_embed("Check the name", name_err),
                ephemeral=True,
            )
            return

        discord_id = str(interaction.user.id)

        # Race: re-check pending after modal submit
        if self.bot.db.fetch_pending_guild_application(discord_id):
            await interaction.followup.send(
                embed=info_embed("Already pending", "You already have a pending application."),
                ephemeral=True,
            )
            return

        # Politely redirect long-tenured members who try to re-apply. Recruits
        # are *exactly* the people who should be applying (they registered,
        # now they want the formal application), so they pass through. Staff
        # always pass through so they can test the flow.
        existing_profile = self.bot.db.fetch_user_profile(discord_id) or {}
        is_staff = _is_reviewer(interaction.user) if isinstance(interaction.user, discord.Member) else False
        if (
            not is_staff
            and existing_profile.get("albion_player_id")
            and _profile_is_home_alliance(self.bot.db, existing_profile)
        ):
            guild_name = (existing_profile.get("guild_name") or "an allied guild").strip()
            await interaction.followup.send(
                embed=info_embed(
                    "You are already Alliance",
                    f"Your Discord is linked as **{existing_profile.get('albion_name') or 'your character'}** "
                    f"in **{guild_name}**, which is already part of our alliance.",
                    hint=(
                        "You do not need a HomeGuild guild application. "
                        "If you are actually transferring into HomeGuild, open a help ticket or ask an officer."
                    ),
                ),
                ephemeral=True,
            )
            return
        if (
            not is_staff
            and existing_profile.get("albion_player_id")
            and (existing_profile.get("lifecycle_role") or "") in ("Probationary", "Member")
        ):
            await interaction.followup.send(
                embed=info_embed(
                    "You\u2019re already registered",
                    f"Your profile is already linked as **{existing_profile.get('albion_name') or 'your character'}** "
                    f"with lifecycle **{existing_profile.get('lifecycle_role')}**.",
                    hint="If something is wrong with your link, ping a staff member instead of re-applying.",
                ),
                ephemeral=True,
            )
            return

        # In-flight lock — user clicked Apply twice while the API was busy.
        if discord_id in _pending_applications:
            await interaction.followup.send(
                embed=info_embed(
                    "Hold on",
                    "Your previous application submission is still being processed. Give it a moment.",
                ),
                ephemeral=True,
            )
            return

        # Membership-age gate. New arrivals must hang out in the server
        # for a configurable cooldown (default 72h) before applying. This
        # weeds out drive-by joiners and gives officers a chance to vibe-
        # check them in #general first. Staff and existing reviewers
        # bypass.
        if not is_staff and isinstance(interaction.user, discord.Member):
            min_hours = _get_min_membership_hours(self.bot.db)
            if min_hours > 0:
                joined_at = interaction.user.joined_at
                if joined_at is None:
                    # Discord didn't give us a join timestamp — fail open
                    # rather than blocking forever.
                    error_log(
                        f"Application gate: no joined_at for {discord_id}; allowing through."
                    )
                else:
                    now = discord.utils.utcnow()
                    elapsed = now - joined_at
                    required = datetime.timedelta(hours=min_hours)
                    if elapsed < required:
                        ready_at = joined_at + required
                        # Discord <t:...:R> renders a live countdown in the
                        # applicant's local time. Much friendlier than "wait
                        # 47 hours".
                        unix_ts = int(ready_at.timestamp())
                        remaining = required - elapsed
                        hours_left = int(remaining.total_seconds() // 3600)
                        mins_left = int((remaining.total_seconds() % 3600) // 60)
                        await interaction.followup.send(
                            embed=info_embed(
                                "Hang out a bit first",
                                f"New members need to be in the server for **{min_hours} hours** "
                                f"before applying. You can apply <t:{unix_ts}:R> "
                                f"(in about {hours_left}h {mins_left}m).",
                                hint=(
                                    "While you wait: introduce yourself in chat, hop in "
                                    "voice, and check the rules / event channels so you "
                                    "know what you're signing up for."
                                ),
                            ),
                            ephemeral=True,
                        )
                        info_log(
                            f"Application gated: {interaction.user} ({discord_id}) "
                            f"in server {elapsed.total_seconds()/3600:.1f}h "
                            f"< required {min_hours}h."
                        )
                        return
        review_channel_id = self.bot.db.get_config(REVIEW_CHANNEL_KEY) or self.bot.db.get_config("officer_channel_id")
        review_channel = None
        resolve_error: str | None = None
        if review_channel_id:
            try:
                rc_id = int(review_channel_id)
            except (TypeError, ValueError):
                resolve_error = f"stored review channel id `{review_channel_id}` is not a number"
            else:
                review_channel = interaction.guild.get_channel(rc_id)
                if review_channel is None:
                    # Cache miss — hit the API. Channel may exist but not
                    # be in the bot's local cache (rare but possible after
                    # a restart or permission change).
                    try:
                        fetched = await self.bot.fetch_channel(rc_id)
                        if isinstance(fetched, discord.TextChannel) and fetched.guild and fetched.guild.id == interaction.guild.id:
                            review_channel = fetched
                        else:
                            resolve_error = (
                                f"channel `{rc_id}` is not a text channel in this server "
                                "(was it moved or deleted?)"
                            )
                    except discord.NotFound:
                        resolve_error = f"channel `{rc_id}` no longer exists (it was deleted)"
                    except discord.Forbidden:
                        resolve_error = (
                            f"the bot can't see channel `{rc_id}` — it needs the "
                            "**View Channel** permission there"
                        )
                    except discord.HTTPException as exc:
                        resolve_error = f"Discord API error fetching channel `{rc_id}`: {exc}"
        if not review_channel:
            detail = resolve_error or "No review channel is set."
            error_log(
                f"Application submission blocked: {detail} "
                f"(applicant={interaction.user.id}). "
                "Admin fix: /apply set-review-channel channel:<#officer-channel>"
            )
            await interaction.followup.send(
                embed=error_embed(
                    "Applications not configured",
                    detail,
                    hint=(
                        "Admin: run `/apply set-review-channel` to pick a new "
                        "review channel, then have the applicant click Apply again."
                    ),
                ),
                ephemeral=True,
            )
            return

        _pending_applications.add(discord_id)
        try:
            home_guild = _get_home_guild(self.bot.db)

            # Fetch the applicant's Albion stats so reviewers can compare against thresholds.
            # Use the full candidate list so we can both (a) bias picking toward the
            # character already in the home guild and (b) warn officers when the
            # name is ambiguous.
            loop = asyncio.get_running_loop()
            candidates = await loop.run_in_executor(
                None, lambda: albion_api.find_player_candidates(name, "americas")
            )
            if not candidates:
                await interaction.followup.send(
                    embed=error_embed(
                        "Character not found",
                        f"`{name}` was not found on the **Americas** server.",
                        hint="Check the spelling (capitalization counts) and try again.",
                    ),
                    ephemeral=True,
                )
                return

            pref = home_guild.lower()
            def _score(p):
                return (
                    1 if (p.get("GuildName") or "").strip().lower() == pref else 0,
                    1 if (p.get("GuildId") or "") else 0,
                    1 if (p.get("AllianceId") or "") else 0,
                    int(p.get("KillFame") or 0) + int(p.get("DeathFame") or 0),
                )
            best = max(candidates, key=_score)
            player_id, exact_name = best["Id"], best["Name"]
            ambiguous = len(candidates) > 1

            # Reject if this Albion character is already linked to a different
            # Discord account. The most common cause is a typo onto someone
            # else's name; the rare legitimate case (one person, two Discord
            # accounts) needs staff intervention anyway.
            other = self.bot.db.fetch_user_profile_by_player_id(player_id)
            if other and other.get("discord_id") and str(other["discord_id"]) != discord_id:
                await interaction.followup.send(
                    embed=error_embed(
                        "That character is already linked",
                        f"**{exact_name}** is already linked to another Discord account.",
                        hint="If that\u2019s you on a different account, contact a staff member \u2014 don\u2019t submit a duplicate application.",
                    ),
                    ephemeral=True,
                )
                return

            raw = await loop.run_in_executor(None, lambda: albion_api.get_player_stats(player_id, "americas"))
            stats = albion_api.parse_stats(raw) if raw else {}
            combat_fame, total_fame = _compute_fame(stats)
            min_total, min_combat = _get_fame_thresholds(self.bot.db)
            meets_total = total_fame >= min_total
            meets_combat = combat_fame >= min_combat
            meets_all = meets_total and meets_combat

            api_guild = (stats.get("guild_name") or "").strip()
            in_home_guild = bool(api_guild) and api_guild.lower() == home_guild.lower()
            in_home_alliance = _stats_in_home_alliance(self.bot.db, stats, home_guild=home_guild)
            if not is_staff and in_home_alliance:
                await interaction.followup.send(
                    embed=info_embed(
                        "You are already Alliance",
                        f"**{exact_name}** is in **{api_guild or 'an allied guild'}**, which is already part of our alliance.",
                        hint=(
                            "No guild application is needed for alliance access. "
                            "If you are actually trying to transfer into HomeGuild, open a help ticket or ask an officer."
                        ),
                    ),
                    ephemeral=True,
                )
                return

            app_id = self.bot.db.insert_guild_application(discord_id, exact_name)

            # Seed the recruitment funnel so every applicant shows up in
            # /recruit list — no extra step for officers.
            _sync_recruit_row(
                self.bot.db, exact_name,
                discord_id=str(discord_id),
                source="application",
            )

            embed_color = discord.Color.green() if in_home_guild else (
                discord.Color.orange() if meets_all else discord.Color.dark_gold()
            )
            embed = discord.Embed(title="New Guild Application", color=embed_color)
            embed.add_field(name="Discord", value=interaction.user.mention, inline=True)
            embed.add_field(name="Albion Name", value=exact_name, inline=True)
            guild_display = api_guild or "\u2014"
            if in_home_guild:
                guild_display = f"{api_guild} \u2705"
            embed.add_field(name="Guild", value=guild_display, inline=True)

            def _fmt(value: int, threshold: int, ok: bool) -> str:
                badge = "PASS" if ok else "BELOW"
                if threshold > 0:
                    return f"{value:,} / {threshold:,} ({badge})"
                return f"{value:,}"

            embed.add_field(name="Total Fame", value=_fmt(total_fame, min_total, meets_total), inline=True)
            embed.add_field(name="Combat Fame", value=_fmt(combat_fame, min_combat, meets_combat), inline=True)
            embed.add_field(name="Avg IP", value=f"{stats.get('average_item_power', 0.0):.0f}", inline=True)
            if not meets_all:
                embed.add_field(
                    name="Requirements",
                    value="Applicant is **below** one or more configured thresholds.",
                    inline=False,
                )
            if ambiguous:
                # Surface duplicate-name risk to reviewers so they can sanity
                # check the linked player_id against an in-game screenshot.
                others = [c for c in candidates if c.get("Id") != player_id][:3]
                others_lines = [
                    f"\u2022 `{(c.get('GuildName') or '\u2014')}` \u2014 KF {int(c.get('KillFame') or 0):,}"
                    for c in others
                ]
                embed.add_field(
                    name=f"\u26a0\ufe0f {len(candidates)} characters share this name",
                    value="Picked the one in our home guild / with the strongest signal. "
                          "Other matches:\n" + ("\n".join(others_lines) or "\u2014"),
                    inline=False,
                )
            embed.set_footer(text=f"Application #{app_id} \u2022 player_id: {player_id}")

            try:
                msg = await review_channel.send(embed=embed, view=ApplicationReviewView(self.bot))
            except discord.Forbidden:
                self.bot.db.update_guild_application_status(app_id, "errored", "system", "Cannot post to review channel")
                await interaction.followup.send(
                    embed=error_embed(
                        "Could not post application",
                        "The bot lacks permission in the review channel.",
                        hint="Contact an admin to fix the channel permissions.",
                    ),
                    ephemeral=True,
                )
                return

            self.bot.db.set_guild_application_message(app_id, str(msg.id))

            await interaction.followup.send(
                embed=success_embed(
                    f"Application #{app_id} submitted",
                    f"Submitted as **{exact_name}**. Staff will review it shortly."
                    + (
                        f"\n\nYou\u2019re already in **{home_guild}** in-game \u2014 reviewers will see that and "
                        "approval is usually quick."
                        if in_home_guild else
                        f"\n\nIf you haven\u2019t already, request to join **{home_guild}** in-game so we can finish "
                        "verifying you."
                    ),
                ),
                ephemeral=True,
            )
            info_log(
                f"{interaction.user} submitted guild application #{app_id} as {exact_name} "
                f"(combat={combat_fame:,}, total={total_fame:,}, in_home_guild={in_home_guild}, "
                f"candidates={len(candidates)})."
            )
        finally:
            _pending_applications.discard(discord_id)


class ApplicationReviewView(discord.ui.View):
    """Persistent Approve/Deny buttons. Looks up the application by the
    enclosing message's ID, so it survives bot restarts without per-app state.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _resolve(self, interaction: discord.Interaction):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only Captains, Officers, and Stewards can review applications."),
                ephemeral=True,
            )
            return None
        app = self.bot.db.fetch_guild_application_by_message(str(interaction.message.id))
        if not app:
            await interaction.response.send_message(
                embed=error_embed("Application not tracked", "This application is no longer in the database."),
                ephemeral=True,
            )
            return None
        if app["status"] != STATUS_PENDING:
            await interaction.response.send_message(
                embed=info_embed("Already resolved", f"Application **#{app['id']}** is **{app['status']}**, not pending."),
                ephemeral=True,
            )
            return None
        return app

    async def _finalize_embed(self, interaction: discord.Interaction, color: discord.Color, status_line: str) -> None:
        try:
            embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
            embed.color = color
            embed.add_field(name="Status", value=status_line, inline=False)
            await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden):
            pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="guild_app_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "You don't have permission to review applications."),
                ephemeral=True,
            )
            return
        app = await self._resolve(interaction)
        if app is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        member = interaction.guild.get_member(int(app["discord_id"]))
        if not member:
            self.bot.db.update_guild_application_status(
                app["id"], STATUS_DENIED, str(interaction.user.id), "Applicant left server"
            )
            await self._finalize_embed(
                interaction, discord.Color.red(), f"Auto-denied \u2014 applicant left server (by {interaction.user.mention})"
            )
            await interaction.followup.send(
                embed=info_embed("Auto-denied", "Applicant is no longer in the server."),
                ephemeral=True,
            )
            return

        # Sync to fill profile + give Verified/Synced. If the applicant is already in
        # the home in-game guild, sync_member_to_albion will auto-promote their lifecycle
        # to Recruit; we mirror that on the application status so the two stay consistent.
        try:
            _success, message = await sync_member_to_albion(self.bot, member, app["albion_name"])
        except Exception as exc:  # noqa: BLE001
            error_log(f"Sync failed during application approve #{app['id']}: {exc}")
            message = f"Sync error: {exc}"

        home_guild = _get_home_guild(self.bot.db)

        # Detect post-sync state: did the auto-promote rule fire?
        synced_profile = self.bot.db.fetch_user_profile(str(member.id)) or {}
        already_in_home = (
            (synced_profile.get("guild_name") or "").strip().lower() == home_guild.lower()
        )
        already_in_alliance = _profile_is_home_alliance(
            self.bot.db,
            synced_profile,
            home_guild=home_guild,
        )

        if already_in_home:
            # Applicant is already in-game in the home guild — finalize the application
            # immediately (skip the "awaiting in-game join" stage). The actual lifecycle
            # role was decided by sync_member_to_albion based on tenure.
            assigned_role = synced_profile.get("lifecycle_role") or "Recruit"
            self.bot.db.update_guild_application_status(
                app["id"], STATUS_APPROVED, str(interaction.user.id), None
            )
            _sync_recruit_row(
                self.bot.db, app["albion_name"],
                discord_id=str(member.id), status="registered",
            )
            try:
                await member.send(
                    f"Your guild application has been **approved**! You're already in "
                    f"**{home_guild}** in-game, so you've been promoted to **{assigned_role}** right away.\n\n"
                    f"Sync result: {message}"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self._finalize_embed(
                interaction,
                discord.Color.green(),
                f"Approved by {interaction.user.mention} \u2014 already in **{home_guild}**, promoted to **{assigned_role}**.\nSync: {message}",
            )
            await interaction.followup.send(
                embed=success_embed(
                    f"Application #{app['id']} approved",
                    f"{member.mention} was already in **{home_guild}** — promoted to **{assigned_role}**.",
                ),
                ephemeral=True,
            )
            info_log(
                f"{interaction.user} approved guild application #{app['id']} for {member}; "
                f"already in {home_guild}, finalized immediately as {assigned_role}."
            )
            return

        if already_in_alliance:
            allied_guild = (synced_profile.get("guild_name") or "an allied guild").strip()
            self.bot.db.update_guild_application_status(
                app["id"],
                STATUS_DENIED,
                str(interaction.user.id),
                f"Closed without action: already an Alliance member in {allied_guild}",
            )
            _sync_recruit_row(
                self.bot.db, app["albion_name"],
                discord_id=str(member.id), status="contacted",
            )
            try:
                await member.send(
                    f"Your HomeGuild guild application was closed because you're already registered "
                    f"as an **Alliance** member in **{allied_guild}**.\n\n"
                    "You do not need a guild application for alliance access. If you are actually transferring "
                    "into HomeGuild, please open a help ticket or ask an officer."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            await self._finalize_embed(
                interaction,
                discord.Color.blue(),
                f"Closed by {interaction.user.mention} — already an **Alliance** member in **{allied_guild}**.\nSync: {message}",
            )
            await interaction.followup.send(
                embed=info_embed(
                    f"Application #{app['id']} closed",
                    f"{member.mention} is already registered as **Alliance** in **{allied_guild}**. "
                    "No HomeGuild join task was created.",
                ),
                ephemeral=True,
            )
            info_log(
                f"{interaction.user} closed guild application #{app['id']} for {member}; "
                f"already alliance member in {allied_guild}."
            )
            return

        # Standard path: applicant must still join the guild in-game.
        self.bot.db.update_guild_application_status(
            app["id"], STATUS_AWAITING_JOIN, str(interaction.user.id), None
        )
        _sync_recruit_row(
            self.bot.db, app["albion_name"],
            discord_id=str(member.id), status="discord",
        )

        try:
            await member.send(
                f"Your guild application has been **approved**!\n\n"
                f"Please join **{home_guild}** in-game. Once the Albion API shows you in the guild "
                f"(usually within a few minutes), you'll automatically be promoted to **Recruit**.\n\n"
                f"Sync result: {message}"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        await self._finalize_embed(
            interaction,
            discord.Color.gold(),
            f"Approved by {interaction.user.mention} \u2014 awaiting in-game join to **{home_guild}**.\nSync: {message}",
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Application #{app['id']} approved",
                f"Waiting for {member.mention} to join **{home_guild}** in-game. They'll be promoted to **Recruit** automatically once the Albion API confirms.",
            ),
            ephemeral=True,
        )
        info_log(
            f"{interaction.user} approved guild application #{app['id']} for {member}; "
            f"awaiting in-game join to {home_guild}."
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="guild_app_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "You don't have permission to review applications."),
                ephemeral=True,
            )
            return
        app = await self._resolve(interaction)
        if app is None:
            return
        await interaction.response.send_modal(DenyReasonModal(self.bot, app["id"]))


class DenyReasonModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Reason (shown to applicant)",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=400,
    )

    def __init__(self, bot, app_id: int):
        super().__init__()
        self.bot = bot
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reason = self.reason.value.strip() or "No reason provided."

        app = self.bot.db.fetch_guild_application(self.app_id)
        if not app or app["status"] != "pending":
            await interaction.response.send_message(
                embed=info_embed("Already resolved", "This application is no longer pending."),
                ephemeral=True,
            )
            return

        self.bot.db.update_guild_application_status(
            self.app_id, "denied", str(interaction.user.id), reason
        )

        member = interaction.guild.get_member(int(app["discord_id"]))
        if member:
            try:
                await member.send(
                    f"Your guild application was **denied**.\nReason: {reason}"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        # Edit the original review message
        try:
            embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
            embed.color = discord.Color.red()
            embed.add_field(
                name="Status",
                value=f"Denied by {interaction.user.mention}\nReason: {reason}",
                inline=False,
            )
            await interaction.message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden):
            pass

        await interaction.response.send_message(
            embed=success_embed(f"Application #{self.app_id} denied", f"Reason: {reason}"),
            ephemeral=True,
        )
        info_log(f"{interaction.user} denied guild application #{self.app_id}: {reason}")


# ── cog ──────────────────────────────────────────────────────────────────────

class Applications(commands.Cog):
    apply_group = app_commands.Group(name="apply", description="Guild application commands.")

    def __init__(self, bot: Bot):
        self.bot = bot
        self.poll_pending_joins.start()

    def cog_unload(self) -> None:
        self.poll_pending_joins.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        # Only register persistent views once per process — on_ready fires on every reconnect.
        if getattr(self, "_ready_done", False):
            return
        self._ready_done = True
        # Persistent views: one global Apply view + one global review view.
        self.bot.add_view(ApplyView(self.bot))
        self.bot.add_view(ApplicationReviewView(self.bot))

    # ── background poll: confirm in-game guild membership ────────────────────

    @tasks.loop(minutes=POLL_INTERVAL_MINUTES)
    async def poll_pending_joins(self) -> None:
        rows = self.bot.db.fetch_guild_applications_by_status(STATUS_AWAITING_JOIN)
        if not rows:
            return

        home_guild = _get_home_guild(self.bot.db)
        loop = asyncio.get_running_loop()

        for app in rows:
            try:
                profile = self.bot.db.fetch_user_profile(app["discord_id"])
                player_id = profile.get("albion_player_id") if profile else None

                if not player_id:
                    lookup = await loop.run_in_executor(
                        None,
                        lambda n=app["albion_name"], g=home_guild: albion_api.get_player_id(
                            n, "americas", prefer_guild_name=g
                        ),
                    )
                    if not lookup:
                        continue
                    player_id, _exact = lookup

                raw = await loop.run_in_executor(
                    None, lambda pid=player_id: albion_api.get_player_stats(pid, "americas")
                )
                stats = albion_api.parse_stats(raw) if raw else {}
                current_guild = (stats.get("guild_name") or "").strip()

                if current_guild != home_guild:
                    continue

                # Confirmed in guild — promote to Recruit.
                await self._promote_to_recruit(app, current_guild)
            except Exception as exc:  # noqa: BLE001
                error_log(f"poll_pending_joins error for app #{app.get('id')}: {exc}")

    @poll_pending_joins.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def _promote_to_recruit(self, app: dict, confirmed_guild: str) -> None:
        """Grant the appropriate lifecycle role + DM applicant + finalize application status.

        The lifecycle role is *tenure-aware*: a returning Member/Veteran who re-applies
        after going Inactive/Alumni keeps their tenure-earned role instead of being
        demoted to Recruit. New/short-tenured applicants get Recruit.
        """
        # Local import to avoid a circular dependency at module load time.
        from cogs.users_profile import _get_lifecycle_thresholds, _member_since_iso, _ALL_LIFECYCLE
        from config import derive_lifecycle

        # Locate the member across guilds the bot is in.
        target_member = None
        target_guild = None
        for guild in self.bot.guilds:
            m = guild.get_member(int(app["discord_id"]))
            if m:
                target_member, target_guild = m, guild
                break

        if not target_member or not target_guild:
            self.bot.db.update_guild_application_status(
                app["id"], STATUS_DENIED, "system", "Applicant not in any Discord guild during promotion"
            )
            return

        # Decide lifecycle: derive from tenure, then bump Probationary up to Recruit
        # (since they're now confirmed in-game). Member/Veteran are preserved.
        probationary_days, member_days = _get_lifecycle_thresholds(self.bot.db)
        derived = derive_lifecycle(_member_since_iso(target_member), probationary_days, member_days)
        new_lifecycle = "Recruit" if derived == "Probationary" else derived

        target_role = discord.utils.get(target_guild.roles, name=new_lifecycle)
        roles_to_remove = []
        for role_name in _ALL_LIFECYCLE:
            if role_name == new_lifecycle:
                continue
            r = discord.utils.get(target_guild.roles, name=role_name)
            if r and r in target_member.roles:
                roles_to_remove.append(r)
        try:
            if roles_to_remove:
                await target_member.remove_roles(*roles_to_remove, reason=f"Application #{app['id']} confirmed in-game join")
            if target_role and target_role not in target_member.roles:
                await target_member.add_roles(target_role, reason=f"Application #{app['id']} confirmed in-game join")
            self.bot.db.set_lifecycle_role(str(target_member.id), new_lifecycle)
        except discord.Forbidden:
            error_log(f"Missing permission to assign {new_lifecycle} role to {target_member}.")

        self.bot.db.update_guild_application_status(
            app["id"], STATUS_APPROVED, app.get("reviewed_by") or "system",
            f"Confirmed in {confirmed_guild} (lifecycle={new_lifecycle})",
        )
        _sync_recruit_row(
            self.bot.db, app["albion_name"],
            discord_id=str(target_member.id), status="registered",
        )

        try:
            await target_member.send(
                f"Welcome! You've been confirmed in **{confirmed_guild}** in-game and "
                f"promoted to **{new_lifecycle}**."
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        try:
            from cogs.users_profile import post_new_member_shoutout
            await post_new_member_shoutout(
                self.bot, target_member,
                lifecycle=new_lifecycle, home_guild=confirmed_guild,
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"shout-out for {target_member} failed: {exc!r}")

        # Update the original review embed if we can find the channel/message.
        review_channel_id = self.bot.db.get_config(REVIEW_CHANNEL_KEY) or self.bot.db.get_config("officer_channel_id")
        if review_channel_id and app.get("message_id"):
            channel = target_guild.get_channel(int(review_channel_id))
            if channel:
                try:
                    msg = await channel.fetch_message(int(app["message_id"]))
                    embed = msg.embeds[0] if msg.embeds else discord.Embed()
                    embed.color = discord.Color.green()
                    embed.add_field(
                        name="In-game Join",
                        value=f"Confirmed in **{confirmed_guild}** \u2014 **{new_lifecycle}** role granted.",
                        inline=False,
                    )
                    await msg.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        info_log(f"Application #{app['id']} confirmed in {confirmed_guild}; promoted {target_member} to {new_lifecycle}.")

    @apply_group.command(name="setup", description="Post the Apply button in the current channel.")
    @app_commands.default_permissions(manage_guild=True)
    async def setup(self, interaction: discord.Interaction):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains, Officers, and Stewards can post the apply embed."),
                ephemeral=True,
            )
            return

        home_guild = _get_home_guild(self.bot.db)
        min_total, min_combat = _get_fame_thresholds(self.bot.db)

        embed = discord.Embed(
            title=f"Apply to Join {home_guild} \u00b7 Postular \u00b7 Candidatar-se",
            description=(
                f"\ud83c\uddec\ud83c\udde7 Want to join **{home_guild}** in Albion Online? Submit an application "
                "here and our staff will review it. **Already in the guild in-game?** Use `/profile sync` instead.\n"
                f"\ud83c\uddea\ud83c\uddf8 \u00bfQuieres unirte a **{home_guild}** en Albion Online? Env\u00eda tu "
                "postulaci\u00f3n aqu\u00ed y el staff la revisar\u00e1. **\u00bfYa est\u00e1s en el gremio?** Usa `/profile sync`.\n"
                f"\ud83c\udde7\ud83c\uddf7 Quer entrar na **{home_guild}** no Albion Online? Envie sua candidatura "
                "aqui e a equipe ir\u00e1 analisar. **J\u00e1 est\u00e1 na guilda?** Use `/profile sync`."
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="\ud83c\uddec\ud83c\udde7 How it works",
            value=(
                "1\ufe0f\u20e3 Click **Apply** below.\n"
                "2\ufe0f\u20e3 Enter your **Albion character name** (Americas server).\n"
                "3\ufe0f\u20e3 Staff review your fame and stats from the Albion API.\n"
                "4\ufe0f\u20e3 If approved, you'll get a DM telling you to join "
                f"**{home_guild}** in-game.\n"
                "5\ufe0f\u20e3 Once the API confirms you in the guild (within ~10 min), "
                "you'll automatically be promoted to **Recruit**."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83c\uddea\ud83c\uddf8 C\u00f3mo funciona",
            value=(
                "1\ufe0f\u20e3 Haz clic en **Postular** abajo.\n"
                "2\ufe0f\u20e3 Escribe el **nombre de tu personaje** (servidor Americas).\n"
                "3\ufe0f\u20e3 El staff revisa tu fama y estad\u00edsticas desde la API de Albion.\n"
                f"4\ufe0f\u20e3 Si te aprueban, recibir\u00e1s un MD pidi\u00e9ndote unirte a **{home_guild}** en el juego.\n"
                "5\ufe0f\u20e3 Cuando la API confirme tu ingreso al gremio (en ~10 min), "
                "ser\u00e1s promovido a **Recruit** autom\u00e1ticamente."
            ),
            inline=False,
        )
        embed.add_field(
            name="\ud83c\udde7\ud83c\uddf7 Como funciona",
            value=(
                "1\ufe0f\u20e3 Clique em **Candidatar-se** abaixo.\n"
                "2\ufe0f\u20e3 Digite o **nome do seu personagem** (servidor Americas).\n"
                "3\ufe0f\u20e3 A equipe analisa sua fama e estat\u00edsticas pela API do Albion.\n"
                f"4\ufe0f\u20e3 Se aprovado, voc\u00ea receber\u00e1 uma DM pedindo para entrar na **{home_guild}** no jogo.\n"
                "5\ufe0f\u20e3 Quando a API confirmar sua entrada na guilda (em ~10 min), "
                "voc\u00ea ser\u00e1 promovido a **Recruit** automaticamente."
            ),
            inline=False,
        )
        req_lines = []
        if min_total > 0:
            req_lines.append(f"\u2022 **Total fame:** {min_total:,}+")
        if min_combat > 0:
            req_lines.append(f"\u2022 **Combat fame:** {min_combat:,}+")
        if req_lines:
            req_lines.append("\u2022 Below threshold? You can still apply \u2014 staff have final say.")
            embed.add_field(name="Suggested experience", value="\n".join(req_lines), inline=False)
        embed.set_footer(text="One application per person at a time. You'll be DM'd with the result.")

        try:
            await interaction.channel.send(embed=embed, view=ApplyView(self.bot))
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to post in this channel.", ephemeral=True
            )
            return

        self.bot.db.set_config(APPLY_CHANNEL_KEY, str(interaction.channel.id))
        await interaction.response.send_message(
            f"Apply embed posted in {interaction.channel.mention}.", ephemeral=True
        )

    @apply_group.command(name="set-review-channel", description="Set the channel where applications are reviewed.")
    @app_commands.describe(channel="Channel staff use to review applications")
    @app_commands.default_permissions(manage_guild=True)
    async def set_review_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains, Officers, and Stewards can change this."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(REVIEW_CHANNEL_KEY, str(channel.id))
        await interaction.response.send_message(
            embed=success_embed("Review channel updated", f"Applications will now be posted in {channel.mention}."),
            ephemeral=True,
        )

    @apply_group.command(
        name="set-wait",
        description="Set how many hours new members must wait before applying.",
    )
    @app_commands.describe(
        hours="Required server-membership hours before /apply works. 0 = no gate.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def set_wait(
        self,
        interaction: discord.Interaction,
        hours: app_commands.Range[int, 0, 720] | None = None,
    ):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed(
                    "Reviewer role required",
                    "Only Captains, Officers, and Stewards can change this.",
                ),
                ephemeral=True,
            )
            return
        if hours is None:
            current = _get_min_membership_hours(self.bot.db)
            await interaction.response.send_message(
                embed=info_embed(
                    "Current application wait",
                    f"New members must be in the server for **{current} hours** before "
                    f"`/apply` works. *(0 = no gate.)*",
                    hint="Pass `hours:` to change it. e.g. `hours:72`.",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(MIN_MEMBERSHIP_HOURS_KEY, str(int(hours)))
        msg = (
            f"New members must now be in the server for **{hours} hours** before applying."
            if hours > 0
            else "Wait gate disabled. Anyone can `/apply` immediately."
        )
        await interaction.response.send_message(
            embed=success_embed("Application wait updated", msg),
            ephemeral=True,
        )

    @apply_group.command(name="requirements", description="Set minimum total/combat fame for new applications.")
    @app_commands.describe(
        min_total_fame="Minimum total fame (combat + PvE + gather + craft + fish + farm). 0 = no gate.",
        min_combat_fame="Minimum combat (PvP kill) fame. 0 = no gate.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def requirements(
        self,
        interaction: discord.Interaction,
        min_total_fame: app_commands.Range[int, 0, 2_000_000_000] | None = None,
        min_combat_fame: app_commands.Range[int, 0, 2_000_000_000] | None = None,
    ):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains, Officers, and Stewards can change requirements."),
                ephemeral=True,
            )
            return

        if min_total_fame is None and min_combat_fame is None:
            cur_total, cur_combat = _get_fame_thresholds(self.bot.db)
            await interaction.response.send_message(
                embed=info_embed(
                    "Current application thresholds",
                    f"• Total fame: **{cur_total:,}**\n• Combat fame: **{cur_combat:,}**\n\n*0 = no gate.*",
                ),
                ephemeral=True,
            )
            return

        if min_total_fame is not None:
            self.bot.db.set_config(MIN_TOTAL_FAME_KEY, str(min_total_fame))
        if min_combat_fame is not None:
            self.bot.db.set_config(MIN_COMBAT_FAME_KEY, str(min_combat_fame))

        new_total, new_combat = _get_fame_thresholds(self.bot.db)
        await interaction.response.send_message(
            embed=success_embed(
                "Thresholds updated",
                f"• Total fame: **{new_total:,}**\n• Combat fame: **{new_combat:,}**",
            ),
            ephemeral=True,
        )

    @apply_group.command(name="set-home-guild", description="Set the in-game guild applicants must join.")
    @app_commands.describe(name="Exact in-game guild name (autocompletes from tracked guilds).")
    @app_commands.autocomplete(name=autocomplete_guild_name)
    @app_commands.default_permissions(manage_guild=True)
    async def set_home_guild(self, interaction: discord.Interaction, name: str):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains, Officers, and Stewards can change this."),
                ephemeral=True,
            )
            return
        clean = name.strip()
        if not clean:
            await interaction.response.send_message(
                embed=error_embed("Guild name required", "Please provide a non-empty guild name."),
                ephemeral=True,
            )
            return
        self.bot.db.set_config(HOME_GUILD_KEY, clean)
        await interaction.response.send_message(
            embed=success_embed(
                "Home guild updated",
                f"Approved applicants must join **{clean}** in-game.",
            ),
            ephemeral=True,
        )

    @apply_group.command(name="check-now", description="Force an immediate poll of approved applicants for in-game join.")
    @app_commands.default_permissions(manage_guild=True)
    async def check_now(self, interaction: discord.Interaction):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains, Officers, and Stewards can run this."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.poll_pending_joins()
        rows = self.bot.db.fetch_guild_applications_by_status(STATUS_AWAITING_JOIN)
        await interaction.followup.send(
            embed=info_embed("Poll complete", f"**{len(rows)}** application(s) still awaiting in-game join."),
            ephemeral=True,
        )

    @apply_group.command(name="pending", description="List pending guild applications.")
    @app_commands.default_permissions(manage_guild=True)
    async def pending(self, interaction: discord.Interaction):
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Reviewer role required", "Only Captains, Officers, and Stewards can view this."),
                ephemeral=True,
            )
            return

        rows = self.bot.db.fetch_pending_guild_applications()
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No pending applications", "There are no guild applications awaiting review."),
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="Pending Guild Applications", color=discord.Color.orange())
        for app in rows[:25]:
            who = f"<@{app['discord_id']}>"
            embed.add_field(
                name=f"#{app['id']} — {app['albion_name']}",
                value=f"{who}\nApplied: {app['applied_at']}",
                inline=False,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"Showing 25 of {len(rows)} — review some to see more.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Applications(bot))
