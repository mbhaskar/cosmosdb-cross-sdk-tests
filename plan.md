# CosmosDB SDK Pre-Release Test Plan

Comprehensive scenario-based test plan for validating CosmosDB SDKs before release,
written from the perspective of a customer taking an application to production.

---

## Phase 1: Client Bootstrapping & Metadata Resolution

### 1.1 Client Initialization & Auth
| # | Scenario | Validation |
|---|----------|------------|
| 1 | Create client with master key auth | Client initializes, no errors |
| 2 | Create client with AAD/token credential auth | Client creates successfully; token acquisition is lazy (happens on first request, not during construction) |
| 3 | Create client with resource tokens | Scoped access works correctly |
| 4 | Create client with permission feed | Permission-based auth functional, resource tokens parsed eagerly |
| 5 | Create client with invalid endpoint | Meaningful error surfaced quickly (GetDatabaseAccount fails) |
| 6 | Create client with invalid key/token | Auth error surfaced on first operation, client does not hang |
| 7 | Create client in Direct mode (default) | RNTBD transport client created; TCP connections are lazy until first op or warmup |
| 8 | Create client in Gateway mode | HTTPS pipeline used for all data plane calls |
| 9 | Create client with custom connection policy (timeouts, idle connection timeout, max pool size) | Settings honored in transport layer |
| 10 | Create client with preferred regions configured | Endpoint resolution uses preferred region list |
| 11 | Create client with proxy settings | Traffic routes through proxy |
| 12 | Client creation with proactive warmup (`openConnectionsAndInitCaches`) for specific containers | Collection cache + PKRange cache + addresses pre-populated for those containers |
| 13 | Proactive warmup partially fails (one container doesn't exist) | Client still usable for other containers; missing container errors on access |
| 14 | AAD token endpoint temporarily unreachable at client creation time | Client creation succeeds (token fetch is lazy); first operation retries token acquisition |

### 1.2 Account Metadata Resolution (Eager — during `init()`)
| # | Scenario | Validation |
|---|----------|------------|
| 15 | GetDatabaseAccount on startup (eager call) | Account properties (consistency, regions, capabilities) fetched and cached |
| 16 | GetDatabaseAccount fails on default endpoint, retries on preferred region endpoints | Client initializes successfully by discovering account from alternate endpoint |
| 17 | GetDatabaseAccount fails on all endpoints | Client creation fails with clear connectivity/auth error |
| 18 | Account with multiple write regions detected | Multi-master config set, session container may upgrade to RegionScopedSessionContainer |
| 19 | Account with single write + multiple read regions | Correct endpoint topology resolved in LocationCache |
| 20 | GatewayServiceConfigurationReader reads replication policy, consistency, query engine config | Configuration available for subsequent operations |
| 21 | Account metadata refresh on manual/automatic failover | New write/read endpoints discovered via background refresh |
| 22 | Force refresh of database account metadata | Stale endpoints replaced |

### 1.3 Global Endpoint Manager & Background Refresh
| # | Scenario | Validation |
|---|----------|------------|
| 23 | Periodic location refresh timer starts after init | Background scheduler runs without blocking operations |
| 24 | Background refresh discovers new region added to account | New region becomes routable without client restart |
| 25 | Background refresh discovers region removed from account | Removed region no longer used for routing |
| 26 | Background endpoint health check marks region unhealthy | Reads/writes reroute to healthy region |
| 27 | Background health check detects region recovery | Traffic returns to recovered region |
| 28 | Endpoint refresh during sustained workload | No latency spike or operation failure during refresh |

### 1.4 Collection/Container Metadata Cache (Lazy)
| # | Scenario | Validation |
|---|----------|------------|
| 29 | First operation to a container triggers lazy `ReadCollection` call | Container metadata (PK definition, indexing policy, RID) fetched and cached |
| 30 | Subsequent operations to same container reuse cached metadata | No additional `ReadCollection` network call |
| 31 | Collection cache invalidation after container recreate (NameCacheIsStale / Gone 410) | Stale cache detected, re-fetched, operation retried transparently |
| 32 | Read collection properties explicitly | Returns PK path, indexing policy, TTL, unique keys, conflict resolution policy |
| 33 | Collection cache with large number of containers (100+) | All containers resolvable without perf degradation |
| 34 | Container properties cache populated via `_get_partition_key_definition` on first item op | PK definition available for routing without explicit ReadContainer |
| 35 | Lazy collection fetch fails (transient 503) | Retry succeeds, operation completes transparently |

### 1.5 Partition Key Range Cache (Lazy)
| # | Scenario | Validation |
|---|----------|------------|
| 36 | First query/write triggers lazy `ReadPartitionKeyRanges` fetch | PK ranges fetched from gateway and cached in routing map |
| 37 | Subsequent operations reuse cached PKRange map | No additional network call for routing |
| 38 | PKRange cache with multi-page response (50+ physical partitions) | All continuation pages fetched, complete `CollectionRoutingMap` built |
| 39 | PKRange cache refresh after partition split (Gone/PartitionKeyRangeGone) | Parent range discarded, child ranges fetched, operation retried |
| 40 | PKRange cache refresh after partition merge | Merged ranges detected, routing map updated |
| 41 | Cross-partition query with split occurring mid-enumeration | Query resumes on child ranges via updated cache |
| 42 | Lazy PKRange fetch fails on first attempt (transient error) | Retry succeeds, operation completes |
| 43 | `_discard_parent_ranges` correctly removes stale parent after split | Only child ranges remain in routing map |

### 1.6 Address Resolution / Routing (Lazy in Direct Mode)
| # | Scenario | Validation |
|---|----------|------------|
| 44 | First operation triggers address resolution for target partition (Direct mode) | Replica addresses resolved via gateway, connection established |
| 45 | Address cache populated for multiple replicas (read quorum) | Multiple replica endpoints available for reads |
| 46 | Address refresh on replica failover (Gone/PartitionMigrated) | Stale address detected, new replica address resolved |
| 47 | Address cache TTL expiry and background refresh | Addresses refreshed before they become stale |
| 48 | Force address refresh after TCP connection failure | New replicas discovered, operation retried on fresh connection |
| 49 | RNTBD TCP + TLS handshake on first connection to replica | Connection established, protocol version negotiated successfully |
| 50 | Background connection health monitoring detects dead connection | Unhealthy connection recycled, new connection established transparently |
| 51 | Gateway mode: all routing through gateway endpoint | No direct replica address resolution needed |

### 1.7 Session Container Lifecycle (Lazy)
| # | Scenario | Validation |
|---|----------|------------|
| 52 | Session container initialized empty at client creation | No session tokens present until first response |
| 53 | First successful write populates session token in container | Subsequent reads carry session token for read-your-writes |
| 54 | Session container upgraded to RegionScopedSessionContainer for multi-write accounts | Per-region session tokens tracked independently |
| 55 | Session token propagation: write in Region A, read in Region B with session consistency | Read waits for replication or retries with correct regional token |
| 56 | Session token merge/split when partition key range splits | Combined token returned for new child ranges, no session loss |
| 57 | Session token header set on all requests when consistency is Session | `x-ms-session-token` present in request headers |
| 58 | disableSessionCapturing flag honored | Session tokens not captured even on writes |

### 1.8 Pipeline & Policy Composition
| # | Scenario | Validation |
|---|----------|------------|
| 59 | Request pipeline policies execute in correct order (Headers → Proxy → UserAgent → Retry → Auth → Tracing → Logging) | Each policy processes request/response in sequence |
| 60 | Custom retry policy injected via builder | Custom policy invoked instead of default |
| 61 | User-agent string contains SDK version, OS, custom suffix | Server receives correct user-agent header |
| 62 | Distributed tracing policy creates spans for operations | OpenTelemetry spans emitted with correct attributes |
| 63 | Content response on write disabled (`noResponseOnWrite`) | Write operations return 204, no body, lower RU |

### 1.9 Telemetry & Diagnostics Initialization
| # | Scenario | Validation |
|---|----------|------------|
| 64 | Client telemetry background scheduler starts during init | Periodic telemetry reports emitted without impacting operation latency |
| 65 | CPU/memory diagnostics listener registered | System metrics visible in CosmosDiagnostics on exceptions |
| 66 | Diagnostics logging enabled at client level | Slow/failed requests logged with full timeline |
| 67 | Client metrics (Micrometer/OpenTelemetry) wired during init | RU, latency, connection metrics available from first operation |

### 1.10 Throughput Control Initialization
| # | Scenario | Validation |
|---|----------|------------|
| 68 | Throughput control group enabled before first operation | Rate limiter wired into store model, first op subject to throughput control |
| 69 | Throughput control with server-side control group | Control group reads from dedicated container, limits enforced |
| 70 | Multiple throughput control groups configured | Each group enforced independently |

---

## Phase 2: Database & Container Lifecycle (Control Plane)

### 2.1 Database Operations
| # | Scenario | Validation |
|---|----------|------------|
| 71 | Create database with shared throughput | Database created, offer attached |
| 72 | Create database (serverless account) | Database created without throughput |
| 73 | Create database - already exists (409) | Conflict error returned |
| 74 | CreateIfNotExists - database exists | No error, existing DB returned |
| 75 | CreateIfNotExists - database doesn't exist | Database created |
| 76 | Read database properties | Properties match creation params |
| 77 | List all databases (multi-page if >100) | Pagination works, all DBs returned |
| 78 | Delete database | Database removed, subsequent access fails |
| 79 | Read/Replace database throughput | Throughput read matches, replace updates RU/s |

### 2.2 Container Operations
| # | Scenario | Validation |
|---|----------|------------|
| 80 | Create container with partition key (single path) | Container created, PK set |
| 81 | Create container with hierarchical partition key | Multi-level PK functional |
| 82 | Create container with dedicated throughput | Offer attached to container |
| 83 | Create container with autoscale throughput | Autoscale max RU/s set |
| 84 | Create container with TTL enabled (default + per-item) | TTL honored on item expiry |
| 85 | Create container with unique key policy | Unique constraint enforced on insert |
| 86 | Create container with custom indexing policy (include/exclude paths, composite, spatial) | Index config persisted |
| 87 | Create container with vector embedding policy | Vector index created |
| 88 | Create container with full-text search policy | Full-text index created |
| 89 | Create container with computed properties | Computed props stored in definition |
| 90 | Create container with conflict resolution policy (LWW, custom, async) | Policy honored |
| 91 | Create container with change feed policy (all versions and deletes) | Change feed mode enabled |
| 92 | Create container with analytical store TTL | Analytical store enabled |
| 93 | CreateIfNotExists - container exists | Existing container returned |
| 94 | Read container properties | All policies returned correctly |
| 95 | Replace container (change indexing policy, TTL) | Changes persisted |
| 96 | List all containers in a database | All containers enumerated |
| 97 | Delete container | Container removed |
| 98 | Read/Replace container throughput (manual ↔ autoscale migration) | Throughput updated |

---

## Phase 3: Item (Document) CRUD Operations

### 3.1 Create (Insert)
| # | Scenario | Validation |
|---|----------|------------|
| 99 | Create item with auto-generated ID | Item persisted, ID assigned |
| 100 | Create item with explicit ID and partition key | Item stored in correct partition |
| 101 | Create item - conflict (409, duplicate ID+PK) | Conflict error returned |
| 102 | Create item with nested/complex JSON | Full document round-trips correctly |
| 103 | Create item with hierarchical partition key value | Routed to correct sub-partition |
| 104 | Create item with TTL set on item | Item expires after TTL seconds |
| 105 | Create item violating unique key constraint | 409 returned |
| 106 | Create large item (close to 2MB limit) | Succeeds at limit, fails above |

### 3.2 Read (Point Read)
| # | Scenario | Validation |
|---|----------|------------|
| 107 | Read item by ID + partition key | Correct document returned, low RU |
| 108 | Read item - not found (404) | NotFound error, no crash |
| 109 | Read item with specific consistency level override (e.g., Eventual on a Session account) | Consistency honored |
| 110 | Read item with ETag / If-None-Match (conditional read, 304) | Not-modified response when unchanged |

### 3.3 Replace
| # | Scenario | Validation |
|---|----------|------------|
| 111 | Replace item (full document update) | New content persisted |
| 112 | Replace item with ETag (optimistic concurrency) - match | Replace succeeds |
| 113 | Replace item with ETag - mismatch (412 Precondition Failed) | Conflict surfaced |
| 114 | Replace item - not found (404) | Error returned |

### 3.4 Upsert
| # | Scenario | Validation |
|---|----------|------------|
| 115 | Upsert - item does not exist (insert path) | Item created |
| 116 | Upsert - item exists (replace path) | Item updated |
| 117 | Upsert with ETag condition | Conditional behavior honored |

### 3.5 Patch (Partial Update)
| # | Scenario | Validation |
|---|----------|------------|
| 118 | Patch - Add operation | New property added |
| 119 | Patch - Set operation | Existing property updated |
| 120 | Patch - Replace operation | Property value replaced |
| 121 | Patch - Remove operation | Property removed |
| 122 | Patch - Increment operation | Numeric value incremented atomically |
| 123 | Patch - Move operation | Property moved to new path |
| 124 | Patch with multiple operations in one request | All ops applied atomically |
| 125 | Patch with filter predicate (conditional patch) | Patch applied only if predicate met |
| 126 | Patch on non-existent item (404) | Error returned |

### 3.6 Delete
| # | Scenario | Validation |
|---|----------|------------|
| 127 | Delete item by ID + partition key | Item removed |
| 128 | Delete item - not found (404) | Error or no-op depending on SDK |
| 129 | Delete item with ETag condition | Conditional delete honored |
| 130 | Delete all items by partition key (async purge) | All items in PK removed |

### 3.7 Read Many (Batch Point Reads)
| # | Scenario | Validation |
|---|----------|------------|
| 131 | ReadMany with multiple (ID, PK) pairs from same partition | Items returned |
| 132 | ReadMany with (ID, PK) pairs spanning multiple partitions | Cross-partition read aggregated |
| 133 | ReadMany with some items not found | Found items returned, missing ones indicated |
| 134 | ReadMany with large batch (1000+ items) | Paginated internally, all returned |

---

## Phase 4: Query Operations

### 4.1 Basic Queries
| # | Scenario | Validation |
|---|----------|------------|
| 135 | Single-partition query with partition key specified | Only target partition scanned |
| 136 | Cross-partition query (fan-out) | Results from all partitions aggregated |
| 137 | Query with parameterized SQL (`@param`) | Parameters bound, injection-safe |
| 138 | Query returning 0 results | Empty result set, no error |
| 139 | Query with TOP/LIMIT | Result count capped |
| 140 | Query with ORDER BY (single field, composite) | Results sorted correctly |
| 141 | Query with GROUP BY / aggregates (COUNT, SUM, AVG, MIN, MAX) | Correct aggregation |
| 142 | Query with DISTINCT | Duplicates eliminated |
| 143 | Query with WHERE on indexed property | Efficient (low RU) |
| 144 | Query with WHERE on non-indexed property | Full scan (higher RU) but correct |
| 145 | Query with ARRAY_CONTAINS, spatial functions, UDF calls | Function evaluation correct |
| 146 | Query with JOIN (intra-document) | Nested array join works |
| 147 | Query with OFFSET...LIMIT (pagination) | Correct page returned |
| 148 | Query with VALUE projection | Scalar/flat results returned |

### 4.2 Pagination (Continuation Tokens)
| # | Scenario | Validation |
|---|----------|------------|
| 149 | Query with small maxItemCount (e.g., 10) forcing multi-page | All pages fetched via continuation |
| 150 | Resume query from continuation token (stateless pagination) | Correct next page returned |
| 151 | Cross-partition query pagination | Pages span partitions correctly |
| 152 | Continuation token with ORDER BY cross-partition | Ordering maintained across pages |
| 153 | Invalid/expired continuation token | Meaningful error |
| 154 | Enumerate all pages to completion (drain) | HasMoreResults becomes false, no data loss |

### 4.3 Feed Range Queries
| # | Scenario | Validation |
|---|----------|------------|
| 155 | Read feed ranges for a container | Full set of ranges returned |
| 156 | Query scoped to specific feed range | Only that range's data returned |
| 157 | Parallel queries over all feed ranges | Full data set covered, no overlap |

---

## Phase 5: Change Feed

| # | Scenario | Validation |
|---|----------|------------|
| 158 | Change feed - start from beginning (LatestVersion mode) | All historical changes returned |
| 159 | Change feed - start from Now | Only new changes captured |
| 160 | Change feed - start from specific point in time | Changes after timestamp returned |
| 161 | Change feed - resume from continuation token | No missed or duplicate changes |
| 162 | Change feed - single partition key scope | Only that PK's changes |
| 163 | Change feed - feed range scope | Only that range's changes |
| 164 | Change feed - AllVersionsAndDeletes mode | Deletes and all versions captured |
| 165 | Change feed with partition split during processing | Continuation splits, both children processed |
| 166 | Change feed processor (Java) / equivalent pattern | Lease-based distributed processing works |
| 167 | Change feed with large batch of changes (multi-page) | All pages drained |

---

## Phase 6: Bulk & Transactional Batch

### 6.1 Transactional Batch
| # | Scenario | Validation |
|---|----------|------------|
| 168 | Batch with mixed ops (create + replace + delete) in same PK | All-or-nothing atomicity |
| 169 | Batch - one operation fails (e.g., conflict) | Entire batch rolled back |
| 170 | Batch at max operation count (100 ops) | Succeeds at limit |
| 171 | Batch exceeding size limit | 413 error returned |
| 172 | Batch with read operations | Reads + writes in single transaction |
| 173 | Batch with patch operations | Partial updates in transaction |

### 6.2 Bulk Execution (Java / non-transactional)
| # | Scenario | Validation |
|---|----------|------------|
| 174 | Bulk create of 10,000 items | All items created, throughput utilized efficiently |
| 175 | Bulk upsert spanning multiple partitions | Items routed correctly |
| 176 | Bulk with throttling (429) | SDK retries internally, all items eventually succeed |
| 177 | Bulk with partial failures (some 409 conflicts) | Failures reported per-item, successes committed |
| 178 | Bulk with rate limiting / congestion control | Throughput adapts, doesn't overwhelm service |

---

## Phase 7: Stored Procedures, Triggers & UDFs

| # | Scenario | Validation |
|---|----------|------------|
| 179 | Create stored procedure | SP registered |
| 180 | Execute stored procedure (with params, within PK scope) | Correct result returned |
| 181 | SP execution exceeding timeout | Timeout error returned |
| 182 | Create pre-trigger / post-trigger | Trigger registered |
| 183 | Item create with pre-trigger | Trigger modifies request |
| 184 | Create UDF | UDF registered |
| 185 | Query using UDF | UDF evaluated in query |
| 186 | Replace / Delete SP, trigger, UDF | CRUD lifecycle works |
| 187 | List all SPs / triggers / UDFs | Enumeration works |

---

## Phase 8: Users & Permissions

| # | Scenario | Validation |
|---|----------|------------|
| 188 | Create user in database | User resource created |
| 189 | Create permission (read/all) on container for user | Permission with resource token generated |
| 190 | Access container with resource token (scoped permission) | Allowed ops succeed, denied ops fail (403) |
| 191 | Permission with partition key scope | Access limited to specific PK |
| 192 | Permission expiry (custom token validity) | Token expires, access denied after TTL |
| 193 | List / Replace / Delete permissions | Full lifecycle works |

---

## Phase 9: Retry & Error Handling

### 9.1 Throttling (429 - Rate Limiting)
| # | Scenario | Validation |
|---|----------|------------|
| 194 | Single request throttled (429) | SDK retries after retry-after-ms, succeeds |
| 195 | Sustained throttling beyond max retry count | 429 surfaced to caller after retries exhausted |
| 196 | Custom throttle retry options (max wait, max retries) | Custom config honored |
| 197 | Bulk/concurrent workload causing throttling | Backpressure applied, throughput stabilizes |
| 198 | Throughput bucket-scoped throttling | Only bucket-scoped requests throttled |

### 9.2 Transient Failures & Connection Errors
| # | Scenario | Validation |
|---|----------|------------|
| 199 | TCP connection reset mid-request (Direct mode) | SDK retries on new connection |
| 200 | HTTP timeout on gateway call | Retry with backoff |
| 201 | DNS resolution failure (transient) | Retry succeeds after DNS recovers |
| 202 | Service unavailable (503) | SDK retries, succeeds after recovery |
| 203 | Request timeout configured + exceeded | Timeout error surfaced to caller |

### 9.3 Gone Exceptions (410) / Partition Splits
| # | Scenario | Validation |
|---|----------|------------|
| 204 | Gone - PartitionKeyRangeGone (split) | PKRange cache refreshed, request re-routed |
| 205 | Gone - NameCacheIsStale (collection recreated) | Collection cache refreshed, retry succeeds |
| 206 | Gone - PartitionMigrated | Address cache refreshed, request re-routed |
| 207 | Gone during query enumeration | Query resumes from split child ranges |
| 208 | Gone during change feed processing | Continuation updated for new ranges |

### 9.4 Session Consistency Retries
| # | Scenario | Validation |
|---|----------|------------|
| 209 | Read with session token not yet available on replica | SDK retries on another replica |
| 210 | Session token propagation across requests | Write's session token used in subsequent read |
| 211 | Session retry with region-scoped session tokens | Correct region's token used |
| 212 | SessionNotAvailable after max retries | Error surfaced to caller |

### 9.5 Write Retries & Idempotency
| # | Scenario | Validation |
|---|----------|------------|
| 213 | Idempotent write retry (create with same ID after ambiguous failure) | No duplicate created |
| 214 | Non-idempotent write retry policy (if enabled) | Retry behavior matches config |
| 215 | Write timeout with unknown outcome | Caller informed, can check and retry |

### 9.6 Endpoint Discovery & Failover Retries
| # | Scenario | Validation |
|---|----------|------------|
| 216 | Write to failed-over region | Endpoint discovery triggers, write routes to new primary |
| 217 | Read failover to secondary region | Reads succeed on secondary |
| 218 | All preferred regions unavailable | Falls back to available region or errors clearly |

---

## Phase 10: Multi-Region & High Availability

| # | Scenario | Validation |
|---|----------|------------|
| 219 | Client configured with preferred regions | Reads go to nearest preferred region |
| 220 | Manual failover (write region change) | Client discovers new write endpoint |
| 221 | Automatic failover (simulated region down) | Client fails over reads/writes transparently |
| 222 | Multi-master: writes to local region | Writes succeed in preferred write region |
| 223 | Multi-master: conflict detection (LWW) | LWW resolves conflicts, winner consistent |
| 224 | Region exclusion (exclude specific region) | Excluded region not used for routing |
| 225 | Per-partition automatic failover | Unhealthy partition routes to different region |
| 226 | Availability strategy with hedged requests | Parallel request to secondary if primary slow |
| 227 | Cross-region session consistency | Session token honored across regions |
| 228 | Circuit breaker per partition | Repeated failures trip circuit, traffic re-routed |

---

## Phase 11: Consistency Levels

| # | Scenario | Validation |
|---|----------|------------|
| 229 | Strong consistency - read after write | Latest write always visible |
| 230 | Bounded staleness - read within staleness window | Data within configured lag |
| 231 | Session consistency - read your own writes | Same session sees own writes |
| 232 | Consistent prefix - ordered reads | No out-of-order observations |
| 233 | Eventual consistency - low-latency reads | Reads succeed, may be stale |
| 234 | Per-request consistency level override (weaker than account) | Override honored |
| 235 | Attempt stronger consistency than account (should fail) | Error returned |

---

## Phase 12: Diagnostics, Metrics & Observability

| # | Scenario | Validation |
|---|----------|------------|
| 236 | Request diagnostics on success | RU charge, latency, regions contacted, retries visible |
| 237 | Request diagnostics on failure | Full context (endpoint, status code, sub-status, retries, timeline) |
| 238 | End-to-end latency breakdown in diagnostics | Network, backend, retry time visible |
| 239 | Diagnostics logging enabled | Logs emitted for slow/failed requests |
| 240 | Client-level metrics (Micrometer/OpenTelemetry) | RU, latency, error rate metrics exported |
| 241 | RNTBD/connection pool metrics (Direct mode) | Connection count, transit time visible |
| 242 | Distributed tracing (OpenTelemetry spans) | Spans created for each SDK operation |
| 243 | Response headers (activity-id, session-token, RU charge) accessible | Headers available via response/hook |

---

## Phase 13: Throughput Management

| # | Scenario | Validation |
|---|----------|------------|
| 244 | Read throughput (database-level shared) | Current RU/s returned |
| 245 | Read throughput (container-level dedicated) | Current RU/s returned |
| 246 | Replace throughput (scale up) | New RU/s takes effect |
| 247 | Replace throughput (scale down) | New RU/s takes effect |
| 248 | Autoscale: read max throughput | Max RU/s returned |
| 249 | Autoscale: scale activity under load | Auto-scales up, then back down |
| 250 | Throughput bucket configuration | Bucket isolation honored |
| 251 | Serverless account: no throughput provisioning | Throughput operations not applicable |

---

## Phase 14: Performance & Concurrency

| # | Scenario | Validation |
|---|----------|------------|
| 252 | Sustained write workload (1000 ops/sec for 5 min) | Stable latency, no connection leaks |
| 253 | Sustained read workload (5000 reads/sec for 5 min) | Stable P50/P99, no degradation |
| 254 | Concurrent async operations (1000 in-flight) | All complete, no deadlocks |
| 255 | Connection pool behavior under load (Direct mode) | Pool scales up, recycles idle conns |
| 256 | Client singleton pattern (shared across threads) | Thread-safe, no corruption |
| 257 | Large document reads/writes (1MB+) | No timeout, data integrity maintained |
| 258 | Query with large result set (100K+ items) | All pages drained, memory stable |

---

## Phase 15: SDK Lifecycle & Configuration

| # | Scenario | Validation |
|---|----------|------------|
| 259 | Client close/dispose | Connections released, resources freed |
| 260 | Operations after client close | Clear error, no hang |
| 261 | Multiple client instances to same account | Both functional, no interference |
| 262 | User-agent string customization | Custom suffix appears in headers |
| 263 | Request-level options override (consistency, partition key, session token) | Per-request overrides work |
| 264 | Response hook / interceptor | Hooks invoked with response metadata |
| 265 | Content serialization (custom serializer config) | POJOs / custom types round-trip correctly |
| 266 | Null / empty partition key handling | Routed to "empty PK" partition |
| 267 | No-response-on-write optimization | 204 returned, RU saved |

---

## Phase 16: Edge Cases & Negative Testing

| # | Scenario | Validation |
|---|----------|------------|
| 268 | Operation on deleted database (404) | Clear error |
| 269 | Operation on deleted container (404) | Clear error |
| 270 | Malformed JSON document | 400 Bad Request |
| 271 | Item exceeding max size (>2MB) | 413 Request Entity Too Large |
| 272 | Deeply nested document (128 levels) | Accepted at limit, rejected beyond |
| 273 | Partition key value exceeding max length | Error surfaced |
| 274 | Query exceeding max response size without pagination | Forced pagination via continuation |
| 275 | Network partition (complete connectivity loss) | Timeout errors, retry exhaustion, clear diagnostics |
| 276 | Clock skew between client and service | Auth still works within tolerance |
| 277 | Concurrent replace with ETag (conflict detection) | One wins, other gets 412 |
| 278 | UTF-8 / Unicode in document properties and partition keys | Full Unicode support |
| 279 | Special characters in IDs (spaces, slashes, etc.) | URL-encoded correctly, round-trips |
| 280 | Rapid container create/delete/recreate | Client handles stale caches gracefully |

---

## Execution Notes

**Priority order for pre-release validation:**
1. Phase 1 (Bootstrap) → Phase 3 (CRUD) → Phase 4 (Query) → Phase 9 (Retry) — these are the critical path
2. Phase 10 (Multi-region) → Phase 5 (Change Feed) → Phase 6 (Bulk/Batch) — production resilience
3. Remaining phases — completeness

**Environment matrix:**
- Connection modes: Direct + Gateway
- Consistency levels: Session (default) + at least one other
- Account types: Provisioned throughput + Serverless
- Regions: Single-region + Multi-region (2+ regions)
- Partition counts: Single partition + Multi-partition (50+ physical partitions for pagination tests)
- SDK variants: Sync + Async client APIs

**Key metrics to capture:**
- End-to-end latency (P50, P95, P99)
- RU consumption per operation type
- Retry count and success rate
- Connection pool utilization
- Memory / resource leak indicators over sustained runs

