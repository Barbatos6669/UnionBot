"""Guild-analytics chart builders extracted from ``cogs/graphs.py``.

These functions all consume DB-fetched rows and return a ``discord.File``
ready to attach to a Discord message. They are pure (no Cog state) and
are safe to run on the executor — the cog calls them via
``loop.run_in_executor`` so matplotlib doesn't block the event loop.

Kept under a leading-underscore filename so the cog auto-loader skips
this file (it globs ``cogs/*.py`` and ignores ``_*.py``).
"""

from __future__ import annotations

from collections import defaultdict

import discord
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker

from cogs._graphs_theme import (
    ACCENT, BG_AXES, BG_FIG, GRID_COLOR, MUTED_TEXT, PALETTE, TEXT_COLOR,
)
from cogs._graphs_primitives import (
    _apply_date_axis,
    _empty_panel,
    _fig_to_file,
    _fmt_compact,
    _parse_dt,
    _style_axes,
)

# ── shared accent colours for KPI tiles & status indicators ──────────────────
_OK_COLOR    = "#27ae60"   # green
_WARN_COLOR  = "#f0a500"   # amber
_BAD_COLOR   = "#c0392b"   # red


def _kpi_tile(
    ax,
    value: str,
    label: str,
    sub: str = "",
    accent: str = ACCENT,
) -> None:
    """Render one KPI tile: big number, uppercase label, optional sub-line.

    A coloured stripe runs down the left edge to make the tile read as
    a "card" rather than blank canvas. Designed to live in a thin top
    row of a gridspec — sub-line carries the directional trend / colour.
    """
    ax.set_facecolor(BG_FIG)
    ax.set_xticks([])
    ax.set_yticks([])
    # Explicit data extents — without these, constrained_layout sees an empty
    # axes (no ticks, no spines) and collapses its width, which shoves the
    # rightmost tile off the figure edge.
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    for spine in ax.spines.values():
        spine.set_visible(False)
    # Left accent stripe (drawn in axes coords so it scales with the tile).
    ax.add_patch(plt.Rectangle(
        (0.0, 0.06), 0.014, 0.88, transform=ax.transAxes,
        color=accent, clip_on=False, zorder=5,
    ))
    ax.text(0.06, 0.70, value, color=TEXT_COLOR,
            fontsize=22, fontweight="800",
            transform=ax.transAxes, va="center")
    ax.text(0.06, 0.34, label.upper(), color=MUTED_TEXT,
            fontsize=8.5, fontweight="700",
            transform=ax.transAxes, va="center")
    if sub:
        ax.text(0.06, 0.12, sub, color=accent,
                fontsize=9, fontweight="600",
                transform=ax.transAxes, va="center")


# ── analytics chart builders ──────────────────────────────────────────────────
# Pure functions: take rows, return a discord.File. Run on the executor so the
# event loop isn't blocked by matplotlib.

# Lifecycle ordering used in the donut chart so colors line up with progression.
_LIFECYCLE_ORDER = (
    "Recruit", "Probationary", "Member", "Veteran",
    "Inactive", "Alumni", "Unassigned",
)
_LIFECYCLE_COLORS = {
    "Recruit":      "#9b7bd4",
    "Probationary": "#e6b54a",
    "Member":       "#3fb6a8",
    "Veteran":      "#27ae60",
    "Inactive":     "#8d99ae",
    "Alumni":       "#6b7280",
    "Unassigned":   "#c5cad1",
}

# Friendly labels for known LFG event types; anything else falls through verbatim.
_EVENT_TYPE_LABELS = {
    "ZVZ": "ZvZ", "GANK": "Ganking", "GANKING": "Ganking",
    "MISTS": "Mists", "HG": "Hellgates", "HELLGATES": "Hellgates",
    "ROADS": "Ava Roads", "AVAROADS": "Ava Roads",
    "FAME": "Fame Farm", "GATHER": "Gathering", "CRAFT": "Crafting",
    "HCE": "HCE", "ARENA": "Arena", "GENERAL": "General",
}
_EVENT_TYPE_COLORS = [
    "#e07a5f", "#4a90d9", "#5fbf78", "#e6b54a", "#9b7bd4",
    "#3fb6a8", "#8d99ae", "#c0392b", "#27ae60", "#6b7280",
]

_STAFF_FUNNEL_SERIES = (
    ("Approved", "#27ae60"),
    ("Pending",  "#f0a500"),
    ("Denied",   "#c0392b"),
)


def _draw_staff_pipeline(
    ax,
    funnel: list[tuple[str, int, int, int, int]],
    *,
    compact: bool = False,
) -> None:
    """Draw staff applications as outcomes, with current holders as metadata."""
    ranks = [r[0] for r in funnel]
    applied = [int(r[1]) for r in funnel]
    approved = [int(r[2]) for r in funnel]
    denied = [int(r[3]) for r in funnel]
    held = [int(r[4]) for r in funnel]
    pending = [
        max(0, total - ok - no)
        for total, ok, no in zip(applied, approved, denied)
    ]
    outcome_values = {
        "Approved": approved,
        "Pending": pending,
        "Denied": denied,
    }

    y = list(range(len(ranks)))
    left = [0] * len(ranks)
    bar_height = 0.58 if compact else 0.64
    max_signal = max(applied + held + [1])

    for label, color in _STAFF_FUNNEL_SERIES:
        vals = outcome_values[label]
        ax.barh(
            y, vals, left=left, height=bar_height, color=color,
            label=label, zorder=3, edgecolor=BG_AXES, linewidth=0.8,
        )
        for yy, start, value in zip(y, left, vals):
            if not value:
                continue
            if value >= 1 and (not compact or max_signal <= 8):
                ax.text(
                    start + (value / 2), yy, str(value),
                    ha="center", va="center", color="white",
                    fontsize=7 if compact else 8, fontweight="700",
                    zorder=4,
                )
        left = [l + v for l, v in zip(left, vals)]

    for yy, total, current in zip(y, applied, held):
        if total:
            ax.text(
                total + max_signal * 0.035, yy,
                f"{total} app{'s' if total != 1 else ''}",
                va="center", ha="left", color=MUTED_TEXT,
                fontsize=7 if compact else 8, fontweight="600",
            )
        if current:
            ax.text(
                0.98, yy, f"{current} held",
                transform=ax.get_yaxis_transform(), va="center", ha="right",
                color=ACCENT, fontsize=7 if compact else 8,
                fontweight="700",
                bbox={
                    "boxstyle": "round,pad=0.18",
                    "facecolor": BG_AXES,
                    "edgecolor": "none",
                    "alpha": 0.90,
                },
            )

    ax.set_yticks(y)
    ax.set_yticklabels(ranks, color=TEXT_COLOR, fontsize=9 if compact else 10)
    ax.invert_yaxis()
    ax.set_xlim(0, max_signal * 1.35)
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax.set_xlabel("applications", color=MUTED_TEXT, fontsize=8)
    _style_axes(ax)
    ax.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    ax.grid(axis="y", visible=False)


def _build_roster_chart(profiles: list[dict], staff_holders: dict[str, int]) -> discord.File:
    """Side-by-side: lifecycle donut + staff-rank horizontal bar."""
    counts: dict[str, int] = defaultdict(int)
    for p in profiles:
        key = (p.get("lifecycle_role") or "Unassigned").strip() or "Unassigned"
        counts[key] += 1
    # Order known lifecycles first, then any unknowns alphabetically.
    ordered = [k for k in _LIFECYCLE_ORDER if counts.get(k)]
    extras = sorted(k for k in counts if k not in _LIFECYCLE_ORDER and counts[k])
    ordered.extend(extras)
    sizes = [counts[k] for k in ordered]
    colors = [_LIFECYCLE_COLORS.get(k, ACCENT) for k in ordered]
    total = sum(sizes) or 1

    fig, (ax_pie, ax_bar) = plt.subplots(
        1, 2, figsize=(11, 5.2), constrained_layout=True,
        gridspec_kw={"width_ratios": [1.05, 1]},
    )
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"Roster Health  •  {total} registered",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    # Donut
    ax_pie.set_facecolor(BG_FIG)
    wedges, _texts = ax_pie.pie(
        sizes, colors=colors, startangle=90, counterclock=False,
        wedgeprops={"width": 0.42, "edgecolor": BG_FIG, "linewidth": 2.0},
    )
    ax_pie.text(
        0, 0.06, f"{total}", ha="center", va="center",
        color=TEXT_COLOR, fontsize=22, fontweight="700",
    )
    ax_pie.text(
        0, -0.14, "members", ha="center", va="center",
        color=MUTED_TEXT, fontsize=10,
    )
    ax_pie.set_title("Lifecycle", color=TEXT_COLOR, fontsize=11,
                     fontweight="600", loc="left", pad=6)
    ax_pie.legend(
        wedges,
        [f"{k}  ({counts[k]}, {counts[k] / total:.0%})" for k in ordered],
        loc="center left", bbox_to_anchor=(1.02, 0.5),
        frameon=False, fontsize=9, labelcolor=TEXT_COLOR,
    )

    # Staff-rank bar
    if staff_holders:
        ranks = list(staff_holders.keys())
        held = [staff_holders[r] for r in ranks]
        y = list(range(len(ranks)))
        ax_bar.barh(y, held, color=ACCENT, height=0.6, zorder=3)
        for i, v in enumerate(held):
            ax_bar.text(v + max(held) * 0.02, i, str(v),
                        va="center", color=TEXT_COLOR, fontsize=9, fontweight="600")
        ax_bar.set_yticks(y)
        ax_bar.set_yticklabels(ranks, color=TEXT_COLOR, fontsize=10)
        ax_bar.invert_yaxis()
        ax_bar.set_xlim(0, max(held) * 1.18 if max(held) else 1)
        ax_bar.set_title("Staff ranks held", color=TEXT_COLOR,
                         fontsize=11, fontweight="600", loc="left", pad=6)
        _style_axes(ax_bar)
        ax_bar.grid(axis="x", linestyle="-", linewidth=0.6,
                    color=GRID_COLOR, zorder=0)
        ax_bar.grid(axis="y", visible=False)
    else:
        _empty_panel(ax_bar, "No staff role holders")

    return _fig_to_file(fig, "roster.png")


