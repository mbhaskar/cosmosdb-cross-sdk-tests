"""Loads YAML scenario specs into an in-memory catalog."""

from __future__ import annotations

import glob
import os
from typing import Any, Dict, List

import yaml


def load_scenarios(specs_dir: str) -> List[Dict[str, Any]]:
    scenarios: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(specs_dir, "**", "*.yaml"), recursive=True)):
        if os.path.basename(path) == "schema.yaml":
            continue
        try:
            doc = yaml.safe_load(open(path))
        except Exception as exc:  # noqa: BLE001
            print(f"[scenario_loader] skipping {path}: {exc}")
            continue
        if not isinstance(doc, dict) or "id" not in doc:
            continue
        doc["_path"] = os.path.relpath(path, specs_dir)
        doc.setdefault("backends", ["mock", "emulator", "live"])
        scenarios.append(doc)

    scenarios.sort(key=lambda d: _id_sort_key(d["id"]))
    return scenarios


def _id_sort_key(scenario_id: Any):
    s = str(scenario_id)
    return (0, int(s)) if s.isdigit() else (1, s)


def catalog_view(scenarios: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Slim representation for the matrix UI."""
    return [
        {
            "id": str(s["id"]),
            "phase": s.get("phase"),
            "section": s.get("section"),
            "title": s.get("title"),
            "tags": s.get("tags", []),
            "backends": s.get("backends", []),
            "path": s.get("_path"),
        }
        for s in scenarios
    ]
