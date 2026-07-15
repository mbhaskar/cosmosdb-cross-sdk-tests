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


def _parse_duration(spec: Any) -> "float | None":
    """Parse a loop duration like '30s', '500ms', '2m' into seconds. Returns None
    when not provided."""
    if spec is None:
        return None
    if isinstance(spec, (int, float)):
        return float(spec)
    s = str(spec).strip().lower()
    try:
        if s.endswith("ms"):
            return float(s[:-2]) / 1000.0
        if s.endswith("s"):
            return float(s[:-1])
        if s.endswith("m"):
            return float(s[:-1]) * 60.0
        return float(s)
    except ValueError:
        return None


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
        # When a scenario opts into transport-level fault injection and a proxy
        # endpoint is configured, point the SDK client at Toxiproxy instead of the
        # backend directly so injected toxics are on the wire.
        self.fault_injection = scenario.get("fault_injection")
        fi = self.fault_injection if isinstance(self.fault_injection, dict) else {}
        # Route the SDK client through the proxy chain when a scenario opts into
        # fault injection. For L7 (protocol / mitmproxy) scenarios, prefer the
        # mitm endpoint, which fronts Toxiproxy so both tiers apply; otherwise use
        # the Toxiproxy endpoint. No-op when nothing is configured (talks direct).
        if self.fault_injection and config.get("backend") != "mock":
            if fi.get("protocol"):
                proxy_ep = config.get("mitm_endpoint") or config.get("proxy_endpoint")
            else:
                proxy_ep = config.get("proxy_endpoint")
            if proxy_ep:
                config = {**config, "endpoint": proxy_ep}
                # Keep the client pinned to the proxy endpoint. Without this, the
                # SDK's gateway endpoint discovery adopts the address the emulator
                # self-advertises (localhost:8081) for data-plane requests and
                # bypasses the proxy, so injected toxics never hit the wire.
                # Multi-region failover scenarios need discovery ON (and a real
                # multi-region account), so leave it alone there.
                if not fi.get("multi_region"):
                    config["enable_endpoint_discovery"] = False
                self.config = config
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
        # latency (ms) and resource samples keyed by scope (loop id) for
        # latency_percentile / resource_stable assertions.
        self.latency_samples: Dict[str, List[float]] = {}
        self.resource_samples: Dict[str, List[float]] = {}
        # timeline events grouped by (when, step_id).
        self.timeline = self._index_timeline(scenario.get("timeline", []))
        # Lazily-built Toxiproxy controller for net_* / region_* timeline verbs.
        self.fault_controller = self._make_fault_controller()
        # Lazily-built mitmproxy controller for L7 protocol verbs (throttle window).
        self.protocol_controller = self._make_protocol_controller()

    def _make_fault_controller(self):
        if not self.fault_injection or self.config.get("backend") == "mock":
            return None
        try:
            from .faults import ProxyFaultController
        except Exception:  # noqa: BLE001
            return None
        fi = self.fault_injection if isinstance(self.fault_injection, dict) else {}
        return ProxyFaultController(
            admin_url=self.config.get("toxiproxy_url"),
            proxy=fi.get("proxy", "cosmos"),
            secondary_proxy=fi.get("secondary_proxy", "cosmos-secondary"),
        )

    def _make_protocol_controller(self):
        if not self.fault_injection or self.config.get("backend") == "mock":
            return None
        try:
            from .faults import ProtocolFaultController
        except Exception:  # noqa: BLE001
            return None
        # Control channel lives on the mitm endpoint (falls back to the proxy /
        # SDK endpoint, since the addon serves /__fault/* on the same port).
        endpoint = (self.config.get("mitm_endpoint")
                    or self.config.get("proxy_endpoint")
                    or self.config.get("endpoint"))
        return ProtocolFaultController(control_endpoint=endpoint)

    def _log(self, msg: str) -> None:
        line = f"[{_now_iso()}] {msg}"
        self.logs.append(line)
        self.log(line)

    def _run_step(self, step: Dict[str, Any], scope: str = None,
                  evaluate: bool = True) -> OpResult:
        action = step["action"]
        params = _resolve(step.get("params", {}), self.ctx)
        t_step = time.time()
        result = execute_action(self.backend, action, params, self.ctx)
        elapsed_ms = (time.time() - t_step) * 1000.0
        if scope:
            self.latency_samples.setdefault(scope, []).append(elapsed_ms)
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

        if evaluate:
            self._evaluate(step, result)
        return result

    def _evaluate(self, step: Dict[str, Any], result: OpResult, scope: str = None) -> None:
        ctx = {"latency": self.latency_samples, "resource": self.resource_samples, "scope": scope}
        for outc in assertions.evaluate(step.get("expect", []), result, self.backend,
                                        self.metric_snapshots, ctx):
            outc["step"] = step.get("id", step.get("action"))
            self.assertion_results.append(outc)
            status = "PASS" if outc["passed"] else "FAIL"
            self._log(f"  assert {outc['name']}: {status} {outc['detail']}")

    def _run_loop(self, step: Dict[str, Any]) -> None:
        """Repeat nested steps count times or for a duration. Records per-iteration
        latency + resource samples under the loop's scope, then evaluates the
        loop's own expect (e.g. latency_percentile / resource_stable)."""
        scope = step.get("id", "loop")
        inner = step.get("steps", [])
        count = step.get("count")
        duration = _parse_duration(step.get("duration"))
        last: OpResult = OpResult(ok=True)
        started = time.time()
        i = 0
        while True:
            if count is not None and i >= int(count):
                break
            if duration is not None and (time.time() - started) >= duration:
                break
            if count is None and duration is None:
                break
            for sub in inner:
                last = self._run_step(sub, scope=scope, evaluate=True)
            # Sample a cheap resource signal per iteration (connection count is a
            # good "no connection storm / no leak" proxy in gateway mode).
            self.resource_samples.setdefault(scope, []).append(
                float(self.backend.metrics.as_dict().get("connections_opened", 0)))
            i += 1
        self._log(f"loop '{scope}' ran {i} iteration(s)")
        self._evaluate(step, last, scope=scope)

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
                if step.get("action") == "loop":
                    self._run_loop(step)
                else:
                    self._run_step(step)
                self._fire_events("after", sid)
            if any(not a["passed"] for a in self.assertion_results):
                status = "fail"
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"ERROR {error}")
        finally:
            if self.fault_controller is not None:
                try:
                    self.fault_controller.reset()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"fault reset warning: {exc}")
            if self.protocol_controller is not None:
                try:
                    self.protocol_controller.reset()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"protocol reset warning: {exc}")
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

    _TRANSPORT_EVENTS = {"net_latency", "net_timeout", "net_reset", "net_bandwidth",
                         "net_slow_close", "region_down", "region_up", "reset_faults"}
    _PROTOCOL_EVENTS = {"net_throttle_window", "throttle_window_clear"}

    def _fire_events(self, when: str, step_id) -> None:
        if not step_id:
            return
        for ev in self.timeline.get((when, step_id), []):
            event = ev["event"]
            args = ev.get("args", {})
            # L7 protocol faults (mitmproxy: throttle window / 429s).
            if event in self._PROTOCOL_EVENTS:
                if self.protocol_controller is None:
                    self._log(f"  timeline[{when} {step_id}]: '{event}' skipped "
                              f"(no protocol controller; needs emulator/live + mitmproxy)")
                    continue
                self.protocol_controller.apply(event, args)
                self._log(f"  timeline[{when} {step_id}]: {event} {args or ''}")
                continue
            # L4 transport faults (Toxiproxy) vs mock control-plane faults.
            if event in self._TRANSPORT_EVENTS:
                if self.fault_controller is None:
                    self._log(f"  timeline[{when} {step_id}]: '{event}' skipped "
                              f"(no fault controller; needs emulator/live + Toxiproxy)")
                    continue
                self.fault_controller.apply(event, args)
                self._log(f"  timeline[{when} {step_id}]: {event} {args or ''}")
                continue
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
            # Namespace the auto db per SDK so parallel Python/Java runs of the
            # same scenario don't share a database (and collide on hardcoded item
            # ids). Falls back to "python" for standalone CLI use.
            sdk = str(self.config.get("sdk", "python"))
            db_id = f"mvp-{self.scenario.get('id')}-{sdk}-{self.run_id}"
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
