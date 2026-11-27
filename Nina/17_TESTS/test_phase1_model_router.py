"""Phase 1 / Layer 3: model-routing fallback layer, spend cap and provider interface."""

from __future__ import annotations

import copy
import json
import os
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
VERIFIED_OPENROUTER = {"integrations": {"openrouter": {"status": "verified"}}}


def _router(tmp_path: Path | None = None, provider=None, **kw):
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc") if tmp_path else None
    provider = provider or _mr.MockProvider()
    table = kw.pop("routing_table", None)
    if table is None:
        table = copy.deepcopy(TABLE)
        for metadata in table["models"].values():
            if metadata["endpoint_tier"] == "free":
                metadata["endpoint_tier"] = "paid"
                metadata["logs_prompts"] = False
    kw.setdefault("request_budget", _mr.RequestBudget())
    return _mr.ModelRouter(provider=provider, routing_table=table, audit_logger=audit, **kw), provider, audit


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
    (lambda t: t["classes"]["fast"].update(fallbacks=["nvidia/nemotron-3-ultra-550b-a55b:free"]), "repeats"),
    (lambda t: t["classes"]["fast"].update(max_tokens_per_call=0), "max_tokens"),
    (lambda t: t["models"].pop("nvidia/nemotron-3-ultra-550b-a55b:free"), "missing endpoint metadata"),
    (lambda t: t["models"]["nvidia/nemotron-3-ultra-550b-a55b:free"].update(logs_prompts=False), "marked as logging"),
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
    assert router.cheapest_chain("fast")[0] == TABLE["classes"]["fast"]["primary"]


def test_hosted_route_mapping_uses_only_founder_approved_free_slugs() -> None:
    ultra = "nvidia/nemotron-3-ultra-550b-a55b:free"
    super_vl = "nvidia/nemotron-3.5-super-vl:free"
    assert set(TABLE["models"]) == {ultra, super_vl}
    for model_class in ("reasoning", "research", "coding", "fast", "creative"):
        assert TABLE["classes"][model_class]["primary"] == ultra
        assert TABLE["classes"][model_class]["fallbacks"] == [super_vl]
    assert TABLE["classes"]["vision"]["primary"] == super_vl
    assert TABLE["classes"]["vision"]["fallbacks"] == []


def test_free_nvidia_models_are_marked_as_logged_free_endpoints() -> None:
    for model_id in ("nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3.5-super-vl:free"):
        assert TABLE["models"][model_id] == {"endpoint_tier": "free", "logs_prompts": True}


def _policy_table(chain: list[str], metadata: dict[str, dict]) -> dict:
    table = copy.deepcopy(TABLE)
    table["classes"]["fast"]["primary"] = chain[0]
    table["classes"]["fast"]["fallbacks"] = chain[1:]
    table["models"].update(metadata)
    return table


FREE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
PAID_MODEL = "test/paid-no-log"
LOCAL_MODEL = "test/local-private"
POLICY_MODELS = {
    FREE_MODEL: {"endpoint_tier": "free", "logs_prompts": True},
    PAID_MODEL: {"endpoint_tier": "paid", "logs_prompts": False},
    LOCAL_MODEL: {"endpoint_tier": "local", "logs_prompts": False},
}


@pytest.mark.parametrize("data_class", ["internal", "restricted"])
def test_non_public_data_is_blocked_before_free_provider_and_audited(data_class: str, tmp_path: Path) -> None:
    table = _policy_table([FREE_MODEL], POLICY_MODELS)
    provider = _mr.MockProvider()
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc")
    router = _mr.ModelRouter(provider=provider, routing_table=table, audit_logger=audit)

    with pytest.raises(_mr.DataPolicyViolationError):
        if data_class == "internal":
            router.complete("fast", MSGS, now=NOW)
        else:
            router.complete("fast", MSGS, data_class=data_class, now=NOW)

    events = _events(tmp_path)
    assert provider.calls == []
    assert len(events) == 1 and events[0]["action"] == "model_route" and events[0]["outcome"] == "blocked"
    assert events[0]["inputs_summary"]["data_class"] == data_class
    _sv.CanonicalSchemaValidator().validate("17_audit_event.schema.json", events[0])


