import asyncio
import sqlite3
from pathlib import Path

import pytest

from cogs._event_report_time import event_window
from cogs._event_report_ui import _parse_silver_amount
from cogs.automation import _run_nightly_backup


def test_event_report_window_defaults_and_voice_extension():
    event = {
        "starts_at": "2026-06-25T02:00:00+00:00",
        "ends_at": "2026-06-25T03:00:00+00:00",
        "voice_channel_deleted_at": "2026-06-25T03:40:00+00:00",
    }

    starts_at, ends_at, report_start, report_end = event_window(event)

    assert starts_at.isoformat() == "2026-06-25T02:00:00+00:00"
    assert ends_at.isoformat() == "2026-06-25T03:00:00+00:00"
    assert report_start.isoformat() == "2026-06-25T01:30:00+00:00"
    assert report_end.isoformat() == "2026-06-25T03:40:00+00:00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.2m", 4_200_000),
        ("750k", 750_000),
        ("2 thousand", 2_000),
        ("500 silver", 500),
        ("1,250_000", 1_250_000),
    ],
)
def test_parse_silver_amount_supported_shapes(raw, expected):
    assert _parse_silver_amount(raw) == expected


def test_parse_silver_amount_rejects_bad_values():
    with pytest.raises(ValueError):
        _parse_silver_amount("two thousand")


def test_nightly_backup_opens_source_connection_in_worker_thread(tmp_path, monkeypatch):
    db_path = tmp_path / "source.db"
    live_connection = sqlite3.connect(str(db_path))
    live_connection.execute("CREATE TABLE sample (value TEXT)")
    live_connection.execute("INSERT INTO sample VALUES ('ok')")
    live_connection.commit()

    class FakeDb:
        database_path = str(db_path)
        connection = live_connection

        def get_config(self, key):
            if key == "automation_backup_keep_days":
                return "7"
            return None

    class FakeBot:
        db = FakeDb()

    monkeypatch.chdir(tmp_path)

    try:
        asyncio.run(_run_nightly_backup(FakeBot()))
    finally:
        live_connection.close()

    backups = sorted(Path("data/backups").glob("db-*-auto.db"))
    assert len(backups) == 1
    with sqlite3.connect(str(backups[0])) as backed_up:
        assert backed_up.execute("SELECT value FROM sample").fetchone()[0] == "ok"
