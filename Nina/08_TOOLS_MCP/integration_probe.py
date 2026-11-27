"""Read-only, credential-conscious probes for local and hosted integrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import socket
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

Integration = Literal["openrouter", "n8n", "openclaw", "telegram"]
MAX_RESPONSE_BYTES = 1_048_576
DEFAULT_TIMEOUT_SECONDS = 5.0


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        response: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        return None


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library GET transport; it never sends request bodies."""

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        request = Request(url, headers=dict(headers or {}), method="GET")
        opener = build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read(MAX_RESPONSE_BYTES + 1))
        except HTTPError as error:
            try:
                return HttpResponse(error.code, error.read(MAX_RESPONSE_BYTES + 1))
            finally:
                error.close()


@dataclass(frozen=True)
class ProbeResult:
    timestamp: str
    integration: str
    status: Literal["passed", "failed"]
    status_code: int | None
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _result(
    integration: str,
    status: Literal["passed", "failed"],
    status_code: int | None,
    detail: str,
) -> ProbeResult:
    return ProbeResult(_timestamp(), integration, status, status_code, detail)


def _request(
    integration: str,
    transport: HttpTransport,
    url: str,
    headers: Mapping[str, str] | None = None,
) -> tuple[HttpResponse | None, ProbeResult | None]:
    try:
        response = transport.get(url, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
    except (TimeoutError, socket.timeout):
        return None, _result(integration, "failed", None, "request timed out")
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            detail = "request timed out"
        else:
            detail = "request failed"
        return None, _result(integration, "failed", None, detail)
    except (OSError, ValueError):
        return None, _result(integration, "failed", None, "request failed")

    if not isinstance(response, HttpResponse):
        return None, _result(integration, "failed", None, "transport returned a malformed response")
    if len(response.body) > MAX_RESPONSE_BYTES:
        return None, _result(
            integration,
            "failed",
            response.status_code,
            "response exceeded the allowed size",
        )
    return response, None


def _http_failure(integration: str, response: HttpResponse) -> ProbeResult:
    if response.status_code in (401, 403):
        detail = "authentication failed"
    else:
        detail = f"HTTP request failed with status {response.status_code}"
    return _result(integration, "failed", response.status_code, detail)


def _json_body(response: HttpResponse) -> tuple[object | None, bool]:
    try:
        return json.loads(response.body.decode("utf-8")), True
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, False


def _openrouter(
    transport: HttpTransport,
    env: Mapping[str, str],
) -> ProbeResult:
    api_key = env.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return _result("openrouter", "failed", None, "OPENROUTER_API_KEY is not configured")

    base_url = env.get("SILICIUM_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    response, failure = _request(
        "openrouter",
        transport,
        f"{base_url}/key",
        {"Authorization": f"Bearer {api_key}"},
    )
    if failure:
        return failure
    assert response is not None
    if response.status_code != 200:
        return _http_failure("openrouter", response)

    body, valid_json = _json_body(response)
    if not valid_json or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return _result("openrouter", "failed", response.status_code, "malformed key metadata response")
    data = body["data"]
    rate_limit = data.get("rate_limit")
    if (
        not isinstance(rate_limit, dict)
        or isinstance(rate_limit.get("requests"), bool)
        or not isinstance(rate_limit.get("requests"), int)
        or rate_limit["requests"] < 1
        or not isinstance(rate_limit.get("interval"), str)
        or not re.fullmatch(r"[1-9][0-9]*(?:s|m|h|d)", rate_limit["interval"])
    ):
        return _result("openrouter", "failed", response.status_code, "malformed enforced rate-limit metadata")
    if "is_free_tier" in data and not isinstance(data["is_free_tier"], bool):
        return _result("openrouter", "failed", response.status_code, "malformed key tier metadata")

    limits: dict[str, object] = {
        "rate_limit": {
            "requests": rate_limit["requests"],
            "interval": rate_limit["interval"],
        }
    }
    if "is_free_tier" in data:
        limits["is_free_tier"] = data["is_free_tier"]
    for field in ("limit", "limit_remaining", "usage_daily"):
        value = data.get(field)
        if value is None:
            if field in data:
                limits[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return _result("openrouter", "failed", response.status_code, "malformed key limit metadata")
        limits[field] = value
    rendered_limits = json.dumps(limits, sort_keys=True, separators=(",", ":"))
    return _result("openrouter", "passed", response.status_code, f"key valid; enforced limits: {rendered_limits}")


def _n8n_health_url(env: Mapping[str, str]) -> str:
    """Use n8n's /healthz default and honor its configured health path overrides."""
    base_url = env.get("N8N_BASE_URL", "http://localhost:5678").rstrip("/")
    custom_endpoint = env.get("N8N_ENDPOINT_HEALTH")
    path_prefix = env.get("N8N_PATH", "")
    endpoint = custom_endpoint if custom_endpoint is not None else f"{path_prefix.rstrip('/')}/healthz"
    return f"{base_url}/{endpoint.lstrip('/')}"


def _n8n(transport: HttpTransport, env: Mapping[str, str]) -> ProbeResult:
    response, failure = _request("n8n", transport, _n8n_health_url(env))
    if failure:
        return failure
    assert response is not None
    if response.status_code != 200:
        return _http_failure("n8n", response)
    body, valid_json = _json_body(response)
    if not valid_json or not isinstance(body, dict) or body.get("status") != "ok":
        return _result("n8n", "failed", response.status_code, "malformed or unhealthy health response")
    return _result("n8n", "passed", response.status_code, "health endpoint reported status ok")


def _openclaw(transport: HttpTransport, env: Mapping[str, str]) -> ProbeResult:
    base_url = env.get("OPENCLAW_BASE_URL", "http://localhost:59048").rstrip("/")
    token = env.get("OPENCLAW_GATEWAY_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response, failure = _request("openclaw", transport, f"{base_url}/", headers)
    if failure:
        return failure
    assert response is not None
    if not 200 <= response.status_code < 300:
        return _http_failure("openclaw", response)
    return _result("openclaw", "passed", response.status_code, "gateway is reachable")


def _telegram(transport: HttpTransport, env: Mapping[str, str]) -> ProbeResult:
    token = env.get("TELEGRAM_APPROVAL_BOT_TOKEN", "")
    if not token:
        return _result("telegram", "failed", None, "TELEGRAM_APPROVAL_BOT_TOKEN is not configured")

    base_url = f"https://api.telegram.org/bot{token}"
    last_status_code: int | None = None
    for method in ("getMe", "getWebhookInfo"):
        response, failure = _request("telegram", transport, f"{base_url}/{method}")
        if failure:
            return failure
        assert response is not None
        last_status_code = response.status_code
        if response.status_code != 200:
            return _http_failure("telegram", response)
        body, valid_json = _json_body(response)
        if (
            not valid_json
            or not isinstance(body, dict)
            or body.get("ok") is not True
            or not isinstance(body.get("result"), dict)
        ):
            return _result("telegram", "failed", response.status_code, f"malformed {method} response")
    return _result(
        "telegram",
        "passed",
        last_status_code,
        "approval bot getMe and getWebhookInfo succeeded",
    )


def probe_integration(
    integration: Integration,
    transport: HttpTransport,
    env: Mapping[str, str] | None = None,
) -> ProbeResult:
    """Run exactly one integration's read-only probe; never persist response bodies."""
    variables = env if env is not None else {}
    probes = {
        "openrouter": _openrouter,
        "n8n": _n8n,
        "openclaw": _openclaw,
        "telegram": _telegram,
    }
    probe = probes.get(integration)
    if probe is None:
        raise ValueError(f"unknown integration: {integration}")
    return probe(transport, variables)


def write_evidence(result: ProbeResult, directory: str | Path) -> Path:
    """Persist only the probe summary; credentials and upstream bodies are never included."""
    evidence_directory = Path(directory)
    evidence_directory.mkdir(parents=True, exist_ok=True)
    filename = (
        f"{result.integration}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        f"-{uuid4().hex[:8]}.json"
    )
    evidence_path = evidence_directory / filename
    with evidence_path.open("x", encoding="utf-8", newline="\n") as evidence_file:
        json.dump(result.to_dict(), evidence_file, indent=2, sort_keys=True)
        evidence_file.write("\n")
    return evidence_path
