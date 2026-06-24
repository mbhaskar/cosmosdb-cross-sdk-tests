"""Resolves backend connection settings (endpoint/key) for non-mock runs.

Precedence (highest first):
  1. Values supplied explicitly in the run request config.
  2. Environment variables ``COSMOS_ENDPOINT`` / ``COSMOS_KEY`` (live backend).
  3. ``config/default.yaml`` block for the backend, with ``${ENV}`` expansion.

Secrets are never persisted: :func:`redact` masks the key before storage.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a declared dependency
    yaml = None

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


def load_defaults(path: str) -> Dict[str, Any]:
    """Load config/default.yaml (env-expanded). Returns {} if missing/unreadable."""
    if not path or yaml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:
        return {}
    out: Dict[str, Any] = {}
    for backend in ("emulator", "live"):
        block = data.get(backend) or {}
        out[backend] = {k: _expand_env(v) for k, v in block.items()}
    return out


def _first_nonempty(*values: Optional[str]) -> Optional[str]:
    for v in values:
        if v:
            return v
    return None


def resolve(config: Dict[str, Any], defaults: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (resolved_config, error). For mock, config passes through unchanged."""
    backend = config.get("backend", "mock")
    if backend == "mock":
        return config, None

    block = defaults.get(backend, {})
    env_endpoint = os.environ.get("COSMOS_ENDPOINT") if backend == "live" else None
    env_key = os.environ.get("COSMOS_KEY") if backend == "live" else None

    endpoint = _first_nonempty(config.get("endpoint"), env_endpoint, block.get("endpoint"))
    key = _first_nonempty(config.get("key"), env_key, block.get("key"))

    if not endpoint or not key:
        return None, (
            f"backend '{backend}' requires an endpoint and key. Supply them in the request, "
            f"set COSMOS_ENDPOINT / COSMOS_KEY environment variables, or fill the '{backend}' "
            f"block in config/default.yaml."
        )

    resolved = dict(config)
    resolved["endpoint"] = endpoint
    resolved["key"] = key
    return resolved, None


def redact(config: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of config safe to persist/return: the key is masked."""
    safe = dict(config)
    if safe.get("key"):
        safe["key"] = "***redacted***"
    return safe
