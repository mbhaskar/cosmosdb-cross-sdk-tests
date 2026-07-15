package com.azure.cosmos.testrunner;

import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.function.Consumer;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Executes a scenario step-by-step; mirrors the Python ScenarioRunner. */
public class ScenarioExecutor {

    private static final Pattern VAR = Pattern.compile("\\$\\{([^}]+)\\}");
    private static final DateTimeFormatter ISO =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC);

    private final Map<String, Object> scenario;
    private final Map<String, Object> config;
    private final String sdkVersion;
    private final Backend backend;
    private final Map<String, Object> ctx = new LinkedHashMap<>();
    private final List<Map<String, Object>> assertionResults = new ArrayList<>();
    private final List<Map<String, Object>> diagnostics = new ArrayList<>();
    private final List<String> logs = new ArrayList<>();
    private final Consumer<String> logSink;

    // Fault-injection plumbing (mirrors the Python ScenarioRunner).
    private final Map<String, Object> faultInjection;
    private final ProxyFaultController faultController;      // L4 transport (Toxiproxy)
    private final ProtocolFaultController protocolController; // L7 protocol (mitmproxy)
    // timeline events grouped by "<when>\u0000<stepId>".
    private final Map<String, List<Map<String, Object>>> timeline;
    // latency(ms) + resource samples keyed by scope (loop id).
    private final Map<String, List<Double>> latencySamples = new LinkedHashMap<>();
    private final Map<String, List<Double>> resourceSamples = new LinkedHashMap<>();

    private static final java.util.Set<String> TRANSPORT_EVENTS = new java.util.HashSet<>(java.util.Arrays.asList(
            "net_latency", "net_timeout", "net_reset", "net_bandwidth",
            "net_slow_close", "region_down", "region_up", "reset_faults"));
    private static final java.util.Set<String> PROTOCOL_EVENTS = new java.util.HashSet<>(java.util.Arrays.asList(
            "net_throttle_window", "throttle_window_clear", "inject_fault", "fault_clear"));

    @SuppressWarnings("unchecked")
    public ScenarioExecutor(Map<String, Object> scenario, Map<String, Object> config,
                            String sdkVersion, Consumer<String> logSink) {
        this.scenario = scenario;
        this.sdkVersion = sdkVersion;
        this.logSink = logSink;

        Object fi = scenario.get("fault_injection");
        this.faultInjection = fi instanceof Map ? (Map<String, Object>) fi : null;

        // When a scenario opts into transport/protocol fault injection and a proxy
        // endpoint is configured, point the SDK client at the proxy chain instead
        // of the backend directly, so injected toxics/faults are on the wire.
        // Mirrors Python executor.py:104-119.
        Map<String, Object> routed = config;
        boolean isMock = "mock".equals(String.valueOf(config.getOrDefault("backend", "mock")));
        if (this.faultInjection != null && !isMock) {
            boolean protocol = truthy(this.faultInjection.get("protocol"));
            Object proxyEp = protocol
                    ? firstNonNull(config.get("mitm_endpoint"), config.get("proxy_endpoint"))
                    : config.get("proxy_endpoint");
            if (proxyEp != null) {
                routed = new LinkedHashMap<>(config);
                routed.put("endpoint", proxyEp);
                // Pin the client to the proxy endpoint: without this, gateway
                // endpoint discovery adopts the emulator's self-advertised address
                // (localhost:8081) and bypasses the proxy. Multi-region failover
                // needs discovery ON (and a real multi-region account).
                if (!truthy(this.faultInjection.get("multi_region"))) {
                    routed.put("enable_endpoint_discovery", Boolean.FALSE);
                }
            }
        }
        this.config = routed;
        this.backend = makeBackend(routed);

        String runId = String.valueOf(routed.getOrDefault("run_id", UUID.randomUUID().toString().substring(0, 8)));
        ctx.put("run_id", runId);
        ctx.put("sdk", String.valueOf(routed.getOrDefault("sdk", "java")));
        ctx.put("connection_mode", routed.getOrDefault("connection_mode", "gateway"));
        ctx.put("config", routed);
        ctx.put("steps", new LinkedHashMap<String, Object>());

        this.timeline = indexTimeline((List<Map<String, Object>>) scenario.get("timeline"));
        this.faultController = makeFaultController();
        this.protocolController = makeProtocolController();
    }

    @SuppressWarnings("unchecked")
    private static Backend makeBackend(Map<String, Object> config) {
        String backend = String.valueOf(config.getOrDefault("backend", "mock"));
        if ("mock".equals(backend)) {
            return new MockBackend(loadMockProfile(config));
        }
        Object endpoint = config.get("endpoint");
        Object key = config.get("key");
        if (endpoint == null || key == null) {
            throw new IllegalArgumentException("backend '" + backend + "' requires endpoint and key");
        }
        // Emulator / proxied endpoints present self-signed certs; trust is handled
        // via the JVM trust store (see scripts/build-java-truststore.sh), so we
        // only record intent here. Explicit tls_verify overrides.
        boolean verifyTls = config.containsKey("tls_verify")
                ? truthy(config.get("tls_verify"))
                : false;
        // enable_endpoint_discovery is injected above for single-region fault runs.
        Boolean discovery = config.containsKey("enable_endpoint_discovery")
                ? Boolean.valueOf(truthy(config.get("enable_endpoint_discovery")))
                : null;
        return new SdkBackend(String.valueOf(endpoint), String.valueOf(key), verifyTls, discovery);
    }

    private ProxyFaultController makeFaultController() {
        if (faultInjection == null || "mock".equals(String.valueOf(config.getOrDefault("backend", "mock")))) {
            return null;
        }
        return new ProxyFaultController(
                str(config.get("toxiproxy_url")),
                strOr(faultInjection.get("proxy"), "cosmos"),
                strOr(faultInjection.get("secondary_proxy"), "cosmos-secondary"));
    }

    private ProtocolFaultController makeProtocolController() {
        if (faultInjection == null || "mock".equals(String.valueOf(config.getOrDefault("backend", "mock")))) {
            return null;
        }
        Object ep = firstNonNull(config.get("mitm_endpoint"), config.get("proxy_endpoint"), config.get("endpoint"));
        return new ProtocolFaultController(ep == null ? null : String.valueOf(ep));
    }

    private static Object firstNonNull(Object... vals) {
        for (Object v : vals) {
            if (v != null && !String.valueOf(v).isEmpty()) return v;
        }
        return null;
    }

    private static boolean truthy(Object o) {
        if (o == null) return false;
        if (o instanceof Boolean) return (Boolean) o;
        String s = String.valueOf(o).trim().toLowerCase();
        return s.equals("true") || s.equals("1") || s.equals("yes");
    }

    private static String str(Object o) {
        return o == null ? null : String.valueOf(o);
    }

    private static String strOr(Object o, String dflt) {
        return o == null ? dflt : String.valueOf(o);
    }

    /**
     * Returns the mock behavior profile. Preference: (1) inline
     * {@code config.mock_profile} injected by the orchestrator (single source of
     * truth, read once); (2) {@code specs/mock-profile.json} located by walking
     * up from the working directory (standalone CLI use).
     */
    @SuppressWarnings("unchecked")
    private static Map<String, Object> loadMockProfile(Map<String, Object> config) {
        Object inline = config.get("mock_profile");
        if (inline instanceof Map) {
            return (Map<String, Object>) inline;
        }
        com.fasterxml.jackson.databind.ObjectMapper mapper = new com.fasterxml.jackson.databind.ObjectMapper();
        java.nio.file.Path dir = java.nio.file.Paths.get("").toAbsolutePath();
        for (int i = 0; i < 6 && dir != null; i++) {
            java.nio.file.Path p = dir.resolve("specs").resolve("mock-profile.json");
            if (java.nio.file.Files.exists(p)) {
                try {
                    return mapper.readValue(p.toFile(), Map.class);
                } catch (Exception e) {
                    throw new RuntimeException("failed to read mock profile " + p + ": " + e.getMessage(), e);
                }
            }
            dir = dir.getParent();
        }
        throw new RuntimeException("mock profile not found: provide config.mock_profile or specs/mock-profile.json");
    }

    private static String nowIso() {
        return ISO.format(Instant.now());
    }

    /**
     * The actual azure-cosmos version loaded on the classpath (not the label the
     * caller passed). Read from the SDK's bundled properties (the same source the
     * SDK uses for its User-Agent), falling back to the package manifest.
     */
    private static String resolveSdkVersion() {
        try {
            Map<String, String> props =
                    com.azure.core.util.CoreUtils.getProperties("azure-cosmos.properties");
            if (props != null && props.get("version") != null) {
                return props.get("version");
            }
        } catch (Throwable ignored) {
            // fall through
        }
        try {
            String v = com.azure.cosmos.CosmosClient.class.getPackage().getImplementationVersion();
            if (v != null && !v.isEmpty()) {
                return v;
            }
        } catch (Throwable ignored) {
            // fall through
        }
        return null;
    }

    private void log(String msg) {
        String line = "[" + nowIso() + "] " + msg;
        logs.add(line);
        logSink.accept(line);
    }

    @SuppressWarnings("unchecked")
    public Map<String, Object> run() {
        String startedAt = nowIso();
        long t0 = System.currentTimeMillis();
        String status = "pass";
        String error = null;

        Map<String, Object> fixture = (Map<String, Object>) scenario.get("fixture");
        try {
            setupFixture(fixture);
            List<Map<String, Object>> steps = (List<Map<String, Object>>) scenario.getOrDefault("steps", new ArrayList<>());
            for (Map<String, Object> step : steps) {
                Object sid = step.get("id");
                fireEvents("before", sid);
                if ("loop".equals(String.valueOf(step.get("action")))) {
                    runLoop(step);
                } else {
                    runStep(step, null, true);
                }
                fireEvents("after", sid);
            }
            for (Map<String, Object> a : assertionResults) {
                if (!(Boolean) a.get("passed")) {
                    status = "fail";
                    break;
                }
            }
        } catch (Exception e) {
            status = "error";
            error = e.getClass().getSimpleName() + ": " + e.getMessage();
            log("ERROR " + error);
        } finally {
            if (faultController != null) {
                try {
                    faultController.reset();
                } catch (Exception e) {
                    log("fault reset warning: " + e.getMessage());
                }
            }
            if (protocolController != null) {
                try {
                    protocolController.reset();
                } catch (Exception e) {
                    log("protocol reset warning: " + e.getMessage());
                }
            }
            teardownFixture(fixture);
        }

        long durationMs = System.currentTimeMillis() - t0;
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("scenario_id", String.valueOf(scenario.get("id")));
        result.put("title", scenario.get("title"));
        result.put("sdk", "java");
        result.put("sdk_version", sdkVersion);
        result.put("sdk_source", String.valueOf(config.getOrDefault("sdk_source", "published")));
        result.put("resolved_sdk_version", resolveSdkVersion());
        result.put("backend", config.getOrDefault("backend", "mock"));
        result.put("status", status);
        result.put("duration_ms", durationMs);
        result.put("started_at", startedAt);
        result.put("completed_at", nowIso());
        result.put("metrics", backend.metrics().asMap());
        result.put("assertions", assertionResults);
        result.put("diagnostics", diagnostics);
        result.put("error", error);
        result.put("logs", logs);
        return result;
    }

    @SuppressWarnings("unchecked")
    private OpResult runStep(Map<String, Object> step, String scope, boolean evaluate) {
        String action = String.valueOf(step.get("action"));
        Map<String, Object> params = (Map<String, Object>) resolve(step.getOrDefault("params", new LinkedHashMap<>()));
        long tStep = System.currentTimeMillis();
        OpResult result = StepHandlers.execute(backend, action, params, ctx);
        double elapsedMs = System.currentTimeMillis() - tStep;
        if (scope != null) {
            latencySamples.computeIfAbsent(scope, k -> new ArrayList<>()).add(elapsedMs);
        }

        Object id = step.get("id");
        if (id != null) {
            ((Map<String, Object>) ctx.get("steps")).put(String.valueOf(id), result.asContext());
        }
        String outcome = result.ok ? "ok" : ("FAILED(" + result.statusCode + " " + result.errorCode + ")");
        log("step '" + (id != null ? id : action) + "' action=" + action + " -> " + outcome);

        if (result.diagnostics != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("step", id != null ? id : action);
            d.put("action", action);
            d.put("status_code", result.statusCode);
            d.put("text", result.diagnostics);
            diagnostics.add(d);
        }

        if (evaluate) {
            evaluate(step, result, scope);
        }
        return result;
    }

    @SuppressWarnings("unchecked")
    private void evaluate(Map<String, Object> step, OpResult result, String scope) {
        Map<String, Object> context = new LinkedHashMap<>();
        context.put("latency", latencySamples);
        context.put("resource", resourceSamples);
        context.put("scope", scope);
        Object id = step.get("id");
        List<Map<String, Object>> expect = (List<Map<String, Object>>) step.get("expect");
        for (Map<String, Object> outc : Assertions.evaluate(expect, result, backend, context)) {
            outc.put("step", id != null ? id : step.get("action"));
            assertionResults.add(outc);
            log("  assert " + outc.get("name") + ": " + ((Boolean) outc.get("passed") ? "PASS" : "FAIL")
                    + " " + outc.get("detail"));
        }
    }

    /**
     * Repeat nested steps {@code count} times or for a {@code duration}. Records
     * per-iteration latency + resource samples under the loop's scope, then
     * evaluates the loop's own expect (e.g. latency_percentile / resource_stable).
     * Mirrors Python executor._run_loop.
     */
    @SuppressWarnings("unchecked")
    private void runLoop(Map<String, Object> step) {
        String scope = step.get("id") != null ? String.valueOf(step.get("id")) : "loop";
        List<Map<String, Object>> inner = (List<Map<String, Object>>) step.getOrDefault("steps", new ArrayList<>());
        Integer count = step.get("count") != null ? asInt(step.get("count")) : null;
        Double duration = parseDuration(step.get("duration"));
        OpResult last = OpResult.ok(200);
        long started = System.currentTimeMillis();
        int i = 0;
        while (true) {
            if (count != null && i >= count) break;
            if (duration != null && (System.currentTimeMillis() - started) / 1000.0 >= duration) break;
            if (count == null && duration == null) break;
            for (Map<String, Object> sub : inner) {
                last = runStep(sub, scope, true);
            }
            resourceSamples.computeIfAbsent(scope, k -> new ArrayList<>())
                    .add((double) asInt(backend.metrics().asMap().getOrDefault("connections_opened", 0)));
            i++;
        }
        log("loop '" + scope + "' ran " + i + " iteration(s)");
        evaluate(step, last, scope);
    }

    // -- timeline / fault events ------------------------------------------- //

    private static Map<String, List<Map<String, Object>>> indexTimeline(List<Map<String, Object>> timeline) {
        Map<String, List<Map<String, Object>>> idx = new LinkedHashMap<>();
        if (timeline == null) return idx;
        for (Map<String, Object> ev : timeline) {
            String when = ev.containsKey("before") ? "before" : "after";
            Object anchor = ev.get(when);
            idx.computeIfAbsent(when + "\u0000" + anchor, k -> new ArrayList<>()).add(ev);
        }
        return idx;
    }

    @SuppressWarnings("unchecked")
    private void fireEvents(String when, Object stepId) {
        if (stepId == null) return;
        List<Map<String, Object>> events = timeline.get(when + "\u0000" + stepId);
        if (events == null) return;
        for (Map<String, Object> ev : events) {
            String event = String.valueOf(ev.get("event"));
            Map<String, Object> args = ev.get("args") instanceof Map
                    ? (Map<String, Object>) ev.get("args") : new LinkedHashMap<>();
            if (PROTOCOL_EVENTS.contains(event)) {
                if (protocolController == null) {
                    log("  timeline[" + when + " " + stepId + "]: '" + event
                            + "' skipped (no protocol controller; needs emulator/live + mitmproxy)");
                    continue;
                }
                protocolController.apply(event, args);
                log("  timeline[" + when + " " + stepId + "]: " + event + " " + args);
                continue;
            }
            if (TRANSPORT_EVENTS.contains(event)) {
                if (faultController == null) {
                    log("  timeline[" + when + " " + stepId + "]: '" + event
                            + "' skipped (no fault controller; needs emulator/live + Toxiproxy)");
                    continue;
                }
                faultController.apply(event, args);
                log("  timeline[" + when + " " + stepId + "]: " + event + " " + args);
                continue;
            }
            // Mock control-plane events are handled by the Python harness only;
            // Java's MockBackend has no control_event, and fault scenarios never
            // run on mock, so just note it.
            log("  timeline: backend ignores control event '" + event + "'");
        }
    }

    private static Double parseDuration(Object spec) {
        if (spec == null) return null;
        if (spec instanceof Number) return ((Number) spec).doubleValue();
        String s = String.valueOf(spec).trim().toLowerCase();
        try {
            if (s.endsWith("ms")) return Double.parseDouble(s.substring(0, s.length() - 2)) / 1000.0;
            if (s.endsWith("s")) return Double.parseDouble(s.substring(0, s.length() - 1));
            if (s.endsWith("m")) return Double.parseDouble(s.substring(0, s.length() - 1)) * 60.0;
            return Double.parseDouble(s);
        } catch (NumberFormatException e) {
            return null;
        }
    }

    private static int asInt(Object o) {
        if (o instanceof Number) return ((Number) o).intValue();
        return Integer.parseInt(String.valueOf(o));
    }

    @SuppressWarnings("unchecked")
    private void setupFixture(Map<String, Object> fixture) {
        if (fixture == null) return;
        String dbId = String.valueOf(fixture.getOrDefault("database", "auto"));
        if ("auto".equals(dbId)) {
            // Namespace the auto db per SDK so parallel Python/Java runs of the
            // same scenario don't share a database (and collide on hardcoded item
            // ids). Falls back to "java" for standalone CLI use.
            dbId = "mvp-" + scenario.get("id") + "-" + ctx.get("sdk") + "-" + ctx.get("run_id");
        }
        ctx.put("db", dbId);
        backend.createClient(String.valueOf(ctx.get("connection_mode")));
        backend.createDatabase(dbId, true);
        Map<String, Object> cont = (Map<String, Object>) fixture.get("container");
        if (cont != null) {
            String containerId = String.valueOf(cont.get("id"));
            ctx.put("container", containerId);
            backend.createContainer(dbId, containerId,
                    String.valueOf(cont.getOrDefault("partition_key", "/pk")), true);
        }
        log("fixture ready: db=" + dbId + " container=" + ctx.get("container"));
    }

    private void teardownFixture(Map<String, Object> fixture) {
        if (fixture == null || ctx.get("db") == null) return;
        try {
            backend.deleteDatabase(String.valueOf(ctx.get("db")));
            log("fixture cleaned up: db=" + ctx.get("db"));
        } catch (Exception e) {
            log("fixture cleanup warning: " + e.getMessage());
        }
    }

    // -- substitution ------------------------------------------------------ //

    @SuppressWarnings("unchecked")
    private Object resolve(Object value) {
        if (value instanceof String) {
            String s = (String) value;
            Matcher whole = VAR.matcher(s);
            if (whole.matches()) {
                return lookup(whole.group(1));
            }
            Matcher m = VAR.matcher(s);
            StringBuffer sb = new StringBuffer();
            while (m.find()) {
                m.appendReplacement(sb, Matcher.quoteReplacement(String.valueOf(lookup(m.group(1)))));
            }
            m.appendTail(sb);
            return sb.toString();
        }
        if (value instanceof List) {
            List<Object> out = new ArrayList<>();
            for (Object v : (List<Object>) value) {
                out.add(resolve(v));
            }
            return out;
        }
        if (value instanceof Map) {
            Map<String, Object> out = new LinkedHashMap<>();
            for (Map.Entry<String, Object> e : ((Map<String, Object>) value).entrySet()) {
                out.put(e.getKey(), resolve(e.getValue()));
            }
            return out;
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    private Object lookup(String expr) {
        expr = expr.trim();
        if ("uuid".equals(expr)) return UUID.randomUUID().toString();
        if ("now".equals(expr)) return nowIso();
        Object cur = ctx;
        for (String part : expr.split("\\.")) {
            if (cur instanceof Map) {
                cur = ((Map<String, Object>) cur).get(part);
            } else {
                cur = null;
            }
            if (cur == null) break;
        }
        return cur != null ? cur : "";
    }
}
