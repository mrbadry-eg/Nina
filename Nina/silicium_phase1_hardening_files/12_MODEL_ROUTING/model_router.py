"""Model-routing fallback layer (SILICIUM-SPEC-001 sections 3.5 and 6).

Agents request a ``model_class`` (fast, reasoning, creative, vision, coding,
research). Only this module knows concrete provider model IDs, read from
``12_MODEL_ROUTING/routing_table.json``.

Behaviour
---------
* **Fallback chain.** Primary model first, then each fallback in order. A retryable
  provider failure (simulated ``503`` capacity, ``429`` quota) moves to the next
  model. A non-retryable failure (``400`` bad request) is raised immediately,
  because another model would not fix it and fallback would mask a bug.
* **Exhaustion.** If every model fails, a ``system_failure`` incident is logged that
  references ``INC-20261125-CAP1`` (the Phase 0 provider-capacity incident) and
  ``ModelRoutingExhaustedError`` is raised. Provider capacity is a runtime
  condition, not an architectural defect.
* **Daily spend cap.** Recorded spend is compared with ``daily_cap_usd`` before each
  call. At ``soft_alert_ratio`` (80%) a Founder alert is emitted once per day. At
  ``circuit_breaker_ratio`` (100%) non-critical calls are refused; *critical* calls
  (customer-support alerts) continue on the cheapest ``fast`` model. The cap is
  checked before a call, so a single call can overshoot by at most one call's cost
  (bounded by ``max_tokens_per_call``). The spend day is the UTC calendar day.
* **Provider interface.** ``ModelProvider`` is the only seam to a real gateway.
  While ``openrouter`` is ``unavailable`` the router uses ``MockProvider``, which is
  deterministic, credential-free and always flagged ``simulated``. If the status is
  ``verified`` but no provider was injected, construction fails closed instead of
  silently mocking.

Mock output is a placeholder. Callers must never treat it as real model reasoning.
"""

from __future__ import annotations

import json
import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")

ROUTING_TABLE_PATH = REPO_ROOT / "12_MODEL_ROUTING" / "routing_table.json"
INTEGRATION_STATUS_PATH = REPO_ROOT / "08_TOOLS_MCP" / "integration_status.json"
AGENT_TASK_SCHEMA_PATH = REPO_ROOT / "01_ARCHITECTURE" / "schemas" / "07_agent_task.schema.json"

REFERENCE_INCIDENT_ID = "INC-20261125-CAP1"
FOUNDER_PRINCIPAL = "founder:abdulrahman"
CRITICAL_FALLBACK_CLASS = "fast"


# ---------------------------------------------------------------------- errors


class RoutingError(Exception):
    """Base class for routing failures."""


class RoutingTableError(RoutingError, ValueError):
    """routing_table.json is malformed or disagrees with the canonical schema."""


class UnknownModelClassError(RoutingError, ValueError):
    """The requested model_class is not defined. The router never guesses a default."""


class ProviderNotConfiguredError(RoutingError):
    """The gateway is marked verified but no provider was injected."""


class CircuitBreakerOpenError(RoutingError):
    """Daily spend cap reached; non-critical calls are paused."""


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ProviderCapacityError(ProviderError):
    def __init__(self, message: str = "503 provider capacity unavailable") -> None:
        super().__init__(message, status_code=503, retryable=True)


class ProviderQuotaError(ProviderError):
    def __init__(self, message: str = "429 provider quota exceeded") -> None:
        super().__init__(message, status_code=429, retryable=True)


class ProviderRequestError(ProviderError):
    def __init__(self, message: str = "400 invalid request") -> None:
        super().__init__(message, status_code=400, retryable=False)


