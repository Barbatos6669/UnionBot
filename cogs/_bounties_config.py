"""Constants, formatters, parsers, and embed builder for the bounties cog.

Pure presentation/parsing — no DB access. Imported by the views/modals/cog.
"""

from __future__ import annotations

import datetime

import discord

from time_utils import utc_now_iso, utc_now_naive

# ── config keys ─────────────────────────────────────────────────────────────
CFG_BOARD_CHANNEL  = "bounty_board_channel_id"
CFG_REVIEW_CHANNEL = "bounty_review_channel_id"
CFG_FLEX_CHANNEL   = "bounty_flex_channel_id"

# Lifetime silver-earned thresholds that get an extra milestone shoutout.
# Each user only gets credited once per tier (see bounty_milestones table).
BOUNTY_MILESTONES: tuple[int, ...] = (
    1_000_000,
    5_000_000,
    10_000_000,
    25_000_000,
    50_000_000,
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
)

# ── statuses ────────────────────────────────────────────────────────────────
STATUS_PENDING   = "pending"
STATUS_OPEN      = "open"
STATUS_CLAIMED   = "claimed"
STATUS_SUBMITTED = "submitted"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED   = "expired"

ACTIVE_STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_SUBMITTED)
PUBLIC_STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_SUBMITTED,
                   STATUS_COMPLETED, STATUS_EXPIRED)

STATUS_COLORS = {
    STATUS_PENDING:   discord.Color.purple(),
    STATUS_OPEN:      discord.Color.gold(),
    STATUS_CLAIMED:   discord.Color.blue(),
    STATUS_SUBMITTED: discord.Color.orange(),
    STATUS_COMPLETED: discord.Color.green(),
    STATUS_CANCELLED: discord.Color.dark_grey(),
    STATUS_EXPIRED:   discord.Color.dark_grey(),
}

STATUS_EMOJI = {
    STATUS_PENDING:   "🟣",
    STATUS_OPEN:      "🟡",
    STATUS_CLAIMED:   "🔵",
    STATUS_SUBMITTED: "🟠",
    STATUS_COMPLETED: "🟢",
    STATUS_CANCELLED: "⚪",
    STATUS_EXPIRED:   "⚫",
}

# ── tunables ────────────────────────────────────────────────────────────────
MAX_REWARD = 100_000_000  # 100M silver cap on a single bounty

# Silent participation rewards: bounties pay silver in-game; the bot quietly
# tops up internal activity points so engagement is tracked without inflating
# the visible "reward".
SILENT_POINTS_PROPOSER_APPROVED = 2  # bounty was approved for posting
SILENT_POINTS_CLAIMER_PAID      = 5  # bounty was approved for payout

# Sentinel-SA structured-bounty prefix. Used to switch the submit modal into
# the multi-field SSO route prompt instead of plain free-form proof.
SSO_TITLE_PREFIX        = "[SSO Route]"
DAILY_SSO_DEFAULT_TITLE = f"{SSO_TITLE_PREFIX} Submit today's HO portal route"

# ── button templates (DynamicItem custom_id regexes) ───────────────────────
CLAIM_TEMPLATE   = r"bounty:claim:(?P<bid>[0-9]+)"
UNCLAIM_TEMPLATE = r"bounty:unclaim:(?P<bid>[0-9]+)"
SUBMIT_TEMPLATE  = r"bounty:submit:(?P<bid>[0-9]+)"
APPROVE_TEMPLATE = r"bounty:approve:(?P<bid>[0-9]+)"
REJECT_TEMPLATE  = r"bounty:reject:(?P<bid>[0-9]+)"
PAID_TEMPLATE    = r"bounty:paid:(?P<bid>[0-9]+)"


# ── formatters ──────────────────────────────────────────────────────────────
def fmt_silver(n: int) -> str:
    """Format a silver amount for display."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        amount = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{amount}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return f"{n:,}"


def now_iso() -> str:
    return utc_now_iso(sep=" ")


def parse_deadline(raw: str | None) -> str | None:
    """Parse a flexible deadline string into ISO; returns None if blank or invalid.

    Accepts:
      - ``2026-05-15`` (date only -> end of day UTC)
      - ``2026-05-15 21:00``
      - ``3d`` / ``12h`` / ``30m`` (relative)
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None

    units = {"m": 60, "h": 3600, "d": 86400}
    if len(s) >= 2 and s[-1] in units and s[:-1].isdigit():
        secs = int(s[:-1]) * units[s[-1]]
        dt = utc_now_naive() + datetime.timedelta(seconds=secs)
        return dt.replace(microsecond=0).isoformat(sep=" ")

    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            d = datetime.date.fromisoformat(s)
            dt = datetime.datetime.combine(d, datetime.time(23, 59, 0))
            return dt.replace(microsecond=0).isoformat(sep=" ")
        dt = datetime.datetime.fromisoformat(s.replace("T", " "))
        return dt.replace(microsecond=0).isoformat(sep=" ")
    except (ValueError, TypeError):
        return None


def fmt_deadline(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(iso)
        return f"<t:{int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
    except (ValueError, TypeError):
        return iso


def bounty_needs_payment(b: dict) -> bool:
    """True when a completed bounty still needs officer in-game settlement."""
    try:
        reward = int(b.get("reward_points") or 0)
    except (TypeError, ValueError):
        reward = 0
    return (
        b.get("status") == STATUS_COMPLETED
        and reward > 0
        and bool(b.get("claimed_by"))
        and not b.get("paid_at")
    )


def bounty_to_embed(b: dict) -> discord.Embed:
    status = b["status"]
    emoji = STATUS_EMOJI.get(status, "•")
    color = STATUS_COLORS.get(status, discord.Color.greyple())
    embed = discord.Embed(
        title=f"{emoji} Bounty #{b['id']} — {b['title']}",
        description=b["description"],
        color=color,
    )
    embed.add_field(name="Reward", value=f"🪙 **{fmt_silver(b['reward_points'])}** silver", inline=True)
    embed.add_field(name="Status", value=status.capitalize(), inline=True)
    embed.add_field(name="Deadline", value=fmt_deadline(b.get("deadline")), inline=True)
    embed.add_field(name="Posted by", value=f"<@{b['posted_by']}>", inline=True)
    if b.get("claimed_by"):
        embed.add_field(name="Claimed by", value=f"<@{b['claimed_by']}>", inline=True)
    if status == STATUS_SUBMITTED and b.get("proof"):
        embed.add_field(name="Proof", value=str(b["proof"])[:1024], inline=False)
    if b.get("completed_by"):
        embed.add_field(name="Approved by", value=f"<@{b['completed_by']}>", inline=True)
    if status == STATUS_COMPLETED:
        if b.get("paid_at"):
            paid_value = f"✅ Paid by <@{b.get('paid_by')}>" if b.get("paid_by") else "✅ Paid"
            try:
                dt = datetime.datetime.fromisoformat(str(b["paid_at"]).replace("T", " "))
                paid_value += f"\n<t:{int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())}:R>"
            except (ValueError, TypeError):
                paid_value += f"\n`{b['paid_at']}`"
        else:
            paid_value = "🟡 Awaiting in-game payment"
        embed.add_field(name="Payment", value=paid_value, inline=True)
    embed.set_footer(text=f"Posted {b.get('posted_at', '')}")
    return embed
