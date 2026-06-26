from __future__ import annotations

from scripts.ai_eval import evaluate_cases, load_cases


def test_ai_eval_cases_pass() -> None:
    results = evaluate_cases(load_cases())
    failures = [result for result in results if not result.passed]

    assert not failures, "\n".join(
        f"{result.case_id}: {result.detail}"
        for result in failures
    )