class ModelRoutingExhaustedError(RoutingError):
    def __init__(self, message: str, *, attempts: list[dict[str, Any]], incident: dict[str, Any] | None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.incident = incident


# ------------------------------------------------------------------- providers


@dataclass(frozen=True)
class ProviderResponse:
    model_id: str
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    provider: str
    simulated: bool


class ModelProvider(ABC):
    """The single seam between the router and any real model gateway."""

    name: str = "provider"
    simulated: bool = False

    @abstractmethod
    def complete(self, *, model_id: str, messages: list[dict[str, Any]], max_tokens: int, now: datetime) -> ProviderResponse:
        """Return a response or raise a ``ProviderError`` subclass."""

    @abstractmethod
    def price_per_1k_tokens(self, model_id: str) -> float:
        """Blended price in USD per 1,000 tokens, used to find the cheapest fallback."""


# SIMULATED rates for the credential-free mock. They are not real provider pricing.
SIMULATED_PRICE_PER_1K: dict[str, float] = {
    "google/gemini-2.5-flash": 0.0005,
    "anthropic/claude-3.5-haiku": 0.0008,
    "openai/gpt-4o-mini": 0.0004,
    "anthropic/claude-sonnet-4": 0.009,
    "google/gemini-2.5-pro": 0.006,
    "openai/gpt-4o": 0.008,
    "perplexity/sonar-pro": 0.007,
}
SIMULATED_DEFAULT_PRICE = 0.002
MOCK_OUTPUT_TOKENS = 64


class MockProvider(ModelProvider):
    """Deterministic local provider. Failures are scripted, never random."""

    name = "mock"
    simulated = True

    def __init__(self, *, price_table: dict[str, float] | None = None) -> None:
        self.prices = dict(SIMULATED_PRICE_PER_1K if price_table is None else price_table)
        self._scripted: dict[str, list[ProviderError | None]] = {}
        self._persistent: dict[str, ProviderError] = {}
        self.calls: list[dict[str, Any]] = []

    # -- scripting helpers (tests and evaluation only)
    def script(self, model_id: str, *outcomes: ProviderError | None) -> "MockProvider":
        """Queue per-call outcomes for a model: an error to raise, or ``None`` to succeed."""
        self._scripted.setdefault(model_id, []).extend(outcomes)
        return self

    def fail_always(self, model_id: str, error: ProviderError) -> "MockProvider":
        self._persistent[model_id] = error
        return self

    def recover(self, model_id: str) -> "MockProvider":
        self._persistent.pop(model_id, None)
        self._scripted.pop(model_id, None)
        return self

    def price_per_1k_tokens(self, model_id: str) -> float:
        return self.prices.get(model_id, SIMULATED_DEFAULT_PRICE)

    def complete(self, *, model_id: str, messages: list[dict[str, Any]], max_tokens: int, now: datetime) -> ProviderResponse:
        self.calls.append({"model_id": model_id, "max_tokens": max_tokens, "at": now.isoformat()})
        queue = self._scripted.get(model_id)
        if queue:
            outcome = queue.pop(0)
            if outcome is not None:
                raise outcome
        elif model_id in self._persistent:
            raise self._persistent[model_id]

        canonical = _sv.canonical_json(messages)
        input_tokens = max(1, math.ceil(len(canonical) / 4))
        output_tokens = min(max_tokens, MOCK_OUTPUT_TOKENS)
        digest = _sv.canonical_sha256(messages)[:16]
        content = json.dumps({"simulated": True, "model": model_id, "input_digest": digest}, sort_keys=True)
        cost = round((input_tokens + output_tokens) / 1000.0 * self.price_per_1k_tokens(model_id), 6)
        return ProviderResponse(model_id, content, input_tokens, output_tokens, cost, self.name, self.simulated)


def build_default_provider(integration_status: dict[str, Any] | None = None) -> ModelProvider:
    """Mock while ``openrouter`` is unavailable. Fail closed if it is verified but not injected."""
    status = integration_status
    if status is None:
        status = json.loads(INTEGRATION_STATUS_PATH.read_text(encoding="utf-8"))
    gateway_state = status.get("integrations", {}).get("openrouter", {}).get("status", "unavailable")
    if gateway_state == "verified":
        raise ProviderNotConfiguredError("openrouter is 'verified' but no ModelProvider was injected; refusing to fall back to the mock")
    return MockProvider()


# ---------------------------------------------------------------- spend ledger


class SpendLedger:
    """Per-day recorded spend. In memory by default; pass ``path`` to persist across processes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._days: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            self._days = json.loads(path.read_text(encoding="utf-8")).get("days", {})

    def _day(self, day: str) -> dict[str, Any]:
        return self._days.setdefault(day, {"spent_usd": 0.0, "soft_alerted": False, "breaker_alerted": False})

    def spent(self, day: str) -> float:
        return float(self._day(day)["spent_usd"])

    def add(self, day: str, usd: float) -> float:
        entry = self._day(day)
        entry["spent_usd"] = round(entry["spent_usd"] + usd, 6)
        self._save()
        return entry["spent_usd"]

    def flag(self, day: str, name: str) -> bool:
        """Set an alert flag. Returns True only the first time (so alerts fire once per day)."""
        entry = self._day(day)
        if entry[name]:
            return False
        entry[name] = True
        self._save()
        return True

    def _save(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"days": self._days}, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------- router


def _load_canonical_model_classes() -> list[str]:
    schema = json.loads(AGENT_TASK_SCHEMA_PATH.read_text(encoding="utf-8"))
    for part in schema.get("allOf", []):
        enum = part.get("properties", {}).get("model_class", {}).get("enum")
        if enum:
            return list(enum)
    raise RoutingTableError("07_agent_task.schema.json does not define a model_class enum")


class ModelRouter:
    def __init__(
        self,
        *,
        provider: ModelProvider | None = None,
        routing_table: dict[str, Any] | None = None,
        integration_status: dict[str, Any] | None = None,
        ledger: SpendLedger | None = None,
        audit_logger: Any | None = None,
        alert_sink: Callable[[dict[str, Any]], None] | None = None,
        environment: str = "dev",
        max_attempts_per_model: int = 1,
    ) -> None:
        table = routing_table if routing_table is not None else json.loads(ROUTING_TABLE_PATH.read_text(encoding="utf-8"))
        self.table = self._validate_table(table)
        self.provider = provider or build_default_provider(integration_status)
        self.ledger = ledger or SpendLedger()
        self.audit = audit_logger
        self.alert_sink = alert_sink
        self.environment = environment
        if max_attempts_per_model < 1:
            raise ValueError("max_attempts_per_model must be >= 1")
        self.max_attempts_per_model = max_attempts_per_model
        self.alerts: list[dict[str, Any]] = []
        self.incidents: list[dict[str, Any]] = []
        self._incident_keys: set[tuple[str, str]] = set()

    # -- configuration

    @staticmethod
    def _validate_table(table: dict[str, Any]) -> dict[str, Any]:
        canonical = _load_canonical_model_classes()
        classes = table.get("classes")
        if not isinstance(classes, dict) or sorted(classes) != sorted(canonical):
            raise RoutingTableError(f"routing classes must equal the canonical model_class enum {sorted(canonical)}")
        for name, cfg in classes.items():
            if not isinstance(cfg.get("primary"), str) or not cfg["primary"]:
                raise RoutingTableError(f"class '{name}' needs a primary model")
            fallbacks = cfg.get("fallbacks")
            if not isinstance(fallbacks, list) or not all(isinstance(f, str) and f for f in fallbacks):
                raise RoutingTableError(f"class '{name}' fallbacks must be a list of model IDs")
            chain = [cfg["primary"], *fallbacks]
            if len(set(chain)) != len(chain):
                raise RoutingTableError(f"class '{name}' repeats a model in its chain")
            if not isinstance(cfg.get("max_tokens_per_call"), int) or cfg["max_tokens_per_call"] <= 0:
                raise RoutingTableError(f"class '{name}' needs a positive max_tokens_per_call")
        controls = table.get("cost_controls", {})
        cap, soft, breaker = controls.get("daily_cap_usd"), controls.get("soft_alert_ratio"), controls.get("circuit_breaker_ratio")
        if not (isinstance(cap, (int, float)) and cap > 0):
            raise RoutingTableError("cost_controls.daily_cap_usd must be > 0")
        if not (isinstance(soft, (int, float)) and isinstance(breaker, (int, float)) and 0 < soft < breaker):
            raise RoutingTableError("cost_controls need 0 < soft_alert_ratio < circuit_breaker_ratio")
        return table

    @property
    def daily_cap_usd(self) -> float:
        return float(self.table["cost_controls"]["daily_cap_usd"])

    def chain_for(self, model_class: str) -> list[str]:
        cfg = self._class(model_class)
        return [cfg["primary"], *cfg["fallbacks"]]

    def cheapest_chain(self, model_class: str) -> list[str]:
        """The class chain ordered cheapest first (stable for equal prices)."""
        chain = self.chain_for(model_class)
        return sorted(chain, key=lambda m: (self.provider.price_per_1k_tokens(m), chain.index(m)))

    def _class(self, model_class: str) -> dict[str, Any]:
        if model_class not in self.table["classes"]:
            raise UnknownModelClassError(f"Unknown model_class '{model_class}'")
        return self.table["classes"][model_class]

    # -- spend state

    @staticmethod
    def _day(now: datetime) -> str:
        return now.astimezone(timezone.utc).strftime("%Y-%m-%d")

    def spend_state(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        day = self._day(current)
        spent = self.ledger.spent(day)
        ratio = spent / self.daily_cap_usd
        controls = self.table["cost_controls"]
        return {
            "day": day,
            "spent_usd": round(spent, 6),
            "cap_usd": self.daily_cap_usd,
            "ratio": round(ratio, 6),
            "soft_alert": ratio >= controls["soft_alert_ratio"],
            "circuit_breaker_open": ratio >= controls["circuit_breaker_ratio"],
        }

    # -- completion

    def complete(
        self,
        model_class: str,
        messages: list[dict[str, Any]],
        *,
        task_id: str | None = None,
        agent: str | None = None,
        critical: bool = False,
        max_tokens: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) and "role" in m and "content" in m for m in messages):
            raise ValueError("messages must be a non-empty list of {'role', 'content'} objects")

        cfg = self._class(model_class)
        state = self.spend_state(current)
        effective_class = model_class
        chain = self.chain_for(model_class)
        degraded: str | None = None

        if state["circuit_breaker_open"]:
            if not critical:
                self._log(
                    "model_route", "blocked", "medium", model_class, task_id, agent, started,
                    inputs={"model_class": model_class, "critical": False},
                    outputs={"reason": "circuit_breaker_open", "spend": state},
                    error="Daily spend cap reached; non-critical model calls are paused",
                )
                self._emit_alert(state, "circuit_breaker_open", current)
                raise CircuitBreakerOpenError(
                    f"Daily spend cap reached ({state['spent_usd']} of {state['cap_usd']} USD); non-critical calls are paused"
                )
            effective_class = CRITICAL_FALLBACK_CLASS
            cfg = self._class(effective_class)
            chain = self.cheapest_chain(effective_class)
            degraded = "circuit_breaker_critical_on_cheapest_fast"

        limit = cfg["max_tokens_per_call"]
        call_max = min(max_tokens, limit) if max_tokens else limit

        attempts: list[dict[str, Any]] = []
        for model_id in chain:
            for attempt_no in range(1, self.max_attempts_per_model + 1):
                try:
                    resp = self.provider.complete(model_id=model_id, messages=messages, max_tokens=call_max, now=current)
                except ProviderError as exc:
                    attempts.append({"model_id": model_id, "attempt": attempt_no, "outcome": "error", "status_code": exc.status_code, "error": str(exc), "retryable": exc.retryable})
                    if not exc.retryable:
                        self._log(
                            "model_route", "failure", "medium", model_class, task_id, agent, started,
                            inputs={"model_class": model_class}, outputs={"attempts": attempts}, error=str(exc),
                        )
                        raise
                    continue
                self._check_response(resp, model_id)
                attempts.append({"model_id": model_id, "attempt": attempt_no, "outcome": "success", "status_code": 200, "error": None, "retryable": False})
                return self._success(resp, model_class, effective_class, chain, attempts, degraded, critical, current, task_id, agent, started)

        incident = self._open_incident(model_class, chain, attempts, current)
        self._log(
            "model_route", "failure", "high", model_class, task_id, agent, started,
            inputs={"model_class": model_class, "critical": critical},
            outputs={"attempts": attempts, "incident_id": incident["incident_id"] if incident else None},
            error="All models in the fallback chain failed",
        )
        raise ModelRoutingExhaustedError(
            f"All models failed for class '{model_class}': " + ", ".join(f"{a['model_id']}={a['status_code']}" for a in attempts),
            attempts=attempts, incident=incident,
        )

    # -- internals

    @staticmethod
    def _check_response(resp: ProviderResponse, requested: str) -> None:
        ok = (
            resp.model_id == requested
            and isinstance(resp.input_tokens, int) and resp.input_tokens >= 0
            and isinstance(resp.output_tokens, int) and resp.output_tokens >= 0
            and isinstance(resp.cost_usd, (int, float)) and resp.cost_usd >= 0
        )
        if not ok:
            raise RoutingError(f"Provider contract violation for model '{requested}'")

    def _success(
        self, resp: ProviderResponse, requested_class: str, effective_class: str, chain: list[str],
        attempts: list[dict[str, Any]], degraded: str | None, critical: bool, current: datetime,
        task_id: str | None, agent: str | None, started: float,
    ) -> dict[str, Any]:
        day = self._day(current)
        self.ledger.add(day, float(resp.cost_usd))
        state = self.spend_state(current)
        alerts: list[dict[str, Any]] = []
        if state["circuit_breaker_open"]:
            alert = self._emit_alert(state, "circuit_breaker_open", current)
            if alert:
                alerts.append(alert)
        if state["soft_alert"]:
            alert = self._emit_alert(state, "spend_soft_alert", current)
            if alert:
                alerts.append(alert)

        fallback_used = resp.model_id != chain[0] or degraded is not None
        model_used = f"{resp.provider}:{resp.model_id}" if resp.simulated else resp.model_id
        result = {
            "model_class_requested": requested_class,
            "model_class_used": effective_class,
            "model_id": resp.model_id,
            "model_used": model_used,
            "provider": resp.provider,
            "simulated": resp.simulated,
            "fallback_used": fallback_used,
            "degraded_mode": degraded,
            "critical": critical,
            "attempts": attempts,
            "content": resp.content,
            "token_usage": {"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
            "cost_usd": float(resp.cost_usd),
            "spend": state,
            "alerts": alerts,
        }
        self._log(
            "model_route", "success", "low", requested_class, task_id, agent, started,
            inputs={"model_class": requested_class, "critical": critical},
            outputs={
                "model_used": model_used, "fallback_used": fallback_used, "degraded_mode": degraded,
                "attempts": attempts, "spend": state,
            },
            model=model_used,
            tokens={"input": resp.input_tokens, "output": resp.output_tokens},  # audit schema uses input/output
            cost_usd=result["cost_usd"],
        )
        return result

    def _emit_alert(self, state: dict[str, Any], kind: str, current: datetime) -> dict[str, Any] | None:
        flag = "soft_alerted" if kind == "spend_soft_alert" else "breaker_alerted"
        if not self.ledger.flag(state["day"], flag):
            return None
        pct = int(round(state["ratio"] * 100))
        alert = {
            "type": kind,
            "severity": "warning" if kind == "spend_soft_alert" else "critical",
            "notify": FOUNDER_PRINCIPAL,
            "day": state["day"],
            "spent_usd": state["spent_usd"],
            "cap_usd": state["cap_usd"],
            "ratio": state["ratio"],
            "message": (
                f"Model spend is at {pct}% of the daily cap ({state['spent_usd']} of {state['cap_usd']} USD)."
                if kind == "spend_soft_alert"
                else f"Daily model spend cap reached ({state['spent_usd']} of {state['cap_usd']} USD). Non-critical calls are paused; critical calls use the cheapest fast model."
            ),
            "raised_at": current.isoformat(),
        }
        self.alerts.append(alert)
        if self.alert_sink is not None:
            self.alert_sink(alert)
        self._log(
            f"model_{kind}", "success" if kind == "spend_soft_alert" else "blocked",
            "medium" if kind == "spend_soft_alert" else "high", "spend", None, None, time.perf_counter(),
            inputs={"day": state["day"]}, outputs=alert,
        )
        return alert

    def _open_incident(self, model_class: str, chain: list[str], attempts: list[dict[str, Any]], current: datetime) -> dict[str, Any] | None:
        key = (self._day(current), model_class)
        if key in self._incident_keys:  # one incident per class per day
            return None
        self._incident_keys.add(key)
        statuses = ", ".join(f"{a['model_id']}={a['status_code']}" for a in attempts)
        incident: dict[str, Any] = {
            "incident_id": None,
            "description": (
                f"Model routing exhausted for class '{model_class}' ({statuses}). Runtime provider capacity/quota condition, "
                f"not an architecture defect; same failure class as {REFERENCE_INCIDENT_ID}."
            ),
        }
        if self.audit is not None:
            incident = self.audit.log_incident(
                incident_type="system_failure",
                severity="p3",
                description=incident["description"],
                affected_resources=[f"runtime:model-gateway:{model_class}"],
                detected_by="model_router",
                related_incidents=[REFERENCE_INCIDENT_ID],
            )
        else:
            incident["related_incidents"] = [REFERENCE_INCIDENT_ID]
        self.incidents.append(incident)
        return incident

    def _log(
        self, action: str, outcome: str, risk: str, resource: str, task_id: str | None, agent: str | None, started: float,
        *, inputs: dict[str, Any], outputs: dict[str, Any], error: str | None = None,
        model: str | None = None, tokens: dict[str, int] | None = None, cost_usd: float | None = None,
    ) -> None:
        if self.audit is None:
            return
        self.audit.log_audit(
            environment=self.environment,
            task_id=task_id,
            agent=agent or "model_router",
            action=action,
            tool="model_router",
            domain="routing",
            resource=resource,
            inputs=inputs,
            outputs=outputs,
            risk_level=risk,
            autonomy_level=0,
            outcome=outcome,
            error=error,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model=model,
            tokens=tokens,
            cost_usd=cost_usd,
        )
