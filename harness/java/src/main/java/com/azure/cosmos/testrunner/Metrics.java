package com.azure.cosmos.testrunner;

import java.util.LinkedHashMap;
import java.util.Map;

/** Per-run metrics, kept structurally identical to the Python harness output. */
public class Metrics {
    public double ruConsumed = 0.0;
    public int retries = 0;
    public int connectionsOpened = 0;
    public String connectionMode = null;
    public final Map<String, Integer> metadataCalls = new LinkedHashMap<>();

    public Metrics() {
        metadataCalls.put("get_database_account", 0);
        metadataCalls.put("read_collection", 0);
        metadataCalls.put("read_pk_ranges", 0);
    }

    public void incr(String name) {
        metadataCalls.merge(name, 1, Integer::sum);
    }

    public double charge(double amount) {
        ruConsumed += amount;
        return amount;
    }

    public Map<String, Object> asMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ru_consumed", Math.round(ruConsumed * 1000.0) / 1000.0);
        m.put("retries", retries);
        m.put("connections_opened", connectionsOpened);
        m.put("connection_mode", connectionMode);
        m.put("metadata_calls", metadataCalls);
        return m;
    }
}
