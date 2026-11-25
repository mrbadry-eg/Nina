# Antigravity Engineering Plane Boundaries

Google Antigravity operates strictly inside the **Engineering Plane (`dev` / `staging`)** and does NOT automatically inherit production business authority.

| Action | Allowed | Enforcement |
| :--- | :--- | :--- |
| Modify code, schemas, validators, tests, docs | **YES** | Workspace-scoped repo access |
| Author/validate n8n JSON & MCP tool defs | **YES** | Dev validators & test fixtures |
| Inspect redacted audit logs & eval reports | **YES** | Secret-stripped audit sink |
| Propose Shopify / prod deployment changes | **YES (Draft / APR)** | Requires Tier 0 `approval_request` |
| Change prod prices, discounts, or issue refunds | **NO (`L5` Blocked)** | Absent prod keys + Tool Gateway deny |
| Publish social media or public brand claims | **NO (`L4` Approval)** | Requires Founder signed `APR-*` |
| Read/modify production secrets or credentials | **NO (`L5` Blocked)** | Excluded from repo and runtime env |
