"""Market arbitrage cog — find buy-low/sell-high opportunities across cities.

Powered by the Albion Online Data Project (AODP), a community-run price feed.
Data freshness varies by city/item — we filter out anything older than 6h.

Slash commands:
- /market scan <item>      — show current prices for one item across cities
- /market arbitrage        — top deals from the watch list right now
- /market watch list       — show the watch list
- /market watch add <item> — add an item ID to the watch list (officer)
- /market watch remove …   — remove an item (officer)
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands, tasks

import market_api
from cogs._typing import Bot
from cogs._autocomplete import make_item_id_autocomplete
from debug import info_log, error_log
from utils import error_embed, info_embed, is_officer, success_embed


# Shared autocomplete: any Albion item (gear, bags, mats, artifacts, etc.).
_ac_item_id = make_item_id_autocomplete()


async def _ac_watchlist(
    interaction: discord.Interaction, current: str,
) -> list[app_commands.Choice[str]]:
    """Suggest items currently on the market watch list."""
    db = getattr(interaction.client, "db", None)
    if db is None:
        return []
    try:
        rows = db.list_market_watch()
    except Exception:
        return []
    q = (current or "").strip().lower()
    matches = [r for r in rows if not q or q in str(r.get("item_id", "")).lower()]
    return [
        app_commands.Choice(name=str(r["item_id"])[:100], value=str(r["item_id"]))
        for r in matches[:25]
    ]


# Auto-post threshold and caps. The point is to be useful, not spammy:
# only post if there's real money to be made, group by trade route, cap
# the number of embeds.
AUTOPOST_MIN_PROFIT_PCT = 30.0
AUTOPOST_MIN_PROFIT_SILVER = 5_000
AUTOPOST_MAX_ROUTES = 5
AUTOPOST_DEALS_PER_ROUTE = 4
AUTOPOST_TOTAL_DEAL_CAP = 15
# Run once per day. 22:00 UTC = ~5pm CST = solid prime-time for Americas.
AUTOPOST_TIME = datetime.time(hour=22, minute=0, tzinfo=datetime.timezone.utc)
AODP_URL = "https://www.albion-online-data.com/"


def _fmt_silver(n: int) -> str:
    """1234567 → '1.23M'. Discord embeds get crowded fast."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def _pretty_item(item_id: str) -> str:
    """T8_BAG@2 → 'T8 Bag @2'. Mirrors albion_api._pretty_item_name but kept
    local so this cog doesn't depend on a private helper there."""
    if not item_id:
        return "—"
    raw, _, ench = item_id.partition("@")
    parts = raw.split("_")
    tier = parts[0] if parts and parts[0].startswith("T") else ""
    rest = " ".join(p.title() for p in parts[1:]) if tier else " ".join(p.title() for p in parts)
    suffix = f" @{ench}" if ench else ""
    return f"{tier} {rest}{suffix}".strip()


def _format_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _city_list() -> str:
    return ", ".join(market_api.DEFAULT_CITIES)


def _arbitrage_methodology(
    item_count: int,
    *,
    min_profit_pct: float,
    min_profit_silver: int | None = None,
) -> str:
    silver_clause = (
        f" and at least **{_fmt_silver(min_profit_silver)} silver**"
        if min_profit_silver is not None else ""
    )
    return (
        f"Prices come from the [Albion Online Data Project]({AODP_URL}), "
        "a community market feed populated by players running its client. "
        f"The bot scans **{item_count}** watch-list items across "
        f"**{len(market_api.DEFAULT_CITIES)}** markets: {_city_list()}.\n"
        f"It currently compares **normal-quality sell-order minimums** only, "
        f"ignores missing or stale rows older than **{market_api.DEFAULT_MAX_AGE_HOURS:.0f}h**, "
        "and drops extreme outliers above/below the 3x median guard.\n"
        "**Trade model:** this is a limit-order style trade idea, not a promise "
        "that the item will still be on the market at that price. Use the source "
        "price as a buy target/max bid, place a buy order or wait for a fill "
        "instead of chasing, then haul and list near the destination sell target "
        "if the spread still exists.\n"
        f"Profit = destination sell minimum after **{market_api.DEFAULT_TAX_PCT:.1f}% tax** "
        f"minus source buy target. Routes must clear **{min_profit_pct:.0f}%** profit"
        f"{silver_clause}. Treat this as a lead, not live in-game confirmation."
    )


