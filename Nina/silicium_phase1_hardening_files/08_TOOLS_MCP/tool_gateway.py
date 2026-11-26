"""Machine-enforced Tool Gateway implementing the 8-step authorization algorithm."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_fixture = load_module("08_TOOLS_MCP/fixtures/shopify_fixture.py", "silicium_shopify_fixture")
_truth = load_module("04_SOURCE_OF_TRUTH/truth_resolver.py", "silicium_truth_resolver")


class ToolGateway:
    """Deterministic policy & authorization gateway between agents and tools."""

    def __init__(
        self,
        *,
        fixture: Any | None = None,
        approvals: Any | None = None,
        audit_logger: Any | None = None,
        tool_registry_path: Path | None = None,
        agent_registry_path: Path | None = None,
        truth_resolver: Any | None = None,
    ) -> None:
        self.validator = _sv.CanonicalSchemaValidator()
        # Optional Source-of-Truth layer: enforces freshness of evidence-backed actions (step 6b).
        self.truth_resolver = truth_resolver
        self.fixture = fixture or _fixture.ShopifyFixtureAdapter()
        self.approvals = approvals or _approval.ApprovalStore()
        self.audit = audit_logger or _audit.AuditLogger()

        t_path = tool_registry_path or (REPO_ROOT / "08_TOOLS_MCP" / "tool_registry.json")
        a_path = agent_registry_path or (REPO_ROOT / "08_TOOLS_MCP" / "agent_registry.json")

        raw_tools = json.loads(t_path.read_text(encoding="utf-8"))["tools"]
        self.tools: dict[str, dict[str, Any]] = {}
        for tool in raw_tools:
            self.validator.validate("08_tool_contract.schema.json", tool)
            self.tools[tool["name"]] = tool

        self.agents: dict[str, dict[str, Any]] = json.loads(a_path.read_text(encoding="utf-8"))["agents"]

    def _build_result(
        self,
        *,
        task_id: str,
        tool_name: str,
        agent: str,
        inputs_hash: str,
        outputs: dict[str, Any] | None,
        status: str,
        duration_ms: int,
        approval_id: str | None = None,
        verified: bool = False,
        verification_method: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        res = {
            "execution_id": f"EXE-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
            "task_id": task_id,
            "tool_name": tool_name,
            "agent": agent,
            "inputs_hash": inputs_hash,
            "outputs": outputs,
            "status": status,
            "error": error,
            "duration_ms": duration_ms,
            "model_used": None,
            "tokens": None,
            "cost_usd": None,
            "approval_id": approval_id,
            "verified": verified,
            "verification_method": verification_method,
            "executed_at": now.isoformat(),
        }
        self.validator.validate("10_execution_result.schema.json", res)
        return res

    def invoke(
        self,
        *,
        task_id: str,
        agent_id: str,
        environment: str,
        tool_name: str,
        inputs: dict[str, Any],
        approval_id: str | None = None,
        now: datetime | None = None,
        evidence: list[Any] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        inputs_hash = _sv.canonical_sha256(inputs)
        resource_label = str(inputs.get("order_id", "unknown"))

        def blocked(code: str, msg: str, domain: str = "internal", risk: str = "high", autonomy: int = 4) -> dict[str, Any]:
            dur = int((time.perf_counter() - started) * 1000)
            self.audit.log_audit(
                environment=environment,
                task_id=task_id,
                agent=agent_id,
                action=f"invoke:{tool_name}",
                tool=tool_name,
                domain=domain,
                resource=resource_label,
                inputs=inputs,
                outputs={"blocked_code": code},
                risk_level=risk,
                autonomy_level=autonomy,
                approval_id=approval_id,
                outcome="blocked",
                error=msg,
                duration_ms=dur,
            )
            return self._build_result(
                task_id=task_id,
                tool_name=tool_name,
                agent=agent_id,
                inputs_hash=inputs_hash,
                outputs=None,
                status="blocked",
                duration_ms=dur,
                approval_id=approval_id,
                verified=False,
                error={"code": code, "message": msg, "recoverable": code != "L5_PROHIBITED"},
            )

        # Step 2: Principal verification (WHO)
        if agent_id not in self.agents:
            return blocked("PERMISSION_DENIED", f"Unregistered agent principal: {agent_id}")
        principal = self.agents[agent_id]

        # Step 3: Tool Contract resolution (WHAT)
        if tool_name not in self.tools:
            return blocked("PERMISSION_DENIED", f"Unregistered tool: {tool_name}")
        tool = self.tools[tool_name]
        domain = tool["domain"]
        risk = tool["risk_level"]
        autonomy_req = tool["autonomy_required"]

        # Step 4: L5 Hard Block (unconditional across all environments)
        if autonomy_req >= 5:
            self.audit.log_incident(
                incident_type="security_event",
                severity="p2",
                description=f"Agent {agent_id} attempted L5 prohibited tool {tool_name}",
                affected_resources=[f"{domain}:{resource_label}"],
                detected_by="tool_gateway",
            )
            return blocked("L5_PROHIBITED", f"Tool {tool_name} is Level 5 PROHIBITED for autonomous AI execution", domain, risk, autonomy_req)

        # Step 5: Environment boundary + Permission & Autonomy checks
        if environment not in principal["allowed_environments"] or environment not in tool.get("allowed_environments", ["dev"]):
            return blocked("PERMISSION_DENIED", f"Environment '{environment}' not permitted for {agent_id}->{tool_name}", domain, risk, autonomy_req)

        if tool_name not in principal["allowed_tools"]:
            return blocked("PERMISSION_DENIED", f"Agent {agent_id} is not granted access to {tool_name}", domain, risk, autonomy_req)

        if autonomy_req > principal["max_autonomy_level"]:
            return blocked("PERMISSION_DENIED", f"Tool requires L{autonomy_req}, exceeds {agent_id} max L{principal['max_autonomy_level']}", domain, risk, autonomy_req)

        # Step 6: Validate inputs against ToolContract.inputs_schema
        try:
            self.validator.validate_with_inline_schema(tool["inputs_schema"], inputs, label=f"{tool_name}.inputs")
        except _sv.SchemaValidationError as exc:
            return blocked("VALIDATION_ERROR", str(exc), domain, risk, autonomy_req)

        # Step 6b: Source-of-Truth freshness precondition (only when evidence is supplied).
        # Runs BEFORE step 7 so a stale-data refusal never consumes a single-use approval.
        if evidence:
            if self.truth_resolver is None:
                return blocked("PRECONDITION_FAILED", "Evidence supplied but no TruthResolver is configured (fail closed)", domain, risk, autonomy_req)
            target = inputs.get("order_id")
            if target is not None:
                foreign = [c.fact_key for c in evidence if not c.fact_key.startswith(f"order:{target}:")]
                if foreign:
                    return blocked("VALIDATION_ERROR", f"Evidence does not concern target resource '{target}': {foreign}", domain, risk, autonomy_req)
            try:
                self.truth_resolver.assert_fresh(evidence, now=now)
            except _truth.StaleLiveDataError as exc:
                return blocked("STALE_LIVE_DATA", str(exc), domain, risk, autonomy_req)
            except _truth.TruthResolverError as exc:
                return blocked("VALIDATION_ERROR", str(exc), domain, risk, autonomy_req)

        # Step 7: Enforce SHA-256 payload-bound Approval Gate (L4 or approval_required)
        if autonomy_req == 4 or tool["approval_required"]:
            if not approval_id:
                return blocked("APPROVAL_REQUIRED", f"Tool {tool_name} requires Tier 0 approval_id", domain, risk, autonomy_req)
            try:
                self.approvals.verify_and_consume(
                    request_id=approval_id,
                    tool_name=tool_name,
                    payload_hash=inputs_hash,
                    now=now,
                )
            except _approval.ApprovalError as exc:
                code = "APPROVAL_HASH_MISMATCH" if "APPROVAL_HASH_MISMATCH" in str(exc) else "APPROVAL_REQUIRED"
                return blocked(code, str(exc), domain, risk, autonomy_req)

        # Step 8: Execute -> Verify -> Audit
        try:
            if tool_name == "shopify.orders.get":
                output = self.fixture.get_order(inputs["order_id"])
                verified = True
                verification_method = "fixture_direct_read"
            elif tool_name == "shopify.orders.add_internal_note":
                output = self.fixture.add_internal_note(
                    inputs["order_id"], inputs["note"], inputs["idempotency_key"]
                )
                read_back = self.fixture.get_order(inputs["order_id"])
                verified = inputs["note"] in read_back["internal_notes"]
                verification_method = "post_write_read_back"
                if not verified:
                    raise RuntimeError("Read-back verification failed after note write")
            else:
                raise NotImplementedError(f"No handler wired for {tool_name}")
        except _fixture.FixtureNotFoundError as exc:
            dur = int((time.perf_counter() - started) * 1000)
            self.audit.log_audit(
                environment=environment,
                task_id=task_id,
                agent=agent_id,
                action=f"invoke:{tool_name}",
                tool=tool_name,
                domain=domain,
                resource=resource_label,
                inputs=inputs,
                outputs={"error": str(exc)},
                risk_level=risk,
                autonomy_level=autonomy_req,
                approval_id=approval_id,
                outcome="failure",
                error=str(exc),
                duration_ms=dur,
            )
            return self._build_result(
                task_id=task_id,
                tool_name=tool_name,
                agent=agent_id,
                inputs_hash=inputs_hash,
                outputs=None,
                status="failure",
                duration_ms=dur,
                approval_id=approval_id,
                verified=False,
                error={"code": "NOT_FOUND", "message": str(exc), "recoverable": False},
            )

        dur = int((time.perf_counter() - started) * 1000)
        self.audit.log_audit(
            environment=environment,
            task_id=task_id,
            agent=agent_id,
            action=f"invoke:{tool_name}",
            tool=tool_name,
            domain=domain,
            resource=resource_label,
            inputs=inputs,
            outputs=output,
            risk_level=risk,
            autonomy_level=autonomy_req,
            approval_id=approval_id,
            outcome="success",
            duration_ms=dur,
        )
        return self._build_result(
            task_id=task_id,
            tool_name=tool_name,
            agent=agent_id,
            inputs_hash=inputs_hash,
            outputs=output,
            status="success",
            duration_ms=dur,
            approval_id=approval_id,
            verified=verified,
            verification_method=verification_method,
        )
