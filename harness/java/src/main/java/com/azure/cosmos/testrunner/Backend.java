package com.azure.cosmos.testrunner;

import java.util.List;
import java.util.Map;

/** Common interface implemented by the mock and real-SDK backends. */
public interface Backend {
    Metrics metrics();

    OpResult createClient(String connectionMode);

    /**
     * Create the client with an explicit consistency level (CAP-12, e.g. Session
     * for read-your-writes in C-313). Default ignores consistency and delegates to
     * the single-arg form; the real SDK backend overrides this.
     */
    default OpResult createClient(String connectionMode, String consistencyLevel) {
        return createClient(connectionMode);
    }

    OpResult createDatabase(String dbId, boolean createIfNotExists);

    OpResult createContainer(String dbId, String containerId, String partitionKey, boolean createIfNotExists);

    OpResult createItem(String dbId, String containerId, Map<String, Object> item);

    OpResult readItem(String dbId, String containerId, String itemId, Object partitionKey);

    OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey, Map<String, Object> item);

    /**
     * Replace with optimistic-concurrency preconditions (CAP-4). When {@code ifMatch}
     * is set the SDK sends If-Match; a stale ETag yields 412 (D-400). Default drops
     * the preconditions and delegates; the real SDK backend overrides this.
     */
    default OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey,
                                 Map<String, Object> item, String ifMatch, String ifNoneMatch) {
        return replaceItem(dbId, containerId, itemId, partitionKey, item);
    }

    OpResult upsertItem(String dbId, String containerId, Map<String, Object> item);

    OpResult deleteItem(String dbId, String containerId, String itemId, Object partitionKey);

    OpResult queryItems(String dbId, String containerId, String query,
                        List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition);

    /**
     * Real paged drain (CAP-6). Follows server-minted continuation tokens page by
     * page. {@code maxItemCount} sets the page size, {@code maxPages} stops early
     * (exposing {@link OpResult#continuation} for a later resume), and
     * {@code continuation} resumes from a previously captured token -- proving the
     * token survives a topology change (C-311) or is exact across pages (D-403).
     * Default delegates to {@link #queryItems} (single fetch) so non-SDK backends
     * still function; the real SDK backend overrides this.
     */
    default OpResult queryDrain(String dbId, String containerId, String query,
                                List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition,
                                Integer maxItemCount, Integer maxPages, String continuation) {
        OpResult r = queryItems(dbId, containerId, query, parameters, partitionKey, crossPartition);
        r.pageCount = 1;
        return r;
    }

    /**
     * Transactional batch (CAP-2): all-or-nothing on one partition key. A failing
     * sub-operation fails the whole batch and rolls back every operation (D-401).
     * Default is unsupported; the real SDK backend overrides this.
     */
    default OpResult executeBatch(String dbId, String containerId,
                                  List<Map<String, Object>> operations, Object partitionKey) {
        return OpResult.fail(0, "NotImplemented", "execute_batch not supported by this backend");
    }

    OpResult deleteDatabase(String dbId);

    /**
     * Read the container's feed ranges (one per physical partition key range).
     * Drives the SDK's routing-map / feed-range cache: it fetches and assembles
     * every pkranges page. Against a mitmproxy-synthesized topology this yields M
     * ranges over a single-partition emulator (mirrors Python read_feed_ranges).
     * {@code items} carries one entry per returned range.
     */
    OpResult readFeedRanges(String dbId, String containerId);

    /**
     * Read the <em>engine's</em> raw {@code /pkranges} for the container (one item
     * per physical partition key range) directly from the gateway, bypassing the
     * SDK's routing-map cache. Where {@link #readFeedRanges} reflects what the SDK
     * believes (and may coalesce freshly-split siblings back into the parent EPK
     * span, or fail entirely against this emulator's gateway), this returns the
     * engine's ground-truth topology. Used by the in-memory emulator tier to
     * deterministically observe a real split/merge (mirrors Python read_pkranges).
     */
    OpResult readPkranges(String dbId, String containerId);

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
