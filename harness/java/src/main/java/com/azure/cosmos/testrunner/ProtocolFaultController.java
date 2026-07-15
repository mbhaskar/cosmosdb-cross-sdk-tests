package com.azure.cosmos.testrunner;

import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.cert.X509Certificate;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Layer-7 (HTTP) protocol-fault control via the mitmproxy addon
 * ({@code proxy/mitm/fault_addon.py}). Mirrors the Python
 * {@code faults.ProtocolFaultController}.
 *
 * <p>Toxiproxy is TCP-only and cannot emit an HTTP status code, so protocol faults
 * (429 throttle, 410 Gone, 449 RetryWith, 503) are injected by the mitmproxy addon
 * instead. The control channel is the magic {@code /__fault/*} path the addon
 * serves on the same proxy endpoint the SDK talks to (default {@code $MITM_ENDPOINT}
 * / the scenario's proxy endpoint).
 *
 * <p>The control endpoint uses a self-signed cert and the call is local and carries
 * no secrets, so this client skips TLS verification for the {@code /__fault/*} POSTs
 * only (the SDK data-plane trust is handled separately via the JVM trust store).
 */
public class ProtocolFaultController {

    private final String controlEndpoint;
    private final HttpClient http;

    public ProtocolFaultController(String controlEndpoint) {
        String ep = controlEndpoint;
        if (ep == null || ep.isEmpty()) {
            ep = System.getenv().getOrDefault("MITM_ENDPOINT", "https://localhost:18091");
        }
        this.controlEndpoint = ep.replaceAll("/+$", "");
        this.http = insecureClient();
    }

    private static HttpClient insecureClient() {
        try {
            TrustManager[] trustAll = new TrustManager[]{new X509TrustManager() {
                public void checkClientTrusted(X509Certificate[] c, String a) { }
                public void checkServerTrusted(X509Certificate[] c, String a) { }
                public X509Certificate[] getAcceptedIssuers() { return new X509Certificate[0]; }
            }};
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, trustAll, new java.security.SecureRandom());
            // Also disable hostname verification for the local control endpoint.
            SSLParameters params = new SSLParameters();
            params.setEndpointIdentificationAlgorithm(null);
            return HttpClient.newBuilder()
                    .sslContext(ctx)
                    .sslParameters(params)
                    .connectTimeout(Duration.ofSeconds(10))
                    .build();
        } catch (Exception e) {
            throw new RuntimeException("failed to build insecure control client: " + e.getMessage(), e);
        }
    }

    private void post(String path) {
        try {
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(controlEndpoint + path))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.noBody())
                    .build();
            HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() >= 400) {
                throw new RuntimeException("POST " + path + " -> HTTP " + resp.statusCode() + ": " + resp.body());
            }
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new RuntimeException("cannot reach mitmproxy control at " + controlEndpoint
                    + ": " + e.getMessage() + ". Start proxy/mitm (see proxy/README.md) first.", e);
        }
    }

    /** Apply a single protocol-fault timeline verb. */
    public void apply(String event, Map<String, Object> args) {
        Map<String, Object> a = args != null ? args : new LinkedHashMap<>();
        if ("net_throttle_window".equals(event) || "inject_fault".equals(event)) {
            // net_throttle_window is the original 429-only verb; inject_fault is the
            // generic form that names any fault in the mitm registry (throttle_429,
            // gone_410, namecache_410, retrywith_449, ...).
            String fault = String.valueOf(a.getOrDefault("fault", "throttle_429"));
            List<String> qs = new ArrayList<>();
            qs.add("fault=" + fault);
            // Scope: time window (seconds) OR first-N requests (count).
            if (a.get("count") != null) {
                qs.add("count=" + a.get("count"));
            } else {
                qs.add("seconds=" + a.getOrDefault("seconds", 120));
            }
            // Optional ad-hoc overrides.
            for (String k : new String[]{"status", "substatus", "retry_after_ms"}) {
                if (a.get(k) != null) {
                    qs.add(k + "=" + a.get(k));
                }
            }
            post("/__fault/arm?" + String.join("&", qs));
        } else if ("throttle_window_clear".equals(event) || "fault_clear".equals(event)) {
            post("/__fault/clear");
        } else {
            throw new IllegalArgumentException("unknown protocol-fault event '" + event + "'");
        }
    }

    public void reset() {
        try {
            post("/__fault/clear");
        } catch (RuntimeException ignored) {
            // best effort
        }
    }
}
