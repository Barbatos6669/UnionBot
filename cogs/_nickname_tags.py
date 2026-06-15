"""Nickname tag helpers for registered Albion members.

The bot manages the leading ``[TAG]`` prefix on verified members so officers
can quickly see a member's Albion alliance in Discord.
"""

from __future__ import annotations

import json
import re
from typing import Any

from config import HOME_GUILD_NICK_TAG, HOME_ALLIANCE_TAG

DEFAULT_HOME_NICK_TAG = HOME_GUILD_NICK_TAG
LEGACY_HOME_NICK_TAGS = tuple(tag for tag in (HOME_ALLIANCE_TAG,) if tag)
HOME_NICK_TAG_CONFIG = "member_nickname_home_tag"
GUILD_NICK_TAGS_CONFIG = "member_nickname_guild_tags"

_TAG_RE = re.compile(r"^\[([^\]\r\n]{1,12})\]\s+(.+)$")
_UNSAFE_TAG_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_TAG_LIKE_RE = re.compile(r"^[A-Za-z0-9_-]{2,12}$")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CAMEL_INITIAL_RE = re.compile(r"[A-Z](?=[a-z0-9]|[A-Z][a-z]|$)")


def clean_nick_tag(raw: Any) -> str | None:
    """Return a compact Discord-safe tag, or None if no usable tag exists."""
    tag = str(raw or "").strip().strip("[]").strip()
    if not tag:
        return None
    tag = _UNSAFE_TAG_CHARS.sub("", tag)
    if not tag:
        return None
    return tag[:12].upper()


def alliance_display_tag(raw_tag: Any, raw_name: Any = None) -> str | None:
    """Return the best visible alliance tag from Albion API fields.

    Albion responses sometimes put the short alliance tag in ``AllianceName``
    while leaving ``AllianceTag`` blank. Use that fallback only when the name
    already looks like a compact tag, so full names like "Example Alliance"
    do not become awkward nickname prefixes.
    """
    tag = clean_nick_tag(raw_tag)
    if tag:
        return tag
    name = str(raw_name or "").strip().strip("[]").strip()
    if _TAG_LIKE_RE.match(name):
        return name
    return None


def home_nick_tag(db) -> str:
    """Configured home-guild nickname tag, defaulting to ``HOME_GUILD_NICK_TAG``."""
    configured = clean_nick_tag(db.get_config(HOME_NICK_TAG_CONFIG))
    if configured:
        return configured
    return DEFAULT_HOME_NICK_TAG


def guild_initials_tag(raw_name: Any) -> str | None:
    """Derive a short guild tag from an Albion guild name.

    Alliance members should be visible by guild, not just alliance. Multi-word
    guilds use initials (``Divine Departure`` -> ``DD``). CamelCase one-word
    guilds use capital letters (``HomeGuild`` -> ``HG``), while plain
    one-word names fall back to the first few characters.
    """
    name = str(raw_name or "").strip().strip("[]").strip()
    if not name:
        return None

    words = _WORD_RE.findall(name)
    if len(words) >= 2:
        return clean_nick_tag("".join(w[0] for w in words[:5]))

    word = words[0] if words else ""
    if not word:
        return None

    camel = "".join(_CAMEL_INITIAL_RE.findall(word))
    if len(camel) >= 2:
        return clean_nick_tag(camel[:5])

    # A one-word all-caps guild name is already a tag-like identity. Keep it
    # readable but short enough for Discord nicknames.
    if word.isupper() and len(word) <= 5:
        return clean_nick_tag(word)

    return clean_nick_tag(word[:4])


def _guild_tag_overrides(db) -> dict[str, str]:
    """Configured guild tag overrides keyed by guild ID or guild name.

    Stored as JSON in ``member_nickname_guild_tags``. Example:
    ``{"Divine Departure": "DD", "albion-guild-id": "DD"}``.
    """
    raw = db.get_config(GUILD_NICK_TAGS_CONFIG)
    if not raw:
        return {}
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    overrides: dict[str, str] = {}
    for key, value in data.items():
        tag = clean_nick_tag(value)
        if tag:
            overrides[str(key).strip().lower()] = tag
    return overrides


def _row_get(row: Any, key: str) -> Any:
    if not row:
        return None
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _clean_id(raw: Any) -> str | None:
    value = str(raw or "").strip()
    return value or None


