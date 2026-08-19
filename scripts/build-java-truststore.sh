#!/usr/bin/env bash
#
# build-java-truststore.sh — assemble a JVM trust store that trusts the local
# Cosmos emulator + mitmproxy self-signed certificates, so the Java runner's
# azure-cosmos client can talk to the proxied HTTPS endpoints.
#
# Why this exists (and why it isn't code):
#   azure-cosmos 4.63.0's CosmosClientBuilder exposes no custom HttpClient and no
#   "insecure TLS" switch. In gateway mode the SDK uses reactor-netty, whose
#   SslContextBuilder.forClient() defaults to the JVM trust store. So the correct,
#   standard way to make Java trust the emulator + mitmproxy certs is a trust
#   store passed via -Djavax.net.ssl.trustStore, NOT a code change. (The Python
#   SDK, by contrast, can just set verify=False in the test config.)
#
# Usage:
#   ./scripts/build-java-truststore.sh [OUT_JKS] [STOREPASS]
#     OUT_JKS    output keystore path   (default: build/java-cosmos-truststore.jks)
#     STOREPASS  keystore password      (default: changeit)
#
# Then run the harness/dispatcher with the store on the JVM. The dispatcher
# inherits the environment, so exporting JAVA_TOOL_OPTIONS is enough:
#
#   export JAVA_TOOL_OPTIONS="-Djavax.net.ssl.trustStore=$PWD/build/java-cosmos-truststore.jks \
#                             -Djavax.net.ssl.trustStorePassword=changeit"
#
# Re-run this script whenever the emulator or mitmproxy CA is regenerated.
set -euo pipefail

OUT_JKS="${1:-build/java-cosmos-truststore.jks}"
STOREPASS="${2:-changeit}"
MITM_CA="${MITM_CA:-$HOME/.mitmproxy/mitmproxy-ca-cert.pem}"
EMULATOR_HOST="${EMULATOR_HOST:-localhost}"
EMULATOR_PORT="${EMULATOR_PORT:-8081}"

command -v keytool >/dev/null 2>&1 || { echo "error: keytool not on PATH (install a JDK)"; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "error: openssl not on PATH"; exit 1; }

mkdir -p "$(dirname "$OUT_JKS")"
# Start from a copy of the JDK's default cacerts so public CAs (live accounts)
# still validate, then add our two local certs.
DEFAULT_CACERTS="${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")}/lib/security/cacerts"
if [[ -f "$DEFAULT_CACERTS" ]]; then
  cp "$DEFAULT_CACERTS" "$OUT_JKS"
  # Default cacerts password is 'changeit'; if the caller picked a different
  # STOREPASS we keep the original since keytool reads the existing store pass.
  SRCPASS="changeit"
else
  rm -f "$OUT_JKS"
  SRCPASS="$STOREPASS"
fi

import_cert() {
  local alias="$1" pem="$2"
  echo ">> importing $alias from $pem"
  keytool -importcert -noprompt -trustcacerts \
    -alias "$alias" -file "$pem" \
    -keystore "$OUT_JKS" -storepass "$SRCPASS" >/dev/null
}

# 1) mitmproxy CA (covers every host mitm intercepts, incl. the 18091 chain).
# In the Dockerized stack the CA lives inside the mitmproxy container rather than
# ~/.mitmproxy, so fall back to copying it out when it's not on the host.
if [[ ! -f "$MITM_CA" ]]; then
  MITM_CONTAINER="${MITM_CONTAINER:-mitmproxy}"
  if command -v docker >/dev/null 2>&1 \
     && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$MITM_CONTAINER"; then
    FETCHED="$(dirname "$OUT_JKS")/mitmproxy-ca-cert.pem"
    if docker cp "$MITM_CONTAINER:/home/mitmproxy/.mitmproxy/mitmproxy-ca-cert.pem" \
          "$FETCHED" >/dev/null 2>&1; then
      echo ">> pulled mitmproxy CA from container '$MITM_CONTAINER'"
      MITM_CA="$FETCHED"
    fi
  fi
fi
if [[ -f "$MITM_CA" ]]; then
  import_cert "mitmproxy-ca" "$MITM_CA"
else
  echo "warn: mitmproxy CA not found at $MITM_CA (run mitmproxy once to generate it, or set MITM_CA=...)"
fi

# 2) Emulator leaf cert, fetched straight off the running gateway. The gateway
# proxy may still be binding its TLS listener for a moment after the container
# reports healthy, and macOS LibreSSL's s_client can transiently yield nothing,
# so retry a few times before giving up.
TMP_PEM="$(mktemp)"
trap 'rm -f "$TMP_PEM"' EXIT
scrape_leaf() {
  local host="${1:-$EMULATOR_HOST}" port="${2:-$EMULATOR_PORT}" out="${3:-$TMP_PEM}"
  : >"$out"
  openssl s_client -connect "${host}:${port}" -servername "${host}" \
    </dev/null 2>/dev/null | openssl x509 >"$out" 2>/dev/null
  [[ -s "$out" ]]
}
leaf_ok=""
for _ in $(seq 1 10); do
  if scrape_leaf; then leaf_ok=1; break; fi
  sleep 1
done
if [[ -n "$leaf_ok" ]]; then
  import_cert "cosmos-emulator" "$TMP_PEM"
else
  echo "warn: could not fetch emulator cert from ${EMULATOR_HOST}:${EMULATOR_PORT} after 10 tries" >&2
  echo "      (is the gateway serving TLS there? try: openssl s_client -connect ${EMULATOR_HOST}:${EMULATOR_PORT})" >&2
fi

# 3) Extra emulator endpoints (best-effort). The vNext Cosmos emulator (default
# :8081) used by the `emulator` backend tier presents a DIFFERENT self-signed
# leaf than the inmemory gateway (:49151). When both tiers are exercised from the
# same Java runner they must both be trusted, so import each reachable extra
# endpoint under its own alias. Unreachable endpoints are silently skipped so a
# single-tier setup still succeeds. Override the list via EMULATOR_EXTRA_ENDPOINTS
# ("host:port host:port ..."); set it empty to disable.
EMULATOR_EXTRA_ENDPOINTS="${EMULATOR_EXTRA_ENDPOINTS:-localhost:8081}"
for endpoint in $EMULATOR_EXTRA_ENDPOINTS; do
  ehost="${endpoint%%:*}"; eport="${endpoint##*:}"
  # Skip if it duplicates the primary endpoint we already imported.
  [[ "$ehost:$eport" == "$EMULATOR_HOST:$EMULATOR_PORT" ]] && continue
  EXTRA_PEM="$(mktemp)"
  if scrape_leaf "$ehost" "$eport" "$EXTRA_PEM"; then
    import_cert "cosmos-emulator-${eport}" "$EXTRA_PEM"
  fi
  rm -f "$EXTRA_PEM"
done

echo
echo "trust store written: $OUT_JKS"
echo "run Java with:"
echo "  export JAVA_TOOL_OPTIONS=\"-Djavax.net.ssl.trustStore=$PWD/$OUT_JKS -Djavax.net.ssl.trustStorePassword=$SRCPASS\""