def _build_content_mix_chart(events: list[dict], weeks: int) -> discord.File:
    """Stacked bar: counts of LFG events grouped by ISO week × event_type."""
    # Bucket events by (iso year, iso week) and event_type.
    by_week: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        try:
            dt = _parse_dt(e["starts_at"])
        except Exception:
            continue
        iso = dt.isocalendar()
        raw = (e.get("event_type") or "GENERAL").upper()
        label = _EVENT_TYPE_LABELS.get(raw, raw.title())
        by_week[(iso[0], iso[1])][label] += 1

    if not by_week:
        fig, ax = plt.subplots(figsize=(10, 4.5), constrained_layout=True)
        fig.patch.set_facecolor(BG_FIG)
        fig.suptitle(f"Content Mix  •  last {weeks} weeks",
                     fontsize=15, fontweight="700", color=TEXT_COLOR,
                     x=0.02, ha="left")
        _empty_panel(ax, "No LFG events scheduled yet")
        return _fig_to_file(fig, "content_mix.png")

    week_keys = sorted(by_week.keys())[-weeks:]
    types = sorted({t for k in week_keys for t in by_week[k].keys()})
    color_for = {t: _EVENT_TYPE_COLORS[i % len(_EVENT_TYPE_COLORS)]
                 for i, t in enumerate(types)}

    x_labels = [f"W{k[1]:02d}" for k in week_keys]
    x = list(range(len(week_keys)))
    bottoms = [0] * len(week_keys)

    fig, ax = plt.subplots(figsize=(11, 5), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    total_events = sum(sum(by_week[k].values()) for k in week_keys)
    fig.suptitle(
        f"Content Mix  •  last {len(week_keys)} weeks  •  {total_events} events",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    for t in types:
        vals = [by_week[k].get(t, 0) for k in week_keys]
        ax.bar(x, vals, bottom=bottoms, color=color_for[t],
               width=0.7, label=t, zorder=3, edgecolor=BG_AXES, linewidth=0.8)
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    # Total label on top of each bar
    for i, total in enumerate(bottoms):
        if total:
            ax.text(i, total + max(bottoms) * 0.02, str(total),
                    ha="center", color=TEXT_COLOR, fontsize=9, fontweight="600")

    ax.set_xticks(x)
    ax.set_xticklabels(x_labels, color=TEXT_COLOR, fontsize=9)
    ax.set_ylabel("events", color=MUTED_TEXT, fontsize=9)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    _style_axes(ax)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0),
              frameon=False, fontsize=9, labelcolor=TEXT_COLOR,
              title="Event type", title_fontsize=9)

    return _fig_to_file(fig, "content_mix.png")


def _build_staff_funnel_chart(funnel: list[tuple[str, int, int, int, int]]) -> discord.File:
    """Staff applications by rank, split by outcome, with current holders noted."""
    if not funnel:
        fig, ax = plt.subplots(figsize=(10, 4.5), constrained_layout=True)
        fig.patch.set_facecolor(BG_FIG)
        fig.suptitle("Staff Application Pipeline",
                     fontsize=15, fontweight="700", color=TEXT_COLOR,
                     x=0.02, ha="left")
        _empty_panel(ax, "No staff applications recorded yet")
        return _fig_to_file(fig, "staff_funnel.png")

    applied = [r[1] for r in funnel]
    n = len(funnel)
    fig, ax = plt.subplots(figsize=(11, max(4.5, 0.7 * n + 2)),
                           constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"Staff Application Pipeline  •  {sum(applied)} total applications",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    _draw_staff_pipeline(ax, funnel, compact=False)
    ax.legend(
        loc="lower center", bbox_to_anchor=(0.5, -0.22),
        frameon=False, fontsize=9, labelcolor=TEXT_COLOR, ncol=3,
    )

    return _fig_to_file(fig, "staff_funnel.png")


def _build_movers_chart(
    movers: list[dict], metric_label: str, days: int,
) -> discord.File:
    """Horizontal bar of top fame gainers in the window."""
    fig, ax = plt.subplots(figsize=(10, max(4.5, 0.45 * len(movers) + 2)),
                           constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"Top Movers  •  {metric_label}  •  last {days}d",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    if not movers:
        _empty_panel(ax, "No movement in this window")
        return _fig_to_file(fig, "movers.png")

    names = [m["name"] or m["discord_id"] for m in movers]
    deltas = [int(m["delta"]) for m in movers]
    y = list(range(len(names)))
    ax.barh(y, deltas, color=PALETTE["kill"], height=0.65, zorder=3)
    for i, v in enumerate(deltas):
        ax.text(v + max(deltas) * 0.01, i, _fmt_compact(v),
                va="center", color=TEXT_COLOR, fontsize=9, fontweight="600")
    ax.set_yticks(y)
    ax.set_yticklabels(names, color=TEXT_COLOR, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlim(0, max(deltas) * 1.15)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    _style_axes(ax)
    ax.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    ax.grid(axis="y", visible=False)
    return _fig_to_file(fig, "movers.png")


def _build_heatmap_chart(
    rows: list[dict], days: int,
) -> discord.File:
    """7×24 grid: weekday × hour-of-day, colored by activity volume.

    SQLite ``strftime('%w', ...)`` gives Sunday=0; we re-order to start the
    week on Monday for readability.
    """
    # Build a 7x24 matrix of zeros, then fill from rows.
    grid = [[0 for _ in range(24)] for _ in range(7)]
    # Map sun-first 0..6 → mon-first 0..6: sun(0)→6, mon(1)→0, ...
    sun_to_mon = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5}
    for r in rows:
        wd = sun_to_mon.get(int(r["weekday"]), 0)
        hr = int(r["hour"])
        grid[wd][hr] = int(r["n"] or 0)

    fig, ax = plt.subplots(figsize=(11, 4.4), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    total = sum(sum(row) for row in grid)
    fig.suptitle(
        f"Activity Heatmap  •  last {days}d  •  {total} player-hours",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    if total == 0:
        _empty_panel(ax, "No activity recorded in this window")
        return _fig_to_file(fig, "heatmap.png")

    im = ax.imshow(
        grid, aspect="auto", cmap="viridis", origin="upper",
        interpolation="nearest",
    )
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 24, 2)],
                       color=MUTED_TEXT, fontsize=9)
    ax.set_yticks(range(7))
    ax.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                       color=TEXT_COLOR, fontsize=10)
    ax.set_xlabel("Hour (UTC)", color=MUTED_TEXT, fontsize=9)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.tick_params(length=0)

    # Annotate the hottest cell — useful "when do we play?" callout.
    flat_max = max(max(row) for row in grid)
    if flat_max > 0:
        for wd in range(7):
            for hr in range(24):
                if grid[wd][hr] == flat_max:
                    ax.text(hr, wd, "★", ha="center", va="center",
                            color="#fff8e0", fontsize=14, fontweight="700")
                    break
            else:
                continue
            break

    cbar = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cbar.outline.set_visible(False)
    cbar.ax.tick_params(colors=MUTED_TEXT, labelsize=8, length=0)
    cbar.set_label("active players", color=MUTED_TEXT, fontsize=9)
    return _fig_to_file(fig, "heatmap.png")


