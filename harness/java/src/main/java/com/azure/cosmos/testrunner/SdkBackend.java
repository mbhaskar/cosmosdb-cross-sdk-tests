package com.azure.cosmos.testrunner;

import com.azure.cosmos.CosmosClient;
import com.azure.cosmos.CosmosClientBuilder;
import com.azure.cosmos.ConsistencyLevel;
import com.azure.cosmos.CosmosDiagnostics;
import com.azure.cosmos.CosmosException;
import com.azure.cosmos.models.CosmosBatch;
import com.azure.cosmos.models.CosmosBatchResponse;
import com.azure.cosmos.models.CosmosContainerProperties;
import com.azure.cosmos.models.CosmosItemRequestOptions;
import com.azure.cosmos.models.CosmosItemResponse;
import com.azure.cosmos.models.CosmosQueryRequestOptions;
import com.azure.cosmos.models.PartitionKey;
import com.azure.cosmos.util.CosmosPagedIterable;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** Drives the real azure-cosmos SDK (emulator or live account). */
public class SdkBackend implements Backend {

    private final String endpoint;
    private final String key;
    private final boolean verifyTls;
    private final Boolean endpointDiscovery;
    private final Metrics metrics = new Metrics();
    private CosmosClient client;
    private String consistencyLevel;

    public SdkBackend(String endpoint, String key) {
        this(endpoint, key, true, null);
    }

    /**
     * @param verifyTls          when false the emulator/proxy self-signed cert is
     *                           tolerated (handled via the JVM trust store; see
     *                           scripts/build-java-truststore.sh).
     * @param endpointDiscovery  when non-null, pins {@code endpointDiscoveryEnabled}
     *                           to this value. Set false for single-region fault
     *                           runs so the client stays on the configured proxy
     *                           endpoint instead of adopting the address the
     *                           emulator self-advertises (which bypasses the
     *                           Toxiproxy/mitmproxy chain). Left null (SDK default,
     *                           discovery on) for multi-region/live failover.
     */
    public SdkBackend(String endpoint, String key, boolean verifyTls, Boolean endpointDiscovery) {
        this.endpoint = endpoint;
        this.key = key;
        this.verifyTls = verifyTls;
        this.endpointDiscovery = endpointDiscovery;
    }

    @Override
    public Metrics metrics() {
        return metrics;
    }

    @Override
    public OpResult createClient(String connectionMode) {
        return createClient(connectionMode, null);
    }

