# ADR-0002: Development-verified integration status

- **Status:** PROPOSED. Requires Tier 0 approval by `founder:abdulrahman`; no status behavior changes before approval.
- **Date:** 2026-11-27
- **Touches:** `08_TOOLS_MCP/integration_status.json` status semantics and the runtime gate that decides which environments may invoke integrations. This proposal does not change the file or implement a gate.

## Context

`integration_status.json` currently allows live production execution only when an integration has `status == "verified"`. The Founder has stated that n8n, OpenClaw, Telegram, and OpenRouter are connected locally, but those statements are not probe evidence and do not change their recorded `unavailable` statuses. Task 1 adds read-only probes and stores their evidence separately.

## Proposed decision

Add a distinct `dev_verified` status for integrations tested with read-only probes in development or staging. It would permit only the explicitly scoped non-production use approved by a later implementation. `verified` would retain its production-ready meaning and remain required for production execution. A development probe must never promote an integration to either status automatically.

Probe evidence should identify the integration, timestamp, HTTP status code when available, outcome, and a redacted summary. A successful health or authentication probe is evidence of reachability or credential validity only; it is not evidence that an integration is safe or authorized for business mutations.

## Consequences

- Until this ADR is approved and its gate is implemented, existing `integration_status.json` semantics remain unchanged.
- Probe results do not update integration statuses.
- Production remains blocked unless the existing `verified` requirement is independently satisfied.
- Dev/staging use must remain isolated from production credentials and data.

## Alternatives considered

1. Treat the Founder's connection statements as verification. Rejected because they are not recorded, reproducible evidence.
2. Reuse `verified` for local checks. Rejected because that would blur the production gate.
3. Keep all integrations unavailable until Phase 6. Safe but does not distinguish tested local development readiness from production readiness.

## Approval

Pending Founder decision. No implementation or status migration is authorized by this proposal alone.
