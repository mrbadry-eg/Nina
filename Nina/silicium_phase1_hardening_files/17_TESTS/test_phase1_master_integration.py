"""Phase 1 / Layer 5: MasterAgent wired to truth resolver, model router, gateway, approval adapter, memory and audit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_ad = load_module("08_TOOLS_MCP/approval_adapter.py", "silicium_approval_adapter")
_gw = load_module("08_TOOLS_MCP/tool_gateway.py", "silicium_tool_gateway")
_mem = load_module("07_MEMORY/memory_store.py", "silicium_memory_store")
_ops = load_module("05_AGENTS/operations/operations_agent.py", "silicium_operations_agent")
_mr = load_module("12_MODEL_ROUTING/model_router.py", "silicium_model_router")
_master = load_module("05_AGENTS/master/master_agent.py", "silicium_master_agent")
_stack = load_module("05_AGENTS/master/phase1_stack.py", "silicium_phase1_stack")

VALIDATOR = _sv.CanonicalSchemaValidator()
TABLE = json.loads((REPO_ROOT / "12_MODEL_ROUTING" / "routing_table.json").read_text(encoding="utf-8"))
REASONING = TABLE["classes"]["reasoning"]


def _task(order_id: str = "ORD-1042") -> dict:
    return {
        "task_id": "TASK-20261125-INT1", "objective": f"Inspect {order_id} fulfillment status", "requester": "founder:abdulrahman",
        "domain": "operations", "priority": "normal", "autonomy_level": 4,
        "inputs": [{"ref_type": "shopify_order", "ref_id": order_id}],
        "required_tools": ["shopify.orders.get", "shopify.orders.add_internal_note"], "constraints": ["Verify before write"],
        "approval_required": True, "success_criteria": ["Verified L0 read", "Approved L4 note"], "verification_required": True,
        "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(), "completed_at": None,
    }


def _events(stack) -> list[dict]:
    p = stack.base_dir / "audit.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _actions(stack) -> list[str]:
    return [e["action"] for e in _events(stack)]


@pytest.fixture
def stack(tmp_path: Path):
    return _stack.build_phase1_stack(tmp_path)


# ------------------------------------------------------------------ the full loop


def test_full_loop_through_every_phase1_component(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    assert plan["status"] == "awaiting_approval"
    assert plan["task_state"]["status"] == "awaiting_approval"

    # Model Router (advisory, simulated) fills the AgentTask usage fields via model_class.
    at = plan["agent_task"]
    assert at["model_class"] == "reasoning" and at["model_used"] == f"mock:{REASONING['primary']}"
    assert at["token_usage"]["input_tokens"] > 0 and at["cost_usd"] > 0
    assert plan["model_route"]["simulated"] is True
    assert plan["model_route"]["advisory"]["epistemic_label"] == "inference"

    # Source-of-Truth: live order facts are fresh, Level 3, authoritative.
    facts = plan["truth_report"]["facts"]
    assert plan["truth_report"]["blocked"] is False
    assert facts["order:ORD-1042:fulfillment_status"]["level"] == 3 and facts["order:ORD-1042:fulfillment_status"]["authoritative"] is True
    assert facts["order:ORD-1042:hours_since_created"]["value"] == 68
    assert any("<= 300s" in c for c in at["constraints"])

    # Approval adapter: Tier 0 message queued locally, honestly not delivered.
    apr = plan["approval_request"]
    assert apr["status"] == "pending"
    dispatch = plan["approval_dispatch"]
    assert dispatch["status"] == "queued_local_stub" and dispatch["receipt"]["delivered"] is False
    assert apr["proposed_action_hash"] == dispatch["message"].inputs_hash
    assert apr["request_id"] in dispatch["message"].text and "TTL: <b>30 min</b>" in dispatch["message"].text

    # Founder presses Approve in the (simulated) Telegram chat; Master applies it.
    update = stack.adapter.simulate_button_press(dispatch["message"], "approve")
    decision = stack.master.handle_approval_update(update)
    assert decision["outcome"] == "approved"

    # Execute exactly the hash-bound payload; fresh re-read proves the SLA breach still holds.
    result = stack.master.execute_approved_request(apr["request_id"])
    assert result["status"] == "executed" and result["recheck"]["sla_breach"] is True
    er = result["execution_result"]
    assert er["status"] == "success" and er["verified"] is True and er["verification_method"] == "post_write_read_back"
    assert len(stack.fixture.get_order("ORD-1042")["internal_notes"]) == 1
    assert len(stack.memory.read_domain("operational")) == 1
    VALIDATOR.validate("11_memory_record.schema.json", result["memory_record"])

    # Approval is single use.
    replay = stack.gateway.invoke(
        task_id="TASK-20261125-INT1", agent_id="agent.operations", environment="dev",
        tool_name="shopify.orders.add_internal_note", inputs=apr["proposed_action"], approval_id=apr["request_id"],
    )
    assert replay["status"] == "blocked"


def test_every_audit_event_is_schema_valid_and_tells_the_whole_story(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    msg = plan["approval_dispatch"]["message"]
    stack.master.handle_approval_update(stack.adapter.simulate_button_press(msg, "approve"))
    stack.master.execute_approved_request(plan["approval_request"]["request_id"])

    events = _events(stack)
    for e in events:
        VALIDATOR.validate("17_audit_event.schema.json", e)
    actions = _actions(stack)
    for expected in ("model_route", "invoke:shopify.orders.get", "master:approval_dispatch", "master:plan_delegate",
                     "master:approval_decision", "master:execute_approved_l4", "invoke:shopify.orders.add_internal_note"):
        assert expected in actions, expected
    # Tool calls are made only under the Operations principal; the Master never calls tools itself.
    for e in events:
        if e["action"].startswith("invoke:"):
            assert e["agent"] == "agent.operations"
        if e["action"].startswith("master:"):
            assert e["agent"] == "master-control-plane" and e["autonomy_level"] == 2
    route_event = next(e for e in events if e["action"] == "model_route")
    assert route_event["model"] == f"mock:{REASONING['primary']}" and route_event["cost_usd"] > 0
    # The plan event links the route usage and the approval id.
    plan_event = next(e for e in events if e["action"] == "master:plan_delegate")
    assert plan_event["outputs_summary"]["approval_id"] == plan["approval_request"]["request_id"]


def test_order_within_sla_creates_no_approval_and_no_note(stack) -> None:
    plan = stack.master.plan_and_delegate(_task("ORD-1043"))
    assert plan["status"] == "completed_no_action" and plan["task_state"]["status"] == "completed"
    assert plan["approval_request"] is None and plan["approval_dispatch"] is None and stack.adapter.outbox == []
    assert plan["truth_report"]["blocked"] is False


def test_prompt_injection_in_customer_note_stays_inert(stack) -> None:
    plan = stack.master.plan_and_delegate(_task("ORD-1044"))
    assert plan["ops_outcome"]["injection_detected"] is True
    # Untrusted free text never becomes a Source-of-Truth claim and never triggers a refund call.
    assert all("customer_note" not in k for k in plan["truth_report"]["facts"])
    assert not any("refund" in a for a in _actions(stack))
    # ORD-1044 is 45h old (< 48h SLA): the hostile note must not create any action or approval.
    assert plan["status"] == "completed_no_action" and plan["approval_request"] is None
    assert stack.fixture.get_order("ORD-1044")["internal_notes"] == []


# ------------------------------------------------------------------ Source of Truth


def test_stale_live_data_blocks_the_approval_request(stack) -> None:
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    plan = stack.master.plan_and_delegate(_task(), now=later)
    assert plan["status"] == "blocked_truth_check" and plan["task_state"]["status"] == "blocked"
    assert plan["approval_request"] is None and plan["approval_dispatch"] is None
    assert plan["truth_report"]["blocked"] is True
    assert "order:ORD-1042:fulfillment_status" in plan["truth_report"]["blocking_facts"]
    assert any("Refresh Level 3" in a for a in plan["truth_report"]["required_actions"])
    assert stack.adapter.outbox == [] and stack.approvals._requests == {}
    events = _events(stack)
    assert any(e["action"] == "truth_resolve" and e["outcome"] == "blocked" for e in events)
    blocked_plan = next(e for e in events if e["action"] == "master:plan_delegate")
    assert blocked_plan["outcome"] == "blocked" and blocked_plan["outputs_summary"]["truth_blocked"] is True


def test_execution_stops_when_order_changed_after_approval(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    apr = plan["approval_request"]
    stack.master.handle_approval_update(stack.adapter.simulate_button_press(plan["approval_dispatch"]["message"], "approve"))
    stack.fixture._orders["ORD-1042"]["fulfillment_status"] = "fulfilled"  # shipped while the approval was pending
    result = stack.master.execute_approved_request(apr["request_id"])
    assert result["status"] == "precondition_changed" and result["execution_result"] is None
    assert stack.fixture.get_order("ORD-1042")["internal_notes"] == []
    assert stack.approvals.get_request(apr["request_id"])["consumed"] is False  # not burned
    assert any(e["action"] == "master:execute_approved_l4" and e["outcome"] == "blocked" for e in _events(stack))


def test_stale_evidence_at_execution_is_blocked_without_consuming_approval(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    apr = plan["approval_request"]
    stack.master.handle_approval_update(stack.adapter.simulate_button_press(plan["approval_dispatch"]["message"], "approve"))
    blocked = stack.master.execute_approved_request(apr["request_id"], now=datetime.now(timezone.utc) + timedelta(seconds=400))
    assert blocked["status"] == "blocked" and blocked["execution_result"]["error"]["code"] == "STALE_LIVE_DATA"
    assert stack.approvals.get_request(apr["request_id"])["consumed"] is False
    assert stack.master.execute_approved_request(apr["request_id"])["status"] == "executed"


def test_tampered_payload_is_still_blocked_after_freshness_checks(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    apr = plan["approval_request"]
    stack.master.handle_approval_update(stack.adapter.simulate_button_press(plan["approval_dispatch"]["message"], "approve"))
    res = stack.master.execute_approved_l4(
        task_id=apr["task_id"], approval_id=apr["request_id"], tool_name=apr["tool_name"],
        inputs={**apr["proposed_action"], "note": "tampered after approval"},
    )
    assert res["status"] == "blocked" and res["execution_result"]["error"]["code"] == "APPROVAL_HASH_MISMATCH"


# ------------------------------------------------------------------ Model Router


def test_503_on_primary_model_falls_back_and_is_recorded_on_the_agent_task(tmp_path: Path) -> None:
    provider = _mr.MockProvider().script(REASONING["primary"], _mr.ProviderCapacityError())
    s = _stack.build_phase1_stack(tmp_path, provider=provider)
    plan = s.master.plan_and_delegate(_task())
    assert plan["agent_task"]["model_used"] == f"mock:{REASONING['fallbacks'][0]}"
    assert plan["model_route"]["fallback_used"] is True and plan["status"] == "awaiting_approval"


def test_planning_degrades_gracefully_when_all_models_fail(tmp_path: Path) -> None:
    provider = _mr.MockProvider()
    for m in [REASONING["primary"], *REASONING["fallbacks"]]:
        provider.fail_always(m, _mr.ProviderCapacityError())
    s = _stack.build_phase1_stack(tmp_path, provider=provider)
    plan = s.master.plan_and_delegate(_task())
    assert plan["status"] == "awaiting_approval"  # deterministic planning does not need a model
    assert plan["agent_task"]["model_used"] == "deterministic-milestone1-local" and plan["agent_task"]["cost_usd"] == 0.0
    assert plan["routing_degraded"]["error"] == "ModelRoutingExhaustedError" and plan["model_route"] is None
    incident = json.loads(next((tmp_path / "incidents").glob("INC-*.json")).read_text(encoding="utf-8"))
    assert incident["related_incidents"] == ["INC-20261125-CAP1"] and incident["detected_by"] == "model_router"


def test_circuit_breaker_pauses_advisory_routing_but_not_the_control_plane(tmp_path: Path) -> None:
    ledger = _mr.SpendLedger()
    sink: list[dict] = []
    s = _stack.build_phase1_stack(tmp_path, ledger=ledger, alert_sink=sink.append)
    ledger.add(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 10.0)
    plan = s.master.plan_and_delegate(_task())
    assert plan["routing_degraded"]["error"] == "CircuitBreakerOpenError"
    assert plan["status"] == "awaiting_approval" and plan["agent_task"]["model_used"] == "deterministic-milestone1-local"
    assert [a["type"] for a in sink] == ["circuit_breaker_open"]


def test_soft_spend_alert_is_surfaced_to_the_founder_in_the_plan_result(tmp_path: Path) -> None:
    ledger = _mr.SpendLedger()
    s = _stack.build_phase1_stack(tmp_path, ledger=ledger)
    ledger.add(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 8.0)
    plan = s.master.plan_and_delegate(_task())
    assert [a["type"] for a in plan["founder_alerts"]] == ["spend_soft_alert"] and plan["founder_alerts"][0]["notify"] == "founder:abdulrahman"


# ------------------------------------------------------------------ Approval adapter


def test_undeliverable_approval_is_rejected_not_left_dangling(stack, monkeypatch) -> None:
    monkeypatch.setattr(_ad, "TELEGRAM_TEXT_LIMIT", 200)
    plan = stack.master.plan_and_delegate(_task())
    assert plan["status"] == "approval_not_deliverable" and plan["task_state"]["status"] == "failed"
    rec = stack.approvals.get_request(plan["approval_request"]["request_id"])
    assert rec["status"] == "rejected" and rec["rejection_reason"].startswith("NOT_DELIVERABLE")
    assert any(e["action"] == "master:approval_dispatch" and e["outcome"] == "failure" for e in _events(stack))


def test_unauthenticated_sender_cannot_approve(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    msg = plan["approval_dispatch"]["message"]
    with pytest.raises(_ad.ApprovalAuthError):
        stack.master.handle_approval_update(stack.adapter.simulate_button_press(msg, "approve", from_user_id="attacker-1"))
    assert stack.approvals.get_request(plan["approval_request"]["request_id"])["status"] == "pending"
    blocked = stack.master.gateway.invoke(
        task_id="TASK-20261125-INT1", agent_id="agent.operations", environment="dev", tool_name="shopify.orders.add_internal_note",
        inputs=plan["approval_request"]["proposed_action"], approval_id=plan["approval_request"]["request_id"],
    )
    assert blocked["error"]["code"] == "APPROVAL_REQUIRED"


def test_founder_rejection_and_change_request_never_execute(stack) -> None:
    plan = stack.master.plan_and_delegate(_task())
    msg = plan["approval_dispatch"]["message"]
    out = stack.master.handle_approval_update(stack.adapter.simulate_button_press(msg, "modify"))
    assert out["outcome"] == "modification_requested" and out["requires_new_request"] is True
    res = stack.master.execute_approved_request(plan["approval_request"]["request_id"])
    assert res["status"] == "blocked" and stack.fixture.get_order("ORD-1042")["internal_notes"] == []


def test_handle_update_requires_an_adapter(tmp_path: Path) -> None:
    s = _stack.build_phase1_stack(tmp_path)
    bare = _master.MasterAgent(gateway=s.gateway, operations_agent=s.ops, memory_store=s.memory)
    with pytest.raises(RuntimeError):
        bare.handle_approval_update({})


# ------------------------------------------------------------------ boundaries and baseline compatibility


def test_invalid_task_is_rejected_before_any_side_effect(stack) -> None:
    bad = {**_task(), "priority": "whenever"}
    with pytest.raises(_sv.SchemaValidationError):
        stack.master.plan_and_delegate(bad)
    assert _events(stack) == [] and stack.adapter.outbox == [] and stack.provider.calls == []


def test_l5_task_is_refused_and_audited_without_routing_or_tools(stack) -> None:
    with pytest.raises(PermissionError):
        stack.master.plan_and_delegate({**_task(), "autonomy_level": 5})
    events = _events(stack)
    assert [e["action"] for e in events] == ["master:plan_delegate"] and events[0]["outcome"] == "blocked"
    assert stack.provider.calls == []


def test_master_without_phase1_components_matches_milestone1_behaviour(tmp_path: Path) -> None:
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc")
    approvals = _approval.ApprovalStore()
    gateway = _gw.ToolGateway(approvals=approvals, audit_logger=audit)
    ops = _ops.OperationsAgent(gateway=gateway)
    master = _master.MasterAgent(gateway=gateway, operations_agent=ops, memory_store=_mem.MemoryStore(stores_dir=tmp_path / "m"))
    plan = master.plan_and_delegate(_task())
    assert plan["agent_task"]["model_used"] == "deterministic-milestone1-local"
    assert plan["model_route"] is None and plan["truth_report"] is None and plan["approval_dispatch"] is None
    approvals.approve(plan["approval_request"]["request_id"])
    res = master.execute_approved_l4(
        task_id="TASK-20261125-INT1", approval_id=plan["approval_request"]["request_id"],
        tool_name="shopify.orders.add_internal_note", inputs=plan["approval_request"]["proposed_action"],
    )
    assert res["execution_result"]["status"] == "success" and res["recheck"] is None
    assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 2  # read + write only


def test_runs_with_no_network_or_credentials(stack) -> None:
    assert stack.provider.simulated is True and stack.adapter.bot_status == "unavailable"
    plan = stack.master.plan_and_delegate(_task())
    assert plan["approval_dispatch"]["receipt"]["delivered"] is False
