# Silicium AI OS: Phase 2 handoff to GitHub Copilot

- **Prepared:** 2026-11-26, after Phase 1 hardening was delivered and verified. **Updated 2026-11-26** with the Founder's directives (section 0.5), which override anything earlier in this file that conflicts.
- **Repository:** `https://github.com/mrbadry-eg/Nina` (project directory = the folder containing `pyproject.toml`).
- **Audience:** GitHub Copilot (agent mode / coding agent) working with the Founder, `founder:abdulrahman`.
- **How to read this:** facts are labelled **[Founder]** (stated by the Founder, not independently verified), **[Verified]** (checked against a public source on 2026-11-26) or **[Proposal]** (a recommendation that needs a decision or an ADR). Do not treat a **[Proposal]** as an instruction to skip the decision.

---

## 0. Start here

1. Read `.github/copilot-instructions.md` (standing rules) and then this file.
2. Run both verification commands and confirm the baseline below before touching anything.
3. Do tasks in the order of section 5. Each task is one small PR. Task 0 must land before any real API key is created or pasted anywhere near this repo.
4. Anything that needs a business decision goes to section 6 ("Open decisions") instead of being guessed.

**Baseline at handoff (must stay green):**

| Command | Result |
| :--- | :--- |
| `pytest -v` | 138 passed, 0 failed |
| `14_EVALUATION/run_eval.py` | 14 / 14 passed (milestone1 5/5, phase1 9/9) |

