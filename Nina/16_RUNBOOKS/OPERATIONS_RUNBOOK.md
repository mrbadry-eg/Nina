# Operations & Milestone 1 Runbook

## Running Milestone 1 Evaluation & Tests
```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe 14_EVALUATION\run_eval.py
```

## Handling Blocked `L4` or `L5` Events
1. Inspect `15_OBSERVABILITY/audit/audit.jsonl` for `outcome: "blocked"`.
2. For `L5` attempts, inspect `15_OBSERVABILITY/incidents/` for automatically emitted `INC-*` records.
3. Never bypass the Tool Gateway to fulfill a blocked action. Resolve policy or issue a fresh Tier 0 `APR-*` token via Telegram.

## Local model provider
See [LOCAL_MODEL_RUNBOOK.md](LOCAL_MODEL_RUNBOOK.md) for LM Studio loopback setup, restricted-data routing, local logging caveats, and the synthetic opt-in smoke test.
