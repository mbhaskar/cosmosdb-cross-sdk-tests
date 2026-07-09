"""Assertion evaluation for scenario steps."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .backends import Backend, OpResult


def _navigate(result: OpResult, path: str) -> Any:
    """Resolve a dotted path against an OpResult (e.g. 'item.total', 'items.0.id')."""
    root: Dict[str, Any] = {
        "item": result.item,
        "items": result.items,
        "status_code": result.status_code,
        "error_code": result.error_code,
        "ok": result.ok,
    }
    cur: Any = root
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _metric(backend: Backend, name: str) -> Any:
    cur: Any = backend.metrics.as_dict()
    for part in name.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def evaluate(expectations: List[Dict[str, Any]], result: OpResult, backend: Backend,
             snapshots: Dict[str, Dict[str, Any]] = None,
             context: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    """Return a list of assertion outcomes ({name, passed, detail}).

    ``snapshots`` maps step id -> metrics dict captured after that step ran; it
    powers span assertions like ``metric_delta``. ``context`` carries latency /
    resource sample series (keyed by scope) and the current scope, for the
    transport-fault assertions (latency_percentile / resource_stable).
    """
    outcomes: List[Dict[str, Any]] = []
    for exp in expectations or []:
        outcomes.append(_evaluate_one(exp, result, backend, snapshots or {}, context or {}))
    return outcomes


def _metric_from(snapshot: Dict[str, Any], path: str) -> Any:
    cur: Any = snapshot
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _evaluate_one(exp: Dict[str, Any], result: OpResult, backend: Backend,
                  snapshots: Dict[str, Dict[str, Any]],
                  context: Dict[str, Any]) -> Dict[str, Any]:
    t = exp.get("type")
    name = exp.get("name") or t

    def outcome(passed: bool, detail: str = "") -> Dict[str, Any]:
        return {"name": name, "passed": bool(passed), "detail": detail}

    if t in ("ok", "no_error"):
        return outcome(result.ok, f"status={result.status_code}" if not result.ok else "")
    if t == "error":
        return outcome(not result.ok, "expected failure but op succeeded" if result.ok else f"got {result.status_code}")
    if t == "status_code":
        return outcome(result.status_code == exp["value"], f"actual={result.status_code}")
    if t == "error_status":
        return outcome((not result.ok) and result.status_code == exp["value"],
                       f"ok={result.ok} status={result.status_code}")
    if t == "item_count":
        actual = len(result.items or [])
        return outcome(actual == exp["value"], f"actual={actual}")
    if t == "count_gte":
        actual = len(result.items or [])
        return outcome(actual >= exp["value"], f"actual={actual}")
    if t == "field_equals":
        actual = _navigate(result, exp["path"])
        return outcome(str(actual) == str(exp["value"]), f"actual={actual!r}")
    if t == "metric_equals":
        actual = _metric(backend, exp["name_path"] if "name_path" in exp else exp["metric"])
        return outcome(actual == exp["value"], f"actual={actual!r}")
    if t == "metric_zero":
        actual = _metric(backend, exp["metric"])
        return outcome(actual == 0, f"actual={actual!r}")

    # --- control-plane assertions ---------------------------------------- #
    if t == "metric_delta":
        # Compare a metric between two step snapshots: value == snap[b] - snap[a].
        over = exp.get("over") or []
        if len(over) != 2:
            return outcome(False, "metric_delta requires over: [stepA, stepB]")
        step_a, step_b = over
        snap_a, snap_b = snapshots.get(step_a), snapshots.get(step_b)
        if snap_a is None or snap_b is None:
            return outcome(False, f"missing snapshot(s) for {over} (have {list(snapshots)})")
        a = _metric_from(snap_a, exp["metric"]) or 0
        b = _metric_from(snap_b, exp["metric"]) or 0
        delta = b - a
        return outcome(delta == exp["value"], f"delta={delta} (a={a} b={b})")
    if t == "sequence":
        of = exp.get("of", "status_codes")
        actual = list(result.status_sequence if of == "status_codes" else result.metadata_events)
        if "equals" in exp:
            want = exp["equals"]
            return outcome(actual == want, f"actual={actual} want={want}")
        if "contains_in_order" in exp:
            want = exp["contains_in_order"]
            ok = _contains_in_order(actual, want)
            return outcome(ok, f"actual={actual} expected_subsequence={want}")
        return outcome(False, "sequence needs 'equals' or 'contains_in_order'")
    if t == "page_size_at_most":
        actual = len(result.items or [])
        return outcome(actual <= exp["value"], f"actual={actual}")

    # --- transport-fault assertions (emulator/live tier) ----------------- #
    if t == "eventually_ok":
        # Op succeeded (after any injected transport fault was cleared / retried).
        return outcome(result.ok, f"status={result.status_code}" if not result.ok else "")
    if t == "retry_count_gte":
        actual = _metric(backend, "retries")
        return outcome((actual or 0) >= exp["value"], f"retries={actual}")
    if t == "latency_percentile":
        scope = exp.get("scope") or context.get("scope")
        samples = (context.get("latency") or {}).get(scope) or []
        if not samples:
            return outcome(False, f"no latency samples for scope {scope!r}")
        p = _percentile(samples, exp.get("percentile", 99))
        return outcome(p <= exp["max_ms"], f"p{exp.get('percentile', 99)}={p:.1f}ms n={len(samples)}")
    if t == "resource_stable":
        scope = exp.get("scope") or context.get("scope")
        samples = (context.get("resource") or {}).get(scope) or []
        if len(samples) < 2:
            return outcome(False, f"insufficient resource samples for scope {scope!r}")
        lo, hi = min(samples), max(samples)
        drift = 0.0 if hi == 0 else (hi - lo) / hi * 100.0
        return outcome(drift <= exp.get("tolerance_pct", 10),
                       f"drift={drift:.1f}% (min={lo} max={hi} n={len(samples)})")
    if t == "failover_region":
        # Best-effort: the contacted region is surfaced in SDK diagnostics headers
        # on emulator/live. Offline/mock has no diagnostics, so this fails clearly.
        want = str(exp.get("equals", ""))
        blob = json.dumps(result.diagnostics or {})
        return outcome(want and want in blob, f"want_region={want!r} present={want in blob}")
    if t == "pages_cover_set":
        ids = [str((r or {}).get("id")) for r in (result.items or []) if isinstance(r, dict)]
        unique = set(ids)
        no_dupes = len(ids) == len(unique)
        expected = exp.get("expected_count")
        covers = expected is None or len(unique) == expected
        return outcome(no_dupes and covers,
                       f"n={len(ids)} unique={len(unique)} expected={expected} no_dupes={no_dupes}")

    return outcome(False, f"unknown assertion type '{t}'")


def _percentile(samples: List[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 >= len(ordered):
        return ordered[-1]
    return ordered[lo] + (ordered[lo + 1] - ordered[lo]) * frac


def _contains_in_order(actual: List[Any], want: List[Any]) -> bool:
    """True if ``want`` appears as an ordered (not necessarily contiguous) subsequence."""
    it = iter(actual)
    return all(any(x == w for x in it) for w in want)
