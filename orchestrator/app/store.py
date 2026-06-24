"""SQLite persistence for runs and per-(scenario, sdk) results."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
    config      TEXT NOT NULL,
    scenarios   TEXT NOT NULL,
    sdks        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    completed_at REAL,
    summary     TEXT
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id      TEXT NOT NULL,
    scenario_id TEXT NOT NULL,
    sdk_name    TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    status      TEXT NOT NULL,
    duration_ms INTEGER,
    result      TEXT,
    created_at  REAL NOT NULL,
    PRIMARY KEY (run_id, scenario_id, sdk_name, sdk_version)
);
CREATE INDEX IF NOT EXISTS idx_results_scenario ON run_results(scenario_id);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- runs -------------------------------------------------------------- #

    def create_run(self, run_id: str, config: Dict, scenarios: List[str], sdks: List[Dict]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (id, status, config, scenarios, sdks, created_at) VALUES (?,?,?,?,?,?)",
                (run_id, "running", json.dumps(config), json.dumps(scenarios), json.dumps(sdks), time.time()),
            )
            self._conn.commit()

    def finish_run(self, run_id: str, status: str, summary: Dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=?, completed_at=?, summary=? WHERE id=?",
                (status, time.time(), json.dumps(summary), run_id),
            )
            self._conn.commit()

    def save_result(self, run_id: str, result: Dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO run_results
                   (run_id, scenario_id, sdk_name, sdk_version, status, duration_ms, result, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run_id, str(result["scenario_id"]), result["sdk"], result.get("sdk_version", ""),
                    result["status"], result.get("duration_ms"), json.dumps(result), time.time(),
                ),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            results = self._conn.execute(
                "SELECT result FROM run_results WHERE run_id=?", (run_id,)
            ).fetchall()
        run = dict(row)
        for key in ("config", "scenarios", "sdks", "summary"):
            run[key] = json.loads(run[key]) if run.get(key) else None
        run["results"] = [json.loads(r["result"]) for r in results]
        return run

    def list_runs(self, limit: int = 25) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, status, created_at, completed_at, summary FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
            out.append(d)
        return out

    def history(self, scenario_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT run_id, sdk_name, sdk_version, status, duration_ms, created_at
                   FROM run_results WHERE scenario_id=? ORDER BY created_at DESC LIMIT 100""",
                (scenario_id,),
            ).fetchall()
        return [dict(r) for r in rows]
