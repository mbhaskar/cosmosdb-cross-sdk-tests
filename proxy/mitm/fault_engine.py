"""Protocol-fault engine for the mitmproxy addon (pure, no mitmproxy import).

This module holds ALL the fault-selection logic so it can be unit-tested without
a running mitmproxy. The addon (``fault_addon.py``) is a thin adapter that
turns :class:`FaultEngine` decisions into ``mitmproxy.http.Response`` objects.

WHY A REGISTRY
--------------
Toxiproxy is Layer-4 (TCP) and cannot emit an HTTP status code. Layer-7 Cosmos
faults -- 429 throttling, 410 Gone, 449 RetryWith, 503 ServiceUnavailable -- are
protocol responses, and each one has a DIFFERENT shape that the SDK branches on:

    fault            status  x-ms-substatus  retry-after?  SDK reaction
    ---------------  ------  --------------  ------------  --------------------------
    throttle_429     429     3200            yes           back off, retry after delay
    gone_410         410     1002 (PKRangeGone)  no        refresh routing cache, retry
    namecache_410    410     1000 (NameCacheStale) no      refresh collection cache, retry
    retrywith_449    449     0               no            immediate retry
    unavailable_503  503     0               yes           retry another replica

So a fault is a *template* (status + substatus + headers + body), not just a
status-code swap. New faults are one entry in :data:`FAULTS` -- no logic changes.

WINDOWING
---------
A fault can be armed two ways (this is how you "switch"/scope faults):

* ``seconds=N``  -> time window (good for a sustained 429 storm).
* ``count=N``    -> inject on the first N proxied requests, then pass through
                    (good for a transient 410 partition-split that recovers).

Only one fault is active at a time; arming a new one replaces the previous.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

# --- response bodies (roughly Cosmos-shaped; SDKs branch on status+substatus) - #
_BODY_429 = (
    b'{"code":"TooManyRequests","message":"Message: {\\"Errors\\":['
    b'\\"Request rate is large. More Request Units may be needed, so no changes '
    b'were made.\\"]}"}'
)
_BODY_410 = (
    b'{"code":"Gone","message":"Message: {\\"Errors\\":['
    b'\\"The requested resource is no longer available at the server.\\"]}"}'
)
_BODY_449 = (
    b'{"code":"RetryWith","message":"Message: {\\"Errors\\":['
    b'\\"Retry the request.\\"]}"}'
)
_BODY_503 = (
    b'{"code":"ServiceUnavailable","message":"Message: {\\"Errors\\":['
    b'\\"Service is currently unavailable, please retry after a while.\\"]}"}'
)

# --- fault registry: name -> response template ------------------------------- #
# retry_after_ms=None means "do not emit an x-ms-retry-after-ms header".
FAULTS: Dict[str, Dict[str, Any]] = {
    "throttle_429":    {"status": 429, "substatus": "3200", "retry_after_ms": 1000, "body": _BODY_429},
    "gone_410":        {"status": 410, "substatus": "1002", "retry_after_ms": None, "body": _BODY_410},
    "namecache_410":   {"status": 410, "substatus": "1000", "retry_after_ms": None, "body": _BODY_410},
    "retrywith_449":   {"status": 449, "substatus": "0",    "retry_after_ms": None, "body": _BODY_449},
    "unavailable_503": {"status": 503, "substatus": "0",    "retry_after_ms": 1000, "body": _BODY_503},
}

# Back-compat: the original addon only knew "throttle". Map it to the registry.
_DEFAULT_FAULT = "throttle_429"


class FaultEngine:
    """Holds the currently-armed fault and decides the response for each request.

    Pure and side-effect-free apart from its own internal counters, so it can be
    unit-tested with no mitmproxy dependency.
    """

    def __init__(self) -> None:
        self._mode = "off"          # "off" | "time" | "count"
        self._until = 0.0           # epoch seconds (time mode)
        self._remaining = 0         # requests left to fault (count mode)
        self._fault = _DEFAULT_FAULT
        self._resolved: Dict[str, Any] = dict(FAULTS[_DEFAULT_FAULT])
        self._injected = 0          # how many faults injected in the current window

    # -- arming / clearing ------------------------------------------------- #
    def arm(self, query: Dict[str, str]) -> Dict[str, Any]:
        """Arm a fault from a control-channel query mapping.

        Recognised keys: ``fault`` (registry name), ``seconds``, ``count``,
        and ad-hoc overrides ``status`` / ``substatus`` / ``retry_after_ms``.
        """
        name = (query.get("fault") or _DEFAULT_FAULT).strip()
        base = dict(FAULTS.get(name, FAULTS[_DEFAULT_FAULT]))

        # Ad-hoc overrides for one-off experiments without a registry entry.
        if "status" in query:
            base["status"] = int(query["status"])
        if "substatus" in query:
            base["substatus"] = str(query["substatus"])
        if "retry_after_ms" in query:
            v = query["retry_after_ms"]
            base["retry_after_ms"] = None if v in ("", "none", "None") else int(v)

        self._fault = name
        self._resolved = base
        self._injected = 0

        count = query.get("count")
        if count is not None and str(count) != "":
            self._mode = "count"
            self._remaining = int(count)
            self._until = 0.0
        else:
            self._mode = "time"
            self._until = time.time() + float(query.get("seconds", "120"))
            self._remaining = 0
        return self.status()

    def clear(self) -> Dict[str, Any]:
        self._mode = "off"
        self._until = 0.0
        self._remaining = 0
        return self.status()

    # -- per-request decision ---------------------------------------------- #
    def active(self) -> bool:
        if self._mode == "time":
            return time.time() < self._until
        if self._mode == "count":
            return self._remaining > 0
        return False

    def next_response(self) -> Optional[Tuple[int, Dict[str, str], bytes]]:
        """Return ``(status, headers, body)`` to inject, or ``None`` to forward
        the request upstream unchanged. Consumes one count in count mode."""
        if not self.active():
            return None
        self._injected += 1
        if self._mode == "count":
            self._remaining -= 1

        r = self._resolved
        headers = {
            "Content-Type": "application/json",
            "x-ms-substatus": str(r.get("substatus", "0")),
            "x-ms-fault-injected": "true",
            "x-ms-fault-name": self._fault,
        }
        if r.get("retry_after_ms") is not None:
            headers["x-ms-retry-after-ms"] = str(r["retry_after_ms"])
        return int(r["status"]), headers, r["body"]

    # -- introspection ----------------------------------------------------- #
    def remaining_s(self) -> float:
        return max(0.0, self._until - time.time()) if self._mode == "time" else 0.0

    def status(self) -> Dict[str, Any]:
        return {
            "armed": self.active(),
            "fault": self._fault,
            "mode": self._mode,
            "remaining_s": round(self.remaining_s(), 1),
            "remaining_count": self._remaining if self._mode == "count" else None,
            "injected": self._injected,
            "status": self._resolved.get("status"),
            "substatus": self._resolved.get("substatus"),
        }
