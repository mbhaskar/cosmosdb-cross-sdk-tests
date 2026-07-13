"""Backend abstraction for the Python test runner.

Two implementations share one interface so scenarios are backend-agnostic:

* ``MockBackend`` - in-memory fake Cosmos DB. Lets the whole pipeline run
  without any infrastructure (no emulator / live account needed). It models
  the operation semantics the MVP scenarios assert on: 409 conflicts, 404
  not-found, upsert, ETag-free replace/delete, simple queries, and lazy
  metadata-call accounting (read_collection / read_pk_ranges).

* ``SdkBackend`` - drives the real ``azure-cosmos`` SDK against an emulator or
  live account. This is the production path; it requires a reachable endpoint.

Every method returns an :class:`OpResult` so the executor can assert uniformly
regardless of backend.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OpResult:
    """Uniform result of a single backend operation."""

    ok: bool
    status_code: int = 0
    error_code: Optional[str] = None
    error: Optional[str] = None
    item: Optional[Dict[str, Any]] = None
    items: Optional[List[Dict[str, Any]]] = None
    ru: float = 0.0
    diagnostics: Optional[Dict[str, Any]] = None
    # Control-plane event streams (populated by the mock, which *is* the server).
    # status_sequence includes any internal protocol responses the SDK would see
    # before the final one (e.g. [410, 200] on a split refresh, [429, 429, 201]
    # on throttle). metadata_events lists metadata calls made during the op.
    status_sequence: List[int] = field(default_factory=list)
    metadata_events: List[str] = field(default_factory=list)


@dataclass
class Metrics:
    ru_consumed: float = 0.0
    retries: int = 0
    connections_opened: int = 0
    connection_mode: Optional[str] = None
    metadata_calls: Dict[str, int] = field(
        default_factory=lambda: {
            "get_database_account": 0,
            "read_collection": 0,
            "read_pk_ranges": 0,
        }
    )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ru_consumed": round(self.ru_consumed, 3),
            "retries": self.retries,
            "connections_opened": self.connections_opened,
            "connection_mode": self.connection_mode,
            "metadata_calls": dict(self.metadata_calls),
        }


class Backend:
    """Common interface implemented by all backends."""

    metrics: Metrics

    def create_client(self, connection_mode: str, **kwargs) -> OpResult:
        raise NotImplementedError

    def create_database(self, db_id: str, create_if_not_exists: bool = False, **kwargs) -> OpResult:
        raise NotImplementedError

    def create_container(self, db_id: str, container_id: str, partition_key: str, **kwargs) -> OpResult:
        raise NotImplementedError

    def create_item(self, db_id: str, container_id: str, item: Dict[str, Any], **kwargs) -> OpResult:
        raise NotImplementedError

    def read_item(self, db_id: str, container_id: str, item_id: str, partition_key: Any, **kwargs) -> OpResult:
        raise NotImplementedError

    def replace_item(self, db_id: str, container_id: str, item_id: str, partition_key: Any, item: Dict[str, Any], **kwargs) -> OpResult:
        raise NotImplementedError

    def upsert_item(self, db_id: str, container_id: str, item: Dict[str, Any], **kwargs) -> OpResult:
        raise NotImplementedError

    def delete_item(self, db_id: str, container_id: str, item_id: str, partition_key: Any, **kwargs) -> OpResult:
        raise NotImplementedError

    def query_items(self, db_id: str, container_id: str, query: str, parameters=None, partition_key=None, cross_partition=False, **kwargs) -> OpResult:
        raise NotImplementedError

    def seed_items(self, db_id: str, container_id: str, count: int, template: Dict[str, Any], **kwargs) -> OpResult:
        """Bulk-seed ``count`` items by expanding ``{n}`` in string template
        values (n = 1..count). Implemented once here over ``create_item`` so it
        works identically on every backend. Returns a single aggregate result
        (ok only if every insert succeeded)."""
        created: List[Dict[str, Any]] = []
        all_ok = True
        last: Optional[OpResult] = None
        for n in range(1, int(count) + 1):
            item = {
                k: (v.replace("{n}", str(n)) if isinstance(v, str) else v)
                for k, v in template.items()
            }
            last = self.create_item(db_id, container_id, item)
            all_ok = all_ok and last.ok
            if last.ok and last.item is not None:
                created.append(last.item)
        return OpResult(
            ok=all_ok,
            status_code=last.status_code if last else 0,
            items=created,
            ru=0.0,
        )

    def delete_database(self, db_id: str, **kwargs) -> OpResult:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Mock backend
# --------------------------------------------------------------------------- #


def _pk_path_to_field(partition_key_path: str) -> str:
    # "/customerId" -> "customerId"; only single-path PKs in the MVP.
    return partition_key_path.lstrip("/").split("/")[0]


class MockBackend(Backend):
    """In-memory Cosmos DB simulation driven entirely by a shared profile.

    All operation semantics (status codes, RU charges, 409/404 branching, lazy
    metadata accounting) come from ``specs/mock-profile.json`` so the Python and
    Java runners cannot diverge. This class only provides the in-memory state
    store plus a tiny interpreter over the profile's step machine.
    """

    def __init__(self, profile: Dict[str, Any]) -> None:
        # databases[db]["containers"][container] =
        #   {"pk_field": str, "pk_path": str, "items": {(id, pk): item},
        #    "read_collection_done": bool}
        self.databases: Dict[str, Dict[str, Any]] = {}
        self.metrics = Metrics()
        self.profile = profile
        self.ru = profile["ru_charges"]
        self.codes = profile["status_codes"]
        self.ops = profile["operations"]
        # ---- control-plane state (mock owns the topology) -------------------
        # Per-container flag: has the SDK's PKRange cache been populated? A split
        # or explicit expiry invalidates it, forcing exactly one refresh.
        self._pkrange_cache_valid: Dict[str, bool] = {}
        # Containers armed with a one-shot 410 Gone on the next routed op (set by
        # split_partition / merge_partitions), modelling a stale partition route.
        self._split_armed: Dict[str, bool] = {}
        # Pending throttle faults keyed by op name -> remaining {count, retry_after_ms, ru_penalty}.
        self._throttle: Dict[str, Dict[str, Any]] = {}
        # Event recorder for the op currently executing (reset per public op).
        self._events: Dict[str, List] = {"status": [], "metadata": []}

    # Data-plane ops routed to a partition: eligible for split (410) faults and
    # PKRange-cache accounting.
    _ROUTED_OPS = {"read_item", "replace_item", "upsert_item", "delete_item",
                   "create_item", "query_items"}

    # -- public Backend API: each builds a context and runs the profile --- #

    def create_client(self, connection_mode: str, **kwargs) -> OpResult:
        return self._run("create_client", {"connection_mode": connection_mode})

    def create_database(self, db_id, create_if_not_exists=False, **kwargs) -> OpResult:
        return self._run("create_database", {"db_id": db_id, "if_not_exists": create_if_not_exists})

    def create_container(self, db_id, container_id, partition_key, **kwargs) -> OpResult:
        return self._run("create_container", {
            "db_id": db_id, "container_id": container_id, "pk_path": partition_key,
            "if_not_exists": kwargs.get("create_if_not_exists", False),
        })

    def create_item(self, db_id, container_id, item, **kwargs) -> OpResult:
        return self._run("create_item", {"db_id": db_id, "container_id": container_id, "item": dict(item)})

    def read_item(self, db_id, container_id, item_id, partition_key, **kwargs) -> OpResult:
        return self._run("read_item", {"db_id": db_id, "container_id": container_id,
                                       "item_id": item_id, "partition_key": partition_key})

    def replace_item(self, db_id, container_id, item_id, partition_key, item, **kwargs) -> OpResult:
        return self._run("replace_item", {"db_id": db_id, "container_id": container_id,
                                          "item_id": item_id, "partition_key": partition_key, "item": dict(item)})

    def upsert_item(self, db_id, container_id, item, **kwargs) -> OpResult:
        return self._run("upsert_item", {"db_id": db_id, "container_id": container_id, "item": dict(item)})

    def delete_item(self, db_id, container_id, item_id, partition_key, **kwargs) -> OpResult:
        return self._run("delete_item", {"db_id": db_id, "container_id": container_id,
                                         "item_id": item_id, "partition_key": partition_key})

    def query_items(self, db_id, container_id, query, parameters=None, partition_key=None,
                    cross_partition=False, **kwargs) -> OpResult:
        return self._run("query_items", {"db_id": db_id, "container_id": container_id, "query": query,
                                         "parameters": parameters or [], "partition_key": partition_key,
                                         "cross_partition": bool(cross_partition)})

    def delete_database(self, db_id, **kwargs) -> OpResult:
        return self._run("delete_database", {"db_id": db_id})

    # -- interpreter ------------------------------------------------------- #

    def _run(self, op: str, ctx: Dict[str, Any]) -> OpResult:
        """Public entry: apply any scheduled control-plane faults, run the profile
        step machine, and attach the observed event streams to the result."""
        self._events = {"status": [], "metadata": []}
        if op in self._ROUTED_OPS:
            self._apply_faults(op, ctx)
        result = self._execute(op, ctx)
        result.status_sequence = list(self._events["status"]) + [result.status_code]
        result.metadata_events = list(self._events["metadata"])
        return result

    def _execute(self, op: str, ctx: Dict[str, Any]) -> OpResult:
        ctx["container"] = self._container(ctx.get("db_id"), ctx.get("container_id"))
        for step in self.ops[op]:
            if "guard" in step:
                if all(self._pred(p, ctx) for p in step["guard"]):
                    return self._build(step["return"], ctx)
            elif "effect" in step:
                self._effect(step, ctx)
                ctx["container"] = self._container(ctx.get("db_id"), ctx.get("container_id"))
            elif "return" in step:
                return self._build(step["return"], ctx)
        raise RuntimeError(f"mock profile op '{op}' produced no result")

    # -- control plane ----------------------------------------------------- #

    @staticmethod
    def _ckey(ctx) -> str:
        return f"{ctx.get('db_id')}/{ctx.get('container_id')}"

    def _refresh_pkranges(self, ckey: str) -> None:
        """Model a ReadPartitionKeyRanges call: one metadata fetch, cache valid."""
        self.metrics.metadata_calls["read_pk_ranges"] += 1
        self._events["metadata"].append("read_pk_ranges")
        self._pkrange_cache_valid[ckey] = True

    def _apply_faults(self, op: str, ctx) -> None:
        ckey = self._ckey(ctx)
        # Throttling: emit N x 429 before the op is allowed to proceed.
        thr = self._throttle.get(op)
        if thr and thr["count"] > 0:
            for _ in range(thr["count"]):
                self._events["status"].append(self.codes["too_many_requests"])
                self.metrics.retries += 1
                if thr.get("ru_penalty"):
                    self.metrics.ru_consumed += float(thr["ru_penalty"])
            del self._throttle[op]
        # Stale partition route after a split: one 410 Gone, then the SDK refreshes
        # its PKRange cache and retries (so exactly one read_pk_ranges follows).
        if self._split_armed.get(ckey):
            self._events["status"].append(self.codes["gone"])
            self.metrics.retries += 1
            self._split_armed[ckey] = False
            self._refresh_pkranges(ckey)

    def control_event(self, event: str, args: Optional[Dict[str, Any]] = None,
                      db_id: Optional[str] = None, container_id: Optional[str] = None) -> None:
        """Apply a timeline-scheduled control-plane mutation to the router state."""
        args = args or {}
        ckey = f"{db_id}/{container_id}"
        if event in ("split_partition", "merge_partitions"):
            # Topology changed: stale the PKRange cache and arm a one-shot 410 on
            # the next routed op against this container.
            self._pkrange_cache_valid[ckey] = False
            self._split_armed[ckey] = True
        elif event == "expire_pkrange_cache":
            # Cache stale without a 410; next cross-partition op does one refresh.
            self._pkrange_cache_valid[ckey] = False
        elif event == "throttle":
            self._throttle[args["op"]] = {
                "count": int(args.get("count", 1)),
                "retry_after_ms": int(args.get("retry_after_ms", 0)),
                "ru_penalty": float(args.get("ru_penalty", 0.0)),
            }
        else:
            raise ValueError(f"unknown control-plane event '{event}'")

    def configure_control_plane(self, cp: Dict[str, Any]) -> None:  # noqa: ARG002
        """Reserved for future use (initial partition count, consistency). The
        current model derives topology lazily, so this is a no-op today."""
        return None

    def _container(self, db_id, container_id) -> Optional[Dict[str, Any]]:
        return self.databases.get(db_id, {}).get("containers", {}).get(container_id)

    @staticmethod
    def _eff_id(ctx) -> Any:
        if ctx.get("item_id") is not None:
            return ctx["item_id"]
        return (ctx.get("item") or {}).get("id")

    @staticmethod
    def _eff_pk(ctx) -> Any:
        if ctx.get("partition_key") is not None:
            return ctx["partition_key"]
        c = ctx.get("container")
        if c is None:
            return None
        return (ctx.get("item") or {}).get(c["pk_field"])

    def _cur_key(self, ctx):
        return (self._eff_id(ctx), _pk_key(self._eff_pk(ctx)))

    # -- predicates -- #
    def _pred(self, name: str, ctx) -> bool:
        c = ctx.get("container")
        if name == "db_exists":
            return ctx.get("db_id") in self.databases
        if name == "db_missing":
            return ctx.get("db_id") not in self.databases
        if name == "container_exists":
            return c is not None
        if name == "container_missing":
            return c is None
        if name == "item_exists":
            return c is not None and self._cur_key(ctx) in c["items"]
        if name == "item_missing":
            return c is None or self._cur_key(ctx) not in c["items"]
        if name == "existed":
            return bool(ctx.get("existed"))
        if name == "if_not_exists":
            return bool(ctx.get("if_not_exists"))
        raise ValueError(f"unknown predicate '{name}'")

    # -- effects -- #
    def _effect(self, step: Dict[str, Any], ctx) -> None:
        name = step["effect"]
        c = ctx.get("container")
        if name == "set_connection":
            self.metrics.connection_mode = ctx.get("connection_mode")
            self.metrics.connections_opened = 1
        elif name == "incr_metadata":
            self.metrics.metadata_calls[step["counter"]] += 1
            self._events["metadata"].append(step["counter"])
        elif name == "touch_collection":
            if c is not None and not c["read_collection_done"]:
                c["read_collection_done"] = True
                self.metrics.metadata_calls["read_collection"] += 1
                self._events["metadata"].append("read_collection")
        elif name == "touch_pk_ranges_if_cross":
            if ctx.get("cross_partition"):
                ckey = self._ckey(ctx)
                # Lazy PKRange fetch, but only when the cache is cold/stale. A warm
                # cache is reused (zero metadata calls) until a split expires it.
                if not self._pkrange_cache_valid.get(ckey):
                    self._refresh_pkranges(ckey)
        elif name == "assign_id":
            ctx["item"].setdefault("id", _new_id())
        elif name == "put_database":
            self.databases[ctx["db_id"]] = {"containers": {}}
        elif name == "remove_database":
            del self.databases[ctx["db_id"]]
        elif name == "put_container":
            self.databases[ctx["db_id"]]["containers"][ctx["container_id"]] = {
                "pk_field": _pk_path_to_field(ctx["pk_path"]),
                "pk_path": ctx["pk_path"],
                "items": {},
                "read_collection_done": False,
            }
        elif name == "put_item":
            key = self._cur_key(ctx)
            item = ctx["item"]
            item["id"] = self._eff_id(ctx)
            ctx["existed"] = key in c["items"]
            c["items"][key] = item
            ctx["result_item"] = item
        elif name == "remove_item":
            del c["items"][self._cur_key(ctx)]
        elif name == "query_filter":
            rows = list(c["items"].values())
            pk = ctx.get("partition_key")
            if pk is not None:
                rows = [r for r in rows if _pk_key(r.get(c["pk_field"])) == _pk_key(pk)]
            ctx["rows"] = _apply_query(ctx["query"], rows, ctx.get("parameters") or [])
        else:
            raise ValueError(f"unknown effect '{name}'")

    # -- return / charge -- #
    def _charge(self, name: Optional[str], ctx) -> float:
        if not name:
            return 0.0
        if name == "query":
            ru = self.ru["query_base"] + self.ru["query_per_row"] * len(ctx.get("rows") or [])
        else:
            ru = self.ru[name]
        self.metrics.ru_consumed += ru
        return ru

    def _build(self, ret: Dict[str, Any], ctx) -> OpResult:
        status = self.codes[ret["status"]]
        ru = self._charge(ret.get("charge"), ctx)
        if not ret.get("ok"):
            return OpResult(ok=False, status_code=status, error_code=ret.get("error_code"),
                            error=_template(ret.get("message", ""), ctx), ru=ru)
        item = None
        items = None
        body = ret.get("body")
        if body == "item":
            item = self._resolve_item(ctx)
        elif body == "items":
            items = ctx.get("rows")
        elif isinstance(body, dict):
            item = {k: _template(v, ctx) for k, v in body.items()}
        return OpResult(ok=True, status_code=status, item=item, items=items, ru=ru)

    def _resolve_item(self, ctx) -> Optional[Dict[str, Any]]:
        if ctx.get("result_item") is not None:
            return ctx["result_item"]
        c = ctx.get("container")
        if c is not None:
            found = c["items"].get(self._cur_key(ctx))
            if found is not None:
                return found
        return ctx.get("item")


def _template(value: Any, ctx) -> Any:
    if not isinstance(value, str):
        return value
    return (value
            .replace("{db}", str(ctx.get("db_id")))
            .replace("{container}", str(ctx.get("container_id")))
            .replace("{pk_path}", str(ctx.get("pk_path"))))


_UUID_COUNTER = {"n": 0}


def _new_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _pk_key(pk: Any) -> str:
    return "" if pk is None else str(pk)


def _apply_query(query: str, rows: List[Dict[str, Any]], parameters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Tiny SQL subset: SELECT * / SELECT VALUE COUNT(1), optional WHERE c.x = @p, TOP n."""
    q = query.strip()
    params = {p["name"]: p["value"] for p in parameters}

    # WHERE c.field = @param  (single equality predicate only)
    where = re.search(r"where\s+c\.(\w+)\s*=\s*(@\w+|'[^']*'|\"[^\"]*\"|\S+)", q, re.IGNORECASE)
    if where:
        field_name = where.group(1)
        raw = where.group(2)
        value = params.get(raw) if raw.startswith("@") else raw.strip("'\"")
        rows = [r for r in rows if str(r.get(field_name)) == str(value)]

    # SELECT VALUE COUNT(1)
    if re.search(r"select\s+value\s+count\(", q, re.IGNORECASE):
        return [len(rows)]

    # TOP n
    top = re.search(r"select\s+top\s+(\d+)", q, re.IGNORECASE)
    if top:
        rows = rows[: int(top.group(1))]
    return rows


