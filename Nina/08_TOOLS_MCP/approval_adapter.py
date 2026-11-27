"""Approval channel adapters: how a Tier 0 approval request reaches the Founder and how the answer comes back.

``ApprovalStore`` owns approval *state* (single use, TTL, hash binding). Adapters own
only *presentation and transport*:

* ``format_request`` renders an ``APR-*`` request as a message. It first re-computes the
  SHA-256 of ``proposed_action`` and refuses to render if it differs from the stored
  ``proposed_action_hash`` (the Founder must see exactly what the hash covers). It also
  refuses expired or non-pending requests, and refuses payloads too large to display in
  full (never approve what cannot be seen).
* ``send`` delivers the message. The Telegram adapter here is a **local stub**: while
  ``telegram_approval_bot`` is ``unavailable`` it only queues to an outbox and reports
  ``delivered: False``. It never claims a message was sent.
* ``parse_decision`` turns a channel callback into an ``ApprovalDecision``. The approver
  identity comes from the channel's authenticated sender, never from message content.
* ``apply_decision`` (shared) checks the button's hash prefix against the stored
  request, then calls ``ApprovalStore.approve`` / ``reject``. A modification request
  rejects the original: any change to a payload changes its hash, so it needs a new
  ``APR-*`` request.

Tier 0 message fields: request id, risk level, reversible flag, TTL class (``30 min``
routine or ``24h`` strategic), expiry, tool, resource, requester, task, action, impact,
the full ``inputs_hash`` and the exact canonical payload.
"""

from __future__ import annotations

import html
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")
_approval = load_module("08_TOOLS_MCP/approval_store.py", "silicium_approval_store")

INTEGRATION_STATUS_PATH = REPO_ROOT / "08_TOOLS_MCP" / "integration_status.json"
FOUNDER_PRINCIPAL = "founder:abdulrahman"

TELEGRAM_TEXT_LIMIT = 4096
CALLBACK_DATA_LIMIT_BYTES = 64
HASH_PREFIX_LEN = 12
ACTION_CODES = {"approve": "a", "reject": "r", "modify": "m"}
CODE_ACTIONS = {v: k for k, v in ACTION_CODES.items()}
_CALLBACK_RE = re.compile(r"^apr\|(APR-\d{8}-[A-Z0-9]{6})\|([arm])\|([0-9a-f]{%d})$" % HASH_PREFIX_LEN)


class ApprovalAdapterError(Exception):
    """Base class for adapter errors."""


class ApprovalFormatError(ApprovalAdapterError, ValueError):
    """The request cannot be shown to the Founder safely."""


class ApprovalAuthError(ApprovalAdapterError, PermissionError):
    """The sender is not the authenticated Founder."""


class ApprovalCallbackError(ApprovalAdapterError, ValueError):
    """The callback is malformed."""


@dataclass(frozen=True)
class ApprovalMessage:
    request_id: str
    channel: str
    inputs_hash: str
    hash_prefix: str
    risk_level: str
    reversible: bool
    ttl_class: str
    ttl_label: str
    expires_at: str
    text: str            # channel-native text (Telegram HTML)
    plain_text: str      # channel-neutral text (CLI)
    payload: dict[str, Any]  # channel-native payload (Telegram sendMessage params)


@dataclass(frozen=True)
class ApprovalDecision:
    request_id: str
    action: str            # approve | reject | modify
    hash_prefix: str
    actor: str             # authenticated principal, e.g. founder:abdulrahman
    channel: str
    received_at: str


def _hash_prefix(digest: str) -> str:
    return digest.split(":")[-1][:HASH_PREFIX_LEN]


def _ttl(created_at: datetime, expires_at: datetime) -> tuple[str, str]:
    seconds = int((expires_at - created_at).total_seconds())
    routine = _approval.ApprovalStore.ROUTINE_TTL_MINUTES * 60
    strategic = _approval.ApprovalStore.STRATEGIC_TTL_HOURS * 3600
    if seconds == routine:
        return "routine", f"{_approval.ApprovalStore.ROUTINE_TTL_MINUTES} min"
    if seconds == strategic:
        return "strategic", f"{_approval.ApprovalStore.STRATEGIC_TTL_HOURS}h"
    return "custom", f"{seconds // 60} min"


