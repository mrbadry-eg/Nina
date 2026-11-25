"""Deterministic module loader for Silicium numeric-prefixed architecture directories."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent


def load_module(relative_path: str, module_name: str | None = None) -> ModuleType:
    """Load a Python module from a repo-relative path with caching in sys.modules."""
    resolved = (REPO_ROOT / relative_path).resolve()
    name = module_name or f"silicium_{relative_path.replace('/', '_').replace('\\', '_').replace('.py', '')}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {resolved}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
