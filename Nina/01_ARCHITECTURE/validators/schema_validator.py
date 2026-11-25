"""Canonical JSON Schema Draft 2020-12 validator and canonical hashing utilities."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "01_ARCHITECTURE" / "schemas"


class SchemaValidationError(ValueError):
    """Raised when a payload fails canonical schema validation."""


def canonical_json(payload: Any) -> str:
    """Serialize payload deterministically for hashing and approval binding."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(payload: Any) -> str:
    """Compute prefixed SHA-256 digest of a canonical JSON payload."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class CanonicalSchemaValidator:
    """Loads all 18 canonical JSON Schemas with $ref resolution and format checks."""

    def __init__(self, schemas_dir: Path | None = None) -> None:
        self.schemas_dir = schemas_dir or SCHEMAS_DIR
        self._schemas: dict[str, dict[str, Any]] = {}
        resources: list[tuple[str, Resource[Any]]] = []

        for path in sorted(self.schemas_dir.glob("*.schema.json")):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self._schemas[path.name] = schema
            schema_id = schema.get("$id")
            if schema_id:
                resources.append((schema_id, Resource.from_contents(schema)))

        self._registry: Registry[Any] = Registry().with_resources(resources)

    @property
    def schema_names(self) -> list[str]:
        return sorted(self._schemas.keys())

    def check_all_schemas(self) -> list[str]:
        """Verify every schema file against Draft 2020-12 meta-schema."""
        checked: list[str] = []
        for name, schema in sorted(self._schemas.items()):
            Draft202012Validator.check_schema(schema)
            checked.append(name)
        return checked

    def validate(self, schema_name: str, instance: Any) -> None:
        """Validate an instance against a named canonical schema or raise SchemaValidationError."""
        if schema_name not in self._schemas:
            raise KeyError(f"Unknown canonical schema: {schema_name}")
        validator = Draft202012Validator(
            self._schemas[schema_name],
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
                for err in errors
            )
            raise SchemaValidationError(f"{schema_name} validation failed: {details}")

    def validate_with_inline_schema(self, schema: dict[str, Any], instance: Any, label: str = "inline") -> None:
        """Validate an instance against an inline JSON Schema (e.g., ToolContract.inputs_schema)."""
        validator = Draft202012Validator(
            schema,
            registry=self._registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
                for err in errors
            )
            raise SchemaValidationError(f"{label} validation failed: {details}")
