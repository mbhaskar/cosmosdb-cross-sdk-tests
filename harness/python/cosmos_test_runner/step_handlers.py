"""Maps scenario actions to backend operations."""

from __future__ import annotations

from typing import Any, Dict

from .backends import Backend, OpResult


def execute_action(backend: Backend, action: str, params: Dict[str, Any], ctx: Dict[str, Any]) -> OpResult:
    db = params.get("database", ctx.get("db"))
    container = params.get("container", ctx.get("container"))

    if action == "create_client":
        return backend.create_client(
            connection_mode=params.get("connection_mode", ctx.get("connection_mode", "gateway")),
            preferred_regions=params.get("preferred_regions", []),
        )
    if action == "create_database":
        return backend.create_database(params["id"], create_if_not_exists=params.get("create_if_not_exists", False))
    if action == "create_container":
        return backend.create_container(
            db, params["id"], params["partition_key"],
            create_if_not_exists=params.get("create_if_not_exists", False),
        )
    if action == "create_item":
        return backend.create_item(db, container, params["item"])
    if action == "seed_items":
        return backend.seed_items(db, container, params["count"], params["template"])
    if action == "read_item":
        return backend.read_item(db, container, params["id"], params["partition_key"])
    if action == "replace_item":
        return backend.replace_item(db, container, params["id"], params["partition_key"], params["item"])
    if action == "upsert_item":
        return backend.upsert_item(db, container, params["item"])
    if action == "delete_item":
        return backend.delete_item(db, container, params["id"], params["partition_key"])
    if action == "query_items":
        return backend.query_items(
            db, container, params["query"],
            parameters=params.get("parameters", []),
            partition_key=params.get("partition_key"),
            cross_partition=params.get("cross_partition", False),
        )
    if action == "query_drain":
        # Drain a (paginated) query to exhaustion. Both backends already
        # materialize the full result set, so this is a cross-partition query
        # that the SDK streams under whatever transport conditions are active.
        return backend.query_items(
            db, container, params["query"],
            parameters=params.get("parameters", []),
            partition_key=params.get("partition_key"),
            cross_partition=params.get("cross_partition", True),
        )
    if action == "delete_database":
        return backend.delete_database(params["id"])

    return OpResult(ok=False, error=f"unknown action '{action}'")
