"""mitmproxy addon: time-windowed HTTP 429 (throttle) injection for Cosmos DB.

WHY THIS EXISTS
---------------
Toxiproxy is a Layer-4 (TCP) fault injector: latency, bandwidth, timeouts,
connection resets. It never parses HTTP, so it *cannot* synthesize an HTTP
status code. A Cosmos 429 (TooManyRequests) is a Layer-7 protocol fault, so it
needs a protocol-aware proxy. This addon is that proxy.

TOPOLOGY (local / CI)
---------------------
    SDK runner ->  mitmproxy (this addon, L7 429 window)  ->  Toxiproxy (L4)  ->  Cosmos
                   :18091 reverse mode                       :18081             emulator/live

The SDK points its endpoint at mitmproxy (:18091). While a throttle window is
armed, every proxied request gets a synthetic 429 with an `x-ms-retry-after-ms`
header (the SDK reads this to back off). When the window expires the addon stops
intercepting and forwards requests to the real backend unchanged, so you observe
genuine SDK retry/backoff and recovery.

CONTROL CHANNEL
---------------
The window is driven over the same proxy port via a magic path prefix the addon
handles locally and never forwards:

    POST https://<proxy>/__fault/throttle?seconds=120&retry_after_ms=1000
    POST https://<proxy>/__fault/clear
    GET  https://<proxy>/__fault/status

`harness/.../faults.py:ProtocolFaultController` drives these, mapping the
`net_throttle_window` / `throttle_window_clear` scenario timeline verbs. You can
also drive it by hand with curl (see proxy/README.md).

RUN
---
    mitmdump --listen-port 18091 \
             --mode reverse:https://localhost:18081 \
             --set ssl_insecure=true \
             -s proxy/mitm/throttle_window.py
"""

from __future__ import annotations

import time

from mitmproxy import ctx, http

# Shared mutable state for the addon instance.
_state = {
    "until": 0.0,          # epoch seconds; throttle active while now < until
    "retry_after_ms": 1000,
    "status": 429,
    "count": 0,            # how many 429s we have injected in the current window
}

_CONTROL_PREFIX = "/__fault/"
_THROTTLE_BODY = (
    b'{"code":"TooManyRequests",'
    b'"message":"Message: {\\"Errors\\":[\\"Request rate is large. '
    b'More Request Units may be needed, so no changes were made.\\"]}"}'
)


def _throttling() -> bool:
    return time.time() < _state["until"]


def _remaining() -> float:
    return max(0.0, _state["until"] - time.time())


def request(flow: http.HTTPFlow) -> None:
    path = flow.request.path

    # --- control channel: handle locally, never forward ------------------ #
    if path.startswith(_CONTROL_PREFIX):
        action = path[len(_CONTROL_PREFIX):].split("?", 1)[0].strip("/")
        q = flow.request.query
        if action == "throttle":
            seconds = float(q.get("seconds", "120"))
            _state["until"] = time.time() + seconds
            _state["retry_after_ms"] = int(q.get("retry_after_ms", "1000"))
            _state["status"] = int(q.get("status", "429"))
            _state["count"] = 0
            ctx.log.info(f"[throttle_window] armed for {seconds}s "
                         f"(status={_state['status']}, retry_after_ms={_state['retry_after_ms']})")
            flow.response = http.Response.make(200, b'{"armed":true}',
                                               {"Content-Type": "application/json"})
        elif action == "clear":
            _state["until"] = 0.0
            ctx.log.info("[throttle_window] cleared")
            flow.response = http.Response.make(200, b'{"armed":false}',
                                               {"Content-Type": "application/json"})
        elif action == "status":
            body = (f'{{"armed":{str(_throttling()).lower()},'
                    f'"remaining_s":{_remaining():.1f},'
                    f'"injected":{_state["count"]}}}').encode()
            flow.response = http.Response.make(200, body, {"Content-Type": "application/json"})
        else:
            flow.response = http.Response.make(404, b'{"error":"unknown control action"}',
                                               {"Content-Type": "application/json"})
        return

    # --- fault injection: synthesize a throttle response ----------------- #
    if _throttling():
        _state["count"] += 1
        flow.response = http.Response.make(
            _state["status"],
            _THROTTLE_BODY,
            {
                "Content-Type": "application/json",
                "x-ms-retry-after-ms": str(_state["retry_after_ms"]),
                "x-ms-substatus": "3200",
                "x-ms-throttle-injected": "true",
            },
        )
    # Otherwise: no response set -> mitmproxy forwards upstream normally.