def test_public_data_is_allowed_on_free_endpoint() -> None:
    provider = _mr.MockProvider()
    router = _mr.ModelRouter(provider=provider, routing_table=_policy_table([FREE_MODEL], POLICY_MODELS))

    result = router.complete("fast", MSGS, data_class="public", now=NOW)

    assert result["model_id"] == FREE_MODEL
    assert [call["model_id"] for call in provider.calls] == [FREE_MODEL]


def test_vision_never_sends_restricted_customer_data_to_hosted_provider() -> None:
    provider = _mr.MockProvider()
    router = _mr.ModelRouter(
        provider=provider,
        routing_table=TABLE,
        request_budget=_mr.RequestBudget(),
    )

    with pytest.raises(_mr.DataPolicyViolationError):
        router.complete("vision", MSGS, data_class="restricted", now=NOW)

    assert provider.calls == []


def test_internal_fallback_never_weakens_privacy() -> None:
    provider = _mr.MockProvider().script(PAID_MODEL, _mr.ProviderCapacityError())
    table = _policy_table([PAID_MODEL, FREE_MODEL, LOCAL_MODEL], POLICY_MODELS)
    router = _mr.ModelRouter(provider=provider, routing_table=table)

    result = router.complete("fast", MSGS, data_class="internal", now=NOW)

    assert result["model_id"] == LOCAL_MODEL
    assert [call["model_id"] for call in provider.calls] == [PAID_MODEL, LOCAL_MODEL]


def test_restricted_data_uses_local_endpoint_only() -> None:
    provider = _mr.MockProvider()
    table = _policy_table([FREE_MODEL, PAID_MODEL, LOCAL_MODEL], POLICY_MODELS)
    router = _mr.ModelRouter(provider=provider, routing_table=table)

    result = router.complete("fast", MSGS, data_class="restricted", now=NOW)

    assert result["model_id"] == LOCAL_MODEL
    assert [call["model_id"] for call in provider.calls] == [LOCAL_MODEL]


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


def test_quota_error_stops_the_whole_chain() -> None:
    router, provider, _ = _router()
    provider.script(REASONING["primary"], _mr.ProviderQuotaError())
    provider.script(REASONING["fallbacks"][0], _mr.ProviderCapacityError())
    with pytest.raises(_mr.ProviderQuotaError):
        router.complete("reasoning", MSGS, now=NOW)
    assert [call["model_id"] for call in provider.calls] == [REASONING["primary"]]


def test_non_retryable_error_does_not_fall_back(tmp_path: Path) -> None:
    router, provider, _ = _router(tmp_path)
    provider.script(REASONING["primary"], _mr.ProviderRequestError())
    with pytest.raises(_mr.ProviderRequestError):
        router.complete("reasoning", MSGS, now=NOW)
    assert len(provider.calls) == 1
    assert _events(tmp_path)[0]["outcome"] == "failure"
    assert router.ledger.spent("2026-11-25") == 0


def test_retry_loops_are_rejected() -> None:
    with pytest.raises(ValueError, match="fixed at 1"):
        _router(max_attempts_per_model=2)


def test_exhausted_chain_logs_incident_referencing_cap1_once_per_day(tmp_path: Path) -> None:
    router, provider, audit = _router(tmp_path)
    for model in router.chain_for("reasoning"):
        provider.fail_always(model, _mr.ProviderCapacityError())
    with pytest.raises(_mr.ModelRoutingExhaustedError) as exc:
        router.complete("reasoning", MSGS, now=NOW)
    assert len(exc.value.attempts) == len(router.chain_for("reasoning"))
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
        provider.fail_always(model, _mr.ProviderCapacityError())
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
    assert res["model_id"] == TABLE["classes"]["fast"]["primary"] and res["degraded_mode"] == "circuit_breaker_critical_on_cheapest_fast"
    assert res["fallback_used"] is True
    assert res["spend"]["spent_usd"] > 10.0  # critical spend is still recorded


