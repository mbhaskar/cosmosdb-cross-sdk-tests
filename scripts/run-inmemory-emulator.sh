#!/usr/bin/env bash
# Up/down helper for the Dockerized Cosmos in-memory emulator host.
#
#   scripts/run-inmemory-emulator.sh up      # build (if needed) + start + wait healthy
#   scripts/run-inmemory-emulator.sh down     # stop + remove
#   scripts/run-inmemory-emulator.sh status   # health + resolved account topology
#   scripts/run-inmemory-emulator.sh logs     # follow logs
#
# Publishes:  http://localhost:49150 (management)   https://localhost:49151 (gateway V1, TLS)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/emulator/inmemory/docker-compose.inmemory.yaml"
MGMT="http://localhost:49150"
GATEWAY="https://localhost:49151"

compose() { docker compose -f "${COMPOSE_FILE}" "$@"; }

# Rebuild the JVM trust store so the Java runner trusts the gateway's self-signed
# TLS leaf. The emulator regenerates that leaf on every container (re)create, so
# the trust store MUST be rebuilt after each `up` or Java inmemory runs fail the
# gateway handshake ("not an SSL/TLS record" / PKIX). Best-effort: skipped (with
# a hint) when the JDK tooling isn't present -- Python runs need no trust store.
rebuild_truststore() {
  local script="${REPO_ROOT}/scripts/build-java-truststore.sh"
  [[ -x "$script" ]] || return 0
  if ! command -v keytool >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
    echo "note: keytool/openssl not on PATH; skipping Java trust store rebuild" >&2
    echo "      (Python inmemory runs work without it; for Java, install a JDK+openssl" >&2
    echo "       then run: EMULATOR_PORT=49151 scripts/build-java-truststore.sh)" >&2
    return 0
  fi
  echo "Rebuilding Java trust store (gateway TLS leaf) ..."
  EMULATOR_HOST=localhost EMULATOR_PORT=49151 "$script" >/dev/null 2>&1 \
    && echo "  trust store: ${REPO_ROOT}/build/java-cosmos-truststore.jks" \
    || echo "  warn: trust store rebuild failed; Java inmemory runs may fail TLS" >&2
}

case "${1:-up}" in
  up)
    compose up -d --build
    echo "Waiting for management /health ..."
    for _ in $(seq 1 60); do
      if curl -sf "${MGMT}/health" >/dev/null 2>&1; then
        echo "Ready. Management: ${MGMT}  Gateway V1: ${GATEWAY} (TLS)"
        curl -s "${MGMT}/health"; echo
        rebuild_truststore
        exit 0
      fi
      sleep 2
    done
    echo "Emulator did not become healthy in time." >&2
    compose logs --tail 50 >&2 || true
    exit 1
    ;;
  down)
    compose down -v
    ;;
  status)
    echo "== /health =="; curl -s "${MGMT}/health"; echo
    echo "== /account =="; curl -s "${MGMT}/account"; echo
    ;;
  logs)
    compose logs -f
    ;;
  *)
    echo "usage: $0 {up|down|status|logs}" >&2
    exit 2
    ;;
esac
