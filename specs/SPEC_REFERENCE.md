# Scenario Spec Reference

A quick reference for every keyword used in the `specs/**/*.yaml` scenario files.
This mirrors the runner code (`harness/python/cosmos_test_runner/`) and the
mitmproxy fault registry (`proxy/mitm/fault_engine.py`); `specs/schema.yaml` is
the inline companion. When code and this doc disagree, the code wins — please
update both.

- Actions: `harness/python/cosmos_test_runner/step_handlers.py`
- Assertions: `harness/python/cosmos_test_runner/assertions.py`
- Timeline routing: `harness/python/cosmos_test_runner/executor.py` (`_fire_events`)
- L7 fault registry: `proxy/mitm/fault_engine.py` (`FAULTS`)

---

## Top-level fields

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Unique scenario id (e.g. `T-308`, `C-230`, `#127`). |
| `phase` | int | Phase number (1..16). |
| `section` | string | Sub-section reference (e.g. `"6.8"`). |
| `title` | string | Short human-readable title. |
| `description` | string | Optional long description. |
| `tags` | [string] | Free-form tags for filtering. |
| `backends` | [string] | Subset of `mock`, `emulator`, `live`. Default: all. |
| `runners` | [string] | Subset of `python`, `java`. Default: all. Gates features a runner hasn't implemented (e.g. transport faults are `[python]`). |
| `control_plane` | block | Opts into the stateful **mock** router (partition topology, PKRange cache, throttle). |
| `fault_injection` | block | Opts into the **proxy stack** (Toxiproxy + mitmproxy). Emulator/live only. |
| `fixture` | block | Provisions an isolated db/container before steps, tears down after. |
| `timeline` | list | Schedules control/fault events relative to steps. |
| `steps` | list | Ordered operations (required). |

---

## `fixture`

```yaml
fixture:
  database: "auto"          # "auto" => unique per-run + per-sdk db id
  container:
    id: "orders"
    partition_key: "/customerId"
```

`"auto"` databases are named `mvp-{scenario}-{sdk}-{run_id}` so concurrent SDK
runs (python/java) never collide on the same data.

---

## `steps`

```yaml
steps:
  - id: <string>            # optional; result stored under ctx.steps.<id>
    action: <string>        # see actions below
    params: {...}           # supports ${...} substitution
    expect: [ ... ]         # assertions for this step's result
```

### Actions

| Action | Notes |
|---|---|
| `create_client` | Explicitly (re)create the SDK client. |
| `create_database` | |
| `create_container` | |
| `create_item` | `params: { item: {...} }` |
| `seed_items` | `params: { count: 25, template: { id: "o{n}", pk: "c{n}" } }` — bulk insert, `{n}` expands 1..count. |
| `read_item` | `params: { id, partition_key }` |
| `replace_item` | Full document update. |
| `upsert_item` | |
| `delete_item` | |
| `query_items` | `params: { query, cross_partition?, parameters? }` |
| `query_drain` | Like `query_items` but drains **all** continuation pages (defaults `cross_partition: true`); used for pagination integrity under faults. |
| `delete_database` | |
| `loop` | Sustained-load construct (see below). |

### `loop`

```yaml
- id: soak
  action: loop
  count: 50                 # iterations, OR
  duration: "10s"           # wall-clock budget: "500ms" | "10s" | "2m"
  steps: [ ... ]            # nested steps run each iteration
  expect: [ ... ]           # loop-scoped assertions (latency/resource)
```

### Substitution variables (in `params`)

| Variable | Resolves to |
|---|---|
| `${run_id}` | Unique id for this run. |
| `${db}` / `${container}` | Fixture database / container ids. |
| `${connection_mode}` | Configured connection mode. |
| `${uuid}` / `${now}` | Generated values. |
| `${steps.<id>.id}` | Id of the item produced by a prior step. |
| `${steps.<id>.item.<field>}` | Field from a prior step's returned item. |

---

## `expect` — assertion types

Any assertion accepts an optional `name:` to override the reported label.

### Basic

| Assertion | Meaning |
|---|---|
| `{ type: ok }` (alias `no_error`) | Operation succeeded. |
| `{ type: error }` | Operation failed. |
| `{ type: status_code, value: 201 }` | Exact HTTP status. |
| `{ type: error_status, value: 409 }` | Failed with the given status. |
| `{ type: item_count, value: 3 }` | Query returned exactly N items. |
| `{ type: count_gte, value: 1 }` | Query returned >= N items. |
| `{ type: field_equals, path: item.total, value: 99 }` | Field equals value. |
| `{ type: metric_equals, metric: metadata_calls.read_collection, value: 1 }` | Metric equals value. |
| `{ type: metric_zero, metric: metadata_calls.read_pk_ranges }` | Metric is zero. |

### Control-plane (require `control_plane`/timeline; python runner)

| Assertion | Meaning |
|---|---|
| `{ type: metric_delta, metric: ..., over: [stepA, stepB], value: 0 }` | Metric change between two step snapshots (cache hit = 0). |
| `{ type: sequence, of: status_codes, equals: [410, 200] }` | Exact ordered protocol responses for a step, including internal retries. |
| `{ type: sequence, of: metadata_calls, contains_in_order: [read_pk_ranges] }` | Ordered subsequence of metadata calls during a step. |
| `{ type: page_size_at_most, value: 2 }` | Result page has <= N items. |

