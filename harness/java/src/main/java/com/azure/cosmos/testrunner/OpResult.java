package com.azure.cosmos.testrunner;

import java.util.LinkedHashMap;
import java.util.Map;

/** Uniform result of a single backend operation (mirrors the Python OpResult). */
public class OpResult {
    public boolean ok;
    public int statusCode;
    public String errorCode;
    public String error;
    public Map<String, Object> item;
    public java.util.List<Object> items;
    public double ru;
    public String diagnostics;

    public static OpResult ok(int status, Map<String, Object> item) {
        OpResult r = new OpResult();
        r.ok = true;
        r.statusCode = status;
        r.item = item;
        return r;
    }

    public static OpResult ok(int status) {
        return ok(status, null);
    }

    public static OpResult fail(int status, String code, String message) {
        OpResult r = new OpResult();
        r.ok = false;
        r.statusCode = status;
        r.errorCode = code;
        r.error = message;
        return r;
    }

    public Map<String, Object> asContext() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", ok);
        m.put("status_code", statusCode);
        m.put("item", item == null ? new LinkedHashMap<>() : item);
        m.put("items", items == null ? new java.util.ArrayList<>() : items);
        m.put("id", item == null ? null : item.get("id"));
        return m;
    }
}
