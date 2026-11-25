"""Tier 1 Silicium Master Agent / Control Plane (Milestone 1).

Executes the canonical Milestone 1 loop:
Think -> Plan -> Delegate -> Tool L0 -> Verify -> Founder Approval -> Tool L4 -> Verify -> Audit -> Learn.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from silicium_loader import load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")


class MasterAgent:
    AGENT_ID = "master-control-plane"

    def __init__(self, *, gateway: Any, operations_agent: Any, memory_store: Any) -> None:
        self.gateway = gateway
        self.operations_agent = operations_agent
        self.memory_store = memory_store
        self.validator = _sv.CanonicalSchemaValidator()

    def plan_and_delegate(self, task: dict[str, Any], environment: str = "dev") -> dict[str, Any]:
        """Validate incoming Task, decompose into AgentTask, delegate L0 inspection, and emit ApprovalRequest if L4 needed."""
        self.validator.validate("06_task.schema.json", task)

        # Master never lets an incoming task request L5 autonomously.
        if task["autonomy_level"] >= 5:
            raise PermissionError("Level 5 tasks are unconditionally prohibited in Master Control Plane")

        now = datetime.now(timezone.utc)
        subtask_id = f"TASK-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        agent_task: dict[str, Any] = {
            "task_id": subtask_id,
            "parent_task_id": task["task_id"],
            "objective": f"Inspect order state and propose operational note if SLA breached: {task['objective']}",
            "requester": self.AGENT_ID,
            "domain": "operations",
            "priority": task["priority"],
            "autonomy_level": 4,
            "inputs": task["inputs"],
            "required_tools": ["shopify.orders.get", "shopify.orders.add_internal_note"],
            "constraints": list(task["constraints"]) + ["Creative memory != product truth", "No autonomous financial actions"],
            "approval_required": True,
            "success_criteria": task["success_criteria"],
            "verification_required": True,
            "status": "active",
            "created_at": now.isoformat(),
            "completed_at": None,
            "assigned_agent": "agent.operations",
            "environment": environment,
            "model_class": "reasoning",
            "model_used": "deterministic-milestone1-local",
            "token_usage": {"input_tokens": 0, "output_tokens": 0},
            "cost_usd": 0.0,
            "delegation_chain": [task["requester"], self.AGENT_ID, "agent.operations"],
        }
        self.validator.validate("07_agent_task.schema.json", agent_task)

        ops_outcome = self.operations_agent.run(agent_task)
        approval_req = None
        if ops_outcome["proposed_l4_action"] is not None:
            prop = ops_outcome["proposed_l4_action"]
            approval_req = self.gateway.approvals.create_request(
                task_id=task["task_id"],
                requesting_agent="agent.operations",
                tool_name=prop["tool_name"],
                action_description=prop["action_description"],
                risk_level=prop["risk_level"],
                domain="operations",
                resource_affected=prop["resource_affected"],
                proposed_action=prop["inputs"],
                reversible=prop["reversible"],
                estimated_impact=prop["estimated_impact"],
                strategic=False,
                now=now,
            )

        return {
            "task_id": task["task_id"],
            "agent_task": agent_task,
            "ops_outcome": ops_outcome,
            "approval_request": approval_req,
        }

    def execute_approved_l4(
        self,
        *,
        task_id: str,
        approval_id: str,
        tool_name: str,
        inputs: dict[str, Any],
        environment: str = "dev",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Execute an L4 action through Tool Gateway, verify result, and record operational lesson."""
        exec_res = self.gateway.invoke(
            task_id=task_id,
            agent_id="agent.operations",
            environment=environment,
            tool_name=tool_name,
            inputs=inputs,
            approval_id=approval_id,
            now=now,
        )

        memory_record = None
        if exec_res["status"] == "success" and exec_res["verified"]:
            memory_record = self.memory_store.append(
                memory_domain="operational",
                content_type="lesson",
                content={
                    "task_id": task_id,
                    "order_id": inputs.get("order_id"),
                    "action": tool_name,
                    "approval_id": approval_id,
                    "outcome": "Verified L4 operational note applied after SLA check.",
                },
                source_system=self.AGENT_ID,
                source_authority_level=7,
                tags=["milestone1", "sla", "operations"],
                confidence=1.0,
                verified_by="tool_gateway:post_write_read_back",
            )

        return {
            "execution_result": exec_res,
            "memory_record": memory_record,
        }
