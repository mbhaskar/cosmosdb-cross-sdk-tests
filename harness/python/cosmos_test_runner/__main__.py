"""CLI entry point for the Python test runner.

Reads a JSON job from --input (or stdin), executes the scenario, and writes the
result JSON to --output (or stdout). Human-readable logs go to stderr so stdout
stays a clean JSON document for the orchestrator to parse.

Job schema:
  { "scenario": {...}, "config": {...}, "sdk_version": "4.9.0" }
"""

from __future__ import annotations

import argparse
import json
import sys

from .executor import ScenarioRunner


def _stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cosmos_test_runner")
    parser.add_argument("--input", help="Path to job JSON (default: stdin)")
    parser.add_argument("--output", help="Path to write result JSON (default: stdout)")
    args = parser.parse_args(argv)

    raw = open(args.input).read() if args.input else sys.stdin.read()
    job = json.loads(raw)

    config = job.get("config", {})
    # Surface the requested SDK source (published|local) to the executor.
    config["sdk_source"] = job.get("sdk_source", "published")

    runner = ScenarioRunner(
        scenario=job["scenario"],
        config=config,
        sdk_version=job.get("sdk_version", "unknown"),
        log=_stderr_log,
    )
    result = runner.run()

    out = json.dumps(result, indent=2, default=str)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(out)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
