# n8n Deterministic Orchestration Plane (Phase 3+ Target — Unavailable in Phase 0)

Status: `unavailable` (see `08_TOOLS_MCP/integration_status.json`).

## Role Separation
- **n8n orchestrates:** Triggers, crons, webhooks, retries, idempotency, routing, approval waiting, notifications.
- **n8n does NOT reason:** Semantic classification, drafting, and planning happen in Agents via the Model Router.
- **n8n does NOT bypass policy:** All writes route through `08_TOOLS_MCP` Tool Gateway.