ROYAL_SAFE_CITIES = {
    "Bridgewatch",
    "Fort Sterling",
    "Lymhurst",
    "Martlock",
    "Thetford",
}
LETHAL_ROUTE_MARKETS = {"Caerleon", "Black Market"}
BRECILIEN_ROUTE_MARKETS = {"Brecilien"}


@dataclass(frozen=True)
class RouteRisk:
    emoji: str
    label: str
    detail: str
    color: discord.Color


def _route_risk(buy_city: str, sell_city: str) -> RouteRisk:
    """Return a conservative transport-risk label for an arbitrage route."""
    cities = {str(buy_city or "").strip(), str(sell_city or "").strip()}

    if len(cities) == 1:
        return RouteRisk(
            "🟢",
            "Local flip",
            "No city-to-city haul needed. Still verify the listing in-game before buying.",
            discord.Color.green(),
        )

    if cities & LETHAL_ROUTE_MARKETS:
        return RouteRisk(
            "☠️",
            "Lethal red-zone haul",
            "Route touches Caerleon or the Black Market. Expect full-loot red-zone transport risk; scout first and avoid overloading.",
            discord.Color.red(),
        )

    if cities & BRECILIEN_ROUTE_MARKETS:
        return RouteRisk(
            "🌀",
            "Unstable Brecilien haul",
            "Brecilien access depends on Mists/Roads routing. Check current exits before loading a transport mount.",
            discord.Color.orange(),
        )

    if cities <= ROYAL_SAFE_CITIES:
        return RouteRisk(
            "🟢",
            "Royal-city haul",
            "Usually blue/yellow-zone transport between safe cities. Lower risk, but prices can still move before arrival.",
            discord.Color.green(),
        )

    return RouteRisk(
        "⚠️",
        "Unknown route risk",
        "The bot does not recognize this city pair. Treat the haul as risky until a scout confirms the route.",
        discord.Color.gold(),
    )


