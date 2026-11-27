"""Model-routing fallback layer (SILICIUM-SPEC-001 sections 3.5 and 6).

Agents request a ``model_class`` (fast, reasoning, creative, vision, coding,
research). Only this module knows concrete provider model IDs, read from
``12_MODEL_ROUTING/routing_table.json``.

Behaviour
---------
* **Fallback chain.** Primary model first, then each fallback in order. Capacity
  failures can move to the next model; a ``429`` stops the entire chain because
  hosted free models share one account quota. ``400`` and provider-configuration
  errors are non-retryable and never fall back.
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
* **Request budget.** Hosted free-tier calls persist request counts in SQLite under
  ``SILICIUM_DATA_DIR`` (or the user-local state directory), enforce 20 requests per
  rolling minute and the key's daily limit (50 by default), and reserve 20% for
  critical calls.

Mock output is a placeholder. Callers must never treat it as real model reasoning.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import threading
from collections.abc import Mapping
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")

ROUTING_TABLE_PATH = REPO_ROOT / "12_MODEL_ROUTING" / "routing_table.json"
INTEGRATION_STATUS_PATH = REPO_ROOT / "08_TOOLS_MCP" / "integration_status.json"
AGENT_TASK_SCHEMA_PATH = REPO_ROOT / "01_ARCHITECTURE" / "schemas" / "07_agent_task.schema.json"

REFERENCE_INCIDENT_ID = "INC-20261125-CAP1"
FOUNDER_PRINCIPAL = "founder:abdulrahman"
CRITICAL_FALLBACK_CLASS = "fast"
DATA_CLASS_RANK = {"public": 0, "internal": 1, "restricted": 2}


# ---------------------------------------------------------------------- errors


class RoutingError(Exception):
    """Base class for routing failures."""


class RoutingTableError(RoutingError, ValueError):
    """routing_table.json is malformed or disagrees with the canonical schema."""


class UnknownModelClassError(RoutingError, ValueError):
    """The requested model_class is not defined. The router never guesses a default."""


class DataPolicyViolationError(RoutingError):
    """No endpoint in the routing chain is permitted to receive this data class."""


class ProviderNotConfiguredError(RoutingError):
    """The gateway is marked verified but no provider was injected."""


class CircuitBreakerOpenError(RoutingError):
    """Daily spend cap reached; non-critical calls are paused."""


class RequestBudgetError(RoutingError):
    """A request-count quota prevents this model call."""

    def __init__(self, message: str, *, state: dict[str, Any]) -> None:
        super().__init__(message)
        self.state = state


class RequestBudgetReserveError(RequestBudgetError):
    """Non-critical requests cannot consume the reserved critical-call quota."""


class RequestBudgetRateLimitError(RequestBudgetError):
    """The rolling per-minute request limit has been reached."""


class RequestBudgetExhaustedError(RequestBudgetError):
    """The daily request quota has been exhausted."""


class ProviderError(Exception):
    def __init__(self, message: str, *, status_code: int, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ProviderCapacityError(ProviderError):
    def __init__(self, message: str = "503 provider capacity unavailable", *, status_code: int = 503) -> None:
        super().__init__(message, status_code=status_code, retryable=True)


class ProviderQuotaError(ProviderError):
    def __init__(self, message: str = "429 provider quota exceeded") -> None:
        super().__init__(message, status_code=429, retryable=True)


class ProviderRequestError(ProviderError):
    def __init__(self, message: str = "400 invalid request", *, status_code: int = 400) -> None:
        super().__init__(message, status_code=status_code, retryable=False)


class ProviderConfigurationError(ProviderError):
    """Provider credentials or account configuration are invalid; fallback is unsafe."""

    def __init__(self, message: str = "provider configuration rejected", *, status_code: int) -> None:
        super().__init__(message, status_code=status_code, retryable=False)


class ProviderResponseError(ProviderError):
    """The upstream returned a response that violated the OpenAI-compatible contract."""

    def __init__(self, message: str = "provider returned an invalid response") -> None:
        super().__init__(message, status_code=502, retryable=False)


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


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: object, code: int, message: str, headers: object, new_url: str):
        return None


class UrllibHttpTransport:
    """Small standard-library HTTP transport with redirects disabled."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        opener = build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read(1_048_577))
        except HTTPError as error:
            try:
                return HttpResponse(error.code, error.read(1_048_577))
            finally:
                error.close()


