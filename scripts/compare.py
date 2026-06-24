#!/usr/bin/env python3
"""Cross-SDK comparison report generator.

Reads runner result JSON files (one per scenario/SDK, as emitted by the runners
or exported from the orchestrator) and produces a Markdown comparison report
that highlights behavioral divergences between SDKs.

Usage:
    python scripts/compare.py results/*.json
    python scripts/compare.py --run run-abc123 --db orchestrator/results.db
    python scripts/compare.py results/ -o report.md
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List

ICON = {"pass": "✅", "fail": "❌", "error": "⚠️", "skip": "—"}


def load_from_files(paths: List[str]) -> List[Dict[str, Any]]:
    results = []
    expanded: List[str] = []
    for p in paths:
        if os.path.isdir(p):
            expanded += glob.glob(os.path.join(p, "**", "*.json"), recursive=True)
        else:
            expanded += glob.glob(p)
    for f in expanded:
        try:
            doc = json.load(open(f))
        except Exception as exc:  # noqa: BLE001
            print(f"skip {f}: {exc}", file=sys.stderr)
            continue
        results.extend(doc if isinstance(doc, list) else [doc])
    return results


def load_from_db(db_path: str, run_id: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT result FROM run_results WHERE run_id=?", (run_id,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def find_divergences(results: List[Dict[str, Any]]) -> List[str]:
    """Scenario ids where the participating SDKs disagree on status.

    A divergence requires at least two SDKs to have produced a (non-missing)
    result for the scenario, and for those statuses to differ.
    """
    sdks = sorted({f"{r['sdk']} {r.get('sdk_version','')}".strip() for r in results})
    by_scenario: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for r in results:
        sdk = f"{r['sdk']} {r.get('sdk_version','')}".strip()
        by_scenario[r["scenario_id"]][sdk] = r

    divergent = []
    for scid in by_scenario:
        statuses = {by_scenario[scid][s]["status"] for s in sdks if s in by_scenario[scid]}
        if len(statuses) > 1:
            divergent.append(scid)
    return sorted(divergent, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))


def build_report(results: List[Dict[str, Any]]) -> str:
    sdks = sorted({f"{r['sdk']} {r.get('sdk_version','')}".strip() for r in results})
    by_scenario: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    titles: Dict[str, str] = {}
    for r in results:
        sdk = f"{r['sdk']} {r.get('sdk_version','')}".strip()
        by_scenario[r["scenario_id"]][sdk] = r
        titles[r["scenario_id"]] = r.get("title", "")

    # Summary
    tally: Dict[str, Dict[str, int]] = {s: defaultdict(int) for s in sdks}
    for r in results:
        sdk = f"{r['sdk']} {r.get('sdk_version','')}".strip()
        tally[sdk][r["status"]] += 1

    lines = ["# Cross-SDK Test Comparison Report", ""]
    lines.append("## Summary")
    lines.append("")
    lines.append("| SDK | ✅ Pass | ❌ Fail | ⚠️ Error | — Skip |")
    lines.append("|-----|------|------|-------|------|")
    for s in sdks:
        t = tally[s]
        lines.append(f"| {s} | {t['pass']} | {t['fail']} | {t['error']} | {t['skip']} |")
    lines.append("")

    # Divergences
    divergent = find_divergences(results)

    lines.append("## Behavioral Divergences")
    lines.append("")
    if not divergent:
        lines.append("_No divergences — all SDKs agree on every scenario._")
    else:
        header = "| # | Scenario | " + " | ".join(sdks) + " |"
        lines.append(header)
        lines.append("|" + "---|" * (len(sdks) + 2))
        for scid in divergent:
            cells = []
            for s in sdks:
                r = by_scenario[scid].get(s)
                if not r:
                    cells.append("—")
                else:
                    cells.append(f"{ICON.get(r['status'],'')} {r['status']} ({r.get('duration_ms','?')}ms)")
            lines.append(f"| {scid} | {titles.get(scid,'')} | " + " | ".join(cells) + " |")
    lines.append("")

    # Full matrix
    lines.append("## Full Matrix")
    lines.append("")
    lines.append("| # | Scenario | " + " | ".join(sdks) + " |")
    lines.append("|" + "---|" * (len(sdks) + 2))
    for scid in sorted(by_scenario, key=lambda x: (0, int(x)) if x.isdigit() else (1, x)):
        cells = []
        for s in sdks:
            r = by_scenario[scid].get(s)
            cells.append(f"{ICON.get(r['status'],'—')}" if r else "—")
        lines.append(f"| {scid} | {titles.get(scid,'')} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="result JSON files or directories")
    ap.add_argument("--run", help="orchestrator run id (use with --db)")
    ap.add_argument("--db", default="orchestrator/results.db", help="orchestrator SQLite db")
    ap.add_argument("-o", "--output", help="write report to file instead of stdout")
    ap.add_argument("--fail-on-divergence", action="store_true",
                    help="exit non-zero if any scenario's SDKs disagree on status "
                         "(no-op when fewer than two SDKs are present)")
    args = ap.parse_args(argv)

    if args.run:
        results = load_from_db(args.db, args.run)
    elif args.paths:
        results = load_from_files(args.paths)
    else:
        ap.error("provide result paths or --run <id>")

    if not results:
        print("no results found", file=sys.stderr)
        return 1

    report = build_report(results)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(report)
        print(f"wrote {args.output}")
    else:
        print(report)

    if args.fail_on_divergence:
        sdk_count = len({f"{r['sdk']} {r.get('sdk_version','')}".strip() for r in results})
        if sdk_count < 2:
            print("[compare] only one SDK present; divergence check skipped.",
                  file=sys.stderr)
        else:
            divergent = find_divergences(results)
            if divergent:
                print(f"[compare] DIVERGENCE: {len(divergent)} scenario(s) where SDKs "
                      f"disagree: {', '.join(divergent)}", file=sys.stderr)
                return 1
            print("[compare] no divergences — all SDKs agree.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
