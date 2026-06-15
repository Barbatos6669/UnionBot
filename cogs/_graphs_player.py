"""Player / guild / hourly / K-D chart builders extracted from
``cogs/graphs.py``.

Pure functions: each takes data rows and returns a ``discord.File``.
Imported by ``cogs/graphs.py`` and called from the cog's slash commands
and the live-tracker refresh loop.

Kept under a leading-underscore filename so the cog auto-loader skips it.
"""

from __future__ import annotations

import datetime
from collections import defaultdict

import discord
import matplotlib.ticker

from cogs._graphs_theme import (
    ACCENT,
    CANDLE_DOWN,
    CANDLE_UP,
    CUMULATIVE_METRICS,
    MUTED_TEXT,
    PALETTE,
    PLAYER_METRICS,
    PLAYER_METRIC_BY_KEY,
    TIMEFRAMES,
)
from cogs._graphs_primitives import (
    _bucket_daily,
    _canvas,
    _empty_panel,
    _fig_to_file,
    _fmt_compact,
    _parse_dates,
    _parse_dt,
    _plot_daily_bars,
    _plot_series,
    _style_axes,
    _values_for,
)


# ── player chart ────────────────────────────────────────────────────────────

def _build_player_chart(name: str, rows: list, stat_key: str | None = None) -> discord.File:
    """Daily bar chart per cumulative metric, smooth area chart for snapshot
    metrics (Avg Item Power). Shows actual day-to-day activity instead of a
    near-flat line."""
    if stat_key and stat_key in PLAYER_METRIC_BY_KEY:
        label, color = PLAYER_METRIC_BY_KEY[stat_key]
        fig, ax = _canvas((12, 5), f"{name}  ·  Daily {label}")
        if stat_key in CUMULATIVE_METRICS:
            d_dates, d_vals = _bucket_daily(rows, stat_key)
            _plot_daily_bars(ax, d_dates, d_vals, label, color)
        else:
            _plot_series(ax, _parse_dates(rows), _values_for(rows, stat_key), label, color)
        return _fig_to_file(fig, "player_stat.png")

    fig, axes = _canvas((12, 8), f"{name}  ·  Daily Stat Gains",
                        nrows=3, ncols=2)
    for ax, (label, key, color) in zip(axes.flat, PLAYER_METRICS):
        if key in CUMULATIVE_METRICS:
            d_dates, d_vals = _bucket_daily(rows, key)
            _plot_daily_bars(ax, d_dates, d_vals, label, color)
        else:
            _plot_series(ax, _parse_dates(rows), _values_for(rows, key), label, color)
    return _fig_to_file(fig, "player_stats.png")


# ── guild chart ─────────────────────────────────────────────────────────────

def _build_guild_chart(name: str, rows: list) -> discord.File:
    panels = [
        ("Kill Fame",    "kill_fame",    PALETTE["kill"]),
        ("Death Fame",   "death_fame",   PALETTE["death"]),
        ("Member Count", "member_count", PALETTE["members"]),
    ]

    fig, axes = _canvas((13, 4.2), f"{name}  ·  Guild History (Daily)",
                        nrows=1, ncols=3)
    for ax, (label, key, color) in zip(axes, panels):
        if key == "member_count":
            # Snapshot metric — line+fill is correct (count varies smoothly).
            _plot_series(ax, _parse_dates(rows), [float(r[key] or 0) for r in rows], label, color)
        else:
            d_dates, d_vals = _bucket_daily(rows, key)
            _plot_daily_bars(ax, d_dates, d_vals, label, color)

    return _fig_to_file(fig, "guild_stats.png")


# ── hourly activity chart ───────────────────────────────────────────────────

def _build_hourly_bar(metric_key: str, hourly_rows: list, days: int = 7,
                      mode: str = "avg_per_day") -> discord.File:
    """24-hour line chart (UTC hour-of-day on X, fame earned on Y).

    ``mode='avg_per_day'`` (default) divides each bucket by the number of
    distinct days that contributed, so a single multi-hour grind doesn't
    dominate the guild-wide rhythm. ``mode='sum'`` shows raw window totals.

    Auto switches to log scale when the largest value is more than 100x the
    second-largest so a single grind session doesn't crush every other hour
    to invisibility.
    """
    label, color = PLAYER_METRIC_BY_KEY.get(metric_key, ("Activity", ACCENT))

    by_hour = {int(r["hour"]): float(r["total"] or 0) for r in hourly_rows}
    hours  = list(range(24))
    values = [by_hour.get(h, 0.0) for h in hours]

    mode_label = "Avg / day" if mode == "avg_per_day" else "Total"
    fig, ax = _canvas(
        (13, 5),
        f"Hourly {label}  ·  {mode_label} · Last {days} day{'s' if days != 1 else ''} (UTC)",
    )

    # Soft fill under the line for shape, with a crisp line + dots on top.
    ax.fill_between(hours, values, 0, color=color, alpha=0.18, zorder=2)
    ax.plot(hours, values, color=color, linewidth=2.0, marker="o",
            markersize=5, markerfacecolor=color, markeredgecolor="white",
            markeredgewidth=1.0, zorder=4)

    ax.set_xticks(hours)
    ax.set_xticklabels([f"{h:02d}" for h in hours], fontsize=9)
    ax.set_xlim(-0.5, 23.5)
    ax.set_xlabel("Hour of day (UTC)", color=MUTED_TEXT, fontsize=10, labelpad=8)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    _style_axes(ax)

    # Auto log-scale when one hour dwarfs the rest (single grind sessions
    # otherwise flatten every other hour to a barely-visible sliver).
    nonzero = sorted((v for v in values if v > 0), reverse=True)
    use_log = len(nonzero) >= 2 and nonzero[0] >= 100 * nonzero[1]
    if use_log:
        ax.set_yscale("log")
        ax.set_ylim(bottom=max(1, nonzero[-1] * 0.5))
    else:
        ax.set_ylim(bottom=0)

    # Annotate only the local peaks so labels don't crowd. A peak is any
    # bucket strictly larger than its neighbours and ≥ 5% of the global max.
    max_val = max(values) if values else 0
    if max_val > 0:
        for h in hours:
            v = values[h]
            if v <= 0 or v < max_val * 0.05:
                continue
            left  = values[h - 1] if h > 0 else -1
            right = values[h + 1] if h < 23 else -1
            if v >= left and v >= right:
                ax.annotate(
                    _fmt_compact(v), (h, v),
                    xytext=(0, 6 if not use_log else 4),
                    textcoords="offset points",
                    ha="center", va="bottom",
                    fontsize=8, color=MUTED_TEXT, fontweight="600",
                )

    if use_log:
        ax.text(0.99, 0.97, "log scale", transform=ax.transAxes,
                ha="right", va="top", fontsize=8, color=MUTED_TEXT,
                style="italic", alpha=0.8)

    return _fig_to_file(fig, "hourly_activity.png")


