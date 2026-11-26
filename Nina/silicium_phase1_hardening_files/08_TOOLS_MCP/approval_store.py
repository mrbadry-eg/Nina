"""Single-use, SHA-256 payload-bound approval store for Tier 0 Founder approvals."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from silicium_loader import load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")


class ApprovalError(PermissionError):
    """Raised when an approval token is missing, invalid, expired, consumed, or hash-mismatched."""


class ApprovalStore:
    """In-memory approval manager conforming to 09_approval_request.schema.json."""

    ROUTINE_TTL_MINUTES = 30
    STRATEGIC_TTL_HOURS = 24

    def __init__(self) -> None:
        self._requests: dict[str, dict[str, Any]] = {}
        self.validator = _sv.CanonicalSchemaValidator()

    def create_request(
        self,
        *,
        task_id: str,
        requesting_agent: str,
        tool_name: str,
        action_description: str,
        risk_level: str,
        domain: str,
        resource_affected: str,
        proposed_action: dict[str, Any],
        reversible: bool,
        estimated_impact: str,
        strategic: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        ttl = timedelta(hours=self.STRATEGIC_TTL_HOURS) if strategic else timedelta(minutes=self.ROUTINE_TTL_MINUTES)
        req_id = f"APR-{current.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        record: dict[str, Any] = {
            "request_id": req_id,
            "task_id": task_id,
            "requesting_agent": requesting_agent,
            "tool_name": tool_name,
            "action_description": action_description,
            "risk_level": risk_level,
            "domain": domain,
            "resource_affected": resource_affected,
            "proposed_action": proposed_action,
            "proposed_action_hash": _sv.canonical_sha256(proposed_action),
            "reversible": reversible,
            "estimated_impact": estimated_impact,
            "requires_response_by": (current + ttl).isoformat(),
            "status": "pending",
            "consumed": False,
            "approver": None,
            "approved_at": None,
            "rejection_reason": None,
            "modifications": None,
            "created_at": current.isoformat(),
        }
        self.validator.validate("09_approval_request.schema.json", record)
        self._requests[req_id] = record
        return dict(record)

    def get_request(self, request_id: str) -> dict[str, Any]:
        """Return a copy of a request record (read-only view for channel adapters)."""
        if request_id not in self._requests:
            raise ApprovalError(f"Unknown approval request: {request_id}")
        return dict(self._requests[request_id])

    def approve(self, request_id: str, approver: str = "founder:abdulrahman", now: datetime | None = None) -> dict[str, Any]:
        if request_id not in self._requests:
            raise ApprovalError(f"Unknown approval request: {request_id}")
        if approver != "founder:abdulrahman":
            raise ApprovalError("Only Tier 0 founder:abdulrahman may approve L4 actions")
        rec = self._requests[request_id]
        if rec["status"] != "pending":
            raise ApprovalError(f"Approval request {request_id} is '{rec['status']}'; only 'pending' requests can be approved")
        current = now or datetime.now(timezone.utc)
        if datetime.fromisoformat(rec["requires_response_by"]) <= current:
            rec["status"] = "expired"
            raise ApprovalError(f"Approval request {request_id} expired before approval")
        rec["status"] = "approved"
        rec["approver"] = approver
        rec["approved_at"] = current.isoformat()
        self.validator.validate("09_approval_request.schema.json", rec)
        return dict(rec)

    def reject(self, request_id: str, reason: str) -> dict[str, Any]:
        if request_id not in self._requests:
            raise ApprovalError(f"Unknown approval request: {request_id}")
        rec = self._requests[request_id]
        if rec["status"] != "pending":
            raise ApprovalError(f"Approval request {request_id} is '{rec['status']}'; only 'pending' requests can be rejected")
        rec["status"] = "rejected"
        rec["rejection_reason"] = reason
        self.validator.validate("09_approval_request.schema.json", rec)
        return dict(rec)

    def verify_and_consume(
        self,
        *,
        request_id: str,
        tool_name: str,
        payload_hash: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if request_id not in self._requests:
            raise ApprovalError(f"Approval {request_id} not found")
        rec = self._requests[request_id]
        current = now or datetime.now(timezone.utc)

        if rec["status"] != "approved":
            raise ApprovalError(f"Approval {request_id} status is '{rec['status']}', expected 'approved'")
        if rec["approver"] != "founder:abdulrahman":
            raise ApprovalError(f"Approval {request_id} lacks Tier 0 founder approver")
        if rec["consumed"]:
            raise ApprovalError(f"Approval {request_id} was already consumed (single-use violated)")
        if datetime.fromisoformat(rec["requires_response_by"]) <= current:
            rec["status"] = "expired"
            raise ApprovalError(f"Approval {request_id} has expired")
        if rec["tool_name"] != tool_name:
            raise ApprovalError(f"Approval {request_id} bound to {rec['tool_name']}, not {tool_name}")
        if rec["proposed_action_hash"] != payload_hash:
            raise ApprovalError("APPROVAL_HASH_MISMATCH: Execution payload does not match approved SHA-256 hash")

        rec["consumed"] = True
        self.validator.validate("09_approval_request.schema.json", rec)
        return dict(rec)
