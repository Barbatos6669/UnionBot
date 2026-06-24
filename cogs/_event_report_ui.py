"""Discord UI controls for post-event reports."""
from __future__ import annotations

import re
from typing import Any

import discord

from cogs._event_report_format import fmt_num
from debug import error_log, info_log
from utils import error_embed, is_officer, success_embed


LOOT_BUTTON_TEMPLATE = r"eventreport:loot:(?P<eid>[0-9]+)"


def _parse_silver_amount(raw: str | None) -> int:
    """Parse officer-friendly silver text like ``4.2m`` or ``750k``."""
    text = str(raw or "").strip().lower().replace(",", "").replace("_", "")
    if not text:
        return 0
    text = re.sub(r"\bsilver\b", "", text).strip()
    multiplier = 1
    suffixes = (
        ("million", 1_000_000),
        ("mil", 1_000_000),
        ("m", 1_000_000),
        ("thousand", 1_000),
        ("k", 1_000),
    )
    for suffix, value in suffixes:
        if text.endswith(suffix):
            multiplier = value
            text = text[: -len(suffix)].strip()
            break
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise ValueError("Use a number like `4200000`, `4.2m`, or `750k`.")
    return max(0, int(float(text) * multiplier))


def build_event_report_view(event_id: int) -> discord.ui.View:
    """Officer tools attached to event scorecards."""
    view = discord.ui.View(timeout=None)
    view.add_item(EventReportLootButton(int(event_id)))
    return view


class EventLootInputModal(discord.ui.Modal, title="Input Event Loot"):
    def __init__(self, event_id: int, existing: dict | None = None) -> None:
        super().__init__(timeout=300)
        self.event_id = int(event_id)
        existing = existing or {}
        self.gross_loot = discord.ui.TextInput(
            label="Total loot value",
            placeholder="e.g. 4.2m or 4200000",
            required=True,
            max_length=32,
            default=str(existing.get("gross_loot") or ""),
        )
        self.guild_cut = discord.ui.TextInput(
            label="Guild cut / reserve",
            placeholder="optional, e.g. 500k",
            required=False,
            max_length=32,
            default=str(existing.get("guild_cut") or ""),
        )
        self.notes = discord.ui.TextInput(
            label="Notes",
            placeholder="optional: loot sold, still holding items, tax reason, etc.",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=700,
            default=str(existing.get("notes") or ""),
        )
        self.add_item(self.gross_loot)
        self.add_item(self.guild_cut)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Only staff can update event loot analytics."),
                ephemeral=True,
            )
            return

        try:
            gross = _parse_silver_amount(str(self.gross_loot.value))
            guild_cut = _parse_silver_amount(str(self.guild_cut.value))
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed("Bad silver value", str(exc)),
                ephemeral=True,
            )
            return
        if gross <= 0:
            await interaction.response.send_message(
                embed=error_embed("Loot required", "Enter the total loot value brought home from the event."),
                ephemeral=True,
            )
            return
        if guild_cut > gross:
            await interaction.response.send_message(
                embed=error_embed("Cut too high", "Guild cut/reserve cannot be higher than total loot."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        db = getattr(bot, "db", None)
        if db is None:
            await interaction.followup.send(
                embed=error_embed("Bot DB unavailable", "I could not save that loot summary."),
                ephemeral=True,
            )
            return
        event = db.fetch_lfg_event(self.event_id)
        if not event:
            await interaction.followup.send(
                embed=error_embed("Event not found", f"No LFG event with id `{self.event_id}`."),
                ephemeral=True,
            )
            return

        db.upsert_event_loot_summary(
            self.event_id,
            gross_loot=gross,
            guild_cut=guild_cut,
            notes=str(self.notes.value or "").strip() or None,
            updated_by=str(interaction.user.id),
        )
        info_log(
            f"{interaction.user} updated event loot summary #{self.event_id}: "
            f"gross={gross} guild_cut={guild_cut}."
        )

        channel = interaction.channel
        if not hasattr(channel, "send"):
            await interaction.followup.send(
                embed=success_embed(
                    "Loot saved",
                    "Saved the loot summary, but I could not repost the scorecard in this channel.",
                ),
                ephemeral=True,
            )
            return

        try:
            from cogs._event_reports import batch_embeds_for_send, build_event_report_embed

            graph_files: list[discord.File] = []
            extra_embeds: list[discord.Embed] = []
            embed = await build_event_report_embed(
                bot,
                event,
                threshold_pct=int(db.get_config("automation_voice_attendance_min_pct") or "50"),
                fetch_killboard=True,
                include_graph=True,
                graph_files=graph_files,
                extra_embeds=extra_embeds,
            )
            report_embeds = [embed, *extra_embeds]
            for idx, embed_batch in enumerate(batch_embeds_for_send(report_embeds)):
                kwargs: dict[str, Any] = {
                    "embeds": embed_batch,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if idx == 0:
                    kwargs["view"] = build_event_report_view(self.event_id)
                    if graph_files:
                        kwargs["file"] = graph_files[0]
                await channel.send(**kwargs)
        except Exception as exc:  # noqa: BLE001
            error_log(f"event loot scorecard repost failed for #{self.event_id}: {exc!r}")
            await interaction.followup.send(
                embed=error_embed(
                    "Loot saved, report failed",
                    "The loot value was saved, but I could not repost the scorecard. Check the bot logs.",
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=success_embed(
                "Loot saved",
                (
                    f"Saved **{fmt_num(gross)}** loot for event **#{self.event_id}** "
                    f"with **{fmt_num(guild_cut)}** guild cut/reserve. "
                    "I posted an updated scorecard below."
                ),
            ),
            ephemeral=True,
        )


class EventReportLootButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=LOOT_BUTTON_TEMPLATE,
):
    def __init__(self, event_id: int) -> None:
        self.event_id = int(event_id)
        super().__init__(
            discord.ui.Button(
                label="Input Event Loot",
                style=discord.ButtonStyle.success,
                custom_id=f"eventreport:loot:{self.event_id}",
                emoji="💰",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "EventReportLootButton":
        return cls(int(match["eid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "Only staff can update event loot analytics."),
                ephemeral=True,
            )
            return
        db = getattr(interaction.client, "db", None)
        event = db.fetch_lfg_event(self.event_id) if db is not None else None
        if not event:
            await interaction.response.send_message(
                embed=error_embed("Event not found", f"No LFG event with id `{self.event_id}`."),
                ephemeral=True,
            )
            return
        existing = db.fetch_event_loot_summary(self.event_id)
        await interaction.response.send_modal(EventLootInputModal(self.event_id, existing))


def register_persistent_event_report_views(bot) -> None:
    """Wake up event-report DynamicItem buttons after bot restart."""
    bot.add_dynamic_items(EventReportLootButton)
