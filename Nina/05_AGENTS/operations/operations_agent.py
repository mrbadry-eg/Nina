"""Bounded Tier 2 Operations Agent (Milestone 1).

Reads live/fixture order state at L0 via Tool Gateway, detects deterministic
operational exceptions, separates epistemic categories, and proposes an L4
internal note action — never mutating state without Tier 0 approval.
"""

from __future__ import annotations

from typing import Any

from silicium_loader import load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")


class OperationsAgent:
    AGENT_ID = "agent.operations"
    SLA_UNFULFILLED_HOURS = 48

    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway
        self.validator = _sv.CanonicalSchemaValidator()

    def run(self, agent_task: dict[str, Any]) -> dict[str, Any]:
        self.validator.validate("07_agent_task.schema.json", agent_task)
        if agent_task["assigned_agent"] != self.AGENT_ID:
            raise ValueError(f"Task assigned to {agent_task['assigned_agent']}, expected {self.AGENT_ID}")

        order_refs = [inp["ref_id"] for inp in agent_task["inputs"] if inp["ref_type"] == "shopify_order"]
        if not order_refs:
            raise ValueError("OperationsAgent requires at least one shopify_order input ref")

        order_id = order_refs[0]
        env = agent_task["environment"]
        task_id = agent_task["task_id"]

        # Step 1: L0 Read via Tool Gateway
        read_res = self.gateway.invoke(
            task_id=task_id,
            agent_id=self.AGENT_ID,
            environment=env,
            tool_name="shopify.orders.get",
            inputs={"order_id": order_id},
        )
        if read_res["status"] != "success" or not read_res["outputs"]:
            return {
                "agent": self.AGENT_ID,
                "task_id": task_id,
                "status": "failed",
                "read_result": read_res,
                "epistemic_report": {
                    "retrieved_live_data": [],
                    "calculated_result": [],
                    "inference": [],
                    "recommendation": [],
                    "required_human_decision": [f"Order {order_id} could not be retrieved from fixture."],
                },
                "proposed_l4_action": None,
            }

        order = read_res["outputs"]
        hours = int(order.get("hours_since_created", 0))
        sla_breach = order["fulfillment_status"] == "unfulfilled" and hours >= self.SLA_UNFULFILLED_HOURS

        # Untrusted customer_note is treated strictly as inert data (never instructions).
        customer_note = str(order.get("customer_note", ""))
        injection_detected = any(
            marker in customer_note.lower()
            for marker in ("ignore previous", "system override", "shopify.orders.refund", "refund immediately")
        )

        epistemic_report = {
            "retrieved_live_data": [
                f"Order {order_id}: status={order['status']}, fulfillment={order['fulfillment_status']}, "
                f"payment={order['payment_method']}, total_egp={order['total_egp']}, city={order['city']}"
            ],
            "calculated_result": [
                f"Order age={hours}h vs SLA={self.SLA_UNFULFILLED_HOURS}h -> sla_breach={sla_breach}"
            ],
            "inference": [
                "Untrusted customer_note contains prompt-injection pattern; ignored safely."
                if injection_detected
                else "No prompt-injection markers detected in customer_note."
            ],
            "recommendation": [
                f"Append internal ops SLA alert note to {order_id} and prioritize Cairo/Egypt courier check."
                if sla_breach
                else f"Order {order_id} is within normal fulfillment parameters; no note mutation needed."
            ],
            "required_human_decision": [
                f"Approve L4 internal note write on {order_id} via Telegram"
                if sla_breach
                else "None"
            ],
        }

        proposed_l4 = None
        if sla_breach:
            proposed_l4 = {
                "tool_name": "shopify.orders.add_internal_note",
                "resource_affected": f"shopify:order:{order_id}",
                "action_description": f"Add SLA follow-up internal note to unfulfilled {order['payment_method'].upper()} order {order_id} ({hours}h old).",
                "risk_level": "medium",
                "reversible": True,
                "estimated_impact": "Internal operational note only; no customer message or financial mutation.",
                "inputs": {
                    "order_id": order_id,
                    "note": f"[OPS-SLA] Unfulfilled {order['payment_method'].upper()} order aged {hours}h in {order['city']}; courier dispatch verification queued.",
                    "idempotency_key": f"ops-sla-{task_id}-{order_id}",
                },
            }

        return {
            "agent": self.AGENT_ID,
            "task_id": task_id,
            "status": "proposal_ready" if proposed_l4 else "completed_no_action",
            "read_result": read_res,
            "injection_detected": injection_detected,
            "epistemic_report": epistemic_report,
            "proposed_l4_action": proposed_l4,
        }

    def recheck_sla(self, *, order_id: str, task_id: str, environment: str = "dev") -> dict[str, Any]:
        """Fresh L0 read used right before an approved L4 write.

        An approval can be granted minutes before execution; the order may have been fulfilled in
        between. The caller decides whether to proceed from ``sla_breach`` and uses the returned
        ``read_result`` as time-stamped evidence for the Tool Gateway freshness precondition.
        """
        read_res = self.gateway.invoke(
            task_id=task_id,
            agent_id=self.AGENT_ID,
            environment=environment,
            tool_name="shopify.orders.get",
            inputs={"order_id": order_id},
        )
        if read_res["status"] != "success" or not read_res["outputs"]:
            return {"status": "failed", "read_result": read_res, "sla_breach": None, "hours": None}
        order = read_res["outputs"]
        hours = int(order.get("hours_since_created", 0))
        breach = order["fulfillment_status"] == "unfulfilled" and hours >= self.SLA_UNFULFILLED_HOURS
        return {
            "status": "ok",
            "read_result": read_res,
            "sla_breach": breach,
            "hours": hours,
            "fulfillment_status": order["fulfillment_status"],
        }
