"""Domain-isolated memory store enforcing 11_memory_record.schema.json and authority boundaries."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")

VALID_DOMAINS = frozenset({"business", "founder", "product", "customer", "operational", "creative", "engineering"})


class MemoryBoundaryError(PermissionError):
    """Raised when a memory write violates domain isolation or source-authority rules."""


class MemoryStore:
    """Stores schema-validated records in isolated JSONL files per memory domain."""

    def __init__(self, stores_dir: Path | None = None) -> None:
        self.stores_dir = stores_dir or (REPO_ROOT / "07_MEMORY" / "stores")
        self.stores_dir.mkdir(parents=True, exist_ok=True)
        self.validator = _sv.CanonicalSchemaValidator()

    def append(
        self,
        *,
        memory_domain: str,
        content_type: str,
        content: dict[str, Any],
        source_system: str,
        source_authority_level: int,
        tags: list[str],
        confidence: float | None = None,
        verified_by: str | None = None,
    ) -> dict[str, Any]:
        if memory_domain not in VALID_DOMAINS:
            raise MemoryBoundaryError(f"Invalid memory domain: {memory_domain}")
        if memory_domain == "creative" and source_authority_level <= 4:
            raise MemoryBoundaryError("Creative Memory cannot store Level 1-4 authoritative product/policy truth")

        now = datetime.now(timezone.utc)
        rec_id = f"MEM-{memory_domain.upper()}-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        record: dict[str, Any] = {
            "record_id": rec_id,
            "memory_domain": memory_domain,
            "content_type": content_type,
            "content": content,
            "source_system": source_system,
            "source_authority_level": source_authority_level,
            "created_at": now.isoformat(),
            "expires_at": None,
            "superseded_by": None,
            "confidence": confidence,
            "verified_by": verified_by,
            "tags": tags,
        }
        self.validator.validate("11_memory_record.schema.json", record)
        path = self.stores_dir / f"{memory_domain}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read_domain(self, memory_domain: str) -> list[dict[str, Any]]:
        path = self.stores_dir / f"{memory_domain}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
