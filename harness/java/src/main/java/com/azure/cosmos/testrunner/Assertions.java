package com.azure.cosmos.testrunner;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Evaluates step assertions; mirrors the Python assertions module. */
public final class Assertions {

    private Assertions() {
    }

    @SuppressWarnings("unchecked")
    public static List<Map<String, Object>> evaluate(List<Map<String, Object>> expectations,
                                                     OpResult result, Backend backend,
                                                     Map<String, Object> context) {
        List<Map<String, Object>> outcomes = new ArrayList<>();
        if (expectations == null) return outcomes;
        for (Map<String, Object> exp : expectations) {
            outcomes.add(evaluateOne(exp, result, backend, context));
        }
        return outcomes;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> evaluateOne(Map<String, Object> exp, OpResult result,
                                                   Backend backend, Map<String, Object> context) {
        String type = String.valueOf(exp.get("type"));
        Object nameObj = exp.getOrDefault("name", type);
        String name = String.valueOf(nameObj);

        switch (type) {
            case "ok":
            case "no_error":
                return outcome(name, result.ok, result.ok ? "" : "status=" + result.statusCode);
            case "error":
                return outcome(name, !result.ok, result.ok ? "expected failure but op succeeded" : "");
            case "status_code":
                return outcome(name, result.statusCode == asInt(exp.get("value")), "actual=" + result.statusCode);
            case "error_status":
                return outcome(name, !result.ok && result.statusCode == asInt(exp.get("value")),
                        "ok=" + result.ok + " status=" + result.statusCode);
            case "item_count": {
                int actual = result.items == null ? 0 : result.items.size();
                return outcome(name, actual == asInt(exp.get("value")), "actual=" + actual);
            }
            case "count_gte": {
                int actual = result.items == null ? 0 : result.items.size();
                return outcome(name, actual >= asInt(exp.get("value")), "actual=" + actual);
            }
            case "field_equals": {
                Object actual = navigate(result, String.valueOf(exp.get("path")));
                return outcome(name, String.valueOf(actual).equals(String.valueOf(exp.get("value"))),
                        "actual=" + actual);
            }
            case "metric_equals": {
                Object actual = metric(backend, String.valueOf(exp.get("metric")));
                return outcome(name, String.valueOf(actual).equals(String.valueOf(exp.get("value"))),
                        "actual=" + actual);
            }
            case "metric_zero": {
                Object actual = metric(backend, String.valueOf(exp.get("metric")));
                return outcome(name, asInt(actual) == 0, "actual=" + actual);
            }

            // --- transport / protocol fault assertions (emulator/live tier) ---- //
            case "eventually_ok":
                // Op succeeded (after any injected fault was cleared / retried).
                return outcome(name, result.ok, result.ok ? "" : "status=" + result.statusCode);
            case "retry_count_gte": {
                int actual = asInt(metric(backend, "retries"));
                return outcome(name, actual >= asInt(exp.get("value")), "retries=" + actual);
            }
            case "latency_percentile": {
                String scope = scope(exp, context);
                List<Double> samples = samples(context, "latency", scope);
                if (samples.isEmpty()) {
                    return outcome(name, false, "no latency samples for scope '" + scope + "'");
                }
                int pct = exp.get("percentile") != null ? asInt(exp.get("percentile")) : 99;
                double p = percentile(samples, pct);
                return outcome(name, p <= asDouble(exp.get("max_ms")),
                        String.format("p%d=%.1fms n=%d", pct, p, samples.size()));
            }
            case "resource_stable": {
                String scope = scope(exp, context);
                List<Double> samples = samples(context, "resource", scope);
                if (samples.size() < 2) {
                    return outcome(name, false, "insufficient resource samples for scope '" + scope + "'");
                }
                double lo = samples.stream().mapToDouble(Double::doubleValue).min().orElse(0);
                double hi = samples.stream().mapToDouble(Double::doubleValue).max().orElse(0);
                double drift = hi == 0 ? 0.0 : (hi - lo) / hi * 100.0;
                double tol = exp.get("tolerance_pct") != null ? asDouble(exp.get("tolerance_pct")) : 10.0;
                return outcome(name, drift <= tol,
                        String.format("drift=%.1f%% (min=%s max=%s n=%d)", drift, lo, hi, samples.size()));
            }
            case "failover_region": {
                // Best-effort: the contacted region is surfaced in SDK diagnostics on
                // emulator/live. Offline/mock has no diagnostics, so this fails clearly.
                String want = String.valueOf(exp.getOrDefault("equals", ""));
                String blob = result.diagnostics != null ? result.diagnostics : "";
                boolean present = !want.isEmpty() && blob.contains(want);
                return outcome(name, present, "want_region='" + want + "' present=" + present);
            }
            case "pages_cover_set": {
                List<String> ids = new ArrayList<>();
                if (result.items != null) {
                    for (Object r : result.items) {
                        if (r instanceof Map) {
                            ids.add(String.valueOf(((Map<String, Object>) r).get("id")));
                        }
                    }
                }
                java.util.Set<String> unique = new java.util.HashSet<>(ids);
                boolean noDupes = ids.size() == unique.size();
                Object expected = exp.get("expected_count");
                boolean covers = expected == null || unique.size() == asInt(expected);
                return outcome(name, noDupes && covers,
                        "n=" + ids.size() + " unique=" + unique.size() + " expected=" + expected
                                + " no_dupes=" + noDupes);
            }

            default:
                return outcome(name, false, "unknown assertion type '" + type + "'");
        }
    }

    @SuppressWarnings("unchecked")
    private static String scope(Map<String, Object> exp, Map<String, Object> context) {
        Object s = exp.get("scope");
        if (s != null) return String.valueOf(s);
        return context != null && context.get("scope") != null ? String.valueOf(context.get("scope")) : null;
    }

    @SuppressWarnings("unchecked")
    private static List<Double> samples(Map<String, Object> context, String key, String scope) {
        if (context == null) return new ArrayList<>();
        Object byScope = context.get(key);
        if (!(byScope instanceof Map)) return new ArrayList<>();
        Object list = ((Map<String, Object>) byScope).get(scope);
        return list instanceof List ? (List<Double>) list : new ArrayList<>();
    }

    private static double percentile(List<Double> samples, int pct) {
        List<Double> sorted = new ArrayList<>(samples);
        java.util.Collections.sort(sorted);
        if (sorted.size() == 1) return sorted.get(0);
        double rank = pct / 100.0 * (sorted.size() - 1);
        int lo = (int) Math.floor(rank);
        int hi = (int) Math.ceil(rank);
        double frac = rank - lo;
        return sorted.get(lo) + (sorted.get(hi) - sorted.get(lo)) * frac;
    }

    private static double asDouble(Object v) {
        if (v instanceof Number) return ((Number) v).doubleValue();
        return Double.parseDouble(String.valueOf(v));
    }

    @SuppressWarnings("unchecked")
    private static Object navigate(OpResult result, String path) {
        Object cur = result.asContext();
        for (String part : path.split("\\.")) {
            if (cur == null) return null;
            if (cur instanceof List) {
                cur = ((List<Object>) cur).get(Integer.parseInt(part));
            } else if (cur instanceof Map) {
                cur = ((Map<String, Object>) cur).get(part);
            } else {
                return null;
            }
        }
        return cur;
    }

    @SuppressWarnings("unchecked")
    private static Object metric(Backend backend, String name) {
        Object cur = backend.metrics().asMap();
        for (String part : name.split("\\.")) {
            if (cur instanceof Map) {
                cur = ((Map<String, Object>) cur).get(part);
            } else {
                return null;
            }
        }
        return cur;
    }

    private static int asInt(Object v) {
        if (v instanceof Number) return ((Number) v).intValue();
        return Integer.parseInt(String.valueOf(v));
    }

    private static Map<String, Object> outcome(String name, boolean passed, String detail) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("passed", passed);
        m.put("detail", detail);
        return m;
    }
}