# ── K/D line chart ──────────────────────────────────────────────────────────

def _aggregate_kd(rows: list, timeframe: str) -> list[dict]:
    """Net = kill_fame - death_fame, bucketed by timeframe.

    Each bucket keeps the closing net value (last sample in the bucket) so
    the line traces the player's trajectory through time.
    """
    bucket_fn = TIMEFRAMES[timeframe][1]
    by_bucket: dict[datetime.datetime, list[tuple[datetime.datetime, float]]] = defaultdict(list)
    for r in rows:
        dt  = _parse_dt(r["recorded_at"])
        net = (r.get("kill_fame") or 0) - (r.get("death_fame") or 0)
        by_bucket[bucket_fn(dt)].append((dt, float(net)))

    series = []
    for bucket, samples in sorted(by_bucket.items()):
        samples.sort(key=lambda s: s[0])
        nets = [n for _, n in samples]
        series.append({
            "bucket": bucket,
            "close":  nets[-1],
            "high":   max(nets),
            "low":    min(nets),
        })
    return series


def _build_kd_candles(name: str, rows: list, timeframe: str = "1d") -> discord.File:
    """Line chart of net K/D fame (kill − death) over time."""
    if timeframe not in TIMEFRAMES:
        timeframe = "1d"
    tf_label, _bucket_fn, tick_fmt = TIMEFRAMES[timeframe]
    points = _aggregate_kd(rows, timeframe)

    fig, ax = _canvas((13, 6), f"{name}  ·  {tf_label} K/D Fame  (Kill − Death)")

    if not points:
        _empty_panel(ax)
        return _fig_to_file(fig, "kd_candles.png")

    xs     = list(range(len(points)))
    closes = [p["close"] for p in points]
    highs  = [p["high"]  for p in points]
    lows   = [p["low"]   for p in points]

    # Soft band showing intra-bucket high/low spread; the bold line is the
    # closing net at each bucket so the trajectory is unambiguous.
    ax.fill_between(xs, lows, highs, color=ACCENT, alpha=0.10,
                    linewidth=0, zorder=1, label="High/Low range")
    ax.plot(xs, closes, color=ACCENT, linewidth=2.2, marker="o",
            markersize=5, markerfacecolor=ACCENT, markeredgecolor="white",
            markeredgewidth=1.0, zorder=4, label="Net Fame")

    # Color the area between the line and zero so positive (green) vs
    # negative (red) regions read at a glance.
    ax.fill_between(xs, closes, 0, where=[c >= 0 for c in closes],
                    color=CANDLE_UP, alpha=0.18, interpolate=True, zorder=2)
    ax.fill_between(xs, closes, 0, where=[c < 0 for c in closes],
                    color=CANDLE_DOWN, alpha=0.18, interpolate=True, zorder=2)

    # Zero baseline — break-even between kill & death fame
    ax.axhline(0, color=MUTED_TEXT, linewidth=0.8, linestyle="--", alpha=0.6, zorder=1)

    ax.set_xlim(-0.5, len(points) - 0.5)
    ax.set_xticks(xs)
    step = max(1, len(points) // 12)
    labels = [p["bucket"].strftime(tick_fmt) if (i % step == 0) else ""
              for i, p in enumerate(points)]
    ax.set_xticklabels(labels, fontsize=9, rotation=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    ax.set_ylabel("Net Fame (Kill − Death)", color=MUTED_TEXT, fontsize=10)
    _style_axes(ax)

    ax.text(0.99, 0.97,
            f"{tf_label} buckets    ▲ above zero = net kills    ▼ below = net deaths",
            transform=ax.transAxes, ha="right", va="top",
            fontsize=9, color=MUTED_TEXT)

    return _fig_to_file(fig, "kd_candles.png")
