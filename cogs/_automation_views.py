"""Discord UI views/modals used by the Automation cog.

* ``TreasuryEntryModal`` / ``TreasuryPromptView`` — daily treasury check-in.
* ``InactivitySweepView`` — officer-action buttons on the inactivity report
  (review members, snooze 24h, dismiss).
* ``PolicyDriftView`` — officer-action buttons on the policy-drift alert
  (re-snapshot, snooze, dismiss).
Kept under a leading-underscore filename so ``bot.py``'s cog auto-loader
skips this file. Functions referenced from the daily-routines section of
``cogs/automation.py`` (``_build_inactivity_embed``, ``_detect_policy_drift``)
are imported lazily inside the button callbacks to avoid an import cycle.
"""
from __future__ import annotations

from cogs._typing import Bot
import hashlib

import discord

from debug import info_log, error_log
from utils import error_embed, info_embed, success_embed

from cogs._automation_helpers import (
    _now,
    _set_snooze,
)


# ── Officer-permission helpers ──────────────────────────────────────────────

def _officer_check(interaction: discord.Interaction) -> bool:
    """True iff the clicker has manage_guild. Used to gate officer buttons."""
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


async def _reject_non_officer(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=error_embed(
            "Officer-only", "Only officers can use this control.",
        ),
        ephemeral=True,
    )


