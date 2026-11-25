# Security Model (`SILICIUM-SEC-001`)

1. **No Secrets in Code, Prompts, Git, or Workflow Exports:** Runtime secrets inject via isolated environment/secret managers only.
2. **Machine-Enforced Tool Gateway:** Autonomy levels `L0–L5` are enforced in code (`08_TOOLS_MCP/tool_gateway.py`), never via prompt trust.
3. **SHA-256 Payload-Bound Approvals:** Every `L4` execution checks `approval.proposed_action_hash == canonical_sha256(inputs)` and single-use `consumed == false` + TTL expiration (`30 min` routine / `24h` strategic).
4. **Absolute L5 Deny List:** Money spend, refunds, credits, pricing/discounts, supplier commitments, policy/legal edits, prod deletions, and credential changes are unconditionally blocked.
5. **Dev / Prod Isolation:** Milestone 1 runs strictly in `dev` against credential-free fixtures.
6. **Append-Only Audit Trail & Redaction:** Every `success`, `failure`, and `blocked` event writes to `15_OBSERVABILITY/audit/` conforming to `17_audit_event.schema.json`.