def _build_dashboard_chart(
    profiles: list[dict],
    staff_holders: dict[str, int],
    events: list[dict],
    weeks: int,
    funnel: list[tuple[str, int, int, int, int]],
    movers: list[dict],
    days: int,
    silver: dict | None = None,
    lifecycle_weekly: list[dict] | None = None,
) -> discord.File:
    """One mega-figure: KPI strip + 2x3 grid of guild analytics panels.

    Layout:
        Row 0 (thin):  Roster · Churn · Events/wk · Staff · Approval · Silver
        Row 1:         lifecycle donut · content mix · joiners vs leavers
        Row 2:         staff funnel    · top movers  · silver ledger
    """
    fig = plt.figure(figsize=(20, 12), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    total = len(profiles)
    fig.suptitle(
        f"Guild Analytics Dashboard  •  last {days}d window",
        fontsize=17, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )
    # Nested gridspecs: a thin KPI row that owns its own 6-column layout (so
    # tiles aren't squeezed by the wider panels' tick labels below), plus a
    # 2×3 panel grid below. Using a single 3×6 grid caused the rightmost KPI
    # tile to clip the figure edge because constrained_layout aligned KPI
    # column boundaries with the panels' interior data area, not the figure.
    gs_outer = fig.add_gridspec(2, 1, height_ratios=[0.16, 2.0], hspace=0.10)
    gs_kpi    = gs_outer[0].subgridspec(1, 6, wspace=0.18)
    gs_panels = gs_outer[1].subgridspec(2, 3, hspace=0.30, wspace=0.22)

    def _kpi(col: int):
        return fig.add_subplot(gs_kpi[0, col])

    def _panel(row: int, col: int):
        return fig.add_subplot(gs_panels[row, col])

    # ── KPI strip ────────────────────────────────────────────────────────
    # All values derived from data already passed in — no extra DB queries.
    period_joins  = sum(int(r.get("joins")  or 0) for r in (lifecycle_weekly or []))
    period_leaves = sum(int(r.get("leaves") or 0) for r in (lifecycle_weekly or []))
    period_net    = period_joins - period_leaves
    # Approximate the starting roster as today − net change (good enough for
    # a churn-rate KPI; exact value would require a historical snapshot).
    start_roster  = max(1, total - period_net)
    churn_pct     = (period_leaves / start_roster * 100.0) if start_roster else 0.0

    events_per_week = (len(events) / max(1, weeks)) if events else 0.0

    staff_filled   = sum(staff_holders.values())
    total_approved = sum(r[2] for r in (funnel or []))
    total_denied   = sum(r[3] for r in (funnel or []))
    total_pending  = sum(max(0, r[1] - r[2] - r[3]) for r in (funnel or []))
    decisions      = total_approved + total_denied
    approval_pct   = (total_approved / decisions * 100.0) if decisions else 0.0

    totals       = (silver or {}).get("totals") or {}
    owed_to      = int(totals.get("owed_to_members", 0))
    owed_by      = int(totals.get("owed_by_members", 0))
    silver_float = owed_to + owed_by
    silver_net   = owed_by - owed_to  # +ve = members owe the guild

    def _trend(v: float) -> str:
        if v > 0: return _OK_COLOR
        if v < 0: return _BAD_COLOR
        return MUTED_TEXT

    def _churn_color(pct: float) -> str:
        if pct <= 2.0:  return _OK_COLOR
        if pct <= 5.0:  return _WARN_COLOR
        return _BAD_COLOR

    def _events_color(epw: float) -> str:
        if epw >= 5.0: return _OK_COLOR
        if epw >= 2.0: return _WARN_COLOR
        return _BAD_COLOR

    _kpi_tile(
        _kpi(0),
        f"{total}", "Roster",
        f"{period_net:+d} net this period",
        _trend(period_net),
    )
    _kpi_tile(
        _kpi(1),
        f"{churn_pct:.1f}%", "Churn",
        f"{period_leaves} left · {period_joins} joined",
        _churn_color(churn_pct),
    )
    _kpi_tile(
        _kpi(2),
        f"{events_per_week:.1f}", "Events / week",
        f"{len(events)} over last {weeks}w",
        _events_color(events_per_week),
    )
    _kpi_tile(
        _kpi(3),
        f"{staff_filled}", "Staff filled",
        (f"{total_pending} pending application{'s' if total_pending != 1 else ''}"
         if total_pending else "no pending apps"),
        _WARN_COLOR if total_pending else ACCENT,
    )
    _kpi_tile(
        _kpi(4),
        f"{approval_pct:.0f}%" if decisions else "—",
        "Approval rate",
        (f"{total_approved}/{decisions} decisions"
         if decisions else "no decisions yet"),
        ACCENT if decisions else MUTED_TEXT,
    )
    silver_sub = (
        f"members owe {_fmt_compact(silver_net)}" if silver_net > 0
        else f"guild owes {_fmt_compact(-silver_net)}" if silver_net < 0
        else "balanced"
    )
    _kpi_tile(
        _kpi(5),
        _fmt_compact(silver_float), "Silver float",
        silver_sub,
        _OK_COLOR if silver_net >= 0 else _BAD_COLOR,
    )

    # ── Panel 1: roster donut ─────────────────────────────────────────────
    ax1 = _panel(0, 0)
    counts: dict[str, int] = defaultdict(int)
    for p in profiles:
        key = (p.get("lifecycle_role") or "Unassigned").strip() or "Unassigned"
        counts[key] += 1
    ordered = [k for k in _LIFECYCLE_ORDER if counts.get(k)]
    extras = sorted(k for k in counts if k not in _LIFECYCLE_ORDER and counts[k])
    ordered.extend(extras)
    sizes = [counts[k] for k in ordered]
    colors = [_LIFECYCLE_COLORS.get(k, ACCENT) for k in ordered]
    ax1.set_facecolor(BG_FIG)
    if sizes:
        wedges, _t = ax1.pie(
            sizes, colors=colors, startangle=90, counterclock=False,
            wedgeprops={"width": 0.40, "edgecolor": BG_FIG, "linewidth": 2.0},
        )
        ax1.text(0, 0.06, f"{total}", ha="center", va="center",
                 color=TEXT_COLOR, fontsize=20, fontweight="700")
        ax1.text(0, -0.16, "members", ha="center", va="center",
                 color=MUTED_TEXT, fontsize=9)
        ax1.legend(
            wedges,
            [f"{k}  {counts[k]}" for k in ordered],
            loc="center left", bbox_to_anchor=(1.02, 0.5),
            frameon=False, fontsize=8, labelcolor=TEXT_COLOR,
        )
    else:
        _empty_panel(ax1, "No registered members")
    ax1.set_title("Lifecycle", color=TEXT_COLOR, fontsize=12,
                  fontweight="600", loc="left", pad=4)

    # ── Panel 2: content mix ──────────────────────────────────────────────
    ax2 = _panel(0, 1)
    by_week: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        try:
            dt = _parse_dt(e["starts_at"])
        except Exception:
            continue
        iso = dt.isocalendar()
        raw = (e.get("event_type") or "GENERAL").upper()
        label = _EVENT_TYPE_LABELS.get(raw, raw.title())
        by_week[(iso[0], iso[1])][label] += 1
    if by_week:
        week_keys = sorted(by_week.keys())[-weeks:]
        types = sorted({t for k in week_keys for t in by_week[k].keys()})
        color_for = {t: _EVENT_TYPE_COLORS[i % len(_EVENT_TYPE_COLORS)]
                     for i, t in enumerate(types)}
        x = list(range(len(week_keys)))
        bottoms = [0] * len(week_keys)
        for t in types:
            vals = [by_week[k].get(t, 0) for k in week_keys]
            ax2.bar(x, vals, bottom=bottoms, color=color_for[t],
                    width=0.7, label=t, zorder=3,
                    edgecolor=BG_AXES, linewidth=0.8)
            bottoms = [b + v for b, v in zip(bottoms, vals)]
        ax2.set_xticks(x)
        ax2.set_xticklabels([f"W{k[1]:02d}" for k in week_keys],
                            color=TEXT_COLOR, fontsize=8)
        ax2.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax2)
        ax2.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0),
                   frameon=False, fontsize=8, labelcolor=TEXT_COLOR)
    else:
        _empty_panel(ax2, "No LFG events")
    ax2.set_title(f"Content mix ({len(by_week)}w)", color=TEXT_COLOR,
                  fontsize=12, fontweight="600", loc="left", pad=4)

    # ── Panel 3: staff funnel ─────────────────────────────────────────────
    ax3 = _panel(1, 0)
    if funnel:
        _draw_staff_pipeline(ax3, funnel, compact=True)
        ax3.legend(
            loc="lower center", bbox_to_anchor=(0.5, -0.18),
            frameon=False, fontsize=8, labelcolor=TEXT_COLOR, ncol=3,
        )
    else:
        _empty_panel(ax3, "No staff applications")
    ax3.set_title("Staff pipeline", color=TEXT_COLOR, fontsize=12,
                  fontweight="600", loc="left", pad=4)

    # ── Panel 4: top movers ───────────────────────────────────────────────
    ax4 = _panel(1, 1)
    if movers:
        names = [m["name"] or m["discord_id"] for m in movers][:10]
        deltas = [int(m["delta"]) for m in movers][:10]
        y = list(range(len(names)))
        ax4.barh(y, deltas, color=PALETTE["kill"], height=0.65, zorder=3)
        for i, v in enumerate(deltas):
            ax4.text(v + max(deltas) * 0.01, i, _fmt_compact(v),
                     va="center", color=TEXT_COLOR, fontsize=8,
                     fontweight="600")
        ax4.set_yticks(y)
        ax4.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax4.invert_yaxis()
        ax4.set_xlim(0, max(deltas) * 1.18)
        ax4.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax4)
        ax4.grid(axis="x", linestyle="-", linewidth=0.6,
                 color=GRID_COLOR, zorder=0)
        ax4.grid(axis="y", visible=False)
    else:
        _empty_panel(ax4, "No fame movement")
    ax4.set_title(f"Top kill-fame movers ({days}d)", color=TEXT_COLOR,
                  fontsize=12, fontweight="600", loc="left", pad=4)

    # ── Panel 5: joiners vs leavers (weekly) ─────────────────────────────
    ax5 = _panel(0, 2)
    rows_lc = lifecycle_weekly or []
    if rows_lc:
        labels = [r["week_start"] for r in rows_lc]
        joins  = [int(r.get("joins")  or 0) for r in rows_lc]
        leaves = [int(r.get("leaves") or 0) for r in rows_lc]
        x = list(range(len(labels)))
        bar_w = 0.4
        ax5.bar([xi - bar_w/2 for xi in x], joins,  width=bar_w,
                color="#27ae60", label="Joined", zorder=3)
        ax5.bar([xi + bar_w/2 for xi in x], leaves, width=bar_w,
                color="#c0392b", label="Left",   zorder=3)
        # Net line on a twin axis so it's readable regardless of bar scale.
        net = [j - l for j, l in zip(joins, leaves)]
        ax5b = ax5.twinx()
        net_line, = ax5b.plot(x, net, color=ACCENT, linewidth=1.8,
                              marker="o", markersize=4, zorder=4,
                              label="Net (joins − leaves)")
        ax5b.axhline(0, color=MUTED_TEXT, linewidth=0.6,
                     linestyle="--", alpha=0.5)
        ax5b.tick_params(axis="y", colors=ACCENT, labelsize=8)
        ax5b.set_ylabel("Net (joins − leaves)", color=ACCENT, fontsize=8)
        # Pad the net axis so the line doesn't crash into the bar tops.
        if net:
            lo, hi = min(net + [0]), max(net + [0])
            pad = max(1, int(round((hi - lo) * 0.15)))
            ax5b.set_ylim(lo - pad, hi + pad)
        for spine in ax5b.spines.values():
            spine.set_visible(False)
        # Compact week labels (drop the year, keep MM-DD).
        ax5.set_xticks(x)
        ax5.set_xticklabels([(s or "")[5:] for s in labels],
                            color=TEXT_COLOR, fontsize=8, rotation=0)
        ax5.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax5)
        # Combined legend so the Net line shows alongside the bars. Split
        # across two rows (bars on top, net line below) so it never collides
        # with the title or summary stat regardless of legend label length.
        bar_handles, bar_labels = ax5.get_legend_handles_labels()
        ax5.legend(bar_handles + [net_line],
                   bar_labels + [net_line.get_label()],
                   loc="upper left", frameon=False, fontsize=8,
                   labelcolor=TEXT_COLOR, ncol=2,
                   handlelength=1.4, columnspacing=1.0, handletextpad=0.4,
                   borderaxespad=0.2)
        # Headroom on both y-axes so the legend never sits on top of the
        # tallest bar / net peak.
        cur_lo, cur_hi = ax5.get_ylim()
        ax5.set_ylim(cur_lo, cur_hi * 1.35 if cur_hi > 0 else cur_hi)
        nlo, nhi = ax5b.get_ylim()
        ax5b.set_ylim(nlo, nhi + max(1, (nhi - nlo) * 0.25))
        # Summary stat is rolled into the title (set below) instead of an
        # in-axes annotation — keeps the plot area clean.
        total_j = sum(joins); total_l = sum(leaves)
        title_suffix = (
            f"  ·  +{total_j} joined · −{total_l} left  ({total_j - total_l:+d} net)"
        )
    else:
        _empty_panel(ax5, "No join/leave history yet")
        title_suffix = ""
    ax5.set_title(f"Joiners vs leavers{title_suffix}",
                  color=TEXT_COLOR, fontsize=12,
                  fontweight="600", loc="left", pad=4)

    # ── Panel 6: silver ledger ───────────────────────────────────────────
    ax6 = _panel(1, 2)
    sv = silver or {}
    creditors = sv.get("creditors") or []
    debtors   = sv.get("debtors")   or []
    if creditors or debtors:
        # Stack top creditors (positive) above top debtors (negative) in one
        # diverging bar with zero centred — guild's perspective: bars to the
        # right are silver the guild owes; bars to the left are silver owed
        # back to the guild.
        rows = [(c["name"] or c["discord_id"], int(c["balance"]))
                for c in creditors]
        rows += [(d["name"] or d["discord_id"], int(d["balance"]))
                 for d in debtors]
        # Largest absolute value at top so the eye reads worst→best.
        rows.sort(key=lambda r: r[1], reverse=True)
        names  = [r[0] for r in rows]
        values = [r[1] for r in rows]
        colors_ = ["#27ae60" if v >= 0 else "#c0392b" for v in values]
        y = list(range(len(names)))
        ax6.barh(y, values, color=colors_, height=0.65, zorder=3)
        max_abs = max((abs(v) for v in values), default=1)
        for i, v in enumerate(values):
            offset = max_abs * 0.02
            ax6.text(v + (offset if v >= 0 else -offset), i,
                     _fmt_compact(abs(v)), va="center",
                     ha="left" if v >= 0 else "right",
                     color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax6.axvline(0, color=MUTED_TEXT, linewidth=0.8, alpha=0.6, zorder=2)
        ax6.set_yticks(y)
        ax6.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax6.invert_yaxis()
        pad = max_abs * 0.25
        ax6.set_xlim(-max_abs - pad, max_abs + pad)
        ax6.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax6)
        ax6.grid(axis="x", linestyle="-", linewidth=0.6,
                 color=GRID_COLOR, zorder=0)
        ax6.grid(axis="y", visible=False)
        totals = sv.get("totals") or {}
        owed_to = totals.get("owed_to_members", 0)
        owed_by = totals.get("owed_by_members", 0)
        ax6.text(0.99, 0.97,
                 f"Guild owes {_fmt_compact(owed_to)}  ·  "
                 f"Members owe {_fmt_compact(owed_by)}",
                 transform=ax6.transAxes, ha="right", va="top",
                 fontsize=8, color=MUTED_TEXT, fontweight="600")
    else:
        _empty_panel(ax6, "No silver balances")
    ax6.set_title("Silver ledger  ·  ▶ guild owes  ◀ owed back",
                  color=TEXT_COLOR, fontsize=12, fontweight="600",
                  loc="left", pad=4)

    return _fig_to_file(fig, "dashboard.png")


