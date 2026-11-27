"""Phase 1 / Layer 4: approval channel adapters (Telegram payload format, CLI, decision handling)."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silicium_loader import load_module

_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")
_gw = load_module("08_TOOLS_MCP/tool_gateway.py", "silicium_tool_gateway")
_ad = load_module("08_TOOLS_MCP/approval_adapter.py", "silicium_approval_adapter")

NOTE = {"order_id": "ORD-1042", "note": "SLA breach: unfulfilled > 48h", "idempotency_key": "IDEM-AD-1"}


def _request(approvals, *, strategic=False, inputs=NOTE, description="Add SLA breach note to order", now=None):
    return approvals.create_request(
        task_id="TASK-20261125-ADP1", requesting_agent="agent.operations", tool_name="shopify.orders.add_internal_note",
        action_description=description, risk_level="medium", domain="operations", resource_affected="shopify:order:ORD-1042",
        proposed_action=inputs, reversible=True, estimated_impact="Internal note only; no customer impact",
        strategic=strategic, now=now,
    )


def _visible(telegram_html: str) -> str:
    """The text the Founder actually sees: tags removed, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", telegram_html))


def _setup():
    approvals = _approval.ApprovalStore()
    adapter = _ad.TelegramApprovalAdapter()
    return approvals, adapter


# ------------------------------------------------------------------ Tier 0 payload format


def test_telegram_payload_contains_all_tier0_fields() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    msg = adapter.format_request(req)
    text = _visible(msg.text)
    assert req["request_id"].startswith("APR-") and req["request_id"] in text
    assert "Risk: MEDIUM" in text and "Reversible: YES" in text
    assert "TTL: 30 min (routine)" in text
    assert f"inputs_hash: {req['proposed_action_hash']}" in text  # full hash, not truncated
    assert "shopify.orders.add_internal_note" in text and "shopify:order:ORD-1042" in text
    assert "agent.operations" in text and "TASK-20261125-ADP1" in text
    assert msg.inputs_hash == req["proposed_action_hash"] and msg.ttl_class == "routine"


def test_payload_preview_is_exactly_the_canonical_payload_the_hash_covers() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    msg = adapter.format_request(req)
    canonical = _sv.canonical_json(req["proposed_action"])
    assert f"<pre>{html.escape(canonical)}</pre>" in msg.text
    assert _sv.canonical_sha256(json.loads(canonical)) == req["proposed_action_hash"]


def test_strategic_requests_show_24h_ttl_and_irreversible_is_flagged() -> None:
    approvals, adapter = _setup()
    req = approvals.create_request(
        task_id="TASK-20261125-ADP2", requesting_agent="agent.operations", tool_name="shopify.orders.add_internal_note",
        action_description="Strategic irreversible change", risk_level="high", domain="operations", resource_affected="shopify:order:ORD-1042",
        proposed_action=NOTE, reversible=False, estimated_impact="Cannot be undone", strategic=True,
    )
    msg = adapter.format_request(req)
    seen = _visible(msg.text)
    assert "TTL: 24h (strategic)" in seen and "Reversible: NO" in seen and "Risk: HIGH" in seen
    assert msg.ttl_class == "strategic" and msg.reversible is False


def test_telegram_send_message_shape_and_button_limits() -> None:
    approvals, adapter = _setup()
    msg = adapter.format_request(_request(approvals))
    p = msg.payload
    assert p["method"] == "sendMessage" and p["parse_mode"] == "HTML" and p["chat_id"] == "MOCK_FOUNDER_CHAT"
    assert len(p["text"]) <= _ad.TELEGRAM_TEXT_LIMIT
    row = p["reply_markup"]["inline_keyboard"][0]
    assert [b["text"].split()[-1] for b in row] == ["Approve", "Reject", "changes"]
    for b in row:
        assert len(b["callback_data"].encode()) <= _ad.CALLBACK_DATA_LIMIT_BYTES
        assert msg.request_id in b["callback_data"] and msg.hash_prefix in b["callback_data"]


def test_untrusted_text_is_html_escaped() -> None:
    approvals, adapter = _setup()
    req = _request(approvals, description="Add note <script>alert(1)</script> & more")
    msg = adapter.format_request(req)
    assert "<script>" not in msg.text and "&lt;script&gt;" in msg.text


# ------------------------------------------------------------------ refusal to render unsafe requests


def test_tampered_payload_hash_mismatch_is_refused() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    req["proposed_action"] = {**req["proposed_action"], "note": "something else entirely"}
    with pytest.raises(_ad.ApprovalFormatError, match="does not match"):
        adapter.format_request(req)


