"""Phase 1 composition root: builds a fully wired, credential-free Silicium control plane.

Everything lives under ``base_dir`` (audit trail, incidents, memory), so building a stack never
touches repository state. All providers and channels are deterministic local stubs while the
corresponding integrations are ``unavailable`` in ``08_TOOLS_MCP/integration_status.json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from silicium_loader import load_module

_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_adapter = load_module("08_TOOLS_MCP/approval_adapter.py", "silicium_approval_adapter")
_fixture = load_module("08_TOOLS_MCP/fixtures/shopify_fixture.py", "silicium_shopify_fixture")
_gw = load_module("08_TOOLS_MCP/tool_gateway.py", "silicium_tool_gateway")
_mem = load_module("07_MEMORY/memory_store.py", "silicium_memory_store")
_ops = load_module("05_AGENTS/operations/operations_agent.py", "silicium_operations_agent")
_truth = load_module("04_SOURCE_OF_TRUTH/truth_resolver.py", "silicium_truth_resolver")
_router = load_module("12_MODEL_ROUTING/model_router.py", "silicium_model_router")
_master = load_module("05_AGENTS/master/master_agent.py", "silicium_master_agent")


@dataclass
class Phase1Stack:
    base_dir: Path
    fixture: Any
    approvals: Any
    audit: Any
    resolver: Any
    gateway: Any
    memory: Any
    provider: Any
    router: Any
    adapter: Any
    ops: Any
    master: Any


def build_phase1_stack(
    base_dir: Path,
    *,
    provider: Any | None = None,
    routing_table: dict[str, Any] | None = None,
    policy_index: dict[str, Any] | None = None,
    integration_status: dict[str, Any] | None = None,
    adapter: Any | None = None,
    ledger: Any | None = None,
    alert_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Phase1Stack:
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    fixture = _fixture.ShopifyFixtureAdapter()
    approvals = _approval.ApprovalStore()
    audit = _audit.AuditLogger(audit_path=base_dir / "audit.jsonl", incidents_dir=base_dir / "incidents")
    resolver = _truth.TruthResolver(audit_logger=audit, policy_index=policy_index)
    gateway = _gw.ToolGateway(fixture=fixture, approvals=approvals, audit_logger=audit, truth_resolver=resolver)
    memory = _mem.MemoryStore(stores_dir=base_dir / "memory")
    router = _router.ModelRouter(
        provider=provider, routing_table=routing_table, integration_status=integration_status,
        ledger=ledger, audit_logger=audit, alert_sink=alert_sink,
    )
    ops = _ops.OperationsAgent(gateway=gateway)
    channel = adapter or _adapter.TelegramApprovalAdapter(integration_status=integration_status)
    master = _master.MasterAgent(
        gateway=gateway, operations_agent=ops, memory_store=memory,
        truth_resolver=resolver, model_router=router, approval_adapter=channel, audit_logger=audit,
    )
    return Phase1Stack(
        base_dir=base_dir, fixture=fixture, approvals=approvals, audit=audit, resolver=resolver, gateway=gateway,
        memory=memory, provider=router.provider, router=router, adapter=channel, ops=ops, master=master,
    )