class TreasuryEntryModal(discord.ui.Modal, title="Record guild treasury"):
    """Modal triggered by the daily officer-task button or by a slash cmd."""

    balance = discord.ui.TextInput(
        label="Guild silver balance (numbers only)",
        placeholder="e.g. 250000000",
        required=True,
        max_length=15,
    )
    note = discord.ui.TextInput(
        label="Note (optional)",
        placeholder="e.g. after weekly payouts",
        required=False,
        max_length=200,
        style=discord.TextStyle.short,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re
        raw = re.sub(r"[^0-9-]", "", str(self.balance.value))
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                embed=error_embed("Bad number", "Couldn't parse a numeric balance."),
                ephemeral=True,
            )
            return
        if amount < 0:
            await interaction.response.send_message(
                embed=error_embed("Bad value", "Treasury balance can't be negative."),
                ephemeral=True,
            )
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        date = _now().strftime("%Y-%m-%d")
        ok = bot.db.record_guild_treasury(
            date, amount,
            recorded_by=str(interaction.user.id),
            note=(str(self.note.value).strip() or None),
        )
        if not ok:
            await interaction.response.send_message(
                embed=error_embed("Save failed", "Database write failed; check logs."),
                ephemeral=True,
            )
            return
        # Build a confirmation + graph, post both back to the channel.
        rows = bot.db.fetch_guild_treasury_history(days=30)
        embed = success_embed(
            f"Treasury recorded — {date}",
            f"Balance: **{amount:,}** silver "
            f"(by {interaction.user.mention})",
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
        try:
            from cogs.graphs import render_treasury_graph
            file = render_treasury_graph(rows)
            await interaction.followup.send(file=file)
        except Exception as exc:  # noqa: BLE001
            error_log(f"treasury graph render failed: {exc!r}")
        info_log(
            f"{interaction.user} recorded guild treasury {date}: {amount:,} silver."
        )


class TreasuryPromptView(discord.ui.View):
    """Persistent view attached to the daily treasury prompt. The button has
    a fixed custom_id so it survives bot restarts via `bot.add_view()`.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Record today's balance",
        style=discord.ButtonStyle.primary,
        emoji="💰",
        custom_id="automation:treasury:record",
    )
    async def record(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                embed=error_embed("Officer-only", "Only officers can record the treasury."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(TreasuryEntryModal())


# ── Officer-action views (inactivity, policy drift) ─────────────────────────


class InactivitySweepView(discord.ui.View):
    """Persistent view on the daily inactivity sweep embed. Lets officers
    refresh the list, snooze repostings, or dismiss the alert.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.primary,
        emoji="🔄",
        custom_id="automation:inactivity:refresh",
    )
    async def refresh(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        from cogs.automation import _build_inactivity_embed
        embed, count = _build_inactivity_embed(bot)
        if embed is None:
            embed = info_embed(
                "🧹  Inactivity sweep",
                "Nobody is currently over the inactivity threshold. ✅",
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return
        await interaction.response.edit_message(embed=embed, view=self)
        info_log(
            f"{interaction.user} refreshed inactivity sweep ({count} candidates).",
        )

    @discord.ui.button(
        label="Snooze 24h",
        style=discord.ButtonStyle.secondary,
        emoji="💤",
        custom_id="automation:inactivity:snooze24",
    )
    async def snooze_24(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        until = _set_snooze(bot, "inactivity", 24)
        await interaction.response.send_message(
            embed=success_embed(
                "Snoozed",
                f"Inactivity sweep paused until <t:{int(until.timestamp())}:f>.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} snoozed inactivity sweep for 24h.")

    @discord.ui.button(
        label="Snooze 7d",
        style=discord.ButtonStyle.secondary,
        emoji="🛌",
        custom_id="automation:inactivity:snooze7d",
    )
    async def snooze_7d(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        until = _set_snooze(bot, "inactivity", 24 * 7)
        await interaction.response.send_message(
            embed=success_embed(
                "Snoozed",
                f"Inactivity sweep paused until <t:{int(until.timestamp())}:f>.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} snoozed inactivity sweep for 7d.")

    @discord.ui.button(
        label="Dismiss",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="automation:inactivity:dismiss",
    )
    async def dismiss(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        try:
            if interaction.message:
                await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        if not interaction.response.is_done():
            await interaction.response.defer()


class PolicyDriftView(discord.ui.View):
    """Persistent view on the daily policy-drift embed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Re-snapshot all",
        style=discord.ButtonStyle.success,
        emoji="📌",
        custom_id="automation:policy:resnapshot",
    )
    async def resnapshot(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot: Bot = interaction.client  # type: ignore[assignment]
        from cogs.automation import _detect_policy_drift
        drifted = await _detect_policy_drift(bot)
        if not drifted:
            await interaction.followup.send(
                embed=info_embed(
                    "Nothing to do",
                    "No drift detected right now — snapshots already match.",
                ),
                ephemeral=True,
            )
            try:
                if interaction.message:
                    await interaction.message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        updated: list[str] = []
        skipped: list[str] = []
        for snap, _reason in drifted:
            ch = bot.get_channel(int(snap["channel_id"]))
            if not isinstance(ch, discord.TextChannel):
                skipped.append(f"<#{snap['channel_id']}> (channel gone)")
                continue
            try:
                pins = await ch.pins()
            except (discord.Forbidden, discord.HTTPException):
                skipped.append(f"{ch.mention} (cannot read pins)")
                continue
            if not pins:
                skipped.append(f"{ch.mention} (no pins)")
                continue
            msg = pins[0]
            content = msg.content or ""
            h = hashlib.sha256(content.encode("utf-8")).hexdigest()
            bot.db.upsert_policy_snapshot(
                channel_id=str(ch.id),
                channel_name=ch.name,
                message_id=str(msg.id),
                content=content,
                content_hash=h,
            )
            updated.append(ch.mention)
        lines = []
        if updated:
            lines.append("**Re-snapshotted:** " + ", ".join(updated))
        if skipped:
            lines.append("**Skipped:** " + ", ".join(skipped))
        await interaction.followup.send(
            embed=success_embed(
                "Policy snapshots updated",
                "\n".join(lines) if lines else "No changes applied.",
            ),
            ephemeral=True,
        )
        try:
            if interaction.message:
                done = info_embed(
                    "📜  Policy drift resolved",
                    f"{interaction.user.mention} re-snapshotted "
                    f"{len(updated)} channel(s).",
                )
                await interaction.message.edit(embed=done, view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
        info_log(
            f"{interaction.user} re-snapshotted {len(updated)} policy channel(s).",
        )

    @discord.ui.button(
        label="Snooze 24h",
        style=discord.ButtonStyle.secondary,
        emoji="💤",
        custom_id="automation:policy:snooze24",
    )
    async def snooze_24(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        until = _set_snooze(bot, "policy", 24)
        await interaction.response.send_message(
            embed=success_embed(
                "Snoozed",
                f"Policy drift checks paused until <t:{int(until.timestamp())}:f>.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} snoozed policy drift for 24h.")

    @discord.ui.button(
        label="Dismiss",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="automation:policy:dismiss",
    )
    async def dismiss(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        try:
            if interaction.message:
                await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        if not interaction.response.is_done():
            await interaction.response.defer()


# ── Registration cleanup task buttons ───────────────────────────────────────


class RegistrationCleanupConfirmKickView(discord.ui.View):
    """Short-lived confirmation for kicking long-unverified members."""

    def __init__(self, *, days: int) -> None:
        super().__init__(timeout=60)
        self.days = int(days)

    @discord.ui.button(
        label="Confirm kick eligible",
        style=discord.ButtonStyle.danger,
        emoji="🚪",
        custom_id="automation:registration_cleanup:kick_confirm",
    )
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot: Bot = interaction.client  # type: ignore[assignment]
        from cogs._automation_registration import _run_registration_cleanup_kicks

        embed = await _run_registration_cleanup_kicks(
            bot, interaction.guild, actor=interaction.user, days=self.days,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        emoji="✖️",
        custom_id="automation:registration_cleanup:kick_cancel",
    )
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        await interaction.response.edit_message(
            embed=info_embed("Cancelled", "No members were kicked."),
            view=None,
        )


class RegistrationCleanupView(discord.ui.View):
    """Persistent controls for an officer registration-cleanup task."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh list",
        style=discord.ButtonStyle.secondary,
        emoji="🔄",
        custom_id="automation:registration_cleanup:refresh",
    )
    async def refresh(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from the server."),
                ephemeral=True,
            )
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        from cogs._automation_registration import _build_registration_cleanup_embed

        await interaction.response.edit_message(
            embed=_build_registration_cleanup_embed(bot, interaction.guild),
            view=self,
        )

    @discord.ui.button(
        label="DM register reminder",
        style=discord.ButtonStyle.primary,
        emoji="📝",
        custom_id="automation:registration_cleanup:nudge",
    )
    async def nudge(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from the server."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot: Bot = interaction.client  # type: ignore[assignment]
        from cogs._automation_registration import _run_registration_cleanup_nudges

        embed = await _run_registration_cleanup_nudges(
            bot, interaction.guild, actor=interaction.user,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Kick eligible",
        style=discord.ButtonStyle.danger,
        emoji="🚪",
        custom_id="automation:registration_cleanup:kick",
    )
    async def kick(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Guild only", "Run this from the server."),
                ephemeral=True,
            )
            return
        bot: Bot = interaction.client  # type: ignore[assignment]
        from cogs._automation_helpers import (
            _DEFAULT_UNVERIFIED_KICK_DAYS,
            _get_int_config,
        )
        from cogs._automation_registration import _collect_registration_cleanup_targets

        days = _get_int_config(
            bot.db, "automation_unverified_kick_days",
            _DEFAULT_UNVERIFIED_KICK_DAYS,
        )
        targets = _collect_registration_cleanup_targets(
            interaction.guild, min_days=days,
        )
        if not targets:
            await interaction.response.send_message(
                embed=info_embed(
                    "Nothing eligible to kick",
                    f"No Unverified members are past the **{days}d** kick threshold.",
                ),
                ephemeral=True,
            )
            return
        lines = [f"• {m.mention} (`{m}`) — {age}d" for m, age in targets[:10]]
        if len(targets) > 10:
            lines.append(f"…and {len(targets) - 10} more.")
        embed = discord.Embed(
            title=f"Confirm kick — {len(targets)} eligible",
            description=(
                f"This will kick Unverified members at or above the **{days}d** threshold.\n\n"
                + "\n".join(lines)
            ),
            color=discord.Color.dark_red(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=RegistrationCleanupConfirmKickView(days=days),
            ephemeral=True,
        )


class UnderfillAlertView(discord.ui.View):
    """Per-event view attached to the "comp under-filled" automation alert.

    Buttons:
      • Cancel event — officer (or the event creator) can scrub the event;
                       this calls the same cancel logic used by /lfg.
      • Dismiss      — officer removes the alert without touching the event.

    Non-persistent (the alert is only meaningful in the short window before
    the event starts, and the same actions are available elsewhere).
    """

    def __init__(self, event_id: int, creator_id: str | None) -> None:
        super().__init__(timeout=60 * 60 * 6)  # 6h — well past most lead windows
        self._event_id = int(event_id)
        self._creator_id = str(creator_id) if creator_id else None

    def _can_cancel(self, interaction: discord.Interaction) -> bool:
        if _officer_check(interaction):
            return True
        return (
            self._creator_id is not None
            and str(interaction.user.id) == self._creator_id
        )

    @discord.ui.button(
        label="Cancel event",
        style=discord.ButtonStyle.danger,
        emoji="🛑",
    )
    async def cancel_event(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not self._can_cancel(interaction):
            await interaction.response.send_message(
                embed=error_embed(
                    "Not allowed",
                    "Only the event creator or an officer can cancel this event.",
                ),
                ephemeral=True,
            )
            return

        bot: Bot = interaction.client  # type: ignore[assignment]
        event = bot.db.fetch_lfg_event(self._event_id)
        if not event:
            await interaction.response.send_message(
                embed=error_embed(
                    "Event missing", "That event no longer exists.",
                ),
                ephemeral=True,
            )
            return
        if event.get("status") == "cancelled":
            await interaction.response.send_message(
                embed=info_embed(
                    "Already cancelled",
                    f"Event #{self._event_id} is already cancelled.",
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        bot.db.cancel_lfg_event(self._event_id)
        try:
            from cogs._primetime_claims import refresh_prime_claim_trackers

            await refresh_prime_claim_trackers(bot)
        except Exception as exc:  # noqa: BLE001
            error_log(f"prime claims refresh after under-fill cancel failed: {exc!r}")

        # Best-effort: also cancel the linked Discord scheduled event.
        sched_id = event.get("scheduled_event_id")
        if sched_id and interaction.guild is not None:
            try:
                sched = interaction.guild.get_scheduled_event(int(sched_id)) \
                    or await interaction.guild.fetch_scheduled_event(int(sched_id))
                if sched is not None:
                    await sched.cancel(
                        reason=f"LFG #{self._event_id} cancelled via under-fill alert",
                    )
            except (
                discord.NotFound, discord.Forbidden,
                discord.HTTPException, ValueError,
            ) as exc:
                error_log(
                    f"cancel scheduled event for LFG #{self._event_id} "
                    f"failed: {exc!r}"
                )
            bot.db.set_lfg_scheduled_event_id(self._event_id, None)

        info_log(
            f"{interaction.user} cancelled event #{self._event_id} "
            f"via under-fill alert."
        )

        await interaction.followup.send(
            embed=success_embed(
                "Event cancelled",
                f"#{self._event_id} **{event.get('title') or 'event'}** is cancelled. "
                "Signed-up members will need to be notified separately.",
            ),
            ephemeral=False,
        )
        # Disable buttons on the alert so it's clear the action is done.
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            if interaction.message:
                await interaction.message.edit(view=self)
        except (discord.Forbidden, discord.HTTPException):
            pass
        self.stop()

    @discord.ui.button(
        label="Keep — dismiss alert",
        style=discord.ButtonStyle.secondary,
        emoji="✅",
    )
    async def dismiss(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        if not _officer_check(interaction):
            await _reject_non_officer(interaction)
            return
        try:
            if interaction.message:
                await interaction.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.stop()
