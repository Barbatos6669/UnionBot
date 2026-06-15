import time
import requests
import debug
from collections import deque
from typing import Any, Optional, Tuple, Dict

SERVER_URLS = {
    "americas": "https://gameinfo.albiononline.com/api/gameinfo",
    "europe":   "https://gameinfo-ams.albiononline.com/api/gameinfo",
    "asia":     "https://gameinfo-sgp.albiononline.com/api/gameinfo",
}

# Albion's gameinfo endpoint is often slow (5-20s) and occasionally times
# out for a single request even when the next one succeeds. Default to a
# generous timeout and retry transient timeouts/connection errors once.
_DEFAULT_TIMEOUT = 25.0
_RETRY_ON = (requests.Timeout, requests.ConnectionError)

# ── Rate-limit sentry ───────────────────────────────────────────────────────
# Albion tolerates roughly 180 requests/minute before soft-banning the IP.
# We keep a sliding window of recent call timestamps and log a WARNING when
# we cross 100/min so a runaway sync loop is visible before it gets us
# blackholed.
_RATE_WINDOW_SEC = 60.0
_RATE_WARN_THRESHOLD = 100
_call_times: deque = deque()


def _record_call() -> None:
    now = time.monotonic()
    _call_times.append(now)
    cutoff = now - _RATE_WINDOW_SEC
    while _call_times and _call_times[0] < cutoff:
        _call_times.popleft()
    n = len(_call_times)
    if n == _RATE_WARN_THRESHOLD or n == _RATE_WARN_THRESHOLD + 50:
        debug.error_log(
            f"Albion API rate sentry: {n} calls in last 60s "
            f"(soft-ban risk threshold ~180/min)."
        )


