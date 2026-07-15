"""Dispatches a scenario job to a language-native runner subprocess.

Each runner reads a job JSON on stdin and writes a result JSON on stdout, so
adding a new SDK is just a new entry in ``RUNNERS``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
HARNESS = os.path.join(REPO_ROOT, "harness")
SDK_CATALOG_PATH = os.path.join(REPO_ROOT, "config", "default.yaml")

# Default artifact locations for the "local" (source-built) SDK variants. These
# can be overridden per-entry in config/default.yaml (jar:/venv:).
DEFAULT_LOCAL_JAR = os.path.join(HARNESS, "java", "variants", "local", "cosmos-test-runner.jar")
DEFAULT_PUBLISHED_JAR = os.path.join(HARNESS, "java", "target", "cosmos-test-runner.jar")
DEFAULT_LOCAL_VENV = os.path.join(HARNESS, "python", ".venv-local")

# Languages this orchestrator knows how to drive.
SDK_NAMES = ["python", "java"]


def _venv_python(venv_dir: str) -> str:
    win = os.path.join(venv_dir, "Scripts", "python.exe")
    nix = os.path.join(venv_dir, "bin", "python")
    return win if os.path.exists(win) else nix


def resolve_runner(sdk: str, source: str, entry: Optional[Dict[str, Any]] = None):
    """Return (cmd, cwd, env) for a (sdk, source) pair, or (None, cwd, env) when
    the required artifact is missing.

    source == "published": the released SDK (Maven Central / PyPI).
    source == "local":      an SDK built from an azure-sdk-for-* branch.
    """
    entry = entry or {}
    if sdk == "python":
        cwd = os.path.join(HARNESS, "python")
        env = {"PYTHONPATH": os.path.join(HARNESS, "python")}
        if source == "local":
            venv = os.path.join(REPO_ROOT, entry.get("venv", DEFAULT_LOCAL_VENV)) \
                if not os.path.isabs(entry.get("venv", DEFAULT_LOCAL_VENV)) \
                else entry.get("venv", DEFAULT_LOCAL_VENV)
            py = _venv_python(venv)
            return (([py, "-m", "cosmos_test_runner"] if os.path.exists(py) else None), cwd, env)
        # published: run with the orchestrator's own interpreter (azure-cosmos from pip).
        return ([sys.executable, "-m", "cosmos_test_runner"], cwd, env)

    if sdk == "java":
        cwd = os.path.join(HARNESS, "java")
        jar = entry.get("jar", DEFAULT_LOCAL_JAR if source == "local" else DEFAULT_PUBLISHED_JAR)
        if not os.path.isabs(jar):
            jar = os.path.join(REPO_ROOT, jar)
        return ((["java", "-jar", jar] if os.path.exists(jar) else None), cwd, {})

    return (None, REPO_ROOT, {})


# Kept for backward compatibility (mock/published default path).
RUNNERS = {name: {"cwd": resolve_runner(name, "published")[1]} for name in SDK_NAMES}


def _load_catalog() -> Dict[str, Any]:
    try:
        import yaml
        with open(SDK_CATALOG_PATH) as fh:
            data = yaml.safe_load(fh) or {}
        return data.get("sdks", {}) or {}
    except Exception:
        return {}


def _entries_for(name: str, catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    block = catalog.get(name, {})
    versions = block.get("versions") if isinstance(block, dict) else None
    if not versions:
        return [{"label": _fallback_version(name), "source": "published"}]
    out = []
    for v in versions:
        if isinstance(v, str):
            out.append({"label": v, "source": "published"})
        elif isinstance(v, dict):
            out.append({"label": v.get("label", "latest"),
                        "source": v.get("source", "published"),
                        "jar": v.get("jar"), "venv": v.get("venv")})
    return out


def available_sdks() -> List[Dict[str, Any]]:
    catalog = _load_catalog()
    sdks = []
    for name in SDK_NAMES:
        versions = []
        for entry in _entries_for(name, catalog):
            cmd, _, _ = resolve_runner(name, entry["source"],
                                       {k: entry[k] for k in ("jar", "venv") if entry.get(k)})
            versions.append({
                "label": entry["label"],
                "source": entry["source"],
                "available": cmd is not None,
            })
        sdks.append({
            "name": name,
            "available": any(v["available"] for v in versions),
            "versions": versions,
        })
    return sdks


def _fallback_version(name: str) -> str:
    return {"python": "4.9.0", "java": "4.63.0"}.get(name, "latest")


def _entry_for_source(name: str, source: str) -> Dict[str, Any]:
    for entry in _entries_for(name, _load_catalog()):
        if entry["source"] == source:
            return {k: entry[k] for k in ("jar", "venv") if entry.get(k)}
    return {}


def dispatch(sdk: str, scenario: Dict, config: Dict, sdk_version: str,
             source: str = "published", timeout: int = 120) -> Dict[str, Any]:
    """Run one (scenario, sdk) job and return its result dict."""
    if sdk not in SDK_NAMES:
        return _error_result(scenario, sdk, sdk_version, config, f"unknown sdk '{sdk}'")
    cmd, cwd, run_env = resolve_runner(sdk, source, _entry_for_source(sdk, source))
    if cmd is None:
        hint = ("local SDK variant not built — build it from an azure-sdk-for-* branch first"
                if source == "local" else "runner not built (no binary/jar found)")
        return _error_result(scenario, sdk, sdk_version, config, f"{sdk} ({source}): {hint}")

    # Inject the SDK identity so each runner namespaces its auto-provisioned
    # database per SDK (mvp-<scenario>-<sdk>-<run_id>). Without this, Python and
    # Java share one db per (scenario, run) and collide on the hardcoded item ids
    # when a mutating scenario (e.g. delete-item) runs on both against a real
    # backend. Copy config (don't mutate) -- the same dict is shared across the
    # concurrent dispatch calls for every SDK in the run.
    job = {
        "scenario": scenario,
        "config": {**config, "sdk": sdk},
        "sdk_version": sdk_version,
        "sdk_source": source,
    }
    env = dict(os.environ)
    env.update(run_env)
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(job), capture_output=True, text=True,
            cwd=cwd, env=env, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _error_result(scenario, sdk, sdk_version, config, f"runner timed out after {timeout}s")

    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout or "no output").strip()[-800:]
        return _error_result(scenario, sdk, sdk_version, config, f"runner exited {proc.returncode}: {detail}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _error_result(scenario, sdk, sdk_version, config, f"invalid result JSON: {exc}")


def _error_result(scenario: Dict, sdk: str, sdk_version: str, config: Dict, msg: str) -> Dict[str, Any]:
    return {
        "scenario_id": str(scenario.get("id")),
        "title": scenario.get("title"),
        "sdk": sdk,
        "sdk_version": sdk_version,
        "backend": config.get("backend", "mock"),
        "status": "error",
        "duration_ms": 0,
        "metrics": {},
        "assertions": [],
        "error": msg,
        "logs": [msg],
    }
