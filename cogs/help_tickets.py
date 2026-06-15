"""Help-channel officer tickets.

When a member asks a question in the configured help channel, the bot
auto-creates a "help ticket" and posts an officer card (with buttons)
into the officer review channel. An officer hits **Take** to claim it,
then **Mark Solved** once they've helped — keeping it in line with the
existing officer task flow (bounties, regear, etc).

States:
    open      — auto-created from a help-channel message; nobody has it yet
    claimed   — an officer claimed it; only they (or another officer) can solve
    solved    — terminal; officer marked it resolved
    cancelled — terminal; officer dismissed it (false positive / off-topic)

Slash commands:
    /help-ticket list                       — open + claimed tickets
    /help-ticket mine                       — tickets you (officer) have claimed
    /help-ticket view <id>                  — full details
    /help-ticket take <id>                  — officer: claim a ticket
    /help-ticket solve <id> [note]          — officer: mark resolved
    /help-ticket cancel <id> [reason]       — officer: dismiss
    /help-ticket config set-channel         — set the help channel (auto-creates tickets)
    /help-ticket config set-review-channel  — set where officer cards post
"""
from __future__ import annotations

from cogs._typing import Bot
import datetime

import discord
from discord import app_commands
from discord.ext import commands

from debug import error_log, info_log
from utils import error_embed, info_embed, success_embed, warning_embed
from utils import is_officer as _is_officer


# ── Constants ────────────────────────────────────────────────────────────────
CFG_HELP_CHANNEL   = "help_channel_id"
CFG_REVIEW_CHANNEL = "help_review_channel_id"

STATUS_OPEN      = "open"
STATUS_CLAIMED   = "claimed"
STATUS_SOLVED    = "solved"
STATUS_CANCELLED = "cancelled"

ACTIVE_STATUSES = (STATUS_OPEN, STATUS_CLAIMED)

STATUS_COLORS = {
    STATUS_OPEN:      discord.Color.gold(),
    STATUS_CLAIMED:   discord.Color.blue(),
    STATUS_SOLVED:    discord.Color.green(),
    STATUS_CANCELLED: discord.Color.dark_grey(),
}

STATUS_EMOJI = {
    STATUS_OPEN:      "🆘",
    STATUS_CLAIMED:   "🛠️",
    STATUS_SOLVED:    "✅",
    STATUS_CANCELLED: "⚪",
}

QUESTION_PREVIEW_LEN = 1500


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()