Windows: `.\.venv\Scripts\python.exe -m pytest -v` and `.\.venv\Scripts\python.exe 14_EVALUATION\run_eval.py`. Ubuntu: `python3 -m pytest -v` and `python3 14_EVALUATION/run_eval.py`. (Phase 1 was verified on Linux, Python 3.12, against a fresh clone. Confirm once on the Founder's Windows Python 3.13 venv.)

---

## 0.5 Founder directives (these override earlier recommendations)

1. **Budget is $0.** Free and open-source first. No paid APIs, credits, subscriptions or SaaS. Every new dependency must be free, and OSI-licensed where possible. Free but proprietary services (OpenRouter free tier, Telegram Bot API, Shopify Admin API) are acceptable only behind an interface so they can be swapped. The earlier "buy $10 of credits" option is **withdrawn**; design for the 50 requests/day limit instead.
2. **Free models are the baseline.** Confirmed from the Founder's OpenRouter comparison screen: both Nemotron models cost $0 per million input and output tokens, have 1M context and reasoning; Super VL accepts text, images and video, Ultra accepts text only; both output text only; each shows **1 provider**.
3. **End goal:** a capable, helpful, efficient AI agent that manages the company like a consultant plus a team of experts: it gives insights and takes actions through autonomous workflows with authorized access to company assets. Build value-first in three stages: (a) read-only insights, (b) supervised actions, (c) standing authorizations (item 5).
4. **Trust model.** The Founder consents to treating their own stack (tools, apps, services) as trusted parties. Implement this as **trust zones** (ADR-0001): *trusted infrastructure* (n8n, the OpenClaw gateway as transport, Telegram, the Shopify API, OpenRouter, local models) is trusted for authentication and transport; *untrusted content* (customer messages, hotline transcripts, supplier emails, web pages, third-party product text, and all model output) is never an instruction, no matter which trusted component delivered it. Do **not** remove schema validation, hash binding, the freshness check or the L5 block on the strength of this directive. A prompt injection arrives through a trusted tool, not around it. (Test `ORD-1044`: a hostile customer note demanding a refund must stay inert.)
5. **Autonomy comes from standing authorizations, not blanket trust.** The Founder pre-approves a *class* of low-risk actions with explicit scope, limits and an expiry, revocable in one tap; each use is audited. Level 5 (refunds, credits, pricing and discounts, spend, supplier terms, legal or policy text, production deletions, credential changes) **stays human-executed**; the agent prepares everything so the last step is one tap. The Founder owns that rule and can change it only by an ADR after evidence from shadow mode. See `01_ARCHITECTURE/adr/ADR-0001-trust-zones-and-standing-authorizations.md`.
6. **Consent scope.** The Founder's consent covers the Founder's business data. It does not cover customers' personal data (phone numbers, voices, addresses). Hosted free endpoints log prompts and the provider may use them, and OpenRouter's notice explicitly excludes personal data including voices. So anything containing customer personal data runs on **local open-source models only** (Task 3b), never on a hosted free endpoint.
7. **010hotline.** [Founder] The Founder operates it. Its purpose and how it connects (phone calls, WhatsApp, SMS, recordings) have **not been explained**. Do not design an integration or assume a channel. Treat it as an untrusted-content channel that carries personal data, and ask the Founder first (D10).

### $0 stack

| Layer | Free choice | Notes |
| :--- | :--- | :--- |
| Models, non-personal work | OpenRouter free Nemotron Ultra / Super VL | Free service, not open source. Behind `ModelProvider`, so replaceable. 50 requests/day, account-wide. |
| Models, personal or internal data | Local open-weight model served by Ollama or llama.cpp in Docker on Ubuntu | Free and private. Model size depends on the machine's RAM/GPU (D11). Both expose an OpenAI-compatible chat endpoint. |
| Speech-to-text (hotline, later) | Local Whisper-family model (for example faster-whisper) | Open source. Measure accuracy on real Egyptian-Arabic samples before trusting it. |
| Orchestration | n8n, self-hosted | Free for internal use but **fair-code (Sustainable Use License), not OSI open source**. Node-RED (Apache-2.0) is the open-source alternative if that ever matters. |
| Agent gateway | OpenClaw | Check its license in its repo before relying on it. |
| State and logs | SQLite and JSONL | Public-domain / plain files. |
| Service layer | Python standard library; FastAPI (MIT) only if accepted | Keep dependencies minimal. |
| Messaging | Telegram Bot API | Free service. |
| Store data | Shopify Admin API through a custom app | No API fee; the existing store plan is the only cost. |
| Quality and security | pytest, jsonschema, gitleaks | All open source; run locally. |

---

## 1. What exists (Phase 1 is done)

Control loop: Think -> Plan -> Delegate -> Tool L0 -> Verify -> Founder approval (SHA-256 bound) -> Tool L4 -> Verify -> Audit -> Learn. It runs end to end against a credential-free Shopify fixture.

| Layer | Path | What it does |
| :--- | :--- | :--- |
| Brand and policy | `03_BUSINESS_CONTEXT/BRAND_BIBLE.md`, `policies/*.md`, `policies/policy_index.json` | All **PROPOSED**, not approved. Business values the Founder has not decided are `UNSET` open decisions (return windows, COD fees, couriers...). Never invent them. |
| Source of Truth | `04_SOURCE_OF_TRUTH/truth_resolver.py` | 8-level precedence, staleness (300 s live, 86400 s catalog), blocks on stale live/catalog data, discrepancy logging, policy only counts when Founder-approved. |
| Gateway | `08_TOOLS_MCP/tool_gateway.py` | 8-step authorization (L0-L5). New: optional `evidence=` freshness precondition (step 6b) that runs **before** the single-use approval is consumed. |
| Approval state | `08_TOOLS_MCP/approval_store.py` | Single use, TTL, hash binding. **In memory.** New: only `pending` requests can be approved/rejected; `get_request()`. |
| Approval channel | `08_TOOLS_MCP/approval_adapter.py` | `ApprovalChannelAdapter`, `TelegramApprovalAdapter` (Bot API payload + local stub), `LocalCliApprovalAdapter`. Buttons carry a hash prefix; sender identity comes from an allowlist, never from message text. |
| Model routing | `12_MODEL_ROUTING/model_router.py`, `routing_table.json` | `model_class` -> fallback chain, `ModelProvider` interface, `MockProvider`, USD spend cap (80% soft alert, 100% breaker), incident on chain exhaustion referencing `INC-20261125-CAP1`. |
| Master Agent | `05_AGENTS/master/master_agent.py`, `phase1_stack.py` | Wires everything. `build_phase1_stack(base_dir, ...)` builds a fully wired, isolated stack for tests/evals. All new components are optional. |
| Evaluation | `14_EVALUATION/run_eval.py`, `phase1_eval.py`, `cases/phase1_cases.json` | Runs both suites. Phase 1 evals were mutation-tested (breaking the safety logic makes the right case fail). |

**Known limitations of Phase 1 (by design, now yours to improve):**
- The model provider is a **mock** (`simulated=True`, model ID shown as `mock:<slug>`). Nothing has called a real model.
- The Telegram adapter is a **local stub**: it queues to an outbox and reports `delivered: False`.
- `ApprovalStore`, the spend ledger (unless given a path) and per-process incident de-duplication are **in memory**.
- The USD spend cap cannot trip on free models (cost is $0).
- The Master's planning call is **advisory only** and labelled `inference`; all decisions are deterministic.
- "Request changes" on an approval rejects the original (a changed payload has a new hash, so it needs a new `APR-*`).
- The cap is checked before each call, so one call can overshoot by at most its own cost. The spend day is the UTC calendar day.

---

## 2. Environment and infrastructure

### 2.1 Stated by the Founder [Founder]
- Models: **NVIDIA Nemotron 3 Ultra (free)** or **NVIDIA Nemotron 3.5 Super VL (free)**, via **OpenRouter**.
- **OpenClaw** is connected to **Telegram** and **OpenRouter**. UI/gateway: `http://localhost:59048/`.
- **n8n:** `http://localhost:5678/`.
- **Docker** and **Ubuntu** are installed.
- **Google Antigravity** is on a free plan with very limited usage. **Do not depend on it.**
- Host Python venv is Windows: `.venv\Scripts\python.exe`. The Phase 0 spec targets a Windows workspace under a OneDrive-synced folder (see F8).

> The Founder says these are "connected", but `08_TOOLS_MCP/integration_status.json` still lists every one as `unavailable`, and its rule says nothing is invoked live until `verified`. Nobody has recorded evidence. Task 1 builds the probes that produce it.

### 2.2 The two models [Verified: openrouter.ai model pages, 2026-11-26]

| | Nemotron 3 Ultra (free) | Nemotron 3.5 Super VL (free) |
| :--- | :--- | :--- |
| Slug | `nvidia/nemotron-3-ultra-550b-a55b:free` | `nvidia/nemotron-3.5-super-vl:free` |
| Input | text | text, images, documents/GUI screenshots, video |
| Context | 1M tokens | 1M tokens |
| Max completion | 65,536 | 131,072 |
| Tool calling (`tools`, `tool_choice`) | yes | yes |
| `response_format` (JSON schema) | **not supported** (JSON not enforced) | supported |
| Reasoning | reasoning model | on by default; can be turned off or given a token budget |
| Released | 2026-06-04 | 2026-10-15 |

Both pages carry the same warning for the free endpoint: **do not send confidential information or personal data; use is logged and used to improve NVIDIA products and services.** Paid (non-`:free`) variants of these models exist with different terms; check OpenRouter before relying on them.

Consequences: Ultra output must always be validated in code (never trust it to be JSON). Use Super VL when you need schema-constrained output or vision (for example a customer's photo of a damaged item), but see F1 before sending any customer image.

### 2.3 OpenRouter free-tier limits [Verified: OpenRouter docs/support, 2026-11-26]
- Free (`:free`) models: **20 requests/minute**, and **50 requests/day** until the account has purchased **$10 of credits in total (lifetime)**, then **1,000/day**. The upgrade is permanent.
- The limits are **account-wide, not per model**. Pinning a different free model does not raise them.
- A third-party evaluation reports that **failed requests can still count** against the daily allowance, so blind retry loops can burn the quota. [Third-party observation; verify with `GET /api/v1/key`.]
- `GET https://openrouter.ai/api/v1/key` returns the limits actually enforced on a key. Use it for probes: it is a metadata call, not a model completion.
- Negative account balance can break even free models.

### 2.4 OpenClaw [Public docs; verify against the installed version]
Public documentation describes OpenClaw as a **local-first gateway daemon**: it connects messaging channels (Telegram via the Bot API, WhatsApp, Discord, Slack...) to an embedded agent runtime that can **call tools, execute code (`exec`), browse, and run cron/webhooks**. The gateway is token-authenticated, and community Docker images default to a different port than the Founder's `59048`, so the Founder's instance has custom settings. The docs recommend loopback-only binding. Treat OpenClaw as a **powerful, LLM-driven component** that must sit *outside* the trust boundary of approvals (see F4).

### 2.5 Antigravity
Not a dependency. `11_ANTIGRAVITY/BOUNDARIES.md` and spec section 8.4 describe an "engineering plane" with dev/prod isolation. Copilot is now the implementing agent, so **those isolation rules bind Copilot**, the tool does not matter. In particular: engineering work has no authority to execute production business mutations and no production Shopify write key may exist in the dev environment.

---

## 3. Non-negotiables

Carried over from Phase 1, unchanged:
1. Milestone 1 baseline and all Phase 1 tests/evals stay green.
2. Credential-free by default; deterministic local fakes for tests.
3. Language: internal `en`; customer-facing default `ar-EG` (`en` secondary); social per brief.
4. Agents reason; n8n orchestrates; tools act; memory remembers; policy controls; the Founder approves.
5. Creative memory is not product truth. Approvals are bound to the canonical payload SHA-256. L5 is blocked unconditionally at the Tool Gateway.

New:
6. Free OpenRouter endpoints receive **public data only** (F1).
7. No approval decision ever flows through an LLM (F4).
8. No integration is marked verified on assertion; evidence is recorded by a probe (F5).

---

## 4. Findings that change the plan

**F1. The free endpoints log prompts and NVIDIA may use them.** The Phase 1 policy `POL-DATA-MINIMIZATION-001` (DM-05) already says send the minimum to providers, but the router currently sends whatever it is given. The Master's advisory call includes the task objective, inputs (order IDs) and constraints, which are internal business data. Open decision **OD-DM-3** (provider data terms) is therefore not a formality: until the Founder decides what may go to which provider, a live provider must not be enabled by default. Customer-facing replies (personal data, Arabic) cannot use the free endpoints at all. *Needs a data classification and enforcement in the router (Task 2).*

**F2. The quota is account-wide and shared.** Falling back from Ultra to Super VL does nothing against a daily or per-minute 429, because both draw from the same pool, and OpenClaw uses the same key (assuming it was configured with the same OpenRouter account). The Phase 1 fallback chain would simply burn through failed attempts. The USD circuit breaker never trips at $0. *Needs a request-count budget (rpm/rpd), no retry loops, and a quota reserve for critical calls (Task 3).*

**F3. The Master's advisory call is expensive at this tier.** With 50 requests/day, one advisory call per task is a poor use of quota, and it is the call that carries internal data. *Make advisory routing opt-in and default it off when a free-tier provider is active (Task 3). The control plane already works without it.*

**F4. Who owns the Telegram connection?** OpenClaw already holds a Telegram bot connection. Telegram allows only one active update consumer (long-poll or webhook) per bot token, so a second component using the same token will conflict. More importantly, OpenClaw routes inbound messages through an LLM agent that can execute tools. An approval path that passes through it could be spoofed by prompt injection. *Recommendation [Proposal]: a **separate, dedicated approval bot** with its own token, handled by deterministic Silicium code (directly or relayed by n8n), with an allowlist of the Founder's Telegram user ID. Keep OpenClaw's bot for conversation only.* Decision D2.

**F5. Roadmap vs reality.** Spec section 10 puts live credentials in Phase 6 (after a red-team). The Founder already runs OpenRouter, OpenClaw and n8n locally. That is fine for development, but `verified` currently means "may be invoked for live production execution". *Recommendation [Proposal]: add a distinct `dev_verified` state (dev/staging only, never production), introduced by an ADR the Founder approves, so local use is honest and production stays gated.* Decision D5.

**F6. No `.gitignore`, and compiled files are committed.** Verified: no `.gitignore` exists; 10 `__pycache__/*.cpython-313.pyc` files are tracked. The security model forbids secrets in Git but nothing enforces it, and runtime audit logs (which can hold PII) would be committed too. *Task 0.*

**F7. State is in memory.** `ApprovalStore` loses pending approvals on restart, and single-use consumption is not atomic across processes. That is acceptable for a library test harness and unacceptable once n8n and OpenClaw call a long-running service. *Task 4.*

**F8. OneDrive and append-only audit files.** The spec targets a OneDrive-synced Windows path. Sync clients can lock, duplicate or conflict-copy files that are appended to constantly, and they replicate PII-bearing logs to the cloud. *Keep runtime data (`SILICIUM_DATA_DIR`) outside any synced folder.*

**F9. Topology is unknown.** n8n and OpenClaw run on the Founder's machine; Python is a Windows venv; Docker and Ubuntu exist (possibly WSL2). A container reaching a service on the host needs `host.docker.internal` (Docker Desktop) or `extra_hosts: "host.docker.internal:host-gateway"` (Linux). Ask where each component actually runs before writing compose files (Decision D7).

**F10. Both models share one provider.** OpenRouter shows "1 provider" for each Nemotron model. Falling back from Ultra to Super VL therefore gives no real redundancy: an NVIDIA or OpenRouter outage, or the account quota running out, takes both down together. A **local open-source model as the last fallback** (Task 3b) is the only way to keep the agent working at $0, and it is also the private path for personal data.

**F11. Consent scope (see 0.5 item 6).** The hotline brings customers' phone numbers and voices into scope. The Founder can accept risk for their own data, but not on behalf of customers; applicable Egyptian data-protection obligations remain (OD-DM-1: counsel review).

---

## 5. Work plan (do in order; one PR each)

Every PR: tests first, `pytest` and `run_eval.py` green with counts reported, evals added for new safety properties, no network in unit tests, no secrets.

### Task 0: Repository hygiene (no behavior change)
- Add `.gitignore` at the git root (provided in this pack) and `.env.example` in the project directory (provided).
- Untrack the 10 committed compiled files (they stay on disk). Bash: `git ls-files '*.pyc' | xargs -d '\n' git rm --cached`. PowerShell: `git ls-files "*.pyc" | ForEach-Object { git rm --cached $_ }`. Then commit.
- Add secret scanning as a pre-commit hook or CI job (for example `gitleaks`) and document it in `16_RUNBOOKS/`.
- Add a test that fails if any tracked file matches common secret patterns (`sk-or-`, `shpat_`, bot-token shape `\d{8,10}:[A-Za-z0-9_-]{35}`) or if a tracked path is under `15_OBSERVABILITY/audit/`.
- **Done when:** a fresh clone is clean after running tests; `git status` shows no runtime artifacts.

### Task 1: Integration probes and recorded evidence
- New `08_TOOLS_MCP/integration_probe.py` with one read-only probe per integration and an injected HTTP transport:
  - `openrouter`: `GET /api/v1/key` (validates the key and reads enforced limits; no model call).
  - `n8n`: its health endpoint on `http://localhost:5678` (verify the exact path for the installed version).
  - `openclaw`: reachability of `http://localhost:59048/` (authenticated if a token is configured).
  - `telegram`: `getMe` and `getWebhookInfo` for the **approval** bot only.
- Probes write evidence (timestamp, status code, redacted detail) to `15_OBSERVABILITY/probes/` (git-ignored). They never edit the tracked `integration_status.json`.
- CLI: `python -m` entry or `08_TOOLS_MCP/probe_cli.py --integration n8n`.
- **Done when:** unit tests use fake transports (success, 401, timeout, malformed body); a `live`-marked smoke test is skipped unless `SILICIUM_LIVE=1`; no secret appears in evidence files (test it).
- ADR for `dev_verified` (see D5) drafted but **not enabled** until the Founder approves.

### Task 2: Data classification and enforcement
- Add a data class to model calls: `public`, `internal`, `restricted`. Default is `internal` (fail closed).
- Routing table metadata per model: `{"endpoint_tier": "free"|"paid"|"local", "logs_prompts": true|false}`. Free NVIDIA endpoints: `free`, `logs_prompts: true`. They accept **`public` only**.
- `ModelRouter.complete(..., data_class=...)` raises a new `DataPolicyViolationError` before any provider call when the class exceeds what the endpoint allows, logs an audit event (outcome `blocked`), and never falls back to a *less* private tier.
- Add proposed rule `DM-11` to `data_minimization_policy.md` (status stays PROPOSED) and update `policy_index.json` machine values.
- Master advisory prompt: classify as `internal`. With only free endpoints active it is blocked, so planning degrades gracefully (already supported).
- **Done when:** tests prove `internal`/`restricted` never reach a free endpoint, a paid/local fallback is allowed only if it is at least as private, and a new eval (`EVAL-SECURITY-0xx`) covers it. Mutation-test the eval.

### Task 3: Real OpenRouter provider and quota-aware routing
- `OpenRouterProvider(ModelProvider)` using the OpenAI-compatible `POST {base}/chat/completions`. Prefer the standard library (`urllib`) or one explicit dependency decision. API key from `OPENROUTER_API_KEY` only.
- Map errors: `429` -> `ProviderQuotaError`; `5xx`/`503` -> `ProviderCapacityError`; `400` -> `ProviderRequestError` (non-retryable); `401/402/403` -> a new non-retryable configuration error that opens an incident and never falls back.
- Update `routing_table.json` with the real slugs from section 2.2. All six classes must still exist and equal the `model_class` enum. Suggested mapping [Proposal]: `reasoning`, `research`, `coding`, `fast`, `creative` -> Ultra primary with Super VL fallback, then the local model (Task 3b) as final fallback; `vision` -> Super VL primary (never for customer photos, see 0.5 item 6). Confirm with the Founder; tune `max_tokens_per_call` for free-tier latency.
- `RequestBudget` (persistent, UTC day): rolling 20/min and N/day (N from `GET /api/v1/key`, default 50). Rules: stop the whole chain on a daily-quota 429 (a fallback cannot help); **no retry loops**, `max_attempts_per_model=1`; reserve a share (for example 20%) of the daily budget for `critical=True` calls; alert the Founder at 80% and open a breaker at 100% by *request count*, in addition to the USD cap.
- Make Master advisory routing opt-in (config flag, default off when a free-tier provider is active).
- `simulated` must be `False` and `model_used` must be the bare slug for real calls (the `mock:` prefix is only for the mock).
- Remember reasoning models spend completion tokens on reasoning; set the output cap accordingly and **validate any structured output in code**.
- **Done when:** tests use a fake HTTP transport and cover each status mapping, budget exhaustion, critical reserve, persistence across restart and the contract-violation path; a `live` smoke test makes one tiny call and is skipped by default.

### Task 3b: Local open-source provider ($0, private)
- One `OpenAICompatibleProvider(base_url, api_key=None)` class can serve both OpenRouter and a local Ollama or llama.cpp server (both speak the OpenAI-style chat endpoint). Reuse it for Task 3 instead of writing two clients.
- Run the local server in Docker on Ubuntu; choose the model after the Founder reports the machine's RAM/GPU (D11). Record it in `routing_table.json` with `endpoint_tier: "local"`, `logs_prompts: false`. It is the only tier that may receive `restricted` data (customer personal data).
- Chain rule: a hosted free model may fall back **to** local, never the reverse for personal data. If the local server is down, a `restricted` call **fails closed**; it must not spill to a hosted endpoint.
- **Done when:** tests use a fake HTTP transport; an eval proves `restricted` data never reaches a hosted endpoint, even when local is down (mutation-test it); a `live` smoke test is skipped by default.

### Task 4: Durable state
- SQLite (stdlib) implementation of the `ApprovalStore` interface with the same behavior (single use, TTL, hash binding, `pending`-only transitions). Consume must be atomic: a threaded double-consume test must see exactly one success.
- Persist `SpendLedger`/`RequestBudget` and incident de-duplication in the same database or files under `SILICIUM_DATA_DIR`.
- Make audit appends safe for multiple writers (file lock or one writer process) and keep the JSONL format and schema 17 unchanged.
- **Done when:** restart-safety tests (approve, restart, execute) pass; the in-memory store stays available for unit tests; the Master and Gateway need no change beyond constructor injection.

### Task 5: Silicium HTTP service (the seam for n8n and OpenClaw)
- Thin service wrapping `MasterAgent` (framework is a decision; the stdlib `http.server` is enough for a first version, or FastAPI if the Founder accepts the dependency). Bind **127.0.0.1** by default.
- Endpoints [Proposal]: `POST /v1/tasks` (validated against schema 06 -> `plan_and_delegate`), `POST /v1/approvals/telegram-update` (raw Bot API update -> `handle_approval_update`), `POST /v1/approvals/{id}/execute`, `GET /healthz`.
- Auth: bearer `SILICIUM_SERVICE_TOKEN`, constant-time comparison, reject missing/short tokens at startup. Request size limits. Idempotency key on task creation so n8n retries cannot double-plan.
- Dockerfile and a compose snippet for Ubuntu/Docker; document how n8n reaches it (F9).
- **Done when:** tests drive the service in-process; schema-invalid bodies return 4xx without side effects; an L5 task is refused; no endpoint can execute without a valid approval.

### Task 6: Telegram approval transport
- Per Decision D2, implement the `transport` callable for `TelegramApprovalAdapter` (Bot API `sendMessage` with the inline keyboard already produced) and the inbound path for `callback_query` updates.
- Inbound: webhook with Telegram's `secret_token` header check, or n8n relaying the raw update to `/v1/approvals/telegram-update`. Either way: allowlist the Founder's numeric Telegram user ID from `TELEGRAM_FOUNDER_USER_ID`, ignore everything else.
- Handle Telegram constraints already encoded in the adapter (4096-character text, 64-byte callback data). Answer the callback query and remove the keyboard after a decision so the button cannot be pressed twice.
- Keep the stub as the default when the bot is not `verified`.
- **Done when:** tests replay recorded Bot API payloads (approve, reject, change request, forged sender, wrong chat, replayed press, expired request); an eval proves a prompt-injected message cannot approve anything.

### Task 7: n8n workflows (orchestration only)
- Put exported workflow JSON in `09_N8N/` with **credentials stripped**. Workflows only call the Silicium service and format/route data. **No business rules, no direct Shopify or model calls, no approval logic in nodes.**
- First workflows: task intake -> `/v1/tasks`; Telegram approval relay -> `/v1/approvals/telegram-update`; scheduled integration health probe -> Task 1.
- Add a check (test) that scans workflow JSON for secrets, hard-coded tokens and `localhost` URLs that should be variables.
- **Done when:** the workflows import into the Founder's n8n and run against the dev service; the check passes.

### Task 8: OpenClaw boundary
- Document and template (`10_OPENCLAW/`) the safe configuration: loopback-only bind, gateway token from env, Telegram allowlist limited to the Founder, **no Shopify credentials and no direct write tools**, actions only through the Silicium service (so the Tool Gateway still authorizes everything), `exec`/`browser` tools disabled or sandboxed unless the Founder explicitly wants them.
- Register OpenClaw in `08_TOOLS_MCP/agent_registry.json` with minimal autonomy (read-only/L0-L1) via an ADR.
- Quota coordination: OpenClaw and Silicium share one OpenRouter account; decide whether they use separate keys (keys do not raise limits, but separate keys give separate visibility) and how the budget is split.
- **Done when:** an eval shows a hostile Telegram message to OpenClaw cannot cause an L4/L5 action or an approval; the runbook explains how to rotate the OpenClaw token and the OpenRouter key.

### Task 9: Standing authorizations (after Task 4, and only after the Founder approves ADR-0001)
- Implement the mechanism in the ADR: a Founder-created, scoped, limited, expiring, revocable authorization that the Tool Gateway accepts as an alternative to a per-action `approval_id`. Creation itself needs a Tier 0 approval (`APR-*`, 24 h). Each use logs the authorization ID and the SHA-256 of the exact payload.
- One-tap revoke through the approval channel, and an automatic stop when a limit is reached or it expires.
- **Done when:** evals prove an action outside scope, over a limit, after expiry, after revocation, or at Level 5 is blocked and that no authorization can be created by an agent.

### Task 10: Read-only live Shopify "shadow mode" and the Founder Daily Brief [Proposal: needs a Founder ADR to pull this forward from Phase 6, read-only only]
- Shopify custom-app token with **read scopes only**, read from the environment. Add read tools to the Tool Gateway (orders, products, inventory) with `verification_method`, and build Truth Resolver claims from live reads.
- Daily Brief (the first "consultant" output): SLA breaches, low stock, catalog-quality gaps, source discrepancies, and a short list of recommended actions. Analysis is deterministic. Any model narration uses **aggregated, non-personal data** on a hosted free model, or a local model if personal data is involved.
- Delivered to the Founder through the approval/notification channel. No writes. Run in shadow mode and record outcomes as evidence for the stage-(b) decision.
- **Done when:** runs against the fixture in tests and against the live store read-only on the Founder's machine; no personal data appears in any hosted-model prompt (test it).

*After Task 8, Phase 2 exit criteria (spec section 10) are: Master Agent passes routing, delegation and L5-block evaluation suites with the real provider in shadow mode.*

---

## 6. Open decisions for the Founder (Copilot: ask, do not decide)

| ID | Decision | Recommendation |
| :--- | :--- | :--- |
| D1 | Which data classes may go to which provider (OD-DM-3). | Free NVIDIA endpoints: public data only. Anything with customer personal data needs a paid provider with acceptable terms or a local model. |
| D2 | Who owns the Telegram approval channel. | Separate approval-only bot with its own token, deterministic code, Founder-ID allowlist. Do not route approvals through OpenClaw. |
| D3 | ~~Buy $10 of OpenRouter credits.~~ **Withdrawn: budget is $0.** | Design for 50 requests/day: request budget, no retry loops, critical reserve, local fallback. |
| D4 | Local open-source model for `restricted` data. | **Now required** for anything with customer personal data (0.5 item 6). Task 3b. |
| D5 | Introduce a `dev_verified` integration state. | Yes, via ADR; keeps `verified` meaning production-ready. |
| D6 | Advisory model routing in the Master: on or off. | Off by default on free tier. |
| D7 | Where each component runs (Windows host, WSL2/Ubuntu, Docker) and where the repo and `SILICIUM_DATA_DIR` live. | Repo and data outside OneDrive. Put the Silicium service in Docker on Ubuntu alongside n8n if feasible. |
| D8 | Approve the Brand Bible and the four policies, and fill the `UNSET` values (return window, who pays return shipping, COD limits/fees, InstaPay verification window, courier list...). | Until then the resolver correctly reports a policy gap and escalates. |
| D9 | Have a native Egyptian-Arabic speaker review the Brand Bible examples. | Yes, before approval. |
| D10 | What is 010hotline and how does it connect (inbound calls, WhatsApp, SMS)? Is anything recorded or transcribed? | Needed before any design. Personal data is involved either way. |
| D11 | The machine that runs Docker/Ubuntu: RAM, GPU, free disk. | Sizes the local model (Task 3b). |
| D12 | Approve ADR-0001 (trust zones and standing authorizations). | Yes; it is how autonomy grows safely. Review the limits it proposes. |
| D13 | Pull **read-only** live Shopify forward from Phase 6 (Task 10). | Yes: it gives real insights at $0 with no write risk. |

---

## 7. Working agreements

- **Small PRs**, tests first, explain the *why*. Include the exact `pytest` and `run_eval` counts.
- **Evals for safety properties**, and mutation-test them: temporarily break the logic and confirm the intended case fails. (Phase 1 caught a weak eval this way.)
- **ADRs** in `01_ARCHITECTURE/adr/` for anything that changes a boundary, a status semantic, a schema or a registry. Frozen artifacts need Founder approval.
- **Never** add a path around the Tool Gateway, weaken L5, store or log secrets, mark an integration verified without evidence, skip or delete a failing test, or call the production Shopify store.
- If you discover a bug in baseline code, fix it minimally with a test and call it out explicitly in the PR (Phase 1 found and fixed one: a rejected approval could be re-approved).

---

## 8. Quick API reference (Phase 1)

```python
# Truth
TruthResolver(truth_config=None, founder_context=None, policy_index=None, audit_logger=None)
.resolve(fact_key, claims, now=None, max_age_seconds=None, require_authoritative=True, task_id=None, agent=None)
.resolve_many(claims, ...) ; .resolve_policy(policy_id, key, extra_claims=(), ...)
.assert_fresh(claims, now=None) -> raises StaleLiveDataError
.claims_from_product_truth(record) ; .claims_from_order_result(exec_result)
Claim(fact_key, value, level, source, observed_at, verification=None, metadata={})

# Gateway
ToolGateway(fixture=None, approvals=None, audit_logger=None, truth_resolver=None)
.invoke(task_id, agent_id, environment, tool_name, inputs, approval_id=None, now=None, evidence=None)

# Router
ModelRouter(provider=None, routing_table=None, integration_status=None, ledger=None,
            audit_logger=None, alert_sink=None, environment="dev", max_attempts_per_model=1)
.complete(model_class, messages, task_id=None, agent=None, critical=False, max_tokens=None, now=None)
ModelProvider.complete(model_id=, messages=, max_tokens=, now=) -> ProviderResponse
# errors: ProviderCapacityError(503) ProviderQuotaError(429) ProviderRequestError(400, non-retryable)
#         CircuitBreakerOpenError  ModelRoutingExhaustedError  UnknownModelClassError  ProviderNotConfiguredError

# Approvals
TelegramApprovalAdapter(founder_chat_id=, identity_map=, transport=None, integration_status=None)
.format_request(request, now=None) -> ApprovalMessage ; .send(message) ; .parse_decision(update) ; .apply_decision(decision, approvals)

# Master
MasterAgent(gateway=, operations_agent=, memory_store=, truth_resolver=None, model_router=None,
            approval_adapter=None, audit_logger=None)
.plan_and_delegate(task, environment="dev", now=None) ; .handle_approval_update(update)
.execute_approved_request(request_id) ; .execute_approved_l4(...)
build_phase1_stack(base_dir, provider=None, routing_table=None, policy_index=None, ...)
```

Audit field gotcha: audit `tokens` is `{input, output}`; AgentTask `token_usage` is `{input_tokens, output_tokens}`.