# ── shared dashboard helpers (used by finance / recruitment / combat) ────────

def _dashboard_layout(
    fig, suptitle: str, days: int,
) -> tuple[list, list[list]]:
    """Build the standard "KPI strip + 2×3 panel grid" layout used by all
    dashboard variants. Returns ``(kpi_axes, panel_axes)`` where:

    - ``kpi_axes`` is a list of 6 axes (one per KPI tile, left→right)
    - ``panel_axes`` is a 2-row × 3-col list-of-lists of panel axes

    Callers populate KPI tiles via :func:`_kpi_tile` and draw onto the
    panel axes directly. Identical nesting strategy to ``_build_dashboard_chart``
    so panel tick-label geometry can't squeeze the KPI strip off the figure.
    """
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"{suptitle}  •  last {days}d window",
        fontsize=17, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )
    gs_outer = fig.add_gridspec(2, 1, height_ratios=[0.16, 2.0], hspace=0.10)
    gs_kpi    = gs_outer[0].subgridspec(1, 6, wspace=0.18)
    gs_panels = gs_outer[1].subgridspec(2, 3, hspace=0.30, wspace=0.22)
    kpi_axes = [fig.add_subplot(gs_kpi[0, c]) for c in range(6)]
    panel_axes = [
        [fig.add_subplot(gs_panels[r, c]) for c in range(3)]
        for r in range(2)
    ]
    return kpi_axes, panel_axes


def _panel_title(ax, text: str) -> None:
    ax.set_title(text, color=TEXT_COLOR, fontsize=12,
                 fontweight="600", loc="left", pad=4)


# ── Finance dashboard ────────────────────────────────────────────────────────

