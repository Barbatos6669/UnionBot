"""Discord UI controls for the bounty board.

The heavy business logic stays on ``Bounties``; these persistent buttons and
modals call back into that cog by name so the public cog file stays readable.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from cogs._bounties_config import (
    APPROVE_TEMPLATE,
    CLAIM_TEMPLATE,
    MAX_REWARD,
    PAID_TEMPLATE,
    REJECT_TEMPLATE,
    STATUS_CLAIMED,
    STATUS_COMPLETED,
    STATUS_OPEN,
    STATUS_PENDING,
    STATUS_SUBMITTED,
    SUBMIT_TEMPLATE,
    UNCLAIM_TEMPLATE,
    fmt_silver as _fmt_silver,
    parse_deadline as _parse_deadline,
    bounty_needs_payment as _bounty_needs_payment,
)
from cogs._bounties_db import db_create as _db_create, db_get as _db_get
from cogs._bounties_roads import RoadsCoreBoardView
from cogs._bounties_sso import SSORouteBoardView, SubmitSSORouteModal
from cogs._typing import Bot
from debug import info_log
from utils import error_embed, success_embed
from utils import is_officer as _is_officer

SSO_TITLE_PREFIX = "[SSO Route]"


class _PostBountyModal(discord.ui.Modal, title="Post a Bounty"):
    def __init__(self, cog: "Bounties") -> None:
        super().__init__()
        self.cog = cog

        self.title_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Title", placeholder="e.g. Bring 100 T6 Hide",
            max_length=128, required=True,
        )
        self.desc_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Description",
            placeholder="What needs to be done? Where? Any specifics?",
            style=discord.TextStyle.paragraph, max_length=1500, required=True,
        )
        self.reward_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Reward (silver)", placeholder="e.g. 250000  (250k silver)",
            max_length=12, required=True,
        )
        self.deadline_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Deadline (optional)",
            placeholder="2026-05-15  •  3d  •  12h  •  blank for none",
            max_length=32, required=False,
        )
        for item in (self.title_input, self.desc_input,
                     self.reward_input, self.deadline_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Parse reward
        try:
            reward = int(str(self.reward_input.value).strip())
        except (TypeError, ValueError):
            await interaction.response.send_message(
                embed=error_embed("Invalid reward", "Reward must be a whole number."),
                ephemeral=True,
            )
            return
        if reward <= 0 or reward > MAX_REWARD:
            await interaction.response.send_message(
                embed=error_embed("Invalid reward", f"Pick a value between 1 and {MAX_REWARD:,} silver."),
                ephemeral=True,
            )
            return

        # Parse deadline
        deadline_raw = str(self.deadline_input.value or "").strip()
        deadline_iso = _parse_deadline(deadline_raw) if deadline_raw else None
        if deadline_raw and deadline_iso is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "Invalid deadline",
                    "Use a date like `2026-05-15`, a datetime like "
                    "`2026-05-15 21:00`, or relative like `3d`, `12h`, `30m`.",
                ),
                ephemeral=True,
            )
            return

        bounty_id = _db_create(
            self.cog.bot.db,
            title=str(self.title_input.value).strip(),
            description=str(self.desc_input.value).strip(),
            reward=reward,
            posted_by=str(interaction.user.id),
            deadline=deadline_iso,
        )
        if not bounty_id:
            await interaction.response.send_message(
                embed=error_embed("Database error", "Could not create the bounty."),
                ephemeral=True,
            )
            return

        bounty = _db_get(self.cog.bot.db, bounty_id)  # type: ignore[attr-defined]
        if bounty:
            await self.cog._post_or_update_board_message(bounty)

        info_log(f"{interaction.user} proposed bounty #{bounty_id}: {self.title_input.value}.")
        await interaction.response.send_message(
            embed=success_embed(
                f"Bounty #{bounty_id} submitted for review",
                f"**{self.title_input.value}** — reward **{_fmt_silver(reward)}** silver.\n\n"
                f"An officer will review and either approve it onto the public "
                f"board or reject it.",
            ),
            ephemeral=True,
        )


# ── Buttons (DynamicItem persistent components) ──────────────────────────────
# custom_id formats:
#   bounty:claim:<id>      bounty:unclaim:<id>     bounty:submit:<id>
#   bounty:approve:<id>    bounty:reject:<id>      (officer; works in both gates)
# Regex templates live in cogs._bounties_config and are imported above.




def _get_cog(interaction: discord.Interaction):
    bot = interaction.client
    cog = bot.get_cog("Bounties") if isinstance(bot, commands.Bot) else None
    return cog


class _RejectModal(discord.ui.Modal, title="Reject bounty"):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(timeout=None)
        self.bounty_id = bounty_id
        self.reason = discord.ui.TextInput(
            label="Reason (proposer / claimer sees this)",
            placeholder="Be specific.",
            style=discord.TextStyle.paragraph,
            min_length=5, max_length=400, required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._do_reject(interaction, self.bounty_id, str(self.reason.value))


class _SubmitProofModal(discord.ui.Modal, title="Submit proof"):
    def __init__(self, bounty_id: int) -> None:
        super().__init__(timeout=None)
        self.bounty_id = bounty_id
        self.proof = discord.ui.TextInput(
            label="Proof (link or short description)",
            placeholder="Screenshot URL, in-game receipt, etc.",
            style=discord.TextStyle.paragraph,
            min_length=3, max_length=1500, required=True,
        )
        self.add_item(self.proof)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if not cog:
            await interaction.response.send_message("Cog not loaded.", ephemeral=True)
            return
        await cog._do_submit(interaction, self.bounty_id, str(self.proof.value))

class _BountyTierPickSelect(discord.ui.Select):
    """One-shot Select shown to an officer approving a SUBMITTED tiered
    bounty. Picking a tier overrides the row's reward and finalizes."""

    def __init__(self, cog, bounty_id: int, tiers: list[dict]) -> None:
        self.cog = cog
        self.bounty_id = bounty_id
        self.tiers = tiers
        options: list[discord.SelectOption] = []
        for t in tiers[:25]:
            name = str(t.get("name") or "?")
            silver = int(t.get("silver", 0))
            options.append(discord.SelectOption(
                label=f"{name} — {_fmt_silver(silver)} silver"[:100],
                value=name,
                emoji=(t.get("emoji") or None),
                description=f"Pay {_fmt_silver(silver)} silver"[:100],
            ))
        super().__init__(
            placeholder="Pick the delivered tier…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        choice = self.values[0]
        tier = next((t for t in self.tiers if t.get("name") == choice), None)
        if not tier:
            await interaction.response.send_message(
                embed=error_embed("Bad tier", "That tier no longer exists."),
                ephemeral=True,
            )
            return
        silver = int(tier.get("silver", 0))
        # Defer so _finalize can edit_original_response().
        await interaction.response.defer(ephemeral=True, thinking=False)
        await self.cog._finalize_bounty_payout(
            interaction, self.bounty_id, silver,
            tier_label=str(tier.get("name")),
        )


class BountyClaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=CLAIM_TEMPLATE,
):
    def __init__(self, bounty_id: int) -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label="Claim", style=discord.ButtonStyle.success, emoji="🎯",
            custom_id=f"bounty:claim:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if cog:
            await cog._do_claim(interaction, self.bounty_id)


class BountyUnclaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=UNCLAIM_TEMPLATE,
):
    def __init__(self, bounty_id: int) -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label="Unclaim", style=discord.ButtonStyle.secondary, emoji="🔓",
            custom_id=f"bounty:unclaim:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = _get_cog(interaction)
        if cog:
            await cog._do_unclaim(interaction, self.bounty_id)


class BountySubmitButton(
    discord.ui.DynamicItem[discord.ui.Button], template=SUBMIT_TEMPLATE,
):
    def __init__(self, bounty_id: int) -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label="Submit Proof", style=discord.ButtonStyle.primary, emoji="📤",
            custom_id=f"bounty:submit:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        # SSO Route bounties get a structured 5-field modal so scouts are
        # prompted for portals/note/ttl rather than free-form text.
        cog = _get_cog(interaction)
        is_sso = False
        if cog is not None:
            try:
                b = _db_get(cog.bot.db, self.bounty_id)
                title = (b or {}).get("title", "") or ""
                is_sso = title.startswith(SSO_TITLE_PREFIX)
            except Exception:
                is_sso = False
        if is_sso:
            await interaction.response.send_modal(SubmitSSORouteModal(self.bounty_id))
        else:
            await interaction.response.send_modal(_SubmitProofModal(self.bounty_id))


class BountyApproveButton(
    discord.ui.DynamicItem[discord.ui.Button], template=APPROVE_TEMPLATE,
):
    def __init__(self, bounty_id: int, *, label: str = "Approve") -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"bounty:approve:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        cog = _get_cog(interaction)
        if cog:
            await cog._do_approve(interaction, self.bounty_id)


class BountyRejectButton(
    discord.ui.DynamicItem[discord.ui.Button], template=REJECT_TEMPLATE,
):
    def __init__(self, bounty_id: int, *, label: str = "Reject") -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label=label, style=discord.ButtonStyle.danger, emoji="❌",
            custom_id=f"bounty:reject:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(_RejectModal(self.bounty_id))


class BountyConfirmPaidButton(
    discord.ui.DynamicItem[discord.ui.Button], template=PAID_TEMPLATE,
):
    def __init__(self, bounty_id: int, *, label: str | None = None) -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label=label or f"Paid #{bounty_id}", style=discord.ButtonStyle.success, emoji="💸",
            custom_id=f"bounty:paid:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        cog = _get_cog(interaction)
        if cog:
            await cog._do_confirm_paid(interaction, self.bounty_id)


def _view_for_bounty(b: dict) -> discord.ui.View | None:
    """Return the right action view for a bounty's current status, or None."""
    status = b["status"]
    bid = int(b["id"])
    view = discord.ui.View(timeout=None)
    if status == STATUS_PENDING:
        view.add_item(BountyApproveButton(bid, label="Approve & Post"))
        view.add_item(BountyRejectButton(bid, label="Deny"))
    elif status == STATUS_OPEN:
        view.add_item(BountyClaimButton(bid))
    elif status == STATUS_CLAIMED:
        view.add_item(BountySubmitButton(bid))
        view.add_item(BountyUnclaimButton(bid))
    elif status == STATUS_SUBMITTED:
        view.add_item(BountyApproveButton(bid, label="Approve & Pay"))
        view.add_item(BountyRejectButton(bid, label="Send Back"))
    elif status == STATUS_COMPLETED and _bounty_needs_payment(b):
        view.add_item(BountyConfirmPaidButton(bid))
    else:
        return None
    return view


def register_persistent_bounty_views(bot: Bot) -> None:
    bot.add_dynamic_items(
        BountyClaimButton, BountyUnclaimButton, BountySubmitButton,
        BountyApproveButton, BountyRejectButton, BountyConfirmPaidButton,
    )
    bot.add_view(SSORouteBoardView())
    bot.add_view(RoadsCoreBoardView())
