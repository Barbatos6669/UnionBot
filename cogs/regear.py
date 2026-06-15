"""Regear request system.

Members open the regear board's "Submit Regear Request" button → modal →
they're prompted to post a screenshot of their death recap in the channel
(same UX as the registration flow). The screenshot, fight context, and
gear value land in the officer review channel with persistent
Approve / Deny buttons (DynamicItem, survives bot restarts).

On approval the requester is DM-ed and (optionally) charged
``points_regear_cost`` points. On denial the officer is prompted for a
public-to-applicant reason via a modal.

DB: see ``regear_requests`` in sql_database.initialize_automation_tables.
Config keys:
    regear_board_channel_id      — channel where the Submit button lives
    regear_review_channel_id     — channel where staff approve/deny
"""
from __future__ import annotations

from cogs._typing import Bot
import asyncio
import datetime as _dt
import json
import re

import discord
from discord import app_commands
from discord.ext import commands

import albion_api
from debug import info_log, error_log
from utils import error_embed, info_embed, success_embed


SUBMIT_CUSTOM_ID    = "regear:submit"
FROM_DEATH_CUSTOM_ID = "regear:from_death"
APPROVE_TEMPLATE    = r"regear:approve:(?P<rid>[0-9]+)"
DENY_TEMPLATE       = r"regear:deny:(?P<rid>[0-9]+)"

CONTENT_CHOICES     = ("ZvZ", "Ganking", "Mists", "Hellgate", "HCE", "Avalon", "Other")

# Tracks discord_ids currently in the regear submission flow (screenshot pending).
_pending_regears: set[str] = set()


# ── Permission helper ────────────────────────────────────────────────────────

