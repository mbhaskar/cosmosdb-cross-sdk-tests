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


def python_runner_cmd() -> Optional[List[str]]:
    return [sys.executable, "-m", "cosmos_test_runner"]


def java_runner_cmd() -> Optional[List[str]]:
    jar = os.path.join(HARNESS, "java", "target", "cosmos-test-runner.jar")
    if not os.path.exists(jar):
        return None
    return ["java", "-jar", jar]


RUNNERS = {
    "python": {
        "cmd": python_runner_cmd,
        "cwd": os.path.join(HARNESS, "python"),
        "env": {"PYTHONPATH": os.path.join(HARNESS, "python")},
    },
    "java": {
        "cmd": java_runner_cmd,
        "cwd": os.path.join(HARNESS, "java"),
        "env": {},
    },
}


def available_sdks() -> List[Dict[str, Any]]:
    sdks = []
    for name, spec in RUNNERS.items():
        cmd = spec["cmd"]()
        sdks.append({
            "name": name,
            "available": cmd is not None,
            "versions": _default_versions(name),
        })
    return sdks


def _default_versions(name: str) -> List[str]:
    return {"python": ["4.9.0"], "java": ["4.63.0"]}.get(name, ["latest"])


def dispatch(sdk: str, scenario: Dict, config: Dict, sdk_version: str, timeout: int = 120) -> Dict[str, Any]:
    """Run one (scenario, sdk) job and return its result dict."""
    spec = RUNNERS.get(sdk)
    if spec is None:
        return _error_result(scenario, sdk, sdk_version, config, f"unknown sdk '{sdk}'")
    cmd = spec["cmd"]()
    if cmd is None:
        return _error_result(scenario, sdk, sdk_version, config,
                             f"{sdk} runner not built (no binary/jar found)")

    job = {"scenario": scenario, "config": config, "sdk_version": sdk_version}
    env = dict(os.environ)
    env.update(spec.get("env", {}))
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(job), capture_output=True, text=True,
            cwd=spec["cwd"], env=env, timeout=timeout,
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
