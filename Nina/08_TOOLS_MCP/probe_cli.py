"""Command-line entry point for read-only integration probes."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
from typing import cast

from integration_probe import Integration, HttpTransport, UrllibTransport, probe_integration, write_evidence


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: HttpTransport | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only Silicium integration probe.")
    parser.add_argument(
        "--integration",
        required=True,
        choices=("openrouter", "n8n", "openclaw", "telegram"),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="Evidence destination (defaults to 15_OBSERVABILITY/probes/).",
    )
    args = parser.parse_args(argv)
    variables = os.environ if env is None else env
    result = probe_integration(
        cast(Integration, args.integration),
        transport if transport is not None else UrllibTransport(),
        variables,
    )
    project_root = Path(__file__).resolve().parents[1]
    evidence_path = write_evidence(
        result,
        args.evidence_dir or project_root / "15_OBSERVABILITY" / "probes",
    )
    print(
        json.dumps(
            {"result": result.to_dict(), "evidence_file": str(evidence_path)},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