def test_critical_breaker_path_still_falls_back_within_fast_chain() -> None:
    router, provider, _ = _router()
    router.ledger.add("2026-11-25", 10.0)
    provider.script(TABLE["classes"]["fast"]["primary"], _mr.ProviderCapacityError())
    res = router.complete("fast", MSGS, critical=True, now=NOW)
    assert res["model_id"] == TABLE["classes"]["fast"]["fallbacks"][0]


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
    router = _mr.ModelRouter(request_budget=_mr.RequestBudget())
    assert isinstance(router.provider, _mr.MockProvider) and router.provider.simulated is True
    assert router.complete("fast", MSGS, data_class="public", now=NOW)["simulated"] is True


def test_verified_gateway_without_injected_provider_fails_closed() -> None:
    status = {"integrations": {"openrouter": {"status": "verified"}}}
    with pytest.raises(_mr.ProviderNotConfiguredError):
        _mr.ModelRouter(integration_status=status, request_budget=_mr.RequestBudget())

    class Real(_mr.MockProvider):
        name = "openrouter"
        simulated = False

    router = _mr.ModelRouter(integration_status=status, provider=Real(), request_budget=_mr.RequestBudget())
    res = router.complete("fast", MSGS, data_class="public", now=NOW)
    assert res["simulated"] is False and res["model_used"] == FAST["primary"]  # no 'mock:' prefix for real providers


class FakeHttpTransport:
    def __init__(self, *responses: _mr.HttpResponse) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, *, headers, body=None, timeout=30.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def _http_json(status: int, payload: dict) -> _mr.HttpResponse:
    return _mr.HttpResponse(status, json.dumps(payload).encode("utf-8"))


def _openai_success(model_id: str = "nvidia/nemotron-3-ultra-550b-a55b:free", content: str = "ok") -> _mr.HttpResponse:
    return _http_json(200, {
        "model": model_id,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    })


@pytest.mark.parametrize(("status", "error_type", "retryable"), [
    (429, _mr.ProviderQuotaError, True),
    (500, _mr.ProviderCapacityError, True),
    (503, _mr.ProviderCapacityError, True),
    (400, _mr.ProviderRequestError, False),
    (401, _mr.ProviderConfigurationError, False),
    (402, _mr.ProviderConfigurationError, False),
    (403, _mr.ProviderConfigurationError, False),
])
def test_openai_compatible_provider_maps_http_statuses(monkeypatch, status, error_type, retryable) -> None:
    secret = "provider-error-body-secret"
    transport = FakeHttpTransport(_mr.HttpResponse(status, f"upstream detail {secret}".encode()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)

    with pytest.raises(error_type) as exc:
        provider.complete(model_id=TABLE["classes"]["reasoning"]["primary"], messages=MSGS, max_tokens=8, now=NOW)

    assert exc.value.retryable is retryable
    assert exc.value.status_code == status
    assert secret not in str(exc.value)
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer environment-only-test-key"


def test_openrouter_rejects_explicit_api_key_parameter(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", api_key="must-not-be-used")


def test_openai_compatible_provider_parses_real_response_without_simulation_or_response_format(monkeypatch) -> None:
    key_metadata = _http_json(200, {"data": {"requests_per_day": 42}})
    transport = FakeHttpTransport(key_metadata, _openai_success())
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)

    limits = provider.get_request_limits()
    result = provider.complete(
        model_id=TABLE["classes"]["reasoning"]["primary"], messages=MSGS, max_tokens=128, now=NOW
    )

    request = json.loads(transport.calls[1]["body"].decode("utf-8"))
    assert limits == {"daily_requests": 42}
    assert result.simulated is False and result.model_id == TABLE["classes"]["reasoning"]["primary"]
    assert result.provider == "openrouter" and result.cost_usd == 0
    assert request == {
        "model": TABLE["classes"]["reasoning"]["primary"],
        "messages": MSGS,
        "max_tokens": 128,
    }
    assert transport.calls[0]["url"] == "https://openrouter.ai/api/v1/key"
    assert transport.calls[1]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert "response_format" not in request


@pytest.mark.parametrize(("metadata", "expected"), [
    ({}, 50),
    ({"rate_limit": {"requests": 73, "interval": "1d"}}, 73),
])
def test_openrouter_key_metadata_daily_limit_defaults_or_uses_daily_rate(monkeypatch, metadata, expected) -> None:
    transport = FakeHttpTransport(_http_json(200, {"data": metadata}))
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)

    assert provider.get_request_limits() == {"daily_requests": expected}


