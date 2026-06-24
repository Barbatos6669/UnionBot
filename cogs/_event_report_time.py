"""Time-window helpers shared by event report embeds and scorecards."""
from __future__ import annotations

import datetime as dt
from typing import Any


UTC = dt.timezone.utc
DEFAULT_PREP_MINUTES = 30
DEFAULT_REVIEW_MINUTES = 15


def parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def event_window(
    event: dict,
) -> tuple[dt.datetime | None, dt.datetime | None, dt.datetime | None, dt.datetime | None]:
    """Return start, end, report_start, report_end for event analytics."""
    starts_at = parse_dt(event.get("starts_at"))
    ends_at = parse_dt(event.get("ends_at"))
    if not starts_at or not ends_at:
        return starts_at, ends_at, starts_at, ends_at

    prep = max(0, int(event.get("prep_minutes") or DEFAULT_PREP_MINUTES))
    review = max(0, int(event.get("review_minutes") or DEFAULT_REVIEW_MINUTES))
    report_end = ends_at + dt.timedelta(minutes=review)
    voice_deleted_at = parse_dt(event.get("voice_channel_deleted_at"))
    if voice_deleted_at and voice_deleted_at > report_end:
        report_end = voice_deleted_at

    return (
        starts_at,
        ends_at,
        starts_at - dt.timedelta(minutes=prep),
        report_end,
    )
