"""FastAPI orchestrator for the CosmosDB cross-SDK test runner (MVP)."""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config_resolver, proxy_manager, runner_dispatcher, scenario_loader
from .store import Store

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SPECS_DIR = os.path.join(REPO_ROOT, "specs")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DB_PATH = os.environ.get("ORCH_DB", os.path.join(REPO_ROOT, "orchestrator", "results.db"))
MOCK_PROFILE_PATH = os.path.join(SPECS_DIR, "mock-profile.json")
DEFAULTS_PATH = os.path.join(REPO_ROOT, "config", "default.yaml")
DEFAULTS: Dict[str, Any] = config_resolver.load_defaults(DEFAULTS_PATH)


def _load_mock_profile() -> Dict[str, Any]:
    import json
    with open(MOCK_PROFILE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


MOCK_PROFILE: Dict[str, Any] = _load_mock_profile()

app = FastAPI(title="CosmosDB Cross-SDK Test Runner", version="0.1.0")

SCENARIOS: List[Dict[str, Any]] = scenario_loader.load_scenarios(SPECS_DIR)
SCENARIOS_BY_ID: Dict[str, Dict[str, Any]] = {str(s["id"]): s for s in SCENARIOS}
store = Store(DB_PATH)
_executor = ThreadPoolExecutor(max_workers=4)


class SdkSel(BaseModel):
    name: str
    version: str = "latest"
    source: str = "published"


class RunConfig(BaseModel):
    backend: str = "mock"
    connection_mode: str = "direct"
    consistency: str = "Session"
    endpoint: Optional[str] = None
    key: Optional[str] = None
    # Fault-injection proxy wiring (optional; T-3xx scenarios only).
    proxy_endpoint: Optional[str] = None
    toxiproxy_url: Optional[str] = None
    mitm_endpoint: Optional[str] = None


class RunRequest(BaseModel):
    scenarios: List[str] = ["*"]
    sdks: List[SdkSel]
    config: RunConfig = RunConfig()


# --------------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------------- #

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    index_html = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_html):
        return FileResponse(index_html)
    return JSONResponse({"message": "UI not found; API is at /api"})


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/scenarios")
def list_scenarios():
    return scenario_loader.catalog_view(SCENARIOS)


@app.get("/api/scenarios/{scenario_id}")
def get_scenario(scenario_id: str):
    s = SCENARIOS_BY_ID.get(scenario_id)
    if not s:
        raise HTTPException(404, f"scenario {scenario_id} not found")
    return {"scenario": s, "history": store.history(scenario_id)}


@app.get("/api/sdks")
def list_sdks():
    return runner_dispatcher.available_sdks()


@app.get("/api/proxy/profiles")
def list_proxy_profiles():
    """List the declarative Toxiproxy fault profiles (proxy/profiles/*.yaml)."""
    return {"profiles": proxy_manager.list_profiles()}


class ProxyActivateRequest(BaseModel):
    profile: str
    proxy: Optional[str] = None


@app.post("/api/proxy/activate")
def activate_proxy_profile(req: ProxyActivateRequest):
    """Apply a fault profile against the running Toxiproxy stack."""
    try:
        return proxy_manager.activate(req.profile, req.proxy)
    except proxy_manager.ProxyError as exc:
        raise HTTPException(502, str(exc))


@app.post("/api/proxy/clear")
def clear_proxy(proxy: str = "cosmos"):
    """Remove all toxics from a proxy (network heals)."""
    try:
        return proxy_manager.clear(proxy)
    except proxy_manager.ProxyError as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/runs")
def list_runs():
    return store.list_runs()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"run {run_id} not found")
    return run


@app.post("/api/runs")
def create_run(req: RunRequest):
    if req.scenarios == ["*"] or req.scenarios == ["all"]:
        scenario_ids = list(SCENARIOS_BY_ID.keys())
    else:
        scenario_ids = [s for s in req.scenarios if s in SCENARIOS_BY_ID]
    if not scenario_ids:
        raise HTTPException(400, "no valid scenarios selected")
    if not req.sdks:
        raise HTTPException(400, "no sdks selected")

    run_id = "run-" + uuid.uuid4().hex[:8]
    config = req.config.dict()

    # Resolve endpoint/key for non-mock backends from request > env > default.yaml.
    resolved, err = config_resolver.resolve(config, DEFAULTS)
    if err:
        raise HTTPException(400, err)

    sdks = [s.dict() for s in req.sdks]
    # Persist a redacted copy so secrets never land in the results DB.
    store.create_run(run_id, config_resolver.redact(resolved), scenario_ids, sdks)

    threading.Thread(
        target=_execute_run, args=(run_id, scenario_ids, sdks, resolved), daemon=True
    ).start()
    return {"run_id": run_id, "scenarios": scenario_ids, "sdks": sdks}


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def _execute_run(run_id: str, scenario_ids: List[str], sdks: List[Dict], config: Dict) -> None:
    jobs = []
    for sid in scenario_ids:
        scenario = SCENARIOS_BY_ID[sid]
        backend = config.get("backend", "mock")
        for sdk in sdks:
            # Skip scenarios that don't support the selected backend.
            if backend not in scenario.get("backends", ["mock"]):
                store.save_result(run_id, _skipped(
                    scenario, sdk, backend,
                    f"scenario does not support backend '{backend}'"))
                continue
            # Skip scenarios gated to specific SDK runners the selected SDK is not
            # part of (e.g. control-plane / fault-injection scenarios the Java
            # runner does not implement yet). Mirrors scripts/run-matrix.py.
            allowed_runners = scenario.get("runners")
            if allowed_runners and sdk["name"] not in allowed_runners:
                store.save_result(run_id, _skipped(
                    scenario, sdk, backend,
                    f"scenario limited to runners {allowed_runners}"))
                continue
            jobs.append((scenario, sdk))

    job_config = {**config, "run_id": run_id}
    # Inject the shared mock profile once so both runners interpret identical
    # mock semantics (single source of truth: specs/mock-profile.json).
    if config.get("backend", "mock") == "mock":
        job_config["mock_profile"] = MOCK_PROFILE

    futures = {
        _executor.submit(
            runner_dispatcher.dispatch,
            sdk["name"], scenario, job_config, sdk.get("version", "latest"),
            sdk.get("source", "published"),
        ): (scenario, sdk)
        for scenario, sdk in jobs
    }
    for fut in as_completed(futures):
        result = fut.result()
        store.save_result(run_id, result)

    summary = _summarize(run_id)
    overall = "completed"
    store.finish_run(run_id, overall, summary)


def _skipped(scenario: Dict, sdk: Dict, backend: str, reason: str = None) -> Dict[str, Any]:
    reason = reason or f"scenario does not support backend '{backend}'"
    return {
        "scenario_id": str(scenario["id"]),
        "title": scenario.get("title"),
        "sdk": sdk["name"],
        "sdk_version": sdk.get("version", "latest"),
        "backend": backend,
        "status": "skip",
        "duration_ms": 0,
        "metrics": {},
        "assertions": [],
        "error": None,
        "logs": [reason],
    }


def _summarize(run_id: str) -> Dict[str, Any]:
    run = store.get_run(run_id)
    by_sdk: Dict[str, Dict[str, int]] = {}
    for r in run["results"]:
        key = f"{r['sdk']} {r.get('sdk_version', '')}".strip()
        bucket = by_sdk.setdefault(key, {"pass": 0, "fail": 0, "error": 0, "skip": 0})
        bucket[r["status"]] = bucket.get(r["status"], 0) + 1
    return {"by_sdk": by_sdk, "total": len(run["results"])}
