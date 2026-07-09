"""Scenario execution engine."""

from __future__ import annotations

import re
import time
import uuid
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from . import assertions
from .backends import OpResult, make_backend
from .step_handlers import execute_action

_VAR = re.compile(r"\$\{([^}]+)\}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _resolved_sdk_version():
    """The actual azure-cosmos version installed in this interpreter (not the
    label the caller passed). None when the SDK isn't installed (e.g. a
    mock-only environment)."""
    try:
        from importlib import metadata
        return metadata.version("azure-cosmos")
    except Exception:
        return None


def _resolve(value: Any, ctx: Dict[str, Any]) -> Any:
    """Recursively substitute ${...} placeholders inside scenario params."""
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            return str(_lookup(m.group(1), ctx))
        # Whole-string placeholder keeps native type (e.g. numbers).
        whole = _VAR.fullmatch(value)
        if whole:
            return _lookup(whole.group(1), ctx)
        return _VAR.sub(repl, value)
    if isinstance(value, list):
        return [_resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, ctx) for k, v in value.items()}
    return value


def _lookup(expr: str, ctx: Dict[str, Any]) -> Any:
    expr = expr.strip()
    if expr == "uuid":
        return str(uuid.uuid4())
    if expr == "now":
        return _now_iso()
    parts = expr.split(".")
    cur: Any = ctx
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = getattr(cur, p, None)
        if cur is None:
            break
    return cur if cur is not None else ""


class ScenarioRunner:
    def __init__(self, scenario: Dict[str, Any], config: Dict[str, Any], sdk_version: str, log):
        self.scenario = scenario
        self.config = config
        self.sdk_version = sdk_version
        self.log = log
        self.backend = make_backend(config)
        self.run_id = config.get("run_id", uuid.uuid4().hex[:8])
        self.ctx: Dict[str, Any] = {
            "run_id": self.run_id,
            "connection_mode": config.get("connection_mode", "gateway"),
            "config": config,
            "steps": {},
        }
        self.assertion_results: List[Dict[str, Any]] = []
        self.diagnostics: List[Dict[str, Any]] = []
        self.logs: List[str] = []
        # metrics captured after each step id, for span assertions (metric_delta).
        self.metric_snapshots: Dict[str, Dict[str, Any]] = {}
        # timeline events grouped by (when, step_id).
        self.timeline = self._index_timeline(scenario.get("timeline", []))

    def _log(self, msg: str) -> None:
        line = f"[{_now_iso()}] {msg}"
        self.logs.append(line)
        self.log(line)

    def _run_step(self, step: Dict[str, Any]) -> OpResult:
        action = step["action"]
        params = _resolve(step.get("params", {}), self.ctx)
        result = execute_action(self.backend, action, params, self.ctx)
        if step.get("id"):
            self.ctx["steps"][step["id"]] = {
                "ok": result.ok,
                "status_code": result.status_code,
                "item": result.item or {},
                "items": result.items or [],
                "id": (result.item or {}).get("id"),
            }
        outcome = "ok" if result.ok else f"FAILED({result.status_code} {result.error_code})"
        self._log(f"step '{step.get('id', action)}' action={action} -> {outcome}")

        # Snapshot metrics after this step so later metric_delta spans can read it.
        if step.get("id"):
            self.metric_snapshots[step["id"]] = self.backend.metrics.as_dict()

        if result.status_sequence and result.status_sequence != [result.status_code]:
            self._log(f"  observed status sequence: {result.status_sequence}")

        if result.diagnostics is not None:
            self.diagnostics.append({
                "step": step.get("id", action),
                "action": action,
                "status_code": result.status_code,
                "text": json.dumps(result.diagnostics, indent=2),
            })

        for outc in assertions.evaluate(step.get("expect", []), result, self.backend, self.metric_snapshots):
            outc["step"] = step.get("id", action)
            self.assertion_results.append(outc)
            status = "PASS" if outc["passed"] else "FAIL"
            self._log(f"  assert {outc['name']}: {status} {outc['detail']}")
        return result

    def run(self) -> Dict[str, Any]:
        started_at = _now_iso()
        t0 = time.time()
        status = "pass"
        error = None

        fixture = self.scenario.get("fixture")
        try:
            self._setup_fixture(fixture)
            cp = self.scenario.get("control_plane")
            if cp and hasattr(self.backend, "configure_control_plane"):
                self.backend.configure_control_plane(cp)
            for step in self.scenario.get("steps", []):
                sid = step.get("id")
                self._fire_events("before", sid)
                self._run_step(step)
                self._fire_events("after", sid)
            if any(not a["passed"] for a in self.assertion_results):
                status = "fail"
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"ERROR {error}")
        finally:
            self._teardown_fixture(fixture)

        duration_ms = int((time.time() - t0) * 1000)
        return {
            "scenario_id": str(self.scenario.get("id")),
            "title": self.scenario.get("title"),
            "sdk": "python",
            "sdk_version": self.sdk_version,
            "sdk_source": self.config.get("sdk_source", "published"),
            "resolved_sdk_version": _resolved_sdk_version(),
            "backend": self.config.get("backend", "mock"),
            "status": status,
            "duration_ms": duration_ms,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "metrics": self.backend.metrics.as_dict(),
            "assertions": self.assertion_results,
            "diagnostics": self.diagnostics,
            "error": error,
            "logs": self.logs,
        }

    # -- timeline / control-plane events ----------------------------------- #

    @staticmethod
    def _index_timeline(timeline: List[Dict[str, Any]]) -> Dict:
        """Group timeline events by (when, step_id). Each entry may use ``after``
        or ``before`` to name the anchor step."""
        idx: Dict = {}
        for ev in timeline or []:
            when = "before" if "before" in ev else "after"
            anchor = ev.get(when)
            idx.setdefault((when, anchor), []).append(ev)
        return idx

    def _fire_events(self, when: str, step_id) -> None:
        if not step_id:
            return
        for ev in self.timeline.get((when, step_id), []):
            event = ev["event"]
            args = ev.get("args", {})
            if not hasattr(self.backend, "control_event"):
                self._log(f"  timeline: backend ignores control event '{event}'")
                continue
            self.backend.control_event(
                event, args,
                db_id=self.ctx.get("db"), container_id=self.ctx.get("container"),
            )
            self._log(f"  timeline[{when} {step_id}]: {event} {args or ''}")

    # -- fixture lifecycle ------------------------------------------------- #

    def _setup_fixture(self, fixture) -> None:
        if not fixture:
            return
        db_id = fixture.get("database", "auto")
        if db_id == "auto":
            db_id = f"mvp-{self.scenario.get('id')}-{self.run_id}"
        self.ctx["db"] = db_id
        # An eager client is created so bootstrap metrics are populated.
        self.backend.create_client(connection_mode=self.ctx["connection_mode"])
        self.backend.create_database(db_id, create_if_not_exists=True)
        cont = fixture.get("container")
        if cont:
            self.ctx["container"] = cont["id"]
            self.backend.create_container(
                db_id, cont["id"], cont.get("partition_key", "/pk"),
                create_if_not_exists=True,
            )
        self._log(f"fixture ready: db={db_id} container={self.ctx.get('container')}")

    def _teardown_fixture(self, fixture) -> None:
        if not fixture or not self.ctx.get("db"):
            return
        try:
            self.backend.delete_database(self.ctx["db"])
            self._log(f"fixture cleaned up: db={self.ctx['db']}")
        except Exception as exc:  # noqa: BLE001
            self._log(f"fixture cleanup warning: {exc}")