def _build_finance_dashboard_chart(
    *,
    treasury_latest: dict | None,
    treasury_history: list[dict],
    silver_debts: list[dict],
    unpaid_aged: list[dict],
    revenue_rows: list[dict],
    revenue_30d: int,
    days: int,
) -> discord.File:
    """Treasury + silver flow analytics.

    KPIs: Treasury · Outstanding (guild owes) · Members owe · Aged ≥14d · 30d revenue · Creditors
    Panels: Treasury trend · Top creditors · Top debtors · Aged debts · Revenue by source · Recent revenue trend
    """
    fig = plt.figure(figsize=(20, 12), constrained_layout=True)
    kpis, panels = _dashboard_layout(fig, "Finance Dashboard", days)

    # ── derive aggregates ────────────────────────────────────────────────
    treasury = int((treasury_latest or {}).get("balance") or 0)
    creditors = [d for d in silver_debts if int(d["silver_balance"]) > 0]
    debtors   = [d for d in silver_debts if int(d["silver_balance"]) < 0]
    outstanding = sum(int(d["silver_balance"]) for d in creditors)
    members_owe = sum(-int(d["silver_balance"]) for d in debtors)
    aged_14 = [r for r in unpaid_aged if int(r.get("days_waiting") or 0) >= 14]

    # ── KPI strip ────────────────────────────────────────────────────────
    _kpi_tile(kpis[0], _fmt_compact(treasury), "Treasury",
              f"recorded {(treasury_latest or {}).get('date', '—')}", ACCENT)
    _kpi_tile(kpis[1], _fmt_compact(outstanding), "Outstanding",
              f"guild owes {len(creditors)} member{'s' if len(creditors) != 1 else ''}",
              _BAD_COLOR if outstanding > treasury else _WARN_COLOR if outstanding else _OK_COLOR)
    _kpi_tile(kpis[2], _fmt_compact(members_owe), "Members owe",
              f"{len(debtors)} debtor{'s' if len(debtors) != 1 else ''}",
              _OK_COLOR if members_owe else MUTED_TEXT)
    _kpi_tile(kpis[3], f"{len(aged_14)}", "Aged ≥14d",
              "needs payout" if aged_14 else "all current",
              _BAD_COLOR if aged_14 else _OK_COLOR)
    _kpi_tile(kpis[4], _fmt_compact(revenue_30d), "Revenue (30d)",
              f"{len(revenue_rows)} entries",
              _OK_COLOR if revenue_30d > 0 else MUTED_TEXT)
    coverage = (treasury / outstanding) if outstanding else None
    _kpi_tile(
        kpis[5],
        f"{coverage:.1f}×" if coverage is not None else "∞",
        "Coverage",
        "treasury ÷ outstanding" if coverage is not None else "no debt",
        _OK_COLOR if coverage is None or coverage >= 1.0
        else _WARN_COLOR if coverage >= 0.5 else _BAD_COLOR,
    )

    # ── Panel 1: treasury trend ──────────────────────────────────────────
    ax1 = panels[0][0]
    if treasury_history:
        xs = [_parse_dt(r["date"] + "T00:00:00+00:00") for r in treasury_history]
        ys = [int(r["balance"]) for r in treasury_history]
        ax1.plot(xs, ys, color=ACCENT, linewidth=1.8, marker="o",
                 markersize=3.5, zorder=3)
        ax1.fill_between(xs, ys, color=ACCENT, alpha=0.12, zorder=2)
        ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _apply_date_axis(ax1, xs)
        _style_axes(ax1)
        ax1.grid(axis="y", linestyle="-", linewidth=0.6,
                 color=GRID_COLOR, zorder=0)
        # Min / max annotations
        lo_i = ys.index(min(ys)); hi_i = ys.index(max(ys))
        for idx, color, tag in [(lo_i, _BAD_COLOR, "lo"), (hi_i, _OK_COLOR, "hi")]:
            ax1.annotate(_fmt_compact(ys[idx]), (xs[idx], ys[idx]),
                         textcoords="offset points", xytext=(0, 8),
                         ha="center", color=color, fontsize=8, fontweight="700")
    else:
        _empty_panel(ax1, "No treasury history")
    _panel_title(ax1, "Treasury trend")

    # ── Panel 2: top creditors (guild owes) ──────────────────────────────
    ax2 = panels[0][1]
    top_cred = creditors[:10]
    if top_cred:
        names = [(c["albion_name"] or c["username"] or c["discord_id"])
                 for c in top_cred]
        vals  = [int(c["silver_balance"]) for c in top_cred]
        y = list(range(len(names)))
        ax2.barh(y, vals, color=_WARN_COLOR, height=0.65, zorder=3)
        for i, v in enumerate(vals):
            ax2.text(v + max(vals) * 0.01, i, _fmt_compact(v),
                     va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax2.set_yticks(y); ax2.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax2.invert_yaxis()
        ax2.set_xlim(0, max(vals) * 1.18)
        ax2.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax2)
        ax2.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax2.grid(axis="y", visible=False)
    else:
        _empty_panel(ax2, "No outstanding payouts")
    _panel_title(ax2, "Top creditors (guild owes)")

    # ── Panel 3: aged unpaid silver ──────────────────────────────────────
    ax3 = panels[0][2]
    if unpaid_aged:
        top_aged = sorted(unpaid_aged,
                          key=lambda r: int(r.get("days_waiting") or 0),
                          reverse=True)[:10]
        names = [(r["albion_name"] or r["username"] or r["discord_id"])
                 for r in top_aged]
        ages  = [int(r.get("days_waiting") or 0) for r in top_aged]
        bals  = [int(r.get("balance") or 0) for r in top_aged]
        y = list(range(len(names)))
        colors_ = [_BAD_COLOR if a >= 14 else _WARN_COLOR if a >= 7 else _OK_COLOR
                   for a in ages]
        ax3.barh(y, ages, color=colors_, height=0.65, zorder=3)
        for i, (a, b) in enumerate(zip(ages, bals)):
            ax3.text(a + max(ages) * 0.01, i, f"{a}d · {_fmt_compact(b)}",
                     va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax3.set_yticks(y); ax3.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax3.invert_yaxis()
        ax3.set_xlim(0, max(ages) * 1.22)
        ax3.set_xlabel("days waiting", color=MUTED_TEXT, fontsize=8)
        _style_axes(ax3)
        ax3.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax3.grid(axis="y", visible=False)
    else:
        _empty_panel(ax3, "Nothing aged out")
    _panel_title(ax3, "Aged unpaid (oldest credit)")

    # ── Panel 4: revenue by source ───────────────────────────────────────
    ax4 = panels[1][0]
    by_source: dict[str, int] = defaultdict(int)
    for r in revenue_rows:
        by_source[(r.get("source") or "other").upper()] += int(r.get("amount") or 0)
    if by_source:
        items = sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)
        labels = [k for k, _ in items]
        vals   = [v for _, v in items]
        colors_ = [_EVENT_TYPE_COLORS[i % len(_EVENT_TYPE_COLORS)] for i in range(len(labels))]
        ax4.set_facecolor(BG_FIG)
        wedges, _t = ax4.pie(
            vals, colors=colors_, startangle=90, counterclock=False,
            wedgeprops={"width": 0.40, "edgecolor": BG_FIG, "linewidth": 2.0},
        )
        ax4.text(0, 0.06, _fmt_compact(sum(vals)), ha="center", va="center",
                 color=TEXT_COLOR, fontsize=18, fontweight="700")
        ax4.text(0, -0.16, "total", ha="center", va="center",
                 color=MUTED_TEXT, fontsize=9)
        ax4.legend(
            wedges, [f"{lbl}  {_fmt_compact(v)}" for lbl, v in items],
            loc="center left", bbox_to_anchor=(1.02, 0.5),
            frameon=False, fontsize=8, labelcolor=TEXT_COLOR,
        )
    else:
        _empty_panel(ax4, "No revenue logged")
    _panel_title(ax4, "Revenue by source")

    # ── Panel 5: silver flow (treasury Δ + outstanding) ──────────────────
    ax5 = panels[1][1]
    if len(treasury_history) >= 2:
        xs = [_parse_dt(r["date"] + "T00:00:00+00:00") for r in treasury_history]
        ys = [int(r["balance"]) for r in treasury_history]
        deltas = [ys[i] - ys[i - 1] for i in range(1, len(ys))]
        dxs = xs[1:]
        bcols = [_OK_COLOR if d >= 0 else _BAD_COLOR for d in deltas]
        ax5.bar(dxs, deltas, color=bcols, width=0.7, zorder=3)
        ax5.axhline(0, color=MUTED_TEXT, linewidth=0.8, alpha=0.6)
        ax5.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _apply_date_axis(ax5, dxs)
        _style_axes(ax5)
        ax5.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        net = sum(deltas)
        ax5.text(0.99, 0.97, f"net Δ {_fmt_compact(net):>}",
                 transform=ax5.transAxes, ha="right", va="top",
                 fontsize=9, fontweight="700",
                 color=_OK_COLOR if net >= 0 else _BAD_COLOR)
    else:
        _empty_panel(ax5, "Need ≥2 treasury snapshots")
    _panel_title(ax5, "Treasury Δ (day-over-day)")

    # ── Panel 6: top debtors (members owe) ───────────────────────────────
    ax6 = panels[1][2]
    top_deb = debtors[-10:][::-1]  # most-negative first
    if top_deb:
        names = [(d["albion_name"] or d["username"] or d["discord_id"])
                 for d in top_deb]
        vals  = [-int(d["silver_balance"]) for d in top_deb]  # positive bars
        y = list(range(len(names)))
        ax6.barh(y, vals, color=_OK_COLOR, height=0.65, zorder=3)
        for i, v in enumerate(vals):
            ax6.text(v + max(vals) * 0.01, i, _fmt_compact(v),
                     va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax6.set_yticks(y); ax6.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax6.invert_yaxis()
        ax6.set_xlim(0, max(vals) * 1.18)
        ax6.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax6)
        ax6.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax6.grid(axis="y", visible=False)
    else:
        _empty_panel(ax6, "No member debts")
    _panel_title(ax6, "Top debtors (members owe)")

    return _fig_to_file(fig, "finance_dashboard.png")


# ── Recruitment & Retention dashboard ────────────────────────────────────────

