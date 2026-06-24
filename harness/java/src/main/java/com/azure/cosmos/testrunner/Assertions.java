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
                                                     OpResult result, Backend backend) {
        List<Map<String, Object>> outcomes = new ArrayList<>();
        if (expectations == null) return outcomes;
        for (Map<String, Object> exp : expectations) {
            outcomes.add(evaluateOne(exp, result, backend));
        }
        return outcomes;
    }

    private static Map<String, Object> evaluateOne(Map<String, Object> exp, OpResult result, Backend backend) {
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
            default:
                return outcome(name, false, "unknown assertion type '" + type + "'");
        }
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
