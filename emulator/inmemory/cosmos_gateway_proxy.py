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
import json
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

# Host/scheme this proxy is reachable at from the SDK clients. The upstream
# emulator advertises its regions in the DatabaseAccount handshake as
# ``http://127.0.0.1:<port>/`` (cleartext loopback). The Azure Cosmos *Java*
# SDK resolves the write/read region from those advertised locations even in
# gateway mode with endpoint discovery disabled, then routes data-plane ops to
# that cleartext http address -- which bypasses the TLS listener and fails with
# RetryExhaustedException. We rewrite the advertised region endpoints in the
# topology response to point back at THIS listener (https://localhost:<port>)
# so both SDKs route every op through the TLS proxy. Python (discovery off,
# verify off) is unaffected -- the rewritten URL equals its configured endpoint.
ADVERTISE_HOST = os.environ.get("PROXY_ADVERTISE_HOST", "localhost")
ADVERTISE_SCHEME = os.environ.get(
    "PROXY_ADVERTISE_SCHEME", "https" if (TLS_CERT and TLS_KEY) else "http"
)
_EXTERNAL_AUTHORITY = f"{ADVERTISE_SCHEME}://{ADVERTISE_HOST}:{LISTEN_PORT}"


def _rewrite_topology(payload: bytes) -> bytes:
    """Rewrite advertised region endpoints in the DatabaseAccount response.

    Only touches bodies that look like the account topology (they contain
    ``writableLocations``); replaces the upstream's advertised loopback
    authority with this proxy's externally reachable authority so the SDKs
    keep routing through the TLS listener.
    """
    if not payload or b"writableLocations" not in payload:
        return payload
    for advertised_host in ("127.0.0.1", "localhost"):
        for scheme in ("http", "https"):
            src = f"{scheme}://{advertised_host}:{UPSTREAM_PORT}".encode()
            if src != _EXTERNAL_AUTHORITY.encode():
                payload = payload.replace(src, _EXTERNAL_AUTHORITY.encode())
    return payload

# Hop-by-hop headers must not be forwarded verbatim (RFC 7230 §6.1).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# --- rid (_self link) -> friendly-name translation ------------------------------
# This in-memory emulator's Gateway V1 only serves *name*-addressed resource paths
# (``/dbs/{dbName}/colls/{collName}/...``). The Azure Cosmos **Java** SDK reads the
# collection routing map (partition key ranges) via the collection's rid-based
# ``_self`` link (``/dbs/{dbRid}/colls/{collRid}/pkranges``) -- the canonical
# behaviour -- which the emulator answers with 404. That missing routing map
# surfaces as CollectionRoutingMapNotFoundException ("collectionRoutingMapValueHolder.v
# cannot be null") and every data-plane op fails with RetryExhaustedException.
# azure-cosmos **Python** reads pkranges by name, so it was unaffected.
#
# The proxy learns rid->name mappings from resource responses that flow through it
# (database/collection create + read all carry ``id`` + ``_rid`` + ``_self``), then
# rewrites incoming rid-addressed path segments back to friendly names before
# forwarding upstream. Name-addressed requests pass through untouched (a segment is
# only rewritten when it exactly matches a learned rid).
_DB_RID_TO_NAME: dict[str, str] = {}
_COLL_RID_TO_NAME: dict[str, str] = {}


def _learn_rid_names(payload: bytes) -> None:
    """Record rid->name mappings from any db/collection resource(s) in a response."""
    if not payload or b'"_self"' not in payload or b'"_rid"' not in payload:
        return
    try:
        doc = json.loads(payload)
    except Exception:  # noqa: BLE001 - non-JSON or partial body; nothing to learn
        return
    # Responses are either a single resource or a feed wrapper (Databases /
    # DocumentCollections arrays). Normalise to a flat list of resource dicts.
    candidates = [doc]
    for key in ("Databases", "DocumentCollections"):
        val = doc.get(key) if isinstance(doc, dict) else None
        if isinstance(val, list):
            candidates.extend(val)
    for res in candidates:
        if not isinstance(res, dict):
            continue
        name = res.get("id")
        self_link = res.get("_self")
        if not name or not isinstance(self_link, str):
            continue
        parts = [p for p in self_link.split("/") if p]
        # dbs/{dbRid}                       -> database
        # dbs/{dbRid}/colls/{collRid}       -> collection
        if len(parts) == 2 and parts[0] == "dbs":
            _DB_RID_TO_NAME[parts[1]] = name
        elif len(parts) == 4 and parts[0] == "dbs" and parts[2] == "colls":
            _COLL_RID_TO_NAME[parts[3]] = name


def _translate_rid_path(path: str) -> str:
    """Rewrite rid-addressed /dbs/{rid}/colls/{rid} segments to friendly names."""
    if "/dbs/" not in path:
        return path
    segs = path.split("/")
    for i in range(len(segs) - 1):
        if segs[i] == "dbs" and segs[i + 1] in _DB_RID_TO_NAME:
            segs[i + 1] = _DB_RID_TO_NAME[segs[i + 1]]
        elif segs[i] == "colls" and segs[i + 1] in _COLL_RID_TO_NAME:
            segs[i + 1] = _COLL_RID_TO_NAME[segs[i + 1]]
    return "/".join(segs)


def _normalize_target(raw: str) -> str:
    """Strip exactly one trailing slash from the path, preserving the query.

    Also translates rid-addressed (``_self``-link) path segments to friendly names
    so the Java SDK's rid-based routing-map reads resolve on this name-only gateway.
    """
    if "?" in raw:
        path, query = raw.split("?", 1)
        query = "?" + query
    else:
        path, query = raw, ""
    path = _translate_rid_path(path)
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
            # The Azure Cosmos Java SDK sends an *empty* x-ms-continuation header on
            # the first page of a (cross-partition) query. The real Cosmos gateway
            # treats an empty/absent continuation as "start from the beginning", but
            # this emulator rejects a present-but-empty token with 400 "Invalid
            # continuation token" -- which fails every Java query_drain. Drop the
            # header when blank so the first page starts fresh; real continuation
            # tokens (non-empty) still pass through untouched. Python omits the
            # header entirely, so it was unaffected.
            if name.lower() == "x-ms-continuation" and not (value or "").strip():
                continue
            headers[name] = value
        headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"

        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            _learn_rid_names(payload)
            payload = _rewrite_topology(payload)
            if os.environ.get("PROXY_DEBUG"):
                sys.stderr.write(
                    f"PROXYDBG {self.command} {target} -> {resp.status} len={len(payload)}\n"
                )
                sys.stderr.flush()
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
