"""Append-only, secret-redacted JSONL audit sink and incident logger."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")

REDACT_KEYS = frozenset({
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "phone",
    "email",
    "address",
    "card",
    "cvv",
})


def redact_dict(data: Any) -> Any:
    """Recursively redact sensitive keys before writing to audit logs."""
    if isinstance(data, dict):
        return {
            k: ("[REDACTED]" if str(k).lower() in REDACT_KEYS else redact_dict(v))
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redact_dict(item) for item in data]
    return data


def _gen_id(prefix: str) -> str:
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:6].upper()
    return f"{prefix}-{date_part}-{suffix}"


class AuditLogger:
    """Appends schema-validated AuditEvent and Incident records to disk."""

    def __init__(self, audit_path: Path | None = None, incidents_dir: Path | None = None) -> None:
        self.audit_path = audit_path or (REPO_ROOT / "15_OBSERVABILITY" / "audit" / "audit.jsonl")
        self.incidents_dir = incidents_dir or (REPO_ROOT / "15_OBSERVABILITY" / "incidents")
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.incidents_dir.mkdir(parents=True, exist_ok=True)
        self.validator = _sv.CanonicalSchemaValidator()

    def log_audit(
        self,
        *,
        environment: str,
        agent: str,
        action: str,
        tool: str,
        domain: str,
        resource: str,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        risk_level: str,
        autonomy_level: int,
        outcome: str,
        duration_ms: int,
        task_id: str | None = None,
        approval_id: str | None = None,
        error: str | None = None,
        model: str | None = None,
        tokens: dict[str, int] | None = None,
        cost_usd: float | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "event_id": _gen_id("AUD"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": environment,
            "task_id": task_id,
            "agent": agent,
            "action": action,
            "tool": tool,
            "domain": domain,
            "resource": resource,
            "inputs_summary": redact_dict(inputs),
            "outputs_summary": redact_dict(outputs),
            "risk_level": risk_level,
            "autonomy_level": autonomy_level,
            "approval_id": approval_id,
            "outcome": outcome,
            "error": error,
            "duration_ms": duration_ms,
            "model": model,
            "tokens": tokens,
            "cost_usd": cost_usd,
        }
        self.validator.validate("17_audit_event.schema.json", event)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def log_incident(
        self,
        *,
        incident_type: str,
        severity: str,
        description: str,
        affected_resources: list[str],
        detected_by: str,
        related_incidents: list[str] | None = None,
    ) -> dict[str, Any]:
        incident: dict[str, Any] = {
            "incident_id": _gen_id("INC"),
            "type": incident_type,
            "severity": severity,
            "description": description,
            "affected_resources": affected_resources,
            "detected_by": detected_by,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "assigned_to": "founder:abdulrahman",
            "resolution": None,
            "resolved_at": None,
            "root_cause": None,
            "prevention_actions": [],
            "related_incidents": list(related_incidents or []),
        }
        self.validator.validate("16_incident.schema.json", incident)
        out_file = self.incidents_dir / f"{incident['incident_id']}.json"
        out_file.write_text(json.dumps(incident, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return incident
