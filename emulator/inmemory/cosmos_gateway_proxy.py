#!/usr/bin/env python3
"""Trailing-slash-normalizing HTTP reverse proxy for the Cosmos in-memory emulator.

The hosted Rust in-memory emulator's Gateway V1 rejects request paths that end
in a trailing slash (``GET /dbs/foo/`` -> 400 "trailing slash rejected"). The
stock Azure Cosmos SDKs (Python ``azure-cosmos``, Java) build every resource
path via ``GetPathFromLink`` which appends a trailing slash -- behaviour that
real Cosmos gateways tolerate but this emulator does not. The emulator was
validated against the Rust SDK, which does not emit that slash.

This proxy sits in front of the emulator's Gateway V1 loopback listener and
strips exactly one trailing slash from the path component (never the query
string, never the root ``/``) before forwarding. It also serves the
eth0 -> 127.0.0.1 bridge role (the emulator binds loopback only, so Docker
published ports must be relayed in from the container's routable interface).

The emulator performs no auth validation and the Cosmos master-key signature is
computed over the resource id/type (not the literal URL slash), so rewriting the
path here changes nothing the emulator or the SDK depends on.

Env:
  PROXY_LISTEN_HOST   interface to bind (default 0.0.0.0)
  PROXY_LISTEN_PORT   port to listen on (required)
  PROXY_UPSTREAM_HOST emulator host (default 127.0.0.1)
  PROXY_UPSTREAM_PORT emulator port (default = PROXY_LISTEN_PORT)
"""
from __future__ import annotations

import http.client
import os
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.environ.get("PROXY_UPSTREAM_HOST", "127.0.0.1")
LISTEN_HOST = os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ["PROXY_LISTEN_PORT"])
UPSTREAM_PORT = int(os.environ.get("PROXY_UPSTREAM_PORT", str(LISTEN_PORT)))

# Optional TLS termination. The Azure Cosmos Java SDK forces HTTPS for its
# gateway-mode DatabaseAccount handshake (it rewrites the endpoint scheme to
# https and does a real TLS handshake), so a cleartext-http gateway is
# unreachable from Java -- netty raises "not an SSL/TLS record". When
# PROXY_TLS_CERT + PROXY_TLS_KEY are set this proxy serves HTTPS on the front
# side while still forwarding cleartext HTTP to the loopback emulator upstream.
# Python (azure-cosmos) reaches the same https listener with connection_verify
# disabled; Java trusts the self-signed leaf via an injected JVM trust store.
TLS_CERT = os.environ.get("PROXY_TLS_CERT")
TLS_KEY = os.environ.get("PROXY_TLS_KEY")

# Hop-by-hop headers must not be forwarded verbatim (RFC 7230 §6.1).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _normalize_target(raw: str) -> str:
    """Strip exactly one trailing slash from the path, preserving the query."""
    if "?" in raw:
        path, query = raw.split("?", 1)
        query = "?" + query
    else:
        path, query = raw, ""
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path + query


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cosmos-gw-normalizer/1.0"

    def log_message(self, *args):  # keep stderr quiet; the emulator logs itself
        return

    def _proxy(self):
        target = _normalize_target(self.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        headers = {}
        for name, value in self.headers.items():
            if name.lower() in _HOP_BY_HOP or name.lower() == "host":
                continue
            headers[name] = value
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"

        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        except Exception as exc:  # noqa: BLE001 - surface upstream failures as 502
            msg = f'{{"error":"gateway proxy upstream failure: {exc}"}}'.encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in _HOP_BY_HOP or name.lower() == "content-length":
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        conn.close()

    # Cosmos data-plane verbs.
    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_HEAD = _proxy
    do_PATCH = _proxy
    do_OPTIONS = _proxy


def main() -> int:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=TLS_CERT, keyfile=TLS_KEY)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    sys.stderr.write(
        f"[gw-normalizer] {scheme}://{LISTEN_HOST}:{LISTEN_PORT} -> "
        f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT} (stripping one trailing slash)\n"
    )
    sys.stderr.flush()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