def _build_recruitment_dashboard_chart(
    *,
    funnel: dict[str, int],
    cohorts: dict[str, dict[int, int]],
    profiles: list[dict],
    lifecycle_weekly: list[dict],
    pending_apps: list[dict],
    inactive_30d: list[dict],
    inactive_60d: list[dict],
    streaks: list[dict],
    days: int,
) -> discord.File:
    """Recruitment funnel · cohort retention · lifecycle distribution · joins/leaves · pending apps · top streaks."""
    fig = plt.figure(figsize=(20, 12), constrained_layout=True)
    kpis, panels = _dashboard_layout(fig, "Recruitment & Retention", days)

    total = len(profiles)
    period_joins  = sum(int(r.get("joins")  or 0) for r in (lifecycle_weekly or []))
    period_leaves = sum(int(r.get("leaves") or 0) for r in (lifecycle_weekly or []))
    period_net    = period_joins - period_leaves
    active_30d    = int(funnel.get("active_30d", 0))
    activity_pct  = (active_30d / total * 100.0) if total else 0.0
    at_risk_30    = len(inactive_30d or [])
    at_risk_60    = len(inactive_60d or [])

    # ── KPI strip ────────────────────────────────────────────────────────
    _kpi_tile(kpis[0], f"{total}", "Registered",
              f"{period_net:+d} this period",
              _OK_COLOR if period_net > 0 else _BAD_COLOR if period_net < 0 else MUTED_TEXT)
    _kpi_tile(kpis[1], f"{period_joins}", "Joined",
              f"over {len(lifecycle_weekly or [])} weeks", _OK_COLOR)
    _kpi_tile(kpis[2], f"{period_leaves}", "Left",
              "this period", _BAD_COLOR if period_leaves else _OK_COLOR)
    _kpi_tile(kpis[3], f"{len(pending_apps)}", "Pending apps",
              "awaiting review" if pending_apps else "all reviewed",
              _WARN_COLOR if pending_apps else _OK_COLOR)
    _kpi_tile(kpis[4], f"{activity_pct:.0f}%", "Active 30d",
              f"{active_30d}/{total}",
              _OK_COLOR if activity_pct >= 60 else _WARN_COLOR if activity_pct >= 40 else _BAD_COLOR)
    _kpi_tile(kpis[5], f"{at_risk_60}", "At risk 60d+",
              f"+{at_risk_30 - at_risk_60} idle 30–60d",
              _BAD_COLOR if at_risk_60 else _WARN_COLOR if at_risk_30 else _OK_COLOR)

    # ── Panel 1: recruitment funnel ──────────────────────────────────────
    ax1 = panels[0][0]
    stages = [
        ("Discord", int(funnel.get("discord_members", 0))),
        ("Registered", int(funnel.get("registered", 0))),
        ("Verified", int(funnel.get("verified", 0))),
        ("In home guild", int(funnel.get("in_home_guild", 0))),
        ("Active 30d", int(funnel.get("active_30d", 0))),
    ]
    if any(v for _, v in stages):
        labels = [s[0] for s in stages]
        vals   = [s[1] for s in stages]
        y = list(range(len(labels)))
        max_v = max(vals + [1])
        colors_ = [_EVENT_TYPE_COLORS[i % len(_EVENT_TYPE_COLORS)] for i in range(len(labels))]
        ax1.barh(y, vals, color=colors_, height=0.65, zorder=3)
        for i, v in enumerate(vals):
            pct = (v / vals[0] * 100.0) if vals[0] else 0.0
            ax1.text(v + max_v * 0.01, i, f"{v}  ({pct:.0f}%)",
                     va="center", color=TEXT_COLOR, fontsize=9, fontweight="600")
        ax1.set_yticks(y); ax1.set_yticklabels(labels, color=TEXT_COLOR, fontsize=9)
        ax1.invert_yaxis()
        ax1.set_xlim(0, max_v * 1.22)
        _style_axes(ax1)
        ax1.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax1.grid(axis="y", visible=False)
    else:
        _empty_panel(ax1, "No funnel data")
    _panel_title(ax1, "Recruitment funnel")

    # ── Panel 2: cohort retention ────────────────────────────────────────
    ax2 = panels[0][1]
    if cohorts:
        cohort_keys = sorted(cohorts.keys())[-6:]  # last 6 cohorts
        max_week = max((max(d.keys()) for d in cohorts.values()), default=0)
        weeks_axis = list(range(max_week + 1))
        for i, ck in enumerate(cohort_keys):
            data = cohorts[ck]
            base = int(data.get(0, 0)) or 1
            ys = [(int(data.get(w, 0)) / base * 100.0) for w in weeks_axis]
            ax2.plot(weeks_axis, ys, marker="o", markersize=4, linewidth=1.6,
                     color=_EVENT_TYPE_COLORS[i % len(_EVENT_TYPE_COLORS)],
                     label=f"{ck}  (n={base})", zorder=3)
        ax2.set_xlabel("weeks since join", color=MUTED_TEXT, fontsize=8)
        ax2.set_ylabel("% active", color=MUTED_TEXT, fontsize=8)
        ax2.set_ylim(0, 105)
        ax2.set_xticks(weeks_axis)
        _style_axes(ax2)
        ax2.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax2.legend(loc="lower left", frameon=False, fontsize=7,
                   labelcolor=TEXT_COLOR, ncol=2,
                   handlelength=1.2, columnspacing=0.8)
    else:
        _empty_panel(ax2, "No cohort data yet")
    _panel_title(ax2, "Cohort retention (% active / week)")

    # ── Panel 3: lifecycle distribution ──────────────────────────────────
    ax3 = panels[0][2]
    counts: dict[str, int] = defaultdict(int)
    for p in profiles:
        key = (p.get("lifecycle_role") or "Unassigned").strip() or "Unassigned"
        counts[key] += 1
    ordered = [k for k in _LIFECYCLE_ORDER if counts.get(k)]
    extras = sorted(k for k in counts if k not in _LIFECYCLE_ORDER and counts[k])
    ordered.extend(extras)
    sizes = [counts[k] for k in ordered]
    colors_ = [_LIFECYCLE_COLORS.get(k, ACCENT) for k in ordered]
    ax3.set_facecolor(BG_FIG)
    if sizes:
        wedges, _t = ax3.pie(
            sizes, colors=colors_, startangle=90, counterclock=False,
            wedgeprops={"width": 0.40, "edgecolor": BG_FIG, "linewidth": 2.0},
        )
        ax3.text(0, 0.06, f"{total}", ha="center", va="center",
                 color=TEXT_COLOR, fontsize=20, fontweight="700")
        ax3.text(0, -0.16, "members", ha="center", va="center",
                 color=MUTED_TEXT, fontsize=9)
        ax3.legend(
            wedges, [f"{k}  {counts[k]}" for k in ordered],
            loc="center left", bbox_to_anchor=(1.02, 0.5),
            frameon=False, fontsize=8, labelcolor=TEXT_COLOR,
        )
    else:
        _empty_panel(ax3, "No registered members")
    _panel_title(ax3, "Lifecycle distribution")

    # ── Panel 4: joins vs leaves weekly ──────────────────────────────────
    ax4 = panels[1][0]
    if lifecycle_weekly:
        labels = [r["week_start"] for r in lifecycle_weekly]
        joins  = [int(r.get("joins") or 0) for r in lifecycle_weekly]
        leaves = [int(r.get("leaves") or 0) for r in lifecycle_weekly]
        x = list(range(len(labels)))
        bar_w = 0.4
        ax4.bar([xi - bar_w/2 for xi in x], joins, width=bar_w,
                color=_OK_COLOR, label="Joined", zorder=3)
        ax4.bar([xi + bar_w/2 for xi in x], leaves, width=bar_w,
                color=_BAD_COLOR, label="Left", zorder=3)
        ax4.set_xticks(x)
        ax4.set_xticklabels([(s or "")[5:] for s in labels],
                            color=TEXT_COLOR, fontsize=8)
        _style_axes(ax4)
        ax4.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax4.legend(loc="upper left", frameon=False, fontsize=8,
                   labelcolor=TEXT_COLOR, ncol=2)
        cur_lo, cur_hi = ax4.get_ylim()
        ax4.set_ylim(cur_lo, cur_hi * 1.30 if cur_hi > 0 else cur_hi)
    else:
        _empty_panel(ax4, "No join/leave history")
    _panel_title(ax4, "Joins vs leaves (weekly)")

    # ── Panel 5: pending apps by age ─────────────────────────────────────
    ax5 = panels[1][1]
    if pending_apps:
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        rows_ = []
        for app in pending_apps[:10]:
            applied = app.get("applied_at")
            if not applied:
                continue
            try:
                dt = _parse_dt(applied)
                age_days = max(0, int((now - dt).total_seconds() // 86400))
            except Exception:
                continue
            name = app.get("albion_name") or app.get("discord_id")
            rows_.append((name, age_days))
        rows_.sort(key=lambda r: r[1], reverse=True)
        if rows_:
            names = [r[0] for r in rows_]
            ages  = [r[1] for r in rows_]
            y = list(range(len(names)))
            colors_ = [_BAD_COLOR if a >= 14 else _WARN_COLOR if a >= 7 else _OK_COLOR
                       for a in ages]
            ax5.barh(y, ages, color=colors_, height=0.65, zorder=3)
            for i, a in enumerate(ages):
                ax5.text(a + max(ages + [1]) * 0.02, i, f"{a}d",
                         va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
            ax5.set_yticks(y); ax5.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
            ax5.invert_yaxis()
            ax5.set_xlim(0, max(ages + [1]) * 1.22)
            ax5.set_xlabel("days waiting", color=MUTED_TEXT, fontsize=8)
            _style_axes(ax5)
            ax5.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
            ax5.grid(axis="y", visible=False)
        else:
            _empty_panel(ax5, "No dated applications")
    else:
        _empty_panel(ax5, "No pending applications")
    _panel_title(ax5, "Pending applications by age")

    # ── Panel 6: top active streaks ──────────────────────────────────────
    ax6 = panels[1][2]
    top_streaks = (streaks or [])[:10]
    if top_streaks:
        names = [(s.get("albion_name") or s.get("discord_id")) for s in top_streaks]
        cur   = [int(s.get("current_streak") or s.get("activity_streak_days") or 0)
                 for s in top_streaks]
        best  = [int(s.get("best_streak") or s.get("activity_streak_best") or 0)
                 for s in top_streaks]
        y = list(range(len(names)))
        bar_h = 0.36
        ax6.barh([yi - bar_h/2 for yi in y], cur,  height=bar_h,
                 color=_OK_COLOR, label="Current", zorder=3)
        ax6.barh([yi + bar_h/2 for yi in y], best, height=bar_h,
                 color=ACCENT, label="Best", zorder=3)
        for i, (c, b) in enumerate(zip(cur, best)):
            ax6.text(c + max(best + [1]) * 0.01, i - bar_h/2, f"{c}d",
                     va="center", color=TEXT_COLOR, fontsize=8)
            ax6.text(b + max(best + [1]) * 0.01, i + bar_h/2, f"{b}d",
                     va="center", color=TEXT_COLOR, fontsize=8)
        ax6.set_yticks(y); ax6.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax6.invert_yaxis()
        ax6.set_xlim(0, max(best + [1]) * 1.18)
        ax6.set_xlabel("days", color=MUTED_TEXT, fontsize=8)
        _style_axes(ax6)
        ax6.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax6.grid(axis="y", visible=False)
        ax6.legend(loc="lower right", frameon=False, fontsize=8,
                   labelcolor=TEXT_COLOR, ncol=2)
    else:
        _empty_panel(ax6, "No streak data")
    _panel_title(ax6, "Top activity streaks")

    return _fig_to_file(fig, "recruitment_dashboard.png")


# ── Combat Performance dashboard ─────────────────────────────────────────────

def _build_combat_dashboard_chart(
    *,
    profiles: list[dict],
    movers: list[dict],
    heatmap_rows: list[dict],
    hourly_rows: list[dict],
    attendance_rows: list[dict],
    voice_rows: list[dict],
    days: int,
) -> discord.File:
    """K/D · top movers · activity heatmap · hourly fame · attendance · voice."""
    fig = plt.figure(figsize=(20, 12), constrained_layout=True)
    kpis, panels = _dashboard_layout(fig, "Combat Performance", days)

    fighters = [p for p in profiles if int(p.get("kill_fame") or 0) > 0]
    total_kf = sum(int(p.get("kill_fame") or 0) for p in profiles)
    total_df = sum(int(p.get("death_fame") or 0) for p in profiles)
    guild_kd = (total_kf / total_df) if total_df else (float("inf") if total_kf else 0.0)
    ip_values = [int(p.get("average_item_power") or 0) for p in profiles
                 if int(p.get("average_item_power") or 0) > 0]
    avg_ip = (sum(ip_values) / len(ip_values)) if ip_values else 0.0
    period_kf = sum(int(m.get("delta") or 0) for m in (movers or []))
    top_fighter = (movers or [{}])[0].get("name") or "—" if movers else "—"
    events_attended = sum(1 for r in (attendance_rows or [])
                          if int(r.get("attended") or 0) > 0)

    # ── KPI strip ────────────────────────────────────────────────────────
    _kpi_tile(kpis[0], f"{len(fighters)}", "Active fighters",
              f"{len(profiles)} registered", ACCENT)
    _kpi_tile(kpis[1], _fmt_compact(period_kf), "Fame ({}d)".format(days),
              f"top: {top_fighter}",
              _OK_COLOR if period_kf > 0 else MUTED_TEXT)
    _kpi_tile(kpis[2],
              f"{guild_kd:.2f}" if guild_kd != float("inf") else "∞",
              "Guild K/D",
              f"{_fmt_compact(total_kf)} / {_fmt_compact(total_df)}",
              _OK_COLOR if guild_kd >= 1.5 else _WARN_COLOR if guild_kd >= 1.0 else _BAD_COLOR)
    _kpi_tile(kpis[3], f"{avg_ip:.0f}", "Avg IP",
              f"{len(ip_values)} with gear data",
              _OK_COLOR if avg_ip >= 1300 else _WARN_COLOR if avg_ip >= 1100 else _BAD_COLOR)
    _kpi_tile(kpis[4], f"{events_attended}", "Event attends",
              f"{len(attendance_rows or [])} event(s)",
              _OK_COLOR if events_attended else MUTED_TEXT)
    voice_hours = sum(int(v.get("seconds") or 0) for v in (voice_rows or [])) / 3600
    _kpi_tile(kpis[5], f"{voice_hours:.0f}h", "Voice ({}d)".format(days),
              f"top {len(voice_rows or [])} members",
              _OK_COLOR if voice_hours else MUTED_TEXT)

    # ── Panel 1: top kill-fame movers ────────────────────────────────────
    ax1 = panels[0][0]
    if movers:
        top = (movers or [])[:10]
        names = [m["name"] or m["discord_id"] for m in top]
        deltas = [int(m["delta"]) for m in top]
        y = list(range(len(names)))
        ax1.barh(y, deltas, color=PALETTE.get("kill", ACCENT), height=0.65, zorder=3)
        for i, v in enumerate(deltas):
            ax1.text(v + max(deltas) * 0.01, i, _fmt_compact(v),
                     va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax1.set_yticks(y); ax1.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax1.invert_yaxis()
        ax1.set_xlim(0, max(deltas) * 1.18)
        ax1.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax1)
        ax1.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax1.grid(axis="y", visible=False)
    else:
        _empty_panel(ax1, "No fame movement")
    _panel_title(ax1, f"Top kill-fame movers ({days}d)")

    # ── Panel 2: activity heatmap (7×24) ─────────────────────────────────
    ax2 = panels[0][1]
    if heatmap_rows:
        grid = [[0] * 24 for _ in range(7)]
        for r in heatmap_rows:
            wd = int(r.get("weekday") or 0) % 7
            hr = int(r.get("hour") or 0) % 24
            grid[wd][hr] = int(r.get("n") or 0)
        import numpy as _np
        arr = _np.array(grid)
        im = ax2.imshow(arr, aspect="auto", cmap="YlOrRd",
                        interpolation="nearest")
        ax2.set_xticks(range(0, 24, 3))
        ax2.set_xticklabels([f"{h:02d}" for h in range(0, 24, 3)],
                            color=TEXT_COLOR, fontsize=8)
        ax2.set_yticks(range(7))
        ax2.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                            color=TEXT_COLOR, fontsize=8)
        ax2.tick_params(colors=MUTED_TEXT)
        for s in ax2.spines.values():
            s.set_visible(False)
        cbar = fig.colorbar(im, ax=ax2, shrink=0.7, pad=0.02)
        cbar.ax.tick_params(colors=MUTED_TEXT, labelsize=7)
        cbar.outline.set_visible(False)  # type: ignore[union-attr]
    else:
        _empty_panel(ax2, "No activity data")
    _panel_title(ax2, "When the guild plays (UTC)")

    # ── Panel 3: hourly kill-fame distribution ───────────────────────────
    ax3 = panels[0][2]
    if hourly_rows:
        hours = [int(r.get("hour") or 0) for r in hourly_rows]
        vals  = [float(r.get("total") or 0) for r in hourly_rows]
        # Ensure all 24 hours represented
        by_h = dict(zip(hours, vals))
        xs = list(range(24))
        ys = [by_h.get(h, 0.0) for h in xs]
        ax3.bar(xs, ys, color=PALETTE.get("kill", ACCENT), width=0.85, zorder=3)
        ax3.set_xticks(range(0, 24, 3))
        ax3.set_xticklabels([f"{h:02d}" for h in range(0, 24, 3)],
                            color=TEXT_COLOR, fontsize=8)
        ax3.set_xlim(-0.5, 23.5)
        ax3.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
        _style_axes(ax3)
        ax3.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    else:
        _empty_panel(ax3, "No hourly data")
    _panel_title(ax3, "Avg kill-fame per UTC hour")

    # ── Panel 4: K/D leaderboard ─────────────────────────────────────────
    ax4 = panels[1][0]
    kd_rows = []
    for p in profiles:
        kf = int(p.get("kill_fame") or 0); df = int(p.get("death_fame") or 0)
        if kf + df < 10_000:  # filter noise — needs some skin in the game
            continue
        kd = kf / df if df else float("inf")
        kd_rows.append((p.get("albion_name") or p.get("username")
                        or p.get("discord_id"), kd, kf, df))
    kd_rows.sort(key=lambda r: r[1] if r[1] != float("inf") else 9999, reverse=True)
    kd_rows = kd_rows[:10]
    if kd_rows:
        names = [r[0] for r in kd_rows]
        kds   = [r[1] if r[1] != float("inf") else 999 for r in kd_rows]
        y = list(range(len(names)))
        colors_ = [_OK_COLOR if k >= 2.0 else _WARN_COLOR if k >= 1.0 else _BAD_COLOR
                   for k in kds]
        ax4.barh(y, kds, color=colors_, height=0.65, zorder=3)
        for i, (k, (_, _, kf, df)) in enumerate(zip(kds, kd_rows)):
            label = "∞" if k == 999 else f"{k:.2f}"
            ax4.text(k + max(kds) * 0.01, i,
                     f"{label}  ({_fmt_compact(kf)} / {_fmt_compact(df)})",
                     va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax4.set_yticks(y); ax4.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax4.invert_yaxis()
        ax4.set_xlim(0, max(kds) * 1.30)
        ax4.set_xlabel("K/D ratio", color=MUTED_TEXT, fontsize=8)
        _style_axes(ax4)
        ax4.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax4.grid(axis="y", visible=False)
    else:
        _empty_panel(ax4, "Not enough combat data")
    _panel_title(ax4, "K/D leaderboard")

    # ── Panel 5: attendance by event type ────────────────────────────────
    ax5 = panels[1][1]
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"signed": 0, "attended": 0})
    for r in (attendance_rows or []):
        et = (r.get("event_type") or "OTHER").upper()
        by_type[et]["signed"]   += int(r.get("signed") or 0)
        by_type[et]["attended"] += int(r.get("attended") or 0)
    if by_type:
        items = sorted(by_type.items(),
                       key=lambda kv: kv[1]["attended"], reverse=True)
        labels = [_EVENT_TYPE_LABELS.get(k, k.title()) for k, _ in items]
        signed   = [v["signed"]   for _, v in items]
        attended = [v["attended"] for _, v in items]
        x = list(range(len(labels)))
        bar_w = 0.4
        ax5.bar([xi - bar_w/2 for xi in x], signed,   width=bar_w,
                color=MUTED_TEXT, label="Signed", zorder=3)
        ax5.bar([xi + bar_w/2 for xi in x], attended, width=bar_w,
                color=_OK_COLOR, label="Attended", zorder=3)
        ax5.set_xticks(x); ax5.set_xticklabels(labels, color=TEXT_COLOR,
                                                fontsize=8, rotation=15, ha="right")
        _style_axes(ax5)
        ax5.grid(axis="y", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax5.legend(loc="upper right", frameon=False, fontsize=8,
                   labelcolor=TEXT_COLOR, ncol=2)
        cur_lo, cur_hi = ax5.get_ylim()
        ax5.set_ylim(cur_lo, cur_hi * 1.25 if cur_hi > 0 else cur_hi)
    else:
        _empty_panel(ax5, "No attendance data")
    _panel_title(ax5, "Attendance by event type")

    # ── Panel 6: top voice participants ──────────────────────────────────
    ax6 = panels[1][2]
    top_voice_ = (voice_rows or [])[:10]
    if top_voice_:
        names = [(v.get("albion_name") or v.get("username") or v.get("discord_id"))
                 for v in top_voice_]
        hours = [int(v.get("seconds") or 0) / 3600 for v in top_voice_]
        y = list(range(len(names)))
        ax6.barh(y, hours, color=ACCENT, height=0.65, zorder=3)
        for i, h in enumerate(hours):
            ax6.text(h + max(hours) * 0.01, i, f"{h:.1f}h",
                     va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
        ax6.set_yticks(y); ax6.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
        ax6.invert_yaxis()
        ax6.set_xlim(0, max(hours) * 1.18)
        ax6.set_xlabel("hours", color=MUTED_TEXT, fontsize=8)
        _style_axes(ax6)
        ax6.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
        ax6.grid(axis="y", visible=False)
    else:
        _empty_panel(ax6, "No voice data")
    _panel_title(ax6, f"Top voice ({days}d)")

    return _fig_to_file(fig, "combat_dashboard.png")


def _build_cohort_retention_chart(
    cohorts: dict[str, dict[int, int]], retention_weeks: int,
) -> discord.File:
    """Line per cohort: % of members active each week after registration."""
    fig, ax = plt.subplots(figsize=(11, 5.2), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"Cohort Retention  •  {len(cohorts)} cohorts  •  {retention_weeks}w follow-up",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    if not cohorts:
        _empty_panel(ax, "Not enough registration history yet")
        return _fig_to_file(fig, "cohort.png")

    # Sort cohorts oldest-first; assign a colormap shade per cohort for an
    # intuitive "older = darker" feel.
    sorted_keys = sorted(cohorts.keys())
    cmap = plt.get_cmap("viridis")
    n = len(sorted_keys)

    x = list(range(retention_weeks + 1))
    for i, key in enumerate(sorted_keys):
        bucket = cohorts[key]
        total = bucket.get(0, 0) or 1
        pct = [100.0 * bucket.get(w, 0) / total for w in x]
        color = cmap(0.15 + 0.7 * (i / max(1, n - 1)))
        ax.plot(x, pct, marker="o", linewidth=2.0, color=color,
                markersize=5, label=f"{key}  (n={total})", zorder=3)

    ax.set_xticks(x)
    ax.set_xticklabels([f"W{w}" for w in x], color=TEXT_COLOR, fontsize=9)
    ax.set_ylabel("% active", color=MUTED_TEXT, fontsize=9)
    ax.set_ylim(0, 105)
    ax.axhline(100, color=GRID_COLOR, linewidth=0.8, zorder=1)
    _style_axes(ax)
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0),
              frameon=False, fontsize=8, labelcolor=TEXT_COLOR,
              title="Cohort (week of)", title_fontsize=9)
    return _fig_to_file(fig, "cohort.png")


def _build_standing_chart(
    movers: list[dict], target_id: str, metric_label: str, days: int,
) -> discord.File:
    """Sorted bar of all players' deltas in the window, target highlighted."""
    fig, ax = plt.subplots(figsize=(11, max(4.5, 0.32 * len(movers) + 2)),
                           constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"Standing  •  {metric_label}  •  last {days}d",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )
    if not movers:
        _empty_panel(ax, "No movement in this window")
        return _fig_to_file(fig, "standing.png")

    # Sort descending so rank 1 is at top.
    movers = sorted(movers, key=lambda m: int(m["delta"] or 0), reverse=True)
    target_idx = next(
        (i for i, m in enumerate(movers) if str(m["discord_id"]) == str(target_id)),
        None,
    )
    names = [m["name"] or m["discord_id"] for m in movers]
    deltas = [int(m["delta"] or 0) for m in movers]

    # Shrink to top 25 + the target row if outside the top.
    cap = 25
    if target_idx is not None and target_idx >= cap:
        names = names[: cap - 1] + ["…", names[target_idx]]
        deltas = deltas[: cap - 1] + [0, deltas[target_idx]]
        target_idx_render = cap  # last row
    else:
        names = names[:cap]
        deltas = deltas[:cap]
        target_idx_render = target_idx

    colors = [MUTED_TEXT] * len(names)
    if target_idx_render is not None and 0 <= target_idx_render < len(names):
        colors[target_idx_render] = ACCENT

    y = list(range(len(names)))
    ax.barh(y, deltas, color=colors, height=0.65, zorder=3)
    for i, v in enumerate(deltas):
        if v:
            ax.text(v + max(deltas) * 0.01, i, _fmt_compact(v),
                    va="center", color=TEXT_COLOR, fontsize=8, fontweight="600")
    ax.set_yticks(y)
    ax.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, max(deltas + [1]) * 1.18)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
    _style_axes(ax)
    ax.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    ax.grid(axis="y", visible=False)

    # Add a "rank X of Y" caption when target is identifiable.
    if target_idx is not None:
        ax.text(
            0.99, 0.02,
            f"Rank {target_idx + 1} of {len(movers)}",
            transform=ax.transAxes, ha="right", va="bottom",
            color=ACCENT, fontsize=10, fontweight="700",
        )
    return _fig_to_file(fig, "standing.png")


def _build_attendance_chart(event: dict, counts: dict) -> discord.File:
    """Single-event attendance funnel: signed → attended / not marked."""
    fig, ax = plt.subplots(figsize=(10, 4.5), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    title = event.get("title") or f"Event #{event.get('id')}"
    fig.suptitle(
        f"Attendance  •  {title}",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )
    signed = int(counts.get("signed", 0))
    if signed == 0:
        _empty_panel(ax, "No signups for this event")
        return _fig_to_file(fig, "attendance.png")

    attended = int(counts.get("attended", 0))
    not_marked = max(0, signed - attended)
    labels = ["Signed up", "Attended", "Not marked attended"]
    vals = [
        signed,
        attended,
        not_marked,
    ]
    colors = ["#9b7bd4", "#27ae60", "#8d99ae"]
    y = list(range(len(labels)))
    ax.barh(y, vals, color=colors, height=0.55, zorder=3)
    for i, v in enumerate(vals):
        ax.text(v + max(vals) * 0.01, i, str(v),
                va="center", color=TEXT_COLOR, fontsize=10, fontweight="600")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, color=TEXT_COLOR, fontsize=11)
    ax.invert_yaxis()
    ax.set_xlim(0, max(vals) * 1.18)
    _style_axes(ax)
    ax.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    ax.grid(axis="y", visible=False)

    if signed:
        rate = 100.0 * attended / max(1, signed)
        ax.text(
            0.99, 0.04,
            f"Attendance captured: {rate:.0f}%",
            transform=ax.transAxes, ha="right", va="bottom",
            color=ACCENT, fontsize=11, fontweight="700",
        )
    return _fig_to_file(fig, "attendance.png")


def _build_attendance_trend_chart(rows: list[dict], weeks: int) -> discord.File:
    """Per-event attended/signed rate (%) over time, with a rolling average."""
    fig, ax = plt.subplots(figsize=(11, 5.2), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        f"Attendance Trend  •  last {weeks}w  •  {len(rows)} events",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )
    if not rows:
        _empty_panel(ax, "No attended events in window")
        return _fig_to_file(fig, "attendance_trend.png")

    dates = []
    rates = []
    sizes = []
    for r in rows:
        try:
            dt = _parse_dt(r["starts_at"])
        except Exception:
            continue
        attended = int(r.get("attended") or 0)
        signed = int(r.get("signed") or 0)
        if signed <= 0 or attended <= 0:
            continue
        dates.append(dt)
        rates.append(100.0 * attended / signed)
        sizes.append(40 + 12 * signed)

    if not dates:
        _empty_panel(ax, "No attended events yet")
        return _fig_to_file(fig, "attendance_trend.png")

    ax.scatter(dates, rates, s=sizes, color=ACCENT, alpha=0.55,
               edgecolor=TEXT_COLOR, linewidth=0.4, zorder=3)
    # Rolling mean (window=4) — looks like a momentum line.
    if len(rates) >= 3:
        win = min(4, len(rates))
        ma = []
        for i in range(len(rates)):
            lo = max(0, i - win + 1)
            ma.append(sum(rates[lo:i + 1]) / (i - lo + 1))
        ax.plot(dates, ma, color=PALETTE["kill"], linewidth=2.0, zorder=4)
    ax.set_ylabel("% attended of signed", color=MUTED_TEXT, fontsize=9)
    ax.set_ylim(0, 105)
    ax.axhline(100, color=GRID_COLOR, linewidth=0.8, zorder=1)
    _style_axes(ax)
    _apply_date_axis(ax, dates)
    return _fig_to_file(fig, "attendance_trend.png")


def _build_recruitment_funnel_chart(funnel: dict[str, int]) -> discord.File:
    """Horizontal funnel: discord → registered → verified → in-guild → active."""
    fig, ax = plt.subplots(figsize=(10, 4.6), constrained_layout=True)
    fig.patch.set_facecolor(BG_FIG)
    fig.suptitle(
        "Recruitment Funnel",
        fontsize=15, fontweight="700", color=TEXT_COLOR, x=0.02, ha="left",
    )

    stages = [
        ("Discord members", "discord_members", "#9b7bd4"),
        ("Registered",      "registered",      "#3498db"),
        ("Verified",        "verified",        "#27ae60"),
        ("In home guild",   "in_home_guild",   ACCENT),
        ("Active (30d)",    "active_30d",      "#e67e22"),
    ]
    labels = [s[0] for s in stages]
    vals = [int(funnel.get(s[1], 0)) for s in stages]
    colors = [s[2] for s in stages]

    if not any(vals):
        _empty_panel(ax, "No data — register someone first.")
        return _fig_to_file(fig, "recruitment_funnel.png")

    y = list(range(len(labels)))
    ax.barh(y, vals, color=colors, height=0.62, zorder=3)
    top = vals[0] or 1
    for i, v in enumerate(vals):
        pct = 100.0 * v / top
        ax.text(
            v + max(vals) * 0.012, i,
            f"{v}  ({pct:.0f}%)",
            va="center", color=TEXT_COLOR, fontsize=10, fontweight="600",
        )
    ax.set_yticks(y)
    ax.set_yticklabels(labels, color=TEXT_COLOR, fontsize=11)
    ax.invert_yaxis()
    ax.set_xlim(0, max(vals) * 1.22)
    _style_axes(ax)
    ax.grid(axis="x", linestyle="-", linewidth=0.6, color=GRID_COLOR, zorder=0)
    ax.grid(axis="y", visible=False)

    drop_off = vals[0] - vals[-1] if vals[0] else 0
    ax.text(
        0.99, 0.04,
        f"Drop-off: {drop_off}  ({100.0 * drop_off / top:.0f}%)",
        transform=ax.transAxes, ha="right", va="bottom",
        color=ACCENT, fontsize=11, fontweight="700",
    )
    return _fig_to_file(fig, "recruitment_funnel.png")
