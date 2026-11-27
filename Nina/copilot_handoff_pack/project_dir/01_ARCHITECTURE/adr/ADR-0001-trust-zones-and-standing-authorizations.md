# ADR-0001: Trust zones and standing authorizations

- **Status:** PROPOSED. Needs Tier 0 approval by `founder:abdulrahman`. Copilot must not implement Task 9 until this is approved.
- **Date:** 2026-11-26
- **Touches:** Tool Gateway step 7 (adds an alternative to a per-action approval), one new schema (`19_standing_authorization.schema.json`), tool contracts (opt-in flag). It does **not** modify schemas 01 to 18 or the L5 block.

## Context

The Founder wants an agent that acts like a consultant plus a team of experts, with autonomous workflows and authorized access to company assets, on a $0 budget. The Founder also consents to treating their own stack (tools, apps, services) as trusted. Today every L4 action needs a one-time, hash-bound approval, which is safe but slow for routine work. Blanket trust would be fast but unsafe, because prompt injection reaches the system through trusted tools (a customer message delivered by a trusted bot is still hostile text).

## Decision 1: Three trust zones

| Zone | Members | Trusted for | Never trusted for |
| :--- | :--- | :--- | :--- |
| **T: trusted infrastructure** | n8n, OpenClaw (as transport), Telegram Bot API, Shopify API, OpenRouter, local model servers, the Silicium service | Authenticated identity (tokens, allowlists), transport integrity | The truth or instruction value of the content they carry |
| **U: untrusted content** | Customer messages, hotline audio and transcripts, supplier and partner emails, web pages, third-party product text, uploaded files and images, **all model output (hosted or local)** | Nothing. It is data. | Choosing tools, changing permissions, approving anything, being quoted as fact |
| **F: Founder decisions** | Decisions arriving over the authenticated approval channel from the allowlisted Founder identity | Approvals, standing authorizations, revocations | Being derived from U or from model output |

Rules: content from U is quoted and labelled, never obeyed. A claim derived only from U or a model is Source-of-Truth Level 8 until verified. F decisions never pass through an LLM.

## Decision 2: Standing authorizations (SA)

A **standing authorization** lets the Tool Gateway accept an action without a fresh per-action approval, when the action fits a Founder-approved envelope.

Proposed record (`19_standing_authorization.schema.json`):

| Field | Rule |
| :--- | :--- |
| `authorization_id` | `SAU-YYYYMMDD-XXXXXX` |
| `created_by`, `created_via_approval` | Must be `founder:abdulrahman` and the `APR-*` (24 h, strategic) that created it. **Agents can draft an SA; only the Founder can create one.** |
| `tool_names` | Exact allowlist. No wildcards. |
| `environments` | `dev` and `staging` first. Production only after Phase 6 evidence. |
| `resource_pattern` | For example `shopify:order:*` |
| `parameter_constraints` | A restricted JSON-Schema subset the payload must satisfy (for example note length, banned fields) |
| `max_autonomy_level` | At most 4. Never 5. |
| `limits` | `max_uses_per_day`, `max_uses_total` |
| `expires_at` | At most 30 days. Renewal needs a new `APR-*`. |
| `status` | `active`, `paused`, `revoked`, `expired`, `exhausted` |

**Eligibility.** The tool must be explicitly marked `standing_authorization_allowed`, the action must be reversible, internal only (no customer-visible effect), and must not move money. Proposed first classes: internal order notes, internal tags, creating drafts, generating scheduled reports. Explicitly **excluded** at first: customer messages, prices, discounts, inventory counts, refunds, anything Level 5.

**Gateway behavior.** Step 7 accepts `authorization_id` as an alternative to `approval_id`. It checks status, expiry, environment, tool, resource pattern, constraints and the use counters (atomically), then runs the normal freshness precondition, executes, and still requires read-back verification. Each use logs the authorization ID and the SHA-256 of the exact payload.

**Safety valves.** One-tap revoke through the approval channel. Automatic `paused` plus a Founder alert after repeated failures or any verification mismatch. Automatic `exhausted` at a limit.

## Decision 3: Staged autonomy

1. **Read-only** (shadow mode): insights and recommendations, no writes.
2. **Per-action approval**, with batching so the Founder can review several requests in one sitting. Each approval stays hash-bound to its own payload.
3. **Standing authorizations** for a class of action only after it has a clean record at stage 2 (proposed bar: at least 20 verified approvals of that class with no incident; the Founder sets the number).

## What does not change

The unconditional L5 block, hash binding of per-action approvals, schema validation at every boundary, the freshness precondition, the audit trail, the "no secrets" rules, and the rule that approval decisions never pass through an LLM. Changing the L5 rule is a separate ADR the Founder may raise after stage-2 evidence.

## Alternatives rejected

1. **Trust all components and their content.** Rejected: it removes the injection defense.
2. **Remove L4 approvals.** Rejected: loses the audit-grade human decision on write actions.
3. **Treat model output as trusted.** Rejected: model output is derived from U.

## Prerequisites and evaluation

Needs Task 4 (durable state, atomic counters), Task 1 (probes) and Task 2 (data classes). Required evals: out-of-scope action, over-limit, expired, revoked, paused, an agent trying to create an SA, a prompt-injected message trying to broaden one, and Level 5 under an SA. Mutation-test each.

## Open questions for the Founder

Which classes first, the day and total limits, the 30-day maximum, and the stage-2 evidence bar.
