"""Regear-facing text helpers for post-event reports."""
from __future__ import annotations

from cogs._event_report_format import fmt_num as _fmt_num


def member_name(profile: dict | None, discord_id: str) -> str:
    profile = profile or {}
    return str(profile.get("albion_name") or profile.get("username") or f"<@{discord_id}>")


def regear_death_line(
    death: dict,
    *,
    profiles: dict[str, dict],
    signup_ids: set[str],
) -> str:
    did = str(death.get("discord_id") or "")
    name = member_name(profiles.get(did), did)
    url = death.get("killboard_url") or ""
    linked = f"[{name}]({url})" if url else name
    loc = death.get("location") or "unknown zone"
    killer = death.get("killer_name") or "Unknown"
    signed = "yes" if did in signup_ids else "no"
    est_value = int(death.get("estimated_value") or 0)
    value_text = (
        f"Est gear: **{_fmt_num(est_value)}**"
        if est_value > 0 else
        "Est gear: **manual pricing needed**"
    )
    return (
        f"{linked} - {_fmt_num(death.get('fame'))} fame, {loc}, "
        f"killed by {killer}. Signup: {signed}; VC: yes. {value_text}."
    )


def suppressed_auto_regear_lines(deaths: list[dict]) -> list[str]:
    priced = sum(1 for death in deaths if int(death.get("estimated_value") or 0) > 0)
    manual = max(0, len(deaths) - priced)
    total = sum(int(death.get("estimated_value") or 0) for death in deaths)
    lines = [
        "Individual regear request cards are **not** auto-created from event reconcile.",
        "Use the consolidated **Regear Review** list and continuation embed(s) instead.",
        f"Deaths listed: **{len(deaths)}**",
        f"Estimated value listed: **{_fmt_num(total)}**",
    ]
    if manual:
        lines.append(f"Manual pricing needed: **{manual}** death(s)")
    lines.append("Manual/player-submitted regear requests still use the normal regear board.")
    return lines
