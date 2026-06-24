"""SSO route modal and board controls for bounty routes."""
from __future__ import annotations

import re

import discord
from discord.ext import commands

from utils import error_embed


def _get_bounty_cog(interaction: discord.Interaction):
    bot = interaction.client
    return bot.get_cog("Bounties") if isinstance(bot, commands.Bot) else None


def normalize_sso_ttl(raw: str) -> tuple[str | None, str | None]:
    """Return (normalized_ttl, error)."""
    ttl_raw = (raw or "").strip().lower()
    if not ttl_raw:
        return None, None
    for old, new in (
        ("hours", "h"),
        ("hour", "h"),
        ("hrs", "h"),
        ("hr", "h"),
        ("minutes", "m"),
        ("minute", "m"),
        ("mins", "m"),
        ("min", "m"),
    ):
        ttl_raw = ttl_raw.replace(old, new)
    ttl_raw = ttl_raw.replace(" ", "")
    if not ttl_raw:
        return None, None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", ttl_raw)
    if m and (m.group(1) or m.group(2)):
        hours = int(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
        total = hours * 60 + minutes
    elif ttl_raw.isdigit():
        total = int(ttl_raw)
    else:
        return None, "Use a TTL like `2h`, `90m`, or `1h30m`."
    if total <= 0:
        return None, "TTL must be longer than 0 minutes."
    if total > 24 * 60:
        return None, "TTL looks too long. Use the time left on the current connection, max 24h."
    hours, minutes = divmod(total, 60)
    if hours and minutes:
        return f"{hours}h{minutes}m", None
    if hours:
        return f"{hours}h", None
    return f"{minutes}m", None


class SubmitSSORouteModal(discord.ui.Modal, title="Submit SSO Route"):
    """Structured prompt for the daily Sentinel-SA portal-route bounty."""

    def __init__(self, bounty_id: int) -> None:
        super().__init__(timeout=None)
        self.bounty_id = bounty_id

        self.portal1 = discord.ui.TextInput(
            label="Portal 1 (required, scouted from HO)",
            placeholder="e.g. Sentinel-SA-Odesos",
            style=discord.TextStyle.short,
            min_length=2,
            max_length=80,
            required=True,
        )
        self.portal2 = discord.ui.TextInput(
            label="Portal 2 (optional)",
            placeholder="e.g. Birchcops",
            style=discord.TextStyle.short,
            max_length=80,
            required=False,
        )
        self.portal3 = discord.ui.TextInput(
            label="Portal 3 (optional)",
            placeholder="e.g. Caerleon",
            style=discord.TextStyle.short,
            max_length=80,
            required=False,
        )
        self.note = discord.ui.TextInput(
            label="Note (optional)",
            placeholder="e.g. 30s walk from CL portal, watch for gankers",
            style=discord.TextStyle.short,
            max_length=200,
            required=False,
        )
        self.ttl = discord.ui.TextInput(
            label="Time left on connection (optional)",
            placeholder="e.g. 1h45m  or  90m  or  2h",
            style=discord.TextStyle.short,
            max_length=20,
            required=False,
        )
        for item in (self.portal1, self.portal2, self.portal3, self.note, self.ttl):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        portals = [
            str(self.portal1.value).strip(),
            str(self.portal2.value).strip(),
            str(self.portal3.value).strip(),
        ]
        parts: list[str] = [portal for portal in portals if portal]
        note = str(self.note.value).strip()
        if note:
            parts.append(f"note: {note}")
        ttl = str(self.ttl.value).strip()
        normalized_ttl, ttl_error = normalize_sso_ttl(ttl)
        if ttl_error:
            await interaction.response.send_message(
                embed=error_embed("Check the route timer", ttl_error),
                ephemeral=True,
            )
            return
        if normalized_ttl:
            parts.append(f"ttl: {normalized_ttl}")
        proof = " > ".join(parts)
        await cog._do_submit(interaction, self.bounty_id, proof)


class SSORouteBoardView(discord.ui.View):
    """Persistent action row for the single SSO route board message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Add / Update Route",
        style=discord.ButtonStyle.primary,
        emoji="🐎",
        custom_id="sso_routes:add",
    )
    async def add_route(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._open_sso_route_modal(interaction)

    @discord.ui.button(
        label="Format",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        custom_id="sso_routes:format",
    )
    async def show_format(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._show_sso_route_format(interaction)

    @discord.ui.button(
        label="Mark Closed",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="sso_routes:close",
    )
    async def mark_closed(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._close_current_sso_route(interaction)
