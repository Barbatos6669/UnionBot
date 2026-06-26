"""Roads power-core bounty board controls."""
from __future__ import annotations

import discord
from discord.ext import commands

from cogs._bounties_config import fmt_silver
from utils import info_embed

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


def image_attachment_url(message) -> str | None:
    image_ext = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    for attachment in getattr(message, "attachments", []) or []:
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
        filename = str(getattr(attachment, "filename", "") or "").lower()
        if content_type.startswith("image/") or filename.endswith(image_ext):
            return str(getattr(attachment, "url", "") or "").strip() or None
    return None


class RoadsCoreColorSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="Green core",
                value="green",
                emoji=ROAD_CORE_EMOJIS["green"],
                description=f"{fmt_silver(ROAD_CORE_REWARDS['green'])} silver",
            ),
            discord.SelectOption(
                label="Blue core",
                value="blue",
                emoji=ROAD_CORE_EMOJIS["blue"],
                description=f"{fmt_silver(ROAD_CORE_REWARDS['blue'])} silver",
            ),
            discord.SelectOption(
                label="Purple core",
                value="purple",
                emoji=ROAD_CORE_EMOJIS["purple"],
                description=f"{fmt_silver(ROAD_CORE_REWARDS['purple'])} silver",
            ),
        ]
        super().__init__(
            placeholder="Pick the core color...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="roads_cores:color",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._start_roads_core_image_capture(interaction, color=self.values[0])


class RoadsCoreColorView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.add_item(RoadsCoreColorSelect())


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
                + "\n\nPick a color, then paste/upload the screenshot directly in the bounty channel. Officers approve the payout before silver is owed.",
            ),
            ephemeral=True,
        )