# --------------------------------------------------------------------------- #
# Real SDK backend
# --------------------------------------------------------------------------- #


class _CountingTransport:
    """Wraps the azure-core HTTP transport to give the *real* SDK a retry signal.

    The Python SDK retries connection resets / timeouts / throttles internally and
    does not expose a retry count. Fault-injection assertions such as
    ``retry_count_gte`` need one, so we count:

      * every transport-level failure (``send`` raises, e.g. a ``reset_peer``
        toxic surfaces as a ServiceRequestError) — the SDK will retry, and
      * every retriable *response* status (429/503/449/410) the server returns.

    Each such event increments ``metrics.retries`` live, so no per-op snapshotting
    is needed. Non-retriable 2xx/4xx responses are ignored, so ordinary multi-call
    operations do not inflate the count.
    """

    _RETRIABLE_STATUS = {429, 503, 449, 410}

    def __init__(self, inner, metrics: "Metrics") -> None:
        self._inner = inner
        self._metrics = metrics

    # Context-manager + lifecycle passthrough (azure-core drives these).
    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *args):
        return self._inner.__exit__(*args)

    def open(self):
        return self._inner.open()

    def close(self):
        return self._inner.close()

    def send(self, request, **kwargs):
        try:
            response = self._inner.send(request, **kwargs)
        except Exception:
            # Transport failure (reset/timeout) — the retry policy will re-send.
            self._metrics.retries += 1
            raise
        status = getattr(response, "status_code", None)
        if status in self._RETRIABLE_STATUS:
            self._metrics.retries += 1
        return response


