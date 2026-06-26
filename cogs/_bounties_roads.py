"""Roads power-core bounty board controls."""
from __future__ import annotations

import re

import discord
from discord.ext import commands

from cogs._bounties_config import fmt_silver
from utils import error_embed, info_embed

ROAD_CORE_TITLE_PREFIX = "[Roads Core]"
ROAD_CORE_REWARDS: dict[str, int] = {
    "green": 1_000_000,
    "blue": 3_000_000,
    "purple": 5_000_000,
}
ROAD_CORE_EMOJIS: dict[str, str] = {
    "green": "🟢",
    "blue": "🔵",
    "purple": "🟣",
}


def _get_bounty_cog(interaction: discord.Interaction):
    bot = interaction.client
    return bot.get_cog("Bounties") if isinstance(bot, commands.Bot) else None


def normalize_road_core_color(raw: str) -> tuple[str | None, str | None]:
    text = str(raw or "").strip().lower()
    aliases = {
        "g": "green",
        "green": "green",
        "t4": "green",
        "blue": "blue",
        "b": "blue",
        "t6": "blue",
        "purple": "purple",
        "purp": "purple",
        "p": "purple",
        "t8": "purple",
    }
    color = aliases.get(text)
    if color:
        return color, None
    return None, "Use `green`, `blue`, or `purple`."


def road_core_title(color: str) -> str:
    emoji = ROAD_CORE_EMOJIS.get(color, "⚡")
    return f"{ROAD_CORE_TITLE_PREFIX} {emoji} {color.title()} Power Core"


def road_core_proof_text(
    *,
    color: str,
    screenshot: str,
    party: str,
    note: str = "",
) -> str:
    lines = [
        f"color: {color}",
        f"screenshot: {screenshot.strip()}",
        f"party: {party.strip()}",
    ]
    if note.strip():
        lines.append(f"note: {note.strip()}")
    return "\n".join(lines)


def parse_road_core_proof(proof: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in str(proof or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            out[key] = value
    return out


def proof_has_url(raw: str) -> bool:
    return bool(re.search(r"https?://\S+", str(raw or ""), re.IGNORECASE))


class SubmitRoadsCoreModal(discord.ui.Modal, title="Submit Roads Core"):
    """Structured prompt for Roads hideout power-core payouts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.color = discord.ui.TextInput(
            label="Core color",
            placeholder="green, blue, or purple",
            style=discord.TextStyle.short,
            min_length=1,
            max_length=20,
            required=True,
        )
        self.screenshot = discord.ui.TextInput(
            label="Screenshot / proof link",
            placeholder="Paste a Discord attachment link or screenshot URL",
            style=discord.TextStyle.short,
            min_length=8,
            max_length=300,
            required=True,
        )
        self.party = discord.ui.TextInput(
            label="Party members",
            placeholder="Names or @mentions of who helped deliver the core",
            style=discord.TextStyle.paragraph,
            min_length=2,
            max_length=500,
            required=True,
        )
        self.note = discord.ui.TextInput(
            label="Notes (optional)",
            placeholder="Road, fight details, chest/core context, etc.",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        for item in (self.color, self.screenshot, self.party, self.note):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        color, error = normalize_road_core_color(str(self.color.value))
        if error or not color:
            await interaction.response.send_message(
                embed=error_embed("Check the core color", error or "Invalid color."),
                ephemeral=True,
            )
            return
        screenshot = str(self.screenshot.value).strip()
        if not proof_has_url(screenshot):
            await interaction.response.send_message(
                embed=error_embed(
                    "Screenshot link needed",
                    "Upload the screenshot in Discord first, copy the attachment link, then paste it here.",
                ),
                ephemeral=True,
            )
            return
        await cog._submit_roads_core_bounty(
            interaction,
            color=color,
            screenshot=screenshot,
            party=str(self.party.value).strip(),
            note=str(self.note.value or "").strip(),
        )


class RoadsCoreBoardView(discord.ui.View):
    """Persistent action row for the single Roads core bounty board."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit Core",
        style=discord.ButtonStyle.primary,
        emoji="⚡",
        custom_id="roads_cores:submit",
    )
    async def submit_core(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._open_roads_core_modal(interaction)

    @discord.ui.button(
        label="Payouts",
        style=discord.ButtonStyle.secondary,
        emoji="🧾",
        custom_id="roads_cores:payouts",
    )
    async def show_payouts(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        lines = [
            f"{ROAD_CORE_EMOJIS[color]} **{color.title()}** — 🪙 **{fmt_silver(amount)}**"
            for color, amount in ROAD_CORE_REWARDS.items()
        ]
        await interaction.response.send_message(
            embed=info_embed(
                "Roads core payouts",
                "\n".join(lines)
                + "\n\nSubmit a screenshot/proof link and party list. Officers approve the payout before silver is owed.",
            ),
            ephemeral=True,
        )
