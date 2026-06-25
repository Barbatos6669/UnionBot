"""Small UTC time helpers shared by cogs.

Most existing database timestamps in this bot are stored as naive UTC ISO
strings. Keep that shape here so callers can move off deprecated ``utcnow()``
without changing stored data formats.
"""
from __future__ import annotations

import datetime as dt


def utc_now_naive() -> dt.datetime:
    """Return the current UTC time as a naive ``datetime``."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def utc_now_iso(*, sep: str = " ", timespec: str = "seconds") -> str:
    """Return current naive UTC as an ISO string for existing DB fields."""
    return utc_now_naive().replace(microsecond=0).isoformat(sep=sep, timespec=timespec)
