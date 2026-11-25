# Phase 0 Decision Record (Freeze Approved)

**Date:** 2026-11-25  
**Approved By:** Abdulrahman El-Badry (Tier 0 Founder)  
**Canonical Specification:** `SILICIUM-SPEC-001` (`v1.0.0`)

## Locked Decisions

1. **Milestone 1 Scope (`agent.operations`):**
   - `L0` Read Tool: `shopify.orders.get` backed by a credential-free deterministic Shopify fixture adapter.
   - `L4` Approval-Gated Partner Action: `shopify.orders.add_internal_note` on the sandbox order fixture, verified via read-back.
   - Proves the six-component Milestone 1 loop:
     `Think -> Plan -> Delegate -> Tool L0 -> Verify -> Founder Approval -> Tool L4 -> Verify -> Audit -> Learn`.
   - Security invariant: Approval tokens (`APR-*`) are cryptographically bound via SHA-256 (`proposed_action_hash`) to the exact canonical JSON payload. Any modified payload MUST be blocked by the Tool Gateway even with a valid `approval_id`.
   - `L5` boundaries remain absolute (refunds, price changes, spending, supplier commitments, credential changes, destructive actions are unconditionally denied at Gateway).

2. **Approval Channel & TTL Policy:**
   - Primary Tier 0 Approval Channel: **Telegram** (`telegram`)
   - Routine Approval TTL: **30 minutes** (`1800` seconds)
   - Strategic / Non-Urgent Approval TTL: **24 hours** (`86400` seconds)

3. **Language Policy:**
   - Internal reasoning, schemas, logs, and engineering artifacts: **English (`en`)**
   - Customer-facing default: **Egyptian Arabic (`ar-EG`)**, with English (`en`) supported where appropriate
   - Social content: `ar-EG`, `en`, or `bilingual` according to creative brief and channel

4. **Infrastructure Availability Discipline:**
   - Zero assumed live availability. Every external integration is explicitly tracked as `configured`, `verified`, or `unavailable` in `08_TOOLS_MCP/integration_status.json`.
   - Until independently verified, `shopify`, `n8n`, `openrouter`, `notion`, `metricool`, `openclaw`, `google_workspace`, and `telegram` remain `unavailable` for production execution.

5. **Runtime Incident Handling Note:**
   - Five transient `503 / No capacity` model gateway errors encountered during Phase 0 design work are logged as runtime/capacity incident `INC-20261125-CAP1` in `15_OBSERVABILITY/incidents/phase0_capacity_incident.json`, strictly separate from architectural validity.
