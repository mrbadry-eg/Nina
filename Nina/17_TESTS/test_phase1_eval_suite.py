"""Runs the Phase 1 evaluation suite under pytest so a plain `pytest` run covers it."""

from __future__ import annotations

import json

import pytest

from silicium_loader import REPO_ROOT, load_module

_phase1 = load_module("14_EVALUATION/phase1_eval.py", "silicium_phase1_eval")


def test_phase1_cases_file_and_implementations_are_in_sync() -> None:
    data = json.loads((REPO_ROOT / "14_EVALUATION" / "cases" / "phase1_cases.json").read_text(encoding="utf-8"))
    assert [c["case_id"] for c in data["cases"]] == list(_phase1.CASES)
    assert len(data["cases"]) == 13
    assert all(c["severity"] in {"critical", "high", "medium", "low"} for c in data["cases"])


def test_every_phase1_evaluation_case_passes() -> None:
    results = _phase1.run_phase1_suite()
    failed = [r for r in results if r["result"] != "pass"]
    assert not failed, failed
    assert len(results) == 13


def test_restricted_offline_eval_detects_hosted_fallback_mutation(monkeypatch, tmp_path) -> None:
    router_class = _phase1._router.ModelRouter
    original = router_class._policy_safe_chain

    def allow_all_endpoints(self, chain, data_class):
        if data_class == "restricted":
            return chain
        return original(self, chain, data_class)

    monkeypatch.setattr(router_class, "_policy_safe_chain", allow_all_endpoints)

    passed, details = _phase1.CASES["EVAL-SECURITY-018"](tmp_path)

    assert passed is False
    assert "hosted_prompt_never_sent=False" in details