# ── DB helpers ───────────────────────────────────────────────────────────────
def _db_create(db, *, asker_id: str, asker_name: str, question: str,
               source_channel_id: str | None, source_message_id: str | None,
               source_jump_url: str | None, guild_id: str | None = None) -> int:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        '''INSERT INTO help_tickets
           (guild_id, asker_id, asker_name, question, source_channel_id,
            source_message_id, source_jump_url, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (guild_id, asker_id, asker_name, question, source_channel_id,
         source_message_id, source_jump_url, STATUS_OPEN),
    )
    db.connection.commit()
    return int(db.cursor.lastrowid or 0)


def _db_get(db, ticket_id: int) -> dict | None:
    if not db.connection:
        db.connect()
    db.cursor.execute("SELECT * FROM help_tickets WHERE id = ?", (ticket_id,))
    row = db.cursor.fetchone()
    return dict(row) if row else None


def _db_list(db, statuses: tuple[str, ...]) -> list[dict]:
    if not db.connection:
        db.connect()
    placeholders = ",".join("?" * len(statuses))
    db.cursor.execute(
        f"SELECT * FROM help_tickets WHERE status IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 50",
        statuses,
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _db_list_for_officer(db, officer_id: str, statuses: tuple[str, ...]) -> list[dict]:
    if not db.connection:
        db.connect()
    placeholders = ",".join("?" * len(statuses))
    db.cursor.execute(
        f"SELECT * FROM help_tickets WHERE claimed_by = ? AND status IN ({placeholders}) "
        "ORDER BY id DESC LIMIT 50",
        (officer_id, *statuses),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _db_update(db, ticket_id: int, **fields) -> None:
    if not fields:
        return
    if not db.connection:
        db.connect()
    cols = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [ticket_id]
    db.cursor.execute(f"UPDATE help_tickets SET {cols} WHERE id = ?", values)
    db.connection.commit()


# ── Embeds ───────────────────────────────────────────────────────────────────
def _ticket_embed(t: dict) -> discord.Embed:
    status = t["status"]
    emoji = STATUS_EMOJI.get(status, "❔")
    color = STATUS_COLORS.get(status, discord.Color.greyple())
    question = t["question"] or "(no content)"
    if len(question) > QUESTION_PREVIEW_LEN:
        question = question[:QUESTION_PREVIEW_LEN] + "…"

    embed = discord.Embed(
        title=f"{emoji} Help ticket #{t['id']} — {status.title()}",
        description=question,
        color=color,
    )
    embed.add_field(
        name="Asked by",
        value=f"<@{t['asker_id']}> ({t['asker_name']})",
        inline=True,
    )
    if t.get("source_jump_url"):
        embed.add_field(
            name="Original message",
            value=f"[Jump]({t['source_jump_url']})",
            inline=True,
        )
    if t.get("claimed_by"):
        embed.add_field(
            name="Claimed by",
            value=f"<@{t['claimed_by']}>",
            inline=True,
        )
    if status == STATUS_SOLVED and t.get("resolved_by"):
        embed.add_field(
            name="Resolved by",
            value=f"<@{t['resolved_by']}>",
            inline=True,
        )
        if t.get("resolution_note"):
            embed.add_field(
                name="Note",
                value=t["resolution_note"],
                inline=False,
            )
    if status == STATUS_CANCELLED and t.get("resolution_note"):
        embed.add_field(name="Reason", value=t["resolution_note"], inline=False)

    embed.set_footer(text=f"Created {t['created_at']}")
    return embed


# ── Persistent buttons (DynamicItem) ─────────────────────────────────────────
TAKE_TEMPLATE   = r"helpticket:take:(?P<tid>[0-9]+)"
UNCLAIM_TEMPLATE= r"helpticket:unclaim:(?P<tid>[0-9]+)"
SOLVE_TEMPLATE  = r"helpticket:solve:(?P<tid>[0-9]+)"
CANCEL_TEMPLATE = r"helpticket:cancel:(?P<tid>[0-9]+)"


def _get_cog(interaction: discord.Interaction) -> "HelpTickets | None":
    bot = interaction.client
    cog = bot.get_cog("HelpTickets") if isinstance(bot, commands.Bot) else None
    return cog if isinstance(cog, HelpTickets) else None


class _SolveModal(discord.ui.Modal, title="Mark help ticket solved"):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.note = discord.ui.TextInput(
            label="Resolution note (optional)",
            placeholder="Brief summary of how it was resolved.",
            style=discord.TextStyle.paragraph,
            required=False, max_length=400,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._do_solve(interaction, self.ticket_id, str(self.note.value or ""))


class _CancelModal(discord.ui.Modal, title="Dismiss help ticket"):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(timeout=None)
        self.ticket_id = ticket_id
        self.reason = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="e.g. off-topic, duplicate, not a question.",
            style=discord.TextStyle.paragraph,
            required=False, max_length=300,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._do_cancel(interaction, self.ticket_id, str(self.reason.value or ""))


class HelpTakeButton(
    discord.ui.DynamicItem[discord.ui.Button], template=TAKE_TEMPLATE,
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(
            label="Take", style=discord.ButtonStyle.success, emoji="🛠️",
            custom_id=f"helpticket:take:{ticket_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        cog = _get_cog(interaction)
        if cog:
            await cog._do_take(interaction, self.ticket_id)


class HelpUnclaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=UNCLAIM_TEMPLATE,
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(
            label="Unclaim", style=discord.ButtonStyle.secondary, emoji="🔓",
            custom_id=f"helpticket:unclaim:{ticket_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        cog = _get_cog(interaction)
        if cog:
            await cog._do_unclaim(interaction, self.ticket_id)


class HelpSolveButton(
    discord.ui.DynamicItem[discord.ui.Button], template=SOLVE_TEMPLATE,
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(
            label="Mark Solved", style=discord.ButtonStyle.primary, emoji="✅",
            custom_id=f"helpticket:solve:{ticket_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_SolveModal(self.ticket_id))


class HelpCancelButton(
    discord.ui.DynamicItem[discord.ui.Button], template=CANCEL_TEMPLATE,
):
    def __init__(self, ticket_id: int) -> None:
        self.ticket_id = ticket_id
        super().__init__(discord.ui.Button(
            label="Dismiss", style=discord.ButtonStyle.danger, emoji="🗑️",
            custom_id=f"helpticket:cancel:{ticket_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_CancelModal(self.ticket_id))


def _view_for_ticket(t: dict) -> discord.ui.View | None:
    status = t["status"]
    tid = int(t["id"])
    view = discord.ui.View(timeout=None)
    if status == STATUS_OPEN:
        view.add_item(HelpTakeButton(tid))
        view.add_item(HelpCancelButton(tid))
    elif status == STATUS_CLAIMED:
        view.add_item(HelpSolveButton(tid))
        view.add_item(HelpUnclaimButton(tid))
        view.add_item(HelpCancelButton(tid))
    else:
        return None
    return view


def register_persistent_help_views(bot: Bot) -> None:
    bot.add_dynamic_items(
        HelpTakeButton, HelpUnclaimButton, HelpSolveButton, HelpCancelButton,
    )


# ── Cog ──────────────────────────────────────────────────────────────────────
class HelpTickets(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        register_persistent_help_views(self.bot)

    help_group = app_commands.Group(
        name="help-ticket",
        description="Officer help-ticket queue (auto-created from the help channel).",
    )
    config_group = app_commands.Group(
        name="config",
        description="Officer-only help-ticket configuration.",
        parent=help_group,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ── Listener: auto-create tickets from the help channel ─────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.guild is None:
            return
        db = self.bot.db  # type: ignore[attr-defined]
        help_chan_id = db.get_config(CFG_HELP_CHANNEL)
        if not help_chan_id:
            return
        try:
            if int(help_chan_id) != message.channel.id:
                return
        except (TypeError, ValueError):
            error_log(f"help_tickets: bad {CFG_HELP_CHANNEL}={help_chan_id!r}")
            return

        info_log(
            f"help_tickets: message in help channel from {message.author} "
            f"(officer={_is_officer(message.author)}, len={len(message.content or '')}, "
            f"attachments={len(message.attachments)})"
        )

        # Skip very short noise (e.g. "ok", "ty", "lol")
        content = (message.content or "").strip()
        if len(content) < 4 and not message.attachments:
            info_log("help_tickets: skipped — message too short / no attachments.")
            return

        # Build question text including any attachment hints.
        question = content or "(see attachments)"
        if message.attachments:
            urls = "\n".join(f"📎 {a.url}" for a in message.attachments[:3])
            question = f"{question}\n\n{urls}"

        try:
            ticket_id = _db_create(
                db,
                asker_id=str(message.author.id),
                asker_name=str(message.author),
                question=question[:4000],
                source_channel_id=str(message.channel.id),
                source_message_id=str(message.id),
                source_jump_url=message.jump_url,
                guild_id=str(message.guild.id),
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"help_tickets: DB create failed: {exc!r}")
            return

        info_log(f"help_tickets: created ticket #{ticket_id} for {message.author}.")
        ticket = _db_get(db, ticket_id)
        if not ticket:
            error_log(f"help_tickets: ticket #{ticket_id} vanished after insert?")
            return
        try:
            await self._post_or_update_ticket_message(ticket)
        except Exception as exc:  # noqa: BLE001
            error_log(f"help_tickets: post officer card failed for #{ticket_id}: {exc!r}")
        try:
            await message.add_reaction("🆘")
        except discord.HTTPException as exc:
            error_log(f"help_tickets: reaction add failed: {exc!r}")

    # ── Officer card posting ────────────────────────────────────────────────
    def _review_channel_id(self) -> str | None:
        db = self.bot.db  # type: ignore[attr-defined]
        return db.get_config(CFG_REVIEW_CHANNEL) or db.get_config("officer_channel_id")

    def _guild_for_ticket(self, ticket: dict) -> discord.Guild | None:
        """Resolve the guild a ticket came from. Falls back to guilds[0] for
        legacy tickets created before guild_id was tracked."""
        gid = ticket.get("guild_id")
        if gid:
            try:
                guild = self.bot.get_guild(int(gid))
            except (TypeError, ValueError):
                guild = None
            if guild is not None:
                return guild
        return self.bot.guilds[0] if self.bot.guilds else None

    async def _delete_source_message(self, ticket: dict) -> None:
        """Delete the asker's original message from the help channel (best-effort)."""
        chan_id = ticket.get("source_channel_id")
        msg_id  = ticket.get("source_message_id")
        if not chan_id or not msg_id:
            return
        guild = self._guild_for_ticket(ticket)
        if guild is None:
            return
        try:
            chan = guild.get_channel(int(chan_id))
        except (TypeError, ValueError):
            return
        if not isinstance(chan, discord.TextChannel):
            return
        try:
            msg = await chan.fetch_message(int(msg_id))
            await msg.delete()
            info_log(f"help_tickets: deleted source message for ticket #{ticket['id']}.")
        except discord.NotFound:
            pass  # already gone
        except discord.Forbidden as exc:
            error_log(
                f"help_tickets: cannot delete source message (need Manage Messages "
                f"in #{chan.name}): {exc!r}"
            )
        except discord.HTTPException as exc:
            error_log(f"help_tickets: source message delete failed: {exc!r}")

    async def _post_or_update_ticket_message(self, ticket: dict) -> None:
        guild = self._guild_for_ticket(ticket)
        if guild is None:
            error_log("help_tickets: no guilds on bot, cannot post card.")
            return
        embed = _ticket_embed(ticket)
        view = _view_for_ticket(ticket)

        existing_chan_id = ticket.get("ticket_channel_id")
        existing_msg_id  = ticket.get("ticket_message_id")
        if existing_chan_id and existing_msg_id:
            chan = guild.get_channel(int(existing_chan_id))
            if isinstance(chan, discord.TextChannel):
                try:
                    msg = await chan.fetch_message(int(existing_msg_id))
                    await msg.edit(embed=embed, view=view)
                    info_log(f"help_tickets: updated card for #{ticket['id']} in #{chan.name}.")
                    return
                except (discord.NotFound, discord.HTTPException) as exc:
                    info_log(f"help_tickets: existing card missing ({exc!r}); re-posting.")

        # Need to post fresh.
        review_chan_id = self._review_channel_id()
        if not review_chan_id:
            error_log(
                "help_tickets: no review channel configured. Run "
                "`/help-ticket config set-review-channel` or `/admin set-officer-channel`."
            )
            return
        chan = guild.get_channel(int(review_chan_id))
        if not isinstance(chan, discord.TextChannel):
            error_log(
                f"help_tickets: review channel {review_chan_id} not found / not a text channel "
                f"in guild {guild.name}."
            )
            return
        # Sanity check: can the bot actually send there?
        me = guild.me
        if me is not None:
            perms = chan.permissions_for(me)
            if not (perms.send_messages and perms.embed_links):
                error_log(
                    f"help_tickets: bot missing send_messages/embed_links in #{chan.name} "
                    f"(send={perms.send_messages}, embed={perms.embed_links})."
                )
                return
        try:
            msg = await chan.send(embed=embed, view=view)
        except discord.Forbidden as exc:
            error_log(f"help_tickets: forbidden sending to #{chan.name}: {exc!r}")
            return
        except discord.HTTPException as exc:
            error_log(f"help_tickets: HTTP error sending to #{chan.name}: {exc!r}")
            return
        info_log(f"help_tickets: posted card for #{ticket['id']} in #{chan.name}.")
        _db_update(
            self.bot.db,  # type: ignore[attr-defined]
            int(ticket["id"]),
            ticket_channel_id=str(chan.id),
            ticket_message_id=str(msg.id),
        )

    # ── Action handlers ─────────────────────────────────────────────────────
    async def _do_take(self, interaction: discord.Interaction, ticket_id: int) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        ticket = _db_get(db, ticket_id)
        if not ticket:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Ticket #{ticket_id} doesn't exist."),
                ephemeral=True,
            )
            return
        if ticket["status"] != STATUS_OPEN:
            await interaction.response.send_message(
                embed=warning_embed(
                    "Already taken",
                    f"Ticket is **{ticket['status']}**" + (
                        f" by <@{ticket['claimed_by']}>." if ticket.get("claimed_by") else "."
                    ),
                ),
                ephemeral=True,
            )
            return
        _db_update(
            db, ticket_id,
            status=STATUS_CLAIMED,
            claimed_by=str(interaction.user.id),
            claimed_by_name=str(interaction.user),
            claimed_at=_now_iso(),
        )
        updated = _db_get(db, ticket_id)
        if updated:
            await self._post_or_update_ticket_message(updated)
        await interaction.response.send_message(
            embed=success_embed(
                "Claimed",
                f"You've taken ticket **#{ticket_id}**. Reach out to <@{ticket['asker_id']}> "
                f"to help, then hit **Mark Solved** when done.",
            ),
            ephemeral=True,
        )

    async def _do_unclaim(self, interaction: discord.Interaction, ticket_id: int) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        ticket = _db_get(db, ticket_id)
        if not ticket:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Ticket #{ticket_id} doesn't exist."),
                ephemeral=True,
            )
            return
        if ticket["status"] != STATUS_CLAIMED:
            await interaction.response.send_message(
                embed=warning_embed("Nothing to unclaim",
                                    f"Ticket is **{ticket['status']}**."),
                ephemeral=True,
            )
            return
        _db_update(
            db, ticket_id,
            status=STATUS_OPEN,
            claimed_by=None, claimed_by_name=None, claimed_at=None,
        )
        updated = _db_get(db, ticket_id)
        if updated:
            await self._post_or_update_ticket_message(updated)
        await interaction.response.send_message(
            embed=info_embed("Released", f"Ticket **#{ticket_id}** is back in the queue."),
            ephemeral=True,
        )

    async def _do_solve(
        self, interaction: discord.Interaction, ticket_id: int, note: str
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        ticket = _db_get(db, ticket_id)
        if not ticket:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Ticket #{ticket_id} doesn't exist."),
                ephemeral=True,
            )
            return
        if ticket["status"] not in (STATUS_OPEN, STATUS_CLAIMED):
            await interaction.response.send_message(
                embed=warning_embed("Already closed",
                                    f"Ticket is **{ticket['status']}**."),
                ephemeral=True,
            )
            return
        _db_update(
            db, ticket_id,
            status=STATUS_SOLVED,
            resolved_by=str(interaction.user.id),
            resolved_by_name=str(interaction.user),
            resolved_at=_now_iso(),
            resolution_note=note.strip() or None,
            # If they solved without claiming, record claim too.
            claimed_by=ticket.get("claimed_by") or str(interaction.user.id),
            claimed_by_name=ticket.get("claimed_by_name") or str(interaction.user),
            claimed_at=ticket.get("claimed_at") or _now_iso(),
        )
        updated = _db_get(db, ticket_id)
        if updated:
            await self._post_or_update_ticket_message(updated)
        # Clean up the asker's original message in the help channel.
        await self._delete_source_message(ticket)
        # DM the asker that someone helped.
        try:
            asker = await self.bot.fetch_user(int(ticket["asker_id"]))
            dm_embed = info_embed(
                "Your question was answered ✅",
                f"An officer marked your help-channel question (ticket #{ticket_id}) as resolved."
                + (f"\n\n**Note:** {note.strip()}" if note.strip() else ""),
            )
            await asker.send(embed=dm_embed)
        except (discord.HTTPException, discord.Forbidden):
            pass

        await interaction.response.send_message(
            embed=success_embed(
                "Marked solved",
                f"Ticket **#{ticket_id}** closed. Thanks for helping!",
            ),
            ephemeral=True,
        )

    async def _do_cancel(
        self, interaction: discord.Interaction, ticket_id: int, reason: str
    ) -> None:
        db = self.bot.db  # type: ignore[attr-defined]
        ticket = _db_get(db, ticket_id)
        if not ticket:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Ticket #{ticket_id} doesn't exist."),
                ephemeral=True,
            )
            return
        if ticket["status"] not in (STATUS_OPEN, STATUS_CLAIMED):
            await interaction.response.send_message(
                embed=warning_embed("Already closed",
                                    f"Ticket is **{ticket['status']}**."),
                ephemeral=True,
            )
            return
        _db_update(
            db, ticket_id,
            status=STATUS_CANCELLED,
            resolved_by=str(interaction.user.id),
            resolved_by_name=str(interaction.user),
            resolved_at=_now_iso(),
            resolution_note=reason.strip() or None,
        )
        updated = _db_get(db, ticket_id)
        if updated:
            await self._post_or_update_ticket_message(updated)
        await interaction.response.send_message(
            embed=info_embed("Dismissed", f"Ticket **#{ticket_id}** dismissed."),
            ephemeral=True,
        )

    # ── Slash commands ──────────────────────────────────────────────────────
    @help_group.command(name="list", description="Show open / claimed help tickets.")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        rows = _db_list(self.bot.db, ACTIVE_STATUSES)  # type: ignore[attr-defined]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Help queue", "No open or claimed tickets. 🎉"),
                ephemeral=True,
            )
            return
        lines = []
        for t in rows:
            emoji = STATUS_EMOJI.get(t["status"], "❔")
            preview = (t["question"] or "").splitlines()[0][:80]
            who = f"<@{t['claimed_by']}>" if t.get("claimed_by") else "—"
            lines.append(
                f"{emoji} **#{t['id']}** by <@{t['asker_id']}> · claimed: {who}\n  › {preview}"
            )
        await interaction.response.send_message(
            embed=info_embed("Help queue", "\n".join(lines)),
            ephemeral=True,
        )

    @help_group.command(name="mine", description="Help tickets you've claimed.")
    async def mine_cmd(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        rows = _db_list_for_officer(
            self.bot.db, str(interaction.user.id), (STATUS_CLAIMED,)  # type: ignore[attr-defined]
        )
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Your help tickets", "Nothing claimed."),
                ephemeral=True,
            )
            return
        lines = [
            f"{STATUS_EMOJI.get(t['status'],'❔')} **#{t['id']}** "
            f"<@{t['asker_id']}> — {(t['question'] or '').splitlines()[0][:80]}"
            for t in rows
        ]
        await interaction.response.send_message(
            embed=info_embed("Your help tickets", "\n".join(lines)),
            ephemeral=True,
        )

    @help_group.command(name="view", description="Show full details of a help ticket.")
    @app_commands.describe(ticket_id="Ticket ID")
    async def view_cmd(self, interaction: discord.Interaction, ticket_id: int) -> None:
        ticket = _db_get(self.bot.db, ticket_id)  # type: ignore[attr-defined]
        if not ticket:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"Ticket #{ticket_id} doesn't exist."),
                ephemeral=True,
            )
            return
        view = _view_for_ticket(ticket) if _is_officer(interaction.user) else None
        await interaction.response.send_message(
            embed=_ticket_embed(ticket), view=view, ephemeral=True,
        )

    @help_group.command(name="take", description="Officer: claim a help ticket.")
    @app_commands.describe(ticket_id="Ticket ID")
    async def take_cmd(self, interaction: discord.Interaction, ticket_id: int) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await self._do_take(interaction, ticket_id)

    @help_group.command(name="solve", description="Officer: mark a help ticket solved.")
    @app_commands.describe(ticket_id="Ticket ID", note="Optional resolution note")
    async def solve_cmd(
        self, interaction: discord.Interaction, ticket_id: int, note: str = "",
    ) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await self._do_solve(interaction, ticket_id, note)

    @help_group.command(name="cancel", description="Officer: dismiss a help ticket.")
    @app_commands.describe(ticket_id="Ticket ID", reason="Optional reason")
    async def cancel_cmd(
        self, interaction: discord.Interaction, ticket_id: int, reason: str = "",
    ) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await self._do_cancel(interaction, ticket_id, reason)

    # ── Config commands ─────────────────────────────────────────────────────
    @config_group.command(
        name="set-channel",
        description="Set the help channel — messages there auto-create tickets.",
    )
    @app_commands.describe(channel="The text channel members ask questions in")
    async def set_channel_cmd(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config(CFG_HELP_CHANNEL, str(channel.id))  # type: ignore[attr-defined]
        await interaction.response.send_message(
            embed=success_embed(
                "Help channel set",
                f"Tickets will auto-create from messages in {channel.mention}.",
            ),
            ephemeral=True,
        )

    @config_group.command(
        name="set-review-channel",
        description="Set where officer help-ticket cards get posted.",
    )
    @app_commands.describe(channel="Officer review channel")
    async def set_review_channel_cmd(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config(CFG_REVIEW_CHANNEL, str(channel.id))  # type: ignore[attr-defined]
        await interaction.response.send_message(
            embed=success_embed(
                "Review channel set",
                f"Officer help-ticket cards will post in {channel.mention}.",
            ),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(HelpTickets(bot))