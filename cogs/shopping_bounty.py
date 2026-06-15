"""Shopping-bounty cog — per-line claim + material estimator.

Augments the consolidated "Crafting shopping list" bounty (the one posted
by ``post_shopping_bounty.py``) with an interactive UI:

* The bounty embed gets a single **📋 Items & Materials** button.
* Clicking it opens an ephemeral message with a dropdown of all line
  items.
* Selecting an item shows the materials estimate and a **🎯 Claim** /
  **🔓 Release** toggle so crafters can call dibs.
* Claims are stored in ``bounty_shopping_items`` so they survive
  restarts and so multiple crafters don't double-up on the same row.

Whole-bounty submission still goes through the normal /bounty flow; the
line claims are purely a coordination layer.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from cogs._materials import estimate_materials
from cogs._typing import Bot
from debug import error_log, info_log
from utils import error_embed, info_embed, is_officer, success_embed, warning_embed


OPEN_TEMPLATE = r"shopping:open:(?P<bid>[0-9]+)"
CLAIM_TEMPLATE = r"shopping:claim:(?P<bid>[0-9]+):(?P<line>[0-9]+)"
RELEASE_TEMPLATE = r"shopping:release:(?P<bid>[0-9]+):(?P<line>[0-9]+)"
SUBMIT_TEMPLATE = r"shopping:submit:(?P<bid>[0-9]+):(?P<line>[0-9]+)"
CONFIRM_TEMPLATE = r"shopping:confirm:(?P<bid>[0-9]+):(?P<line>[0-9]+)"
REJECT_TEMPLATE = r"shopping:reject:(?P<bid>[0-9]+):(?P<line>[0-9]+)"


SELECT_COLS = (
    "id, line_index, item_id, name, quality, enchant, needed, fulfilled, "
    "unit_reward, service_fee, claimed_by, claimed_at, submitted_at, "
    "confirmed_by, confirmed_at, rejection_note"
)


def _status(row: dict) -> str:
    """Derive a status string from row columns. One of:
    ``open`` → ``claimed`` → ``submitted`` → ``confirmed``.
    """
    if row.get("confirmed_at"):
        return "confirmed"
    if row.get("submitted_at"):
        return "submitted"
    if row.get("claimed_by"):
        return "claimed"
    return "open"


_STATUS_DISPLAY = {
    "open": ("🟩", "Available"),
    "claimed": ("🟨", "Claimed"),
    "submitted": ("🟦", "Awaiting officer confirmation"),
    "confirmed": ("✅", "Delivered & confirmed"),
}


async def _refresh_bounty_message(
    client: discord.Client, db, bounty_id: int,
) -> None:
    """Re-render the consolidated shopping-list bounty message so the
    table reflects current delivery progress (✅ confirmed lines disappear
    from the running totals, 🟨 claimed / 🟦 submitted are tagged inline).

    Best-effort: any failure (missing message, perms, etc.) is logged and
    swallowed so the triggering interaction still succeeds.
    """
    try:
        if not db.connection:
            db.connect()
        db.cursor.execute(
            "SELECT id, title, channel_id, message_id, deadline "
            "FROM bounties WHERE id = ?",
            (int(bounty_id),),
        )
        b = db.cursor.fetchone()
        if not b or not b["channel_id"] or not b["message_id"]:
            return
        rows = _fetch_items(db, bounty_id, include_confirmed=True)
        if not rows:
            return
        channel = client.get_channel(int(b["channel_id"]))
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(int(b["message_id"]))  # type: ignore[attr-defined]
        except (discord.NotFound, discord.Forbidden):
            return

        # Partition for the summary line.
        n_open = sum(1 for r in rows if _status(r) == "open")
        n_claim = sum(1 for r in rows if _status(r) == "claimed")
        n_sub = sum(1 for r in rows if _status(r) == "submitted")
        n_done = sum(1 for r in rows if _status(r) == "confirmed")

        # Reward pool: total vs still-outstanding.
        def _line_payout(r: dict) -> int:
            return (int(r.get("unit_reward") or 0)
                    * int(r["needed"] or 1)
                    + int(r.get("service_fee") or 0))
        total_pool = sum(_line_payout(r) for r in rows)
        remaining = sum(_line_payout(r) for r in rows
                        if _status(r) != "confirmed")

        # Rebuild table.
        emoji_for = {
            "open": "🟩", "claimed": "🟨",
            "submitted": "🟦", "confirmed": "✅",
        }
        table = [
            f"{'St':<2}  {'Need':>4} {'Q':>1} {'E':>1}  "
            f"{'Payout':>10}  Item",
        ]
        for r in rows:
            st = _status(r)
            tag = emoji_for[st]
            name = r["name"]
            if st == "claimed" and r.get("claimed_by"):
                name = f"{name} (claimed)"
            elif st == "submitted":
                name = f"{name} (awaiting officer)"
            elif st == "confirmed":
                name = f"~~{name}~~"
            table.append(
                f"{tag}  {int(r['needed'] or 0):4d} "
                f"{int(r['quality'] or 1):>1} "
                f"{int(r['enchant'] or 0):>1}  "
                f"{_line_payout(r):>10,}  {name}"
            )

        description = (
            "The logistician needs gear. Pick anything from this list, "
            "craft it, and drop it off in the guild bank.\n\n"
            "**Current shopping list** "
            f"(🟩 {n_open} open · 🟨 {n_claim} claimed · "
            f"🟦 {n_sub} pending · ✅ {n_done} delivered)\n"
            "```\n" + "\n".join(table) + "\n```\n"
            "Payout per row = **unit price × quantity + service fee**.\n"
            "👉 Hit **📋 Items & Materials** below to pick a row."
        )

        # Mutate or rebuild the embed.
        embed = msg.embeds[0] if msg.embeds else discord.Embed(
            title=b["title"], colour=0xE5A100,
        )
        embed.description = description
        # Refresh known fields in place; add if missing.
        existing = {f.name: i for i, f in enumerate(embed.fields)}
        pool_text = (
            f"🪙 **{remaining:,}** silver left\n"
            f"(of {total_pool:,} total)"
        )
        if "Total payout pool" in existing:
            embed.set_field_at(
                existing["Total payout pool"],
                name="Total payout pool", value=pool_text, inline=True,
            )
        else:
            embed.add_field(name="Total payout pool",
                            value=pool_text, inline=True)
        all_done = (n_open + n_claim + n_sub) == 0
        if "Status" in existing:
            embed.set_field_at(
                existing["Status"], name="Status",
                value=(
                    "✅ **All items delivered!**" if all_done else
                    "🟢 Open — claim items below or take the whole "
                    f"bounty via `/bounty claim id:{bounty_id}`."
                ),
                inline=False,
            )

        await msg.edit(embed=embed)
    except Exception as exc:  # noqa: BLE001
        error_log(f"shopping bounty refresh failed: {exc!r}")


async def _finalize_officer_task(
    message: discord.Message | None,
    actor: discord.abc.User,
    *,
    outcome: str,
) -> None:
    """Strip the Confirm/Reject buttons off an officer-task message once
    it's been actioned. Adds a footer tag so officers can see who handled
    it at a glance. Best-effort — swallows errors so the main flow still
    succeeds even if the edit fails (e.g. message deleted)."""
    if message is None:
        return
    try:
        embed = message.embeds[0] if message.embeds else None
        if embed is not None:
            embed.set_footer(text=f"{outcome} by {actor}")
            await message.edit(embed=embed, view=None)
        else:
            await message.edit(view=None)
    except Exception as exc:  # noqa: BLE001
        error_log(f"officer task finalize failed: {exc!r}")


def _fetch_items(
    db, bounty_id: int, *, include_confirmed: bool = True,
) -> list[dict]:
    if not db.connection:
        db.connect()
    where = "WHERE bounty_id = ?"
    if not include_confirmed:
        # Confirmed deliveries fall off the shopping list automatically.
        where += " AND confirmed_at IS NULL"
    db.cursor.execute(
        f"SELECT {SELECT_COLS} FROM bounty_shopping_items "
        f"{where} ORDER BY line_index ASC",
        (int(bounty_id),),
    )
    return [dict(r) for r in db.cursor.fetchall()]


def _fetch_line(db, bounty_id: int, line_index: int) -> dict | None:
    if not db.connection:
        db.connect()
    db.cursor.execute(
        f"SELECT {SELECT_COLS} FROM bounty_shopping_items "
        "WHERE bounty_id = ? AND line_index = ?",
        (int(bounty_id), int(line_index)),
    )
    row = db.cursor.fetchone()
    return dict(row) if row else None


def _format_item_label(row: dict) -> str:
    """Short label for the dropdown (max 100 chars)."""
    name = row["name"]
    q = int(row["quality"] or 1)
    e = int(row["enchant"] or 0)
    qbit = f" Q{q}" if q > 1 else ""
    ebit = f" +{e}" if e > 0 else ""
    needed = int(row["needed"] or 1)
    payout = (int(row.get("unit_reward") or 0) * needed
              + int(row.get("service_fee") or 0))
    pay_bit = f" — {payout // 1000}k" if payout >= 1000 else ""
    label = f"{needed}× {name}{qbit}{ebit}{pay_bit}"
    return label[:100]


def _format_item_desc(row: dict) -> str:
    st = _status(row)
    emoji, label = _STATUS_DISPLAY[st]
    claimed = row.get("claimed_by")
    if st == "open":
        return "Available · pick to view materials"[:100]
    if st == "claimed":
        return f"Claimed by member · pick to view materials"[:100]
    if st == "submitted":
        return f"Submitted — awaiting officer confirmation"[:100]
    return f"✅ Delivered & confirmed"[:100]


# ── Components ──────────────────────────────────────────────────────────────


class ShoppingOpenButton(
    discord.ui.DynamicItem[discord.ui.Button], template=OPEN_TEMPLATE,
):
    """Persistent button on the bounty embed: opens the items dropdown."""

    def __init__(self, bounty_id: int) -> None:
        self.bounty_id = bounty_id
        super().__init__(discord.ui.Button(
            label="Items & Materials", style=discord.ButtonStyle.primary,
            emoji="📋",
            custom_id=f"shopping:open:{bounty_id}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "db", None)
        if db is None:
            await interaction.response.send_message(
                embed=error_embed("Internal error", "Database unavailable."),
                ephemeral=True,
            )
            return
        rows = _fetch_items(db, self.bounty_id, include_confirmed=False)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(
                    "All done!",
                    "Every item on this shopping list has been delivered "
                    "and confirmed. Use `/shopping summary` to see the "
                    "full history.",
                ),
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=600)
        view.add_item(ShoppingPickSelect(self.bounty_id, rows))
        await interaction.response.send_message(
            embed=info_embed(
                "🛒 Pick an item",
                "Choose which item you want to craft. You'll see a material "
                "estimate plus a Claim button so others know you're on it.",
            ),
            view=view, ephemeral=True,
        )


class ShoppingPickSelect(discord.ui.Select):
    """Transient (ephemeral-only) Select listing the line items."""

    def __init__(self, bounty_id: int, rows: list[dict]) -> None:
        self.bounty_id = bounty_id
        options = [
            discord.SelectOption(
                label=_format_item_label(r),
                description=_format_item_desc(r),
                value=str(r["line_index"]),
                emoji=_STATUS_DISPLAY[_status(r)][0],
            )
            for r in rows[:25]
        ]
        super().__init__(
            placeholder="Choose an item to view materials…",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        line_index = int(self.values[0])
        db = getattr(interaction.client, "db", None)
        if db is None:
            await interaction.response.send_message(
                embed=error_embed("Internal error", "Database unavailable."),
                ephemeral=True,
            )
            return
        row = _fetch_line(db, self.bounty_id, line_index)
        if not row:
            await interaction.response.send_message(
                embed=error_embed("Gone", "That item is no longer listed."),
                ephemeral=True,
            )
            return
        embed = _build_line_embed(row)
        view = _buttons_for_row(self.bounty_id, line_index, row,
                                interaction.user)
        if view.children:
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=embed, ephemeral=True,
            )


def _buttons_for_row(
    bounty_id: int, line_index: int, row: dict, user: discord.abc.User,
) -> discord.ui.View:
    """Return the right set of action buttons based on status + viewer."""
    view = discord.ui.View(timeout=600)
    st = _status(row)
    user_id = str(user.id)
    owner = row.get("claimed_by") or ""
    officer = is_officer(user)

    if st == "open":
        view.add_item(ShoppingClaimButton(bounty_id, line_index))
    elif st == "claimed":
        if user_id == owner:
            view.add_item(ShoppingSubmitButton(bounty_id, line_index))
            view.add_item(ShoppingReleaseButton(bounty_id, line_index))
    elif st == "submitted":
        if officer:
            view.add_item(ShoppingConfirmButton(bounty_id, line_index))
            view.add_item(ShoppingRejectButton(bounty_id, line_index))
    # confirmed → no buttons
    return view


class ShoppingClaimButton(
    discord.ui.DynamicItem[discord.ui.Button], template=CLAIM_TEMPLATE,
):
    def __init__(self, bounty_id: int, line_index: int) -> None:
        self.bounty_id = bounty_id
        self.line_index = line_index
        super().__init__(discord.ui.Button(
            label="Claim", style=discord.ButtonStyle.success, emoji="🎯",
            custom_id=f"shopping:claim:{bounty_id}:{line_index}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]), int(match["line"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "db", None)
        if db is None:
            await interaction.response.send_message(
                embed=error_embed("Internal error", "Database unavailable."),
                ephemeral=True,
            )
            return
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "UPDATE bounty_shopping_items "
                "SET claimed_by = ?, claimed_at = CURRENT_TIMESTAMP "
                "WHERE bounty_id = ? AND line_index = ? "
                "      AND (claimed_by IS NULL OR claimed_by = '')",
                (str(interaction.user.id), int(self.bounty_id),
                 int(self.line_index)),
            )
            changed = db.cursor.rowcount
            db.connection.commit()
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping claim failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't record the claim."),
                ephemeral=True,
            )
            return
        if not changed:
            await interaction.response.send_message(
                embed=error_embed(
                    "Already claimed",
                    "Someone else beat you to it. Pick another item.",
                ),
                ephemeral=True,
            )
            return
        row = _fetch_line(db, self.bounty_id, self.line_index)
        info_log(
            f"{interaction.user} claimed shopping line "
            f"#{self.bounty_id}/{self.line_index} ({row['item_id'] if row else '?'})."
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Claimed",
                f"You're on **{row['name']}** (×{row['needed']}).\n\n"
                "**Next steps:**\n"
                "1. Craft it and deposit it in the guild bank.\n"
                "2. Re-open this bounty, pick this item again, and hit "
                "**📦 Mark Delivered** to notify an officer.\n"
                "3. An officer will confirm the deposit and your payout "
                "will be released.\n\n"
                "Need to back out? Hit **🔓 Release** to put it back on "
                "the board.",
            ),
            ephemeral=True,
        )
        await _refresh_bounty_message(
            interaction.client, db, self.bounty_id,
        )


class ShoppingReleaseButton(
    discord.ui.DynamicItem[discord.ui.Button], template=RELEASE_TEMPLATE,
):
    def __init__(self, bounty_id: int, line_index: int) -> None:
        self.bounty_id = bounty_id
        self.line_index = line_index
        super().__init__(discord.ui.Button(
            label="Release", style=discord.ButtonStyle.secondary, emoji="🔓",
            custom_id=f"shopping:release:{bounty_id}:{line_index}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]), int(match["line"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "db", None)
        if db is None:
            return
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "UPDATE bounty_shopping_items "
                "SET claimed_by = NULL, claimed_at = NULL "
                "WHERE bounty_id = ? AND line_index = ? AND claimed_by = ?",
                (int(self.bounty_id), int(self.line_index),
                 str(interaction.user.id)),
            )
            db.connection.commit()
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping release failed: {exc!r}")
        await interaction.response.send_message(
            embed=info_embed("Released",
                             "Item is back on the board for someone else."),
            ephemeral=True,
        )
        await _refresh_bounty_message(
            interaction.client, db, self.bounty_id,
        )


class ShoppingSubmitButton(
    discord.ui.DynamicItem[discord.ui.Button], template=SUBMIT_TEMPLATE,
):
    """Claimer flags the line as delivered; awaits officer confirmation."""

    def __init__(self, bounty_id: int, line_index: int) -> None:
        self.bounty_id = bounty_id
        self.line_index = line_index
        super().__init__(discord.ui.Button(
            label="Mark Delivered", style=discord.ButtonStyle.primary,
            emoji="📦",
            custom_id=f"shopping:submit:{bounty_id}:{line_index}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]), int(match["line"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "db", None)
        if db is None:
            return
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "UPDATE bounty_shopping_items "
                "SET submitted_at = CURRENT_TIMESTAMP "
                "WHERE bounty_id = ? AND line_index = ? AND claimed_by = ? "
                "      AND submitted_at IS NULL AND confirmed_at IS NULL",
                (int(self.bounty_id), int(self.line_index),
                 str(interaction.user.id)),
            )
            changed = db.cursor.rowcount
            db.connection.commit()
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping submit failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't record delivery."),
                ephemeral=True,
            )
            return
        if not changed:
            await interaction.response.send_message(
                embed=error_embed(
                    "Can't submit",
                    "Either you don't own this claim or it's already "
                    "been submitted/confirmed.",
                ),
                ephemeral=True,
            )
            return
        info_log(
            f"{interaction.user} submitted shopping line "
            f"#{self.bounty_id}/{self.line_index}."
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Delivery noted",
                "An officer has been pinged to confirm the deposit. "
                "Your payout is released once they verify.",
            ),
            ephemeral=True,
        )
        # Best-effort: ping the bounty channel so officers see it.
        await _notify_officers_of_submission(
            interaction, self.bounty_id, self.line_index,
        )
        await _refresh_bounty_message(
            interaction.client, db, self.bounty_id,
        )


class ShoppingConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button], template=CONFIRM_TEMPLATE,
):
    """Officer confirms delivery → adds to chest + finalizes the payout."""

    def __init__(self, bounty_id: int, line_index: int) -> None:
        self.bounty_id = bounty_id
        self.line_index = line_index
        super().__init__(discord.ui.Button(
            label="Confirm Delivery", style=discord.ButtonStyle.success,
            emoji="✅",
            custom_id=f"shopping:confirm:{bounty_id}:{line_index}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]), int(match["line"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        db = getattr(interaction.client, "db", None)
        if db is None:
            return
        row = _fetch_line(db, self.bounty_id, self.line_index)
        if not row or _status(row) != "submitted":
            await interaction.response.send_message(
                embed=error_embed(
                    "Not submitted",
                    "This line isn't waiting on confirmation right now.",
                ),
                ephemeral=True,
            )
            return
        try:
            db.cursor.execute(
                "UPDATE bounty_shopping_items "
                "SET confirmed_by = ?, confirmed_at = CURRENT_TIMESTAMP, "
                "    fulfilled = 1 "
                "WHERE bounty_id = ? AND line_index = ?",
                (str(interaction.user.id), int(self.bounty_id),
                 int(self.line_index)),
            )
            db.connection.commit()
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping confirm failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't confirm delivery."),
                ephemeral=True,
            )
            return
        # Auto-deposit into the loadout chest.
        try:
            db.chest_adjust(
                row["item_id"],
                int(row["needed"] or 0),
                quality=int(row["quality"] or 1),
                enchant=int(row["enchant"] or 0),
                reason=(
                    f"shopping bounty #{self.bounty_id} line "
                    f"{self.line_index} delivered by {row.get('claimed_by')}"
                ),
                actor_id=str(interaction.user.id),
                ref_type="shopping_bounty",
                ref_id=f"{self.bounty_id}:{self.line_index}",
            )
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping auto-chest deposit failed: {exc!r}")
        payout = (int(row.get("unit_reward") or 0) * int(row["needed"] or 1)
                  + int(row.get("service_fee") or 0))
        # Credit the crafter's silver ledger so the payout shows up in
        # /silver balance and the officer can settle it in-game later.
        new_balance: int | None = None
        claimer_id = str(row.get("claimed_by") or "")
        if claimer_id and payout > 0:
            try:
                new_balance = db.adjust_silver_balance(
                    claimer_id,
                    int(payout),
                    reason=(
                        f"shopping bounty #{self.bounty_id} line "
                        f"{self.line_index} — {row['needed']}× "
                        f"{row['name']}"
                    ),
                    ref_type="shopping_bounty",
                    ref_id=f"{self.bounty_id}:{self.line_index}",
                    actor_id=str(interaction.user.id),
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"shopping ledger credit failed: {exc!r}")
        info_log(
            f"{interaction.user} confirmed shopping line "
            f"#{self.bounty_id}/{self.line_index} "
            f"({row['needed']}× {row['item_id']}), payout {payout:,}."
        )
        bal_line = (
            f"\nNew balance: 🪙 **{new_balance:+,}** silver."
            if new_balance is not None else
            "\n⚠️ Crafter has no profile — silver ledger not updated."
            if claimer_id and payout > 0 else ""
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Delivery confirmed",
                f"Added **{row['needed']}× {row['name']}** to the chest.\n"
                f"Credited <@{row['claimed_by']}> with **{payout:,}** "
                f"silver.{bal_line}",
            ),
            ephemeral=False,  # public so the crafter sees the confirmation
        )
        # Strip the buttons off the officer-task card so it's clear this
        # one is done.
        await _finalize_officer_task(
            interaction.message, interaction.user, outcome="✅ Confirmed",
        )
        await _refresh_bounty_message(
            interaction.client, db, self.bounty_id,
        )


class ShoppingRejectButton(
    discord.ui.DynamicItem[discord.ui.Button], template=REJECT_TEMPLATE,
):
    """Officer rejects the submission → returns line to ``claimed`` state."""

    def __init__(self, bounty_id: int, line_index: int) -> None:
        self.bounty_id = bounty_id
        self.line_index = line_index
        super().__init__(discord.ui.Button(
            label="Reject", style=discord.ButtonStyle.danger, emoji="❌",
            custom_id=f"shopping:reject:{bounty_id}:{line_index}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["bid"]), int(match["line"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ShoppingRejectModal(
                self.bounty_id, self.line_index,
                origin_message=interaction.message,
            ),
        )


class ShoppingRejectModal(discord.ui.Modal, title="Reject delivery"):
    note = discord.ui.TextInput(
        label="Reason (sent to the crafter)",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. wrong tier deposited, count short, ...",
        required=True, max_length=400,
    )

    def __init__(
        self, bounty_id: int, line_index: int,
        *, origin_message: discord.Message | None = None,
    ) -> None:
        super().__init__()
        self.bounty_id = bounty_id
        self.line_index = line_index
        self.origin_message = origin_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db = getattr(interaction.client, "db", None)
        if db is None:
            return
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "UPDATE bounty_shopping_items "
                "SET submitted_at = NULL, rejection_note = ? "
                "WHERE bounty_id = ? AND line_index = ? "
                "      AND submitted_at IS NOT NULL "
                "      AND confirmed_at IS NULL",
                (str(self.note.value), int(self.bounty_id),
                 int(self.line_index)),
            )
            changed = db.cursor.rowcount
            db.connection.commit()
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping reject failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't reject the delivery."),
                ephemeral=True,
            )
            return
        if not changed:
            await interaction.response.send_message(
                embed=error_embed(
                    "Not submitted",
                    "Line isn't waiting on confirmation.",
                ),
                ephemeral=True,
            )
            return
        row = _fetch_line(db, self.bounty_id, self.line_index)
        info_log(
            f"{interaction.user} rejected shopping line "
            f"#{self.bounty_id}/{self.line_index}: {self.note.value!r}"
        )
        await interaction.response.send_message(
            embed=warning_embed(
                "Delivery rejected",
                f"<@{row.get('claimed_by') if row else '?'}> please "
                f"re-deposit and resubmit.\n\n**Reason:** "
                f"{self.note.value}",
            ),
            ephemeral=False,
        )
        await _finalize_officer_task(
            self.origin_message, interaction.user, outcome="❌ Rejected",
        )
        await _refresh_bounty_message(
            interaction.client, db, self.bounty_id,
        )


async def _notify_officers_of_submission(
    interaction: discord.Interaction, bounty_id: int, line_index: int,
) -> None:
    """Drop a notification with Confirm/Reject buttons in the officer-tasks
    channel so officers can action it without navigating to the bounty."""
    db = getattr(interaction.client, "db", None)
    if db is None:
        return
    row = _fetch_line(db, bounty_id, line_index)
    if not row:
        return
    # Prefer the dedicated automation officer-tasks channel; fall back to
    # the generic officer channel; finally the bounty channel.
    channel_id = (
        db.get_config("automation_officer_channel_id")
        or db.get_config("officer_channel_id")
    )
    if not channel_id:
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "SELECT channel_id FROM bounties WHERE id = ?",
                (int(bounty_id),),
            )
            b = db.cursor.fetchone()
            if b and b["channel_id"]:
                channel_id = b["channel_id"]
        except Exception as exc:  # noqa: BLE001
            error_log(f"officer notify channel lookup failed: {exc!r}")
            return
    if not channel_id:
        return
    try:
        channel = interaction.client.get_channel(int(channel_id))
        if channel is None:
            return
        needed = int(row["needed"] or 1)
        payout = (int(row.get("unit_reward") or 0) * needed
                  + int(row.get("service_fee") or 0))
        q = int(row["quality"] or 1)
        e = int(row["enchant"] or 0)
        qbit = f" Q{q}" if q > 1 else ""
        ebit = f" +{e}" if e > 0 else ""
        embed = discord.Embed(
            title="📦 Shopping delivery — officer action needed",
            description=(
                f"<@{interaction.user.id}> says **{needed}× "
                f"{row['name']}{qbit}{ebit}** has been deposited for "
                f"bounty #{bounty_id}.\n\n"
                f"**Payout if confirmed:** 🪙 {payout:,} silver\n"
                f"**Item ID:** `{row['item_id']}`\n\n"
                "Hit **✅ Confirm** to auto-add to the chest and release "
                "the payout, or **❌ Reject** if anything's wrong."
            ),
            colour=discord.Colour.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(ShoppingConfirmButton(bounty_id, line_index))
        view.add_item(ShoppingRejectButton(bounty_id, line_index))
        await channel.send(embed=embed, view=view)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        error_log(f"officer notify failed: {exc!r}")


# ── Embed builder ───────────────────────────────────────────────────────────


def _build_line_embed(row: dict) -> discord.Embed:
    mats, wiki = estimate_materials(
        row["item_id"],
        count=int(row["needed"] or 1),
        enchant=int(row["enchant"] or 0),
    )
    q = int(row["quality"] or 1)
    e = int(row["enchant"] or 0)
    qbit = f" Q{q}" if q > 1 else ""
    ebit = f" +{e}" if e > 0 else ""
    needed = int(row["needed"] or 1)
    unit_reward = int(row.get("unit_reward") or 0)
    service_fee = int(row.get("service_fee") or 0)
    payout = unit_reward * needed + service_fee

    embed = discord.Embed(
        title=f"{row['name']}{qbit}{ebit}",
        description=f"**Needed:** {needed}× · `{row['item_id']}`",
        colour=discord.Colour.gold(),
        url=wiki,
    )
    embed.add_field(name="Estimated materials", value=mats, inline=False)
    embed.add_field(
        name="Exact recipe",
        value=f"[albiononline2d.com]({wiki})",
        inline=False,
    )
    if payout > 0:
        embed.add_field(
            name="Your payout",
            value=(
                f"🪙 **{payout:,}** silver\n"
                f"• {unit_reward:,} × {needed} units = "
                f"{unit_reward * needed:,}\n"
                f"• + {service_fee:,} service fee"
            ),
            inline=False,
        )
    if row.get("rejection_note"):
        embed.add_field(
            name="⚠️ Previous delivery rejected",
            value=str(row["rejection_note"])[:1000],
            inline=False,
        )
    st = _status(row)
    emoji, label = _STATUS_DISPLAY[st]
    status_value = f"{emoji} {label}"
    if st == "claimed":
        status_value += f"\nClaimed by <@{row['claimed_by']}>"
    elif st == "submitted":
        status_value += (
            f"\nClaimed by <@{row['claimed_by']}> — awaiting officer."
        )
    elif st == "confirmed":
        status_value += (
            f"\nDelivered by <@{row['claimed_by']}>, confirmed by "
            f"<@{row['confirmed_by']}>."
        )
    embed.add_field(name="Status", value=status_value, inline=False)
    embed.set_footer(
        text="Payout is a guideline — logistician may tip more for rares.",
    )
    return embed


# ── Cog & registration ──────────────────────────────────────────────────────


def register_persistent_shopping_views(bot: Bot) -> None:
    bot.add_dynamic_items(
        ShoppingOpenButton,
        ShoppingClaimButton,
        ShoppingReleaseButton,
        ShoppingSubmitButton,
        ShoppingConfirmButton,
        ShoppingRejectButton,
    )


class ShoppingBounty(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        register_persistent_shopping_views(self.bot)

    # /shopping summary — quick officer view of claim state.
    shopping = app_commands.Group(
        name="shopping",
        description="Crafting shopping list helpers.",
    )

    @shopping.command(name="summary",
                      description="Show claim status of a shopping bounty.")
    @app_commands.describe(bounty_id="The bounty ID (e.g. 21).")
    async def shopping_summary(
        self, interaction: discord.Interaction, bounty_id: int,
    ) -> None:
        rows = _fetch_items(self.bot.db, bounty_id)
        if not rows:
            await interaction.response.send_message(
                embed=error_embed("Empty",
                                  f"No itemized rows for bounty #{bounty_id}."),
                ephemeral=True,
            )
            return
        buckets: dict[str, list[dict]] = {
            "open": [], "claimed": [], "submitted": [], "confirmed": [],
        }
        for r in rows:
            buckets[_status(r)].append(r)
        embed = discord.Embed(
            title=f"Shopping bounty #{bounty_id} — line status",
            colour=discord.Colour.blurple(),
        )
        embed.description = (
            f"Total: **{len(rows)}** · "
            f"🟩 open **{len(buckets['open'])}** · "
            f"🟨 claimed **{len(buckets['claimed'])}** · "
            f"🟦 submitted **{len(buckets['submitted'])}** · "
            f"✅ confirmed **{len(buckets['confirmed'])}**"
        )
        for st in ("submitted", "claimed", "open", "confirmed"):
            rs = buckets[st]
            if not rs:
                continue
            emoji, label = _STATUS_DISPLAY[st]
            lines = []
            for r in rs[:15]:
                tag = f" → <@{r['claimed_by']}>" if r.get("claimed_by") else ""
                lines.append(f"{emoji} {r['needed']}× {r['name']}{tag}")
            embed.add_field(name=label, value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @shopping.command(
        name="remove",
        description="Officer: drop a line from the active shopping list.",
    )
    @app_commands.describe(
        bounty_id="The bounty ID (e.g. 21).",
        line_index="The line_index from /shopping summary.",
    )
    async def shopping_remove(
        self, interaction: discord.Interaction,
        bounty_id: int, line_index: int,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Officers only."),
                ephemeral=True,
            )
            return
        db = self.bot.db
        row = _fetch_line(db, bounty_id, line_index)
        if not row:
            await interaction.response.send_message(
                embed=error_embed(
                    "Not found",
                    f"No line {line_index} on bounty #{bounty_id}.",
                ),
                ephemeral=True,
            )
            return
        if _status(row) == "claimed":
            await interaction.response.send_message(
                embed=error_embed(
                    "Line is claimed",
                    f"<@{row.get('claimed_by')}> currently has this line "
                    "claimed. Ask them to release it first, or reject "
                    "their submission, before removing.",
                ),
                ephemeral=True,
            )
            return
        try:
            if not db.connection:
                db.connect()
            db.cursor.execute(
                "DELETE FROM bounty_shopping_items "
                "WHERE bounty_id = ? AND line_index = ?",
                (int(bounty_id), int(line_index)),
            )
            db.connection.commit()
        except Exception as exc:  # noqa: BLE001
            error_log(f"shopping remove failed: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed("DB error", "Couldn't remove that line."),
                ephemeral=True,
            )
            return
        info_log(
            f"{interaction.user} removed shopping line "
            f"#{bounty_id}/{line_index} ({row['needed']}× {row['item_id']})."
        )
        await interaction.response.send_message(
            embed=success_embed(
                "Line removed",
                f"Dropped **{row['needed']}× {row['name']}** from bounty "
                f"#{bounty_id}.",
            ),
            ephemeral=True,
        )
        await _refresh_bounty_message(interaction.client, db, bounty_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(ShoppingBounty(bot))
