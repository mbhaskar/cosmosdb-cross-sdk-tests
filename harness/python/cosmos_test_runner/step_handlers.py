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
            consistency_level=params.get("consistency_level"),
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
        return backend.replace_item(db, container, params["id"], params["partition_key"], params["item"],
                                    if_match=params.get("if_match"),
                                    if_none_match=params.get("if_none_match"))
    if action == "upsert_item":
        return backend.upsert_item(db, container, params["item"])
    if action == "delete_item":
        return backend.delete_item(db, container, params["id"], params["partition_key"])
    if action == "execute_batch":
        return backend.execute_batch(
            db, container, params["operations"], params["partition_key"])
    if action == "query_items":
        return backend.query_items(
            db, container, params["query"],
            parameters=params.get("parameters", []),
            partition_key=params.get("partition_key"),
            cross_partition=params.get("cross_partition", False),
        )
    if action == "query_drain":
        # Drain a (paginated) query, following server-minted continuation tokens
        # page-by-page. Optional params: max_item_count (page size), max_pages
        # (stop early and expose the continuation), continuation (resume from a
        # previously captured token) -- see CAP-6 / D-403 / C-311.
        return backend.query_drain(
            db, container, params["query"],
            parameters=params.get("parameters", []),
            partition_key=params.get("partition_key"),
            cross_partition=params.get("cross_partition", True),
            max_item_count=params.get("max_item_count"),
            max_pages=params.get("max_pages"),
            continuation=params.get("continuation") or None,
        )
    if action == "delete_database":
        return backend.delete_database(params["id"])
    if action == "read_feed_ranges":
        return backend.read_feed_ranges(
            db, container, force_refresh=params.get("force_refresh", False))
    if action == "read_pkranges":
        return backend.read_pkranges(db, container)

    return OpResult(ok=False, error=f"unknown action '{action}'")
