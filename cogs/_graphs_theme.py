"""Shared theme constants and small lookup tables for the graphs cog.

Kept under a leading-underscore filename so ``bot.py``'s cog auto-loader
(which globs ``cogs/*.py`` and skips ``_*.py``) ignores it. Imported by
``cogs/graphs.py`` to keep the main file readable.
"""

from __future__ import annotations

import datetime


# ── theme palette ───────────────────────────────────────────────────────────

# Softer than pure white — easier on the eyes in Discord embeds.
BG_FIG      = "#eef1f5"   # outer canvas (subtle cool gray)
BG_AXES     = "#f7f8fa"   # plot area (slightly lighter for depth)
GRID_COLOR  = "#dde1e6"
SPINE_COLOR = "#c5cad1"
TEXT_COLOR  = "#2c3e50"
MUTED_TEXT  = "#6b7280"
ACCENT      = "#3fb6a8"

# Per-metric accent colors, desaturated to sit nicely on the soft background.
PALETTE: dict[str, str] = {
    "kill":    "#e07a5f",
    "death":   "#8d99ae",
    "pve":     "#4a90d9",
    "gather":  "#5fbf78",
    "craft":   "#e6b54a",
    "ip":      "#9b7bd4",
    "members": "#3fb6a8",
}

# Candle colors
CANDLE_UP   = "#27ae60"
CANDLE_DOWN = "#c0392b"
CANDLE_FLAT = "#8d99ae"


# ── metric tables ───────────────────────────────────────────────────────────

# Player metrics — order matters (used for the 3×2 grid layout).
PLAYER_METRICS: list[tuple[str, str, str]] = [
    ("Kill Fame",      "kill_fame",          PALETTE["kill"]),
    ("Death Fame",     "death_fame",         PALETTE["death"]),
    ("PvE Total",      "pve_total",          PALETTE["pve"]),
    ("Gathering",      "gather_all",         PALETTE["gather"]),
    ("Crafting Fame",  "crafting_fame",      PALETTE["craft"]),
    ("Avg Item Power", "average_item_power", PALETTE["ip"]),
]
PLAYER_METRIC_BY_KEY: dict[str, tuple[str, str]] = {
    key: (label, color) for label, key, color in PLAYER_METRICS
}

# Cumulative metrics — deltas should never go negative (clamp resets to 0).
CUMULATIVE_METRICS: set[str] = {
    "kill_fame", "death_fame", "pve_total", "gather_all", "crafting_fame",
}

# Candle timeframes: value → (label, bucket-key fn, tick-format)
TIMEFRAMES: dict[str, tuple[str, "callable", str]] = {
    "1h": ("1 Hour",
           lambda dt: dt.replace(minute=0, second=0, microsecond=0),
           "%m/%d %H:00"),
    "4h": ("4 Hour",
           lambda dt: dt.replace(hour=(dt.hour // 4) * 4, minute=0,
                                 second=0, microsecond=0),
           "%m/%d %H:00"),
    "1d": ("Daily",
           lambda dt: datetime.datetime.combine(dt.date(), datetime.time()),
           "%m/%d"),
    "1w": ("Weekly",
           lambda dt: datetime.datetime.combine(
               dt.date() - datetime.timedelta(days=dt.weekday()),
               datetime.time()),
           "%m/%d"),
}
