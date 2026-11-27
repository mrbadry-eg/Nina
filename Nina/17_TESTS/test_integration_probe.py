"""Read-only integration probe tests use injected transports only."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pytest

from silicium_loader import load_module

_probe = load_module("08_TOOLS_MCP/integration_probe.py", "silicium_integration_probe")


class FakeTransport:
    def __init__(self, *responses: _probe.HttpResponse | BaseException) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 5.0,
    ) -> _probe.HttpResponse:
        self.calls.append((url, dict(headers or {}), timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _json_response(payload: object, status_code: int = 200) -> _probe.HttpResponse:
    return _probe.HttpResponse(status_code, json.dumps(payload).encode("utf-8"))


def test_openrouter_reads_key_limits_without_completion() -> None:
    secret = "openrouter-test-secret"
    transport = FakeTransport(
        _json_response(
            {
                "data": {
                    "is_free_tier": True,
                    "rate_limit": {"requests": 20, "interval": "10s"},
                    "limit": 0,
                    "limit_remaining": 0,
                    "usage_daily": 2.5,
                }
            }
        )
    )

    result = _probe.probe_integration(
        "openrouter",
        transport,
        {"OPENROUTER_API_KEY": secret},
    )

    assert result.status == "passed"
    assert "20" in result.detail and "10s" in result.detail
    assert len(transport.calls) == 1
    url, headers, _ = transport.calls[0]
    assert url == "https://openrouter.ai/api/v1/key"
    assert headers == {"Authorization": f"Bearer {secret}"}


def test_n8n_uses_healthz_and_accepts_configured_endpoint_path() -> None:
    default_transport = FakeTransport(_json_response({"status": "ok"}))
    default_result = _probe.probe_integration("n8n", default_transport, {})
    assert default_result.status == "passed"
    assert default_transport.calls[0][0] == "http://localhost:5678/healthz"

    path_transport = FakeTransport(_json_response({"status": "ok"}))
    _probe.probe_integration(
        "n8n",
        path_transport,
        {
            "N8N_BASE_URL": "http://localhost:5678",
            "N8N_PATH": "/automations/",
        },
    )
    assert path_transport.calls[0][0] == "http://localhost:5678/automations/healthz"

    custom_transport = FakeTransport(_json_response({"status": "ok"}))
    _probe.probe_integration(
        "n8n",
        custom_transport,
        {
            "N8N_BASE_URL": "http://localhost:5678",
            "N8N_PATH": "/automations/",
            "N8N_ENDPOINT_HEALTH": "health",
        },
    )
    assert custom_transport.calls[0][0] == "http://localhost:5678/health"


def test_openclaw_sends_configured_gateway_token_only_as_auth_header() -> None:
    secret = "openclaw-test-secret"
    transport = FakeTransport(_probe.HttpResponse(200, b"<html>gateway</html>"))

    result = _probe.probe_integration(
        "openclaw",
        transport,
        {"OPENCLAW_GATEWAY_TOKEN": secret},
    )

    assert result.status == "passed"
    assert transport.calls[0][0] == "http://localhost:59048/"
    assert transport.calls[0][1] == {"Authorization": f"Bearer {secret}"}


def test_telegram_checks_only_the_approval_bot_read_endpoints() -> None:
    token = "123456789:approval-test-token"
    transport = FakeTransport(
        _json_response({"ok": True, "result": {"id": 123, "is_bot": True}}),
        _json_response({"ok": True, "result": {"url": "", "pending_update_count": 0}}),
    )

    result = _probe.probe_integration(
        "telegram",
        transport,
        {"TELEGRAM_APPROVAL_BOT_TOKEN": token},
    )

    assert result.status == "passed"
    assert [call[0] for call in transport.calls] == [
        f"https://api.telegram.org/bot{token}/getMe",
        f"https://api.telegram.org/bot{token}/getWebhookInfo",
    ]
    assert all(call[1] == {} for call in transport.calls)


def test_http_401_is_failed_without_persisting_response_body() -> None:
    secret = "openrouter-401-secret"
    transport = FakeTransport(_probe.HttpResponse(401, secret.encode("utf-8")))

    result = _probe.probe_integration(
        "openrouter",
        transport,
        {"OPENROUTER_API_KEY": secret},
    )

    assert result.status == "failed"
    assert result.status_code == 401
    assert secret not in result.detail


def test_openrouter_does_not_copy_untrusted_limit_text_into_evidence(tmp_path: Path) -> None:
    secret = "probe-limit-secret-sentinel"
    transport = FakeTransport(
        _json_response(
            {
                "data": {
                    "rate_limit": {"requests": 20, "interval": secret},
                }
            }
        )
    )

    result = _probe.probe_integration(
        "openrouter",
        transport,
        {"OPENROUTER_API_KEY": "test-key"},
    )
    evidence_path = _probe.write_evidence(result, tmp_path)

    assert result.status == "failed"
    assert secret not in result.detail
    assert secret not in evidence_path.read_text(encoding="utf-8")


def test_timeout_is_reported_without_echoing_exception_text() -> None:
    secret = "telegram-timeout-secret"
    transport = FakeTransport(TimeoutError(secret))

    result = _probe.probe_integration(
        "telegram",
        transport,
        {"TELEGRAM_APPROVAL_BOT_TOKEN": secret},
    )

    assert result.status == "failed"
    assert result.status_code is None
    assert "timed out" in result.detail
    assert secret not in result.detail


@pytest.mark.parametrize(
    "integration, responses, env",
    [
        ("openrouter", [_probe.HttpResponse(200, b"not-json")], {"OPENROUTER_API_KEY": "key"}),
        ("n8n", [_probe.HttpResponse(200, b"not-json")], {}),
        (
            "telegram",
            [_probe.HttpResponse(200, b"not-json")],
            {"TELEGRAM_APPROVAL_BOT_TOKEN": "bot-token"},
        ),
    ],
)
def test_malformed_success_body_fails_closed(
    integration: _probe.Integration,
    responses: list[_probe.HttpResponse],
    env: dict[str, str],
) -> None:
    result = _probe.probe_integration(integration, FakeTransport(*responses), env)
    assert result.status == "failed"
    assert result.status_code == 200
    assert "malformed" in result.detail


def test_evidence_contains_redacted_result_and_no_configured_secret(tmp_path: Path) -> None:
    secret = "sk-or-evidence-secret"
    transport = FakeTransport(
        _probe.HttpResponse(401, f"invalid key {secret}".encode("utf-8"))
    )
    result = _probe.probe_integration(
        "openrouter",
        transport,
        {"OPENROUTER_API_KEY": secret},
    )

    evidence_path = _probe.write_evidence(result, tmp_path)
    evidence = evidence_path.read_text(encoding="utf-8")
    parsed = json.loads(evidence)

    assert parsed["timestamp"]
    assert parsed["integration"] == "openrouter"
    assert parsed["status_code"] == 401
    assert parsed["status"] == "failed"
    assert parsed["detail"]
    assert secret not in evidence


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("SILICIUM_LIVE") != "1",
    reason="set SILICIUM_LIVE=1 to probe the local n8n health endpoint",
)
def test_live_n8n_health_probe(tmp_path: Path) -> None:
    result = _probe.probe_integration(
        "n8n",
        _probe.UrllibTransport(),
        {"N8N_BASE_URL": "http://localhost:5678"},
    )
    assert result.status == "passed", result.detail
    _probe.write_evidence(result, tmp_path)
