"""Phase 1 / Layer 3: model-routing fallback layer, spend cap and provider interface."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silicium_loader import REPO_ROOT, load_module

_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_mr = load_module("12_MODEL_ROUTING/model_router.py", "silicium_model_router")

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
MSGS = [{"role": "user", "content": "Summarise order ORD-1042 status"}]
TABLE = json.loads((REPO_ROOT / "12_MODEL_ROUTING" / "routing_table.json").read_text(encoding="utf-8"))
REASONING = TABLE["classes"]["reasoning"]
FAST = TABLE["classes"]["fast"]


def _router(tmp_path: Path | None = None, provider=None, **kw):
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc") if tmp_path else None
    provider = provider or _mr.MockProvider()
    return _mr.ModelRouter(provider=provider, audit_logger=audit, **kw), provider, audit


def _events(tmp_path: Path) -> list[dict]:
    p = tmp_path / "audit.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ------------------------------------------------------------------ table + classes


def test_routing_classes_equal_canonical_model_class_enum() -> None:
    schema = json.loads((REPO_ROOT / "01_ARCHITECTURE/schemas/07_agent_task.schema.json").read_text(encoding="utf-8"))
    enum = next(p["properties"]["model_class"]["enum"] for p in schema["allOf"] if "properties" in p and "model_class" in p["properties"])
    router, _, _ = _router()
    assert sorted(router.table["classes"]) == sorted(enum) == sorted(["fast", "reasoning", "creative", "vision", "coding", "research"])


@pytest.mark.parametrize("mutate,msg", [
    (lambda t: t["classes"].pop("vision"), "canonical"),
    (lambda t: t["classes"]["fast"].update(fallbacks=["google/gemini-2.5-flash"]), "repeats"),
    (lambda t: t["classes"]["fast"].update(max_tokens_per_call=0), "max_tokens"),
    (lambda t: t["cost_controls"].update(soft_alert_ratio=1.2), "ratio"),
    (lambda t: t["cost_controls"].update(daily_cap_usd=0), "daily_cap"),
])
def test_invalid_routing_tables_are_rejected(mutate, msg) -> None:
    table = copy.deepcopy(TABLE)
    mutate(table)
    with pytest.raises(_mr.RoutingTableError, match=msg):
        _mr.ModelRouter(provider=_mr.MockProvider(), routing_table=table)


def test_unknown_model_class_never_defaults() -> None:
    router, _, _ = _router()
    with pytest.raises(_mr.UnknownModelClassError):
        router.complete("telepathy", MSGS, now=NOW)


def test_chains_follow_the_routing_table() -> None:
    router, _, _ = _router()
    for name, cfg in TABLE["classes"].items():
        assert router.chain_for(name) == [cfg["primary"], *cfg["fallbacks"]]
    assert router.cheapest_chain("fast")[0] == "openai/gpt-4o-mini"


# ------------------------------------------------------------------ success + determinism


def test_primary_success_returns_schema_shaped_usage_and_flags_simulation() -> None:
    router, _, _ = _router()
    res = router.complete("reasoning", MSGS, now=NOW)
    assert res["model_id"] == REASONING["primary"] and res["fallback_used"] is False
    assert res["simulated"] is True and res["model_used"] == f"mock:{REASONING['primary']}"
    assert set(res["token_usage"]) == {"input_tokens", "output_tokens"}
    assert res["token_usage"]["input_tokens"] > 0 and res["cost_usd"] > 0
    assert json.loads(res["content"])["simulated"] is True
    task = {"model_used": res["model_used"], "token_usage": res["token_usage"], "cost_usd": res["cost_usd"]}
    assert task["cost_usd"] == pytest.approx(res["spend"]["spent_usd"])


def test_mock_provider_is_deterministic() -> None:
    a = _router()[0].complete("fast", MSGS, now=NOW)
    b = _router()[0].complete("fast", MSGS, now=NOW)
    assert a["content"] == b["content"] and a["cost_usd"] == b["cost_usd"] and a["token_usage"] == b["token_usage"]


def test_max_tokens_is_clamped_to_class_limit() -> None:
    router, provider, _ = _router()
    router.complete("fast", MSGS, max_tokens=999_999, now=NOW)
    assert provider.calls[-1]["max_tokens"] == FAST["max_tokens_per_call"]
    router.complete("fast", MSGS, max_tokens=100, now=NOW)
    assert provider.calls[-1]["max_tokens"] == 100


@pytest.mark.parametrize("bad", [[], None, [{"role": "user"}], "hello"])
def test_invalid_messages_are_rejected(bad) -> None:
    with pytest.raises(ValueError):
        _router()[0].complete("fast", bad, now=NOW)


# ------------------------------------------------------------------ fallback chain


def test_503_on_primary_falls_back_and_records_attempts(tmp_path: Path) -> None:
    router, provider, _ = _router(tmp_path)
    provider.script(REASONING["primary"], _mr.ProviderCapacityError())
    res = router.complete("reasoning", MSGS, now=NOW, task_id="TASK-20261125-RTE1", agent="master-control-plane")
    assert res["model_id"] == REASONING["fallbacks"][0] and res["fallback_used"] is True
    assert [(a["model_id"], a["outcome"], a["status_code"]) for a in res["attempts"]] == [
        (REASONING["primary"], "error", 503), (REASONING["fallbacks"][0], "success", 200),
    ]
    events = _events(tmp_path)
    assert len(events) == 1 and events[0]["outcome"] == "success" and events[0]["task_id"] == "TASK-20261125-RTE1"
    assert events[0]["outputs_summary"]["fallback_used"] is True
    assert events[0]["model"] == f"mock:{REASONING['fallbacks'][0]}"
    assert events[0]["tokens"] == {"input": res["token_usage"]["input_tokens"], "output": res["token_usage"]["output_tokens"]}
    assert events[0]["cost_usd"] == res["cost_usd"]


def test_quota_then_capacity_walks_the_whole_chain() -> None:
    router, provider, _ = _router()
    provider.script(REASONING["primary"], _mr.ProviderQuotaError())
    provider.script(REASONING["fallbacks"][0], _mr.ProviderCapacityError())
    res = router.complete("reasoning", MSGS, now=NOW)
    assert res["model_id"] == REASONING["fallbacks"][1]
    assert [a["status_code"] for a in res["attempts"]] == [429, 503, 200]


def test_non_retryable_error_does_not_fall_back(tmp_path: Path) -> None:
    router, provider, _ = _router(tmp_path)
    provider.script(REASONING["primary"], _mr.ProviderRequestError())
    with pytest.raises(_mr.ProviderRequestError):
        router.complete("reasoning", MSGS, now=NOW)
    assert len(provider.calls) == 1
    assert _events(tmp_path)[0]["outcome"] == "failure"
    assert router.ledger.spent("2026-11-25") == 0


def test_retries_within_a_model_before_falling_back() -> None:
    router, provider, _ = _router(max_attempts_per_model=2)
    provider.script(REASONING["primary"], _mr.ProviderCapacityError(), None)
    res = router.complete("reasoning", MSGS, now=NOW)
    assert res["model_id"] == REASONING["primary"] and res["fallback_used"] is False
    assert [a["attempt"] for a in res["attempts"]] == [1, 2]


def test_exhausted_chain_logs_incident_referencing_cap1_once_per_day(tmp_path: Path) -> None:
    router, provider, audit = _router(tmp_path)
    for model in router.chain_for("reasoning"):
        provider.fail_always(model, _mr.ProviderCapacityError())
    with pytest.raises(_mr.ModelRoutingExhaustedError) as exc:
        router.complete("reasoning", MSGS, now=NOW)
    assert len(exc.value.attempts) == 3
    incident = exc.value.incident
    assert incident["type"] == "system_failure" and incident["severity"] == "p3"
    assert incident["related_incidents"] == ["INC-20261125-CAP1"]
    on_disk = json.loads((tmp_path / "inc" / f"{incident['incident_id']}.json").read_text(encoding="utf-8"))
    _sv.CanonicalSchemaValidator().validate("16_incident.schema.json", on_disk)
    assert on_disk["detected_by"] == "model_router"
    assert router.ledger.spent("2026-11-25") == 0  # failed calls cost nothing

    with pytest.raises(_mr.ModelRoutingExhaustedError) as again:
        router.complete("reasoning", MSGS, now=NOW + timedelta(minutes=5))
    assert again.value.incident is None  # deduplicated
    assert len(list((tmp_path / "inc").glob("INC-*.json"))) == 1
    assert [e["outcome"] for e in _events(tmp_path)] == ["failure", "failure"]


def test_recovery_after_outage() -> None:
    router, provider, _ = _router()
    for model in router.chain_for("fast"):
        provider.fail_always(model, _mr.ProviderQuotaError())
    with pytest.raises(_mr.ModelRoutingExhaustedError):
        router.complete("fast", MSGS, now=NOW)
    provider.recover(FAST["primary"])
    assert router.complete("fast", MSGS, now=NOW)["model_id"] == FAST["primary"]


def test_provider_contract_violation_is_an_error_not_a_silent_success() -> None:
    class Liar(_mr.MockProvider):
        def complete(self, *, model_id, messages, max_tokens, now):
            r = super().complete(model_id=model_id, messages=messages, max_tokens=max_tokens, now=now)
            return _mr.ProviderResponse("some/other-model", r.content, r.input_tokens, r.output_tokens, r.cost_usd, r.provider, True)

    with pytest.raises(_mr.RoutingError, match="contract violation"):
        _router(provider=Liar())[0].complete("fast", MSGS, now=NOW)


# ------------------------------------------------------------------ spend cap


def test_below_threshold_emits_no_alerts() -> None:
    router, _, _ = _router()
    router.ledger.add("2026-11-25", 7.0)
    assert router.complete("fast", MSGS, now=NOW)["alerts"] == []


def test_soft_alert_at_80_percent_fires_once_per_day(tmp_path: Path) -> None:
    sink: list[dict] = []
    router, _, _ = _router(tmp_path, alert_sink=sink.append)
    router.ledger.add("2026-11-25", 8.0)
    first = router.complete("fast", MSGS, now=NOW)
    assert [a["type"] for a in first["alerts"]] == ["spend_soft_alert"]
    alert = first["alerts"][0]
    assert alert["notify"] == "founder:abdulrahman" and alert["severity"] == "warning" and alert["ratio"] >= 0.8
    assert router.complete("fast", MSGS, now=NOW)["alerts"] == []
    assert len(sink) == 1
    assert any(e["action"] == "model_spend_soft_alert" for e in _events(tmp_path))


def test_circuit_breaker_blocks_non_critical_calls_and_alerts_founder(tmp_path: Path) -> None:
    sink: list[dict] = []
    router, provider, _ = _router(tmp_path, alert_sink=sink.append)
    router.ledger.add("2026-11-25", 10.0)
    with pytest.raises(_mr.CircuitBreakerOpenError):
        router.complete("reasoning", MSGS, now=NOW)
    with pytest.raises(_mr.CircuitBreakerOpenError):
        router.complete("creative", MSGS, now=NOW)
    assert provider.calls == []  # no provider spend while open
    assert [a["type"] for a in sink] == ["circuit_breaker_open"]  # alerted once
    assert sink[0]["severity"] == "critical"
    assert [e["outcome"] for e in _events(tmp_path) if e["action"] == "model_route"] == ["blocked", "blocked"]


def test_critical_calls_continue_on_cheapest_fast_model_when_breaker_open() -> None:
    router, provider, _ = _router()
    router.ledger.add("2026-11-25", 10.0)
    res = router.complete("reasoning", MSGS, critical=True, now=NOW)
    assert res["model_class_requested"] == "reasoning" and res["model_class_used"] == "fast"
    assert res["model_id"] == "openai/gpt-4o-mini" and res["degraded_mode"] == "circuit_breaker_critical_on_cheapest_fast"
    assert res["fallback_used"] is True
    assert res["spend"]["spent_usd"] > 10.0  # critical spend is still recorded


def test_critical_breaker_path_still_falls_back_within_fast_chain() -> None:
    router, provider, _ = _router()
    router.ledger.add("2026-11-25", 10.0)
    provider.script("openai/gpt-4o-mini", _mr.ProviderCapacityError())
    res = router.complete("fast", MSGS, critical=True, now=NOW)
    assert res["model_id"] == "google/gemini-2.5-flash"  # next cheapest after gpt-4o-mini


def test_a_call_that_reaches_the_cap_opens_the_breaker_for_the_next_one() -> None:
    router, _, _ = _router()
    router.ledger.add("2026-11-25", 9.999999)
    res = router.complete("fast", MSGS, now=NOW)
    assert res["spend"]["circuit_breaker_open"] is True
    assert {a["type"] for a in res["alerts"]} == {"circuit_breaker_open", "spend_soft_alert"}
    with pytest.raises(_mr.CircuitBreakerOpenError):
        router.complete("fast", MSGS, now=NOW)


def test_spend_resets_on_the_next_utc_day() -> None:
    router, _, _ = _router()
    router.ledger.add("2026-11-25", 10.0)
    with pytest.raises(_mr.CircuitBreakerOpenError):
        router.complete("fast", MSGS, now=NOW)
    tomorrow = router.complete("fast", MSGS, now=datetime(2026, 11, 26, 0, 0, 1, tzinfo=timezone.utc))
    assert tomorrow["spend"]["day"] == "2026-11-26" and tomorrow["alerts"] == []


def test_spend_ledger_persists_when_given_a_path(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    router, _, _ = _router(ledger=_mr.SpendLedger(path))
    router.complete("fast", MSGS, now=NOW)
    spent = router.ledger.spent("2026-11-25")
    assert spent > 0 and _mr.SpendLedger(path).spent("2026-11-25") == pytest.approx(spent)


# ------------------------------------------------------------------ provider interface


def test_default_provider_is_mock_while_openrouter_is_unavailable() -> None:
    status = json.loads((REPO_ROOT / "08_TOOLS_MCP/integration_status.json").read_text(encoding="utf-8"))
    assert status["integrations"]["openrouter"]["status"] == "unavailable"
    router = _mr.ModelRouter()
    assert isinstance(router.provider, _mr.MockProvider) and router.provider.simulated is True


def test_verified_gateway_without_injected_provider_fails_closed() -> None:
    status = {"integrations": {"openrouter": {"status": "verified"}}}
    with pytest.raises(_mr.ProviderNotConfiguredError):
        _mr.ModelRouter(integration_status=status)

    class Real(_mr.MockProvider):
        name = "openrouter"
        simulated = False

    router = _mr.ModelRouter(integration_status=status, provider=Real())
    res = router.complete("fast", MSGS, now=NOW)
    assert res["simulated"] is False and res["model_used"] == FAST["primary"]  # no 'mock:' prefix for real providers
