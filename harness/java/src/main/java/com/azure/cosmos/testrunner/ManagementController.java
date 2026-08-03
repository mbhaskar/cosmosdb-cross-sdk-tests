package com.azure.cosmos.testrunner;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Drives the in-memory Rust emulator's REAL control plane via its management REST
 * API (see {@code emulator/inmemory}; contract from azure-sdk-for-rust
 * {@code azure_data_cosmos_emulator/src/management.rs}). Mirrors the Python
 * {@code faults.ManagementController}.
 *
 * <p>Unlike the mitmproxy topology <em>synthesis</em> used by C-220 (which
 * fabricates a pkranges response so the SDK's cache assembles M ranges over a
 * single-partition emulator), this controller asks the real engine to change its
 * physical topology: split/merge partitions, pause/resume region replication,
 * toggle per-partition failover. The gateway then advertises the new
 * {@code /pkranges} for real, and an unmodified SDK observes it.
 *
 * <p>Cleartext HTTP; the emulator validates no credentials. The control channel is
 * the management endpoint ({@code $COSMOS_MANAGEMENT_ENDPOINT} / the resolved
 * {@code management_endpoint}), distinct from the data-plane gateway endpoint.
 */
public class ManagementController {

    /** Extra settle time (ms) after an op reports terminal, so the gateway's
     *  pkranges read path reflects the new routing map before the next read. */
    private static final long SETTLE_MS = 1000L;
    private static final long POLL_TIMEOUT_MS = 30_000L;
    private static final long POLL_INTERVAL_MS = 250L;

    private final String controlEndpoint;
    private final HttpClient http;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    public ManagementController(String controlEndpoint) {
        String ep = controlEndpoint;
        if (ep == null || ep.isEmpty()) {
            ep = System.getenv().getOrDefault("COSMOS_MANAGEMENT_ENDPOINT", "http://localhost:49150");
        }
        this.controlEndpoint = ep.replaceAll("/+$", "");
        this.http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    }

    private JsonNode request(String method, String path, String body) {
        try {
            HttpRequest.Builder b = HttpRequest.newBuilder()
                    .uri(URI.create(controlEndpoint + path))
                    .timeout(Duration.ofSeconds(15));
            if (body != null) {
                b.header("Content-Type", "application/json");
                b.method(method, HttpRequest.BodyPublishers.ofString(body));
            } else {
                b.method(method, HttpRequest.BodyPublishers.noBody());
            }
            HttpResponse<String> resp = http.send(b.build(), HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() >= 400) {
                throw new RuntimeException(method + " " + path + " -> HTTP " + resp.statusCode()
                        + ": " + resp.body());
            }
            String raw = resp.body();
            return (raw == null || raw.isEmpty()) ? null : MAPPER.readTree(raw);
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new RuntimeException("cannot reach emulator management API at " + controlEndpoint
                    + ": " + e.getMessage() + ". Start emulator/inmemory (see "
                    + "scripts/run-inmemory-emulator.sh) first.", e);
        }
    }

    /** Poll {@code GET /operations/{id}} until the long-running op is terminal. */
    private JsonNode awaitOperation(String operationId) {
        long deadline = System.currentTimeMillis() + POLL_TIMEOUT_MS;
        JsonNode last = null;
        while (System.currentTimeMillis() < deadline) {
            last = request("GET", "/operations/" + operationId, null);
            String status = last != null && last.hasNonNull("status") ? last.get("status").asText() : "";
            if ("Succeeded".equals(status) || "Failed".equals(status)) {
                if ("Failed".equals(status)) {
                    throw new RuntimeException("operation " + operationId + " failed: " + last);
                }
                sleep(SETTLE_MS);
                return last;
            }
            sleep(POLL_INTERVAL_MS);
        }
        throw new RuntimeException("operation " + operationId + " did not terminate within "
                + (POLL_TIMEOUT_MS / 1000) + "s (last=" + last + ")");
    }

    /** Apply a single management timeline verb. */
    public void apply(String event, Map<String, Object> args, String dbId, String containerId) {
        Map<String, Object> a = args != null ? args : new LinkedHashMap<>();
        switch (event) {
            case "split_partition": {
                requireCtx(dbId, containerId, "split_partition");
                Object pid = a.getOrDefault("partition_id", 0);
                Map<String, Object> body = new LinkedHashMap<>();
                if (a.get("mode") != null) body.put("mode", a.get("mode"));   // midpoint|epk|storage
                if (a.get("epk") != null) body.put("epk", a.get("epk"));
                JsonNode op = request("POST",
                        "/databases/" + dbId + "/containers/" + containerId + "/partitions/" + pid + "/split",
                        body.isEmpty() ? null : toJson(body));
                maybeAwait(a, op);
                break;
            }
            case "merge_partitions": {
                requireCtx(dbId, containerId, "merge_partitions");
                String body = a.get("partitions") != null
                        ? toJson(Map.of("partitions", a.get("partitions"))) : null;
                JsonNode op = request("POST",
                        "/databases/" + dbId + "/containers/" + containerId + "/partitions/merge", body);
                maybeAwait(a, op);
                break;
            }
            case "pause_replication":
            case "resume_replication": {
                String region = String.valueOf(a.get("region"));
                String verb = "pause_replication".equals(event) ? "pause" : "resume";
                request("POST", "/regions/" + region + "/replication/" + verb, null);
                break;
            }
            case "set_per_partition_failover": {
                boolean enabled = a.get("enabled") == null || Boolean.TRUE.equals(a.get("enabled"))
                        || "true".equals(String.valueOf(a.get("enabled")));
                request("PUT", "/config/per-partition-failover", toJson(Map.of("enabled", enabled)));
                break;
            }
            default:
                throw new IllegalArgumentException("unknown management event '" + event + "'");
        }
    }

    private void maybeAwait(Map<String, Object> a, JsonNode op) {
        boolean wait = !a.containsKey("wait") || Boolean.TRUE.equals(a.get("wait"))
                || "true".equals(String.valueOf(a.get("wait")));
        if (wait && op != null && op.hasNonNull("operationId")) {
            awaitOperation(op.get("operationId").asText());
        }
    }

    public void reset() {
        // The management plane has no blanket "undo"; topology changes persist for
        // the emulator's lifetime. Auto-namespaced fixture databases keep each run
        // isolated, so reset is a best-effort no-op.
    }

    private static void requireCtx(String dbId, String containerId, String verb) {
        if (dbId == null || containerId == null) {
            throw new RuntimeException(verb + " needs a fixture db + container");
        }
    }

    private static String toJson(Map<String, Object> m) {
        try {
            return MAPPER.writeValueAsString(m);
        } catch (Exception e) {
            throw new RuntimeException("failed to serialize management body: " + e.getMessage(), e);
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
