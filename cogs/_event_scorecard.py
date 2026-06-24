"""Scorecard graph rendering for post-event reports.

This module is intentionally presentation-only. The event report builder owns
Discord embeds and data collection; this file turns already-collected report
rows into the PNG scorecard officers read after content.
"""
from __future__ import annotations

import datetime as dt
import traceback
from typing import Any

import discord

from cogs._graphs_primitives import _empty_panel, _fig_to_file, _fmt_compact, _style_axes
from cogs._graphs_theme import ACCENT, PALETTE, TEXT_COLOR
from debug import error_log


UTC = dt.timezone.utc


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_window(event: dict) -> tuple[dt.datetime | None, dt.datetime | None, dt.datetime | None, dt.datetime | None]:
    starts_at = _parse_dt(event.get("starts_at"))
    ends_at = _parse_dt(event.get("ends_at"))
    prep = int(event.get("prep_minutes") or 0)
    review = int(event.get("review_minutes") or 0)
    report_start = starts_at - dt.timedelta(minutes=max(0, prep)) if starts_at else None
    report_end = ends_at + dt.timedelta(minutes=max(0, review)) if ends_at else None
    return starts_at, ends_at, report_start, report_end


def _short_label(value: str, limit: int = 18) -> str:
    value = str(value or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _annotate_vertical_bars(ax, values: list[int | float]) -> None:
    if not values:
        return
    span = max(abs(float(v)) for v in values) or 1.0
    for idx, raw in enumerate(values):
        value = float(raw or 0)
        pad = span * 0.03
        va = "bottom" if value >= 0 else "top"
        y = value + pad if value >= 0 else value - pad
        ax.text(
            idx,
            y,
            _fmt_compact(value),
            ha="center",
            va=va,
            color=TEXT_COLOR,
            fontsize=8,
            fontweight="700",
        )


def _draw_stat_card(ax, x: float, y: float, w: float, h: float, *, label: str, value: str, color: str, sub: str = "") -> None:
    import matplotlib.patches as patches

    ax.add_patch(
        patches.FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.015,rounding_size=0.025",
            facecolor="#ffffff",
            edgecolor="#dde1e6",
            linewidth=0.8,
            zorder=2,
        )
    )
    ax.text(
        x + 0.04,
        y + h - 0.09,
        label,
        color="#6b7280",
        fontsize=8,
        fontweight="700",
        transform=ax.transAxes,
        zorder=3,
    )
    ax.text(
        x + 0.04,
        y + h - 0.20,
        value,
        color=color,
        fontsize=13,
        fontweight="900",
        transform=ax.transAxes,
        zorder=3,
    )
    if sub:
        ax.text(
            x + 0.04,
            y + 0.06,
            sub,
            color="#6b7280",
            fontsize=7,
            fontweight="600",
            transform=ax.transAxes,
            zorder=3,
        )


