package com.azure.cosmos.testrunner;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * In-memory Cosmos DB simulation driven entirely by a shared profile.
 *
 * All operation semantics (status codes, RU charges, 409/404 branching, lazy
 * metadata accounting) come from {@code specs/mock-profile.json}, the same file
 * the Python runner reads, so mock behavior cannot drift between SDKs. This
 * class only provides the in-memory state store plus a tiny interpreter over
 * the profile's step machine.
 */
public class MockBackend implements Backend {

    private static final class Container {
        String pkField;
        String pkPath;
        boolean readCollectionDone = false;
        final Map<String, Map<String, Object>> items = new LinkedHashMap<>();
    }

    /** Mutable per-operation context the interpreter threads through steps. */
    private static final class OpCtx {
        String dbId;
        String containerId;
        String itemId;          // explicit id (read/replace/delete)
        Object partitionKey;    // explicit pk (read/replace/delete/query)
        Map<String, Object> item;
        boolean ifNotExists;
        String pkPath;
        String query;
        List<Map<String, Object>> parameters;
        boolean crossPartition;
        Container container;
        boolean existed;
        Map<String, Object> resultItem;
        List<Object> rows;
        String connectionMode;
    }

    private final Map<String, Map<String, Container>> databases = new LinkedHashMap<>();
    private final Metrics metrics = new Metrics();

    private final Map<String, Object> ruCharges;
    private final Map<String, Object> statusCodes;
    private final Map<String, List<Map<String, Object>>> operations;

    @SuppressWarnings("unchecked")
    public MockBackend(Map<String, Object> profile) {
        this.ruCharges = (Map<String, Object>) profile.get("ru_charges");
        this.statusCodes = (Map<String, Object>) profile.get("status_codes");
        this.operations = (Map<String, List<Map<String, Object>>>) profile.get("operations");
    }

    @Override
    public Metrics metrics() {
        return metrics;
    }

    // -- public Backend API: each builds a context and runs the profile --- //

    @Override
    public OpResult createClient(String connectionMode) {
        OpCtx ctx = new OpCtx();
        ctx.connectionMode = connectionMode;
        return run("create_client", ctx);
    }

