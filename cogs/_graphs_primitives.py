"""Plotting primitives extracted from ``cogs/graphs.py``.

Pure helpers that build matplotlib figures/axes — no Discord cog state
required. The single Discord touch-point is ``_fig_to_file`` which wraps
the rendered PNG buffer in a ``discord.File``.

Kept under a leading-underscore filename so the cog auto-loader skips
this file (it globs ``cogs/*.py`` and ignores ``_*.py``).
"""

from __future__ import annotations

import datetime
import io
from collections import defaultdict

import discord
import matplotlib
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker

from cogs._graphs_theme import (
    ACCENT,
    BG_AXES,
    BG_FIG,
    CUMULATIVE_METRICS,
    GRID_COLOR,
    MUTED_TEXT,
    SPINE_COLOR,
    TEXT_COLOR,
)


# ── parsing ─────────────────────────────────────────────────────────────────

def _parse_dt(s: str) -> datetime.datetime:
    """Defensive ISO parser — tolerates trailing 'Z' from older snapshots."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(s)


def _parse_dates(rows: list) -> list[datetime.datetime]:
    return [_parse_dt(r["recorded_at"]) for r in rows]


# ── formatting ──────────────────────────────────────────────────────────────

def _fmt_compact(x, _=None) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1_000_000:
        return f"{sign}{x/1_000_000:.1f}M".replace(".0M", "M")
    if x >= 1_000:
        return f"{sign}{x/1_000:.0f}K"
    return f"{sign}{x:,.0f}"


# ── canvas / figure helpers ─────────────────────────────────────────────────

def _fig_to_file(fig, filename: str) -> discord.File:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=fig.get_facecolor())
    buf.seek(0)
    plt.close(fig)
    return discord.File(buf, filename=filename)


def _canvas(figsize: tuple[float, float], title: str,
            nrows: int = 1, ncols: int = 1):
    """Create a themed figure + axes with a left-aligned suptitle."""
    fig, axes = plt.subplots(nrows, ncols, figsize=figsize, constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(title, fontsize=15, fontweight="700",
                 color=TEXT_COLOR, x=0.02, ha="left")
    return fig, axes


def _style_axes(ax) -> None:
    ax.set_facecolor(BG_AXES)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(SPINE_COLOR)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED_TEXT, labelsize=8, length=0)
    ax.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    ax.set_axisbelow(True)


def _apply_date_axis(ax, dates) -> None:
    locator = mdates.AutoDateLocator(minticks=4, maxticks=7)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, show_offset=False))
    ax.tick_params(axis="x", rotation=0)


# ── plotting helpers ────────────────────────────────────────────────────────

def _plot_series(ax, dates, values, label, color) -> None:
    ax.plot(dates, values, linewidth=2.0, color=color,
            solid_capstyle="round", zorder=3)
    ax.fill_between(dates, values, alpha=0.20, color=color, linewidth=0, zorder=2)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    ax.set_title(label, color=TEXT_COLOR, fontsize=10, fontweight="600",
                 loc="left", pad=6)
    _style_axes(ax)
    _apply_date_axis(ax, dates)


def _empty_panel(ax, message: str = "No data yet") -> None:
    ax.text(0.5, 0.5, message, ha="center", va="center",
            color=MUTED_TEXT, fontsize=12, transform=ax.transAxes)
    _style_axes(ax)


# ── delta / bucket transforms ───────────────────────────────────────────────

def _compute_deltas(rows: list, key: str, clamp_negative: bool = True) -> list[float]:
    """Return per-interval deltas for ``key``. First entry is 0 (no prior
    snapshot). If ``clamp_negative``, treats decreases (e.g. fame resets)
    as 0 — fame only grows.
    """
    out: list[float] = [0.0]
    for prev, curr in zip(rows, rows[1:]):
        d = (curr[key] or 0) - (prev[key] or 0)
        if clamp_negative and d < 0:
            d = 0
        out.append(float(d))
    return out


def _values_for(rows: list, key: str) -> list[float]:
    """Pick the right transformation for a metric: cumulative → deltas,
    otherwise raw."""
    if key in CUMULATIVE_METRICS:
        return _compute_deltas(rows, key, clamp_negative=True)
    return [float(r[key] or 0) for r in rows]


def _bucket_daily(rows: list, key: str) -> tuple[list[datetime.datetime], list[float]]:
    """Bucket cumulative-metric rows into one entry per UTC day.

    For each day we sum the per-snapshot positive deltas, giving a clean
    "fame earned that day" series. Days with no snapshots get a 0 bar so
    the chart shows continuous activity vs. inactivity.
    """
    if len(rows) < 2:
        return [], []
    deltas = _compute_deltas(rows, key, clamp_negative=True)
    dates = _parse_dates(rows)
    by_day: dict[datetime.date, float] = defaultdict(float)
    # First delta is 0 (no prior snapshot) — still attribute it to its day so
    # a single-row tail doesn't get dropped.
    for dt, d in zip(dates, deltas):
        by_day[dt.date()] += d
    if not by_day:
        return [], []
    start = min(by_day.keys())
    end = max(by_day.keys())
    out_dates: list[datetime.datetime] = []
    out_values: list[float] = []
    cur = start
    while cur <= end:
        out_dates.append(datetime.datetime.combine(cur, datetime.time()))
        out_values.append(by_day.get(cur, 0.0))
        cur += datetime.timedelta(days=1)
    return out_dates, out_values


def _plot_daily_bars(ax, dates, values, label, color) -> None:
    """Daily bar chart with value labels above non-zero bars and a soft
    'today' highlight."""
    if not dates:
        _empty_panel(ax)
        return
    today = datetime.datetime.utcnow().date()
    bar_colors = [color if d.date() != today else ACCENT for d in dates]
    bar_alphas = [1.0 if v > 0 else 0.25 for v in values]
    bars = ax.bar(
        dates, values, width=0.8, color=bar_colors,
        edgecolor="none", zorder=3,
    )
    # Apply per-bar alpha (matplotlib doesn't accept alpha as a list directly).
    for b, a in zip(bars, bar_alphas):
        b.set_alpha(a)

    ax.set_title(label, color=TEXT_COLOR, fontsize=10, fontweight="600",
                 loc="left", pad=6)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    ax.set_ylim(bottom=0)
    _style_axes(ax)
    _apply_date_axis(ax, dates)

    # Inline value labels — only on non-zero bars, only every Nth bar if dense.
    nz = [v for v in values if v > 0]
    if not nz:
        return
    max_val = max(nz)
    # Show every bar's label up to ~20 days, otherwise sample to keep readable.
    step = 1 if len(values) <= 20 else max(1, len(values) // 18)
    for i, (b, v) in enumerate(zip(bars, values)):
        if v <= 0 or i % step != 0:
            continue
        ax.text(
            b.get_x() + b.get_width() / 2,
            b.get_height() + max_val * 0.04,
            _fmt_compact(v),
            ha="center", va="bottom",
            fontsize=8, color=MUTED_TEXT, fontweight="600",
        )
    # Headroom so labels don't get clipped.
    ax.set_ylim(0, max_val * 1.18)
