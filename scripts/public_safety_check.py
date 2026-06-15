#!/usr/bin/env python3
"""Fail-fast checks before publishing UnionBot.

This is intentionally simple and conservative. It looks for files and strings
that should not appear in a public release. Add your own guild-specific terms
to ``BANNED_TEXT`` before publishing a fork.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BANNED_FILE_NAMES = {
    ".env",
    "database.db",
    "bot.log",
    "connection.log",
    "bot.stdout.log",
}

BANNED_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
}

BANNED_PATH_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}

BANNED_TEXT = {
    "OPENAI_API_KEY=sk-": "Looks like a real OpenAI key.",
    "Barbatos": "Private operator name from the original bot.",
    "TravelersUnion": "Private source guild name from the original bot.",
    "TRAVELERS UNION": "Private source server name from the original bot.",
    "1234370360451792998": "Private source Discord server ID.",
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".service",
    ".example",
    ".yml",
    ".yaml",
    ".json",
}

SECRET_ASSIGNMENTS = ("DISCORD_TOKEN", "OPENAI_API_KEY")
PLACEHOLDER_VALUES = {
    "",
    "...",
    "your-token",
    "your-discord-token",
    "your-openai-key",
    "<your-token>",
    "<your-openai-key>",
}


def should_skip(path: Path) -> bool:
    return any(part in BANNED_PATH_PARTS for part in path.parts)


def main() -> int:
    failures: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if should_skip(rel):
            continue
        if path.is_dir():
            continue
        if path.name in BANNED_FILE_NAMES:
            failures.append(f"banned file name: {rel}")
        if path.suffix in BANNED_SUFFIXES:
            failures.append(f"banned file suffix: {rel}")
        if rel.as_posix() == "scripts/public_safety_check.py":
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            for key in SECRET_ASSIGNMENTS:
                prefix = f"{key}="
                if not stripped.startswith(prefix):
                    continue
                value = stripped.removeprefix(prefix).strip().strip('"').strip("'")
                if value not in PLACEHOLDER_VALUES:
                    failures.append(f"possible secret in {rel}:{line_number}: {key}")
        for needle, reason in BANNED_TEXT.items():
            if needle in text:
                failures.append(f"banned text in {rel}: {needle!r} ({reason})")

    if failures:
        print("Public safety check failed:")
        for item in failures:
            print(f"- {item}")
        return 1
    print("Public safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
