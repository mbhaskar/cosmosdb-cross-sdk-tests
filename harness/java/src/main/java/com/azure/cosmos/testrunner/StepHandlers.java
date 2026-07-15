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
                        ctx.getOrDefault("connection_mode", "gateway"))));
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
                        (Map<String, Object>) params.get("item"));
            case "upsert_item":
                return backend.upsertItem(db, container, (Map<String, Object>) params.get("item"));
            case "delete_item":
                return backend.deleteItem(db, container, str(params.get("id")), params.get("partition_key"));
            case "query_items":
                return backend.queryItems(db, container, str(params.get("query")),
                        (List<Map<String, Object>>) params.get("parameters"),
                        params.get("partition_key"), bool(params.get("cross_partition")));
            case "query_drain":
                // Drain a (paginated) query to exhaustion. Defaults to cross-partition
                // so the SDK streams every page under whatever transport conditions
                // are active (mirrors the Python query_drain).
                return backend.queryItems(db, container, str(params.get("query")),
                        (List<Map<String, Object>>) params.get("parameters"),
                        params.get("partition_key"),
                        params.containsKey("cross_partition") ? bool(params.get("cross_partition")) : true);
            case "delete_database":
                return backend.deleteDatabase(str(params.get("id")));
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
}
