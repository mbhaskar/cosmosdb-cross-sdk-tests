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

case "$CMD" in
  up)
    echo "==> Starting fault-injection stack (emulator + toxiproxy + mitmproxy)"
    "${DC[@]}" -f "$COMPOSE" up -d
    echo
    echo "==> Waiting for the Cosmos emulator to report ready (vNext boots in ~20-40s)..."
    for i in $(seq 1 60); do
      state="$(docker inspect -f '{{.State.Health.Status}}' cosmos-emulator 2>/dev/null || echo unknown)"
      if [ "$state" = "healthy" ]; then echo "    emulator healthy"; break; fi
      # Belt-and-suspenders: also probe the readiness endpoint from the host.
      if curl -sf http://localhost:8080/ready >/dev/null 2>&1; then echo "    emulator ready"; break; fi
      sleep 3
      if [ "$i" = "60" ]; then echo "    WARN: emulator not ready yet; check 'run-fault-stack.sh logs'"; fi
    done
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
    ;;
  logs)
    "${DC[@]}" -f "$COMPOSE" logs -f --tail=100
    ;;
  *)
    echo "usage: $0 {up|down|status|logs}" >&2
    exit 2
    ;;
esac