def test_expired_non_pending_and_invalid_requests_are_refused() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    with pytest.raises(_ad.ApprovalFormatError, match="expired"):
        adapter.format_request(req, now=datetime.now(timezone.utc) + timedelta(minutes=31))
    approvals.approve(req["request_id"])
    with pytest.raises(_ad.ApprovalFormatError, match="pending"):
        adapter.format_request(approvals.get_request(req["request_id"]))
    with pytest.raises(_sv.SchemaValidationError):
        adapter.format_request({**req, "risk_level": "catastrophic"})


def test_oversized_payload_is_refused_not_truncated() -> None:
    approvals, adapter = _setup()
    big = {**NOTE, "note": "x" * 5000}
    req = _request(approvals, inputs=big)
    with pytest.raises(_ad.ApprovalFormatError, match="too large"):
        adapter.format_request(req)


# ------------------------------------------------------------------ stub transport honesty


def test_stub_send_never_claims_delivery() -> None:
    approvals, adapter = _setup()
    receipt = adapter.send(adapter.format_request(_request(approvals)))
    assert receipt["status"] == "queued_local_stub" and receipt["delivered"] is False
    assert "nothing was sent" in receipt["note"] and len(adapter.outbox) == 1


def test_verified_bot_without_transport_fails_closed_and_with_transport_sends() -> None:
    status = {"integrations": {"telegram_approval_bot": {"status": "verified"}}}
    with pytest.raises(_ad.ApprovalAdapterError):
        _ad.TelegramApprovalAdapter(integration_status=status)
    sent: list[dict] = []
    adapter = _ad.TelegramApprovalAdapter(integration_status=status, transport=lambda p: sent.append(p) or {"ok": True})
    approvals = _approval.ApprovalStore()
    receipt = adapter.send(adapter.format_request(_request(approvals)))
    assert receipt["delivered"] is True and len(sent) == 1


# ------------------------------------------------------------------ decisions


def _press(adapter, msg, action, **kw):
    return adapter.parse_decision(adapter.simulate_button_press(msg, action, **kw))


def test_founder_approval_flows_through_store_and_gateway_with_hash_binding(tmp_path: Path) -> None:
    approvals, adapter = _setup()
    audit = _audit.AuditLogger(audit_path=tmp_path / "a.jsonl", incidents_dir=tmp_path / "i")
    gateway = _gw.ToolGateway(approvals=approvals, audit_logger=audit)
    req = _request(approvals)
    msg = adapter.format_request(req)
    outcome = adapter.apply_decision(_press(adapter, msg, "approve"), approvals)
    assert outcome["outcome"] == "approved" and outcome["status"] == "approved"
    # Tampered payload is still blocked by the gateway, approval stays unconsumed.
    bad = gateway.invoke(task_id="TASK-20261125-ADP1", agent_id="agent.operations", environment="dev",
                         tool_name="shopify.orders.add_internal_note", inputs={**NOTE, "note": "tampered"}, approval_id=req["request_id"])
    assert bad["error"]["code"] == "APPROVAL_HASH_MISMATCH"
    ok = gateway.invoke(task_id="TASK-20261125-ADP1", agent_id="agent.operations", environment="dev",
                        tool_name="shopify.orders.add_internal_note", inputs=NOTE, approval_id=req["request_id"])
    assert ok["status"] == "success"


def test_reject_and_modify_never_approve() -> None:
    approvals, adapter = _setup()
    r1 = _request(approvals)
    out = adapter.apply_decision(_press(adapter, adapter.format_request(r1), "reject"), approvals)
    assert out["outcome"] == "rejected" and approvals.get_request(r1["request_id"])["status"] == "rejected"

    r2 = _request(approvals)
    out = adapter.apply_decision(_press(adapter, adapter.format_request(r2), "modify"), approvals)
    assert out["outcome"] == "modification_requested" and out["requires_new_request"] is True
    rec = approvals.get_request(r2["request_id"])
    assert rec["status"] == "rejected" and rec["rejection_reason"].startswith("MODIFICATION_REQUESTED")


def test_only_the_authenticated_founder_can_decide() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    msg = adapter.format_request(req)
    with pytest.raises(_ad.ApprovalAuthError):
        _press(adapter, msg, "approve", from_user_id="attacker-999")
    update = adapter.simulate_button_press(msg, "approve")
    update["callback_query"]["message"]["chat"]["id"] = "OTHER_CHAT"
    with pytest.raises(_ad.ApprovalAuthError):
        adapter.parse_decision(update)
    assert approvals.get_request(req["request_id"])["status"] == "pending"


