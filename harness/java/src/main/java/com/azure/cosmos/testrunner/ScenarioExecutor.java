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

    @SuppressWarnings("unchecked")
    public ScenarioExecutor(Map<String, Object> scenario, Map<String, Object> config,
                            String sdkVersion, Consumer<String> logSink) {
        this.scenario = scenario;
        this.config = config;
        this.sdkVersion = sdkVersion;
        this.logSink = logSink;
        this.backend = makeBackend(config);
        String runId = String.valueOf(config.getOrDefault("run_id", UUID.randomUUID().toString().substring(0, 8)));
        ctx.put("run_id", runId);
        ctx.put("sdk", String.valueOf(config.getOrDefault("sdk", "java")));
        ctx.put("connection_mode", config.getOrDefault("connection_mode", "gateway"));
        ctx.put("config", config);
        ctx.put("steps", new LinkedHashMap<String, Object>());
    }

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
        return new SdkBackend(String.valueOf(endpoint), String.valueOf(key));
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
                runStep(step);
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
    private void runStep(Map<String, Object> step) {
        String action = String.valueOf(step.get("action"));
        Map<String, Object> params = (Map<String, Object>) resolve(step.getOrDefault("params", new LinkedHashMap<>()));
        OpResult result = StepHandlers.execute(backend, action, params, ctx);

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

        List<Map<String, Object>> expect = (List<Map<String, Object>>) step.get("expect");
        for (Map<String, Object> outc : Assertions.evaluate(expect, result, backend)) {
            outc.put("step", id != null ? id : action);
            assertionResults.add(outc);
            log("  assert " + outc.get("name") + ": " + ((Boolean) outc.get("passed") ? "PASS" : "FAIL")
                    + " " + outc.get("detail"));
        }
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
