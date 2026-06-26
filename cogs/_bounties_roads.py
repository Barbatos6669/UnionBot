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
    "gold": 10_000_000,
}
ROAD_CORE_EMOJIS: dict[str, str] = {
    "green": "🟢",
    "blue": "🔵",
    "purple": "🟣",
    "gold": "🟡",
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
        "gold": "gold",
        "yellow": "gold",
        "y": "gold",
        "t10": "gold",
    }
    color = aliases.get(text)
    if color:
        return color, None
    return None, "Use `green`, `blue`, `purple`, or `gold`."


def parse_road_core_price(raw: str) -> tuple[int | None, str | None]:
    text = (
        str(raw or "")
        .strip()
        .lower()
        .replace(",", "")
        .replace("_", "")
    )
    text = text.replace("silver", "").strip()
    if not text:
        return None, "Enter a silver amount."

    parts = text.split()
    if len(parts) == 2:
        number_text, suffix = parts
    elif len(parts) == 1:
        number_text = parts[0]
        suffix = ""
        for candidate in ("million", "mil", "m", "thousand", "k"):
            if number_text.endswith(candidate) and number_text != candidate:
                suffix = candidate
                number_text = number_text[: -len(candidate)]
                break
    else:
        return None, "Use a value like `1m`, `3000000`, or `10 million`."

    multipliers = {
        "": 1,
        "s": 1,
        "silver": 1,
        "k": 1_000,
        "thousand": 1_000,
        "m": 1_000_000,
        "mil": 1_000_000,
        "million": 1_000_000,
    }
    multiplier = multipliers.get(suffix)
    if multiplier is None:
        return None, "Supported suffixes are `k`, `m`, `thousand`, and `million`."
    try:
        number = float(number_text)
    except ValueError:
        return None, "Enter a valid number."
    amount = int(number * multiplier)
    if amount <= 0:
        return None, "Amount must be greater than zero."
    return amount, None


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
    def __init__(self, rewards: dict[str, int] | None = None) -> None:
        rewards = rewards or ROAD_CORE_REWARDS
        options = []
        for color, default_amount in ROAD_CORE_REWARDS.items():
            amount = int(rewards.get(color, default_amount))
            options.append(
                discord.SelectOption(
                    label=f"{color.title()} core",
                    value=color,
                    emoji=ROAD_CORE_EMOJIS[color],
                    description=f"{fmt_silver(amount)} silver",
                )
            )
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
    def __init__(self, rewards: dict[str, int] | None = None) -> None:
        super().__init__(timeout=300)
        self.add_item(RoadsCoreColorSelect(rewards))


class RoadsCorePriceModal(discord.ui.Modal, title="Roads Core Payouts"):
    def __init__(self, rewards: dict[str, int] | None = None) -> None:
        super().__init__(timeout=300)
        rewards = rewards or ROAD_CORE_REWARDS
        self.price_inputs: dict[str, discord.ui.TextInput] = {}
        for color, default_amount in ROAD_CORE_REWARDS.items():
            field = discord.ui.TextInput(
                label=f"{color.title()} core payout",
                default=str(int(rewards.get(color, default_amount))),
                placeholder="Example: 10m",
                required=True,
                max_length=32,
            )
            self.price_inputs[color] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        values = {
            color: str(field.value or "")
            for color, field in self.price_inputs.items()
        }
        await cog._update_roads_core_prices(interaction, values)


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
        cog = _get_bounty_cog(interaction)
        rewards = cog._roads_core_rewards() if cog else ROAD_CORE_REWARDS
        lines = [
            f"{ROAD_CORE_EMOJIS[color]} **{color.title()}** — 🪙 **{fmt_silver(amount)}**"
            for color, amount in rewards.items()
        ]
        await interaction.response.send_message(
            embed=info_embed(
                "Roads core payouts",
                "\n".join(lines)
                + "\n\nPick a color, then paste/upload the screenshot directly in the bounty channel. Officers approve the payout before silver is owed.",
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Edit Prices",
        style=discord.ButtonStyle.secondary,
        emoji="🛠️",
        custom_id="roads_cores:prices",
    )
    async def edit_prices(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = _get_bounty_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._open_roads_core_price_modal(interaction)
