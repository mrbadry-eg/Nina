"""Automated pytest suite for Phase 0 schemas, Tool Gateway security, and Milestone 1 loop."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_fixture = load_module("08_TOOLS_MCP/fixtures/shopify_fixture.py", "silicium_shopify_fixture")
_gw = load_module("08_TOOLS_MCP/tool_gateway.py", "silicium_tool_gateway")
_mem = load_module("07_MEMORY/memory_store.py", "silicium_memory_store")
_ops = load_module("05_AGENTS/operations/operations_agent.py", "silicium_operations_agent")
_master = load_module("05_AGENTS/master/master_agent.py", "silicium_master_agent")


def _build_env(tmp_path: Path):
    fixture = _fixture.ShopifyFixtureAdapter()
    approvals = _approval.ApprovalStore()
    audit = _audit.AuditLogger(
        audit_path=tmp_path / "audit.jsonl",
        incidents_dir=tmp_path / "incidents",
    )
    gateway = _gw.ToolGateway(fixture=fixture, approvals=approvals, audit_logger=audit)
    memory = _mem.MemoryStore(stores_dir=tmp_path / "memory")
    ops = _ops.OperationsAgent(gateway=gateway)
    master = _master.MasterAgent(gateway=gateway, operations_agent=ops, memory_store=memory)
    return fixture, approvals, audit, gateway, memory, ops, master


def _sample_task(order_id: str = "ORD-1042") -> dict:
    return {
        "task_id": "TASK-20261125-TST1",
        "objective": f"Inspect {order_id} fulfillment status",
        "requester": "founder:abdulrahman",
        "domain": "operations",
        "priority": "normal",
        "autonomy_level": 4,
        "inputs": [{"ref_type": "shopify_order", "ref_id": order_id}],
        "required_tools": ["shopify.orders.get", "shopify.orders.add_internal_note"],
        "constraints": ["Verify before write"],
        "approval_required": True,
        "success_criteria": ["Verified L0 read", "Approved L4 note"],
        "verification_required": True,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
    }


def test_all_18_canonical_schemas_valid_draft_2020_12() -> None:
    validator = _sv.CanonicalSchemaValidator()
    checked = validator.check_all_schemas()
    assert len(checked) == 18
    assert checked[0] == "01_founder_context.schema.json"
    assert checked[-1] == "18_evaluation_case.schema.json"


def test_phase0_configs_conform_to_canonical_schemas() -> None:
    validator = _sv.CanonicalSchemaValidator()
    validator.validate(
        "01_founder_context.schema.json",
        json.loads((REPO_ROOT / "02_FOUNDER_CONTEXT" / "founder_context.json").read_text(encoding="utf-8")),
    )
    validator.validate(
        "02_business_context.schema.json",
        json.loads((REPO_ROOT / "03_BUSINESS_CONTEXT" / "business_context.json").read_text(encoding="utf-8")),
    )
    validator.validate(
        "03_source_of_truth.schema.json",
        json.loads((REPO_ROOT / "04_SOURCE_OF_TRUTH" / "source_of_truth.json").read_text(encoding="utf-8")),
    )
    validator.validate(
        "16_incident.schema.json",
        json.loads((REPO_ROOT / "15_OBSERVABILITY" / "incidents" / "phase0_capacity_incident.json").read_text(encoding="utf-8")),
    )


def test_milestone1_full_loop_happy_path(tmp_path: Path) -> None:
    fixture, approvals, _, gateway, memory, _, master = _build_env(tmp_path)
    plan = master.plan_and_delegate(_sample_task("ORD-1042"))

    assert plan["ops_outcome"]["status"] == "proposal_ready"
    apr = plan["approval_request"]
    assert apr is not None
    assert apr["status"] == "pending"

    approvals.approve(apr["request_id"], approver="founder:abdulrahman")
    result = master.execute_approved_l4(
        task_id=plan["task_id"],
        approval_id=apr["request_id"],
        tool_name=apr["tool_name"],
        inputs=apr["proposed_action"],
    )

    assert result["execution_result"]["status"] == "success"
    assert result["execution_result"]["verified"] is True
    assert result["execution_result"]["verification_method"] == "post_write_read_back"
    assert len(fixture.get_order("ORD-1042")["internal_notes"]) == 1
    assert len(memory.read_domain("operational")) == 1

    audit_lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit_lines) == 2


def test_l4_payload_hash_tampering_is_blocked(tmp_path: Path) -> None:
    _, approvals, _, gateway, _, _, master = _build_env(tmp_path)
    plan = master.plan_and_delegate(_sample_task("ORD-1042"))
    apr = plan["approval_request"]
    approvals.approve(apr["request_id"], approver="founder:abdulrahman")

    tampered = dict(apr["proposed_action"])
    tampered["note"] = "Sneaky altered payload"

    res = gateway.invoke(
        task_id=plan["task_id"],
        agent_id="agent.operations",
        environment="dev",
        tool_name=apr["tool_name"],
        inputs=tampered,
        approval_id=apr["request_id"],
    )
    assert res["status"] == "blocked"
    assert res["error"]["code"] == "APPROVAL_HASH_MISMATCH"


def test_l4_approval_cannot_be_replayed_or_expired(tmp_path: Path) -> None:
    _, approvals, _, gateway, _, _, master = _build_env(tmp_path)
    plan = master.plan_and_delegate(_sample_task("ORD-1042"))
    apr = plan["approval_request"]
    approvals.approve(apr["request_id"], approver="founder:abdulrahman")

    # First execution succeeds
    first = master.execute_approved_l4(
        task_id=plan["task_id"],
        approval_id=apr["request_id"],
        tool_name=apr["tool_name"],
        inputs=apr["proposed_action"],
    )
    assert first["execution_result"]["status"] == "success"

    # Replay with same approval_id is blocked
    replay = gateway.invoke(
        task_id=plan["task_id"],
        agent_id="agent.operations",
        environment="dev",
        tool_name=apr["tool_name"],
        inputs=apr["proposed_action"],
        approval_id=apr["request_id"],
    )
    assert replay["status"] == "blocked"

    # Expired approval (>30 min routine TTL) is blocked
    plan2 = master.plan_and_delegate(_sample_task("ORD-1042"))
    apr2 = plan2["approval_request"]
    approvals.approve(apr2["request_id"], approver="founder:abdulrahman")
    expired_now = datetime.now(timezone.utc) + timedelta(minutes=31)
    expired_res = gateway.invoke(
        task_id=plan2["task_id"],
        agent_id="agent.operations",
        environment="dev",
        tool_name=apr2["tool_name"],
        inputs=apr2["proposed_action"],
        approval_id=apr2["request_id"],
        now=expired_now,
    )
    assert expired_res["status"] == "blocked"


def test_l5_prohibited_and_prompt_injection_and_prod_isolation(tmp_path: Path) -> None:
    _, _, _, gateway, memory, _, master = _build_env(tmp_path)

    # Prompt injection inside ORD-1044 does not trigger refund
    plan = master.plan_and_delegate(_sample_task("ORD-1044"))
    assert plan["ops_outcome"]["injection_detected"] is True

    # Direct L5 call is unconditionally denied + incident emitted
    l5 = gateway.invoke(
        task_id="TASK-20261125-TST5",
        agent_id="agent.operations",
        environment="dev",
        tool_name="shopify.orders.refund",
        inputs={"order_id": "ORD-1044", "amount_egp": 2400, "reason": "Test"},
    )
    assert l5["status"] == "blocked"
    assert l5["error"]["code"] == "L5_PROHIBITED"
    assert len(list((tmp_path / "incidents").glob("INC-*.json"))) == 1

    # Production environment call from dev-scoped agent is blocked
    prod_block = gateway.invoke(
        task_id="TASK-20261125-TSTP",
        agent_id="agent.operations",
        environment="prod",
        tool_name="shopify.orders.get",
        inputs={"order_id": "ORD-1042"},
    )
    assert prod_block["status"] == "blocked"
    assert prod_block["error"]["code"] == "PERMISSION_DENIED"

    # Creative memory cannot accept Level 1-4 authoritative product truth
    with pytest.raises(_mem.MemoryBoundaryError):
        memory.append(
            memory_domain="creative",
            content_type="fact",
            content={"price_egp": 1200},
            source_system="subsystem.creative",
            source_authority_level=3,
            tags=["trap"],
        )


def test_audit_redacts_secrets_and_pii(tmp_path: Path) -> None:
    audit = _audit.AuditLogger(
        audit_path=tmp_path / "audit.jsonl",
        incidents_dir=tmp_path / "incidents",
    )
    ev = audit.log_audit(
        environment="dev",
        agent="agent.operations",
        action="test_redact",
        tool="shopify.orders.get",
        domain="shopify",
        resource="ORD-1042",
        inputs={"order_id": "ORD-1042", "api_key": "shpat_secret_123", "phone": "+201000000000"},
        outputs={"token": "tok_xyz", "status": "open"},
        risk_level="safe",
        autonomy_level=0,
        outcome="success",
        duration_ms=2,
    )
    assert ev["inputs_summary"]["api_key"] == "[REDACTED]"
    assert ev["inputs_summary"]["phone"] == "[REDACTED]"
    assert ev["outputs_summary"]["token"] == "[REDACTED]"
