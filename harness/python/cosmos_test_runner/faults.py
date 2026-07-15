"""Transport-fault control for emulator/live scenarios via Toxiproxy.

The stateful mock (see ``backends.MockBackend.control_event``) simulates
*protocol* faults (429/410/split) offline. This module handles the layer the
mock structurally cannot fake: real **TCP transport faults** injected into a
live socket by Toxiproxy, which sits between the SDK runner and the emulator/
live backend.

Scenario ``timeline:`` events whose verb starts with ``net_`` (plus
``region_down``/``region_up``/``reset_faults``) are routed here by the executor
when the backend is not ``mock``. Each verb maps to one Toxiproxy toxic added to
(or removed from) a named proxy via the admin API (default
``http://localhost:8474`` / ``$TOXIPROXY_URL``).

Requires a running Toxiproxy (see ``proxy/docker-compose.proxy.yaml``); it is a
no-op-until-called client with no third-party dependencies (stdlib ``urllib``).
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


class ProxyError(RuntimeError):
    """Raised when the Toxiproxy admin API is unreachable or rejects a call."""


class ProxyFaultController:
    """Thin client over the Toxiproxy HTTP admin API.

    Tracks the toxics it has added so ``reset``/``region_up`` can remove exactly
    those (and nothing an operator added out of band).
    """

    def __init__(self, admin_url: Optional[str] = None,
                 proxy: str = "cosmos", secondary_proxy: str = "cosmos-secondary"):
        self.admin_url = (admin_url or os.environ.get("TOXIPROXY_URL", "http://localhost:8474")).rstrip("/")
        self.proxy = proxy
        self.secondary_proxy = secondary_proxy
        # proxy_name -> list of toxic names we created (for targeted cleanup).
        self._added: Dict[str, List[str]] = {}

    # -- HTTP plumbing ---------------------------------------------------- #

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.admin_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise ProxyError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProxyError(f"cannot reach Toxiproxy at {self.admin_url}: {exc.reason}. "
                             f"Start proxy/docker-compose.proxy.yaml first.") from exc

    # -- toxic verbs ------------------------------------------------------ #

    def _add_toxic(self, proxy: str, name: str, ttype: str, stream: str,
                   attributes: Dict[str, Any], toxicity: float = 1.0) -> None:
        self._request("POST", f"/proxies/{proxy}/toxics", {
            "name": name, "type": ttype, "stream": stream,
            "toxicity": toxicity, "attributes": attributes,
        })
        self._added.setdefault(proxy, []).append(name)

    def _remove_toxic(self, proxy: str, name: str) -> None:
        try:
            self._request("DELETE", f"/proxies/{proxy}/toxics/{name}")
        except ProxyError:
            pass  # already gone
        names = self._added.get(proxy, [])
        if name in names:
            names.remove(name)

    def apply(self, event: str, args: Dict[str, Any]) -> None:
        """Apply a single transport-fault timeline verb."""
        args = args or {}
        if event == "net_latency":
            self._add_toxic(self.proxy, "net_latency", "latency", args.get("stream", "downstream"),
                            {"latency": int(args.get("latency_ms", 500)),
                             "jitter": int(args.get("jitter_ms", 0))})
        elif event == "net_timeout":
            self._add_toxic(self.proxy, "net_timeout", "timeout", args.get("stream", "upstream"),
                            {"timeout": int(args.get("timeout_ms", 0))})
        elif event == "net_reset":
            self._add_toxic(self.proxy, "net_reset", "reset_peer", args.get("stream", "downstream"),
                            {"timeout": int(args.get("after_ms", 0))})
        elif event == "net_bandwidth":
            self._add_toxic(self.proxy, "net_bandwidth", "bandwidth", args.get("stream", "downstream"),
                            {"rate": int(args.get("rate_kbps", 64))})
        elif event == "net_slow_close":
            self._add_toxic(self.proxy, "net_slow_close", "slow_close", args.get("stream", "downstream"),
                            {"delay": int(args.get("delay_ms", 1000))})
        elif event == "region_down":
            # Black-hole the primary region so preferred_regions failover kicks in.
            self._add_toxic(self.proxy, "region_down", "timeout", "upstream", {"timeout": 0})
        elif event == "region_up":
            self._remove_toxic(self.proxy, "region_down")
        elif event == "reset_faults":
            self.reset()
        else:
            raise ValueError(f"unknown transport-fault event '{event}'")

    def reset(self) -> None:
        """Remove every toxic this controller added (leaves proxies enabled)."""
        for proxy, names in list(self._added.items()):
            for name in list(names):
                self._remove_toxic(proxy, name)


class ProtocolFaultController:
    """Drives Layer-7 protocol faults (e.g. a time-windowed 429 storm) via the
    mitmproxy addon in ``proxy/mitm/throttle_window.py``.

    Toxiproxy (see :class:`ProxyFaultController`) is TCP-only and cannot emit an
    HTTP status code, so the ``net_throttle_window`` / ``throttle_window_clear``
    timeline verbs are routed here instead. The control channel is the magic
    ``/__fault/*`` path served by the addon on the same proxy endpoint the SDK
    talks to (default ``$MITM_ENDPOINT`` / the scenario's proxy endpoint).
    """

    def __init__(self, control_endpoint: Optional[str] = None):
        self.control_endpoint = (control_endpoint
                                 or os.environ.get("MITM_ENDPOINT", "https://localhost:18091")).rstrip("/")

    def _post(self, path: str) -> Any:
        url = f"{self.control_endpoint}{path}"
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"Content-Type": "application/json"})
        # The mitm/emulator endpoint uses a self-signed cert; the control call is
        # local and carries no secrets, so skip verification here.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise ProxyError(f"POST {path} -> HTTP {exc.code}: {exc.read().decode(errors='replace')}") from exc
        except urllib.error.URLError as exc:
            raise ProxyError(f"cannot reach mitmproxy control at {self.control_endpoint}: {exc.reason}. "
                             f"Start proxy/mitm (see proxy/README.md) first.") from exc

    def apply(self, event: str, args: Dict[str, Any]) -> None:
        args = args or {}
        if event in ("net_throttle_window", "inject_fault"):
            # net_throttle_window is the original 429-only verb; inject_fault is
            # the generic form that names any fault in the mitm registry
            # (throttle_429, gone_410, namecache_410, retrywith_449, ...).
            fault = args.get("fault", "throttle_429")
            qs = [f"fault={fault}"]
            # Scope: time window (seconds) OR first-N requests (count).
            if args.get("count") is not None:
                qs.append(f"count={args['count']}")
            else:
                qs.append(f"seconds={args.get('seconds', 120)}")
            # Optional ad-hoc overrides.
            for k in ("status", "substatus", "retry_after_ms"):
                if args.get(k) is not None:
                    qs.append(f"{k}={args[k]}")
            self._post("/__fault/arm?" + "&".join(qs))
        elif event in ("throttle_window_clear", "fault_clear"):
            self._post("/__fault/clear")
        else:
            raise ValueError(f"unknown protocol-fault event '{event}'")

    def reset(self) -> None:
        try:
            self._post("/__fault/clear")
        except ProxyError:
            pass
