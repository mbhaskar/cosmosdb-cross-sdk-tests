#!/usr/bin/env bash
# Bring the fault-injection stack (Cosmos emulator + Toxiproxy + mitmproxy) up or
# down. This is SEPARATE from the portal (scripts/run-mvp.sh): the portal serves
# the dashboard and runs mock scenarios with no infra, while the T-3xx
# fault-injection scenarios (backends: [emulator, live]) need this Docker stack.
#
# Usage:
#   scripts/run-fault-stack.sh up       # start emulator + toxiproxy + mitmproxy
#   scripts/run-fault-stack.sh down      # stop and remove the stack
#   scripts/run-fault-stack.sh status    # show container + proxy health
#   scripts/run-fault-stack.sh logs      # tail stack logs
#
# Once "up" reports healthy, run T-3xx from the portal (select the emulator
# backend + Python runner) or via:
#   python scripts/run-matrix.py --backend emulator \
#     --specs specs/phase06-fault-injection --sdks python
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="$ROOT/proxy/docker-compose.proxy.yaml"
CMD="${1:-up}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found. The fault-injection stack requires Docker Desktop." >&2
  echo "       (T-3xx scenarios cannot run without it; mock scenarios need no infra.)" >&2
  exit 1
fi

# Prefer 'docker compose' (v2); fall back to legacy 'docker-compose'.
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

wait_for_url() {
  local label="$1" url="$2" attempts="${3:-60}"
  for i in $(seq 1 "$attempts"); do
    if curl -kfsS "$url" >/dev/null 2>&1; then
      echo "    ${label} ready"
      return 0
    fi
    sleep 3
  done
  echo "ERROR: ${label} did not become ready at ${url}" >&2
  "${DC[@]}" -f "$COMPOSE" ps >&2 || true
  "${DC[@]}" -f "$COMPOSE" logs --tail=100 >&2 || true
  return 1
}

case "$CMD" in
  up)
    echo "==> Starting fault-injection stack (emulator + toxiproxy + mitmproxy)"
    "${DC[@]}" -f "$COMPOSE" up -d
    echo
    echo "==> Waiting for emulator + fault proxies..."
    wait_for_url "Cosmos emulator" "http://localhost:8080/ready"
    wait_for_url "Toxiproxy" "http://localhost:8474/proxies"
    wait_for_url "mitmproxy" "https://localhost:18091/__fault/status"
    echo
    echo "==> Endpoints"
    echo "    SDK (L7+L4 chain):  https://localhost:18091   (mitmproxy)"
    echo "    SDK (L4 only):      https://localhost:18081   (toxiproxy 'cosmos')"
    echo "    Toxiproxy admin:    http://localhost:8474"
    echo "    Emulator direct:    https://localhost:8081    (gateway, HTTPS)"
    echo "    Emulator health:    http://localhost:8080/ready"
    echo "    Data Explorer:      http://localhost:1234"
    echo
    echo "    Now run T-3xx from the portal (emulator backend, Python runner) or:"
    echo "      python scripts/run-matrix.py --backend emulator \\"
    echo "        --specs specs/phase06-fault-injection --sdks python"
    ;;
  down)
    echo "==> Stopping fault-injection stack"
    "${DC[@]}" -f "$COMPOSE" down
    ;;
  status)
    "${DC[@]}" -f "$COMPOSE" ps
    echo
    echo "-- Toxiproxy proxies --"
    curl -sf http://localhost:8474/proxies 2>/dev/null | python3 -m json.tool 2>/dev/null \
      || echo "   (Toxiproxy admin not reachable; is the stack up?)"
    echo
    echo "-- mitmproxy fault engine --"
    curl -ksf https://localhost:18091/__fault/status 2>/dev/null \
      || echo "   (mitmproxy control endpoint not reachable; is the stack up?)"
    ;;
  logs)
    "${DC[@]}" -f "$COMPOSE" logs -f --tail=100
    ;;
  *)
    echo "usage: $0 {up|down|status|logs}" >&2
    exit 2
    ;;
esac
