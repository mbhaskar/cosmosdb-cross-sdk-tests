package com.azure.cosmos.testrunner;

import java.util.List;
import java.util.Map;

/** Maps scenario actions to backend operations; mirrors the Python step_handlers. */
public final class StepHandlers {

    private StepHandlers() {
    }

    @SuppressWarnings("unchecked")
    public static OpResult execute(Backend backend, String action, Map<String, Object> params, Map<String, Object> ctx) {
        String db = str(params.getOrDefault("database", ctx.get("db")));
        String container = str(params.getOrDefault("container", ctx.get("container")));

        switch (action) {
            case "create_client":
                return backend.createClient(str(params.getOrDefault("connection_mode",
                        ctx.getOrDefault("connection_mode", "gateway"))),
                        str(params.get("consistency_level")));
            case "create_database":
                return backend.createDatabase(str(params.get("id")), bool(params.get("create_if_not_exists")));
            case "create_container":
                return backend.createContainer(db, str(params.get("id")), str(params.get("partition_key")),
                        bool(params.get("create_if_not_exists")));
            case "create_item":
                return backend.createItem(db, container, (Map<String, Object>) params.get("item"));
            case "seed_items":
                return backend.seedItems(db, container, asInt(params.get("count")),
                        (Map<String, Object>) params.get("template"));
            case "read_item":
                return backend.readItem(db, container, str(params.get("id")), params.get("partition_key"));
            case "replace_item":
                return backend.replaceItem(db, container, str(params.get("id")), params.get("partition_key"),
                        (Map<String, Object>) params.get("item"),
                        str(params.get("if_match")), str(params.get("if_none_match")));
            case "upsert_item":
                return backend.upsertItem(db, container, (Map<String, Object>) params.get("item"));
            case "delete_item":
                return backend.deleteItem(db, container, str(params.get("id")), params.get("partition_key"));
            case "query_items":
                return backend.queryItems(db, container, str(params.get("query")),
                        (List<Map<String, Object>>) params.get("parameters"),
                        params.get("partition_key"), bool(params.get("cross_partition")));
            case "execute_batch":
                return backend.executeBatch(db, container,
                        (List<Map<String, Object>>) params.get("operations"), params.get("partition_key"));
            case "query_drain":
                // Real paged drain following server-minted continuation tokens
                // (mirrors the Python query_drain / CAP-6). Defaults to
                // cross-partition so the SDK streams every page.
                return backend.queryDrain(db, container, str(params.get("query")),
                        (List<Map<String, Object>>) params.get("parameters"),
                        params.get("partition_key"),
                        params.containsKey("cross_partition") ? bool(params.get("cross_partition")) : true,
                        asIntOrNull(params.get("max_item_count")),
                        asIntOrNull(params.get("max_pages")),
                        str(params.get("continuation")));
            case "delete_database":
                return backend.deleteDatabase(str(params.get("id")));
            case "read_feed_ranges":
                return backend.readFeedRanges(db, container);
            case "read_pkranges":
                return backend.readPkranges(db, container);
            default:
                return OpResult.fail(0, "UnknownAction", "unknown action '" + action + "'");
        }
    }

    private static String str(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private static boolean bool(Object o) {
        return Boolean.TRUE.equals(o) || "true".equals(String.valueOf(o));
    }

    private static int asInt(Object o) {
        if (o instanceof Number) return ((Number) o).intValue();
        return Integer.parseInt(String.valueOf(o));
    }

    private static Integer asIntOrNull(Object o) {
        if (o == null) return null;
        if (o instanceof Number) return ((Number) o).intValue();
        String s = String.valueOf(o);
        return s.isEmpty() ? null : Integer.valueOf(s);
    }
}