def _request_with_retry(method: str, url: str, *, params=None, timeout: float, label: str):
    """GET wrapper that retries once on timeout/connection error, then gives up."""
    last_err: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            _record_call()
            resp = requests.request(method, url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except _RETRY_ON as error:
            last_err = error
            if attempt == 1:
                time.sleep(1.0)
                continue
        except requests.RequestException as error:
            last_err = error
            break
    debug.error_log(f"Request failed for {label}: {last_err}")
    return None


def find_player_candidates(player_name: str, server: str = "americas", timeout: float = _DEFAULT_TIMEOUT) -> list:
    """Search Albion and return the full list of exact-name matches.

    Each item is the raw player dict from the API (keys: Id, Name, GuildId,
    GuildName, AllianceId, KillFame, DeathFame, ...). Empty list on
    "not found" or any error.

    Callers wanting a single best pick should use ``get_player_id`` (which
    delegates here and applies a scoring heuristic).
    """
    player_name = (player_name or "").strip()
    if not player_name:
        debug.error_log("Player name was empty.")
        return []

    base_url = SERVER_URLS.get(server.lower(), SERVER_URLS["americas"])
    resp = _request_with_retry(
        "GET", f"{base_url}/search",
        params={"q": player_name}, timeout=timeout,
        label=f"player search {player_name!r}",
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError as error:
        debug.error_log(f"Invalid JSON response: {error}")
        return []

    return [
        p for p in data.get("players", [])
        if (p.get("Name") or "").lower() == player_name.lower()
    ]


def get_player_id(
    player_name: str,
    server: str = "americas",
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    prefer_guild_name: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """Search for a player by name and return (player_id, exact_name), or None.

    Albion character names aren't globally unique — multiple players can
    share the same name across accounts. The picker prefers, in order:

      1. A candidate already in ``prefer_guild_name`` (case-insensitive),
         if provided. This is the single strongest disambiguator when
         processing applications: an applicant's correct character is
         almost always the one already sitting in your guild.
      2. Highest "activity" signal — being in any guild, having an
         alliance, and total fame (kill + death).

    Falls back to the first match if all heuristics tie.
    """
    candidates = find_player_candidates(player_name, server=server, timeout=timeout)
    if not candidates:
        return None

    pref = (prefer_guild_name or "").strip().lower()

    def _score(p: Dict[str, Any]) -> tuple:
        in_pref_guild = 1 if (pref and (p.get("GuildName") or "").strip().lower() == pref) else 0
        return (
            in_pref_guild,
            1 if (p.get("GuildId") or "") else 0,
            1 if (p.get("AllianceId") or "") else 0,
            int(p.get("KillFame") or 0) + int(p.get("DeathFame") or 0),
        )

    best = max(candidates, key=_score)
    return best["Id"], best["Name"]


def get_player_stats(player_id: str, server: str = "americas", timeout: float = _DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Fetch full stats for a player by their Albion player ID. Returns raw API dict or None."""
    base_url = SERVER_URLS.get(server.lower(), SERVER_URLS["americas"])
    resp = _request_with_retry(
        "GET", f"{base_url}/players/{player_id}",
        timeout=timeout, label=f"player {player_id}",
    )
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as error:
        debug.error_log(f"Invalid JSON response for player {player_id}: {error}")
        return None


def parse_stats(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw Albion API player response to our database column names."""
    lf: Dict[str, Any] = data.get("LifetimeStatistics") or {}
    pve: Dict[str, Any] = lf.get("PvE") or {}
    gathering: Dict[str, Any] = lf.get("Gathering") or {}

    return {
        "albion_name":        data.get("Name"),
        "kill_fame":          data.get("KillFame", 0),
        "death_fame":         data.get("DeathFame", 0),
        "fame_ratio":         data.get("FameRatio", 0.0),
        "average_item_power": data.get("AverageItemPower", 0.0),
        "guild_id":           data.get("GuildId"),
        "guild_name":         data.get("GuildName"),
        "alliance_id":        data.get("AllianceId"),
        "alliance_name":      data.get("AllianceName"),
        "alliance_tag":       data.get("AllianceTag"),
        "pve_total":          pve.get("Total", 0),
        "pve_royal":          pve.get("Royal", 0),
        "pve_outlands":       pve.get("Outlands", 0),
        "pve_avalon":         pve.get("Avalon", 0),
        "pve_hellgate":       pve.get("Hellgate", 0),
        "pve_corrupted":      pve.get("Corrupted", 0),
        "pve_mists":          pve.get("Mists", 0),
        "gather_fiber":       gathering.get("Fiber", {}).get("Total", 0),
        "gather_hide":        gathering.get("Hide", {}).get("Total", 0),
        "gather_ore":         gathering.get("Ore", {}).get("Total", 0),
        "gather_rock":        gathering.get("Rock", {}).get("Total", 0),
        "gather_wood":        gathering.get("Wood", {}).get("Total", 0),
        "gather_all":         gathering.get("All", {}).get("Total", 0),
        "crafting_fame":      lf.get("Crafting", {}).get("Total", 0),
        "crystal_league":     lf.get("CrystalLeague", 0),
        "fishing_fame":       lf.get("FishingFame", 0),
        "farming_fame":       lf.get("FarmingFame", 0),
    }


# ── Guild ─────────────────────────────────────────────────────────────────────

def get_guild_id(guild_name: str, server: str = "americas", timeout: float = _DEFAULT_TIMEOUT) -> Optional[Tuple[str, str]]:
    """Search for a guild by name and return (guild_id, exact_name), or None if not found."""
    guild_name = guild_name.strip()
    if not guild_name:
        debug.error_log("Guild name was empty.")
        return None

    base_url = SERVER_URLS.get(server.lower(), SERVER_URLS["americas"])
    resp = _request_with_retry(
        "GET", f"{base_url}/search",
        params={"q": guild_name}, timeout=timeout,
        label=f"guild search {guild_name!r}",
    )
    if resp is None:
        return None
    try:
        data = resp.json()
    except ValueError as error:
        debug.error_log(f"Invalid JSON response: {error}")
        return None

    for guild in data.get("guilds", []):
        if guild.get("Name", "").lower() == guild_name.lower():
            return guild["Id"], guild["Name"]

    return None


def get_guild_stats(guild_id: str, server: str = "americas", timeout: float = _DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Fetch stats for a guild by its Albion guild ID. Returns raw API dict or None."""
    base_url = SERVER_URLS.get(server.lower(), SERVER_URLS["americas"])
    resp = _request_with_retry(
        "GET", f"{base_url}/guilds/{guild_id}",
        timeout=timeout, label=f"guild {guild_id}",
    )
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as error:
        debug.error_log(f"Invalid JSON response for guild {guild_id}: {error}")
        return None


def parse_guild_stats(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map a raw Albion API guild response to our database column names."""
    return {
        "guild_id":      data.get("Id"),
        "guild_name":    data.get("Name"),
        "founder_name":  data.get("FounderName"),
        "founded":       data.get("Founded"),
        "kill_fame":     data.get("killFame", 0),
        "death_fame":    data.get("DeathFame", 0),
        "member_count":  data.get("MemberCount", 0),
        "alliance_id":   data.get("AllianceId"),
        "alliance_name": data.get("AllianceName"),
        "alliance_tag":  data.get("AllianceTag"),
    }


# ── Death events ─────────────────────────────────────────────────────────────

_QUALITY_NAMES = {
    1: "Normal",
    2: "Good",
    3: "Outstanding",
    4: "Excellent",
    5: "Masterpiece",
}

_KILLBOARD_BASE = "https://albiononline.com/en/killboard/kill"


def get_player_deaths(
    player_id: str, limit: int = 5,
    server: str = "americas", timeout: float = _DEFAULT_TIMEOUT,
) -> list:
    """Return up to `limit` of the player's most recent death events.

    Each item is the raw event dict from `/players/{id}/deaths`. Empty list
    on error or no recorded deaths.
    """
    base_url = SERVER_URLS.get(server.lower(), SERVER_URLS["americas"])
    resp = _request_with_retry(
        "GET", f"{base_url}/players/{player_id}/deaths",
        timeout=timeout, label=f"player {player_id} deaths",
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError as error:
        debug.error_log(f"Invalid JSON response for player {player_id} deaths: {error}")
        return []
    if not isinstance(data, list):
        return []
    return data[: max(0, int(limit))]


def get_player_kills(
    player_id: str, limit: int = 5,
    server: str = "americas", timeout: float = _DEFAULT_TIMEOUT,
) -> list:
    """Return up to `limit` of the player's most recent kill events.

    These are raw Albion killboard event dicts from `/players/{id}/kills`.
    Callers should still de-dupe by EventId before taking any reward action.
    """
    base_url = SERVER_URLS.get(server.lower(), SERVER_URLS["americas"])
    resp = _request_with_retry(
        "GET", f"{base_url}/players/{player_id}/kills",
        params={"limit": max(1, min(50, int(limit or 5)))},
        timeout=timeout, label=f"player {player_id} kills",
    )
    if resp is None:
        return []
    try:
        data = resp.json()
    except ValueError as error:
        debug.error_log(f"Invalid JSON response for player {player_id} kills: {error}")
        return []
    if not isinstance(data, list):
        return []
    return data[: max(0, int(limit))]


def _pretty_item_name(item_type: str) -> str:
    """Convert an Albion item-type string like 'T8_MAIN_SWORD@2' to a
    human-readable label like 'T8 main sword @2'."""
    if not item_type:
        return "—"
    raw = str(item_type)
    # Split off enchant suffix (e.g. "@2") so we can format it separately.
    enchant = ""
    if "@" in raw:
        raw, _, enchant_part = raw.partition("@")
        enchant = f" @{enchant_part}"
    # Drop the tier prefix and lowercase the rest for readability.
    parts = raw.split("_")
    tier = parts[0] if parts and parts[0].startswith("T") else ""
    rest = " ".join(p.lower() for p in parts[1:]) if tier else " ".join(p.lower() for p in parts)
    return f"{tier} {rest}{enchant}".strip()


def _format_equipment_slot(slot_name: str, slot_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Format one equipment slot as ' • Slot: T8 item @2 (Excellent)'.
    Returns None if the slot was empty."""
    if not slot_data:
        return None
    item_type = slot_data.get("Type")
    if not item_type:
        return None
    qual = int(slot_data.get("Quality") or 1)
    qual_word = _QUALITY_NAMES.get(qual, "")
    pretty = _pretty_item_name(item_type)
    qual_suffix = f" ({qual_word})" if qual_word and qual > 1 else ""
    return f"• **{slot_name}:** {pretty}{qual_suffix}"


def format_death_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a normalized summary of one death event for downstream use.

    Returns a dict with: event_id, timestamp, killer_name, killer_guild,
    killer_ip, victim_ip, fame, location, killboard_url, gear_lines (list of str),
    guessed_content_type (str), group_size, participant_count."""
    event_id = event.get("EventId") or 0
    killer = event.get("Killer") or {}
    victim = event.get("Victim") or {}
    equipment = victim.get("Equipment") or {}

    slot_order = [
        ("Main hand", "MainHand"),
        ("Off hand",  "OffHand"),
        ("Head",      "Head"),
        ("Armor",     "Armor"),
        ("Shoes",     "Shoes"),
        ("Cape",      "Cape"),
        ("Bag",       "Bag"),
        ("Mount",     "Mount"),
        ("Potion",    "Potion"),
        ("Food",      "Food"),
    ]
    gear_lines = []
    gear_items: list[dict[str, Any]] = []
    for label, key in slot_order:
        line = _format_equipment_slot(label, equipment.get(key))
        if line:
            gear_lines.append(line)
        slot = equipment.get(key) or {}
        item_type = slot.get("Type")
        if item_type:
            gear_items.append({
                "slot": label,
                "item_id": str(item_type),
                "quality": int(slot.get("Quality") or 1),
                "count":   int(slot.get("Count") or 1),
            })

    participants = event.get("Participants") or []
    group_members = event.get("GroupMembers") or []
    participant_count = len(participants) if isinstance(participants, list) else 0
    group_size = len(group_members) if isinstance(group_members, list) else 0

    # Crude content-type guess from group composition. Officers can override
    # in the modal — this is just a starting hint.
    guessed = ""
    if participant_count >= 10:
        guessed = "ZvZ"
    elif participant_count <= 2 and group_size <= 1:
        guessed = "Ganking"

    return {
        "event_id":             int(event_id),
        "timestamp":            event.get("TimeStamp") or "",
        "killer_name":          killer.get("Name") or "Unknown",
        "killer_guild":         killer.get("GuildName") or "",
        "killer_ip":            float(killer.get("AverageItemPower") or 0),
        "victim_ip":            float(victim.get("AverageItemPower") or 0),
        "fame":                 int(event.get("TotalVictimKillFame") or 0),
        "location":             str(event.get("Location") or ""),
        "killboard_url":        f"{_KILLBOARD_BASE}/{int(event_id)}" if event_id else "",
        "gear_lines":           gear_lines,
        "gear_items":           gear_items,
        "guessed_content_type": guessed,
        "group_size":           group_size,
        "participant_count":    participant_count,
    }
