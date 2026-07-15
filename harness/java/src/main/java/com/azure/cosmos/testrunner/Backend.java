package com.azure.cosmos.testrunner;

import java.util.List;
import java.util.Map;

/** Common interface implemented by the mock and real-SDK backends. */
public interface Backend {
    Metrics metrics();

    OpResult createClient(String connectionMode);

    OpResult createDatabase(String dbId, boolean createIfNotExists);

    OpResult createContainer(String dbId, String containerId, String partitionKey, boolean createIfNotExists);

    OpResult createItem(String dbId, String containerId, Map<String, Object> item);

    OpResult readItem(String dbId, String containerId, String itemId, Object partitionKey);

    OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey, Map<String, Object> item);

    OpResult upsertItem(String dbId, String containerId, Map<String, Object> item);

    OpResult deleteItem(String dbId, String containerId, String itemId, Object partitionKey);

    OpResult queryItems(String dbId, String containerId, String query,
                        List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition);

    OpResult deleteDatabase(String dbId);

    /**
     * Bulk-seed {@code count} items by expanding {@code {n}} in string template
     * values (n = 1..count). Implemented once here over {@link #createItem} so it
     * behaves identically on every backend (mirrors the Python Backend.seed_items).
     * Returns a single aggregate result (ok only if every insert succeeded).
     */
    default OpResult seedItems(String dbId, String containerId, int count, Map<String, Object> template) {
        java.util.List<Object> created = new java.util.ArrayList<>();
        boolean allOk = true;
        OpResult last = OpResult.ok(201);
        for (int n = 1; n <= count; n++) {
            Map<String, Object> item = new java.util.LinkedHashMap<>();
            for (Map.Entry<String, Object> e : template.entrySet()) {
                Object v = e.getValue();
                item.put(e.getKey(), v instanceof String ? ((String) v).replace("{n}", String.valueOf(n)) : v);
            }
            last = createItem(dbId, containerId, item);
            allOk = allOk && last.ok;
            if (last.ok && last.item != null) {
                created.add(last.item);
            }
        }
        OpResult agg = allOk
                ? OpResult.ok(201)
                : OpResult.fail(last.statusCode, last.errorCode, "seed_items: one or more inserts failed");
        agg.items = created;
        return agg;
    }
}
