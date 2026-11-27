"""Repository-wide checks for committed credentials and runtime audit data."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATTERNS = (
    ("OpenRouter", re.compile(rb"(?<![A-Za-z0-9_])sk-or-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("Shopify", re.compile(rb"(?<![A-Za-z0-9_])shpat_[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("Telegram", re.compile(rb"(?<!\d)\d{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")),
)


def _tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, "could not enumerate tracked files"
    return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]


def test_secret_patterns_match_tokens_not_safe_prefix_mentions() -> None:
    examples = (
        b"sk-or-" + b"a" * 40,
        b"shpat_" + b"a" * 32,
        b"12345678:" + b"A" * 35,
    )
    for (name, pattern), example in zip(TOKEN_PATTERNS, examples, strict=True):
        assert pattern.search(example), f"{name} token shape was not recognized"

    safe_text = b"Prefixes sk-or- and shpat_ are documented; token format is \\d{8,10}:..."
    assert all(pattern.search(safe_text) is None for _, pattern in TOKEN_PATTERNS)


def test_tracked_files_contain_no_secrets_or_audit_artifacts() -> None:
    tracked_paths = _tracked_paths()
    audit_paths_found = False
    secret_found = False

    for tracked_path in tracked_paths:
        parts = PurePosixPath(tracked_path).parts
        if any(parts[index : index + 2] == ("15_OBSERVABILITY", "audit") for index in range(len(parts) - 1)):
            audit_paths_found = True

        content = (REPO_ROOT / Path(*parts)).read_bytes()
        if any(pattern.search(content) for _, pattern in TOKEN_PATTERNS):
            secret_found = True

    assert not audit_paths_found, "tracked files under 15_OBSERVABILITY/audit/ are prohibited"
    assert not secret_found, "token-shaped secret detected in tracked files; values are suppressed"