    @Override
    public OpResult createDatabase(String dbId, boolean createIfNotExists) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.ifNotExists = createIfNotExists;
        return run("create_database", ctx);
    }

    @Override
    public OpResult createContainer(String dbId, String containerId, String partitionKey, boolean createIfNotExists) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.pkPath = partitionKey;
        ctx.ifNotExists = createIfNotExists;
        return run("create_container", ctx);
    }

    @Override
    public OpResult createItem(String dbId, String containerId, Map<String, Object> item) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.item = new LinkedHashMap<>(item);
        return run("create_item", ctx);
    }

    @Override
    public OpResult readItem(String dbId, String containerId, String itemId, Object partitionKey) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.itemId = itemId;
        ctx.partitionKey = partitionKey;
        return run("read_item", ctx);
    }

    @Override
    public OpResult replaceItem(String dbId, String containerId, String itemId, Object partitionKey, Map<String, Object> item) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.itemId = itemId;
        ctx.partitionKey = partitionKey;
        ctx.item = new LinkedHashMap<>(item);
        return run("replace_item", ctx);
    }

    @Override
    public OpResult upsertItem(String dbId, String containerId, Map<String, Object> item) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.item = new LinkedHashMap<>(item);
        return run("upsert_item", ctx);
    }

    @Override
    public OpResult deleteItem(String dbId, String containerId, String itemId, Object partitionKey) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.itemId = itemId;
        ctx.partitionKey = partitionKey;
        return run("delete_item", ctx);
    }

    @Override
    public OpResult queryItems(String dbId, String containerId, String query,
                               List<Map<String, Object>> parameters, Object partitionKey, boolean crossPartition) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        ctx.containerId = containerId;
        ctx.query = query;
        ctx.parameters = parameters;
        ctx.partitionKey = partitionKey;
        ctx.crossPartition = crossPartition;
        return run("query_items", ctx);
    }

    @Override
    public OpResult deleteDatabase(String dbId) {
        OpCtx ctx = new OpCtx();
        ctx.dbId = dbId;
        return run("delete_database", ctx);
    }

    // -- interpreter ------------------------------------------------------ //

    @SuppressWarnings("unchecked")
    private OpResult run(String op, OpCtx ctx) {
        ctx.container = container(ctx.dbId, ctx.containerId);
        for (Map<String, Object> step : operations.get(op)) {
            if (step.containsKey("guard")) {
                List<String> preds = (List<String>) step.get("guard");
                boolean all = true;
                for (String p : preds) {
                    if (!pred(p, ctx)) { all = false; break; }
                }
                if (all) {
                    return build((Map<String, Object>) step.get("return"), ctx);
                }
            } else if (step.containsKey("effect")) {
                effect(step, ctx);
                ctx.container = container(ctx.dbId, ctx.containerId);
            } else if (step.containsKey("return")) {
                return build((Map<String, Object>) step.get("return"), ctx);
            }
        }
        throw new IllegalStateException("mock profile op '" + op + "' produced no result");
    }

    private Container container(String dbId, String containerId) {
        Map<String, Container> db = databases.get(dbId);
        return db == null ? null : db.get(containerId);
    }

    private static String pkField(String partitionKeyPath) {
        return partitionKeyPath.replaceFirst("^/", "").split("/")[0];
    }

    private static String pkKey(Object pk) {
        return pk == null ? "" : String.valueOf(pk);
    }

    private static String key(Object id, Object pk) {
        return String.valueOf(id) + "\u0000" + pkKey(pk);
    }

    private static Object effId(OpCtx ctx) {
        if (ctx.itemId != null) return ctx.itemId;
        return ctx.item == null ? null : ctx.item.get("id");
    }

    private static Object effPk(OpCtx ctx) {
        if (ctx.partitionKey != null) return ctx.partitionKey;
        if (ctx.container == null || ctx.item == null) return null;
        return ctx.item.get(ctx.container.pkField);
    }

    private static String curKey(OpCtx ctx) {
        return key(effId(ctx), effPk(ctx));
    }

    // -- predicates -- //
    private boolean pred(String name, OpCtx ctx) {
        Container c = ctx.container;
        switch (name) {
            case "db_exists": return databases.containsKey(ctx.dbId);
            case "db_missing": return !databases.containsKey(ctx.dbId);
            case "container_exists": return c != null;
            case "container_missing": return c == null;
            case "item_exists": return c != null && c.items.containsKey(curKey(ctx));
            case "item_missing": return c == null || !c.items.containsKey(curKey(ctx));
            case "existed": return ctx.existed;
            case "if_not_exists": return ctx.ifNotExists;
            default: throw new IllegalArgumentException("unknown predicate '" + name + "'");
        }
    }

    // -- effects -- //
    private void effect(Map<String, Object> step, OpCtx ctx) {
        String name = (String) step.get("effect");
        Container c = ctx.container;
        switch (name) {
            case "set_connection":
                metrics.connectionMode = ctx.connectionMode;
                metrics.connectionsOpened = 1;
                break;
            case "incr_metadata":
                metrics.incr((String) step.get("counter"));
                break;
            case "touch_collection":
                if (c != null && !c.readCollectionDone) {
                    c.readCollectionDone = true;
                    metrics.incr("read_collection");
                }
                break;
            case "touch_pk_ranges_if_cross":
                if (ctx.crossPartition) {
                    metrics.incr("read_pk_ranges");
                }
                break;
            case "assign_id":
                ctx.item.putIfAbsent("id", UUID.randomUUID().toString());
                break;
            case "put_database":
                databases.put(ctx.dbId, new LinkedHashMap<>());
                break;
            case "remove_database":
                databases.remove(ctx.dbId);
                break;
            case "put_container": {
                Container nc = new Container();
                nc.pkField = pkField(ctx.pkPath);
                nc.pkPath = ctx.pkPath;
                databases.get(ctx.dbId).put(ctx.containerId, nc);
                break;
            }
            case "put_item": {
                String k = curKey(ctx);
                ctx.item.put("id", effId(ctx));
                ctx.existed = c.items.containsKey(k);
                c.items.put(k, ctx.item);
                ctx.resultItem = ctx.item;
                break;
            }
            case "remove_item":
                c.items.remove(curKey(ctx));
                break;
            case "query_filter": {
                List<Object> rows = new ArrayList<>();
                for (Map<String, Object> it : c.items.values()) {
                    if (ctx.partitionKey != null && !pkKey(it.get(c.pkField)).equals(pkKey(ctx.partitionKey))) {
                        continue;
                    }
                    rows.add(it);
                }
                ctx.rows = applyQuery(ctx.query, rows, ctx.parameters == null ? new ArrayList<>() : ctx.parameters);
                break;
            }
            default:
                throw new IllegalArgumentException("unknown effect '" + name + "'");
        }
    }

    // -- return / charge -- //
    private double charge(String name, OpCtx ctx) {
        if (name == null) return 0.0;
        double ru;
        if (name.equals("query")) {
            ru = num(ruCharges.get("query_base"))
                    + num(ruCharges.get("query_per_row")) * (ctx.rows == null ? 0 : ctx.rows.size());
        } else {
            ru = num(ruCharges.get(name));
        }
        return metrics.charge(ru);
    }

    @SuppressWarnings("unchecked")
    private OpResult build(Map<String, Object> ret, OpCtx ctx) {
        int status = ((Number) statusCodes.get(ret.get("status"))).intValue();
        double ru = charge((String) ret.get("charge"), ctx);
        boolean ok = Boolean.TRUE.equals(ret.get("ok"));
        if (!ok) {
            OpResult r = OpResult.fail(status, (String) ret.get("error_code"),
                    template((String) ret.get("message"), ctx));
            r.ru = ru;
            return r;
        }
        Object body = ret.get("body");
        OpResult r;
        if ("item".equals(body)) {
            r = OpResult.ok(status, resolveItem(ctx));
        } else if ("items".equals(body)) {
            r = OpResult.ok(status);
            r.items = ctx.rows;
        } else if (body instanceof Map) {
            Map<String, Object> lit = new LinkedHashMap<>();
            for (Map.Entry<String, Object> e : ((Map<String, Object>) body).entrySet()) {
                Object v = e.getValue();
                lit.put(e.getKey(), v instanceof String ? template((String) v, ctx) : v);
            }
            r = OpResult.ok(status, lit);
        } else {
            r = OpResult.ok(status);
        }
        r.ru = ru;
        return r;
    }

    private Map<String, Object> resolveItem(OpCtx ctx) {
        if (ctx.resultItem != null) return ctx.resultItem;
        if (ctx.container != null) {
            Map<String, Object> found = ctx.container.items.get(curKey(ctx));
            if (found != null) return found;
        }
        return ctx.item;
    }

    private static String template(String v, OpCtx ctx) {
        if (v == null) return null;
        return v.replace("{db}", String.valueOf(ctx.dbId))
                .replace("{container}", String.valueOf(ctx.containerId))
                .replace("{pk_path}", String.valueOf(ctx.pkPath));
    }

    private static double num(Object o) {
        return ((Number) o).doubleValue();
    }

    @SuppressWarnings("unchecked")
    private static List<Object> applyQuery(String query, List<Object> rows, List<Map<String, Object>> parameters) {
        String q = query.trim();
        Map<String, Object> params = new LinkedHashMap<>();
        for (Map<String, Object> p : parameters) {
            params.put(String.valueOf(p.get("name")), p.get("value"));
        }

        Matcher where = Pattern.compile("where\\s+c\\.(\\w+)\\s*=\\s*(@\\w+|'[^']*'|\"[^\"]*\"|\\S+)",
                Pattern.CASE_INSENSITIVE).matcher(q);
        if (where.find()) {
            String field = where.group(1);
            String raw = where.group(2);
            Object value = raw.startsWith("@") ? params.get(raw) : raw.replaceAll("^['\"]|['\"]$", "");
            List<Object> filtered = new ArrayList<>();
            for (Object row : rows) {
                Object actual = ((Map<String, Object>) row).get(field);
                if (String.valueOf(actual).equals(String.valueOf(value))) {
                    filtered.add(row);
                }
            }
            rows = filtered;
        }

        if (Pattern.compile("select\\s+value\\s+count\\(", Pattern.CASE_INSENSITIVE).matcher(q).find()) {
            List<Object> count = new ArrayList<>();
            count.add(rows.size());
            return count;
        }

        Matcher top = Pattern.compile("select\\s+top\\s+(\\d+)", Pattern.CASE_INSENSITIVE).matcher(q);
        if (top.find()) {
            int n = Integer.parseInt(top.group(1));
            if (rows.size() > n) rows = new ArrayList<>(rows.subList(0, n));
        }
        return rows;
    }
}