def _remaining(expires_at: datetime, now: datetime) -> str:
    minutes = max(0, int((expires_at - now).total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 120 else f"{minutes} min"


class ApprovalChannelAdapter(ABC):
    """Interface every approval channel (Telegram, CLI, later a dashboard) implements."""

    channel: str = "abstract"

    def __init__(self) -> None:
        self.validator = _sv.CanonicalSchemaValidator()

    # -- rendering (shared validation, channel-specific layout)

    def _facts(self, request: dict[str, Any], now: datetime | None) -> dict[str, Any]:
        self.validator.validate("09_approval_request.schema.json", request)
        current = now or datetime.now(timezone.utc)
        if request["status"] != "pending":
            raise ApprovalFormatError(f"{request['request_id']} is '{request['status']}', only pending requests are shown")
        recomputed = _sv.canonical_sha256(request["proposed_action"])
        if recomputed != request["proposed_action_hash"]:
            raise ApprovalFormatError(f"{request['request_id']}: proposed_action does not match proposed_action_hash; refusing to render")
        created = datetime.fromisoformat(request["created_at"])
        expires = datetime.fromisoformat(request["requires_response_by"])
        if expires <= current:
            raise ApprovalFormatError(f"{request['request_id']} expired at {expires.isoformat()}")
        ttl_class, ttl_label = _ttl(created, expires)
        return {
            "current": current, "expires": expires, "ttl_class": ttl_class, "ttl_label": ttl_label,
            "canonical": _sv.canonical_json(request["proposed_action"]),
            "prefix": _hash_prefix(request["proposed_action_hash"]),
            "remaining": _remaining(expires, current),
        }

    @abstractmethod
    def format_request(self, request: dict[str, Any], *, now: datetime | None = None) -> ApprovalMessage: ...

    @abstractmethod
    def send(self, message: ApprovalMessage) -> dict[str, Any]: ...

    @abstractmethod
    def parse_decision(self, raw: dict[str, Any], *, now: datetime | None = None) -> ApprovalDecision: ...

    # -- applying a decision (shared, channel independent)

    def apply_decision(self, decision: ApprovalDecision, approvals: Any, *, now: datetime | None = None) -> dict[str, Any]:
        """Apply a parsed decision to the ApprovalStore. Returns an outcome record; never raises on stale input."""
        outcome: dict[str, Any] = {
            "request_id": decision.request_id, "action": decision.action, "outcome": None,
            "status": None, "requires_new_request": False, "error": None,
        }
        try:
            record = approvals.get_request(decision.request_id)
        except _approval.ApprovalError as exc:
            return {**outcome, "outcome": "unknown_request", "error": str(exc)}

        outcome["status"] = record["status"]
        if _hash_prefix(record["proposed_action_hash"]) != decision.hash_prefix:
            return {**outcome, "outcome": "stale_button_hash_mismatch", "error": "Button does not match the current payload hash; nothing changed"}
        if record["status"] != "pending":
            return {**outcome, "outcome": "ignored_not_pending", "error": f"Request is already '{record['status']}'"}

        try:
            if decision.action == "approve":
                updated = approvals.approve(decision.request_id, approver=decision.actor, now=now)
                return {**outcome, "outcome": "approved", "status": updated["status"]}
            if decision.action == "reject":
                updated = approvals.reject(decision.request_id, "REJECTED_BY_FOUNDER")
                return {**outcome, "outcome": "rejected", "status": updated["status"]}
            updated = approvals.reject(decision.request_id, "MODIFICATION_REQUESTED: payload changes require a new approval request")
            return {**outcome, "outcome": "modification_requested", "status": updated["status"], "requires_new_request": True}
        except _approval.ApprovalError as exc:
            current = approvals.get_request(decision.request_id)
            label = "expired" if current["status"] == "expired" else "refused_by_store"
            return {**outcome, "outcome": label, "status": current["status"], "error": str(exc)}


class TelegramApprovalAdapter(ApprovalChannelAdapter):
    """Telegram Bot API formatter and local stub transport (credential-free)."""

    channel = "telegram"

    def __init__(
        self,
        *,
        founder_chat_id: str = "MOCK_FOUNDER_CHAT",
        identity_map: dict[str, str] | None = None,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        integration_status: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.founder_chat_id = founder_chat_id
        # Telegram user id -> principal. Only the Founder is ever mapped to an approving principal.
        self.identity_map = dict(identity_map or {"mock-founder-0001": FOUNDER_PRINCIPAL})
        self.transport = transport
        status = integration_status or json.loads(INTEGRATION_STATUS_PATH.read_text(encoding="utf-8"))
        self.bot_status = status.get("integrations", {}).get("telegram_approval_bot", {}).get("status", "unavailable")
        if self.bot_status == "verified" and transport is None:
            raise ApprovalAdapterError("telegram_approval_bot is 'verified' but no transport was injected; refusing to use the local stub")
        self.outbox: list[dict[str, Any]] = []

    def format_request(self, request: dict[str, Any], *, now: datetime | None = None) -> ApprovalMessage:
        f = self._facts(request, now)
        e = html.escape
        reversible = "YES" if request["reversible"] else "NO"
        risk = request["risk_level"].upper()
        text = "\n".join([
            f"\U0001F510 <b>APPROVAL REQUIRED</b> <code>{e(request['request_id'])}</code>",
            f"Risk: <b>{e(risk)}</b> | Reversible: <b>{reversible}</b>",
            f"TTL: <b>{e(f['ttl_label'])}</b> ({f['ttl_class']}) | Expires: {e(f['expires'].strftime('%Y-%m-%d %H:%M UTC'))} (in {e(f['remaining'])})",
            f"Tool: <code>{e(request['tool_name'])}</code>",
            f"Resource: <code>{e(request['resource_affected'])}</code>",
            f"Requested by: {e(request['requesting_agent'])} | Task: <code>{e(request['task_id'])}</code>",
            f"Action: {e(request['action_description'])}",
            f"Impact: {e(request['estimated_impact'])}",
            f"inputs_hash: <code>{e(request['proposed_action_hash'])}</code>",
            "Exact payload covered by the hash:",
            f"<pre>{e(f['canonical'])}</pre>",
            "Approval is bound to this hash only. Any change needs a new request.",
        ])
        if len(text) > TELEGRAM_TEXT_LIMIT:
            raise ApprovalFormatError(
                f"{request['request_id']}: payload too large to display in full ({len(text)} > {TELEGRAM_TEXT_LIMIT}); refusing to truncate what the hash covers"
            )
        keyboard = [[
            {"text": "\u2705 Approve", "callback_data": self._callback(request["request_id"], "approve", f["prefix"])},
            {"text": "\u274C Reject", "callback_data": self._callback(request["request_id"], "reject", f["prefix"])},
            {"text": "\u270F\uFE0F Request changes", "callback_data": self._callback(request["request_id"], "modify", f["prefix"])},
        ]]
        payload = {
            "method": "sendMessage",
            "chat_id": self.founder_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": keyboard},
        }
        return ApprovalMessage(
            request_id=request["request_id"], channel=self.channel, inputs_hash=request["proposed_action_hash"],
            hash_prefix=f["prefix"], risk_level=request["risk_level"], reversible=request["reversible"],
            ttl_class=f["ttl_class"], ttl_label=f["ttl_label"], expires_at=f["expires"].isoformat(),
            text=text, plain_text=_plain(request, f), payload=payload,
        )

    @staticmethod
    def _callback(request_id: str, action: str, prefix: str) -> str:
        data = f"apr|{request_id}|{ACTION_CODES[action]}|{prefix}"
        if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT_BYTES:
            raise ApprovalFormatError("callback_data exceeds Telegram's 64-byte limit")
        return data

    def send(self, message: ApprovalMessage) -> dict[str, Any]:
        if self.transport is not None:
            response = self.transport(message.payload)
            return {"status": "sent", "channel": self.channel, "request_id": message.request_id, "delivered": True, "transport_response": response}
        entry = {"message_id": len(self.outbox) + 1, "request_id": message.request_id, "payload": message.payload}
        self.outbox.append(entry)
        return {
            "status": "queued_local_stub", "channel": self.channel, "request_id": message.request_id,
            "message_id": entry["message_id"], "delivered": False,
            "note": f"telegram_approval_bot is '{self.bot_status}'; nothing was sent to Telegram",
        }

    def parse_decision(self, raw: dict[str, Any], *, now: datetime | None = None) -> ApprovalDecision:
        query = raw.get("callback_query")
        if not isinstance(query, dict):
            raise ApprovalCallbackError("update has no callback_query")
        sender = str((query.get("from") or {}).get("id", ""))
        principal = self.identity_map.get(sender)
        if principal != FOUNDER_PRINCIPAL:
            raise ApprovalAuthError("Callback sender is not the authenticated Founder")
        chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
        if chat_id is not None and str(chat_id) != str(self.founder_chat_id):
            raise ApprovalAuthError("Callback came from a chat other than the Founder's approval chat")
        match = _CALLBACK_RE.match(str(query.get("data", "")))
        if not match:
            raise ApprovalCallbackError("Malformed callback_data")
        request_id, code, prefix = match.groups()
        return ApprovalDecision(
            request_id=request_id, action=CODE_ACTIONS[code], hash_prefix=prefix, actor=principal,
            channel=self.channel, received_at=(now or datetime.now(timezone.utc)).isoformat(),
        )

    def simulate_button_press(self, message: ApprovalMessage, action: str, *, from_user_id: str = "mock-founder-0001") -> dict[str, Any]:
        """Build a Bot-API-shaped ``callback_query`` update, as Telegram would deliver it (tests and demos)."""
        row = message.payload["reply_markup"]["inline_keyboard"][0]
        data = next(b["callback_data"] for b in row if b["callback_data"].split("|")[2] == ACTION_CODES[action])
        return {
            "update_id": 1,
            "callback_query": {
                "id": "mock-callback-1",
                "from": {"id": from_user_id, "is_bot": False},
                "message": {"message_id": 1, "chat": {"id": self.founder_chat_id}},
                "data": data,
            },
        }


class LocalCliApprovalAdapter(ApprovalChannelAdapter):
    """Plain-text channel for local development. The local operator is treated as the Founder only when ``actor`` says so."""

    channel = "cli"

    def __init__(self) -> None:
        super().__init__()
        self.outbox: list[str] = []

    def format_request(self, request: dict[str, Any], *, now: datetime | None = None) -> ApprovalMessage:
        f = self._facts(request, now)
        text = _plain(request, f)
        return ApprovalMessage(
            request_id=request["request_id"], channel=self.channel, inputs_hash=request["proposed_action_hash"],
            hash_prefix=f["prefix"], risk_level=request["risk_level"], reversible=request["reversible"],
            ttl_class=f["ttl_class"], ttl_label=f["ttl_label"], expires_at=f["expires"].isoformat(),
            text=text, plain_text=text, payload={"text": text},
        )

    def send(self, message: ApprovalMessage) -> dict[str, Any]:
        self.outbox.append(message.plain_text)
        return {"status": "queued_local_stub", "channel": self.channel, "request_id": message.request_id, "delivered": False,
                "note": "CLI channel: render with message.plain_text"}

    def parse_decision(self, raw: dict[str, Any], *, now: datetime | None = None) -> ApprovalDecision:
        if raw.get("actor") != FOUNDER_PRINCIPAL:
            raise ApprovalAuthError("CLI decisions must be made as founder:abdulrahman")
        parts = str(raw.get("command", "")).split()
        if len(parts) != 3 or parts[0] not in ACTION_CODES or not re.fullmatch(r"APR-\d{8}-[A-Z0-9]{6}", parts[1]) or not re.fullmatch(r"[0-9a-f]{%d}" % HASH_PREFIX_LEN, parts[2]):
            raise ApprovalCallbackError("Expected: <approve|reject|modify> <APR-id> <12-hex hash prefix>")
        return ApprovalDecision(
            request_id=parts[1], action=parts[0], hash_prefix=parts[2], actor=FOUNDER_PRINCIPAL,
            channel=self.channel, received_at=(now or datetime.now(timezone.utc)).isoformat(),
        )


def _plain(request: dict[str, Any], f: dict[str, Any]) -> str:
    return "\n".join([
        f"APPROVAL REQUIRED {request['request_id']}",
        f"Risk: {request['risk_level'].upper()} | Reversible: {'YES' if request['reversible'] else 'NO'}",
        f"TTL: {f['ttl_label']} ({f['ttl_class']}) | Expires: {f['expires'].strftime('%Y-%m-%d %H:%M UTC')} (in {f['remaining']})",
        f"Tool: {request['tool_name']}",
        f"Resource: {request['resource_affected']}",
        f"Requested by: {request['requesting_agent']} | Task: {request['task_id']}",
        f"Action: {request['action_description']}",
        f"Impact: {request['estimated_impact']}",
        f"inputs_hash: {request['proposed_action_hash']}",
        "Exact payload covered by the hash:",
        f['canonical'],
        f"Reply: approve|reject|modify {request['request_id']} {f['prefix']}",
    ])