### Transport / protocol fault (require `fault_injection`; emulator/live tier)

| Assertion | Meaning |
|---|---|
| `{ type: eventually_ok }` | Succeeded after the fault cleared/retried; documents recovery (alias of `ok`). |
| `{ type: retry_count_gte, value: 1 }` | SDK retried at least N times (`metrics.retries`). |
| `{ type: latency_percentile, percentile: 99, max_ms: 2000, scope: <loop_id> }` | pNN of per-step latency samples stays under `max_ms` (defaults to current loop scope). |
| `{ type: resource_stable, metric: connections_opened, tolerance_pct: 25 }` | Sampled resource drifts <= N% across a loop (no connection storm). |
| `{ type: failover_region, equals: "secondary" }` | Contacted region (from SDK diagnostics) matches (T-305). |
| `{ type: pages_cover_set, expected_count: 25 }` | Drained result set has no dupes and covers exactly N ids (no data loss). |

> Note: `retry_count_gte` reads a **heuristic** counter (`_CountingTransport` in
> `backends.py`), not the SDK's internal retry count. On a healthy endpoint with
> no proxy/fault in front, `retries=0` is correct — the test simply isn't
> applicable there.

---

## `timeline`

```yaml
timeline:
  - after: <step_id>        # fire after the step completes (default)
    event: <name>
    args: { ... }
  - before: <step_id>       # or fire before the step
    event: <name>
```

Events are routed by tier. On the **mock** tier the transport/protocol verbs
log-and-skip (there's no proxy), so combined fault scenarios are gated to
emulator/live.

### Mock tier (L7, in-process stateful router)

| Event | Args |
|---|---|
| `split_partition` | `{ range: "1", into: ["1-a","1-b"] }` |
| `merge_partitions` | |
| `expire_pkrange_cache` | |
| `throttle` | `{ op: create_item, count: 2, retry_after_ms: 50, ru_penalty: 1.0 }` |

### Proxy tier — Toxiproxy (L4 TCP)

| Event | Args |
|---|---|
| `net_latency` | `{ latency_ms: 150, jitter_ms: 50 }` |
| `net_timeout` | `{ timeout_ms: 0 }` (0 = black-hole) |
| `net_reset` | |
| `net_bandwidth` | `{ rate_kbps: 8 }` |
| `net_slow_close` | `{ delay_ms: 500 }` |
| `region_down` / `region_up` | `{ region: primary }` |
| `reset_faults` | Clears all Toxiproxy toxics. |

### Proxy tier — mitmproxy (L7 HTTP status)

| Event | Args |
|---|---|
| `inject_fault` | `{ fault: <name>, seconds: N \| count: N, [status, substatus, retry_after_ms] }` — generic; names any registry fault. |
| `fault_clear` | Disarms the mitm fault. |
| `net_throttle_window` | `{ seconds: 120, retry_after_ms: 1000, status: 429 }` — **429 back-compat alias** of `inject_fault { fault: throttle_429 }`. |
| `throttle_window_clear` | Back-compat alias of `fault_clear`. |

Scope a mitm fault with **either** `seconds=N` (time window) **or** `count=N`
(inject on the first N requests, then pass through).

#### Registered L7 faults (`fault_engine.FAULTS`)

| `fault` name | status | x-ms-substatus | retry-after | SDK reaction |
|---|---|---|---|---|
| `throttle_429` | 429 | 3200 | yes | back off, retry after delay |
| `gone_410` | 410 | 1002 (PKRangeGone) | no | refresh routing cache, retry |
| `namecache_410` | 410 | 1000 (NameCacheStale) | no | refresh collection cache, retry |
| `retrywith_449` | 449 | 0 | no | immediate retry |
| `unavailable_503` | 503 | 0 | yes | retry another replica |

Add a new fault = one entry in `FAULTS` (no addon logic changes).

---

## `fault_injection`

```yaml
fault_injection:
  proxy: cosmos             # Toxiproxy proxy the net_* verbs target
  multi_region: true        # expects a cosmos-secondary route for failover (T-305)
  protocol: mitmproxy       # route the SDK through mitm (:18091) => L7 faults on
```

Behavior when set (emulator/live only):
- The executor repoints the SDK `endpoint` at the proxy (mitm `:18091` when
  `protocol` is set, else Toxiproxy `:18081`).
- Endpoint discovery is forced **off** (`enable_endpoint_discovery=False`) so the
  SDK stays pinned to the proxy instead of adopting the emulator's
  self-advertised address — **except** when `multi_region` is set (failover needs
  discovery on plus a real multi-region account).

---

## `control_plane`

```yaml
control_plane:
  partitions: 4             # initial PKRange count hint for the fixture
  consistency: session      # reserved; drives session-token behavior
```

---

## Proxy chain (when `fault_injection` is active)

```
SDK client ─▶ mitmproxy (:18091, L7 — 429/410/449/503)
           ─▶ Toxiproxy (:18081, L4 — latency/reset/bandwidth/timeout)
           ─▶ Cosmos emulator (:8081)
```

- Point the SDK at **:18091** for both tiers (L7 + L4).
- Point it at **:18081** for transport faults only.
- Toxiproxy admin API: **:8474**; secondary-region proxy: **:18082**.