class SdkBackend(Backend):
    """Drives the real azure-cosmos SDK (emulator or live account)."""

    def __init__(self, endpoint: str, key: str, verify_tls: bool = True) -> None:
        self.endpoint = endpoint
        self.key = key
        self.verify_tls = verify_tls
        self.metrics = Metrics()
        self._client = None
        self._connection_mode = "gateway"
        self._last_diag: Optional[Dict[str, Any]] = None

    def _capture(self, *args) -> None:
        """response_hook: extract the real per-request diagnostics (HTTP response
        headers) emitted by the SDK. Signature varies across azure-cosmos
        versions, so scan args for the headers mapping."""
        headers = None
        for a in args:
            try:
                keys = list(a.keys())
            except AttributeError:
                continue
            if any(str(k).lower().startswith("x-ms-") for k in keys):
                headers = a
                break
        if headers is None:
            return
        interesting = (
            "x-ms-request-charge", "x-ms-activity-id", "x-ms-session-token",
            "etag", "x-ms-resource-quota", "x-ms-resource-usage",
            "x-ms-serviceversion", "x-ms-gatewayversion",
            "x-ms-global-committed-lsn", "x-ms-number-of-read-regions",
        )
        diag = {k: headers[k] for k in interesting if k in headers}
        diag["all_headers"] = {str(k): str(v) for k, v in headers.items()}
        self._last_diag = diag
        # Accumulate the real server-reported RU charge for this request.
        try:
            self.metrics.ru_consumed += float(headers.get("x-ms-request-charge", 0) or 0)
        except (TypeError, ValueError):
            pass

    def create_client(self, connection_mode: str, **kwargs) -> OpResult:
        try:
            from azure.cosmos import CosmosClient
            from azure.core.pipeline.transport import RequestsTransport
            self._connection_mode = connection_mode
            self.metrics.connection_mode = connection_mode
            # The Python SDK is gateway-mode; connection_mode kept for parity/metrics.
            # The emulator (and the local Toxiproxy/mitmproxy chain in front of it)
            # serves a self-signed certificate, so TLS verification is disabled for
            # those targets. Live accounts keep verification on.
            client_kwargs = {}
            if not self.verify_tls:
                client_kwargs["connection_verify"] = False
            # Wrap the default transport so injected transport faults / throttles
            # surface as a real retry count (see _CountingTransport).
            client_kwargs["transport"] = _CountingTransport(RequestsTransport(), self.metrics)
            self._client = CosmosClient(self.endpoint, credential=self.key, **client_kwargs)
            self.metrics.connections_opened = 1
            self.metrics.metadata_calls["get_database_account"] += 1
            return OpResult(ok=True, status_code=200)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def _db(self, db_id):
        return self._client.get_database_client(db_id)

    def _container(self, db_id, container_id):
        return self._db(db_id).get_container_client(container_id)

    def create_database(self, db_id, create_if_not_exists=False, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            if create_if_not_exists:
                self._client.create_database_if_not_exists(id=db_id, response_hook=self._capture)
            else:
                self._client.create_database(id=db_id, response_hook=self._capture)
            return OpResult(ok=True, status_code=201, item={"id": db_id}, diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def create_container(self, db_id, container_id, partition_key, **kwargs) -> OpResult:
        try:
            from azure.cosmos import PartitionKey
            pk = PartitionKey(path=partition_key)
            db = self._db(db_id)
            self._last_diag = None
            if kwargs.get("create_if_not_exists"):
                db.create_container_if_not_exists(id=container_id, partition_key=pk, response_hook=self._capture)
            else:
                db.create_container(id=container_id, partition_key=pk, response_hook=self._capture)
            return OpResult(ok=True, status_code=201, item={"id": container_id}, diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def create_item(self, db_id, container_id, item, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            created = self._container(db_id, container_id).create_item(
                body=dict(item), response_hook=self._capture)
            return OpResult(ok=True, status_code=201, item=dict(created), diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def read_item(self, db_id, container_id, item_id, partition_key, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            item = self._container(db_id, container_id).read_item(
                item=item_id, partition_key=partition_key, response_hook=self._capture)
            return OpResult(ok=True, status_code=200, item=dict(item), diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def replace_item(self, db_id, container_id, item_id, partition_key, item, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            replaced = self._container(db_id, container_id).replace_item(
                item=item_id, body=dict(item), response_hook=self._capture)
            return OpResult(ok=True, status_code=200, item=dict(replaced), diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def upsert_item(self, db_id, container_id, item, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            upserted = self._container(db_id, container_id).upsert_item(
                body=dict(item), response_hook=self._capture)
            return OpResult(ok=True, status_code=200, item=dict(upserted), diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def delete_item(self, db_id, container_id, item_id, partition_key, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            self._container(db_id, container_id).delete_item(
                item=item_id, partition_key=partition_key, response_hook=self._capture)
            return OpResult(ok=True, status_code=204, diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def query_items(self, db_id, container_id, query, parameters=None, partition_key=None,
                    cross_partition=False, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            kw = {"query": query, "parameters": parameters or [], "response_hook": self._capture}
            if partition_key is not None:
                kw["partition_key"] = partition_key
            else:
                kw["enable_cross_partition_query"] = cross_partition
            items = [dict(i) for i in self._container(db_id, container_id).query_items(**kw)]
            return OpResult(ok=True, status_code=200, items=items, diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)

    def delete_database(self, db_id, **kwargs) -> OpResult:
        try:
            self._last_diag = None
            self._client.delete_database(db_id, response_hook=self._capture)
            return OpResult(ok=True, status_code=204, diagnostics=self._last_diag)
        except Exception as exc:  # noqa: BLE001
            return _sdk_error(exc, self.metrics)


def _sdk_error(exc: Exception, metrics: "Metrics" = None) -> OpResult:
    status = getattr(exc, "status_code", 0) or 0
    sub = getattr(exc, "error_code", None)
    name = type(exc).__name__
    # Failed requests still consume RU (e.g. a 409 conflict on create).
    if metrics is not None:
        headers = getattr(exc, "headers", None) or {}
        try:
            metrics.ru_consumed += float(headers.get("x-ms-request-charge", 0) or 0)
        except (TypeError, ValueError):
            pass
    return OpResult(ok=False, status_code=status, error_code=sub or name, error=str(exc))


def load_mock_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return the mock behavior profile.

    Preference order: (1) inline ``config['mock_profile']`` injected by the
    orchestrator (single source of truth, read once); (2) ``specs/mock-profile.json``
    located by walking up from this file (standalone CLI use).
    """
    inline = config.get("mock_profile")
    if isinstance(inline, dict):
        return inline
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "..", "..", "specs", "mock-profile.json"),
        os.path.join(os.getcwd(), "specs", "mock-profile.json"),
    ]
    for path in candidates:
        path = os.path.abspath(path)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "mock profile not found: provide config['mock_profile'] or specs/mock-profile.json"
    )


def make_backend(config: Dict[str, Any]) -> Backend:
    backend = config.get("backend", "mock")
    if backend == "mock":
        return MockBackend(load_mock_profile(config))
    endpoint = config.get("endpoint")
    key = config.get("key")
    if not endpoint or not key:
        raise ValueError(f"backend '{backend}' requires endpoint and key in config")
    # The emulator serves a self-signed cert; so does the local proxy chain when a
    # scenario routes through Toxiproxy/mitmproxy. Skip TLS verification for those.
    # Live accounts verify by default; override with config['tls_verify'] if a live
    # account is itself fronted by the local proxy.
    tls_verify = config.get("tls_verify")
    if tls_verify is None:
        tls_verify = not (backend == "emulator" or config.get("proxy_endpoint"))
    return SdkBackend(endpoint, key, verify_tls=bool(tls_verify))
