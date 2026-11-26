"""Phase 1 / Layer 2 (gateway integration): freshness precondition and approval-store state guards."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silicium_loader import load_module

_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_gw = load_module("08_TOOLS_MCP/tool_gateway.py", "silicium_tool_gateway")
_tr = load_module("04_SOURCE_OF_TRUTH/truth_resolver.py", "silicium_truth_resolver")

NOTE_INPUTS = {"order_id": "ORD-1042", "note": "SLA breach: unfulfilled > 48h", "idempotency_key": "IDEM-GW-FRESH-1"}


def _env(tmp_path: Path, *, with_resolver: bool = True):
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc")
    approvals = _approval.ApprovalStore()
    resolver = _tr.TruthResolver(audit_logger=audit) if with_resolver else None
    gw = _gw.ToolGateway(approvals=approvals, audit_logger=audit, truth_resolver=resolver)
    return gw, approvals, audit, resolver


def _approved(approvals, inputs=NOTE_INPUTS):
    req = approvals.create_request(
        task_id="TASK-20261125-FRS1", requesting_agent="agent.operations", tool_name="shopify.orders.add_internal_note",
        action_description="Add SLA note", risk_level="medium", domain="operations", resource_affected="shopify:order:ORD-1042",
        proposed_action=inputs, reversible=True, estimated_impact="Internal note only",
    )
    approvals.approve(req["request_id"])
    return req["request_id"]


def _fresh_evidence(gw, task="TASK-20261125-FRS2"):
    read = gw.invoke(task_id=task, agent_id="agent.operations", environment="dev", tool_name="shopify.orders.get", inputs={"order_id": "ORD-1042"})
    return _tr.TruthResolver().claims_from_order_result(read)


def _write(gw, apr, evidence=None, now=None, inputs=NOTE_INPUTS):
    return gw.invoke(
        task_id="TASK-20261125-FRS1", agent_id="agent.operations", environment="dev",
        tool_name="shopify.orders.add_internal_note", inputs=inputs, approval_id=apr, evidence=evidence, now=now,
    )


def test_fresh_evidence_allows_approved_write(tmp_path: Path) -> None:
    gw, approvals, _, _ = _env(tmp_path)
    apr = _approved(approvals)
    res = _write(gw, apr, evidence=_fresh_evidence(gw))
    assert res["status"] == "success" and res["verified"] is True


def test_stale_evidence_blocks_without_consuming_the_approval(tmp_path: Path) -> None:
    gw, approvals, _, _ = _env(tmp_path)
    apr = _approved(approvals)
    evidence = _fresh_evidence(gw)
    later = datetime.now(timezone.utc) + timedelta(seconds=301)
    blocked = _write(gw, apr, evidence=evidence, now=later)
    assert blocked["status"] == "blocked" and blocked["error"]["code"] == "STALE_LIVE_DATA"
    assert approvals._requests[apr]["consumed"] is False  # single-use approval is still intact
    # Re-reading and retrying with the same approval succeeds.
    ok = _write(gw, apr, evidence=_fresh_evidence(gw, "TASK-20261125-FRS3"))
    assert ok["status"] == "success"
    assert approvals._requests[apr]["consumed"] is True


def test_evidence_without_resolver_fails_closed(tmp_path: Path) -> None:
    gw, approvals, _, _ = _env(tmp_path, with_resolver=False)
    apr = _approved(approvals)
    evidence = _fresh_evidence(_env(tmp_path / "x", with_resolver=True)[0])
    res = _write(gw, apr, evidence=evidence)
    assert res["status"] == "blocked" and res["error"]["code"] == "PRECONDITION_FAILED"
    assert approvals._requests[apr]["consumed"] is False


def test_evidence_about_another_order_is_rejected(tmp_path: Path) -> None:
    gw, approvals, _, _ = _env(tmp_path)
    apr = _approved(approvals)
    foreign = [_tr.Claim("order:ORD-2000:fulfillment_status", "unfulfilled", 3, "tool:x", datetime.now(timezone.utc))]
    res = _write(gw, apr, evidence=foreign)
    assert res["status"] == "blocked" and res["error"]["code"] == "VALIDATION_ERROR"
    assert approvals._requests[apr]["consumed"] is False


def test_hash_binding_still_enforced_with_fresh_evidence(tmp_path: Path) -> None:
    gw, approvals, _, _ = _env(tmp_path)
    apr = _approved(approvals)
    tampered = {**NOTE_INPUTS, "note": "different payload"}
    res = _write(gw, apr, evidence=_fresh_evidence(gw), inputs=tampered)
    assert res["status"] == "blocked" and res["error"]["code"] == "APPROVAL_HASH_MISMATCH"


def test_l5_is_blocked_regardless_of_evidence(tmp_path: Path) -> None:
    gw, _, _, _ = _env(tmp_path)
    res = gw.invoke(
        task_id="TASK-20261125-FRS4", agent_id="agent.operations", environment="dev", tool_name="shopify.orders.refund",
        inputs={"order_id": "ORD-1042"}, evidence=_fresh_evidence(gw),
    )
    assert res["status"] == "blocked" and res["error"]["code"] == "L5_PROHIBITED"


def test_calls_without_evidence_behave_exactly_as_before(tmp_path: Path) -> None:
    gw, approvals, audit, _ = _env(tmp_path)
    apr = _approved(approvals)
    assert _write(gw, apr)["status"] == "success"
    events = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["outcome"] for e in events] == ["success"]


# ------------------------------------------------ approval store hardening (state guards)


def test_rejected_or_expired_requests_cannot_be_approved(tmp_path: Path) -> None:
    approvals = _approval.ApprovalStore()
    base = dict(
        task_id="TASK-20261125-FRS5", requesting_agent="agent.operations", tool_name="shopify.orders.add_internal_note",
        action_description="Add SLA note to order", risk_level="medium", domain="operations", resource_affected="shopify:order:ORD-1042",
        proposed_action=NOTE_INPUTS, reversible=True, estimated_impact="Internal note only",
    )
    rejected = approvals.create_request(**base)
    approvals.reject(rejected["request_id"], "no")
    with pytest.raises(_approval.ApprovalError):
        approvals.approve(rejected["request_id"])

    expired = approvals.create_request(**base)
    with pytest.raises(_approval.ApprovalError):
        approvals.approve(expired["request_id"], now=datetime.now(timezone.utc) + timedelta(minutes=31))
    with pytest.raises(_approval.ApprovalError):  # stays dead after expiry
        approvals.approve(expired["request_id"])

    approved = approvals.create_request(**base)
    approvals.approve(approved["request_id"])
    with pytest.raises(_approval.ApprovalError):
        approvals.reject(approved["request_id"], "too late")
