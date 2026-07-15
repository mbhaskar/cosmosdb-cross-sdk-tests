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
if [[ -f "$MITM_CA" ]]; then
  import_cert "mitmproxy-ca" "$MITM_CA"
else
  echo "warn: mitmproxy CA not found at $MITM_CA (run mitmproxy once to generate it, or set MITM_CA=...)"
fi

# 2) Emulator leaf cert, fetched straight off the running gateway.
TMP_PEM="$(mktemp)"
trap 'rm -f "$TMP_PEM"' EXIT
if openssl s_client -connect "${EMULATOR_HOST}:${EMULATOR_PORT}" -servername "${EMULATOR_HOST}" \
      </dev/null 2>/dev/null | openssl x509 >"$TMP_PEM" 2>/dev/null && [[ -s "$TMP_PEM" ]]; then
  import_cert "cosmos-emulator" "$TMP_PEM"
else
  echo "warn: could not fetch emulator cert from ${EMULATOR_HOST}:${EMULATOR_PORT} (is it running?)"
fi

echo
echo "trust store written: $OUT_JKS"
echo "run Java with:"
echo "  export JAVA_TOOL_OPTIONS=\"-Djavax.net.ssl.trustStore=$PWD/$OUT_JKS -Djavax.net.ssl.trustStorePassword=$SRCPASS\""