def _is_reviewer(member: discord.Member | discord.User) -> bool:
    """Reviewers are anyone with manage_guild or any STAFF_ROLES role."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    from config import STAFF_ROLES
    role_names = {r.name for r in member.roles}
    return any(r in role_names for r in STAFF_ROLES)


# ── Modals ───────────────────────────────────────────────────────────────────

class RegearSubmitModal(discord.ui.Modal, title="Submit a regear request"):
    """Members fill this out after clicking the board's Submit button.
    The actual screenshot is collected via wait_for() after the modal closes."""

    content_type = discord.ui.TextInput(
        label="Content type",
        placeholder=", ".join(CONTENT_CHOICES),
        max_length=24,
        required=True,
    )
    gear_value = discord.ui.TextInput(
        label="Estimated gear value (silver)",
        placeholder="e.g. 1500000",
        max_length=14,
        required=True,
    )
    notes = discord.ui.TextInput(
        label="What happened? (shotcaller, fight context)",
        style=discord.TextStyle.paragraph,
        max_length=400,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        db = bot.db
        discord_id = str(interaction.user.id)

        # Validate gear value.
        try:
            silver = int(re.sub(r"[^0-9]", "", str(self.gear_value.value)))
        except (TypeError, ValueError):
            silver = 0
        if silver <= 0:
            await interaction.followup.send(
                embed=error_embed(
                    "Bad value", "Gear value should be a positive number."
                ),
                ephemeral=True,
            )
            return
        # Sanity cap — top-tier full set is ~50M silver, so anything above
        # 100M is almost certainly a typo or bad-faith submission.
        REGEAR_MAX_SILVER = 100_000_000
        if silver > REGEAR_MAX_SILVER:
            await interaction.followup.send(
                embed=error_embed(
                    "Value too high",
                    f"Gear value {silver:,} exceeds the {REGEAR_MAX_SILVER:,} silver "
                    "cap. Submit a screenshot of just the gear actually lost.",
                ),
                ephemeral=True,
            )
            return

        if discord_id in _pending_regears:
            await interaction.followup.send(
                embed=error_embed(
                    "In progress",
                    "You already have a regear submission waiting for a "
                    "screenshot. Post the image, or wait for that one to time out.",
                ),
                ephemeral=True,
            )
            return

        # Verify review channel is set up *before* asking for a screenshot.
        review_channel_id = db.get_config("regear_review_channel_id")
        if not review_channel_id:
            await interaction.followup.send(
                embed=error_embed(
                    "Not configured",
                    "An officer must run `/regear set-review-channel` before "
                    "regear requests can be processed.",
                ),
                ephemeral=True,
            )
            return
        review_channel = bot.get_channel(int(review_channel_id))
        if review_channel is None:
            await interaction.followup.send(
                embed=error_embed(
                    "Misconfigured",
                    "Regear review channel is set but I can't find it.",
                ),
                ephemeral=True,
            )
            return

        ctype = str(self.content_type.value).strip()
        notes_val = str(self.notes.value).strip()

        # Prompt for the screenshot.
        await interaction.followup.send(
            embed=info_embed(
                "📸 Post your death recap",
                "Now drop a screenshot of your death recap **in this channel**.\n"
                "You have 5 minutes — the message will be deleted after we grab it.",
            ),
            ephemeral=True,
        )

        _pending_regears.add(discord_id)
        try:
            try:
                message = await bot.wait_for(
                    "message",
                    check=lambda m: (
                        m.author.id == interaction.user.id
                        and m.channel.id == interaction.channel.id
                        and m.attachments
                        and (m.attachments[0].content_type or "").startswith("image/")
                    ),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                await interaction.followup.send(
                    embed=error_embed(
                        "Timed out",
                        "No screenshot received in 5 minutes. Click Submit again to retry.",
                    ),
                    ephemeral=True,
                )
                return

            attachment = message.attachments[0]
            image_url = attachment.url
            try:
                image_file = await attachment.to_file(filename="death_recap.png")
            except (discord.HTTPException, discord.NotFound):
                image_file = None
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

            request_id = db.create_regear_request(
                discord_id=discord_id,
                event_id=None,
                content_type=ctype,
                gear_value=silver,
                image_url=image_url,
                notes=notes_val,
            )
            if not request_id:
                await interaction.followup.send(
                    embed=error_embed("DB error", "Couldn't save your request."),
                    ephemeral=True,
                )
                return

            embed = _build_review_embed(
                request_id, interaction.user, ctype, silver, notes_val,
                image_url=image_url,
                use_attachment=image_file is not None,
            )
            view = build_review_view(request_id)
            try:
                if image_file is not None:
                    msg = await review_channel.send(
                        embed=embed, view=view, file=image_file,
                    )
                else:
                    msg = await review_channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException) as exc:
                error_log(
                    f"regear review post failed for #{request_id} "
                    f"in {review_channel}: {exc!r}"
                )
                await interaction.followup.send(
                    embed=error_embed(
                        "Couldn't post review",
                        f"I couldn't post the review card to {review_channel.mention}. "
                        "Check that I have View Channel, Send Messages and "
                        "Embed Links there.",
                    ),
                    ephemeral=True,
                )
                return

            db.set_regear_review_message(request_id, str(review_channel.id), str(msg.id))
            await interaction.followup.send(
                embed=success_embed(
                    "Submitted",
                    f"Regear request **#{request_id}** filed. "
                    "You'll be DM-ed when an officer reviews it.",
                ),
                ephemeral=True,
            )
            info_log(
                f"Regear request #{request_id} filed by {interaction.user} "
                f"({ctype}, {silver:,} silver)."
            )
        finally:
            _pending_regears.discard(discord_id)


# ── Death-event auto-fill flow ──────────────────────────────────────────────

def _build_death_notes(summary: dict) -> str:
    """Format the auto-filled notes for a death-sourced regear request."""
    killer = summary["killer_name"]
    if summary["killer_guild"]:
        killer = f"{killer} [{summary['killer_guild']}]"
    lines = [
        f"**Killed by:** {killer}",
        f"**Victim IP:** {int(summary['victim_ip'])}"
        + (f" \u2022 **Killer IP:** {int(summary['killer_ip'])}" if summary['killer_ip'] else ""),
    ]
    if summary["fame"]:
        lines.append(f"**Fame lost:** {summary['fame']:,}")
    if summary["participant_count"]:
        lines.append(
            f"**Fight size:** {summary['participant_count']} participant(s), "
            f"victim party {summary['group_size']}"
        )
    if summary["killboard_url"]:
        lines.append(f"**Killboard:** {summary['killboard_url']}")
    if summary["gear_lines"]:
        lines.append("")
        lines.append("**Gear lost:**")
        lines.extend(summary["gear_lines"])
    est = int(summary.get("estimated_value") or 0)
    if est > 0:
        missing = summary.get("estimated_missing_slots") or []
        suffix = f" ({len(missing)} slot(s) had no AODP data)" if missing else ""
        lines.append("")
        lines.append(f"**AODP estimate:** ~{est:,} silver{suffix}")
    return "\n".join(lines)[:1500]


async def _start_from_death_flow(interaction: discord.Interaction) -> None:
    """Look up the user's last 5 deaths and show a picker. Bound to the
    'From Recent Death' board button."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    bot = interaction.client
    db = bot.db
    discord_id = str(interaction.user.id)

    profile = db.fetch_user_profile(discord_id) or {}
    player_id = profile.get("albion_player_id")
    if not player_id:
        await interaction.followup.send(
            embed=error_embed(
                "Not registered",
                "Register your Albion character first so the bot can look up "
                "your deaths from the Albion API.",
                hint="Use the registration button in the welcome channel.",
            ),
            ephemeral=True,
        )
        return

    # Verify review channel is set up *before* burning an API call.
    if not db.get_config("regear_review_channel_id"):
        await interaction.followup.send(
            embed=error_embed(
                "Not configured",
                "An officer must run `/regear set-review-channel` before "
                "regear requests can be processed.",
            ),
            ephemeral=True,
        )
        return

    try:
        deaths = await asyncio.to_thread(albion_api.get_player_deaths, str(player_id), 5)
    except Exception as exc:  # noqa: BLE001
        error_log(f"regear: get_player_deaths failed for {player_id}: {exc!r}")
        deaths = []

    if not deaths:
        await interaction.followup.send(
            embed=error_embed(
                "No recent deaths",
                "The Albion API doesn't show any recent deaths on your character. "
                "If you died very recently, the API can take a few minutes to "
                "update \u2014 try again shortly, or use the regular **Submit Regear "
                "Request** button with a screenshot.",
            ),
            ephemeral=True,
        )
        return

    summaries = [albion_api.format_death_event(d) for d in deaths]

    # Pre-compute AODP price estimates so the modal can default the gear
    # value field. One pooled AODP call covers every item across all five
    # deaths — the estimator handles missing data gracefully.
    try:
        import market_api as _market_api
        all_items: list[dict] = []
        for s in summaries:
            all_items.extend(s.get("gear_items") or [])
        if all_items:
            pooled = await asyncio.to_thread(_market_api.estimate_gear_value, all_items)
            # Index price lookups by (item_id, quality) → unit_price.
            unit_prices: dict[tuple[str, int], int] = {}
            for row in pooled.get("per_item") or []:
                if row.get("missing"):
                    continue
                unit_prices[(row["item_id"], int(row["quality"]))] = int(row["unit_price"])
            # Split the pooled prices back into per-summary totals.
            for s in summaries:
                total = 0
                priced = 0
                missing_slots: list[str] = []
                for it in s.get("gear_items") or []:
                    key = (it["item_id"], int(it.get("quality") or 1))
                    up = unit_prices.get(key, 0)
                    if up > 0:
                        total += up * int(it.get("count") or 1)
                        priced += 1
                    else:
                        missing_slots.append(it.get("slot") or "?")
                s["estimated_value"] = int(total)
                s["estimated_priced_count"] = priced
                s["estimated_missing_slots"] = missing_slots
    except Exception as exc:  # noqa: BLE001
        error_log(f"regear: gear value estimate failed: {exc!r}")

    view = DeathPickerView(summaries)
    await interaction.followup.send(
        embed=info_embed(
            "Pick the death to regear",
            "Select the fight you want to submit. The form will auto-fill "
            "with gear, IP, and a killboard link \u2014 no screenshot needed.",
        ),
        view=view,
        ephemeral=True,
    )


