"""Orchestrator-side helper for the Toxiproxy fault-injection stack.

Lists the declarative toxic profiles shipped in ``proxy/profiles/*.yaml`` and
lets the UI activate one (or clear all) against a running Toxiproxy admin API.
It mirrors the verb -> toxic mapping in
``harness/python/cosmos_test_runner/faults.py`` but is a standalone stdlib-only
client so the orchestrator has no dependency on the harness package.

A profile file looks like::

    name: latency-spike
    proxy: cosmos
    toxics:
      - name: latency_down
        type: latency
        stream: downstream
        attributes: { latency: 800, jitter: 200 }

Activating a profile POSTs each of its toxics to the named proxy.
"""

from __future__ import annotations

import glob
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import yaml

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROFILES_DIR = os.path.join(_REPO_ROOT, "proxy", "profiles")


class ProxyError(RuntimeError):
    pass


def list_profiles() -> List[Dict[str, Any]]:
    """Return the catalog of declarative toxic profiles on disk."""
    out: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(PROFILES_DIR, "*.yaml"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = yaml.safe_load(fh) or {}
        except Exception as exc:  # pragma: no cover - defensive
            doc = {"name": os.path.basename(path), "error": str(exc)}
        doc.setdefault("name", os.path.splitext(os.path.basename(path))[0])
        doc.setdefault("id", doc["name"])
        doc["_file"] = os.path.relpath(path, _REPO_ROOT)
        out.append(doc)
    return out


def _admin_url() -> str:
    return os.environ.get("TOXIPROXY_URL", "http://localhost:8474").rstrip("/")


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{_admin_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise ProxyError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise ProxyError(f"cannot reach Toxiproxy at {_admin_url()}: {exc.reason}. "
                         f"Start proxy/docker-compose.proxy.yaml first.") from exc


def clear(proxy: str = "cosmos") -> Dict[str, Any]:
    """Remove all toxics from a proxy."""
    existing = _request("GET", f"/proxies/{proxy}/toxics") or []
    for tox in existing:
        _request("DELETE", f"/proxies/{proxy}/toxics/{tox['name']}")
    return {"proxy": proxy, "cleared": [t["name"] for t in existing]}


def activate(profile_id: str, proxy: Optional[str] = None) -> Dict[str, Any]:
    """Apply every toxic in the named profile to its proxy (after clearing)."""
    profile = next((p for p in list_profiles()
                    if p.get("id") == profile_id or p.get("name") == profile_id), None)
    if not profile:
        raise ProxyError(f"unknown profile '{profile_id}'")
    target = proxy or profile.get("proxy", "cosmos")
    clear(target)
    applied: List[str] = []
    for tox in profile.get("toxics", []):
        _request("POST", f"/proxies/{target}/toxics", {
            "name": tox["name"],
            "type": tox["type"],
            "stream": tox.get("stream", "downstream"),
            "toxicity": float(tox.get("toxicity", 1.0)),
            "attributes": tox.get("attributes", {}),
        })
        applied.append(tox["name"])
    return {"proxy": target, "profile": profile.get("name"), "applied": applied}