class OpenAICompatibleProvider(ModelProvider):
    """OpenAI-style chat client for OpenRouter and later local model servers."""

    simulated = False
    MAX_RESPONSE_BYTES = 1_048_576
    DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        transport: Any | None = None,
        timeout: float = 30.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("base_url must be an HTTP(S) URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query or fragment")
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibHttpTransport()
        self.timeout = timeout
        self._is_openrouter = parsed.hostname == "openrouter.ai" or bool(parsed.hostname and parsed.hostname.endswith(".openrouter.ai"))
        self.name = "openrouter" if self._is_openrouter else "openai-compatible"
        if self._is_openrouter and api_key is not None:
            raise ValueError("OpenRouter credentials must be read from OPENROUTER_API_KEY")
        self.api_key = os.environ.get("OPENROUTER_API_KEY") if self._is_openrouter else api_key
        self._key_limits: dict[str, int] | None = None

    def price_per_1k_tokens(self, model_id: str) -> float:
        return 0.0

    def get_request_limits(self) -> dict[str, int]:
        """Fetch and cache OpenRouter key metadata without exposing its response body."""
        if not self._is_openrouter:
            return {}
        if self._key_limits is not None:
            return dict(self._key_limits)
        response = self._request("GET", f"{self.base_url}/key", body=None)
        self._raise_for_status(response.status_code)
        if len(response.body) > self.MAX_RESPONSE_BYTES:
            raise ProviderResponseError("provider key metadata exceeded the allowed size")
        try:
            payload = json.loads(response.body.decode("utf-8"))
            data = payload["data"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            raise ProviderResponseError("provider key metadata violated the expected contract") from None
        if not isinstance(data, dict):
            raise ProviderResponseError("provider key metadata violated the expected contract")
        daily_limit = 50
        has_daily_limit = False
        for field in ("requests_per_day", "daily_request_limit", "daily_requests"):
            value = data.get(field)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ProviderResponseError("provider daily request limit was invalid")
                daily_limit = value
                has_daily_limit = True
                break
        rate_limit = data.get("rate_limit")
        if not has_daily_limit and isinstance(rate_limit, dict) and rate_limit.get("interval") == "1d":
            value = rate_limit.get("requests")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ProviderResponseError("provider daily request limit was invalid")
            daily_limit = value
        self._key_limits = {"daily_requests": daily_limit}
        return dict(self._key_limits)

    def complete(
        self, *, model_id: str, messages: list[dict[str, Any]], max_tokens: int, now: datetime
    ) -> ProviderResponse:
        body = json.dumps(
            {"model": model_id, "messages": messages, "max_tokens": max_tokens},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._request("POST", f"{self.base_url}/chat/completions", body=body)
        self._raise_for_status(response.status_code)
        if len(response.body) > self.MAX_RESPONSE_BYTES:
            raise ProviderResponseError("provider response exceeded the allowed size")
        try:
            payload = json.loads(response.body.decode("utf-8"))
            reported_model = payload["model"]
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            usage = payload["usage"]
            input_tokens = usage["prompt_tokens"]
            output_tokens = usage["completion_tokens"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            raise ProviderResponseError("provider response violated the expected contract") from None
        if (
            reported_model != model_id
            or not isinstance(content, str)
            or isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0
            or isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0
        ):
            raise ProviderResponseError("provider response violated the expected contract")
        return ProviderResponse(model_id, content, input_tokens, output_tokens, 0.0, self.name, False)

    def _request(self, method: str, url: str, *, body: bytes | None) -> HttpResponse:
        if self._is_openrouter and not self.api_key:
            raise ProviderConfigurationError(
                "OPENROUTER_API_KEY is not configured", status_code=401
            )
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.transport.request(
                method, url, headers=headers, body=body, timeout=self.timeout
            )
        except (TimeoutError, OSError, URLError):
            raise ProviderCapacityError("provider request failed", status_code=503) from None
        if not isinstance(response, HttpResponse):
            raise ProviderResponseError("provider transport returned an invalid response")
        if len(response.body) > self.MAX_RESPONSE_BYTES:
            raise ProviderResponseError("provider response exceeded the allowed size")
        return response

    @staticmethod
    def _raise_for_status(status_code: int) -> None:
        if status_code == 429:
            raise ProviderQuotaError()
        if status_code in {401, 402, 403}:
            raise ProviderConfigurationError(
                "provider authentication or account configuration was rejected",
                status_code=status_code,
            )
        if status_code == 400:
            raise ProviderRequestError()
        if 500 <= status_code <= 599:
            raise ProviderCapacityError(status_code=status_code)
        if not 200 <= status_code < 300:
            raise ProviderRequestError(
                f"provider rejected the request with HTTP {status_code}", status_code=status_code
            )


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


def default_request_budget_path() -> Path:
    data_dir = os.environ.get("SILICIUM_DATA_DIR")
    if data_dir:
        root = Path(data_dir).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Silicium"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "silicium"
    return root / "request_budget.sqlite3"


class RequestBudget:
    """Durable account-wide free-tier quota with UTC daily and rolling-minute limits."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        daily_limit: int = 50,
        requests_per_minute: int = 20,
        critical_reserve_ratio: float = 0.2,
    ) -> None:
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 1:
            raise ValueError("daily_limit must be a positive integer")
        if isinstance(requests_per_minute, bool) or not isinstance(requests_per_minute, int) or requests_per_minute < 1:
            raise ValueError("requests_per_minute must be a positive integer")
        if not 0 < critical_reserve_ratio < 1:
            raise ValueError("critical_reserve_ratio must be between 0 and 1")
        self.path = Path(path) if path is not None else None
        self.daily_limit = daily_limit
        self.requests_per_minute = requests_per_minute
        self.critical_reserve_ratio = critical_reserve_ratio
        self._lock = threading.RLock()
        self._memory_state: dict[str, Any] = {
            "daily_limit": daily_limit,
            "days": {},
            "minute_timestamps": [],
        }
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS request_budget_state ("
                    "id INTEGER PRIMARY KEY CHECK (id = 1), state_json TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO request_budget_state (id, state_json) VALUES (1, ?)",
                    (json.dumps(self._memory_state),),
                )
                connection.commit()

    def set_daily_limit(self, daily_limit: int) -> None:
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 1:
            raise ValueError("daily request limit must be a positive integer")
        self.daily_limit = daily_limit

        def update(state: dict[str, Any]) -> None:
            state["daily_limit"] = daily_limit

        self._update(update)

    def reserve(self, now: datetime, *, critical: bool = False) -> dict[str, Any]:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        current = now.astimezone(timezone.utc)
        day = current.strftime("%Y-%m-%d")
        now_epoch = current.timestamp()
        alerts: list[dict[str, Any]] = []
        failure: RequestBudgetError | None = None

        def reserve_in_state(state: dict[str, Any]) -> None:
            nonlocal failure
            limit = state.get("daily_limit", self.daily_limit)
            days = state.setdefault("days", {})
            entry = days.setdefault(day, {"requests": 0, "critical_requests": 0, "soft_alerted": False, "breaker_alerted": False})
            recent = [
                stamp for stamp in state.setdefault("minute_timestamps", [])
                if now_epoch - float(stamp) < 60
            ]
            state["minute_timestamps"] = recent
            request_count = int(entry["requests"])
            result_state = self._state(day, entry, limit, len(recent), self.requests_per_minute)
            if request_count >= limit:
                failure = RequestBudgetExhaustedError("Daily model request quota is exhausted", state=result_state)
                return
            reserve_count = math.ceil(limit * self.critical_reserve_ratio)
            noncritical_limit = limit - reserve_count
            if not critical and request_count >= noncritical_limit:
                failure = RequestBudgetReserveError(
                    "The remaining daily model requests are reserved for critical calls",
                    state=result_state,
                )
                return
            if len(recent) >= self.requests_per_minute:
                failure = RequestBudgetRateLimitError(
                    "Rolling per-minute model request limit is exhausted",
                    state=result_state,
                )
                return
            entry["requests"] += 1
            if critical:
                entry["critical_requests"] += 1
            state["minute_timestamps"].append(now_epoch)
            result_state = self._state(
                day, entry, limit, len(state["minute_timestamps"]), self.requests_per_minute
            )
            if result_state["ratio"] >= 0.8 and not entry["soft_alerted"]:
                entry["soft_alerted"] = True
                alerts.append(self._alert(result_state, "request_soft_alert", current))
            if result_state["circuit_breaker_open"] and not entry["breaker_alerted"]:
                entry["breaker_alerted"] = True
                alerts.append(self._alert(result_state, "request_circuit_breaker_open", current))

        state = self._update(reserve_in_state)
        if failure is not None:
            raise failure
        return {
            **self._state(
                day, state["days"][day], state["daily_limit"], len(state["minute_timestamps"]),
                self.requests_per_minute,
            ),
            "alerts": alerts,
        }

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        day = current.strftime("%Y-%m-%d")
        snapshot = self._read()
        entry = snapshot.setdefault("days", {}).get(
            day, {"requests": 0, "critical_requests": 0, "soft_alerted": False, "breaker_alerted": False}
        )
        recent = [stamp for stamp in snapshot.get("minute_timestamps", []) if current.timestamp() - float(stamp) < 60]
        return self._state(
            day, entry, snapshot.get("daily_limit", self.daily_limit), len(recent), self.requests_per_minute
        )

    @staticmethod
    def _state(day: str, entry: dict[str, Any], limit: int, minute_count: int, minute_limit: int) -> dict[str, Any]:
        count = int(entry["requests"])
        return {
            "day": day,
            "requests": count,
            "critical_requests": int(entry["critical_requests"]),
            "daily_limit": int(limit),
            "ratio": round(count / limit, 6),
            "requests_last_minute": minute_count,
            "requests_per_minute_limit": minute_limit,
            "circuit_breaker_open": count >= limit,
        }

    @staticmethod
    def _alert(state: dict[str, Any], kind: str, current: datetime) -> dict[str, Any]:
        pct = int(round(state["ratio"] * 100))
        return {
            "type": kind,
            "severity": "warning" if kind == "request_soft_alert" else "critical",
            "notify": FOUNDER_PRINCIPAL,
            "day": state["day"],
            "requests": state["requests"],
            "daily_limit": state["daily_limit"],
            "ratio": state["ratio"],
            "message": f"Free-tier model requests are at {pct}% of the daily request quota ({state['requests']} of {state['daily_limit']}).",
            "raised_at": current.isoformat(),
        }

    def _read(self) -> dict[str, Any]:
        with self._lock:
            if self.path is None:
                return json.loads(json.dumps(self._memory_state))
            with closing(sqlite3.connect(self.path, timeout=30)) as connection:
                connection.execute("PRAGMA busy_timeout = 30000")
                row = connection.execute("SELECT state_json FROM request_budget_state WHERE id = 1").fetchone()
            return json.loads(row[0]) if row else json.loads(json.dumps(self._memory_state))

    def _update(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            if self.path is None:
                state = self._memory_state
                callback(state)
                return json.loads(json.dumps(state))
            with closing(sqlite3.connect(self.path, timeout=30)) as connection:
                connection.execute("PRAGMA busy_timeout = 30000")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT state_json FROM request_budget_state WHERE id = 1").fetchone()
                state = json.loads(row[0]) if row else json.loads(json.dumps(self._memory_state))
                callback(state)
                connection.execute(
                    "INSERT INTO request_budget_state (id, state_json) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
                    (json.dumps(state, sort_keys=True),),
                )
                connection.commit()
                return state


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
        request_budget: RequestBudget | None = None,
        audit_logger: Any | None = None,
        alert_sink: Callable[[dict[str, Any]], None] | None = None,
        environment: str = "dev",
        max_attempts_per_model: int = 1,
    ) -> None:
        table = routing_table if routing_table is not None else json.loads(ROUTING_TABLE_PATH.read_text(encoding="utf-8"))
        self.table = self._validate_table(table)
        self.provider = provider or build_default_provider(integration_status)
        if not self.provider.simulated and self.provider.name == "openrouter":
            status = integration_status
            if status is None:
                status = json.loads(INTEGRATION_STATUS_PATH.read_text(encoding="utf-8"))
            gateway_state = status.get("integrations", {}).get("openrouter", {}).get("status", "unavailable")
            if gateway_state != "verified":
                raise ProviderNotConfiguredError(
                    "openrouter is not verified; refusing to invoke the live provider"
                )
        self.ledger = ledger or SpendLedger()
        self.request_budget = request_budget or RequestBudget(default_request_budget_path())
        self.audit = audit_logger
        self.alert_sink = alert_sink
        self.environment = environment
        if max_attempts_per_model != 1:
            raise ValueError("max_attempts_per_model is fixed at 1; provider requests are never retried")
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
        models = table.get("models")
        if not isinstance(models, dict) or not models:
            raise RoutingTableError("routing table needs per-model endpoint metadata")
        referenced = {model_id for cfg in classes.values() for model_id in [cfg["primary"], *cfg["fallbacks"]]}
        if not referenced.issubset(models):
            raise RoutingTableError(f"routing table is missing endpoint metadata for {sorted(referenced - set(models))}")
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not model_id or not isinstance(metadata, dict):
                raise RoutingTableError("model endpoint metadata must be keyed by non-empty model IDs")
            if set(metadata) != {"endpoint_tier", "logs_prompts"}:
                raise RoutingTableError(f"model '{model_id}' metadata must contain only endpoint_tier and logs_prompts")
            if not isinstance(metadata["endpoint_tier"], str) or metadata["endpoint_tier"] not in {"free", "paid", "local"}:
                raise RoutingTableError(f"model '{model_id}' has an invalid endpoint_tier")
            if not isinstance(metadata["logs_prompts"], bool):
                raise RoutingTableError(f"model '{model_id}' logs_prompts must be a boolean")
            if metadata["endpoint_tier"] == "free" and metadata["logs_prompts"] is not True:
                raise RoutingTableError(f"free model '{model_id}' must be marked as logging prompts")
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

    def _privacy_rank(self, model_id: str) -> int:
        metadata = self.table["models"][model_id]
        if metadata["endpoint_tier"] == "free" or metadata["logs_prompts"]:
            return DATA_CLASS_RANK["public"]
        if metadata["endpoint_tier"] == "paid":
            return DATA_CLASS_RANK["internal"]
        return DATA_CLASS_RANK["restricted"]

    def _policy_safe_chain(self, chain: list[str], data_class: str) -> list[str]:
        required_rank = DATA_CLASS_RANK[data_class]
        safe_chain: list[str] = []
        last_privacy_rank = -1
        for model_id in chain:
            privacy_rank = self._privacy_rank(model_id)
            if privacy_rank < required_rank or privacy_rank < last_privacy_rank:
                continue
            safe_chain.append(model_id)
            last_privacy_rank = privacy_rank
        return safe_chain

    def _block_unsafe_chain(
        self, model_class: str, effective_class: str, chain: list[str], data_class: str,
        task_id: str | None, agent: str | None, started: float,
    ) -> None:
        metadata = {model_id: self.table["models"][model_id] for model_id in chain}
        self._log(
            "model_route", "blocked", "high", model_class, task_id, agent, started,
            inputs={"model_class": model_class, "data_class": data_class},
            outputs={"reason": "data_class_exceeds_endpoint_policy", "endpoint_metadata": metadata},
            error=f"No endpoint in the {effective_class} chain may receive {data_class} data",
        )
        raise DataPolicyViolationError(
            f"No endpoint in the {effective_class} chain may receive {data_class} data"
        )

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

    def request_state(self, now: datetime | None = None) -> dict[str, Any]:
        return self.request_budget.state(now)

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
        data_class: str = "internal",
        response_schema: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) and "role" in m and "content" in m for m in messages):
            raise ValueError("messages must be a non-empty list of {'role', 'content'} objects")

        cfg = self._class(model_class)
        if not isinstance(data_class, str) or data_class not in DATA_CLASS_RANK:
            self._log(
                "model_route", "blocked", "high", model_class, task_id, agent, started,
                inputs={"model_class": model_class, "data_class": data_class if isinstance(data_class, str) else type(data_class).__name__},
                outputs={"reason": "invalid_data_class"},
                error="Unknown model-call data class",
            )
            raise DataPolicyViolationError("Unknown model-call data class; expected public, internal, or restricted")
        effective_class = model_class
        chain = self.chain_for(model_class)
        safe_chain = self._policy_safe_chain(chain, data_class)
        if not safe_chain:
            self._block_unsafe_chain(model_class, effective_class, chain, data_class, task_id, agent, started)
        state = self.spend_state(current)
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
            safe_chain = self._policy_safe_chain(chain, data_class)
            if not safe_chain:
                self._block_unsafe_chain(model_class, effective_class, chain, data_class, task_id, agent, started)
            degraded = "circuit_breaker_critical_on_cheapest_fast"

        limit = cfg["max_tokens_per_call"]
        call_max = min(max_tokens, limit) if max_tokens else limit

        attempts: list[dict[str, Any]] = []
        request_alerts: list[dict[str, Any]] = []
        for model_id in safe_chain:
            if self.table["models"][model_id]["endpoint_tier"] == "free":
                get_limits = getattr(self.provider, "get_request_limits", None)
                if callable(get_limits):
                    try:
                        limits = get_limits()
                    except ProviderError as exc:
                        attempts.append({"model_id": model_id, "attempt": 1, "outcome": "error", "status_code": exc.status_code, "error": str(exc), "retryable": exc.retryable})
                        incident = None
                        if isinstance(exc, ProviderConfigurationError):
                            incident = self._open_configuration_incident(model_class, exc, current)
                        self._log(
                            "model_route", "failure", "critical" if incident else "high", model_class, task_id, agent, started,
                            inputs={"model_class": model_class, "data_class": data_class},
                            outputs={"attempts": attempts, "fallback_stopped": True, "incident_id": incident.get("incident_id") if incident else None},
                            error=str(exc),
                        )
                        raise
                    if not isinstance(limits, dict):
                        raise ProviderResponseError("provider request-limit metadata violated the expected contract")
                    daily_limit = limits.get("daily_requests")
                    if daily_limit is not None:
                        self.request_budget.set_daily_limit(daily_limit)
                try:
                    budget = self.request_budget.reserve(current, critical=critical)
                except RequestBudgetError as exc:
                    self._emit_request_alerts(getattr(exc, "alerts", []), current)
                    self._log(
                        "model_route", "blocked", "high" if isinstance(exc, RequestBudgetExhaustedError) else "medium",
                        model_class, task_id, agent, started,
                        inputs={"model_class": model_class, "data_class": data_class, "critical": critical},
                        outputs={"reason": type(exc).__name__, "request_budget": exc.state},
                        error=str(exc),
                    )
                    raise
                request_alerts.extend(budget["alerts"])
                self._emit_request_alerts(budget["alerts"], current)
            try:
                resp = self.provider.complete(model_id=model_id, messages=messages, max_tokens=call_max, now=current)
            except ProviderError as exc:
                attempts.append({"model_id": model_id, "attempt": 1, "outcome": "error", "status_code": exc.status_code, "error": str(exc), "retryable": exc.retryable})
                if isinstance(exc, ProviderConfigurationError):
                    incident = self._open_configuration_incident(model_class, exc, current)
                    self._log(
                        "model_route", "failure", "critical", model_class, task_id, agent, started,
                        inputs={"model_class": model_class, "data_class": data_class},
                        outputs={"attempts": attempts, "incident_id": incident.get("incident_id")},
                        error=str(exc),
                    )
                    raise
                if isinstance(exc, ProviderQuotaError):
                    self._log(
                        "model_route", "failure", "high", model_class, task_id, agent, started,
                        inputs={"model_class": model_class, "data_class": data_class},
                        outputs={"attempts": attempts, "fallback_stopped": True},
                        error=str(exc),
                    )
                    raise
                if not exc.retryable:
                    self._log(
                        "model_route", "failure", "medium", model_class, task_id, agent, started,
                        inputs={"model_class": model_class, "data_class": data_class}, outputs={"attempts": attempts}, error=str(exc),
                    )
                    raise
                continue
            self._check_response(resp, model_id)
            if response_schema is not None:
                try:
                    self._validate_structured_output(resp.content, response_schema)
                except ProviderResponseError as exc:
                    attempts.append({"model_id": model_id, "attempt": 1, "outcome": "error", "status_code": exc.status_code, "error": str(exc), "retryable": False})
                    self._log(
                        "model_route", "failure", "medium", model_class, task_id, agent, started,
                        inputs={"model_class": model_class, "data_class": data_class},
                        outputs={"attempts": attempts, "structured_output_valid": False},
                        error=str(exc),
                    )
                    raise
            attempts.append({"model_id": model_id, "attempt": 1, "outcome": "success", "status_code": 200, "error": None, "retryable": False})
            return self._success(
                resp, model_class, effective_class, chain, attempts, degraded, critical, data_class, current,
                task_id, agent, started, request_alerts,
            )

        incident = self._open_incident(model_class, chain, attempts, current)
        self._log(
            "model_route", "failure", "high", model_class, task_id, agent, started,
            inputs={"model_class": model_class, "data_class": data_class, "critical": critical},
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
            and isinstance(resp.input_tokens, int) and not isinstance(resp.input_tokens, bool) and resp.input_tokens >= 0
            and isinstance(resp.output_tokens, int) and not isinstance(resp.output_tokens, bool) and resp.output_tokens >= 0
            and isinstance(resp.cost_usd, (int, float)) and resp.cost_usd >= 0
            and isinstance(resp.content, str)
            and isinstance(resp.simulated, bool)
            and isinstance(resp.provider, str)
        )
        if not ok:
            raise RoutingError(f"Provider contract violation for model '{requested}'")

    @staticmethod
    def _validate_structured_output(content: str, schema: dict[str, Any]) -> None:
        try:
            Draft202012Validator.check_schema(schema)
            value = json.loads(content)
            Draft202012Validator(schema).validate(value)
        except (json.JSONDecodeError, SchemaError, ValidationError, TypeError):
            raise ProviderResponseError("provider output did not match the requested JSON schema") from None

    def _emit_request_alerts(self, alerts: list[dict[str, Any]], current: datetime) -> None:
        for alert in alerts:
            self.alerts.append(alert)
            if self.alert_sink is not None:
                self.alert_sink(alert)
            self._log(
                f"model_{alert['type']}", "blocked" if alert["severity"] == "critical" else "success",
                "high" if alert["severity"] == "critical" else "medium", "request_budget", None, None, time.perf_counter(),
                inputs={"day": alert["day"]}, outputs=alert,
            )

    def _open_configuration_incident(
        self, model_class: str, error: ProviderConfigurationError, current: datetime
    ) -> dict[str, Any]:
        key = (self._day(current), f"provider_configuration:{model_class}")
        if key in self._incident_keys:
            return {"incident_id": None}
        self._incident_keys.add(key)
        incident: dict[str, Any] = {
            "incident_id": None,
            "description": f"OpenRouter provider configuration was rejected with HTTP {error.status_code}; routing stopped without fallback.",
        }
        if self.audit is not None:
            incident = self.audit.log_incident(
                incident_type="system_failure",
                severity="p2",
                description=incident["description"],
                affected_resources=[f"runtime:model-gateway:{model_class}"],
                detected_by="model_router",
                related_incidents=[],
            )
        self.incidents.append(incident)
        return incident

    def _success(
        self, resp: ProviderResponse, requested_class: str, effective_class: str, chain: list[str],
        attempts: list[dict[str, Any]], degraded: str | None, critical: bool, data_class: str, current: datetime,
        task_id: str | None, agent: str | None, started: float, request_alerts: list[dict[str, Any]],
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
            "request_budget": self.request_budget.state(current),
            "alerts": [*request_alerts, *alerts],
        }
        self._log(
            "model_route", "success", "low", requested_class, task_id, agent, started,
            inputs={"model_class": requested_class, "data_class": data_class, "critical": critical},
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
