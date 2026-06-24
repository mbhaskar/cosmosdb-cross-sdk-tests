#!/usr/bin/env python3
"""Headless cross-SDK matrix runner for CI.

Runs every scenario in ``specs/`` against the selected SDK runners (the same
``runner_dispatcher.dispatch`` the web orchestrator uses), writes one result
JSON per (scenario, sdk) to an output directory, and exits non-zero if any
selected job fails or errors. Scenarios that don't support the chosen backend
are reported as ``skip`` and do not fail the run.

Examples:
    # Full matrix against the deterministic mock backend (CI PR gate):
    python scripts/run-matrix.py --backend mock --sdks both --out results/

    # A single language (no divergence comparison makes sense here):
    python scripts/run-matrix.py --sdks python --out results/

    # Against a live account (endpoint/key from env or config/default.yaml):
    COSMOS_ENDPOINT=... COSMOS_KEY=... \
        python scripts/run-matrix.py --backend live --sdks java --out results/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# Reuse the orchestrator's loaders/dispatcher so CI runs exactly what the
# dashboard runs (single source of truth for the runner contract).
sys.path.insert(0, os.path.join(REPO_ROOT, "orchestrator"))

from app import config_resolver, runner_dispatcher, scenario_loader  # noqa: E402

SPECS_DIR = os.path.join(REPO_ROOT, "specs")
MOCK_PROFILE_PATH = os.path.join(SPECS_DIR, "mock-profile.json")
DEFAULTS_PATH = os.path.join(REPO_ROOT, "config", "default.yaml")

SDK_ALIASES = {"both": ["python", "java"], "all": ["python", "java"]}
FAIL_STATUSES = {"fail", "error"}


def parse_sdks(value: str) -> List[str]:
    value = value.strip().lower()
    if value in SDK_ALIASES:
        return list(SDK_ALIASES[value])
    sdks = [s.strip() for s in value.split(",") if s.strip()]
    unknown = [s for s in sdks if s not in runner_dispatcher.RUNNERS]
    if unknown:
        raise SystemExit(
            f"unknown sdk(s): {', '.join(unknown)} "
            f"(known: {', '.join(runner_dispatcher.RUNNERS)})"
        )
    return sdks


def load_mock_profile() -> Dict[str, Any]:
    try:
        with open(MOCK_PROFILE_PATH) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[run-matrix] WARNING: could not load mock profile: {exc}", file=sys.stderr)
        return {}


def default_version(sdk: str) -> str:
    return {"python": "4.9.0", "java": "4.63.0"}.get(sdk, "latest")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--backend", default="mock", choices=["mock", "emulator", "live"],
                    help="backend tier to exercise (default: mock)")
    ap.add_argument("--sdks", default="both",
                    help="comma-separated SDKs or 'both' (default: both)")
    ap.add_argument("--source", default="published", choices=["published", "local"],
                    help="SDK build source: published (Maven Central/PyPI) or local "
                         "(built from an azure-sdk-for-* branch). Default: published")
    ap.add_argument("--out", default="results",
                    help="directory to write per-job result JSON (default: results/)")
    ap.add_argument("--specs", default=SPECS_DIR, help="scenarios directory")
    ap.add_argument("--timeout", type=int, default=120, help="per-job timeout seconds")
    ap.add_argument("--endpoint", help="override endpoint for non-mock backends")
    ap.add_argument("--key", help="override key for non-mock backends")
    args = ap.parse_args(argv)

    sdks = parse_sdks(args.sdks)
    scenarios = scenario_loader.load_scenarios(args.specs)
    if not scenarios:
        print(f"[run-matrix] no scenarios found in {args.specs}", file=sys.stderr)
        return 2

    # Build + resolve config (endpoint/key for emulator/live; mock passes through).
    config: Dict[str, Any] = {"backend": args.backend}
    if args.endpoint:
        config["endpoint"] = args.endpoint
    if args.key:
        config["key"] = args.key
    defaults = config_resolver.load_defaults(DEFAULTS_PATH)
    resolved, err = config_resolver.resolve(config, defaults)
    if err:
        print(f"[run-matrix] {err}", file=sys.stderr)
        return 2
    if args.backend == "mock":
        resolved = {**resolved, "mock_profile": load_mock_profile()}

    os.makedirs(args.out, exist_ok=True)

    print(f"[run-matrix] backend={args.backend} source={args.source} "
          f"sdks={','.join(sdks)} scenarios={len(scenarios)}")

    failures: List[str] = []
    counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}

    for scenario in scenarios:
        sid = str(scenario["id"])
        supported = scenario.get("backends", ["mock"])
        for sdk in sdks:
            if args.backend not in supported:
                result = _skip(scenario, sdk, args.backend)
            else:
                version_label = "local-source" if args.source == "local" else default_version(sdk)
                result = runner_dispatcher.dispatch(
                    sdk, scenario, resolved, version_label,
                    source=args.source, timeout=args.timeout,
                )
            status = result.get("status", "error")
            counts[status] = counts.get(status, 0) + 1
            _write(args.out, sid, sdk, result)
            mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR ", "skip": "skip"}.get(status, status)
            print(f"  [{mark}] #{sid} {sdk:<6} {scenario.get('title','')}")
            if status in FAIL_STATUSES:
                failures.append(f"#{sid} {sdk}: {result.get('error') or status}")

    print(f"\n[run-matrix] summary: pass={counts['pass']} fail={counts['fail']} "
          f"error={counts['error']} skip={counts['skip']}")
    if failures:
        print("[run-matrix] FAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return 0


def _write(out_dir: str, sid: str, sdk: str, result: Dict[str, Any]) -> None:
    path = os.path.join(out_dir, f"{sid}-{sdk}.json")
    with open(path, "w") as fh:
        json.dump(result, fh, indent=2)


def _skip(scenario: Dict[str, Any], sdk: str, backend: str) -> Dict[str, Any]:
    return {
        "scenario_id": str(scenario["id"]),
        "title": scenario.get("title"),
        "sdk": sdk,
        "sdk_version": default_version(sdk),
        "backend": backend,
        "status": "skip",
        "duration_ms": 0,
        "metrics": {},
        "assertions": [],
        "error": None,
        "logs": [f"scenario does not support backend '{backend}'"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
