"""Squad leader / mentor assignment system.

New members can get lost when onboarding is everybody's job. This cog gives
ownership to specific squad leaders so recruits have a familiar face and
officers can see who is responsible for follow-up.
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from cogs._lfg_config import PRIME_CREATOR_ROLES
from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed, warning_embed


ACTIVE = "active"
CLOSED = "closed"
DEFAULT_MAX_MEMBERS = 12
HOME_GUILD_FALLBACK = "HomeGuild"
SQUAD_LEADER_ROLE_NAME = "Squad Leader"
SQUAD_AUTHORITY_ROLES = PRIME_CREATOR_ROLES


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _parse_dt(raw: str | None) -> datetime.datetime | None:
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _discord_ts(raw: str | None, *, style: str = "R") -> str:
    dt = _parse_dt(raw)
    if dt is None:
        return "unknown"
    return f"<t:{int(dt.timestamp())}:{style}>"


def _clip(text: str, limit: int) -> str:
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."


def _home_guild_name(db) -> str:
    return (db.get_config("home_guild_name") or HOME_GUILD_FALLBACK).strip() or HOME_GUILD_FALLBACK


def _is_home_recruit(db, profile: dict | None) -> bool:
    if not profile:
        return False
    home = _home_guild_name(db).lower()
    guild_name = str(profile.get("guild_name") or "").strip().lower()
    lifecycle = str(profile.get("lifecycle_role") or "").strip()
    return bool(guild_name == home and lifecycle == "Recruit")


def _display_profile(profile: dict | None, fallback: discord.Member | discord.User | None = None) -> str:
    if profile:
        return profile.get("albion_name") or profile.get("username") or (fallback.display_name if fallback else "Unknown")
    return fallback.display_name if fallback else "Unknown"


def _member_has_squad_authority(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(role.name in SQUAD_AUTHORITY_ROLES for role in member.roles)


def _member_has_squad_badge(member: discord.Member) -> bool:
    return any(role.name == SQUAD_LEADER_ROLE_NAME for role in member.roles)


def _member_can_lead_squad(member: discord.Member) -> bool:
    return _member_has_squad_authority(member) and _member_has_squad_badge(member)


def _leader_role_list() -> str:
    preferred = (
        "Shotcaller",
        "Senior Shotcaller",
        "Officer",
        "Captain",
        "Commander",
        "Guild Leader",
        "Alliance Leader",
    )
    names = [name for name in preferred if name in SQUAD_AUTHORITY_ROLES]
    names.extend(sorted(SQUAD_AUTHORITY_ROLES - set(names)))
    return ", ".join(names)


class Squads(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._ensure_schema()
        info_log(f"Initialized {self.__class__.__name__} cog.")

    squad_group = app_commands.Group(
        name="squad",
        description="Assign recruits to squad leaders for onboarding follow-up.",
    )

    # ── Schema / queries ──────────────────────────────────────────────────
    def _ensure_schema(self) -> None:
        db = self.bot.db
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS squad_leaders (
                leader_id   TEXT PRIMARY KEY,
                active      INTEGER NOT NULL DEFAULT 1,
                max_members INTEGER NOT NULL DEFAULT 12,
                note        TEXT,
                added_by    TEXT,
                added_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS squad_assignments (
                member_id       TEXT PRIMARY KEY,
                leader_id       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'active',
                reason          TEXT,
                assigned_by     TEXT,
                assigned_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                released_at     TEXT,
                last_nudged_at  TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS squad_assignment_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                member_id   TEXT NOT NULL,
                leader_id   TEXT,
                action      TEXT NOT NULL,
                actor_id    TEXT,
                note        TEXT,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_squad_assignments_leader ON squad_assignments(leader_id, status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_squad_assignment_log_member ON squad_assignment_log(member_id, created_at)")

    def _leader_rows(self, *, active_only: bool = False) -> list[dict]:
        db = self.bot.db
        if not db.connection:
            db.connect()
        where = "WHERE l.active = 1" if active_only else ""
        db.cursor.execute(
            f"""
            SELECT l.*,
                   COALESCE(COUNT(a.member_id), 0) AS active_count,
                   MAX(a.assigned_at) AS last_assigned_at
              FROM squad_leaders l
              LEFT JOIN squad_assignments a
                ON a.leader_id = l.leader_id
               AND a.status = ?
             {where}
             GROUP BY l.leader_id
             ORDER BY l.active DESC, active_count ASC, datetime(last_assigned_at) ASC, l.added_at ASC
            """,
            (ACTIVE,),
        )
        return [dict(r) for r in db.cursor.fetchall()]

    def _assignment_for_member(self, member_id: str) -> dict | None:
        db = self.bot.db
        if not db.connection:
            db.connect()
        db.cursor.execute("SELECT * FROM squad_assignments WHERE member_id = ?", (str(member_id),))
        row = db.cursor.fetchone()
        return dict(row) if row else None

    def _active_assignments(self, *, leader_id: str | None = None) -> list[dict]:
        db = self.bot.db
        if not db.connection:
            db.connect()
        params: list[str] = [ACTIVE]
        extra = ""
        if leader_id:
            extra = "AND a.leader_id = ?"
            params.append(str(leader_id))
        db.cursor.execute(
            f"""
            SELECT a.*,
                   p.username, p.albion_name, p.guild_name, p.lifecycle_role,
                   p.last_activity_date, p.verified_date, p.points_weekly,
                   p.points_monthly, p.points_season,
                   COALESCE(v.voice_7d_seconds, 0) AS voice_7d_seconds
              FROM squad_assignments a
              LEFT JOIN user_profiles p ON p.discord_id = a.member_id
              LEFT JOIN (
                    SELECT discord_id, SUM(seconds) AS voice_7d_seconds
                      FROM voice_activity
                     WHERE date_utc >= date('now', '-7 day')
                     GROUP BY discord_id
              ) v ON v.discord_id = a.member_id
             WHERE a.status = ?
               {extra}
             ORDER BY datetime(a.assigned_at) ASC
            """,
            tuple(params),
        )
        return [dict(r) for r in db.cursor.fetchall()]

    def _choose_leader(self, *, guild: discord.Guild | None = None, exclude_member_id: str | None = None) -> dict | None:
        leaders = []
        for row in self._leader_rows(active_only=True):
            if exclude_member_id and str(row["leader_id"]) == str(exclude_member_id):
                continue
            if guild:
                member = guild.get_member(int(row["leader_id"]))
                if not member or not _member_can_lead_squad(member):
                    continue
            if int(row.get("active_count") or 0) >= int(row.get("max_members") or DEFAULT_MAX_MEMBERS):
                continue
            leaders.append(row)
        if not leaders:
            return None
        leaders.sort(
            key=lambda r: (
                int(r.get("active_count") or 0),
                _parse_dt(r.get("last_assigned_at")) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                str(r.get("leader_id") or ""),
            )
        )
        return leaders[0]

    def _set_assignment(
        self,
        *,
        member_id: str,
        leader_id: str,
        assigned_by: str,
        reason: str,
    ) -> None:
        now = _now_iso()
        db = self.bot.db
        db.execute(
            """
            INSERT INTO squad_assignments (
                member_id, leader_id, status, reason, assigned_by, assigned_at,
                released_at, last_nudged_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(member_id) DO UPDATE SET
                leader_id = excluded.leader_id,
                status = excluded.status,
                reason = excluded.reason,
                assigned_by = excluded.assigned_by,
                assigned_at = excluded.assigned_at,
                released_at = NULL
            """,
            (str(member_id), str(leader_id), ACTIVE, reason, str(assigned_by), now),
        )
        self._log(member_id=member_id, leader_id=leader_id, action="assign", actor_id=assigned_by, note=reason)

    def activate_leader_row(
        self,
        *,
        leader_id: str,
        added_by: str,
        max_members: int = DEFAULT_MAX_MEMBERS,
        note: str = "",
    ) -> None:
        now = _now_iso()
        self.bot.db.execute(
            """
            INSERT INTO squad_leaders (leader_id, active, max_members, note, added_by, added_at, updated_at)
            VALUES (?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(leader_id) DO UPDATE SET
                active = 1,
                max_members = excluded.max_members,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (str(leader_id), int(max_members), note or "", str(added_by), now, now),
        )
        self._log(
            member_id=str(leader_id),
            leader_id=str(leader_id),
            action="leader_activate",
            actor_id=str(added_by),
            note=note,
        )

    def _close_assignment(self, *, member_id: str, actor_id: str, reason: str) -> dict | None:
        existing = self._assignment_for_member(member_id)
        if not existing or existing.get("status") != ACTIVE:
            return None
        now = _now_iso()
        self.bot.db.execute(
            """
            UPDATE squad_assignments
               SET status = ?, released_at = ?, reason = ?
             WHERE member_id = ?
            """,
            (CLOSED, now, reason, str(member_id)),
        )
        self._log(
            member_id=member_id,
            leader_id=existing.get("leader_id"),
            action="unassign",
            actor_id=actor_id,
            note=reason,
        )
        return existing

    def _log(
        self,
        *,
        member_id: str,
        leader_id: str | None,
        action: str,
        actor_id: str | None,
        note: str | None = None,
    ) -> None:
        self.bot.db.execute(
            """
            INSERT INTO squad_assignment_log (member_id, leader_id, action, actor_id, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(member_id), str(leader_id or ""), action, str(actor_id or ""), note or "", _now_iso()),
        )

    # ── Presentation / notifications ──────────────────────────────────────
    def _line_for_assignment(self, row: dict, guild: discord.Guild | None) -> str:
        member = guild.get_member(int(row["member_id"])) if guild else None
        mention = member.mention if member else f"`{row['member_id']}`"
        name = row.get("albion_name") or row.get("username") or (member.display_name if member else "Unknown")
        vc_min = int(int(row.get("voice_7d_seconds") or 0) / 60)
        activity = _discord_ts(row.get("last_activity_date"))
        assigned = _discord_ts(row.get("assigned_at"))
        return (
            f"• {mention} **{_clip(name, 28)}**"
            f" · {row.get('lifecycle_role') or 'unknown'}"
            f" · last game {activity}"
            f" · VC 7d **{vc_min}m**"
            f" · assigned {assigned}"
        )

    def _leader_status_label(self, leader_id: str, guild: discord.Guild | None, active: bool) -> str:
        if not active:
            return "inactive"
        if guild:
            member = guild.get_member(int(leader_id))
            if not member:
                return "active, missing from server"
            if not _member_has_squad_authority(member):
                return "active, needs Officer/Shotcaller"
            if not _member_has_squad_badge(member):
                return f"active, missing {SQUAD_LEADER_ROLE_NAME}"
        return "active"

    async def _send_assignment_notifications(
        self,
        *,
        guild: discord.Guild,
        member_id: str,
        leader_id: str,
        reason: str,
        automatic: bool,
    ) -> None:
        recruit = guild.get_member(int(member_id))
        leader = guild.get_member(int(leader_id))
        recruit_profile = self.bot.db.fetch_user_profile(str(member_id))
        leader_profile = self.bot.db.fetch_user_profile(str(leader_id))
        recruit_name = _display_profile(recruit_profile, recruit)
        leader_name = _display_profile(leader_profile, leader)

        if recruit and leader:
            with contextlib_suppress_discord():
                await recruit.send(
                    embed=info_embed(
                        "Your TU squad lead",
                        (
                            f"You've been assigned to **{leader.display_name}** as your squad lead.\n\n"
                            "They are your first point of contact for getting settled, finding voice, "
                            "and joining guild content. You can still ask any officer for help."
                        ),
                    )
                )

        if leader:
            with contextlib_suppress_discord():
                await leader.send(
                    embed=info_embed(
                        "New squad member assigned",
                        (
                            f"**{recruit_name}** has been assigned to you.\n"
                            f"Discord: {recruit.mention if recruit else member_id}\n"
                            f"Reason: {reason or ('auto-assignment' if automatic else 'manual assignment')}\n\n"
                            "Please welcome them, help them find the right channels, and try to get them into voice/content."
                        ),
                    )
                )

        channel_id = (
            self.bot.db.get_config("squad_assignment_channel_id")
            or self.bot.db.get_config("officer_channel_id")
            or self.bot.db.get_config("automation_officer_channel_id")
        )
        if channel_id:
            channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
            if isinstance(channel, discord.TextChannel):
                embed = discord.Embed(
                    title="🧭 Squad Assignment",
                    description=(
                        f"**Recruit:** {recruit.mention if recruit else member_id} ({recruit_name})\n"
                        f"**Squad lead:** {leader.mention if leader else leader_id} ({leader_name})\n"
                        f"**Mode:** {'Auto' if automatic else 'Manual'}\n"
                        f"**Reason:** {reason or 'New member onboarding'}"
                    ),
                    color=discord.Color.blurple(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc),
                )
                with contextlib_suppress_discord():
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))

    async def maybe_auto_assign_recruit(
        self,
        member: discord.Member,
        *,
        assigned_by: str = "system",
        reason: str = "New TU recruit",
    ) -> dict | None:
        """Assign a newly confirmed TU Recruit to the least-loaded active leader."""
        profile = self.bot.db.fetch_user_profile(str(member.id))
        if not _is_home_recruit(self.bot.db, profile):
            return None
        existing = self._assignment_for_member(str(member.id))
        if existing and existing.get("status") == ACTIVE:
            return existing
        leader = self._choose_leader(guild=member.guild, exclude_member_id=str(member.id))
        if not leader:
            info_log(f"squad auto-assign skipped for {member}: no active leader with capacity.")
            return None
        self._set_assignment(
            member_id=str(member.id),
            leader_id=str(leader["leader_id"]),
            assigned_by=assigned_by,
            reason=reason,
        )
        await self._send_assignment_notifications(
            guild=member.guild,
            member_id=str(member.id),
            leader_id=str(leader["leader_id"]),
            reason=reason,
            automatic=True,
        )
        info_log(f"Auto-assigned recruit {member} to squad leader {leader['leader_id']}.")
        return self._assignment_for_member(str(member.id))

    # ── Commands ───────────────────────────────────────────────────────────
    @squad_group.command(name="add-leader", description="Officer: add or reactivate a squad leader.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(
        leader="Member who will own recruit follow-up.",
        max_members="Capacity before auto-assignment skips this leader.",
        note="Optional note, timezone, or content focus.",
    )
    async def add_leader(
        self,
        interaction: discord.Interaction,
        leader: discord.Member,
        max_members: app_commands.Range[int, 1, 50] = DEFAULT_MAX_MEMBERS,
        note: str | None = None,
    ) -> None:
        if not _member_has_squad_authority(leader):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not a squad leader role",
                    (
                        f"{leader.mention} needs one of these roles before they can own recruits:\n"
                        f"**{_leader_role_list()}**"
                    ),
                ),
                ephemeral=True,
            )
            return
        squad_role = discord.utils.get(interaction.guild.roles, name=SQUAD_LEADER_ROLE_NAME) if interaction.guild else None
        if not squad_role:
            await interaction.response.send_message(
                embed=error_embed(
                    "Squad Leader role missing",
                    f"Create the **{SQUAD_LEADER_ROLE_NAME}** role or run `/admin setup-roles` first.",
                ),
                ephemeral=True,
            )
            return
        if squad_role not in leader.roles:
            try:
                await leader.add_roles(squad_role, reason=f"Squad leader activated by {interaction.user}")
            except discord.Forbidden:
                await interaction.response.send_message(
                    embed=error_embed("Missing permission", f"I cannot assign **{SQUAD_LEADER_ROLE_NAME}**."),
                    ephemeral=True,
                )
                return
        self.activate_leader_row(
            leader_id=str(leader.id),
            added_by=str(interaction.user.id),
            max_members=int(max_members),
            note=note or "Manual squad leader activation",
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Squad leader ready",
                f"{leader.mention} is active with capacity **{int(max_members)}**.",
            ),
            ephemeral=True,
        )

    @squad_group.command(name="remove-leader", description="Officer: deactivate a squad leader.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove_leader(self, interaction: discord.Interaction, leader: discord.Member) -> None:
        self.bot.db.execute(
            "UPDATE squad_leaders SET active = 0, updated_at = ? WHERE leader_id = ?",
            (_now_iso(), str(leader.id)),
        )
        self._log(member_id=str(leader.id), leader_id=str(leader.id), action="leader_remove", actor_id=str(interaction.user.id))
        squad_role = discord.utils.get(interaction.guild.roles, name=SQUAD_LEADER_ROLE_NAME) if interaction.guild else None
        if squad_role and squad_role in leader.roles:
            try:
                await leader.remove_roles(squad_role, reason=f"Squad leader deactivated by {interaction.user}")
            except discord.Forbidden:
                pass
        await interaction.response.send_message(
            embed=success_embed("Squad leader deactivated", f"{leader.mention} will no longer receive auto-assignments."),
            ephemeral=True,
        )

    @squad_group.command(name="leaders", description="Show current squad leaders and capacity.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def leaders(self, interaction: discord.Interaction) -> None:
        rows = self._leader_rows()
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No squad leaders", "Use `/squad add-leader` to add the first one."),
                ephemeral=True,
            )
            return
        lines = []
        for row in rows:
            member = interaction.guild.get_member(int(row["leader_id"])) if interaction.guild else None
            name = member.mention if member else f"`{row['leader_id']}`"
            state = self._leader_status_label(str(row["leader_id"]), interaction.guild, bool(int(row.get("active") or 0)))
            lines.append(
                f"• {name} · **{state}** · {int(row.get('active_count') or 0)}/{int(row.get('max_members') or DEFAULT_MAX_MEMBERS)} assigned"
                + (f" · {_clip(row.get('note') or '', 60)}" if row.get("note") else "")
            )
        await interaction.response.send_message(
            embed=info_embed("Squad leaders", "\n".join(lines)[:3900]),
            ephemeral=True,
        )

    @squad_group.command(name="assign", description="Officer: manually assign a member to a squad leader.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(member="Recruit/member to assign.", leader="Squad leader.", reason="Optional reason.")
    async def assign(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        leader: discord.Member,
        reason: str | None = None,
    ) -> None:
        if not _member_can_lead_squad(leader):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not a squad leader role",
                    (
                        f"{leader.mention} needs both **{SQUAD_LEADER_ROLE_NAME}** and Officer/Shotcaller authority before recruits can be assigned to them.\n"
                        f"Authority roles: **{_leader_role_list()}**"
                    ),
                ),
                ephemeral=True,
            )
            return
        self._set_assignment(
            member_id=str(member.id),
            leader_id=str(leader.id),
            assigned_by=str(interaction.user.id),
            reason=reason or "Manual officer assignment",
        )
        await self._send_assignment_notifications(
            guild=interaction.guild,
            member_id=str(member.id),
            leader_id=str(leader.id),
            reason=reason or "Manual officer assignment",
            automatic=False,
        )
        await interaction.response.send_message(
            embed=success_embed("Squad assigned", f"{member.mention} is now assigned to {leader.mention}."),
            ephemeral=True,
        )

    @squad_group.command(name="auto-assign", description="Officer: auto-assign one recruit to the least-loaded leader.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def auto_assign(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        assignment = await self.maybe_auto_assign_recruit(
            member,
            assigned_by=str(interaction.user.id),
            reason="Officer-triggered auto assignment",
        )
        if not assignment:
            await interaction.followup.send(
                embed=warning_embed(
                    "Not auto-assigned",
                    (
                        "This member is either not a current TU Recruit, already assigned, "
                        "or there is no active squad leader with capacity."
                    ),
                ),
                ephemeral=True,
            )
            return
        leader = interaction.guild.get_member(int(assignment["leader_id"])) if interaction.guild else None
        await interaction.followup.send(
            embed=success_embed("Auto-assigned", f"{member.mention} → {leader.mention if leader else assignment['leader_id']}"),
            ephemeral=True,
        )

    @squad_group.command(name="unassign", description="Officer: remove a member from squad follow-up.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def unassign(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        existing = self._close_assignment(
            member_id=str(member.id),
            actor_id=str(interaction.user.id),
            reason=reason or "Officer closed squad assignment",
        )
        if not existing:
            await interaction.response.send_message(
                embed=info_embed("No active assignment", f"{member.mention} is not actively assigned."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed("Assignment closed", f"{member.mention} is no longer assigned to a squad leader."),
            ephemeral=True,
        )

    @squad_group.command(name="mine", description="Squad leader: show your assigned members.")
    async def mine(self, interaction: discord.Interaction) -> None:
        rows = self._active_assignments(leader_id=str(interaction.user.id))
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("No squad members", "You do not have any active recruit assignments right now."),
                ephemeral=True,
            )
            return
        lines = [self._line_for_assignment(row, interaction.guild) for row in rows]
        await interaction.response.send_message(
            embed=info_embed("Your squad", "\n".join(lines)[:3900]),
            ephemeral=True,
        )

    @squad_group.command(name="dashboard", description="Officer: show squad assignments and follow-up state.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(post="Post visibly in this channel instead of only showing you.")
    async def dashboard(self, interaction: discord.Interaction, post: bool = False) -> None:
        leaders = self._leader_rows()
        assignments = self._active_assignments()
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in assignments:
            grouped[str(row["leader_id"])].append(row)

        embed = discord.Embed(
            title="🧭 Squad Leader Dashboard",
            description=(
                f"**{len(assignments)}** active assignment(s) across **{sum(1 for l in leaders if int(l.get('active') or 0))}** active leader(s).\n"
                "Use this to see who owns recruit follow-up."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        if not leaders:
            embed.description = "No squad leaders configured yet. Use `/squad add-leader`."
        for leader in leaders[:12]:
            leader_member = interaction.guild.get_member(int(leader["leader_id"])) if interaction.guild else None
            title = leader_member.display_name if leader_member else str(leader["leader_id"])
            status = self._leader_status_label(str(leader["leader_id"]), interaction.guild, bool(int(leader.get("active") or 0)))
            rows = grouped.get(str(leader["leader_id"]), [])
            if rows:
                value = "\n".join(self._line_for_assignment(row, interaction.guild) for row in rows[:6])
                if len(rows) > 6:
                    value += f"\n…and {len(rows) - 6} more."
            else:
                value = "No active assignments."
            embed.add_field(
                name=f"{title} · {status} · {len(rows)}/{int(leader.get('max_members') or DEFAULT_MAX_MEMBERS)}",
                value=value[:1024],
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=not post)

    @squad_group.command(name="nudge-inactive", description="Officer: DM squad leaders about assigned recruits who need follow-up.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(days="No game activity and no VC in this many days. Default 3.")
    async def nudge_inactive(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 30] = 3,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=int(days))
        rows = self._active_assignments()
        stale_by_leader: dict[str, list[dict]] = defaultdict(list)

        for row in rows:
            last_game = _parse_dt(row.get("last_activity_date"))
            voice_seconds = int(row.get("voice_7d_seconds") or 0)
            if (last_game is None or last_game < cutoff) and voice_seconds <= 0:
                stale_by_leader[str(row["leader_id"])].append(row)

        if not stale_by_leader:
            await interaction.followup.send(
                embed=success_embed("No stale squad follow-ups", f"No active assignments are stale by the **{days} day** rule."),
                ephemeral=True,
            )
            return

        sent = 0
        failed = 0
        now = _now_iso()
        for leader_id, leader_rows in stale_by_leader.items():
            leader = interaction.guild.get_member(int(leader_id)) if interaction.guild else None
            if not leader:
                failed += 1
                continue
            lines = [self._line_for_assignment(row, interaction.guild) for row in leader_rows[:12]]
            embed = warning_embed(
                "Squad follow-up needed",
                (
                    f"These assigned members have no recent game/VC signal by the **{days} day** rule.\n\n"
                    + "\n".join(lines)
                    + "\n\nPlease DM them or pull them into voice/content if they are still around."
                )[:3900],
            )
            try:
                await leader.send(embed=embed)
                sent += 1
                for row in leader_rows:
                    self.bot.db.execute(
                        "UPDATE squad_assignments SET last_nudged_at = ? WHERE member_id = ?",
                        (now, str(row["member_id"])),
                    )
                    self._log(
                        member_id=str(row["member_id"]),
                        leader_id=leader_id,
                        action="nudge",
                        actor_id=str(interaction.user.id),
                        note=f"{days}d inactive follow-up",
                    )
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        await interaction.followup.send(
            embed=info_embed(
                "Squad nudges sent",
                f"Sent follow-up DM(s) to **{sent}** leader(s). Failed: **{failed}**.",
            ),
            ephemeral=True,
        )


class contextlib_suppress_discord:
    """Tiny async-friendly suppressor without importing contextlib everywhere."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return exc_type in (discord.Forbidden, discord.HTTPException)

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return exc_type in (discord.Forbidden, discord.HTTPException)


async def setup(bot: Bot) -> None:
    await bot.add_cog(Squads(bot))