class Market(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot: Bot = bot
        info_log(f"Initialized {self.__class__.__name__} cog.")
        self.daily_autopost.start()

    def cog_unload(self) -> None:
        self.daily_autopost.cancel()

    market = app_commands.Group(name="market", description="Albion market price tools.")
    watch = app_commands.Group(
        name="watch", description="Manage the market watch list.", parent=market,
    )

    # ── /market scan ────────────────────────────────────────────────────────
    @market.command(name="scan", description="Show current prices for one item across cities.")
    @app_commands.describe(item="Item ID like T6_BAG or T7_POTION_HEAL")
    @app_commands.autocomplete(item=_ac_item_id)
    async def scan(self, interaction: discord.Interaction, item: str) -> None:
        await interaction.response.defer(thinking=True)
        item_id = item.strip().upper()
        rows = await asyncio.to_thread(
            market_api.get_prices, [item_id], market_api.DEFAULT_CITIES, (1,),
        )
        # Keep only rows with usable sell data (>0).
        rows = [r for r in rows if int(r.get("sell_price_min") or 0) > 0]
        if not rows:
            await interaction.followup.send(
                embed=error_embed(
                    "No data",
                    f"AODP has no recent sell-order data for `{item_id}`. "
                    "Check the item ID — it should look like `T6_BAG` or "
                    "`T5_POTION_HEAL`.",
                ),
            )
            return
        rows.sort(key=lambda r: int(r.get("sell_price_min") or 0))
        lines = []
        for r in rows:
            sell = int(r.get("sell_price_min") or 0)
            age = market_api._parse_age_hours(r.get("sell_price_min_date")) or 0.0
            stale = " ⚠️" if age > market_api.DEFAULT_MAX_AGE_HOURS else ""
            lines.append(
                f"**{r.get('city', '?')}** — `{_fmt_silver(sell)}` "
                f"({_format_age(age)} old){stale}"
            )
        embed = info_embed(
            f"💰 {_pretty_item(item_id)} — sell-order prices",
            "\n".join(lines),
        )
        embed.set_footer(text="Source: Albion Online Data Project (community)")
        await interaction.followup.send(embed=embed)
        info_log(f"{interaction.user} ran /market scan {item_id} ({len(rows)} cities).")

    # ── /market arbitrage ───────────────────────────────────────────────────
    @market.command(name="arbitrage", description="Find buy-low/sell-high deals from the watch list.")
    @app_commands.describe(
        min_profit_pct="Minimum profit % after tax (default 20)",
        limit="How many deals to show (1-25, default 10)",
    )
    async def arbitrage(
        self, interaction: discord.Interaction,
        min_profit_pct: app_commands.Range[int, 1, 500] = 20,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await interaction.response.defer(thinking=True)
        watch = self.bot.db.list_market_watch()
        if not watch:
            await interaction.followup.send(
                embed=error_embed(
                    "Empty watch list",
                    "No items in the market watch list. An officer can add "
                    "items with `/market watch add T6_BAG`.",
                ),
            )
            return
        item_ids = [w["item_id"] for w in watch]
        deals = await asyncio.to_thread(
            market_api.find_arbitrage_batched,
            item_ids,
            min_profit_pct=float(min_profit_pct),
            limit=int(limit),
        )
        if not deals:
            embed = info_embed(
                "No deals right now",
                f"Nothing clears **{min_profit_pct}%** profit after tax "
                f"with fresh (<{int(market_api.DEFAULT_MAX_AGE_HOURS)}h) data.",
            )
            embed.add_field(
                name="Where the numbers come from",
                value=_arbitrage_methodology(
                    len(item_ids),
                    min_profit_pct=float(min_profit_pct),
                )[:1024],
                inline=False,
            )
            await interaction.followup.send(embed=embed)
            return
        lines = []
        for d in deals:
            risk = _route_risk(d["buy_city"], d["sell_city"])
            lines.append(
                f"**{_pretty_item(d['item_id'])}**\n"
                f"  Buy target in **{d['buy_city']}**: `≤ {_fmt_silver(d['buy_price'])}` "
                f"({_format_age(d['buy_age_h'])} old)\n"
                f"  Sell target in **{d['sell_city']}**: list near `{_fmt_silver(d['sell_price'])}` "
                f"({_format_age(d['sell_age_h'])} old)\n"
                f"  → Profit `{_fmt_silver(d['profit'])}` "
                f"(**+{d['profit_pct']:.0f}%** after {d['tax_pct']:.1f}% tax)\n"
                f"  {risk.emoji} **Risk:** {risk.label} — {risk.detail}"
            )
        embed = info_embed(
            "💰 Arbitrage Opportunities",
            (
                "**Read these as limit-order trade ideas.** Put in a buy order "
                "near the source target, wait for a fill, then list near the "
                "destination sell target if the spread is still there. Do not "
                "assume instant listings will match this report.\n\n"
                + "\n\n".join(lines)
            )[:4000],
        )
        embed.add_field(
            name="Where the numbers come from",
            value=_arbitrage_methodology(
                len(item_ids),
                min_profit_pct=float(min_profit_pct),
            )[:1024],
            inline=False,
        )
        embed.set_footer(
            text=(
                f"AODP community data • Q1 sell-order minimums • "
                f"min {min_profit_pct}% profit"
            ),
        )
        await interaction.followup.send(embed=embed)
        info_log(
            f"{interaction.user} ran /market arbitrage "
            f"(min={min_profit_pct}%, limit={limit}, found={len(deals)})."
        )

    # ── /market watch list/add/remove ───────────────────────────────────────
    @watch.command(name="list", description="Show items currently on the market watch list.")
    async def watch_list(self, interaction: discord.Interaction) -> None:
        rows = self.bot.db.list_market_watch()
        if not rows:
            await interaction.response.send_message(
                embed=info_embed("Watch list empty", "No items being tracked yet."),
                ephemeral=True,
            )
            return
        # Group by tier prefix for readability.
        by_tier: dict[str, list[str]] = {}
        for r in rows:
            iid = r["item_id"]
            tier = iid.split("_", 1)[0] if "_" in iid else "?"
            by_tier.setdefault(tier, []).append(iid)
        chunks = []
        for tier in sorted(by_tier):
            ids = sorted(by_tier[tier])
            chunks.append(f"**{tier}** ({len(ids)}): {', '.join(f'`{i}`' for i in ids)}")
        embed = info_embed(
            f"📋 Market Watch List — {len(rows)} items",
            "\n".join(chunks)[:4000],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @watch.command(name="add", description="Add an item ID to the market watch list (officer).")
    @app_commands.describe(item="Item ID like T6_BAG or T7_POTION_HEAL", note="Optional note")
    @app_commands.autocomplete(item=_ac_item_id)
    async def watch_add(
        self, interaction: discord.Interaction, item: str, note: str | None = None,
    ) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This command is restricted to officers."),
                ephemeral=True,
            )
            return
        item_id = item.strip().upper()
        ok = self.bot.db.add_market_watch(item_id, str(interaction.user.id), note)
        if ok:
            await interaction.response.send_message(
                embed=success_embed("Added", f"`{item_id}` added to the watch list."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} added {item_id} to market watch.")
        else:
            await interaction.response.send_message(
                embed=info_embed("Already there", f"`{item_id}` is already on the watch list."),
                ephemeral=True,
            )

    @watch.command(name="remove", description="Remove an item from the market watch list (officer).")
    @app_commands.describe(item="Item ID to remove")
    @app_commands.autocomplete(item=_ac_watchlist)
    async def watch_remove(self, interaction: discord.Interaction, item: str) -> None:
        if not is_officer(interaction.user):
            await interaction.response.send_message(
                embed=error_embed("Officers only", "This command is restricted to officers."),
                ephemeral=True,
            )
            return
        item_id = item.strip().upper()
        ok = self.bot.db.remove_market_watch(item_id)
        if ok:
            await interaction.response.send_message(
                embed=success_embed("Removed", f"`{item_id}` removed from the watch list."),
                ephemeral=True,
            )
            info_log(f"{interaction.user} removed {item_id} from market watch.")
        else:
            await interaction.response.send_message(
                embed=error_embed("Not found", f"`{item_id}` is not on the watch list."),
                ephemeral=True,
            )

    # ── /market set-channel ─────────────────────────────────────────────────
    @market.command(
        name="set-channel",
        description="Set the channel where daily arbitrage opportunities are posted (officer).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
    ) -> None:
        self.bot.db.set_config("market_autopost_channel_id", str(channel.id))
        await interaction.response.send_message(
            embed=success_embed(
                "Auto-post channel set",
                f"Daily arbitrage opportunities will post to {channel.mention} "
                f"at **22:00 UTC**.",
            ),
            ephemeral=True,
        )
        info_log(f"{interaction.user} set market_autopost_channel_id → #{channel.name}.")

    @market.command(
        name="post-now",
        description="Manually trigger an arbitrage post to the configured channel (officer).",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def post_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = await _do_autopost(self.bot, manual=True)
        if posted:
            await interaction.followup.send(
                embed=success_embed("Posted", "Arbitrage update sent."),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=info_embed(
                    "Nothing to post",
                    "Either no channel is configured, or no deals cleared the "
                    f"{int(AUTOPOST_MIN_PROFIT_PCT)}% profit threshold.",
                ),
                ephemeral=True,
            )

    # ── Daily auto-post loop ────────────────────────────────────────────────
    @tasks.loop(time=AUTOPOST_TIME)
    async def daily_autopost(self) -> None:
        try:
            await _do_autopost(self.bot, manual=False)
        except Exception as exc:  # noqa: BLE001
            error_log(f"market daily_autopost failed: {exc!r}")

    @daily_autopost.before_loop
    async def _before_autopost(self) -> None:
        await self.bot.wait_until_ready()


# ── Auto-post helpers ───────────────────────────────────────────────────────

def _route_emoji(buy_city: str, sell_city: str) -> str:
    """Pick a route emoji from the same risk model used in market embeds."""
    return _route_risk(buy_city, sell_city).emoji


async def _do_autopost(bot: Bot, *, manual: bool) -> bool:
    """Build and send the route-grouped arbitrage post. Returns True if a
    message was actually sent."""
    channel_id = bot.db.get_config("market_autopost_channel_id")
    if not channel_id:
        if not manual:
            info_log("market autopost: no channel configured, skipping.")
        return False
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        error_log(f"market autopost: channel id {channel_id} not found.")
        return False
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        error_log(f"market autopost: channel {channel_id} is not a text channel.")
        return False

    watch = bot.db.list_market_watch()
    if not watch:
        info_log("market autopost: watch list empty, skipping.")
        return False
    item_ids = [w["item_id"] for w in watch]

    deals = await asyncio.to_thread(
        market_api.find_arbitrage_batched,
        item_ids,
        min_profit_pct=AUTOPOST_MIN_PROFIT_PCT,
        min_profit_silver=AUTOPOST_MIN_PROFIT_SILVER,
        limit=AUTOPOST_TOTAL_DEAL_CAP,
    )
    if not deals:
        info_log(
            f"market autopost: scanned {len(item_ids)} items, no deals cleared "
            f"{AUTOPOST_MIN_PROFIT_PCT}%."
        )
        return False

    # Group deals by route (buy_city → sell_city). Within a route, sort by
    # profit % desc and cap.
    routes: dict[tuple[str, str], list[dict]] = {}
    for d in deals:
        key = (d["buy_city"], d["sell_city"])
        routes.setdefault(key, []).append(d)
    # Sort routes by their best deal's profit %, take the top N.
    ranked_routes = sorted(
        routes.items(),
        key=lambda kv: max(d["profit_pct"] for d in kv[1]),
        reverse=True,
    )[:AUTOPOST_MAX_ROUTES]

    timestamp = discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    header = info_embed(
        "💰 Union Market — Daily Arbitrage Report",
        f"Top trade routes from the watch list as of **{timestamp}**.\n"
        "Each route includes a transport-risk label based on the cities involved.\n\n"
        "📈 **Trade model:** place buy orders near the source target, wait for fills, "
        "then haul and list near the destination sell target. This is closer to "
        "stock trading than instant shopping.\n\n"
        f"⚠️ **Always double-check prices in-game before you ride out.** "
        f"Albion markets move fast — listings can be undercut, bought out, or "
        f"posted as scams between report time and arrival.",
    )
    header.add_field(
        name="Where the numbers come from",
        value=_arbitrage_methodology(
            len(item_ids),
            min_profit_pct=AUTOPOST_MIN_PROFIT_PCT,
            min_profit_silver=AUTOPOST_MIN_PROFIT_SILVER,
        )[:1024],
        inline=False,
    )
    header.set_footer(
        text=(
            "AODP community data • Q1 sell-order minimums • "
            "Posts daily at 22:00 UTC"
        ),
    )

    embeds: list[discord.Embed] = [header]
    total_listed = 0
    for (buy_city, sell_city), route_deals in ranked_routes:
        route_deals = sorted(route_deals, key=lambda d: d["profit_pct"], reverse=True)
        route_deals = route_deals[:AUTOPOST_DEALS_PER_ROUTE]
        risk = _route_risk(buy_city, sell_city)
        best_pct = route_deals[0]["profit_pct"]
        total_profit = sum(d["profit"] for d in route_deals)
        lines = [f"**Risk:** {risk.label} — {risk.detail}", ""]
        for d in route_deals:
            lines.append(
                f"• **{_pretty_item(d['item_id'])}** — "
                f"buy target `≤ {_fmt_silver(d['buy_price'])}` → "
                f"sell target `{_fmt_silver(d['sell_price'])}` → "
                f"**+{_fmt_silver(d['profit'])}** (+{d['profit_pct']:.0f}%)\n"
                f"  Data age: buy {_format_age(d['buy_age_h'])}, "
                f"sell {_format_age(d['sell_age_h'])}"
            )
            total_listed += 1
        route_embed = discord.Embed(
            title=f"{risk.emoji} {buy_city} → {sell_city}",
            description="\n".join(lines),
            color=risk.color,
        )
        route_embed.set_footer(
            text=(
                f"Best margin +{best_pct:.0f}% • "
                f"Combined profit on this route: {_fmt_silver(total_profit)} silver"
            ),
        )
        embeds.append(route_embed)

    try:
        # Discord allows up to 10 embeds per message.
        await channel.send(embeds=embeds[:10])
    except (discord.Forbidden, discord.HTTPException) as exc:
        error_log(f"market autopost: send failed: {exc!r}")
        return False

    info_log(
        f"market autopost: posted {len(ranked_routes)} routes "
        f"({total_listed} deals) to #{channel.name}."
    )
    return True


async def setup(bot: Bot) -> None:
    # One-time seed on first load.
    bot.db.seed_market_watch_if_empty()
    await bot.add_cog(Market(bot))