def _profile_alliance(db, profile: dict | None) -> tuple[str | None, str | None]:
    alliance_id = _clean_id(_row_get(profile, "alliance_id"))
    alliance_tag = alliance_display_tag(
        _row_get(profile, "alliance_tag"),
        _row_get(profile, "alliance_name"),
    )
    guild_id = _row_get(profile, "guild_id")
    if guild_id:
        try:
            guild = db.fetch_guild(guild_id)
        except Exception:
            guild = None
        if guild:
            alliance_id = alliance_id or _clean_id(_row_get(guild, "alliance_id"))
            alliance_tag = alliance_tag or alliance_display_tag(
                _row_get(guild, "alliance_tag"),
                _row_get(guild, "alliance_name"),
            )
    return alliance_id, alliance_tag


def _profile_guild(profile: dict | None) -> tuple[str | None, str | None]:
    return (
        _clean_id(_row_get(profile, "guild_id")),
        str(_row_get(profile, "guild_name") or "").strip() or None,
    )


def _configured_or_derived_guild_tag(
    db,
    guild_id: str | None,
    guild_name: str | None,
) -> str | None:
    overrides = _guild_tag_overrides(db)
    for key in (guild_id, guild_name):
        if not key:
            continue
        tag = overrides.get(str(key).strip().lower())
        if tag:
            return tag
    return guild_initials_tag(guild_name)


def _is_home_alliance(
    db,
    alliance_id: str | None,
    alliance_tag: str | None,
) -> bool:
    home_alliance_id = str(db.get_config("home_alliance_id") or "").strip() or None
    if home_alliance_id and alliance_id and alliance_id == home_alliance_id:
        return True
    home_alliance_tag = alliance_display_tag(db.get_config("home_alliance_tag"))
    return bool(
        home_alliance_tag and alliance_tag
        and alliance_tag.lower() == home_alliance_tag.lower()
    )


def nickname_tag_for_profile(
    db,
    profile: dict | None,
    *,
    home_member: bool = False,
) -> str | None:
    """Return the tag that should prefix this registered member's nickname."""
    if home_member:
        return home_nick_tag(db)

    alliance_id, alliance_tag = _profile_alliance(db, profile)
    if _is_home_alliance(db, alliance_id, alliance_tag):
        guild_id, guild_name = _profile_guild(profile)
        guild_tag = _configured_or_derived_guild_tag(db, guild_id, guild_name)
        if guild_tag:
            return guild_tag
        return alliance_tag or alliance_display_tag(db.get_config("home_alliance_tag"))
    return alliance_tag


def tagged_nickname(albion_name: str, tag: str | None) -> str:
    name = str(albion_name or "").strip()
    clean_tag = clean_nick_tag(tag)
    return f"[{clean_tag}] {name}" if name and clean_tag else name


def tagged_nickname_for_profile(
    db,
    albion_name: str,
    profile: dict | None,
    *,
    home_member: bool = False,
) -> str:
    return tagged_nickname(
        albion_name,
        nickname_tag_for_profile(db, profile, home_member=home_member),
    )


def extract_tagged_nickname_name(display_name: str) -> str | None:
    """Return the name after a leading [TAG], regardless of the tag value."""
    match = _TAG_RE.match(str(display_name or "").strip())
    if not match:
        return None
    name = match.group(2).strip()
    return name or None


def managed_nickname_tags(db) -> set[str]:
    """Tags the bot knows it may have applied and can safely clean up."""
    tags = {DEFAULT_HOME_NICK_TAG, *LEGACY_HOME_NICK_TAGS, home_nick_tag(db)}
    home_alliance_tag = alliance_display_tag(db.get_config("home_alliance_tag"))
    if home_alliance_tag:
        tags.add(home_alliance_tag)
    try:
        for guild in db.fetch_all_guilds() or []:
            tag = alliance_display_tag(
                _row_get(guild, "alliance_tag"),
                _row_get(guild, "alliance_name"),
            )
            if tag:
                tags.add(tag)
            guild_tag = _configured_or_derived_guild_tag(
                db,
                _clean_id(_row_get(guild, "guild_id")),
                str(_row_get(guild, "guild_name") or "").strip() or None,
            )
            if guild_tag:
                tags.add(guild_tag)
    except Exception:
        pass
    tags.update(_guild_tag_overrides(db).values())
    return {t.lower() for t in tags if t}


def strip_managed_nickname_tag(db, display_name: str) -> str | None:
    """Strip a bot-managed leading tag from a nickname if one is present."""
    match = _TAG_RE.match(str(display_name or "").strip())
    if not match:
        return None
    tag = clean_nick_tag(match.group(1))
    if not tag or tag.lower() not in managed_nickname_tags(db):
        return None
    return match.group(2).strip() or None