def test_openai_compatible_provider_rejects_mismatched_response_model(monkeypatch) -> None:
    transport = FakeHttpTransport(_openai_success(model_id="some/other-model"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)

    with pytest.raises(_mr.ProviderResponseError, match="expected contract"):
        provider.complete(
            model_id=TABLE["classes"]["reasoning"]["primary"], messages=MSGS, max_tokens=8, now=NOW
        )


def test_openrouter_request_budget_uses_key_metadata_and_stops_before_exhausted_call(monkeypatch, tmp_path: Path) -> None:
    transport = FakeHttpTransport(
        _http_json(200, {"data": {"requests_per_day": 2}}),
        _openai_success(),
        _openai_success(),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)
    budget = _mr.RequestBudget(tmp_path / "request-budget.sqlite3")
    router = _mr.ModelRouter(
        provider=provider, routing_table=TABLE, request_budget=budget,
        integration_status=VERIFIED_OPENROUTER,
    )

    router.complete("reasoning", MSGS, data_class="public", now=NOW)
    router.complete("reasoning", MSGS, data_class="public", critical=True, now=NOW)
    with pytest.raises(_mr.RequestBudgetExhaustedError):
        router.complete("reasoning", MSGS, data_class="public", critical=True, now=NOW)

    assert len(transport.calls) == 3  # one metadata read and two completions; no third POST
    assert router.request_state(NOW)["requests"] == 2
    assert router.request_state(NOW)["daily_limit"] == 2


def test_429_stops_fallback_without_retrying_or_leaking_response(monkeypatch, tmp_path: Path) -> None:
    marker = "quota-body-secret"
    transport = FakeHttpTransport(
        _http_json(200, {"data": {"requests_per_day": 50}}),
        _mr.HttpResponse(429, f"quota error {marker}".encode()),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)
    router = _mr.ModelRouter(
        provider=provider,
        routing_table=TABLE,
        request_budget=_mr.RequestBudget(tmp_path / "request-budget.sqlite3"),
        integration_status=VERIFIED_OPENROUTER,
    )

    with pytest.raises(_mr.ProviderQuotaError) as exc:
        router.complete("reasoning", MSGS, data_class="public", now=NOW)

    assert str(exc.value) == "429 provider quota exceeded"
    assert len(transport.calls) == 2
    assert transport.calls[-1]["url"].endswith("/chat/completions")
    assert router.request_state(NOW)["requests"] == 1


@pytest.mark.parametrize("status", [401, 402, 403])
def test_configuration_errors_open_incident_and_never_fall_back(monkeypatch, tmp_path: Path, status: int) -> None:
    marker = "configuration-body-secret"
    transport = FakeHttpTransport(
        _http_json(200, {"data": {"requests_per_day": 50}}),
        _mr.HttpResponse(status, f"invalid credentials {marker}".encode()),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=transport)
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "incidents")
    router = _mr.ModelRouter(
        provider=provider, routing_table=TABLE,
        request_budget=_mr.RequestBudget(tmp_path / "request-budget.sqlite3"),
        audit_logger=audit,
        integration_status=VERIFIED_OPENROUTER,
    )

    with pytest.raises(_mr.ProviderConfigurationError):
        router.complete("reasoning", MSGS, data_class="public", now=NOW)

    assert len(transport.calls) == 2
    incident_files = list((tmp_path / "incidents").glob("INC-*.json"))
    assert len(incident_files) == 1
    evidence = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert marker not in evidence and "environment-only-test-key" not in evidence