def test_identity_comes_from_the_channel_not_from_message_content() -> None:
    approvals, adapter = _setup()
    msg = adapter.format_request(_request(approvals))
    update = adapter.simulate_button_press(msg, "approve")
    update["callback_query"]["actor"] = "founder:abdulrahman"  # attacker-supplied claim is ignored
    update["callback_query"]["from"]["id"] = "attacker-999"
    with pytest.raises(_ad.ApprovalAuthError):
        adapter.parse_decision(update)


@pytest.mark.parametrize("data", ["", "apr|APR-20261125-ABC123|x|0123456789ab", "apr|bad|a|0123456789ab", "approve APR-1", "apr|APR-20261125-ABC123|a|XYZ"])
def test_malformed_callbacks_are_rejected(data: str) -> None:
    _, adapter = _setup()
    with pytest.raises(_ad.ApprovalCallbackError):
        adapter.parse_decision({"callback_query": {"from": {"id": "mock-founder-0001"}, "data": data}})
    with pytest.raises(_ad.ApprovalCallbackError):
        adapter.parse_decision({"message": {"text": "hi"}})


def test_stale_button_for_a_different_payload_hash_changes_nothing() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    decision = _ad.ApprovalDecision(req["request_id"], "approve", "0" * 12, "founder:abdulrahman", "telegram", datetime.now(timezone.utc).isoformat())
    out = adapter.apply_decision(decision, approvals)
    assert out["outcome"] == "stale_button_hash_mismatch"
    assert approvals.get_request(req["request_id"])["status"] == "pending"


def test_double_press_expiry_and_resurrection_are_handled() -> None:
    approvals, adapter = _setup()
    req = _request(approvals)
    msg = adapter.format_request(req)
    assert adapter.apply_decision(_press(adapter, msg, "approve"), approvals)["outcome"] == "approved"
    again = adapter.apply_decision(_press(adapter, msg, "approve"), approvals)
    assert again["outcome"] == "ignored_not_pending"

    rejected = _request(approvals)
    rmsg = adapter.format_request(rejected)
    adapter.apply_decision(_press(adapter, rmsg, "reject"), approvals)
    assert adapter.apply_decision(_press(adapter, rmsg, "approve"), approvals)["outcome"] == "ignored_not_pending"
    assert approvals.get_request(rejected["request_id"])["status"] == "rejected"

    late = _request(approvals)
    lmsg = adapter.format_request(late)
    out = adapter.apply_decision(_press(adapter, lmsg, "approve"), approvals, now=datetime.now(timezone.utc) + timedelta(minutes=31))
    assert out["outcome"] == "expired" and approvals.get_request(late["request_id"])["status"] == "expired"


def test_unknown_request_id_is_reported_not_raised() -> None:
    approvals, adapter = _setup()
    d = _ad.ApprovalDecision("APR-20261125-ZZZZZZ", "approve", "0" * 12, "founder:abdulrahman", "telegram", "now")
    assert adapter.apply_decision(d, approvals)["outcome"] == "unknown_request"


# ------------------------------------------------------------------ CLI adapter


def test_cli_adapter_is_plain_ascii_and_uses_the_same_checks() -> None:
    approvals = _approval.ApprovalStore()
    cli = _ad.LocalCliApprovalAdapter()
    req = _request(approvals)
    msg = cli.format_request(req)
    assert msg.plain_text.isascii() and "APPROVAL REQUIRED" in msg.plain_text and req["proposed_action_hash"] in msg.plain_text
    receipt = cli.send(msg)
    assert receipt["delivered"] is False and cli.outbox == [msg.plain_text]
    cmd = f"approve {req['request_id']} {msg.hash_prefix}"
    out = cli.apply_decision(cli.parse_decision({"actor": "founder:abdulrahman", "command": cmd}), approvals)
    assert out["outcome"] == "approved"
    with pytest.raises(_ad.ApprovalAuthError):
        cli.parse_decision({"actor": "agent.operations", "command": cmd})
    with pytest.raises(_ad.ApprovalCallbackError):
        cli.parse_decision({"actor": "founder:abdulrahman", "command": "approve everything"})
    tampered = {**req, "proposed_action": {**NOTE, "note": "x" * 10}}
    with pytest.raises(_ad.ApprovalFormatError):
        cli.format_request(tampered)
