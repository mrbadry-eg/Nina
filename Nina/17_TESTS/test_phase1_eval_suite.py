"""Runs the Phase 1 evaluation suite under pytest so a plain `pytest` run covers it."""

from __future__ import annotations

import json

from silicium_loader import REPO_ROOT, load_module

_phase1 = load_module("14_EVALUATION/phase1_eval.py", "silicium_phase1_eval")


def test_phase1_cases_file_and_implementations_are_in_sync() -> None:
    data = json.loads((REPO_ROOT / "14_EVALUATION" / "cases" / "phase1_cases.json").read_text(encoding="utf-8"))
    assert [c["case_id"] for c in data["cases"]] == list(_phase1.CASES)
    assert len(data["cases"]) == 12
    assert all(c["severity"] in {"critical", "high", "medium", "low"} for c in data["cases"])


def test_every_phase1_evaluation_case_passes() -> None:
    results = _phase1.run_phase1_suite()
    failed = [r for r in results if r["result"] != "pass"]
    assert not failed, failed
    assert len(results) == 12
