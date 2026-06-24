"""Formatting helpers shared by event report embeds and UI."""
from __future__ import annotations

import datetime as dt

import discord


def discord_ts(value: dt.datetime | None, style: str = "f") -> str:
    if value is None:
        return "unknown"
    return f"<t:{int(value.timestamp())}:{style}>"


def fmt_num(value: int | float | None) -> str:
    n = int(value or 0)
    sign = "-" if n < 0 else ""
    n_abs = abs(n)
    if n_abs >= 1_000_000:
        return f"{sign}{n_abs / 1_000_000:.1f}M"
    if n_abs >= 1_000:
        return f"{sign}{n_abs / 1_000:.1f}K"
    return f"{n:,}"


def clamp_text(text: str, limit: int = 1024) -> str:
    text = str(text or "").strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)].rstrip() + "\n...(truncated)"


def chunk_lines(lines: list[str], *, limit: int = 1000) -> list[str]:
    """Split lines into Discord-field-safe chunks without dropping rows."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for raw in lines:
        line = str(raw or "-").strip() or "-"
        line_len = len(line) + (1 if current else 0)
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if len(line) > limit:
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            if line:
                current = [line]
                current_len = len(line)
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks or ["-"]


def embed_text_size(embed: discord.Embed) -> int:
    """Best-effort count for Discord's per-message embed text budget."""
    data = embed.to_dict()
    total = (
        len(data.get("title") or "")
        + len(data.get("description") or "")
        + len((data.get("footer") or {}).get("text") or "")
        + len((data.get("author") or {}).get("name") or "")
    )
    for field in data.get("fields") or []:
        total += len(field.get("name") or "") + len(field.get("value") or "")
    return total


def batch_embeds_for_send(
    embeds: list[discord.Embed],
    *,
    max_count: int = 10,
    max_text: int = 5800,
) -> list[list[discord.Embed]]:
    """Batch embeds within Discord's count and combined text limits."""
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_size = 0
    for embed in embeds:
        size = embed_text_size(embed)
        if current and (
            len(current) >= max_count
            or current_size + size > max_text
        ):
            batches.append(current)
            current = []
            current_size = 0
        current.append(embed)
        current_size += size
    if current:
        batches.append(current)
    return batches
