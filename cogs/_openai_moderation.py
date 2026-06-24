from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


DEFAULT_MODERATION_MODEL = "omni-moderation-latest"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class ModerationResult:
    flagged: bool
    categories: dict[str, bool]
    category_scores: dict[str, float]

    @property
    def flagged_categories(self) -> list[str]:
        return [name for name, flagged in self.categories.items() if flagged]

    @property
    def top_scores(self) -> list[tuple[str, float]]:
        return sorted(
            self.category_scores.items(),
            key=lambda item: item[1],
            reverse=True,
        )


async def moderate_text(
    *,
    api_key: str,
    text: str,
    model: str = DEFAULT_MODERATION_MODEL,
    base_url: str = DEFAULT_OPENAI_BASE_URL,
    timeout_sec: int = 12,
) -> ModerationResult:
    """Check text with OpenAI's moderation endpoint.

    This helper is shared by the public moderation cog and the AI assistant so
    both use the same model, response parsing, and timeout behavior.
    """
    clean_text = str(text or "").strip()
    if not clean_text:
        return ModerationResult(flagged=False, categories={}, category_scores={})

    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "input": clean_text,
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(f"{base_url.rstrip('/')}/moderations", json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise RuntimeError(f"OpenAI moderation HTTP {resp.status}: {body[:300]}")
            data = await resp.json(content_type=None)

    result = ((data.get("results") or [{}])[0]) if isinstance(data, dict) else {}
    categories = result.get("categories") or {}
    scores = result.get("category_scores") or {}
    return ModerationResult(
        flagged=bool(result.get("flagged")),
        categories={str(key): bool(value) for key, value in dict(categories).items()},
        category_scores={
            str(key): float(value)
            for key, value in dict(scores).items()
            if isinstance(value, (int, float))
        },
    )
