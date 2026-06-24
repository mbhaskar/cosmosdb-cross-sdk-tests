"""Assertion evaluation for scenario steps."""

from __future__ import annotations

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


def evaluate(expectations: List[Dict[str, Any]], result: OpResult, backend: Backend) -> List[Dict[str, Any]]:
    """Return a list of assertion outcomes ({name, passed, detail})."""
    outcomes: List[Dict[str, Any]] = []
    for exp in expectations or []:
        outcomes.append(_evaluate_one(exp, result, backend))
    return outcomes


def _evaluate_one(exp: Dict[str, Any], result: OpResult, backend: Backend) -> Dict[str, Any]:
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

    return outcome(False, f"unknown assertion type '{t}'")
