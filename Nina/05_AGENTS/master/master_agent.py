"""Tier 1 Silicium Master Agent / Control Plane.

Milestone 1 loop:
    Think -> Plan -> Delegate -> Tool L0 -> Verify -> Founder Approval -> Tool L4 -> Verify -> Audit -> Learn.

Phase 1 wiring (every component is optional; with none supplied the Milestone 1 behaviour is unchanged):

* ``truth_resolver``   Source-of-Truth check on the live order facts a proposal rests on. Stale or
                       non-authoritative live data blocks the approval request (spec 4.1, 5.1).
                       Before an approved L4 write the Operations Agent re-reads the order and the
                       Tool Gateway enforces freshness of that evidence (approval is not consumed on refusal).
* ``model_router``     Planning is routed by ``model_class`` ("reasoning"); the Master never names a provider
                       model. The routed call is ADVISORY (labelled ``inference``, simulated while the gateway
                       is unavailable) and fills the AgentTask model/usage fields. Decisions stay deterministic.
                       If routing is unavailable (breaker open, chain exhausted) planning degrades gracefully.
* ``approval_adapter`` Renders the ``APR-*`` request for the Founder, dispatches it, and applies the Founder's
                       decision back to the ApprovalStore.
* ``audit_logger``     One orchestration audit event per Master step, schema-validated (17).

Schemas 06, 07, 09, 10, 11 and 17 are validated at every boundary the Master touches.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from silicium_loader import load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_truth = load_module("04_SOURCE_OF_TRUTH/truth_resolver.py", "silicium_truth_resolver")
_router = load_module("12_MODEL_ROUTING/model_router.py", "silicium_model_router")
_adapter = load_module("08_TOOLS_MCP/approval_adapter.py", "silicium_approval_adapter")

ORDER_BACKED_TOOLS = frozenset({"shopify.orders.add_internal_note"})


class MasterAgent:
    AGENT_ID = "master-control-plane"
    AUTONOMY_LEVEL = 2  # max_autonomy_level in agent_registry.json

    def __init__(
        self,
        *,
        gateway: Any,
        operations_agent: Any,
        memory_store: Any,
        truth_resolver: Any | None = None,
        model_router: Any | None = None,
        approval_adapter: Any | None = None,
        audit_logger: Any | None = None,
    ) -> None:
        self.gateway = gateway
        self.operations_agent = operations_agent
        self.memory_store = memory_store
        self.truth_resolver = truth_resolver
        self.model_router = model_router
        self.approval_adapter = approval_adapter
        self.audit = audit_logger
        self.validator = _sv.CanonicalSchemaValidator()

    # ------------------------------------------------------------------ plan

    def plan_and_delegate(self, task: dict[str, Any], environment: str = "dev", *, now: datetime | None = None) -> dict[str, Any]:
        """Validate incoming Task, decompose into AgentTask, delegate L0 inspection, and emit ApprovalRequest if L4 needed."""
        started = time.perf_counter()
        self.validator.validate("06_task.schema.json", task)

        # Master never lets an incoming task request L5 autonomously.
        if task["autonomy_level"] >= 5:
            self._audit(
                "master:plan_delegate", "blocked", task["task_id"], environment, started, risk="high",
                inputs={"autonomy_level": task["autonomy_level"]}, outputs={"reason": "L5_PROHIBITED"},
                error="Level 5 tasks are unconditionally prohibited in Master Control Plane",
            )
            raise PermissionError("Level 5 tasks are unconditionally prohibited in Master Control Plane")

        plan_now = now or datetime.now(timezone.utc)
        subtask_id = f"TASK-{plan_now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

        route, routing_degraded = self._advisory_route(task, plan_now)

        constraints = list(task["constraints"]) + ["Creative memory != product truth", "No autonomous financial actions"]
        if self.truth_resolver is not None:
            constraints.append(f"Live commerce data must be <= {self.truth_resolver.threshold(3)}s old (Source-of-Truth Level 3)")

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
            "constraints": constraints,
            "approval_required": True,
            "success_criteria": task["success_criteria"],
            "verification_required": True,
            "status": "active",
            "created_at": plan_now.isoformat(),
            "completed_at": None,
            "assigned_agent": "agent.operations",
            "environment": environment,
            "model_class": "reasoning",
            "model_used": route["model_used"] if route else "deterministic-milestone1-local",
            "token_usage": dict(route["token_usage"]) if route else {"input_tokens": 0, "output_tokens": 0},
            "cost_usd": route["cost_usd"] if route else 0.0,
            "delegation_chain": [task["requester"], self.AGENT_ID, "agent.operations"],
        }
        self.validator.validate("07_agent_task.schema.json", agent_task)

        ops_outcome = self.operations_agent.run(agent_task)
        self.validator.validate("10_execution_result.schema.json", ops_outcome["read_result"])

        truth_report = None
        if self.truth_resolver is not None and ops_outcome["status"] != "failed":
            truth_report = self._check_truth(task, ops_outcome, now)
            if truth_report["blocked"]:
                return self._finish_plan(
                    task, agent_task, ops_outcome, None, None, route, routing_degraded, truth_report,
                    status="blocked_truth_check", task_status="blocked", environment=environment, started=started,
                )

        approval_req = None
        dispatch = None
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
                now=plan_now,
            )
            self.validator.validate("09_approval_request.schema.json", approval_req)
            if self.approval_adapter is not None:
                dispatch = self._dispatch_approval(approval_req, plan_now, environment)
                if dispatch["status"] == "not_deliverable":
                    return self._finish_plan(
                        task, agent_task, ops_outcome, approval_req, dispatch, route, routing_degraded, truth_report,
                        status="approval_not_deliverable", task_status="failed", environment=environment, started=started,
                    )

        if ops_outcome["status"] == "failed":
            status, task_status = "failed", "failed"
        elif approval_req is not None:
            status, task_status = "awaiting_approval", "awaiting_approval"
        else:
            status, task_status = "completed_no_action", "completed"
        return self._finish_plan(
            task, agent_task, ops_outcome, approval_req, dispatch, route, routing_degraded, truth_report,
            status=status, task_status=task_status, environment=environment, started=started,
        )

    # ------------------------------------------------------------------ approval (inbound)

    def handle_approval_update(self, raw_update: dict[str, Any], *, now: datetime | None = None, environment: str = "dev") -> dict[str, Any]:
        """Parse a channel callback from the Founder and apply it to the ApprovalStore."""
        if self.approval_adapter is None:
            raise RuntimeError("No approval adapter configured")
        started = time.perf_counter()
        decision = self.approval_adapter.parse_decision(raw_update, now=now)  # raises on unauthenticated sender
        outcome = self.approval_adapter.apply_decision(decision, self.gateway.approvals, now=now)
        record = self.gateway.approvals.get_request(decision.request_id) if outcome["outcome"] != "unknown_request" else None
        self._audit(
            "master:approval_decision", "success" if outcome["outcome"] in ("approved", "rejected", "modification_requested") else "blocked",
            record["task_id"] if record else None, environment, started, risk="medium", resource=decision.request_id,
            inputs={"channel": decision.channel, "action": decision.action, "actor": decision.actor},
            outputs={"outcome": outcome["outcome"], "status": outcome["status"]},
            approval_id=decision.request_id,
        )
        return outcome

    def execute_approved_request(self, request_id: str, *, environment: str = "dev", now: datetime | None = None) -> dict[str, Any]:
        """Execute an approved request exactly as stored (payload is the hash-bound ``proposed_action``)."""
        record = self.gateway.approvals.get_request(request_id)
        return self.execute_approved_l4(
            task_id=record["task_id"], approval_id=request_id, tool_name=record["tool_name"],
            inputs=record["proposed_action"], environment=environment, now=now,
        )

    # ------------------------------------------------------------------ execute

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
        started = time.perf_counter()
        evidence = None
        recheck = None

        if self.truth_resolver is not None and tool_name in ORDER_BACKED_TOOLS and "order_id" in inputs:
            # The approval may be minutes old. Re-read live state and prove it is still current.
            recheck = self.operations_agent.recheck_sla(order_id=inputs["order_id"], task_id=task_id, environment=environment)
            if recheck["status"] != "ok":
                return self._execute_stopped("precondition_failed", task_id, approval_id, tool_name, inputs, recheck, environment, started,
                                             error="Could not re-read the order before executing")
            if not recheck["sla_breach"]:
                return self._execute_stopped("precondition_changed", task_id, approval_id, tool_name, inputs, recheck, environment, started,
                                             error=f"Order is no longer in SLA breach (fulfillment={recheck['fulfillment_status']}, age={recheck['hours']}h); approval left unconsumed")
            evidence = self.truth_resolver.claims_from_order_result(recheck["read_result"])

        exec_res = self.gateway.invoke(
            task_id=task_id,
            agent_id="agent.operations",
            environment=environment,
            tool_name=tool_name,
            inputs=inputs,
            approval_id=approval_id,
            now=now,
            evidence=evidence,
        )
        self.validator.validate("10_execution_result.schema.json", exec_res)

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
            self.validator.validate("11_memory_record.schema.json", memory_record)

        status = "executed" if memory_record is not None else exec_res["status"]
        self._audit(
            "master:execute_approved_l4", "success" if status == "executed" else ("blocked" if exec_res["status"] == "blocked" else "failure"),
            task_id, environment, started, risk="medium", resource=str(inputs.get("order_id", "unknown")), approval_id=approval_id,
            inputs={"tool_name": tool_name, "freshness_checked": evidence is not None},
            outputs={"status": status, "error_code": (exec_res.get("error") or {}).get("code"), "memory_recorded": memory_record is not None},
        )
        return {
            "status": status,
            "execution_result": exec_res,
            "memory_record": memory_record,
            "recheck": recheck,
        }

    # ------------------------------------------------------------------ internals

    def _advisory_route(self, task: dict[str, Any], now: datetime) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Route the planning call by model_class. Advisory only: never a source of fact."""
        if self.model_router is None:
            return None, None
        messages = [
            {"role": "system", "content": "Silicium Master planner. Internal English only. Output is advisory inference, never a source of fact."},
            {"role": "user", "content": _sv.canonical_json({"objective": task["objective"], "inputs": task["inputs"], "constraints": task["constraints"]})},
        ]
        try:
            route = self.model_router.complete(
                "reasoning", messages, task_id=task["task_id"], agent=self.AGENT_ID, data_class="internal", now=now
            )
        except (_router.CircuitBreakerOpenError, _router.ModelRoutingExhaustedError, _router.DataPolicyViolationError) as exc:
            return None, {"error": type(exc).__name__, "reason": str(exc), "fallback": "deterministic planning without model advisory"}
        return route, None

    def _check_truth(self, task: dict[str, Any], ops_outcome: dict[str, Any], now: datetime | None) -> dict[str, Any]:
        """Resolve the live order facts the proposal rests on. Stale or non-authoritative facts block."""
        claims = self.truth_resolver.claims_from_order_result(ops_outcome["read_result"])
        checked_at = now or datetime.now(timezone.utc)
        results = self.truth_resolver.resolve_many(
            claims, now=checked_at, task_id=task["task_id"], agent=self.AGENT_ID, require_authoritative=True,
        )
        facts = {
            key: {
                "status": r["status"], "authoritative": r["authoritative"], "value": r["value"], "epistemic_label": r["epistemic_label"],
                "level": (r["winning_claim"] or {}).get("level"), "source": (r["winning_claim"] or {}).get("source"),
                "age_seconds": (r["freshness"] or {}).get("age_seconds"),
            }
            for key, r in results.items()
        }
        blocking = sorted(k for k, r in results.items() if not r["authoritative"])
        return {
            "checked_at": checked_at.isoformat(),
            "facts": facts,
            "discrepancies": [d for r in results.values() for d in r["discrepancies"]],
            "blocked": bool(blocking),
            "blocking_facts": blocking,
            "required_actions": [results[k]["blocked"]["required_action"] for k in blocking if results[k]["blocked"]],
        }

    def _dispatch_approval(self, approval_req: dict[str, Any], now: datetime, environment: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            message = self.approval_adapter.format_request(approval_req, now=now)
            receipt = self.approval_adapter.send(message)
        except _adapter.ApprovalAdapterError as exc:
            # Never leave a request pending that the Founder could not see in full.
            self.gateway.approvals.reject(approval_req["request_id"], f"NOT_DELIVERABLE: {exc}")
            self._audit(
                "master:approval_dispatch", "failure", approval_req["task_id"], environment, started, risk="medium",
                resource=approval_req["request_id"], approval_id=approval_req["request_id"],
                inputs={"channel": self.approval_adapter.channel}, outputs={"reason": "not_deliverable"}, error=str(exc),
            )
            return {"status": "not_deliverable", "error": str(exc), "message": None, "receipt": None}
        self._audit(
            "master:approval_dispatch", "success", approval_req["task_id"], environment, started, risk="medium",
            resource=approval_req["request_id"], approval_id=approval_req["request_id"],
            inputs={"channel": message.channel, "ttl": message.ttl_label},
            outputs={"receipt_status": receipt["status"], "delivered": receipt["delivered"], "inputs_hash": message.inputs_hash},
        )
        return {"status": receipt["status"], "error": None, "message": message, "receipt": receipt}

    def _finish_plan(
        self, task: dict[str, Any], agent_task: dict[str, Any], ops_outcome: dict[str, Any], approval_req: dict[str, Any] | None,
        dispatch: dict[str, Any] | None, route: dict[str, Any] | None, routing_degraded: dict[str, Any] | None,
        truth_report: dict[str, Any] | None, *, status: str, task_status: str, environment: str, started: float,
    ) -> dict[str, Any]:
        done = task_status in ("completed", "failed", "blocked")
        task_state = {**task, "status": task_status, "completed_at": datetime.now(timezone.utc).isoformat() if done else None}
        self.validator.validate("06_task.schema.json", task_state)

        model_route = None
        if route is not None:
            model_route = {k: route[k] for k in ("model_class_requested", "model_class_used", "model_used", "provider", "simulated",
                                                 "fallback_used", "degraded_mode", "attempts", "token_usage", "cost_usd", "spend")}
            model_route["advisory"] = {"epistemic_label": "inference", "simulated": route["simulated"], "content": route["content"]}

        result = {
            "task_id": task["task_id"],
            "status": status,
            "task_state": task_state,
            "agent_task": agent_task,
            "ops_outcome": ops_outcome,
            "approval_request": approval_req,
            "approval_dispatch": dispatch,
            "truth_report": truth_report,
            "model_route": model_route,
            "routing_degraded": routing_degraded,
            "founder_alerts": list(route["alerts"]) if route else [],
        }
        self._audit(
            "master:plan_delegate", "blocked" if status in ("blocked_truth_check", "approval_not_deliverable") else "success",
            task["task_id"], environment, started, risk="low", resource=task["task_id"],
            inputs={"objective": task["objective"], "autonomy_level": task["autonomy_level"]},
            outputs={
                "status": status, "subtask_id": agent_task["task_id"],
                "approval_id": approval_req["request_id"] if approval_req else None,
                "truth_blocked": bool(truth_report and truth_report["blocked"]),
                "blocking_facts": truth_report["blocking_facts"] if truth_report else [],
                "routing_degraded": routing_degraded["error"] if routing_degraded else None,
            },
            model=route["model_used"] if route else None,
            tokens={"input": route["token_usage"]["input_tokens"], "output": route["token_usage"]["output_tokens"]} if route else None,
            cost=route["cost_usd"] if route else None,
        )
        return result

    def _execute_stopped(
        self, status: str, task_id: str, approval_id: str, tool_name: str, inputs: dict[str, Any], recheck: dict[str, Any],
        environment: str, started: float, *, error: str,
    ) -> dict[str, Any]:
        self._audit(
            "master:execute_approved_l4", "blocked", task_id, environment, started, risk="medium",
            resource=str(inputs.get("order_id", "unknown")), approval_id=approval_id,
            inputs={"tool_name": tool_name}, outputs={"status": status}, error=error,
        )
        return {"status": status, "execution_result": None, "memory_record": None, "recheck": recheck, "error": error}

    def _audit(
        self, action: str, outcome: str, task_id: str | None, environment: str, started: float, *, risk: str,
        inputs: dict[str, Any], outputs: dict[str, Any], resource: str | None = None, approval_id: str | None = None,
        error: str | None = None, model: str | None = None, tokens: dict[str, int] | None = None, cost: float | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.log_audit(
            environment=environment,
            task_id=task_id,
            agent=self.AGENT_ID,
            action=action,
            tool="master.orchestrator",
            domain="orchestration",
            resource=resource or (task_id or "unknown"),
            inputs=inputs,
            outputs=outputs,
            risk_level=risk,
            autonomy_level=self.AUTONOMY_LEVEL,
            approval_id=approval_id,
            outcome=outcome,
            error=error,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model=model,
            tokens=tokens,
            cost_usd=cost,
        )
