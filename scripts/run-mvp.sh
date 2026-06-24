#!/usr/bin/env bash
# One-command MVP startup: installs Python deps, builds the Java runner (if
# Maven is available), and launches the orchestrator + dashboard.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8077}"

echo "==> Installing orchestrator dependencies"
python3 -m pip install --quiet --user -r orchestrator/requirements.txt
python3 -m pip install --quiet --user -r harness/python/requirements.txt || true

if command -v mvn >/dev/null 2>&1; then
  if [ ! -f harness/java/target/cosmos-test-runner.jar ]; then
    echo "==> Building Java runner (first build downloads the SDK; be patient)"
    (cd harness/java && mvn -q -DskipTests package)
  else
    echo "==> Java runner already built"
  fi
else
  echo "==> Maven not found; skipping Java runner (Python-only matrix)"
fi

echo "==> Starting orchestrator on http://127.0.0.1:${PORT}"
export PATH="$HOME/Library/Python/3.9/bin:$HOME/.local/bin:$PATH"
cd "$ROOT/orchestrator"
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}"