def test_critical_request_reserve_alerts_at_80_percent_and_breaks_at_100_percent(tmp_path: Path) -> None:
    sink: list[dict] = []
    budget = _mr.RequestBudget(tmp_path / "request-budget.sqlite3", daily_limit=5)
    router = _mr.ModelRouter(
        provider=_mr.MockProvider(),
        routing_table=TABLE,
        request_budget=budget,
        alert_sink=sink.append,
    )

    for _ in range(3):
        router.complete("fast", MSGS, data_class="public", now=NOW)
    soft_alert_call = router.complete("fast", MSGS, data_class="public", now=NOW)
    with pytest.raises(_mr.RequestBudgetReserveError):
        router.complete("fast", MSGS, data_class="public", now=NOW)
    critical = router.complete("fast", MSGS, data_class="public", critical=True, now=NOW)
    with pytest.raises(_mr.RequestBudgetExhaustedError):
        router.complete("fast", MSGS, data_class="public", critical=True, now=NOW)

    assert [alert["type"] for alert in sink] == ["request_soft_alert", "request_circuit_breaker_open"]
    assert [alert["type"] for alert in soft_alert_call["alerts"]] == ["request_soft_alert"]
    assert [alert["type"] for alert in critical["alerts"]] == ["request_circuit_breaker_open"]
    assert critical["request_budget"]["requests"] == 5
    assert critical["request_budget"]["circuit_breaker_open"] is True
    assert [alert["severity"] for alert in sink] == ["warning", "critical"]


def test_request_budget_enforces_rolling_minute_limit_and_utc_day(tmp_path: Path) -> None:
    budget = _mr.RequestBudget(tmp_path / "request-budget.sqlite3", daily_limit=50, requests_per_minute=20)

    for _ in range(20):
        budget.reserve(NOW, critical=False)
    with pytest.raises(_mr.RequestBudgetRateLimitError):
        budget.reserve(NOW, critical=False)
    after_window = budget.reserve(NOW + timedelta(seconds=61), critical=False)

    assert after_window["requests"] == 21
    assert after_window["requests_last_minute"] == 1
    assert budget.state(datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc))["requests"] == 0


def test_request_budget_persists_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "request-budget.sqlite3"
    first = _mr.RequestBudget(path, daily_limit=5)
    first.reserve(NOW, critical=True)

    restarted = _mr.RequestBudget(path)

    assert restarted.state(NOW)["requests"] == 1
    assert restarted.state(NOW)["critical_requests"] == 1
    assert restarted.state(NOW)["daily_limit"] == 5


def test_default_request_reserve_protects_ten_of_fifty_calls(tmp_path: Path) -> None:
    budget = _mr.RequestBudget(tmp_path / "request-budget.sqlite3", requests_per_minute=100)

    for _ in range(40):
        budget.reserve(NOW, critical=False)
    with pytest.raises(_mr.RequestBudgetReserveError):
        budget.reserve(NOW, critical=False)
    for _ in range(10):
        budget.reserve(NOW, critical=True)

    assert budget.state(NOW)["requests"] == 50
    assert budget.state(NOW)["critical_requests"] == 10
    assert budget.state(NOW)["circuit_breaker_open"] is True


def test_invalid_structured_output_is_rejected_in_code() -> None:
    router, _, _ = _router()

    with pytest.raises(_mr.ProviderResponseError, match="JSON schema"):
        router.complete(
            "fast",
            MSGS,
            response_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
            now=NOW,
        )


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("SILICIUM_LIVE") != "1", reason="set SILICIUM_LIVE=1 to opt into the OpenRouter smoke test")
def test_live_openrouter_smoke() -> None:
    status = json.loads((REPO_ROOT / "08_TOOLS_MCP/integration_status.json").read_text(encoding="utf-8"))
    if status["integrations"]["openrouter"]["status"] != "verified":
        pytest.skip("OpenRouter integration has not been verified")
    provider = _mr.OpenAICompatibleProvider(_mr.OpenAICompatibleProvider.DEFAULT_OPENROUTER_URL)
    response = provider.complete(
        model_id="nvidia/nemotron-3-ultra-550b-a55b:free",
        messages=[{"role": "user", "content": "Reply with OK."}],
        max_tokens=8,
        now=datetime.now(timezone.utc),
    )
    assert response.simulated is False and response.model_id == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_unverified_openrouter_provider_is_not_invoked_by_router(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-only-test-key")
    provider = _mr.OpenAICompatibleProvider("https://openrouter.ai/api/v1", transport=FakeHttpTransport())

    with pytest.raises(_mr.ProviderNotConfiguredError, match="not verified"):
        _mr.ModelRouter(provider=provider, routing_table=TABLE, request_budget=_mr.RequestBudget())
