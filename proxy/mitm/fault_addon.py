"""mitmproxy addon: pluggable HTTP protocol-fault injection for Cosmos DB.

WHY THIS EXISTS
---------------
Toxiproxy is a Layer-4 (TCP) fault injector: latency, bandwidth, timeouts,
connection resets. It never parses HTTP, so it *cannot* synthesize an HTTP
status code. Cosmos protocol faults -- 429 (throttle), 410 (Gone), 449
(RetryWith), 503 (ServiceUnavailable) -- are Layer-7 responses, so they need a
protocol-aware proxy. This addon is that proxy.

All fault *shapes* live in ``fault_engine.py`` (a pure, unit-testable registry).
This file is just the mitmproxy adapter: it maps the control channel to the
engine and turns engine decisions into ``http.Response`` objects.

TOPOLOGY (local / CI)
---------------------
    SDK runner ->  mitmproxy (this addon, L7)  ->  Toxiproxy (L4)  ->  Cosmos
                   :18091 reverse mode             :18081             emulator/live

The SDK points its endpoint at mitmproxy (:18091). While a fault is armed, every
proxied request gets the synthetic fault response. When the window (time- or
count-based) expires the addon stops intercepting and forwards requests to the
real backend unchanged, so you observe genuine SDK retry/backoff and recovery.

CONTROL CHANNEL
---------------
Driven over the same proxy port via a magic path prefix the addon handles
locally and never forwards:

    POST /__fault/arm?fault=gone_410&seconds=60         # arm by registry name
    POST /__fault/arm?fault=throttle_429&count=3        # first-N requests only
    POST /__fault/throttle?seconds=120&retry_after_ms=1000   # back-compat alias
    POST /__fault/clear
    GET  /__fault/status

``harness/.../faults.py:ProtocolFaultController`` drives these; you can also use
curl (see proxy/README.md). Add a new fault = one entry in fault_engine.FAULTS.

RUN
---
    mitmdump --listen-port 18091 \
             --mode reverse:https://localhost:18081 \
             --set ssl_insecure=true \
             -s proxy/mitm/fault_addon.py
"""

from __future__ import annotations

import json
import os
import sys

# Ensure the sibling fault_engine module is importable no matter how mitmproxy
# loads this script (the addons dir is mounted at /addons in the container).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fault_engine import FaultEngine

try:  # allow importing this module (and FaultEngine) without mitmproxy installed
    from mitmproxy import ctx, http
except ImportError:  # pragma: no cover - only hit in offline unit tests
    ctx = None
    http = None

_CONTROL_PREFIX = "/__fault/"
_engine = FaultEngine()


def _reply(flow, code: int, payload: dict) -> None:
    flow.response = http.Response.make(
        code, json.dumps(payload).encode(), {"Content-Type": "application/json"})


def request(flow) -> None:  # mitmproxy hook
    path = flow.request.path

    # --- control channel: handle locally, never forward ------------------ #
    if path.startswith(_CONTROL_PREFIX):
        action = path[len(_CONTROL_PREFIX):].split("?", 1)[0].strip("/")
        q = dict(flow.request.query)
        if action in ("arm", "throttle"):
            # "throttle" is the original 429-only verb; treat it as arming the
            # default throttle fault unless the caller names another one.
            if action == "throttle":
                q.setdefault("fault", "throttle_429")
            state = _engine.arm(q)
            if ctx:
                ctx.log.info(f"[fault] armed {state['fault']} "
                             f"(mode={state['mode']}, status={state['status']}, "
                             f"substatus={state['substatus']})")
            _reply(flow, 200, {"armed": True, **state})
        elif action == "clear":
            _engine.clear()
            if ctx:
                ctx.log.info("[fault] cleared")
            _reply(flow, 200, {"armed": False})
        elif action == "status":
            _reply(flow, 200, _engine.status())
        else:
            _reply(flow, 404, {"error": f"unknown control action '{action}'"})
        return

    # --- fault injection: synthesize a fault response -------------------- #
    decision = _engine.next_response()
    if decision is not None:
        status, headers, body = decision
        flow.response = http.Response.make(status, body, headers)
    # Otherwise: no response set -> mitmproxy forwards upstream normally.
