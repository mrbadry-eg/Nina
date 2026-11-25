# Evaluation Framework & Release Gates

Every agent and gateway change MUST pass deterministic evaluation cases conforming to `01_ARCHITECTURE/schemas/18_evaluation_case.schema.json` across 10 dimensions:
`factuality`, `policy`, `tool`, `permission`, `support`, `creative`, `platform`, `alignment`, `reliability`, `security`.

## Release Gate
- `100%` pass on `critical` and `high` severity cases.
- `100%` schema validation across inputs, agent tasks, approval requests, execution results, memory records, and audit events.
- Explicit failure tests for: missing `approval_id`, `inputs_hash` bait-and-switch, expired TTL, replay after `consumed=true`, and `L5` injection blocking.
