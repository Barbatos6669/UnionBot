from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from cogs.events import _format_goodbye_embed, _tenure_text


def _member(*, joined_at: dt.datetime | None = None):
    return SimpleNamespace(
        id=123456789,
        display_name="ExampleOfficer",
        guild=SimpleNamespace(name="HOME GUILD"),
        joined_at=joined_at,
        display_avatar=SimpleNamespace(url=""),
    )


def test_tenure_text_formats_days() -> None:
    now = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)

    assert _tenure_text(now - dt.timedelta(hours=4), now) == "Less than 1 day"
    assert _tenure_text(now - dt.timedelta(days=1), now) == "1 day"
    assert _tenure_text(now - dt.timedelta(days=14), now) == "14 days"


def test_goodbye_embed_includes_profile_details() -> None:
    now = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
    joined = now - dt.timedelta(days=21)
    embed = _format_goodbye_embed(
        _member(joined_at=joined),
        {
            "albion_name": "ExampleOfficer",
            "guild_name": "Home Guild",
            "lifecycle_role": "Veteran",
        },
        now=now,
    )

    fields = {field.name: field.value for field in embed.fields}

    assert "ExampleOfficer" in embed.description
    assert "ExampleOfficer" in fields["Albion"]
    assert "Home Guild" in fields["Albion"]
    assert fields["Lifecycle"] == "Veteran"
    assert "21 days" in fields["Time in server"]


def test_goodbye_embed_handles_unregistered_member() -> None:
    embed = _format_goodbye_embed(_member(), None)
    fields = {field.name: field.value for field in embed.fields}

    assert fields["Albion"] == "Not registered"
    assert fields["Lifecycle"] == "Unassigned"
    assert fields["Time in server"] == "Unknown"