def build_event_scorecard_graph(
    event: dict,
    *,
    attendance_counts: dict[str, int],
    snapshot_flow: list[dict],
    stat_totals: dict[str, int],
    player_deltas: list[dict],
    kills: list[dict],
    deaths: list[dict],
    kill_fame_value: int,
    death_fame_value: int,
    net_fame_value: int,
    killboard_lookup_enabled: bool = True,
    loot_summary: dict | None = None,
    albionbb_summary: dict | None = None,
) -> discord.File | None:
    """Build a compact officer-facing event scorecard.

    The graph is intentionally a summary, not a source of truth. The embed
    keeps the exact notes/details while the image lets officers scan the event
    outcome quickly.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.ticker
    except Exception as exc:  # noqa: BLE001
        error_log(f"event report graph import failed: {exc!r}")
        return None

    try:
        event_id = int(event.get("id") or 0)
        title = _short_label(str(event.get("title") or "LFG"), 58)
        fig, axes = plt.subplots(2, 3, figsize=(17, 8.6), constrained_layout=True)
        fig.patch.set_facecolor("#eef1f5")
        fig.suptitle(
            f"Event Scorecard  •  #{event_id} {title}",
            fontsize=16,
            fontweight="800",
            color=TEXT_COLOR,
            x=0.02,
            ha="left",
        )

        ax_attendance, ax_battles, ax_stats, ax_roles, ax_value, ax_players = axes.flat

        # Voice attendance over time. This shows the real VC population curve,
        # which is more useful than a static funnel when officers want to know
        # whether people stayed for the run or dropped after form-up.
        flow_points: list[tuple[dt.datetime, int]] = []
        for row in snapshot_flow or []:
            ts = _parse_dt(row.get("snapshot_at"))
            if not ts:
                continue
            flow_points.append((ts, int(row.get("members") or 0)))
        if not flow_points:
            _empty_panel(ax_attendance, "No VC flow captured")
        else:
            starts_at, ends_at, report_start, report_end = _event_window(event)
            ref = report_start or flow_points[0][0]
            xs = [(ts - ref).total_seconds() / 60.0 for ts, _count in flow_points]
            ys = [count for _ts, count in flow_points]
            peak = max(ys) if ys else 0
            first = ys[0] if ys else 0
            final = ys[-1] if ys else 0
            avg = sum(ys) / len(ys) if ys else 0.0
            retention = (100.0 * final / peak) if peak else 0.0
            drop = max(0, peak - final)

            ax_attendance.plot(xs, ys, color=ACCENT, linewidth=2.2, marker="o", markersize=3.8, zorder=4)
            ax_attendance.fill_between(xs, ys, 0, color=ACCENT, alpha=0.18, linewidth=0, zorder=2)
            marker_top = max(peak * 1.15, peak + 2)
            visible_marker_xs: list[float] = []
            for marker, label, color in (
                (starts_at, "start", "#9b7bd4"),
                (ends_at, "end", "#8d99ae"),
                (report_end, "close", "#e6b54a"),
            ):
                if not marker:
                    continue
                mx = (marker - ref).total_seconds() / 60.0
                if min(xs) - 8 <= mx <= max(xs) + 8:
                    visible_marker_xs.append(mx)
                    ax_attendance.axvline(mx, color=color, linestyle="--", linewidth=1.0, alpha=0.75, zorder=3)
                    ax_attendance.text(
                        mx,
                        marker_top,
                        label,
                        color=color,
                        fontsize=7,
                        fontweight="800",
                        ha="center",
                        va="bottom",
                    )
            for x, y in zip(xs, ys):
                if y in (peak, final) or len(xs) <= 6:
                    ax_attendance.text(
                        x,
                        y + max(peak * 0.04, 0.35),
                        str(y),
                        color=TEXT_COLOR,
                        fontsize=7,
                        fontweight="700",
                        ha="center",
                        va="bottom",
                    )
            ax_attendance.set_ylim(0, marker_top * 1.08)
            x_bounds = xs + visible_marker_xs
            if min(x_bounds) == max(x_bounds):
                ax_attendance.set_xlim(xs[0] - 5, xs[0] + 5)
            else:
                ax_attendance.set_xlim(min(x_bounds) - 4, max(x_bounds) + 4)
            ax_attendance.set_title("VC Attendance Flow", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
            ax_attendance.xaxis.set_major_formatter(
                matplotlib.ticker.FuncFormatter(lambda value, _pos=None: f"{int(value)}m")
            )
            _style_axes(ax_attendance)
            ax_attendance.grid(axis="x", linestyle="-", linewidth=0.6, color="#dde1e6", zorder=0)
            ax_attendance.text(
                0.98,
                0.05,
                f"Peak {peak} • Final {final} • Retention {retention:.0f}%\n"
                f"Avg {avg:.1f} • Drop-off {drop} • First {first}",
                transform=ax_attendance.transAxes,
                ha="right",
                va="bottom",
                color=TEXT_COLOR,
                fontsize=8,
                fontweight="800",
            )
            signed = int(attendance_counts.get("signups") or 0)
            confirmed = int(attendance_counts.get("confirmed") or 0)
            if signed or confirmed:
                ax_attendance.text(
                    0.02,
                    0.05,
                    f"Signed {signed} • VC confirmed {confirmed}",
                    transform=ax_attendance.transAxes,
                    ha="left",
                    va="bottom",
                    color=TEXT_COLOR,
                    fontsize=8,
                    fontweight="700",
                )

        # AlbionBB battle timeline. This shows how many battleboard events the
        # attendees touched during the run and whether those moments were tiny
        # skirmishes or meaningful fame swings.
        bb = albionbb_summary or {}
        bb_battles = [
            battle for battle in (bb.get("battles") or [])
            if _parse_dt(battle.get("startedAt"))
        ]
        if not bb.get("enabled") or not bb_battles:
            _empty_panel(ax_battles, "No AlbionBB battle matches")
        else:
            starts_at, _ends_at, report_start, _report_end = _event_window(event)
            ref = report_start or starts_at or _parse_dt(bb_battles[0].get("startedAt"))
            xs = [
                (_parse_dt(battle.get("startedAt")) - ref).total_seconds() / 60.0
                for battle in bb_battles
                if _parse_dt(battle.get("startedAt")) and ref
            ]
            fame_values = [int(battle.get("totalFame") or 0) for battle in bb_battles[: len(xs)]]
            kill_values = [int(battle.get("totalKills") or 0) for battle in bb_battles[: len(xs)]]
            player_values = [int(battle.get("totalPlayers") or 0) for battle in bb_battles[: len(xs)]]
            colors = [
                PALETTE["kill"] if fame >= 250_000 else PALETTE["members"]
                for fame in fame_values
            ]
            if xs and fame_values:
                ax_battles.bar(xs, fame_values, color=colors, width=3.5, alpha=0.78, zorder=3)
                ax_battles.set_ylim(0, max(fame_values) * 1.22)
                ax_battles.set_title("AlbionBB Battle Timeline", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
                ax_battles.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
                ax_battles.xaxis.set_major_formatter(
                    matplotlib.ticker.FuncFormatter(lambda value, _pos=None: f"{int(value)}m")
                )
                _style_axes(ax_battles)
                for x, fame, battle_kills, players in zip(xs, fame_values, kill_values, player_values):
                    if fame <= 0:
                        continue
                    ax_battles.text(
                        x,
                        fame + max(fame_values) * 0.04,
                        f"{_fmt_compact(fame)} fame\n{battle_kills} kills • {players} players",
                        ha="center",
                        va="bottom",
                        color=TEXT_COLOR,
                        fontsize=6,
                        fontweight="700",
                    )
                ax_battles.text(
                    0.02,
                    0.95,
                    f"{len(bb.get('battle_ids') or [])} battle(s) • "
                    f"{_fmt_compact((bb.get('totals') or {}).get('kill_fame'))} attendee kill fame",
                    transform=ax_battles.transAxes,
                    ha="left",
                    va="top",
                    color=TEXT_COLOR,
                    fontsize=8,
                    fontweight="800",
                    bbox={
                        "facecolor": "#f7f8fa",
                        "edgecolor": "#dde1e6",
                        "boxstyle": "round,pad=0.25",
                        "alpha": 0.88,
                    },
                )
                ax_battles.set_xlabel("Minutes after report window start", color="#6b7280", fontsize=7)
            else:
                _empty_panel(ax_battles, "No AlbionBB battle timeline")

        # Stat growth from stored profile snapshots.
        stat_labels = ["PvP", "Deaths", "PvE", "Gather", "Craft"]
        stat_keys = ["kill_fame", "death_fame", "pve_total", "gather_all", "crafting_fame"]
        stat_colors = [
            PALETTE["kill"],
            PALETTE["death"],
            PALETTE["pve"],
            PALETTE["gather"],
            PALETTE["craft"],
        ]
        stat_values = [int(stat_totals.get(key) or 0) for key in stat_keys]
        if max(stat_values or [0]) <= 0:
            _empty_panel(ax_stats, "No stat movement captured yet")
        else:
            ax_stats.bar(stat_labels, stat_values, color=stat_colors, width=0.62, zorder=3)
            ax_stats.set_title("Fame / Stat Growth", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
            ax_stats.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
            _style_axes(ax_stats)
            _annotate_vertical_bars(ax_stats, stat_values)

        # Role/IP mix from AlbionBB rows. The bar length uses unique players
        # because raw AlbionBB player-battle rows are too easy to misread as
        # headcount. Battle appearances remain visible as sample context.
        role_unique = dict((bb.get("role_unique_players") or {})) if bb.get("enabled") else {}
        role_appearances = dict((bb.get("role_counts") or {})) if bb.get("enabled") else {}
        if not role_unique:
            _empty_panel(ax_roles, "No AlbionBB role/IP data")
        else:
            role_items = sorted(
                role_unique.items(),
                key=lambda item: (int(item[1] or 0), int(role_appearances.get(item[0]) or 0)),
                reverse=True,
            )[:8]
            roles = [_short_label(role.title(), 12) for role, _count in role_items]
            counts = [int(count or 0) for _role, count in role_items]
            y = list(range(len(roles)))
            ax_roles.barh(y, counts, color=PALETTE["members"], height=0.58, zorder=3)
            ax_roles.set_yticks(y)
            ax_roles.set_yticklabels(roles, color=TEXT_COLOR, fontsize=8)
            ax_roles.invert_yaxis()
            ax_roles.set_title("Roles by Unique Players", color=TEXT_COLOR, fontsize=11, fontweight="700", loc="left")
            _style_axes(ax_roles)
            ax_roles.grid(axis="x", linestyle="-", linewidth=0.6, color="#dde1e6", zorder=0)
            max_count = max(counts) if counts else 1
            ax_roles.set_xlim(0, max_count * 1.75)
            avg_by_role = bb.get("role_avg_ip") or {}
            for idx, ((role, _count), count) in enumerate(zip(role_items, counts)):
                appearances = int(role_appearances.get(role) or 0)
                ip_text = f" • {int(avg_by_role.get(role) or 0)} avg IP" if avg_by_role.get(role) else ""
                ax_roles.text(
                    count + max_count * 0.03,
                    idx,
                    f"{count} player{'s' if count != 1 else ''}"
                    f" • {appearances} appearance{'s' if appearances != 1 else ''}"
                    f"{ip_text}",
                    va="center",
                    color=TEXT_COLOR,
                    fontsize=7,
                    fontweight="700",
                )
            avg_ip = int((bb.get("totals") or {}).get("avg_ip") or 0)
            ax_roles.text(
                0.98,
                0.05,
                f"Avg attendee IP {avg_ip} • appearances = player-battle rows"
                if avg_ip else "Appearances = player-battle rows",
                transform=ax_roles.transAxes,
                ha="right",
                va="bottom",
                color=TEXT_COLOR,
                fontsize=8,
                fontweight="800",
            )

        # Combat/regear summary. The old "pressure" chart mixed positive and
        # negative fame values, which looked dramatic but was hard to act on.
        # Cards keep the useful run stats explicit.
        ax_value.set_facecolor("#f7f8fa")
        ax_value.set_xlim(0, 1)
        ax_value.set_ylim(0, 1)
        ax_value.axis("off")
        ax_value.text(
            0,
            1.02,
            "Combat / Regear Recap",
            color=TEXT_COLOR,
            fontsize=11,
            fontweight="700",
            transform=ax_value.transAxes,
        )
        if not killboard_lookup_enabled:
            cards = [
                ("Kills found", "skipped", "#8d99ae", "killboard lookup off"),
                ("Deaths found", "skipped", "#8d99ae", "killboard lookup off"),
                ("K:D", "n/a", "#8d99ae", "not calculated"),
                ("Loot value", "not entered", "#8d99ae", "click Input Event Loot"),
                ("Est. gear loss", "n/a", "#8d99ae", "lookup skipped"),
                ("Net silver", "n/a", "#8d99ae", "needs loss data"),
            ]
        else:
            gear_loss = sum(int(d.get("estimated_value") or 0) for d in deaths)
            priced_deaths = sum(1 for d in deaths if int(d.get("estimated_value") or 0) > 0)
            manual = max(0, len(deaths) - priced_deaths)
            bb_totals = (albionbb_summary or {}).get("totals") or {}
            bb_kills = int(bb_totals.get("kills") or 0)
            bb_deaths = int(bb_totals.get("deaths") or 0)
            display_kills = len(kills) if kills else bb_kills
            display_deaths = len(deaths) if deaths else bb_deaths
            kd_ratio = (display_kills / display_deaths) if display_deaths else float(display_kills)
            kd_label = f"{display_kills}:{display_deaths}"
            kd_sub = (
                f"{kd_ratio:.2f} kills/death"
                if display_deaths
                else ("no deaths found" if display_kills else "no events found")
            )
            avg_loss = int(gear_loss / priced_deaths) if priced_deaths else 0
            net_color = "#27ae60" if net_fame_value >= 0 else "#e67e22"
            gross_loot = int((loot_summary or {}).get("gross_loot") or 0)
            guild_cut = int((loot_summary or {}).get("guild_cut") or 0)
            distributable = max(0, gross_loot - guild_cut)
            net_silver = distributable - gear_loss if gross_loot else 0
            net_silver_color = "#27ae60" if net_silver >= 0 else "#c0392b"
            cards = [
                (
                    "Kills found",
                    str(display_kills),
                    "#27ae60",
                    "official kill events" if kills else "AlbionBB player rows",
                ),
                (
                    "Deaths found",
                    str(display_deaths),
                    "#c0392b",
                    "official regear details" if deaths else "AlbionBB row deaths",
                ),
                ("K:D", kd_label, net_color, kd_sub),
                (
                    "Loot value",
                    _fmt_compact(gross_loot) if gross_loot else "not entered",
                    "#27ae60" if gross_loot else "#8d99ae",
                    f"guild cut {_fmt_compact(guild_cut)}" if guild_cut else "click Input Event Loot",
                ),
                (
                    "Est. gear loss",
                    _fmt_compact(gear_loss),
                    "#e67e22" if gear_loss else TEXT_COLOR,
                    f"avg {_fmt_compact(avg_loss)}" + (f" • {manual} manual" if manual else ""),
                ),
                (
                    "Net silver",
                    _fmt_compact(net_silver) if gross_loot else "n/a",
                    net_silver_color if gross_loot else "#8d99ae",
                    "loot after cut minus gear loss" if gross_loot else "enter loot first",
                ),
            ]
        positions = [
            (0.02, 0.58),
            (0.35, 0.58),
            (0.68, 0.58),
            (0.02, 0.14),
            (0.35, 0.14),
            (0.68, 0.14),
        ]
        for (label, value, color, sub), (x, y) in zip(cards, positions):
            _draw_stat_card(ax_value, x, y, 0.28, 0.28, label=label, value=value, color=color, sub=sub)
        ax_value.text(
            0.02,
            0.03,
            "Net silver uses officer-entered loot minus estimated attendee gear loss."
            if killboard_lookup_enabled
            else "Preview only: run reconcile with killboard lookup for combat/regear data.",
            color="#6b7280",
            fontsize=7,
            fontweight="600",
            transform=ax_value.transAxes,
        )

        # Top contributor movement from stat deltas.
        bb_players = [
            row for row in (bb.get("player_totals") or [])
            if int(row.get("impact") or 0) > 0
        ][:6] if bb.get("enabled") else []
        movers = bb_players or [row for row in player_deltas if int(row.get("activity") or 0) > 0][:6]
        if not movers:
            _empty_panel(ax_players, "No contributor movement captured")
        else:
            names = [_short_label(str(row.get("name") or "Unknown"), 18) for row in movers]
            values = [
                int(row.get("impact") or row.get("activity") or 0)
                for row in movers
            ]
            y = list(range(len(names)))
            ax_players.barh(y, values, color=PALETTE["members"], height=0.58, zorder=3)
            for idx, value in enumerate(values):
                ax_players.text(
                    value + max(values) * 0.02,
                    idx,
                    _fmt_compact(value),
                    va="center",
                    color=TEXT_COLOR,
                    fontsize=8,
                    fontweight="700",
                )
            ax_players.set_yticks(y)
            ax_players.set_yticklabels(names, color=TEXT_COLOR, fontsize=9)
            ax_players.invert_yaxis()
            ax_players.set_xlim(0, max(values) * 1.25)
            ax_players.set_title(
                "Top AlbionBB Impact" if bb_players else "Top Attendee Movement",
                color=TEXT_COLOR,
                fontsize=11,
                fontweight="700",
                loc="left",
            )
            ax_players.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_fmt_compact))
            _style_axes(ax_players)
            ax_players.grid(axis="x", linestyle="-", linewidth=0.6, color="#dde1e6", zorder=0)
            ax_players.grid(axis="y", visible=False)

        fig.text(
            0.02,
            0.01,
            "Best-effort analytics: VC flow uses event voice snapshots; AlbionBB enriches battle/role/IP context; regear still uses official killboard evidence.",
            color="#6b7280",
            fontsize=8,
        )
        return _fig_to_file(fig, f"event_report_{event_id}.png")
    except Exception as exc:  # noqa: BLE001
        error_log(f"event report graph build failed: {exc!r}\n{traceback.format_exc()}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None
