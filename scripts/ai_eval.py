#!/usr/bin/env python3
"""Offline eval runner for UnionBot's AI helper knowledge.

This checks deterministic behavior only:
- fast Albion glossary answers
- fast server-workflow answers
- markdown knowledge retrieval doc selection

It intentionally does not call OpenAI, Ollama, Discord, or the production DB.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cogs.ai_assistant import (  # noqa: E402
    _quick_albion_answer,
    _quick_workflow_answer,
    _rank_knowledge_sections,
)

DEFAULT_CASE_FILE = REPO_ROOT / "docs" / "bot_knowledge" / "ai_eval_cases.json"
DEFAULT_CHANNELS = {
    "registration": "<#register>",
    "event_board": "<#event-board>",
    "lfg_posts": "<#lfg>",
    "content_roles": "<#content-roles>",
    "weapon_roles": "<#weapon-roles>",
    "help": "<#help>",
    "regear": "<#regear>",
    "bounties": "<#bounties>",
    "sso_routes": "<#sso-routes>",
    "market": "<#market>",
    "votes": "<#votes>",
    "server_guide": "<#server-guide>",
    "rules": "<#rules>",
    "application": "<#apply>",
    "staff_apps": "<#staff-apps>",
    "announcements": "<#announcements>",
    "bot_commands": "<#bot-commands>",
    "alliance_info": "<#alliance-info>",
    "alliance_events": "<#alliance-events>",
    "alliance_chat": "<#alliance-chat>",
    "martlock_info": "<#martlock-info>",
    "martlock_lfg": "<#martlock-lfg>",
    "faction_chat": "<#faction-chat>",
    "guest_info": "<#guest-info>",
    "guest_chat": "<#guest-chat>",
    "content_planning": "<#content-planning>",
    "comps": "<#comps>",
    "shotcalling_sop": "<#shotcalling-sop>",
    "battle_vods": "<#battle-vods>",
    "suggestions": "<#suggestions>",
    "flex": "<#flex>",
    "hall_of_fame": "<#hall-of-fame>",
    "union_lore": "<#union-lore>",
    "voice_lounge": "<#voice-lounge>",
    "content_voice": "<#content-voice>",
}


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    passed: bool
    detail: str


def load_cases(path: Path = DEFAULT_CASE_FILE) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError(f"{path} must contain a top-level cases list")
    return [case for case in cases if isinstance(case, dict)]


def _has_all(answer: str, needles: list[str]) -> tuple[bool, list[str]]:
    answer_lc = answer.lower()
    missing = [needle for needle in needles if str(needle).lower() not in answer_lc]
    return not missing, missing


def _has_none(answer: str, needles: list[str]) -> tuple[bool, list[str]]:
    answer_lc = answer.lower()
    present = [needle for needle in needles if str(needle).lower() in answer_lc]
    return not present, present


def top_retrieval_docs(question: str, *, top_k: int) -> list[tuple[int, str, str]]:
    return [
        (score, filename, heading)
        for score, filename, heading, _text in _rank_knowledge_sections(
            question,
            limit=max(1, int(top_k)),
        )
    ]


def evaluate_case(case: dict[str, Any]) -> EvalResult:
    case_id = str(case.get("id") or "unknown")
    case_type = str(case.get("type") or "").strip()
    question = str(case.get("question") or "")
    must_include = [str(item) for item in case.get("must_include", [])]
    must_not_include = [str(item) for item in case.get("must_not_include", [])]

    if case_type == "quick_albion":
        answer = _quick_albion_answer(question) or ""
        ok, missing = _has_all(answer, must_include)
        clean, present = _has_none(answer, must_not_include)
        passed = bool(answer) and ok and clean
        detail = "ok" if passed else f"answer={answer!r}; missing={missing}; forbidden={present}"
        return EvalResult(case_id, passed, detail)

    if case_type == "quick_workflow":
        answer = _quick_workflow_answer(question, DEFAULT_CHANNELS) or ""
        ok, missing = _has_all(answer, must_include)
        clean, present = _has_none(answer, must_not_include)
        passed = bool(answer) and ok and clean
        detail = "ok" if passed else f"answer={answer!r}; missing={missing}; forbidden={present}"
        return EvalResult(case_id, passed, detail)

    if case_type == "retrieval":
        top_k = int(case.get("top_k") or 5)
        expected = {str(doc) for doc in case.get("expected_docs", [])}
        docs = top_retrieval_docs(question, top_k=top_k)
        found = {filename for _score, filename, _heading in docs}
        passed = bool(expected) and bool(expected & found)
        detail = "ok" if passed else f"expected={sorted(expected)}; top_docs={docs}"
        return EvalResult(case_id, passed, detail)

    return EvalResult(case_id, False, f"unknown case type {case_type!r}")


def evaluate_cases(cases: list[dict[str, Any]]) -> list[EvalResult]:
    return [evaluate_case(case) for case in cases]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run UnionBot offline AI eval cases.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASE_FILE, help="Path to ai_eval_cases.json")
    parser.add_argument("--verbose", action="store_true", help="Print passing cases too")
    args = parser.parse_args()

    results = evaluate_cases(load_cases(args.cases))
    failed = [result for result in results if not result.passed]
    for result in results:
        if args.verbose or not result.passed:
            status = "PASS" if result.passed else "FAIL"
            print(f"{status} {result.case_id}: {result.detail}")
    print(f"{len(results) - len(failed)}/{len(results)} AI eval cases passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
