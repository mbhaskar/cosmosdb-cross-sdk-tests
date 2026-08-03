#!/usr/bin/env bash
# Entry point for the Dockerized Cosmos in-memory emulator host.
#
# The emulator binds every listener to 127.0.0.1 (enforced by the host), so
# Docker published ports (DNAT'd to the container's eth0 IP, not loopback) must
# be relayed in. Two relay roles:
#
#   * Management port (control-plane REST): raw L4 relay via socat.
#   * Gateway V1 port (data-plane JSON REST): an L7 reverse proxy that ALSO
#     strips the trailing slash the stock Cosmos SDKs append to every resource
#     path (the emulator's Gateway V1 rejects trailing slashes). See
#     cosmos_gateway_proxy.py.
#
# All relays bind the container eth0 IP (NOT 0.0.0.0) so they do not overlap the
# emulator's own 127.0.0.1:<port> sockets.
set -euo pipefail

CONFIG="${EMULATOR_CONFIG:-/etc/cosmos-emulator/account.json}"
# Must match config/account.json.
MGMT_PORT="${EMULATOR_MGMT_PORT:-49150}"
GATEWAY_PORTS="${EMULATOR_GATEWAY_PORTS:-49151}"

# First non-loopback container IP (eth0).
set -- $(hostname -I)
CONTAINER_IP="${1:-}"

# Self-signed TLS leaf for the gateway proxy. The Cosmos Java SDK forces HTTPS
# for its gateway account handshake, so the data-plane proxy must terminate TLS.
# SANs cover both the configured host (localhost) and the emulator's advertised
# loopback IP (127.0.0.1). Regenerated each start; clients trust it via
# connection_verify=off (Python) or an injected JVM trust store (Java, built by
# scripts/build-java-truststore.sh scraping this leaf off the running gateway).
TLS_DIR="/etc/cosmos-emulator/tls"
TLS_CERT="${TLS_DIR}/gateway-cert.pem"
TLS_KEY="${TLS_DIR}/gateway-key.pem"
mkdir -p "${TLS_DIR}"
if [ ! -s "${TLS_CERT}" ] || [ ! -s "${TLS_KEY}" ]; then
  echo "[entrypoint] generating self-signed gateway TLS leaf (CN=localhost)" >&2
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "${TLS_KEY}" -out "${TLS_CERT}" \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" >/dev/null 2>&1
fi

if [ -n "${CONTAINER_IP}" ]; then
  echo "[entrypoint] socat management bridge ${CONTAINER_IP}:${MGMT_PORT} -> 127.0.0.1:${MGMT_PORT}" >&2
  socat "TCP-LISTEN:${MGMT_PORT},bind=${CONTAINER_IP},fork,reuseaddr" "TCP:127.0.0.1:${MGMT_PORT}" &
  for port in ${GATEWAY_PORTS}; do
    echo "[entrypoint] gateway normalizer (TLS) ${CONTAINER_IP}:${port} -> 127.0.0.1:${port}" >&2
    PROXY_LISTEN_HOST="${CONTAINER_IP}" PROXY_LISTEN_PORT="${port}" PROXY_UPSTREAM_PORT="${port}" \
      PROXY_TLS_CERT="${TLS_CERT}" PROXY_TLS_KEY="${TLS_KEY}" \
      python3 /usr/local/bin/cosmos_gateway_proxy.py &
  done
else
  echo "[entrypoint] WARNING: no container IP found; skipping relays" >&2
fi

echo "[entrypoint] starting emulator with --config ${CONFIG}" >&2
exec azure_data_cosmos_emulator --config "${CONFIG}"
