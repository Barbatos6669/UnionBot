"""Live Timer Claim System announcement helpers."""

from __future__ import annotations

import discord

from cogs._lfg_config import (
    PRIME_SLOTS,
    next_prime_slot_window,
    prime_slot_display_label,
)
from cogs._typing import Bot
from debug import error_log, info_log

TRACKER_TYPE = "timer-claim-guide"
TRACKER_ID = "main"
CFG_POSTED_BY = "timer_claim_system_posted_by"


def _claim_guide_color(db) -> discord.Color:
    color_hex = (db.get_config("announce_color_hex") or "#d4af37").strip()
    if not color_hex.startswith("#"):
        color_hex = "#" + color_hex
    try:
        return discord.Color.from_str(color_hex)
    except ValueError:
        return discord.Color.from_str("#d4af37")


def _claim_guide_slots() -> str:
    slot_rows: list[str] = []
    for slot in PRIME_SLOTS:
        start, end = next_prime_slot_window(slot)
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        slot_rows.append(
            f"{slot.emoji} **{prime_slot_display_label(slot)}** — "
            f"<t:{start_ts}:t>-<t:{end_ts}:t> local · <t:{start_ts}:R>"
        )
    return "\n".join(slot_rows)


def build_timer_claim_guide_embed(db, posted_by: str | None = None) -> discord.Embed:
    crest_url = (db.get_config("announce_crest_url") or "").strip() or None
    footer_name = (db.get_config("announce_footer_name") or "Timer Claim System").strip()
    posted_by = (posted_by or db.get_config(CFG_POSTED_BY) or "").strip()

    embed = discord.Embed(
        title="\u2694\ufe0f Guild Timer Claim System",
        description=(
            "Going forward we're organizing guild content around **timer claims**.\n\n"
            "**The goal: less waiting, more content, better organization.**\n\n"
            "Each day has multiple prime-time windows. Instead of waiting until the "
            "last minute to figure out who's leading, what we're running, or what "
            "gear people need, **shotcallers claim a timer slot ahead of time** on "
            "the event board.\n\n"
            "\ud83c\uddea\ud83c\uddf8 *Organizamos el contenido del gremio por **reclamos de timer**. "
            "Reclama tu slot con anticipaci\u00f3n en el panel de eventos.*\n"
            "\ud83c\udde7\ud83c\uddf7 *Organizamos o conte\u00fado da guilda por **reservas de timer**. "
            "Reserve seu slot com anteced\u00eancia no painel de eventos.*"
        ),
        color=_claim_guide_color(db),
    )
    embed.add_field(
        name="\ud83d\udcc5 Prime-Time Slots — next up in your local time",
        value=_claim_guide_slots() + (
            "\n\nClaim slots from the event board posted in "
            "**#looking-for-group** (`/lfg post-board` puts it there). "
            "Click the colored slot button to open the create-event modal."
        ),
        inline=False,
    )
    embed.add_field(
        name="\ud83d\udcdd What a claimed timer must include",
        value=(
            "\u2022 **Timer slot / start time**\n"
            "\u2022 **Shotcaller leading it** (auto-set to whoever claims)\n"
            "\u2022 **Content type** (ZvZ, ganking, mists, HCEs, etc.)\n"
            "\u2022 **Required gear / comp** (IP floor, build list, swaps)\n"
            "\u2022 **Roster / signup list** (members sign up via the event post)\n"
            "\u2022 **Form-up and step-off time**"
        ),
        inline=False,
    )
    embed.add_field(
        name="\ud83c\udfaf Shotcaller Responsibilities",
        value=(
            "If you claim a timer, **you are responsible** for:\n"
            "\u2022 Preparing the event (comp + gear list posted)\n"
            "\u2022 Getting people signed up (ping content roles if needed)\n"
            "\u2022 Confirming the roster meets the IP floor before step-off\n"
            "\u2022 Running the content from form-up through after-action\n"
            "\u2022 Closing the event via `/event close` so attendance + loot lock in"
        ),
        inline=False,
    )
    embed.add_field(
        name="\ud83d\udd13 Open Content Still Welcome",
        value=(
            "Claimed timers **do not block** open LFG. You can still post non-prime "
            "LFG via the event board's general LFG button — it just can't overlap "
            "an already-claimed prime-time slot in the same window."
        ),
        inline=False,
    )
    embed.add_field(
        name="\ud83d\udd25 Compete for the Slots",
        value=(
            "Shotcallers **should be fighting over these slots**. The more timers we "
            "fill with organized content, the stronger and more active the guild "
            "becomes. Empty slots = missed content. Don't let them sit."
        ),
        inline=False,
    )
    embed.add_field(
        name="\u2705 Who Can Claim",
        value=(
            "Prime-time slot creation is restricted to: "
            "**Shotcaller**, **Senior Shotcaller**, **Officer**, **Captain**, "
            "**Commander**, **Guild Leader**.\n"
            "Not a Shotcaller yet? Apply via `/staff apply` in `#command-applications`."
        ),
        inline=False,
    )
    embed.add_field(
        name="\ud83d\udd14 Final Rule",
        value="**Claim the timer. Build the roster. Gear before step-off. Run the content.**",
        inline=False,
    )

    footer = footer_name
    if posted_by:
        footer += f" \u00b7 Posted by {posted_by}"
    if crest_url:
        embed.set_thumbnail(url=crest_url)
        embed.set_footer(text=footer, icon_url=crest_url)
    else:
        embed.set_footer(text=footer)
    return embed


async def refresh_timer_claim_guide_trackers(bot: Bot) -> int:
    try:
        trackers = bot.db.fetch_all_live_graphs()
    except Exception as exc:  # noqa: BLE001
        error_log(f"timer claim guide tracker fetch failed: {exc!r}")
        return 0

    updated = 0
    for tracker in trackers:
        if tracker["type"] != TRACKER_TYPE:
            continue
        try:
            channel = bot.get_channel(int(tracker["channel_id"]))
            if channel is None:
                channel = await bot.fetch_channel(int(tracker["channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue
            message = await channel.fetch_message(int(tracker["message_id"]))
            await message.edit(embed=build_timer_claim_guide_embed(bot.db))
            updated += 1
            info_log("Updated Timer Claim System guide.")
        except discord.NotFound:
            bot.db.delete_live_graph(TRACKER_TYPE, tracker["target_id"])
        except (discord.Forbidden, discord.HTTPException, ValueError) as exc:
            error_log(f"timer claim guide refresh failed: {exc!r}")
    return updated
