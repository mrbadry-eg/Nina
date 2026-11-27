# Silicium AI Operating System (`silicium-ai-operating-system`)

**Status:** Phase 0 Architecture Freeze Approved (`SILICIUM-SPEC-001`)  
**Business:** Silicium Trade (`https://siliciumstore.com/`) — Egypt Technology Commerce  
**Founder / Tier 0 Authority:** Abdulrahman El-Badry  

## Core Architectural Axiom
> **Agents reason. n8n orchestrates. Tools act. Memory remembers. Policy controls. Founder approves consequential decisions.**

## Directory Structure (Phase 0 Freeze)
- `01_ARCHITECTURE/` — Canonical specification, 18 JSON Schema Draft 2020-12 contracts (`schemas/`), and schema/policy validators (`validators/`)
- `02_FOUNDER_CONTEXT/` — Founder context, approval TTLs, and escalation policy (`founder_context.json`)
- `03_BUSINESS_CONTEXT/` — Business profile, brand positioning, and policies (`business_context.json`)
- `04_SOURCE_OF_TRUTH/` — 8-level information authority hierarchy (`source_of_truth.json`)
- `05_AGENTS/` — Master Control Plane (`master/`) and bounded Tier 2 specialist agents (`operations/`, etc.)
- `06_CREATIVE_ENGINE/` — First-class Creative Content Engine subsystem specification & boundaries
- `07_MEMORY/` — Domain-isolated memory stores (`memory_store.py`)
- `08_TOOLS_MCP/` — Machine-enforced Tool Gateway (`tool_gateway.py`), tool registry, integration availability registry, and fixture adapters
- `09_N8N/` — Deterministic workflow definitions (post-Milestone 1)
- `10_OPENCLAW/` — OpenClaw messaging runtime configuration (post-Milestone 1)
- `11_ANTIGRAVITY/` — Engineering-plane rules and dev/prod isolation boundaries
- `12_MODEL_ROUTING/` — OpenAI-compatible OpenRouter provider, task-class routing, and persistent free-tier request budgeting (`routing_table.json`)
- `13_SECURITY/` — Security model, redaction, and SHA-256 payload-bound approval enforcement
- `14_EVALUATION/` — Evaluation framework and deterministic evaluation runner
- `15_OBSERVABILITY/` — Append-only JSONL audit sink (`audit/`) and incident logs (`incidents/`)
- `16_RUNBOOKS/` — Operational & rollback runbooks
- `17_TESTS/` — Automated unit, gateway security, and Milestone 1 end-to-end tests

## Milestone 1 (Current Target)
Credential-free, production-isolated deterministic control loop:
`Think -> Plan -> Delegate -> Tool L0 (shopify.orders.get) -> Verify -> Founder Approval (SHA-256 bound) -> Tool L4 (shopify.orders.add_internal_note) -> Verify -> Audit -> Learn`