    @Override
    public OpResult createClient(String connectionMode, String consistencyLevel) {
        try {
            metrics.connectionMode = connectionMode;
            if (consistencyLevel != null && !consistencyLevel.isEmpty()) {
                this.consistencyLevel = consistencyLevel;
            }
            CosmosClientBuilder builder = new CosmosClientBuilder()
                    .endpoint(endpoint)
                    .key(key);
            if ("direct".equalsIgnoreCase(connectionMode)) {
                builder.directMode();
            } else {
                builder.gatewayMode();
            }
            // Opt-in consistency level (CAP-12). Session consistency is what makes
            // read-your-writes / monotonic-read a client-enforced contract (C-313):
            // the SDK carries the write's session token forward onto the next read.
            if (this.consistencyLevel != null && !this.consistencyLevel.isEmpty()) {
                builder.consistencyLevel(parseConsistency(this.consistencyLevel));
            }
            // Pin the client to the configured endpoint when discovery is disabled
            // (single-region fault runs through the proxy). Left at the SDK default
            // otherwise so multi-region / live failover still works.
            if (endpointDiscovery != null) {
                builder.endpointDiscoveryEnabled(endpointDiscovery);
            }
            client = builder.buildClient();
            metrics.connectionsOpened = 1;
            metrics.incr("get_database_account");
            return OpResult.ok(200);
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    private static ConsistencyLevel parseConsistency(String level) {
        String norm = level.trim().toUpperCase().replace('-', '_').replace(' ', '_');
        switch (norm) {
            case "STRONG": return ConsistencyLevel.STRONG;
            case "BOUNDED_STALENESS": return ConsistencyLevel.BOUNDED_STALENESS;
            case "SESSION": return ConsistencyLevel.SESSION;
            case "CONSISTENT_PREFIX": return ConsistencyLevel.CONSISTENT_PREFIX;
            case "EVENTUAL": return ConsistencyLevel.EVENTUAL;
            default: throw new IllegalArgumentException("unknown consistency level '" + level + "'");
        }
    }

    @Override
    public OpResult createDatabase(String dbId, boolean createIfNotExists) {
        try {
            com.azure.cosmos.models.CosmosDatabaseResponse resp = createIfNotExists
                    ? client.createDatabaseIfNotExists(dbId)
                    : client.createDatabase(dbId);
            return record(OpResult.ok(201, mapId(dbId)), resp.getRequestCharge(), resp.getDiagnostics());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    public OpResult createContainer(String dbId, String containerId, String partitionKey, boolean createIfNotExists) {
        try {
            CosmosContainerProperties props = new CosmosContainerProperties(containerId, partitionKey);
            com.azure.cosmos.models.CosmosContainerResponse resp = createIfNotExists
                    ? client.getDatabase(dbId).createContainerIfNotExists(props)
                    : client.getDatabase(dbId).createContainer(props);
            return record(OpResult.ok(201, mapId(containerId)), resp.getRequestCharge(), resp.getDiagnostics());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult createItem(String dbId, String containerId, Map<String, Object> item) {
        try {
            CosmosItemResponse<Map> resp =
                    client.getDatabase(dbId).getContainer(containerId).createItem(item);
            return record(OpResult.ok(201, bodyOrInput((Map<String, Object>) resp.getItem(), item)),
                    resp.getRequestCharge(), resp.getDiagnostics(), resp.getResponseHeaders());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult readItem(String dbId, String containerId, String itemId, Object partitionKey) {
        try {
            CosmosItemResponse<Map> resp = client.getDatabase(dbId).getContainer(containerId)
                    .readItem(itemId, new PartitionKey(String.valueOf(partitionKey)), Map.class);
            return record(OpResult.ok(200, (Map<String, Object>) resp.getItem()),
                    resp.getRequestCharge(), resp.getDiagnostics(), resp.getResponseHeaders());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    public OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey, Map<String, Object> item) {
        return replaceItem(dbId, containerId, itemId, partitionKey, item, null, null);
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey,
                                Map<String, Object> item, String ifMatch, String ifNoneMatch) {
        try {
            CosmosItemRequestOptions opts = new CosmosItemRequestOptions();
            // Optimistic concurrency (CAP-4): a stale If-Match ETag yields 412
            // PreconditionFailed from the service (surfaced by sdkError) -- D-400.
            if (ifMatch != null && !"null".equals(ifMatch)) {
                opts.setIfMatchETag(ifMatch);
            } else if (ifNoneMatch != null && !"null".equals(ifNoneMatch)) {
                opts.setIfNoneMatchETag(ifNoneMatch);
            }
            CosmosItemResponse<Map> resp = client.getDatabase(dbId).getContainer(containerId)
                    .replaceItem(item, itemId, new PartitionKey(String.valueOf(partitionKey)), opts);
            return record(OpResult.ok(200, bodyOrInput((Map<String, Object>) resp.getItem(), item)),
                    resp.getRequestCharge(), resp.getDiagnostics(), resp.getResponseHeaders());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult upsertItem(String dbId, String containerId, Map<String, Object> item) {
        try {
            CosmosItemResponse<Map> resp =
                    client.getDatabase(dbId).getContainer(containerId).upsertItem(item);
            return record(OpResult.ok(200, bodyOrInput((Map<String, Object>) resp.getItem(), item)),
                    resp.getRequestCharge(), resp.getDiagnostics(), resp.getResponseHeaders());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    public OpResult deleteItem(String dbId, String containerId, String itemId, Object partitionKey) {
        try {
            CosmosItemResponse<Object> resp = client.getDatabase(dbId).getContainer(containerId)
                    .deleteItem(itemId, new PartitionKey(String.valueOf(partitionKey)), new CosmosItemRequestOptions());
            return record(OpResult.ok(204), resp.getRequestCharge(), resp.getDiagnostics());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult queryItems(String dbId, String containerId, String query,
                               List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition) {
        try {
            CosmosQueryRequestOptions options = new CosmosQueryRequestOptions();
            if (partitionKey != null) {
                options.setPartitionKey(new PartitionKey(String.valueOf(partitionKey)));
            }
            // Parameterized query string is interpolated by the runner's substitution for the
            // mock; for the real SDK we pass the raw query (named params bind server-side in a
            // fuller implementation). MVP scenarios use simple equality predicates.
            String finalQuery = query;
            if (parameters != null) {
                for (Map<String, Object> p : parameters) {
                    finalQuery = finalQuery.replace(String.valueOf(p.get("name")),
                            "'" + String.valueOf(p.get("value")) + "'");
                }
            }
            CosmosPagedIterable<Map> it = client.getDatabase(dbId).getContainer(containerId)
                    .queryItems(finalQuery, options, Map.class);
            List<Object> rows = new ArrayList<>();
            CosmosDiagnostics lastDiag = null;
            double totalCharge = 0.0;
            for (com.azure.cosmos.models.FeedResponse<Map> page : it.iterableByPage()) {
                rows.addAll(page.getResults());
                lastDiag = page.getCosmosDiagnostics();
                totalCharge += page.getRequestCharge();
            }
            OpResult r = OpResult.ok(200);
            r.items = rows;
            return record(r, totalCharge, lastDiag);
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult queryDrain(String dbId, String containerId, String query,
                               List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition,
                               Integer maxItemCount, Integer maxPages, String continuation) {
        // Real paged drain (CAP-6): uses the SDK's own iterableByPage() so the
        // continuation token is server-minted and echoed forward exactly as an
        // application would. When `continuation` is supplied the drain RESUMES from
        // that token (proving it survives a split mid-drain, C-311); `maxPages`
        // stops early so a caller can capture the token after the first page (D-403).
        try {
            CosmosQueryRequestOptions options = new CosmosQueryRequestOptions();
            if (partitionKey != null) {
                options.setPartitionKey(new PartitionKey(String.valueOf(partitionKey)));
            }
            String finalQuery = query;
            if (parameters != null) {
                for (Map<String, Object> p : parameters) {
                    finalQuery = finalQuery.replace(String.valueOf(p.get("name")),
                            "'" + String.valueOf(p.get("value")) + "'");
                }
            }
            CosmosPagedIterable<Map> it = client.getDatabase(dbId).getContainer(containerId)
                    .queryItems(finalQuery, options, Map.class);
            int pageSize = (maxItemCount != null && maxItemCount > 0) ? maxItemCount : 100;
            boolean hasCont = continuation != null && !continuation.isEmpty() && !"null".equals(continuation);
            Iterable<com.azure.cosmos.models.FeedResponse<Map>> pages =
                    hasCont ? it.iterableByPage(continuation, pageSize) : it.iterableByPage(pageSize);
            List<Object> rows = new ArrayList<>();
            int pages_seen = 0;
            String lastToken = null;
            CosmosDiagnostics lastDiag = null;
            double totalCharge = 0.0;
            for (com.azure.cosmos.models.FeedResponse<Map> page : pages) {
                rows.addAll(page.getResults());
                lastToken = page.getContinuationToken();
                lastDiag = page.getCosmosDiagnostics();
                totalCharge += page.getRequestCharge();
                pages_seen++;
                if (maxPages != null && pages_seen >= maxPages) {
                    break;
                }
            }
            OpResult r = OpResult.ok(200);
            r.items = rows;
            r.continuation = lastToken;
            r.pageCount = pages_seen;
            return record(r, totalCharge, lastDiag);
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult executeBatch(String dbId, String containerId,
                                 List<Map<String, Object>> operations, Object partitionKey) {
        // Transactional batch (CAP-2): all-or-nothing on one partition key. On a
        // failing sub-operation the Java SDK returns a non-success batch response
        // (no throw) and rolls back every op -- D-401 then reads a would-be-created
        // item back and asserts 404 to prove atomicity.
        try {
            CosmosBatch batch = CosmosBatch.createCosmosBatch(new PartitionKey(String.valueOf(partitionKey)));
            for (Map<String, Object> op : (operations == null ? new ArrayList<Map<String, Object>>() : operations)) {
                String kind = String.valueOf(op.get("op"));
                switch (kind) {
                    case "create":
                        batch.createItemOperation(op.get("item"));
                        break;
                    case "upsert":
                        batch.upsertItemOperation(op.get("item"));
                        break;
                    case "replace":
                        batch.replaceItemOperation(String.valueOf(op.get("id")), op.get("item"));
                        break;
                    case "read":
                        batch.readItemOperation(String.valueOf(op.get("id")));
                        break;
                    case "delete":
                        batch.deleteItemOperation(String.valueOf(op.get("id")));
                        break;
                    default:
                        return OpResult.fail(0, "UnknownBatchOp", "unknown batch op '" + kind + "'");
                }
            }
            CosmosBatchResponse resp = client.getDatabase(dbId).getContainer(containerId)
                    .executeCosmosBatch(batch);
            if (!resp.isSuccessStatusCode()) {
                return record(OpResult.fail(resp.getStatusCode(), "Cosmos" + resp.getStatusCode(),
                        "transactional batch failed and rolled back"), resp.getRequestCharge(), resp.getDiagnostics());
            }
            List<Object> rows = new ArrayList<>();
            for (com.azure.cosmos.models.CosmosBatchOperationResult br : resp.getResults()) {
                try {
                    Map<String, Object> m = br.getItem(Map.class);
                    if (m != null) {
                        rows.add(m);
                    }
                } catch (Exception ignored) {
                    // read/delete ops may carry no body -- skip.
                }
            }
            OpResult r = OpResult.ok(200);
            r.items = rows;
            return record(r, resp.getRequestCharge(), resp.getDiagnostics());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    public OpResult deleteDatabase(String dbId) {
        try {
            com.azure.cosmos.models.CosmosDatabaseResponse resp = client.getDatabase(dbId).delete();
            return record(OpResult.ok(204), resp.getRequestCharge(), resp.getDiagnostics());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    public OpResult readFeedRanges(String dbId, String containerId) {
        // Drives the real routing-map / feed-range cache: the SDK fetches every
        // pkranges page and returns one FeedRange per partition key range. Against
        // a mitmproxy-synthesized topology this yields M ranges over a single
        // emulator partition.
        try {
            List<com.azure.cosmos.models.FeedRange> ranges =
                    client.getDatabase(dbId).getContainer(containerId).getFeedRanges();
            List<Object> items = new ArrayList<>();
            int i = 0;
            for (com.azure.cosmos.models.FeedRange fr : ranges) {
                Map<String, Object> m = new java.util.LinkedHashMap<>();
                m.put("id", String.valueOf(i++));
                m.put("feed_range", String.valueOf(fr));
                items.add(m);
            }
            OpResult r = OpResult.ok(200);
            r.items = items;
            return r;
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    public OpResult readPkranges(String dbId, String containerId) {
        // Engine ground-truth: read the gateway's raw pkranges REST resource,
        // bypassing the SDK routing cache entirely. The in-memory emulator serves
        // cleartext HTTP and validates no credentials, so a bare GET suffices; the
        // path carries no trailing slash (the gateway rejects those, and the
        // normalizer only fixes SDK traffic). Returns one item per real range.
        String url = endpoint.replaceAll("/+$", "")
                + "/dbs/" + dbId + "/colls/" + containerId + "/pkranges";
        try {
            java.net.http.HttpClient hc = java.net.http.HttpClient.newBuilder()
                    .connectTimeout(java.time.Duration.ofSeconds(10)).build();
            java.net.http.HttpRequest req = java.net.http.HttpRequest.newBuilder()
                    .uri(java.net.URI.create(url))
                    .timeout(java.time.Duration.ofSeconds(15))
                    .GET().build();
            java.net.http.HttpResponse<String> resp =
                    hc.send(req, java.net.http.HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() >= 400) {
                return OpResult.fail(resp.statusCode(), "PkRangesError",
                        "pkranges GET " + url + " -> " + resp.statusCode() + ": " + resp.body());
            }
            com.fasterxml.jackson.databind.JsonNode root = DIAG_MAPPER.readTree(resp.body());
            com.fasterxml.jackson.databind.JsonNode ranges = root.get("PartitionKeyRanges");
            List<Object> items = new ArrayList<>();
            if (ranges != null && ranges.isArray()) {
                for (com.fasterxml.jackson.databind.JsonNode r : ranges) {
                    Map<String, Object> m = new java.util.LinkedHashMap<>();
                    m.put("id", r.hasNonNull("id") ? r.get("id").asText() : null);
                    m.put("min", r.hasNonNull("minInclusive") ? r.get("minInclusive").asText() : null);
                    m.put("max", r.hasNonNull("maxExclusive") ? r.get("maxExclusive").asText() : null);
                    items.add(m);
                }
            }
            OpResult r = OpResult.ok(200);
            r.items = items;
            return r;
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    private OpResult sdkError(Exception e) {
        int status = 0;
        String code = e.getClass().getSimpleName();
        if (e instanceof CosmosException) {
            CosmosException ce = (CosmosException) e;
            status = ce.getStatusCode();
            code = "Cosmos" + ce.getStatusCode();
            // Failed requests still consume RU (e.g. a 409 conflict on create).
            return record(OpResult.fail(status, code, e.getMessage()),
                    ce.getRequestCharge(), ce.getDiagnostics());
        }
        return OpResult.fail(status, code, e.getMessage());
    }

    /** Charge the real server-reported RU and attach the CosmosDiagnostics payload. */
    private OpResult record(OpResult r, double requestCharge, CosmosDiagnostics d) {
        r.ru = metrics.charge(requestCharge);
        if (d != null) {
            String text = d.toString();
            r.diagnostics = text;
            metrics.retries += parseRetries(text);
        }
        return r;
    }

    /** Same, plus the response headers (lower-cased) for diagnostic_present (CAP-12). */
    private OpResult record(OpResult r, double requestCharge, CosmosDiagnostics d, Map<String, String> headers) {
        record(r, requestCharge, d);
        if (headers != null) {
            Map<String, Object> lc = new java.util.LinkedHashMap<>();
            for (Map.Entry<String, String> e : headers.entrySet()) {
                lc.put(String.valueOf(e.getKey()).toLowerCase(), e.getValue());
            }
            Map<String, String> lcStr = new java.util.LinkedHashMap<>();
            for (Map.Entry<String, Object> e : lc.entrySet()) {
                lcStr.put(e.getKey(), e.getValue() == null ? null : String.valueOf(e.getValue()));
            }
            r.diagnosticHeaders = lcStr;
        }
        return r;
    }

    private static final ObjectMapper DIAG_MAPPER = new ObjectMapper();

    /**
     * Extract the real retry count from a CosmosDiagnostics JSON payload. The SDK
     * records one or more "retryContext" nodes carrying a "retryCount" field; take
     * the max seen so nested/duplicate contexts within a single request are not
     * double-counted.
     */
    private static int parseRetries(String diagnosticsJson) {
        if (diagnosticsJson == null || diagnosticsJson.isEmpty()) {
            return 0;
        }
        try {
            JsonNode root = DIAG_MAPPER.readTree(diagnosticsJson);
            return maxRetryCount(root);
        } catch (Exception ignored) {
            return 0;
        }
    }

    private static int maxRetryCount(JsonNode node) {
        int max = 0;
        if (node == null) {
            return 0;
        }
        if (node.isObject()) {
            JsonNode rc = node.get("retryCount");
            if (rc != null && rc.isInt()) {
                max = Math.max(max, rc.asInt());
            }
            for (JsonNode child : node) {
                max = Math.max(max, maxRetryCount(child));
            }
        } else if (node.isArray()) {
            for (JsonNode child : node) {
                max = Math.max(max, maxRetryCount(child));
            }
        }
        return max;
    }

    private static Map<String, Object> mapId(String id) {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("id", id);
        return m;
    }

    /**
     * The Java SDK returns null from getItem() when content-response-on-write is
     * disabled. Fall back to the request body (which carries the id) so write
     * assertions like field_equals(item.id) behave the same as the Python SDK.
     */
    private static Map<String, Object> bodyOrInput(Map<String, Object> responseItem, Map<String, Object> input) {
        return responseItem != null ? responseItem : input;
    }
}
