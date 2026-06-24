"""Small AlbionBB API client used for post-event analytics.

AlbionBB is a third-party battle index. Treat it as an enrichment source:
use it for nicer battle/player summaries, but keep our own Discord voice
snapshots and official Albion killboard lookups as the source of truth for
attendance and regear decisions.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

import requests

import debug


SERVER_URLS = {
    "americas": "https://api.albionbb.com/us",
    "west": "https://api.albionbb.com/us",
    "us": "https://api.albionbb.com/us",
    "europe": "https://api.albionbb.com/eu",
    "eu": "https://api.albionbb.com/eu",
    "asia": "https://api.albionbb.com/asia",
    "east": "https://api.albionbb.com/asia",
}

SITE_URLS = {
    "americas": "https://albionbb.com",
    "west": "https://albionbb.com",
    "us": "https://albionbb.com",
    "europe": "https://europe.albionbb.com",
    "eu": "https://europe.albionbb.com",
    "asia": "https://east.albionbb.com",
    "east": "https://east.albionbb.com",
}

DEFAULT_TIMEOUT = 12.0
_RATE_WINDOW_SEC = 60.0
_RATE_WARN_THRESHOLD = 60
_call_times: deque[float] = deque()


def _server_key(server: str | None) -> str:
    key = str(server or "americas").strip().lower()
    return key if key in SERVER_URLS else "americas"


def _record_call() -> None:
    now = time.monotonic()
    _call_times.append(now)
    cutoff = now - _RATE_WINDOW_SEC
    while _call_times and _call_times[0] < cutoff:
        _call_times.popleft()
    if len(_call_times) == _RATE_WARN_THRESHOLD:
        debug.error_log(
            f"AlbionBB API rate sentry: {len(_call_times)} calls in last 60s."
        )


def battle_url(battle_id: int | str, *, server: str = "americas") -> str:
    key = _server_key(server)
    return f"{SITE_URLS[key]}/battles/{battle_id}"


def _get_json(
    path: str,
    *,
    server: str = "americas",
    params: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    label: str = "request",
) -> Any:
    key = _server_key(server)
    base = SERVER_URLS[key].rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    last_err: Exception | None = None
    for attempt in (1, 2):
        try:
            _record_call()
            resp = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "UnionBot AlbionBB analytics",
                },
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc
            if attempt == 1:
                time.sleep(0.75)
                continue
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            break
    debug.error_log(f"AlbionBB API failed for {label}: {last_err}")
    return None


def get_player_battle_stats(
    player_name: str,
    *,
    server: str = "americas",
    min_players: int = 1,
    start: str | None = None,
    end: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return AlbionBB battle stat rows for one player name."""
    player_name = str(player_name or "").strip()
    if not player_name:
        return []
    params: dict[str, Any] = {"minPlayers": max(1, int(min_players or 1))}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    data = _get_json(
        f"/stats/players/{player_name}",
        server=server,
        params=params,
        timeout=timeout,
        label=f"player stats {player_name!r}",
    )
    return data if isinstance(data, list) else []


def get_battle(
    battle_id: int | str,
    *,
    server: str = "americas",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any] | None:
    """Return AlbionBB's normalized battle detail for one battle id."""
    data = _get_json(
        f"/battles/{battle_id}",
        server=server,
        timeout=timeout,
        label=f"battle {battle_id}",
    )
    return data if isinstance(data, dict) else None


def get_battle_kills(
    battle_ids: list[int | str],
    *,
    server: str = "americas",
    timeout: float = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """Return kill rows for one or more AlbionBB battle ids."""
    ids = ",".join(str(i) for i in battle_ids if str(i).strip())
    if not ids:
        return []
    data = _get_json(
        "/battles/kills",
        server=server,
        params={"ids": ids},
        timeout=timeout,
        label=f"battle kills {ids[:80]}",
    )
    return data if isinstance(data, list) else []
