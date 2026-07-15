package com.azure.cosmos.testrunner;

import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Layer-4 (TCP) transport-fault control via the Toxiproxy admin API. Mirrors the
 * Python {@code faults.ProxyFaultController}.
 *
 * <p>Toxiproxy sits between the SDK runner and the emulator/live backend and
 * injects real socket faults (latency, resets, bandwidth caps, timeouts). Timeline
 * verbs starting with {@code net_} (plus {@code region_down}/{@code region_up}/
 * {@code reset_faults}) are routed here by the executor when the backend is not
 * {@code mock}. Each verb maps to one toxic added to (or removed from) a named
 * proxy via the admin API (default {@code http://localhost:8474} /
 * {@code $TOXIPROXY_URL}).
 *
 * <p>Tracks the toxics it added so {@code reset}/{@code region_up} remove exactly
 * those (and nothing an operator added out of band).
 */
public class ProxyFaultController {

    private final String adminUrl;
    private final String proxy;
    private final String secondaryProxy;
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10)).build();
    private final ObjectMapper mapper = new ObjectMapper();
    // proxy name -> list of toxic names we created (for targeted cleanup).
    private final Map<String, List<String>> added = new LinkedHashMap<>();

    public ProxyFaultController(String adminUrl, String proxy, String secondaryProxy) {
        String url = adminUrl;
        if (url == null || url.isEmpty()) {
            url = System.getenv().getOrDefault("TOXIPROXY_URL", "http://localhost:8474");
        }
        this.adminUrl = url.replaceAll("/+$", "");
        this.proxy = proxy != null ? proxy : "cosmos";
        this.secondaryProxy = secondaryProxy != null ? secondaryProxy : "cosmos-secondary";
    }

    // -- HTTP plumbing ----------------------------------------------------- //

    private void request(String method, String path, Map<String, Object> body) {
        try {
            HttpRequest.BodyPublisher pub = body != null
                    ? HttpRequest.BodyPublishers.ofString(mapper.writeValueAsString(body))
                    : HttpRequest.BodyPublishers.noBody();
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(adminUrl + path))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .method(method, pub)
                    .build();
            HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() >= 400 && resp.statusCode() != 404) {
                throw new RuntimeException("Toxiproxy " + method + " " + path
                        + " -> HTTP " + resp.statusCode() + ": " + resp.body());
            }
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new RuntimeException("cannot reach Toxiproxy at " + adminUrl + ": " + e.getMessage()
                    + ". Start proxy/docker-compose.proxy.yaml first.", e);
        }
    }

    // -- toxic verbs ------------------------------------------------------- //

    private void addToxic(String targetProxy, String name, String type, String stream,
                          Map<String, Object> attributes) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("name", name);
        body.put("type", type);
        body.put("stream", stream);
        body.put("toxicity", 1.0);
        body.put("attributes", attributes);
        request("POST", "/proxies/" + targetProxy + "/toxics", body);
        added.computeIfAbsent(targetProxy, k -> new ArrayList<>()).add(name);
    }

    private void removeToxic(String targetProxy, String name) {
        try {
            request("DELETE", "/proxies/" + targetProxy + "/toxics/" + name, null);
        } catch (RuntimeException ignored) {
            // already gone
        }
        List<String> names = added.get(targetProxy);
        if (names != null) {
            names.remove(name);
        }
    }

    /** Apply a single transport-fault timeline verb. */
    public void apply(String event, Map<String, Object> args) {
        Map<String, Object> a = args != null ? args : new LinkedHashMap<>();
        switch (event) {
            case "net_latency":
                addToxic(proxy, "net_latency", "latency", stream(a, "downstream"),
                        attrs("latency", asInt(a.getOrDefault("latency_ms", 500)),
                              "jitter", asInt(a.getOrDefault("jitter_ms", 0))));
                break;
            case "net_timeout":
                addToxic(proxy, "net_timeout", "timeout", stream(a, "upstream"),
                        attrs("timeout", asInt(a.getOrDefault("timeout_ms", 0))));
                break;
            case "net_reset":
                addToxic(proxy, "net_reset", "reset_peer", stream(a, "downstream"),
                        attrs("timeout", asInt(a.getOrDefault("after_ms", 0))));
                break;
            case "net_bandwidth":
                addToxic(proxy, "net_bandwidth", "bandwidth", stream(a, "downstream"),
                        attrs("rate", asInt(a.getOrDefault("rate_kbps", 64))));
                break;
            case "net_slow_close":
                addToxic(proxy, "net_slow_close", "slow_close", stream(a, "downstream"),
                        attrs("delay", asInt(a.getOrDefault("delay_ms", 1000))));
                break;
            case "region_down":
                // Black-hole the primary region so preferred_regions failover kicks in.
                addToxic(proxy, "region_down", "timeout", "upstream", attrs("timeout", 0));
                break;
            case "region_up":
                removeToxic(proxy, "region_down");
                break;
            case "reset_faults":
                reset();
                break;
            default:
                throw new IllegalArgumentException("unknown transport-fault event '" + event + "'");
        }
    }

    /** Remove every toxic this controller added (leaves proxies enabled). */
    public void reset() {
        for (Map.Entry<String, List<String>> e : new LinkedHashMap<>(added).entrySet()) {
            for (String name : new ArrayList<>(e.getValue())) {
                removeToxic(e.getKey(), name);
            }
        }
    }

    private static String stream(Map<String, Object> a, String dflt) {
        Object s = a.get("stream");
        return s != null ? String.valueOf(s) : dflt;
    }

    private static Map<String, Object> attrs(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) {
            m.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return m;
    }

    private static int asInt(Object o) {
        if (o instanceof Number) return ((Number) o).intValue();
        return Integer.parseInt(String.valueOf(o));
    }
}
