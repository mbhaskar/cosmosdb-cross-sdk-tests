package com.azure.cosmos.testrunner;

import com.azure.cosmos.CosmosClient;
import com.azure.cosmos.CosmosClientBuilder;
import com.azure.cosmos.CosmosDiagnostics;
import com.azure.cosmos.CosmosException;
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
        try {
            metrics.connectionMode = connectionMode;
            CosmosClientBuilder builder = new CosmosClientBuilder()
                    .endpoint(endpoint)
                    .key(key);
            if ("direct".equalsIgnoreCase(connectionMode)) {
                builder.directMode();
            } else {
                builder.gatewayMode();
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
                    resp.getRequestCharge(), resp.getDiagnostics());
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
                    resp.getRequestCharge(), resp.getDiagnostics());
        } catch (Exception e) {
            return sdkError(e);
        }
    }

    @Override
    @SuppressWarnings("unchecked")
    public OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey, Map<String, Object> item) {
        try {
            CosmosItemResponse<Map> resp = client.getDatabase(dbId).getContainer(containerId)
                    .replaceItem(item, itemId, new PartitionKey(String.valueOf(partitionKey)),
                            new CosmosItemRequestOptions());
            return record(OpResult.ok(200, bodyOrInput((Map<String, Object>) resp.getItem(), item)),
                    resp.getRequestCharge(), resp.getDiagnostics());
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
                    resp.getRequestCharge(), resp.getDiagnostics());
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
    public OpResult deleteDatabase(String dbId) {
        try {
            com.azure.cosmos.models.CosmosDatabaseResponse resp = client.getDatabase(dbId).delete();
            return record(OpResult.ok(204), resp.getRequestCharge(), resp.getDiagnostics());
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