def _short_death_label(s: dict) -> str:
    """Build a one-line option label (max 100 chars per Discord limit)."""
    killer = s["killer_name"] or "Unknown"
    ip = int(s["victim_ip"]) if s["victim_ip"] else 0
    fame_short = ""
    fame = s["fame"]
    if fame >= 1_000_000:
        fame_short = f"\u2022 {fame / 1_000_000:.1f}M fame"
    elif fame >= 1000:
        fame_short = f"\u2022 {fame // 1000}K fame"
    label = f"\ud83d\udc80 killed by {killer} \u2022 {ip} IP {fame_short}".strip()
    return label[:100]


def _short_death_description(s: dict) -> str:
    """Build the option's secondary description (max 100 chars)."""
    bits = []
    if s["killer_guild"]:
        bits.append(f"[{s['killer_guild']}]")
    if s["guessed_content_type"]:
        bits.append(s["guessed_content_type"])
    if s["timestamp"]:
        # Convert ISO → "Xm/h/d ago" for at-a-glance recency.
        try:
            iso = s["timestamp"].replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(iso)
            age = _dt.datetime.now(tz=dt.tzinfo) - dt
            mins = int(age.total_seconds() // 60)
            if mins < 60:
                bits.append(f"{mins}m ago")
            elif mins < 60 * 48:
                bits.append(f"{mins // 60}h ago")
            else:
                bits.append(f"{mins // 1440}d ago")
        except (ValueError, TypeError):
            pass
    est = int(s.get("estimated_value") or 0)
    if est > 0:
        bits.append(f"~{est // 1000:,}k silver")
    desc = " \u2022 ".join(bits) if bits else "Death event"
    return desc[:100]


class DeathPickerView(discord.ui.View):
    """Ephemeral select listing the user's last 5 deaths. On pick, opens
    the auto-filled regear modal."""

    def __init__(self, summaries: list[dict]) -> None:
        super().__init__(timeout=180)
        # Stash by event_id so the select callback can look them back up.
        self._by_event: dict[int, dict] = {s["event_id"]: s for s in summaries}
        options = [
            discord.SelectOption(
                label=_short_death_label(s),
                description=_short_death_description(s),
                value=str(s["event_id"]),
            )
            for s in summaries
        ]
        select = discord.ui.Select(
            placeholder="Choose a death\u2026",
            min_values=1, max_values=1,
            options=options,
        )
        select.callback = self._on_pick  # type: ignore[assignment]
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        select = interaction.data.get("values") if interaction.data else None
        if not select:
            return
        try:
            event_id = int(select[0])
        except (TypeError, ValueError):
            return
        summary = self._by_event.get(event_id)
        if not summary:
            await interaction.response.send_message(
                embed=error_embed("Picker expired", "Re-open the picker and try again."),
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(RegearFromDeathModal(summary))
        except Exception as exc:  # noqa: BLE001
            error_log(f"regear: from-death modal failed: {exc!r}")


class RegearFromDeathModal(discord.ui.Modal, title="Regear from death event"):
    """Auto-filled regear modal. Skips the screenshot step \u2014 the killboard URL
    is used as evidence instead."""

    def __init__(self, summary: dict) -> None:
        super().__init__(timeout=None)
        self._summary = summary
        self.content_type = discord.ui.TextInput(
            label="Content type",
            placeholder=", ".join(CONTENT_CHOICES),
            default=summary.get("guessed_content_type") or "",
            max_length=24,
            required=True,
        )
        self.gear_value = discord.ui.TextInput(
            label="Estimated gear value (silver)",
            placeholder="e.g. 1500000",
            default=str(summary["estimated_value"]) if summary.get("estimated_value") else "",
            max_length=14,
            required=True,
        )
        self.notes = discord.ui.TextInput(
            label="Extra notes (shotcaller, fight context)",
            style=discord.TextStyle.paragraph,
            max_length=400,
            required=False,
        )
        self.add_item(self.content_type)
        self.add_item(self.gear_value)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = interaction.client
        db = bot.db
        discord_id = str(interaction.user.id)

        try:
            silver = int(re.sub(r"[^0-9]", "", str(self.gear_value.value)))
        except (TypeError, ValueError):
            silver = 0
        if silver <= 0:
            await interaction.followup.send(
                embed=error_embed("Bad value", "Gear value should be a positive number."),
                ephemeral=True,
            )
            return
        REGEAR_MAX_SILVER = 100_000_000
        if silver > REGEAR_MAX_SILVER:
            await interaction.followup.send(
                embed=error_embed(
                    "Value too high",
                    f"Gear value {silver:,} exceeds the {REGEAR_MAX_SILVER:,} silver cap.",
                ),
                ephemeral=True,
            )
            return

        review_channel_id = db.get_config("regear_review_channel_id")
        review_channel = bot.get_channel(int(review_channel_id)) if review_channel_id else None
        if review_channel is None:
            await interaction.followup.send(
                embed=error_embed("Misconfigured", "Regear review channel is set but I can't find it."),
                ephemeral=True,
            )
            return

        ctype = str(self.content_type.value).strip()
        extra_notes = str(self.notes.value or "").strip()
        full_notes = _build_death_notes(self._summary)
        if extra_notes:
            full_notes = f"{full_notes}\n\n**Notes:** {extra_notes}"
        full_notes = full_notes[:1500]

        request_id = db.create_regear_request(
            discord_id=discord_id,
            event_id=int(self._summary["event_id"]) or None,
            content_type=ctype,
            gear_value=silver,
            image_url=self._summary.get("killboard_url") or None,
            notes=full_notes,
            gear_items_json=json.dumps(self._summary.get("gear_items") or []),
        )
        if not request_id:
            await interaction.followup.send(
                embed=error_embed("DB error", "Couldn't save your request."),
                ephemeral=True,
            )
            return

        embed = _build_review_embed(
            request_id, interaction.user, ctype, silver, full_notes,
            image_url=self._summary.get("killboard_url") or "",
            use_attachment=False,
        )
        # Chest pre-check — show the officer at a glance whether the
        # quartermaster will need to restock anything after approval.
        try:
            gear_items = self._summary.get("gear_items") or []
            if gear_items:
                ready, short = _chest_precheck(db, gear_items)
                if not short:
                    embed.add_field(
                        name="📦 Chest pre-check",
                        value=(
                            f"✅ **Ready to ship** — all {ready} item(s) "
                            f"in stock."
                        ),
                        inline=False,
                    )
                else:
                    short_lines = "\n".join(
                        f"• `{iid}` — need **{need}**, have **{have}** "
                        f"(short {need - have})"
                        for iid, need, have in short[:8]
                    )
                    embed.add_field(
                        name="📦 Chest pre-check",
                        value=(
                            f"⚠️ **{len(short)} item(s) short** "
                            f"({ready} in stock):\n{short_lines}"
                        ),
                        inline=False,
                    )
        except Exception as exc:  # noqa: BLE001
            error_log(f"chest pre-check failed for regear #{request_id}: {exc!r}")
        view = build_review_view(request_id)
        try:
            msg = await review_channel.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(
                f"regear review post failed for #{request_id} "
                f"in {review_channel}: {exc!r}"
            )
            await interaction.followup.send(
                embed=error_embed(
                    "Couldn't post review",
                    f"I couldn't post the review card to {review_channel.mention}. "
                    "Check that I have View Channel, Send Messages and "
                    "Embed Links there.",
                ),
                ephemeral=True,
            )
            return

        db.set_regear_review_message(request_id, str(review_channel.id), str(msg.id))
        await interaction.followup.send(
            embed=success_embed(
                "Submitted",
                f"Regear request **#{request_id}** filed from killboard event "
                f"`{self._summary['event_id']}`. You'll be DM-ed when reviewed.",
            ),
            ephemeral=True,
        )
        info_log(
            f"Regear request #{request_id} filed (from-death) by {interaction.user} "
            f"event={self._summary['event_id']} ({ctype}, {silver:,} silver)."
        )


class RegearDenyModal(discord.ui.Modal, title="Deny regear request"):
    def __init__(self, request_id: int) -> None:
        super().__init__(timeout=None)
        self.request_id = request_id
        self.reason = discord.ui.TextInput(
            label="Reason (shown to applicant)",
            placeholder="Be specific — they'll see this verbatim.",
            style=discord.TextStyle.paragraph,
            min_length=5,
            max_length=400,
            required=True,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await _do_resolve(
            interaction.client, interaction.user, self.request_id,
            approved=False, reason=str(self.reason.value),
        )
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            try:
                await interaction.message.edit(view=_resolved_view("denied"))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


# ── Buttons ─────────────────────────────────────────────────────────────────

class RegearSubmitView(discord.ui.View):
    """Persistent view for the regear board's static Submit buttons."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Submit Regear Request",
        style=discord.ButtonStyle.primary,
        custom_id=SUBMIT_CUSTOM_ID,
        emoji="🛡️",
    )
    async def submit_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        info_log(f"Regear submit button clicked by {interaction.user}.")
        try:
            await interaction.response.send_modal(RegearSubmitModal())
        except Exception as exc:  # noqa: BLE001
            error_log(f"Regear submit modal failed: {exc!r}")
            try:
                await interaction.response.send_message(
                    embed=error_embed("Submit failed", f"`{exc!r}`"),
                    ephemeral=True,
                )
            except Exception:  # noqa: BLE001
                pass

    @discord.ui.button(
        label="From Recent Death (auto-fill)",
        style=discord.ButtonStyle.secondary,
        custom_id=FROM_DEATH_CUSTOM_ID,
        emoji="💀",
    )
    async def from_death_button(
        self, interaction: discord.Interaction, button: discord.ui.Button,
    ) -> None:
        info_log(f"Regear from-death button clicked by {interaction.user}.")
        await _start_from_death_flow(interaction)

    async def on_error(
        self, interaction: discord.Interaction,
        error: Exception, item: discord.ui.Item,
    ) -> None:
        error_log(f"RegearSubmitView error on {item}: {error!r}")


class RegearApproveButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=APPROVE_TEMPLATE,
):
    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"regear:approve:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "RegearApproveButton":
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only staff can approve regear requests."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await _do_resolve(
            interaction.client, interaction.user, self.request_id,
            approved=True, reason=None,
        )
        await interaction.followup.send(
            embed=(success_embed("Regear approved", msg) if ok else error_embed("Could not approve", msg)),
            ephemeral=True,
        )
        if ok:
            try:
                await interaction.message.edit(view=_resolved_view("approved"))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class RegearDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=DENY_TEMPLATE,
):
    def __init__(self, request_id: int) -> None:
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=f"regear:deny:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button,
        match: re.Match[str], /,
    ) -> "RegearDenyButton":
        return cls(int(match["rid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_reviewer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Permission denied", "Only staff can deny regear requests."),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RegearDenyModal(self.request_id))


def build_submit_view() -> discord.ui.View:
    """Persistent view for the public regear board — just the Submit button."""
    return RegearSubmitView()


def build_review_view(request_id: int) -> discord.ui.View:
    """Approve/Deny view attached to a fresh review post."""
    view = discord.ui.View(timeout=None)
    view.add_item(RegearApproveButton(request_id))
    view.add_item(RegearDenyButton(request_id))
    return view


def _resolved_view(status: str) -> discord.ui.View:
    """Replace the Approve/Deny pair with one disabled label."""
    view = discord.ui.View(timeout=None)
    label = "Approved ✅" if status == "approved" else "Denied ❌"
    view.add_item(
        discord.ui.Button(
            label=label, style=discord.ButtonStyle.secondary, disabled=True,
            custom_id=f"regear:resolved:{status}",
        )
    )
    return view


# ── Embeds & resolution ─────────────────────────────────────────────────────

def _split_enchant(item_id: str) -> tuple[str, int]:
    """Split an Albion item type string into ``(base_id, enchant_level)``.

    Albion's killboard reports enchanted items as ``T7_HEAD_PLATE_SET2@3``;
    the loadout chest stores stock keyed on the base id plus a separate
    ``enchant`` column. This helper bridges the two formats.
    """
    raw = str(item_id or "").strip()
    if "@" in raw:
        base, _, suffix = raw.partition("@")
        try:
            return base, int(suffix)
        except ValueError:
            return base, 0
    return raw, 0


def _chest_precheck(db, gear_items: list[dict]) -> tuple[int, list[tuple[str, int, int]]]:
    """Inspect chest stock for a parsed gear list.

    Returns ``(ready_count, shortfalls)`` where ``shortfalls`` is a list of
    ``(item_id, needed, on_hand)`` for items whose on-hand count is less
    than what the regear would consume. ``ready_count`` is the number of
    items that *are* in stock at the requested quantity. Quality and
    enchant are taken from the parsed item dict (defaulting to 1/0); the
    enchant level may also be embedded in the item id as ``...@N``.
    """
    ready = 0
    short: list[tuple[str, int, int]] = []
    for it in gear_items or []:
        raw_id = (it or {}).get("item_id")
        if not raw_id:
            continue
        base_id, suffix_ench = _split_enchant(str(raw_id))
        qty = int(it.get("count") or 1)
        quality = int(it.get("quality") or 1)
        enchant = int(it.get("enchant") or suffix_ench)
        on_hand = int(
            db.chest_get(base_id, quality=quality, enchant=enchant) or 0
        )
        if on_hand >= qty:
            ready += 1
        else:
            short.append((base_id, qty, on_hand))
    return ready, short


def _build_review_embed(
    request_id: int, applicant: discord.User | discord.Member,
    content_type: str, gear_value: int, notes: str,
    *, image_url: str, use_attachment: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Regear Request #{request_id}",
        description=notes[:1500],
        color=discord.Color.gold(),
    )
    embed.set_author(
        name=str(applicant),
        icon_url=applicant.display_avatar.url if hasattr(applicant, "display_avatar") else None,
    )
    embed.add_field(name="Content", value=content_type or "—", inline=True)
    embed.add_field(name="Gear value", value=f"{gear_value:,}", inline=True)
    embed.add_field(name="Applicant", value=applicant.mention, inline=True)
    is_killboard_link = bool(image_url) and "albiononline.com/en/killboard" in image_url
    if use_attachment:
        embed.set_image(url="attachment://death_recap.png")
    elif is_killboard_link:
        embed.add_field(
            name="Killboard",
            value=f"[View death event]({image_url})",
            inline=False,
        )
    elif image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text="Approve / Deny below — applicant will be DM-ed.")
    return embed


async def _do_resolve(
    bot: Bot, reviewer: discord.User | discord.Member,
    request_id: int, *, approved: bool, reason: str | None,
) -> tuple[bool, str]:
    """Shared body for approve + deny. Updates DB, DMs applicant, optionally
    deducts points. Returns (ok, message_for_reviewer)."""
    db = bot.db
    request = db.fetch_regear_request(request_id)
    if not request:
        return False, "Request not found."
    if request.get("status") != "pending":
        return False, f"Already resolved as **{request['status']}**."

    status = "approved" if approved else "denied"
    db.resolve_regear_request(
        request_id, status=status, decided_by=str(reviewer.id),
        decision_notes=reason,
    )

    # Optional points cost on approval.
    if approved:
        try:
            from cogs.points import get_point_setting
            cost = get_point_setting(db, "points_regear_cost")
            if cost > 0:
                db.add_points(request["discord_id"], -cost)
                info_log(
                    f"Deducted {cost} regear point(s) from "
                    f"{request['discord_id']} (request #{request_id})."
                )
        except Exception as exc:  # noqa: BLE001
            error_log(f"regear: point deduction failed: {exc!r}")

        # Credit the silver ledger so the guild's debt to the applicant is
        # tracked. Officers run /audit settle in-game to clear it.
        gear_value = int(request.get("gear_value") or 0)
        if gear_value > 0:
            try:
                db.adjust_silver_balance(
                    request["discord_id"], gear_value,
                    reason=f"Regear #{request_id} — {request.get('content_type') or 'regear'}",
                    ref_type="regear", ref_id=str(request_id),
                    actor_id=str(reviewer.id),
                )
            except Exception as exc:  # noqa: BLE001
                error_log(f"regear: silver credit failed for #{request_id}: {exc!r}")

        # Decrement loadout-chest stock for items captured at submit time
        # (death-flow path). Each item drops by `count` (usually 1).
        # Floors at 0 if stock is short — surfacing the shortfall is the
        # quartermaster's problem, not the approval flow's.
        items_json = request.get("gear_items_json")
        low_alerts: list[tuple[str, int]] = []
        if items_json:
            try:
                items = json.loads(items_json)
                decremented = 0
                threshold_raw = db.get_config("chest_low_stock_threshold")
                try:
                    threshold = int(threshold_raw) if threshold_raw else 3
                except (TypeError, ValueError):
                    threshold = 3
                for it in items or []:
                    raw_id = (it or {}).get("item_id")
                    if not raw_id:
                        continue
                    base_id, suffix_ench = _split_enchant(str(raw_id))
                    qty = int(it.get("count") or 1)
                    quality = int(it.get("quality") or 1)
                    enchant = int(it.get("enchant") or suffix_ench)
                    new_count = db.chest_adjust(
                        base_id, -qty, quality=quality, enchant=enchant,
                        reason=f"regear #{request_id} approved",
                        actor_id=str(reviewer.id),
                        ref_type="regear", ref_id=str(request_id),
                    )
                    decremented += 1
                    if 0 <= new_count <= threshold:
                        low_alerts.append((base_id, int(new_count)))
                if decremented:
                    info_log(
                        f"regear #{request_id}: decremented {decremented} "
                        f"chest entries on approval."
                    )
            except Exception as exc:  # noqa: BLE001
                error_log(f"regear: chest decrement failed for #{request_id}: {exc!r}")

        # Post a low-stock alert to the officer channel if any decremented
        # item dropped at or below the configured threshold.
        if low_alerts:
            try:
                alert_chan_id = (
                    db.get_config("chest_alert_channel_id")
                    or db.get_config("officer_channel_id")
                )
                if alert_chan_id:
                    chan = bot.get_channel(int(alert_chan_id))
                    if chan is None:
                        try:
                            chan = await bot.fetch_channel(int(alert_chan_id))
                        except (discord.NotFound, discord.Forbidden):
                            chan = None
                    if isinstance(chan, discord.TextChannel):
                        lines = "\n".join(
                            f"- `{iid}` \u2014 **{cnt}** left"
                            for iid, cnt in low_alerts
                        )
                        embed = info_embed(
                            "Chest low-stock alert",
                            f"After regear **#{request_id}** the following "
                            f"items are at or below the alert threshold:\n\n"
                            f"{lines}\n\n"
                            f"Run `/chest stock` for the full picture, then "
                            f"`/chest add` to restock.",
                        )
                        await chan.send(embed=embed)
            except Exception as exc:  # noqa: BLE001
                error_log(
                    f"regear: low-stock alert post failed for #{request_id}: {exc!r}"
                )

    # DM the applicant.
    try:
        user = await bot.fetch_user(int(request["discord_id"]))
        if approved:
            embed = success_embed(
                f"Regear #{request_id} approved",
                f"Your **{request.get('content_type') or 'regear'}** request "
                f"({int(request.get('gear_value') or 0):,} silver) was "
                f"approved by {reviewer.mention}.",
            )
        else:
            embed = error_embed(
                f"Regear #{request_id} denied",
                f"Reviewed by {reviewer.mention}.\n\n**Reason:** {reason}",
            )
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"regear: DM to {request['discord_id']} failed: {exc!r}")

    return True, f"Request **#{request_id}** marked **{status}**."


# ── Persistent view registration ────────────────────────────────────────────

def register_persistent_regear_views(bot: Bot) -> None:
    """Called on cog load so the Submit view + DynamicItem routes wake up."""
    bot.add_view(RegearSubmitView())
    bot.add_dynamic_items(RegearApproveButton, RegearDenyButton)


# ── Cog ─────────────────────────────────────────────────────────────────────

class RegearGroup(app_commands.Group, name="regear", description="Regear requests."):

    def __init__(self, bot: Bot) -> None:
        super().__init__()
        self.bot: Bot = bot

    @app_commands.command(
        name="post-board",
        description="Post the regear submission board with a Submit button.",
    )
    @app_commands.describe(channel="Channel to post the board in (defaults to here).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def post_board(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        dest = channel or interaction.channel
        embed = discord.Embed(
            title="🛡️ Regear Requests",
            description=(
                "**Lost gear in a guild fight?** Submit a regear request below.\n\n"
                "**Two ways to submit:**\n"
                "🛡️ **Submit Regear Request** — fill out the form and drop a "
                "screenshot of your death recap in this channel.\n"
                "💀 **From Recent Death (auto-fill)** — pulls your last 5 deaths "
                "from the Albion API. Pick the fight and the form is pre-filled "
                "with your gear, IP, and a killboard link. No screenshot needed.\n\n"
                "Either way you'll enter the **content type** (ZvZ / Ganking / "
                "Mists / etc.) and your **estimated gear value** in silver.\n\n"
                "Read **regear-policy** before submitting. Officers will DM "
                "you with the decision."
            ),
            color=discord.Color.gold(),
        )
        try:
            await dest.send(embed=embed, view=build_submit_view())
        except (discord.Forbidden, discord.HTTPException) as exc:
            error_log(f"regear board post failed in {dest}: {exc!r}")
            await interaction.response.send_message(
                embed=error_embed(
                    "Couldn't post board",
                    f"I couldn't post the regear submission board to "
                    f"{dest.mention}. Check that I have View Channel, "
                    "Send Messages and Embed Links there.",
                ),
                ephemeral=True,
            )
            return
        self.bot.db.set_config("regear_board_channel_id", str(dest.id))
        await interaction.response.send_message(
            embed=success_embed("Regear board posted", f"Submission board is live in {dest.mention}."),
            ephemeral=True,
        )
        info_log(f"{interaction.user} posted regear board in #{dest.name}.")

    @app_commands.command(
        name="set-review-channel",
        description="Set the channel where regear requests are sent for review.",
    )
    @app_commands.describe(channel="Channel where staff review regears.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_review_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config("regear_review_channel_id", str(channel.id))
        await interaction.response.send_message(
            f"Regear review channel set to {channel.mention}.", ephemeral=True,
        )
        info_log(
            f"{interaction.user} set regear review channel → #{channel.name}."
        )

    @app_commands.command(
        name="list",
        description="List recent regear requests.",
    )
    @app_commands.describe(
        status="Filter by status (default: pending).",
        limit="Max rows to show (1–25, default 10).",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="pending",  value="pending"),
        app_commands.Choice(name="approved", value="approved"),
        app_commands.Choice(name="denied",   value="denied"),
        app_commands.Choice(name="all",      value="all"),
    ])
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_requests(
        self, interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        db = self.bot.db
        if not db.connection:
            db.connect()
        if status and status.value != "all":
            db.cursor.execute(
                "SELECT * FROM regear_requests WHERE status = ? "
                "ORDER BY submitted_at DESC LIMIT ?",
                (status.value, int(limit)),
            )
        else:
            db.cursor.execute(
                "SELECT * FROM regear_requests ORDER BY submitted_at DESC LIMIT ?",
                (int(limit),),
            )
        rows = [dict(r) for r in db.cursor.fetchall()]
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Regears", "No requests found."),
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            badge = {"pending": "⏳", "approved": "✅", "denied": "❌"}.get(
                r.get("status") or "", "•"
            )
            lines.append(
                f"{badge} **#{r['id']}**  <@{r['discord_id']}>  "
                f"`{r.get('content_type') or '—'}`  "
                f"{int(r.get('gear_value') or 0):,}s"
            )
        await interaction.response.send_message(
            embed=info_embed("Regear requests", "\n".join(lines)),
            ephemeral=True,
        )


class Regear(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.bot.tree.add_command(RegearGroup(bot))
        info_log(f"Initialized {self.__class__.__name__} cog.")

    async def cog_load(self) -> None:
        register_persistent_regear_views(self.bot)

    def cog_unload(self) -> None:
        # Reload-safety: drop the manually-added /regear group.
        try:
            self.bot.tree.remove_command("regear")
        except Exception:  # noqa: BLE001
            pass

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready re-fires on every reconnect; only do startup work once.
        if getattr(self, "_ready_done", False):
            return
        self._ready_done = True
        # Belt-and-suspenders: re-register the persistent Submit view so
        # existing board buttons still route correctly after a reconnect.
        register_persistent_regear_views(self.bot)
        info_log("Regear: persistent views registered (on_ready).")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Diagnostic: log any component interaction whose custom_id begins
        # with 'regear:' so we can see whether button clicks reach us.
        try:
            if interaction.type == discord.InteractionType.component:
                cid = (interaction.data or {}).get("custom_id", "")
                if isinstance(cid, str) and cid.startswith("regear:"):
                    info_log(
                        f"[regear-debug] component click: custom_id={cid!r} "
                        f"by {interaction.user} in #{getattr(interaction.channel, 'name', '?')}"
                    )
        except Exception:  # noqa: BLE001
            pass


async def setup(bot: Bot) -> None:
    await bot.add_cog(Regear(bot))
