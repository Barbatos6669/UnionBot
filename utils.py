"""Shared UI/UX helpers used across cogs.

Centralizes:
  * Embed colors and factories (success / error / warning / info / pending)
  * ConfirmView for destructive-action confirmations
  * App-command checks for role-gated commands
  * Common Choice enums (server)
  * A shared "guild name" autocomplete that pulls from the bot's database
"""
from __future__ import annotations

import discord


def _handled_message_store(bot) -> tuple[set[int], list[int]]:
    ids = getattr(bot, "_unionbot_handled_message_ids", None)
    order = getattr(bot, "_unionbot_handled_message_order", None)
    if ids is None or order is None:
        ids = set()
        order = []
        setattr(bot, "_unionbot_handled_message_ids", ids)
        setattr(bot, "_unionbot_handled_message_order", order)
    return ids, order


def mark_unionbot_handled(bot, message: discord.Message, *, max_size: int = 1000) -> None:
    """Remember that a deterministic helper already answered this message."""
    ids, order = _handled_message_store(bot)
    mid = int(message.id)
    if mid in ids:
        return
    ids.add(mid)
    order.append(mid)
    while len(order) > max_size:
        ids.discard(order.pop(0))


def is_unionbot_handled(bot, message: discord.Message) -> bool:
    ids, _ = _handled_message_store(bot)
    return int(message.id) in ids
from discord import app_commands

# ── Colors ────────────────────────────────────────────────────────────────────
EMBED_COLORS = {
    "success": discord.Color.from_str("#43b581"),
    "error":   discord.Color.from_str("#e74c3c"),
    "warning": discord.Color.from_str("#f0a500"),
    "info":    discord.Color.from_str("#3498db"),
    "pending": discord.Color.from_str("#e67e22"),
}

# ── Embed factories ───────────────────────────────────────────────────────────
def success_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"✅ {title}", description=description, color=EMBED_COLORS["success"])


def error_embed(title: str, description: str = "", *, hint: str = "") -> discord.Embed:
    e = discord.Embed(title=f"❌ {title}", description=description, color=EMBED_COLORS["error"])
    if hint:
        e.add_field(name="Next step", value=hint, inline=False)
    return e


def warning_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"⚠️ {title}", description=description, color=EMBED_COLORS["warning"])


def info_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"ℹ️ {title}", description=description, color=EMBED_COLORS["info"])


def pending_embed(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=f"⏳ {title}", description=description, color=EMBED_COLORS["pending"])


# ── Server enum ───────────────────────────────────────────────────────────────
SERVER_CHOICES = [
    app_commands.Choice(name="Americas", value="americas"),
    app_commands.Choice(name="Europe",   value="europe"),
    app_commands.Choice(name="Asia",     value="asia"),
]
VALID_SERVERS = {c.value for c in SERVER_CHOICES}


# ── Confirmation view ─────────────────────────────────────────────────────────
class ConfirmView(discord.ui.View):
    """Two-button confirm/cancel view. After awaiting `wait()`, check `.confirmed`."""

    def __init__(self, *, author_id: int, timeout: float = 60.0, confirm_label: str = "Confirm",
                 cancel_label: str = "Cancel", danger: bool = True):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: bool | None = None
        self._set_button_styles(confirm_label, cancel_label, danger)

    def _set_button_styles(self, confirm_label: str, cancel_label: str, danger: bool) -> None:
        self.confirm.label = confirm_label
        self.cancel.label = cancel_label
        self.confirm.style = discord.ButtonStyle.danger if danger else discord.ButtonStyle.success

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the user who ran the command can confirm this action.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


async def confirm_action(
    interaction: discord.Interaction,
    *,
    title: str,
    description: str,
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    danger: bool = True,
    timeout: float = 60.0,
) -> bool:
    """Send a confirmation prompt as the initial response. Returns True if confirmed.

    Caller MUST not have responded to the interaction yet. After this returns True,
    use `interaction.followup.send(...)` for further messages (the original was edited).
    """
    view = ConfirmView(
        author_id=interaction.user.id,
        timeout=timeout,
        confirm_label=confirm_label,
        cancel_label=cancel_label,
        danger=danger,
    )
    embed = warning_embed(title, description)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await view.wait()
    if view.confirmed is None:
        # Timed out — disable buttons in-place
        for child in view.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(
                embed=warning_embed(title, f"{description}\n\n*Timed out — no action taken.*"),
                view=view,
            )
        except discord.HTTPException:
            pass
        return False
    return bool(view.confirmed)


# ── Role-gated command checks ─────────────────────────────────────────────────
def is_officer(member: discord.abc.User) -> bool:
    """True if ``member`` has ``manage_guild`` or any role in ``STAFF_ROLES``.

    Centralized replacement for the per-cog ``_is_officer`` helpers. Returns
    False for ``User`` objects (DMs / partial members) since role checks need
    a real ``Member`` from a guild.
    """
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    # Lazy import so a missing/circular config doesn't break utils.
    try:
        from config import STAFF_ROLES
    except ImportError:
        return False
    role_names = {r.name for r in member.roles}
    return any(r in role_names for r in STAFF_ROLES)


def has_any_role(*role_names: str):
    """app_commands.check that requires the invoking member to hold any of `role_names`."""
    allowed = set(role_names)

    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            raise app_commands.CheckFailure("This command can only be used in a server.")
        if any(r.name in allowed for r in member.roles):
            return True
        nice = ", ".join(role_names)
        raise app_commands.CheckFailure(f"You need one of these roles: {nice}.")

    return app_commands.check(predicate)


# ── Guild-name autocomplete ───────────────────────────────────────────────────
async def autocomplete_guild_name(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Autocomplete from the bot's tracked guilds. Case-insensitive substring match."""
    db = getattr(interaction.client, "db", None)
    if db is None:
        return []
    try:
        guilds = db.fetch_all_guilds() or []
    except Exception:
        return []
    needle = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []
    for g in guilds:
        name = g["guild_name"] if isinstance(g, dict) or hasattr(g, "keys") else str(g)
        if not needle or needle in name.lower():
            out.append(app_commands.Choice(name=name, value=name))
        if len(out) >= 25:
            break
    return out
