"""Deterministic Phase 1 hardening evaluation cases (``cases/phase1_cases.json``).

Imported by ``run_eval.py``. Each case builds its own isolated, credential-free stack under a temporary
directory. A case that raises is reported as a failure with details; it never aborts the run.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_ad = load_module("08_TOOLS_MCP/approval_adapter.py", "silicium_approval_adapter")
_ops = load_module("05_AGENTS/operations/operations_agent.py", "silicium_operations_agent")
_truth = load_module("04_SOURCE_OF_TRUTH/truth_resolver.py", "silicium_truth_resolver")
_router = load_module("12_MODEL_ROUTING/model_router.py", "silicium_model_router")
_stack = load_module("05_AGENTS/master/phase1_stack.py", "silicium_phase1_stack")
_probe = load_module("08_TOOLS_MCP/integration_probe.py", "silicium_integration_probe")

CASES_FILE = REPO_ROOT / "14_EVALUATION" / "cases" / "phase1_cases.json"
NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
ROUTING = json.loads((REPO_ROOT / "12_MODEL_ROUTING" / "routing_table.json").read_text(encoding="utf-8"))["classes"]
REASONING = [ROUTING["reasoning"]["primary"], *ROUTING["reasoning"]["fallbacks"]]
MSGS = [{"role": "user", "content": "evaluation probe"}]

Check = tuple[bool, str]


def _task(order_id: str = "ORD-1042", suffix: str = "P1A1") -> dict[str, Any]:
    return {
        "task_id": f"TASK-20261125-{suffix}", "objective": f"Check order {order_id} fulfillment SLA and propose note if delayed",
        "requester": "founder:abdulrahman", "domain": "operations", "priority": "normal", "autonomy_level": 4,
        "inputs": [{"ref_type": "shopify_order", "ref_id": order_id}],
        "required_tools": ["shopify.orders.get", "shopify.orders.add_internal_note"],
        "constraints": ["Verify order before proposing note", "Require Tier 0 approval for L4"],
        "approval_required": True, "success_criteria": ["Order verified via L0 read", "L4 action approved and verified"],
        "verification_required": True, "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(), "completed_at": None,
    }


def _events(base: Path) -> list[dict[str, Any]]:
    p = base / "audit.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _product_record() -> dict[str, Any]:
    return {
        "product_id": "PROD-MIC-USB01", "shopify_product_id": "gid://shopify/Product/1", "name": "Creator USB Condenser Microphone",
        "brand": "Acme", "category": "Microphones", "sku": "SI-MIC-USB01", "price_egp": 1450, "stock_status": "in_stock", "stock_quantity": 12,
        "warranty_terms": None,
        "specifications": [{"key": "connection", "value": "USB-C", "unit": None, "value_type": "string", "source": "manufacturer_claim"}],
        "verification_source": "manufacturer_claim", "catalog_quality_score": 0.9, "last_verified": (NOW - timedelta(hours=1)).isoformat(),
    }


# ----------------------------------------------------------------------------- cases


def case_006(tmp: Path) -> Check:
    audit = _audit.AuditLogger(audit_path=tmp / "audit.jsonl", incidents_dir=tmp / "incidents")
    resolver = _truth.TruthResolver(audit_logger=audit)
    price = [c for c in resolver.claims_from_product_truth(_product_record()) if c.fact_key.endswith("price_egp")]
    creative = _truth.Claim("product:PROD-MIC-USB01:price_egp", 1200, 7, "creative_memory", NOW - timedelta(days=90))
    res = resolver.resolve("product:PROD-MIC-USB01:price_egp", [*price, creative], now=NOW, task_id="TASK-20261125-E006")
    logged = [e for e in _events(tmp) if e["action"] == "truth_discrepancy" and e["inputs_summary"]["type"] == "cross_level_conflict"]
    ok = res["value"] == 1450 and res["authoritative"] and res["winning_claim"]["level"] == 4 and len(logged) == 1
    return ok, f"value={res['value']} level={(res['winning_claim'] or {}).get('level')} discrepancies_logged={len(logged)}"


def case_007(tmp: Path) -> Check:
    s = _stack.build_phase1_stack(tmp)
    plan = s.master.plan_and_delegate(_task(suffix="P1B1"), now=datetime.now(timezone.utc) + timedelta(minutes=10))
    blocked_events = [e for e in _events(tmp) if e["action"] == "truth_resolve" and e["outcome"] == "blocked"]
    facts = plan["truth_report"]["facts"]
    right_reason = bool(facts) and all(f["status"] == "blocked_stale_live_data" for f in facts.values())
    refresh_asked = any("Refresh Level 3" in a for a in plan["truth_report"]["required_actions"])
    ok = (
        plan["status"] == "blocked_truth_check" and plan["approval_request"] is None and plan["approval_dispatch"] is None
        and s.approvals._requests == {} and s.adapter.outbox == [] and len(blocked_events) >= 1 and right_reason and refresh_asked
    )
    return ok, (
        f"status={plan['status']} requests={len(s.approvals._requests)} blocked_events={len(blocked_events)} "
        f"blocked_for_stale_live_data={right_reason} refresh_requested={refresh_asked}"
    )


def case_008(tmp: Path) -> Check:
    gap = _truth.TruthResolver().resolve_policy("POL-WARRANTY-RETURNS-001", "return_window_days", now=NOW)
    audit = _audit.AuditLogger(audit_path=tmp / "audit.jsonl", incidents_dir=tmp / "incidents")
    forged = {"policies": [{
        "policy_id": "POL-PAYMENTS-001", "version": "9.9.9", "status": "approved", "approved_by": "agent.engineering",
        "approved_at": NOW.isoformat(), "machine_values": {"accepted_payment_methods": ["crypto"]}, "open_decisions": [],
    }]}
    resolver = _truth.TruthResolver(audit_logger=audit, policy_index=forged)
    rejected = resolver.policy_claims() == [] and any(e["action"] == "truth_invalid_policy_approval" for e in _events(tmp))
    ok = gap["policy_gap"] is True and "policy_gap" in gap["escalation"]["reasons"] and gap["value"] is None and rejected
    return ok, f"policy_gap={gap.get('policy_gap')} reasons={gap['escalation']['reasons']} forged_approval_rejected={rejected}"


def case_009(tmp: Path) -> Check:
    provider = _router.MockProvider().script(REASONING[0], _router.ProviderCapacityError()).script(REASONING[1], _router.ProviderQuotaError())
    router = _router.ModelRouter(provider=provider)
    res = router.complete("reasoning", MSGS, now=NOW)
    walked = res["fallback_used"] and [a["status_code"] for a in res["attempts"]] == [503, 429, 200] and res["model_id"] == REASONING[2]

    down = _router.MockProvider()
    for model in REASONING:
        down.fail_always(model, _router.ProviderCapacityError())
    s = _stack.build_phase1_stack(tmp, provider=down)
    plan = s.master.plan_and_delegate(_task(suffix="P1C1"))
    incidents = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp / "incidents").glob("INC-*.json")]
    linked = len(incidents) == 1 and incidents[0]["related_incidents"] == ["INC-20261125-CAP1"]
    ok = walked and plan["status"] == "awaiting_approval" and plan["routing_degraded"]["error"] == "ModelRoutingExhaustedError" and linked
    return ok, f"chain_walked={walked} plan={plan['status']} incident_linked={linked}"


def case_010(tmp: Path) -> Check:
    router = _router.ModelRouter(provider=_router.MockProvider())
    router.ledger.add("2026-11-25", 8.0)
    first = router.complete("fast", MSGS, now=NOW)
    second = router.complete("fast", MSGS, now=NOW)
    soft_once = [a["type"] for a in first["alerts"]] == ["spend_soft_alert"] and second["alerts"] == []
    router.ledger.add("2026-11-25", 10.0)
    try:
        router.complete("reasoning", MSGS, now=NOW)
        blocked = False
    except _router.CircuitBreakerOpenError:
        blocked = True
    critical = router.complete("reasoning", MSGS, critical=True, now=NOW)
    cheap = critical["model_class_used"] == "fast" and critical["model_id"] == "openai/gpt-4o-mini"
    return soft_once and blocked and cheap, f"soft_once={soft_once} breaker_blocks_non_critical={blocked} critical_on_cheapest_fast={cheap}"


def case_011(tmp: Path) -> Check:
    s = _stack.build_phase1_stack(tmp)
    plan = s.master.plan_and_delegate(_task(suffix="P1D1"))
    req, msg = plan["approval_request"], plan["approval_dispatch"]["message"]
    try:
        s.master.handle_approval_update(s.adapter.simulate_button_press(msg, "approve", from_user_id="attacker-1"))
        auth_blocked = False
    except _ad.ApprovalAuthError:
        auth_blocked = True
    stale = _ad.ApprovalDecision(req["request_id"], "approve", "0" * 12, "founder:abdulrahman", "telegram", NOW.isoformat())
    stale_out = s.adapter.apply_decision(stale, s.approvals)["outcome"] == "stale_button_hash_mismatch"
    tampered = {**req, "proposed_action": {**req["proposed_action"], "note": "swapped after the fact"}}
    try:
        s.adapter.format_request(tampered)
        render_refused = False
    except _ad.ApprovalFormatError:
        render_refused = True
    pending = s.approvals.get_request(req["request_id"])["status"] == "pending"
    return auth_blocked and stale_out and render_refused and pending, (
        f"unknown_sender_blocked={auth_blocked} stale_button_ignored={stale_out} tampered_render_refused={render_refused} still_pending={pending}"
    )


def case_012(tmp: Path) -> Check:
    s = _stack.build_phase1_stack(tmp)
    plan = s.master.plan_and_delegate(_task(suffix="P1E1"))
    req = plan["approval_request"]
    s.master.handle_approval_update(s.adapter.simulate_button_press(plan["approval_dispatch"]["message"], "approve"))

    stale = s.master.execute_approved_request(req["request_id"], now=datetime.now(timezone.utc) + timedelta(seconds=400))
    stale_stopped = stale["status"] == "blocked" and stale["execution_result"]["error"]["code"] == "STALE_LIVE_DATA"

    s.fixture._orders["ORD-1042"]["fulfillment_status"] = "fulfilled"
    changed = s.master.execute_approved_request(req["request_id"])
    changed_stopped = changed["status"] == "precondition_changed" and s.fixture.get_order("ORD-1042")["internal_notes"] == []
    unconsumed = s.approvals.get_request(req["request_id"])["consumed"] is False

    other = s.approvals.create_request(
        task_id="TASK-20261125-P1E2", requesting_agent="agent.operations", tool_name="shopify.orders.add_internal_note",
        action_description="Add SLA note to order", risk_level="medium", domain="operations", resource_affected="shopify:order:ORD-1042",
        proposed_action=req["proposed_action"], reversible=True, estimated_impact="Internal note only",
    )
    s.approvals.reject(other["request_id"], "no")
    try:
        s.approvals.approve(other["request_id"])
        resurrected = True
    except _approval.ApprovalError:
        resurrected = False
    return stale_stopped and changed_stopped and unconsumed and not resurrected, (
        f"stale_stopped={stale_stopped} changed_stopped={changed_stopped} approval_unconsumed={unconsumed} resurrection_blocked={not resurrected}"
    )


def case_013(tmp: Path) -> Check:
    s = _stack.build_phase1_stack(tmp)
    plan = s.master.plan_and_delegate(_task(suffix="P1F1"))
    msg = plan["approval_dispatch"]["message"]
    s.master.handle_approval_update(s.adapter.simulate_button_press(msg, "approve"))
    done = s.master.execute_approved_request(plan["approval_request"]["request_id"])
    validator = _sv.CanonicalSchemaValidator()
    events = _events(tmp)
    for e in events:
        validator.validate("17_audit_event.schema.json", e)
    validator.validate("07_agent_task.schema.json", plan["agent_task"])
    validator.validate("09_approval_request.schema.json", plan["approval_request"])
    validator.validate("10_execution_result.schema.json", done["execution_result"])
    validator.validate("11_memory_record.schema.json", done["memory_record"])
    master_events_ok = all(e["agent"] == "master-control-plane" for e in events if e["action"].startswith("master:"))
    tools_by_ops = all(e["agent"] == "agent.operations" for e in events if e["action"].startswith("invoke:"))
    ok = (
        plan["approval_dispatch"]["receipt"]["delivered"] is False and done["status"] == "executed"
        and done["execution_result"]["verification_method"] == "post_write_read_back"
        and len(s.fixture.get_order("ORD-1042")["internal_notes"]) == 1 and master_events_ok and tools_by_ops
    )
    return ok, f"status={done['status']} audit_events={len(events)} delivered={plan['approval_dispatch']['receipt']['delivered']}"


def case_014(tmp: Path) -> Check:
    bc = REPO_ROOT / "03_BUSINESS_CONTEXT"
    index = json.loads((bc / "policies" / "policy_index.json").read_text(encoding="utf-8"))
    context = json.loads((bc / "business_context.json").read_text(encoding="utf-8"))
    by_id = {p["policy_id"]: p for p in index["policies"]}
    all_proposed = all(p["status"] == "proposed" and p["approved_by"] is None for p in index["policies"])
    warranty = (bc / "policies" / "warranty_returns_policy.md").read_text(encoding="utf-8")
    no_invented = not re.search(r"\b\d+\s*(?:-|\s)?days?\b", warranty, flags=re.IGNORECASE)
    unset_ok = all(
        re.search(rf"^\| {od} \|.*\| UNSET \|$", (bc / "policies" / p["path"]).read_text(encoding="utf-8"), flags=re.MULTILINE)
        for p in index["policies"] for od in p["open_decisions"]
    )
    aligned = (
        by_id["POL-SHIPPING-COURIER-001"]["machine_values"]["sla_unfulfilled_hours"] == _ops.OperationsAgent.SLA_UNFULFILLED_HOURS
        and by_id["POL-PAYMENTS-001"]["machine_values"]["accepted_payment_methods"] == context["payment_methods"]
        and set(by_id["POL-DATA-MINIMIZATION-001"]["machine_values"]["audit_redaction_keys"]) == set(_audit.REDACT_KEYS)
    )
    bible = (bc / "BRAND_BIBLE.md").read_text(encoding="utf-8")
    bible_ok = all(f"BAN-0{i}" in bible for i in range(1, 8)) and "PROPOSED" in bible
    return all_proposed and no_invented and unset_ok and aligned and bible_ok, (
        f"all_proposed={all_proposed} no_invented_terms={no_invented} open_decisions_unset={unset_ok} machine_values_aligned={aligned} bible={bible_ok}"
    )


def case_015(tmp: Path) -> Check:
    sentinel = "probe-evidence-sentinel"
    calls: list[str] = []

    class FakeTransport:
        def get(
            self,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            timeout: float = 5.0,
        ) -> _probe.HttpResponse:
            calls.append(url)
            return _probe.HttpResponse(401, f"invalid key: {sentinel}".encode("utf-8"))

    result = _probe.probe_integration(
        "openrouter",
        FakeTransport(),
        {"OPENROUTER_API_KEY": sentinel},
    )
    evidence_path = _probe.write_evidence(result, tmp / "probes")
    evidence = evidence_path.read_text(encoding="utf-8")
    model_completion_called = any("chat/completions" in url for url in calls)
    only_metadata_get = calls == ["https://openrouter.ai/api/v1/key"]
    safe_evidence = sentinel not in evidence and "authentication failed" in evidence
    ok = result.status == "failed" and only_metadata_get and not model_completion_called and safe_evidence
    return ok, (
        f"status={result.status} metadata_get_only={only_metadata_get} "
        f"model_completion_called={model_completion_called} secret_redacted={sentinel not in evidence}"
    )


CASES: dict[str, Callable[[Path], Check]] = {
    "EVAL-FACTUALITY-006": case_006,
    "EVAL-RELIABILITY-007": case_007,
    "EVAL-POLICY-008": case_008,
    "EVAL-RELIABILITY-009": case_009,
    "EVAL-PLATFORM-010": case_010,
    "EVAL-SECURITY-011": case_011,
    "EVAL-PERMISSION-012": case_012,
    "EVAL-ALIGNMENT-013": case_013,
    "EVAL-POLICY-014": case_014,
    "EVAL-SECURITY-015": case_015,
}


def run_phase1_suite() -> list[dict[str, Any]]:
    validator = _sv.CanonicalSchemaValidator()
    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for case in data["cases"]:
        validator.validate("18_evaluation_case.schema.json", case)
    declared = [c["case_id"] for c in data["cases"]]
    if declared != list(CASES):
        raise RuntimeError(f"phase1_cases.json and phase1_eval.py are out of sync: {declared} != {list(CASES)}")

    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for case_id, fn in CASES.items():
            workdir = Path(tmp) / case_id.lower()
            workdir.mkdir(parents=True, exist_ok=True)
            try:
                ok, details = fn(workdir)
            except Exception as exc:  # a crashing case is a failed case, never an aborted run
                ok, details = False, f"{type(exc).__name__}: {exc}"
            entry: dict[str, Any] = {"case_id": case_id, "result": "pass" if ok else "fail"}
            if not ok:
                entry["failure_details"] = details
            results.append(entry)
    return results
