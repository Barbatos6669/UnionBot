"""Tiered bounty reward helpers."""
from __future__ import annotations

import json

from cogs._bounties_config import fmt_silver as _fmt_silver


# Some bounties, such as daily Energy Core tasks, pay different amounts based
# on what the player actually delivered. The default scale is stored in config,
# while each posted bounty snapshots its scale so historical payouts do not
# change when officers tune future defaults.
DEFAULT_ENERGY_CORE_TIERS: list[dict] = [
    {"name": "Green",  "emoji": "🟢", "silver":   250_000},
    {"name": "Blue",   "emoji": "🔵", "silver":   500_000},
    {"name": "Purple", "emoji": "🟣", "silver": 1_000_000},
    {"name": "Gold",   "emoji": "🟡", "silver": 3_000_000},
]


def format_tier_scale(tiers: list[dict]) -> str:
    """Bullet list of tier -> silver for bounty descriptions."""
    out = []
    for tier in tiers:
        emoji = tier.get("emoji", "•")
        name = tier.get("name", "?")
        silver = int(tier.get("silver", 0))
        out.append(f"{emoji} **{name}:** 🪙 {_fmt_silver(silver)}")
    return "\n".join(out)


def load_bounty_tier_scale(db, bounty_id: int) -> list[dict] | None:
    """Return the saved tier scale for a bounty, or None for non-tiered rows."""
    raw = db.get_config(f"bounty_tier_scale:{int(bounty_id)}") or ""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list) and data:
            return [tier for tier in data if isinstance(tier, dict) and tier.get("name")]
    except (ValueError, TypeError):
        return None
    return None


def save_bounty_tier_scale(db, bounty_id: int, tiers: list[dict]) -> None:
    db.set_config(
        f"bounty_tier_scale:{int(bounty_id)}",
        json.dumps([
            {
                "name": str(tier["name"]),
                "emoji": str(tier.get("emoji", "")),
                "silver": int(tier.get("silver", 0)),
            }
            for tier in tiers
            if tier.get("name")
        ]),
    )


def load_default_tiers(db, type_key: str, fallback: list[dict]) -> list[dict]:
    """Load the per-type default scale, falling back to the hard-coded default."""
    raw = db.get_config(f"bounty_{type_key}_tiers") or ""
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                cleaned = [
                    tier for tier in data
                    if isinstance(tier, dict)
                    and tier.get("name")
                    and int(tier.get("silver", 0)) > 0
                ]
                if cleaned:
                    return cleaned
        except (ValueError, TypeError):
            pass
    return fallback
