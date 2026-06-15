"""Albion Online Data Project (AODP) client + arbitrage scanner.

AODP is a community-run market price feed populated by players running a
client-side sniffer. Free, no auth. Coverage varies by city/item — black-zone
outposts and unpopular items can be days stale.

Region endpoints:
- west:    https://west.albion-online-data.com/   (Americas)
- east:    https://east.albion-online-data.com/   (Asia)
- europe:  https://europe.albion-online-data.com/

Docs: https://www.albion-online-data.com/
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any, Iterable

import requests

import debug

REGION_URLS = {
    "americas": "https://west.albion-online-data.com",
    "asia":     "https://east.albion-online-data.com",
    "europe":   "https://europe.albion-online-data.com",
}

# AODP politely requests a User-Agent so they can identify clients on abuse.
_USER_AGENT = "UnionBot/1.0 (+https://github.com/ExampleOfficer/GuildOPs)"

# Royal cities + Caerleon + Brecilien. AODP also accepts black-market and
# portal towns but those are rarely useful for arbitrage.
DEFAULT_CITIES = (
    "Caerleon", "Bridgewatch", "Martlock",
    "Lymhurst", "Fort Sterling", "Thetford", "Brecilien",
)

# Albion taxes a market sale ~7.7% total (4.5% setup fee + 3% sales tax for
# non-premium; premium players pay 2.25% + 3% = 5.25%). We use the higher
# number so quoted profits are conservative.
DEFAULT_TAX_PCT = 7.5

# Treat data older than this as untrustworthy.
DEFAULT_MAX_AGE_HOURS = 6.0

# AODP returns 0 for "no data". Skip those rows.
_NO_DATA = 0

_DEFAULT_TIMEOUT = 20.0


def _city_param(city: str) -> str:
    """AODP wants spaces stripped/encoded. 'Fort Sterling' → 'Fort Sterling'
    works as a query param when properly URL-encoded by requests, but the
    safe form is to just pass it through; requests handles the encoding."""
    return city.strip()


def get_prices(
    item_ids: Iterable[str],
    cities: Iterable[str] = DEFAULT_CITIES,
    qualities: Iterable[int] = (1,),
    region: str = "americas",
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Fetch raw price rows for one-or-more items across one-or-more cities.

    Returns a list of dicts: {item_id, city, quality, sell_price_min,
    sell_price_min_date, sell_price_max, buy_price_min, buy_price_max, ...}.
    Empty list on error.
    """
    items = [str(i).strip() for i in item_ids if str(i).strip()]
    if not items:
        return []
    base = REGION_URLS.get(region.lower(), REGION_URLS["americas"])
    url = f"{base}/api/v2/stats/prices/{','.join(items)}.json"
    params = {
        "locations": ",".join(_city_param(c) for c in cities),
        "qualities": ",".join(str(int(q)) for q in qualities),
    }
    try:
        resp = requests.get(
            url, params=params, timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        debug.error_log(f"AODP get_prices failed for {len(items)} items: {exc!r}")
        return []
    if not isinstance(data, list):
        return []
    return data


def _parse_age_hours(iso_ts: str | None) -> float | None:
    """Convert an AODP timestamp like '2026-05-12T18:33:01' to hours-old.
    Returns None on parse failure or empty timestamp."""
    if not iso_ts:
        return None
    try:
        # AODP returns naive UTC strings.
        dt = _dt.datetime.fromisoformat(iso_ts.replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        age = _dt.datetime.now(tz=_dt.timezone.utc) - dt
        return age.total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def find_arbitrage(
    item_ids: Iterable[str],
    cities: Iterable[str] = DEFAULT_CITIES,
    qualities: Iterable[int] = (1,),
    *,
    region: str = "americas",
    min_profit_pct: float = 20.0,
    min_profit_silver: int = 1000,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    tax_pct: float = DEFAULT_TAX_PCT,
    limit: int = 25,
    outlier_factor: float = 3.0,
) -> list[dict[str, Any]]:
    """Scan for "buy low in city A, sell high in city B" opportunities.

    Strategy: for each (item, quality), find the lowest sell-order price
    across all scanned cities (you buy from someone's sell order), and the
    highest sell-order price in any other city (you re-list at that price).
    We deliberately compare sell-order vs sell-order — buy orders are usually
    far below market and require waiting.

    Outlier protection: any city whose price is more than `outlier_factor` x
    the median (in either direction) is dropped. AODP routinely contains
    scam listings (e.g. one item priced at 50M to bait misclicks) and
    stale data left over from old market state. Dropping the extremes
    prevents the bot from cheerfully reporting impossible profits.

    Profit = (sell_price * (1 - tax)) - buy_price.

    Returns deals sorted by profit_pct desc, capped at `limit`.
    """
    rows = get_prices(item_ids, cities, qualities, region=region)
    if not rows:
        return []

    # Group by (item_id, quality) → list of city rows.
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in rows:
        item_id = r.get("item_id") or ""
        quality = int(r.get("quality") or 1)
        if not item_id:
            continue
        # Drop rows with no usable sell price or stale data.
        sell_min = int(r.get("sell_price_min") or 0)
        if sell_min <= _NO_DATA:
            continue
        age = _parse_age_hours(r.get("sell_price_min_date"))
        if age is None or age > max_age_hours:
            continue
        groups.setdefault((item_id, quality), []).append(r)

    deals: list[dict[str, Any]] = []
    tax_factor = max(0.0, 1.0 - (tax_pct / 100.0))

    for (item_id, quality), city_rows in groups.items():
        if len(city_rows) < 2:
            continue
        # Outlier guard: drop any city whose price is wildly off from the
        # median for this item. Catches scam listings and stale data that
        # would otherwise show as 200,000% profit.
        prices = sorted(int(r["sell_price_min"]) for r in city_rows)
        median = prices[len(prices) // 2]
        if median > 0 and outlier_factor > 1.0:
            lo = median / outlier_factor
            hi = median * outlier_factor
            city_rows = [
                r for r in city_rows
                if lo <= int(r["sell_price_min"]) <= hi
            ]
            if len(city_rows) < 2:
                continue
        # Cheapest sell order = where you'd buy.
        buy_row = min(city_rows, key=lambda r: int(r["sell_price_min"]))
        # Most expensive sell order in a *different* city = where you'd re-list.
        sell_candidates = [
            r for r in city_rows
            if (r.get("city") or "") != (buy_row.get("city") or "")
        ]
        if not sell_candidates:
            continue
        sell_row = max(sell_candidates, key=lambda r: int(r["sell_price_min"]))

        buy_price = int(buy_row["sell_price_min"])
        sell_price = int(sell_row["sell_price_min"])
        net_revenue = sell_price * tax_factor
        profit = net_revenue - buy_price
        if profit < min_profit_silver:
            continue
        profit_pct = (profit / buy_price) * 100.0 if buy_price else 0.0
        if profit_pct < min_profit_pct:
            continue

        deals.append({
            "item_id":    item_id,
            "quality":    quality,
            "buy_city":   buy_row.get("city") or "?",
            "buy_price":  buy_price,
            "buy_age_h":  _parse_age_hours(buy_row.get("sell_price_min_date")) or 0.0,
            "sell_city":  sell_row.get("city") or "?",
            "sell_price": sell_price,
            "sell_age_h": _parse_age_hours(sell_row.get("sell_price_min_date")) or 0.0,
            "profit":     int(profit),
            "profit_pct": profit_pct,
            "tax_pct":    tax_pct,
        })

    deals.sort(key=lambda d: d["profit_pct"], reverse=True)
    return deals[: max(1, int(limit))]


def chunk_items(item_ids: list[str], chunk_size: int = 50) -> list[list[str]]:
    """AODP accepts very long URLs, but ~50 items per call is a safe ceiling
    to stay well under the 4KB-ish URL length limit and keep responses snappy."""
    return [item_ids[i:i + chunk_size] for i in range(0, len(item_ids), chunk_size)]


def find_arbitrage_batched(
    item_ids: list[str],
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run `find_arbitrage` over chunks of a large item list and merge the
    top deals across all chunks. Useful for full watch-list scans."""
    limit = int(kwargs.pop("limit", 25))
    all_deals: list[dict[str, Any]] = []
    for chunk in chunk_items(item_ids):
        all_deals.extend(find_arbitrage(chunk, limit=limit, **kwargs))
        # Tiny politeness pause — AODP is a free community service.
        time.sleep(0.25)
    all_deals.sort(key=lambda d: d["profit_pct"], reverse=True)
    return all_deals[:limit]


# ── Gear value estimation ──────────────────────────────────────────────────

def estimate_gear_value(
    items: list[dict[str, Any]],
    *,
    cities: Iterable[str] = DEFAULT_CITIES,
    region: str = "americas",
    max_age_hours: float = 24.0 * 7,  # gear sells slower than mats
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Estimate the total silver value of a list of items (e.g. gear from a
    death event).

    ``items`` is a list of dicts with at least ``item_id`` and optional
    ``quality`` (1-5) and ``count`` (defaults to 1). Returns:

        {
            "total":       int,           # silver, sum of best prices
            "per_item":    [               # one entry per input item
                {"item_id": str, "quality": int, "count": int,
                 "unit_price": int, "city": str, "age_h": float,
                 "subtotal": int, "missing": bool},
                ...
            ],
            "missing_count": int,         # items with no AODP data
        }

    For each item we pull prices across the given cities at all qualities,
    pick the lowest fresh sell-order price matching the actual quality (or
    fall back to quality 1 if the exact tier is missing), and multiply by
    count. Items with no price data at all show up as ``missing=True`` and
    contribute 0 to the total.
    """
    # Deduplicate item IDs for the API call but keep the original list for
    # the result (an item could appear twice, e.g. two stacks of potions).
    item_ids = sorted({str(i.get("item_id", "")).strip() for i in items if i.get("item_id")})
    if not item_ids:
        return {"total": 0, "per_item": [], "missing_count": 0}

    # AODP cap: ~150 items per request; gear has at most ~10 slots so one
    # request is always enough — but stay defensive.
    all_rows: list[dict[str, Any]] = []
    for chunk in chunk_items(item_ids):
        all_rows.extend(
            get_prices(chunk, cities=cities, qualities=(1, 2, 3, 4, 5),
                       region=region, timeout=timeout)
        )

    # Index rows: (item_id, quality) → list of (sell_price, city, age_h)
    index: dict[tuple[str, int], list[tuple[int, str, float]]] = {}
    for r in all_rows:
        iid = str(r.get("item_id", ""))
        qual = int(r.get("quality") or 1)
        price = int(r.get("sell_price_min") or 0)
        if price <= 0:
            continue
        age = _parse_age_hours(r.get("sell_price_min_date"))
        if age is None or age > max_age_hours:
            continue
        index.setdefault((iid, qual), []).append((price, str(r.get("city") or ""), age))

    per_item: list[dict[str, Any]] = []
    total = 0
    missing = 0
    for it in items:
        iid = str(it.get("item_id", "")).strip()
        if not iid:
            continue
        qual = int(it.get("quality") or 1)
        count = max(1, int(it.get("count") or 1))
        # Try exact quality first, then fall back to lower qualities
        # (low-quality versions of the same item are still the same gear).
        rows = index.get((iid, qual)) or []
        if not rows:
            for q_try in (1, 2, 3, 4, 5):
                if q_try == qual:
                    continue
                rows = index.get((iid, q_try)) or []
                if rows:
                    break
        if not rows:
            missing += 1
            per_item.append({
                "item_id": iid, "quality": qual, "count": count,
                "unit_price": 0, "city": "", "age_h": 0.0,
                "subtotal": 0, "missing": True,
            })
            continue
        # Cheapest fresh listing wins — that's the floor to buy the item back.
        rows.sort(key=lambda t: t[0])
        unit_price, city, age = rows[0]
        subtotal = unit_price * count
        total += subtotal
        per_item.append({
            "item_id": iid, "quality": qual, "count": count,
            "unit_price": unit_price, "city": city, "age_h": age,
            "subtotal": subtotal, "missing": False,
        })

    return {"total": total, "per_item": per_item, "missing_count": missing}
