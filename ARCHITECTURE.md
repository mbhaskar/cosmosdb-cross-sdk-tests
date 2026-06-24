# CosmosDB Cross-SDK Test Runner — Architecture & Design

## Overview

A web-based test dashboard that provides a visual matrix of CosmosDB SDK test scenarios, allowing engineers to run, compare, and debug test results across multiple SDK languages and versions from a single interface.

---

## User Experience

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  CosmosDB SDK Test Matrix                              [Config ⚙️] [Run All ▶]  │
├─────────────────────────────────────────────────────────────────────────────────┤
│ Config: Backend [Emulator ▼]  Java SDK [4.63.0 ▼]  Python SDK [4.9.0 ▼]         │
│ Filter: [Phase ▼] [Status ▼] [Tags ▼] [Search...]                               │
├──────┬──────────────────────────────────────────┬──────────┬──────────┬─────────┤
│  #   │ Scenario                                 │  Java    │  Python  │  .NET   │
├──────┼──────────────────────────────────────────┼──────────┼──────────┼─────────┤
│  1   │ Create client with master key auth       │  [▶] ✅  │  [▶] ✅  │   [▶] ✅ │
│  2   │ Create client with AAD token auth        │  [▶] ⏳  │  [▶] ❌  │   [▶] ✅ │
│  3   │ Create client with resource tokens       │  [▶] —   │  [▶] —   │   —     │
│  ...                                                                            │
│  36  │ First query triggers lazy PKRange fetch  │  [▶] ✅  │  [▶] ✅  │   [▶] ✅ │
│  38  │ PKRange multi-page (50+ partitions)      │  [▶] ✅  │  [▶] ⚠️  │   [▶] ✅ │
├──────┴──────────────────────────────────────────┴──────────┴──────────┴─────────┤
│ Summary: Java 4.63.0: 68/70 ✅ │ Python 4.9.0: 65/70 ✅ │      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Key Interactions

- **Per-cell [▶]** — Run one scenario on one SDK version
- **Per-row [▶]** — Run one scenario across all SDKs (comparison mode)
- **Per-column [▶]** — Run all scenarios for one SDK
- **[Run All]** — Full matrix execution
- **Click any result cell** — Expands to detail panel (diagnostics, logs, metrics)
- **Diff column** — Highlights behavioral differences between SDKs
- **Config panel** — Select backend target, SDK versions, connection mode, fault profiles
- Have categories of tests which run only on live account / only with mock data ( ops that require huge amount of data) / only emulator
- Customizable mock data input

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│                         Browser (React / Next.js)                           │
│  ┌───────────────┐  ┌───────────────┐  ┌─────────────────────────────┐    │
│  │ Scenario Grid │  │  Run Controls │  │  Result Detail / Diff View  │    │
│  │   + Filters   │  │  + Config     │  │  + History / Trends         │    │
│  └───────────────┘  └───────────────┘  └─────────────────────────────┘    │
└────────────────────────────────┬───────────────────────────────────────────┘
                                 │ REST + WebSocket
┌────────────────────────────────▼───────────────────────────────────────────┐
│                     Orchestrator API (FastAPI / Python)                     │
│                                                                            │
│  • Reads YAML specs → serves scenario catalog                              │
│  • Manages SDK version registry                                            │
│  • Dispatches test execution to SDK runners (subprocess / container)       │
│  • Manages Toxiproxy fault profiles via API                                │
│  • Streams live execution status via WebSocket                             │
│  • Stores results in database for history/comparison                       │
│  • Supports parallel execution with concurrency control                    │
└───────┬──────────────────┬──────────────────┬──────────────────────────────┘
        │                  │                  │
        ▼                  ▼                  ▼
┌──────────────┐  ┌──────────────┐  ┌─────────────────────────┐
│  Java Runner │  │ Python Runner│  │  Fault Injection Proxy  │
│              │  │              │  │                         │
│  • Multiple  │  │  • Multiple  │  │  • Toxiproxy (TCP)     │
│    versions  │  │    versions  │  │  • Custom CosmosDB     │
│    via Maven │  │    via venv  │  │    response injector   │
│              │  │              │  │                         │
│  Runs as     │  │  Runs as     │  │  Profiles:             │
│  subprocess  │  │  subprocess  │  │  • throttle-429        │
│  or Docker   │  │  or Docker   │  │  • gone-split          │
│              │  │              │  │  • timeout             │
│              │  │              │  │  • region-down         │
└──────┬───────┘  └──────┬───────┘  └───────────┬─────────────┘
       │                  │                      │
       └──────────────────┴──────────────────────┘
                          │
              ┌───────────▼────────────┐
              │  CosmosDB Backend      │
              │                        │
              │  • Azure Cosmos DB     │
              │    Emulator (local)    │
              │  • Live Azure account  │
              └────────────────────────┘
```

---

## Component Details

### 1. Web Frontend

**Technology:** Next.js 14 + React + Tailwind CSS + shadcn/ui

| Feature | Description |
|---------|-------------|
| Scenario Matrix | Virtualized table (handles 280+ rows); columns per SDK; status badges |
| Config Panel | Backend selector (Emulator/Live), SDK version dropdowns (populated from registry), connection mode toggle, region config |
| Run Controls | Play buttons at cell/row/column/global level; cancel in-progress runs |
| Live Updates | WebSocket connection shows real-time progress (⏳ spinner, progress bar) |
| Result Detail | Expandable panel: execution timeline, RU charge, retry count, diagnostics JSON, wire logs |
| Cross-SDK Diff | Side-by-side comparison of behavior: "Java retried 3x in 200ms; Python retried 5x in 450ms" |
| History View | Chart per scenario showing pass/fail over time; regression alerts |
| Filters | By phase, status (pass/fail/skip), tags, fault type, free-text search |

### 2. Orchestrator API

**Technology:** Python (FastAPI) + SQLite/PostgreSQL + WebSocket

#### Endpoints

```
# Scenarios
GET  /api/scenarios                → List all scenarios (from YAML specs)
GET  /api/scenarios/:id            → Single scenario detail + run history

# SDK Registry
GET  /api/sdks                     → Available SDKs and their versions
POST /api/sdks/:name/versions      → Register a new SDK version
GET  /api/sdks/:name/versions      → List available versions for an SDK

# Test Execution
POST /api/runs                     → Start a test run
  Body: {
    scenarios: ["1", "2", "36"],   // or ["*"] for all
    sdks: [
      { name: "java", version: "4.63.0" },
      { name: "python", version: "4.9.0" }
    ],
    config: {
      backend: "emulator",         // or "live"
      connection_mode: "direct",   // or "gateway"
      fault_profile: null          // or profile name
    }
  }
  → Returns: { run_id: "run-abc123" }

GET  /api/runs/:runId              → Run status + results
WS   /api/runs/:runId/stream       → Live execution updates

# Fault Injection
GET  /api/proxy/profiles           → Available fault profiles
POST /api/proxy/activate           → Activate a profile
DELETE /api/proxy/activate         → Reset to passthrough

# History & Comparison
GET  /api/history/:scenarioId      → Past results for a scenario across versions
GET  /api/compare                  → Cross-SDK behavior comparison report
```

#### Orchestrator Execution Flow

```
1. Receive run request (scenarios + SDKs + config)
2. For each (scenario, sdk) pair:
   a. Read scenario YAML spec
   b. Check preconditions (container exists, partition count, etc.)
   c. Activate fault profile if scenario requires one
   d. Resolve SDK runner binary/container for requested version
   e. Spawn runner process with scenario JSON + config
   f. Stream stdout/stderr to WebSocket
   g. Collect structured result JSON from runner
   h. Deactivate fault profile
   i. Store result in database
   j. Push completion event to WebSocket
3. Generate run summary with cross-SDK comparison
```

### 3. SDK Version Management

Each SDK runner supports multiple versions. The orchestrator manages this via:

#### Java Version Management
```yaml
# sdk-registry.yaml
java:
  runner: "harness/java/cosmos-test-runner.jar"
  version_strategy: "maven"  # Download SDK JARs from Maven Central
  versions:
    - "4.63.0"
    - "4.62.0"
    - "4.61.0"
    - "local-snapshot"  # Points to local build in azure-sdk-for-java
  resolve: |
    # For released versions: download from Maven
    # For local-snapshot: use ../azure-sdk-for-java/sdk/cosmos/azure-cosmos/target/
```

#### Python Version Management
```yaml
python:
  runner: "harness/python/cosmos_test_runner"
  version_strategy: "venv"  # Separate virtualenv per version
  versions:
    - "4.9.0"
    - "4.8.0"
    - "4.7.0"
    - "local-dev"  # Points to local editable install
  resolve: |
    # For released versions: pip install azure-cosmos==X.Y.Z in isolated venv
    # For local-dev: pip install -e ../azure-sdk-for-python/sdk/cosmos/azure-cosmos/
```

#### Config Panel UI for SDK Versions

```
┌─ Configuration ──────────────────────────────────────────────┐
│                                                              │
│  Backend Target:  ○ Emulator (localhost:8081)                │
│                   ● Live (cosmosdb-test.documents.azure.com) │
│                                                              │
│  Connection Mode: ● Direct  ○ Gateway                        │
│                                                              │
│  ┌─ SDK Versions ─────────────────────────────────────┐     │
│  │                                                     │     │
│  │  Java SDK:    [4.63.0         ▼]  ☑ Enable         │     │
│  │               Versions: 4.63.0, 4.62.0, 4.61.0,    │     │
│  │               local-snapshot                        │     │
│  │                                                     │     │
│  │  Python SDK:  [4.9.0          ▼]  ☑ Enable         │     │
│  │               Versions: 4.9.0, 4.8.0, 4.7.0,       │     │
│  │               local-dev                             │     │
│  │                                                     │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                              │
│  Preferred Region: [West US      ▼]                          │
│  Consistency:      [Session      ▼]                          │
│                                                              │
│  [Apply & Restart Runners]                                   │
└──────────────────────────────────────────────────────────────┘
```

### 4. SDK Runners (Language-Native Harnesses)

Each runner is a CLI program with a common interface:

#### Input Contract
```json
{
  "scenario": {
    "id": "36",
    "steps": [...],
    "expected_outcome": {...}
  },
  "config": {
    "endpoint": "https://localhost:8081",
    "key": "...",
    "connection_mode": "direct",
    "preferred_regions": ["West US"],
    "consistency": "Session",
    "proxy_endpoint": "http://localhost:8474"
  },
  "sdk_version": "4.63.0"
}
```

#### Output Contract (Common JSON Schema)
```json
{
  "scenario_id": "36",
  "sdk": "java",
  "sdk_version": "4.63.0",
  "status": "pass",
  "duration_ms": 342,
  "started_at": "2026-05-31T20:30:00Z",
  "completed_at": "2026-05-31T20:30:00.342Z",
  "metrics": {
    "ru_consumed": 5.2,
    "retries": 0,
    "regions_contacted": ["West US"],
    "connections_opened": 1,
    "metadata_calls": {
      "get_database_account": 1,
      "read_collection": 0,
      "read_pk_ranges": 1
    }
  },
  "assertions": [
    { "name": "pkrange_not_fetched_at_init", "passed": true, "detail": null },
    { "name": "pkrange_fetched_on_first_query", "passed": true, "detail": "1 call observed" },
    { "name": "pkrange_cached_for_subsequent", "passed": true, "detail": "0 additional calls" }
  ],
  "diagnostics": "... SDK-native diagnostics string ...",
  "error": null,
  "logs": [
    "[INFO] Client created in 120ms",
    "[INFO] Query executed, 5.2 RU consumed",
    "[DEBUG] PKRange cache hit for partition 0-AA"
  ],
  "wire_trace": [
    { "timestamp": "...", "method": "GET", "url": "/dbs/testdb/colls/testcol/pkranges", "status": 200 }
  ]
}
```

### 5. Fault Injection Layer

#### Transport-Level Faults (Toxiproxy)

Toxiproxy sits between SDK runners and the CosmosDB backend:

```
SDK Runner → localhost:18081 (Toxiproxy) → localhost:8081 (Emulator)
                                         → *.documents.azure.com (Live)
```

Managed via Toxiproxy HTTP API from the orchestrator.

#### Protocol-Level Faults (Custom Proxy)

For CosmosDB-specific response injection (429, 410 Gone, partition split simulation), a lightweight **mitmproxy plugin** or custom Go proxy intercepts and modifies responses:

```python
# mitmproxy addon for CosmosDB fault injection
class CosmosDBFaultInjector:
    def __init__(self):
        self.profile = None
        self.request_count = {}

    def response(self, flow):
        if self.profile and self.should_inject(flow):
            flow.response = self.build_fault_response(flow)
```

#### Fault Profile Definitions

```yaml
# proxy/profiles/throttle-429.yaml
name: throttle-429
description: "Return 429 for first 3 write requests, then passthrough"
rules:
  - match:
      method: POST
      path_pattern: "/dbs/.*/colls/.*/docs"
    action: inject_response
    response:
      status_code: 429
      headers:
        x-ms-retry-after-ms: "100"
        x-ms-substatus: "3200"
      body: '{"code":"TooManyRequests","message":"Rate limit exceeded"}'
    max_injections: 3
    then: passthrough
```

```yaml
# proxy/profiles/partition-split-gone.yaml
name: partition-split-gone
description: "Return 410 Gone with PartitionKeyRangeGone on first request to a partition"
rules:
  - match:
      method: POST
      path_pattern: "/dbs/.*/colls/.*/docs"
      header_contains:
        x-ms-documentdb-partitionkeyrangeid: "0"
    action: inject_response
    response:
      status_code: 410
      headers:
        x-ms-substatus: "1002"
      body: '{"code":"Gone","message":"Partition key range is gone"}'
    max_injections: 1
    then: passthrough
```

```yaml
# proxy/profiles/region-down.yaml
name: region-down
description: "Simulate region failure by dropping connections to specific endpoint"
toxiproxy:
  toxic: "timeout"
  attributes:
    timeout: 30000
  target_upstream: "cosmosdb-westus"
```

### 6. Scenario Spec Format (YAML)

```yaml
# specs/phase01-bootstrap/036-lazy-pkrange-fetch.yaml
id: "36"
phase: 1
section: "1.5"
title: "First query triggers lazy ReadPartitionKeyRanges fetch"
description: |
  Validates that partition key range metadata is fetched lazily on first
  query operation, not eagerly during client initialization.

tags: [bootstrap, lazy-init, cache, pkrange, metadata]

preconditions:
  container:
    exists: true
    min_physical_partitions: 5
    partition_key: "/partitionKey"

steps:
  - id: create_client
    action: create_client
    params:
      connection_mode: direct
      preferred_regions: ["${config.region}"]
    capture:
      - network_calls_after_init

  - id: assert_no_pkrange_at_init
    action: assert
    condition:
      type: no_network_call
      target: "pkranges"
      since: create_client
    on_fail: "PKRange should NOT be fetched during client initialization"

  - id: first_query
    action: query_items
    params:
      query: "SELECT * FROM c WHERE c.status = 'active'"
      cross_partition: true
      max_item_count: 10

  - id: assert_pkrange_fetched
    action: assert
    condition:
      type: network_call_made
      target: "pkranges"
      since: create_client
      count: 1
    on_fail: "PKRange should be fetched on first cross-partition query"

  - id: second_query
    action: query_items
    params:
      query: "SELECT TOP 5 FROM c"
      cross_partition: true

  - id: assert_pkrange_cached
    action: assert
    condition:
      type: no_network_call
      target: "pkranges"
      since: first_query
    on_fail: "PKRange should be served from cache on subsequent queries"

expected_outcome:
  status: pass
  key_assertions:
    - pkrange_not_fetched_at_init
    - pkrange_fetched_on_first_query
    - pkrange_cached_for_subsequent

fault_profile: null

applicable_sdks:
  java: { min_version: "4.50.0" }
  python: { min_version: "4.5.0" }

notes: |
  In Java SDK, PKRange cache lives in RxPartitionKeyRangeCache (initialized
  during init() but not populated until first routing request).
  In Python SDK, SmartRoutingMapProvider is created eagerly but
  _ReadPartitionKeyRanges is called lazily via get_overlapping_ranges().
```

### Workflow Scenario Spec Format

Workflow scenarios chain multiple steps with state passing between them:

```yaml
# specs/workflows/w-001-app-lifecycle.yaml
id: "w-001"
type: workflow  # Distinguishes from "unit" (single-operation) tests
phase: "workflows"
title: "Basic app lifecycle: init → create → seed → query → cleanup"
tags: [workflow, e2e, happy-path]

steps:
  - id: init_client
    action: create_client
    params:
      connection_mode: direct

  - id: create_db
    action: create_database
    params:
      id: "workflow-test-db-${run.id}"

  - id: create_container
    action: create_container
    params:
      database: "${create_db.id}"          # Reference prior step output
      id: "orders"
      partition_key: "/customerId"

  - id: seed_data
    action: bulk_create_items
    params:
      database: "${create_db.id}"
      container: "orders"
      count: 100
      template:
        id: "${uuid}"
        customerId: "${random.choice(['cust-1','cust-2','cust-3'])}"
        total: "${random.float(10.0, 500.0)}"

  - id: query_paginated
    action: query_items_all_pages
    params:
      database: "${create_db.id}"
      container: "orders"
      query: "SELECT * FROM c ORDER BY c.total DESC"
      cross_partition: true
      max_item_count: 10
    assert:
      - total_items: 100
      - pages_fetched: ">= 10"

  - id: cleanup
    action: delete_database
    params:
      id: "${create_db.id}"
    on_fail: warn  # Don't fail scenario if cleanup fails

expected_outcome:
  status: pass
metrics_to_capture: [total_duration_ms, ru_consumed_total, retries_total]
```

#### Extended Constructs for Workflows

```yaml
# Parallel execution
- id: mixed_workload
  action: parallel
  branches:
    - steps: [{action: loop, count: 50, steps: [{action: read_item, ...}]}]
    - steps: [{action: loop, count: 10, steps: [{action: query_items, ...}]}]
  join: all  # Wait for all branches to complete

# Loops (count-based or duration-based)
- id: query_loop
  action: loop
  count: 10         # or: duration: "30s"
  steps:
    - action: query_items
      params: {...}

# Mid-workflow fault injection
- id: inject_failure
  action: activate_fault
  profile: "partition-split-gone"
  trigger: after_step("query_page_3")  # Event-driven activation

# Timing/resource assertions
- id: check_latency
  action: assert
  condition:
    type: latency_percentile
    percentile: 99
    max_ms: 50
    scope: "query_loop"

- id: check_no_leak
  action: assert
  condition:
    type: resource_stable
    metric: "connection_pool_size"
    tolerance_pct: 10
    over: "steady_state_loop"
```

---

## Data Model

### Database Schema

```sql
-- SDK Registry
CREATE TABLE sdks (
    name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    runner_path TEXT NOT NULL
);

CREATE TABLE sdk_versions (
    sdk_name TEXT REFERENCES sdks(name),
    version TEXT NOT NULL,
    source TEXT NOT NULL,            -- "maven", "pypi", "local"
    path TEXT,                       -- local path if source=local
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sdk_name, version)
);

-- Test Runs
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    config JSONB NOT NULL,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    summary JSONB
);

CREATE TABLE run_results (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id),
    scenario_id TEXT NOT NULL,
    sdk_name TEXT NOT NULL,
    sdk_version TEXT NOT NULL,
    status TEXT NOT NULL,            -- pass, fail, error, skip
    duration_ms INTEGER,
    metrics JSONB,
    assertions JSONB,
    diagnostics TEXT,
    error TEXT,
    logs JSONB,
    wire_trace JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_results_scenario ON run_results(scenario_id);
CREATE INDEX idx_results_sdk ON run_results(sdk_name, sdk_version);
CREATE INDEX idx_results_run ON run_results(run_id);
CREATE INDEX idx_results_status ON run_results(status);
```

---

## SDK Version Selection — Detailed Flow

### Version Resolution Strategy

```
User selects "Java 4.63.0" in Config Panel
        │
        ▼
Orchestrator checks sdk_versions table
        │
        ├─ source = "maven" → Ensure JAR is downloaded to cache/java/4.63.0/
        │                      (download from Maven Central if not present)
        │
        ├─ source = "local" → Use path directly (e.g., ../azure-sdk-for-java/sdk/cosmos/...)
        │
        └─ source = "pypi"  → Ensure venv exists at cache/python/4.9.0/
                               (create venv + pip install azure-cosmos==4.9.0 if not present)
        │
        ▼
Runner is invoked with classpath/venv pointing to correct version
```

### Adding a New SDK Version

```bash
# Via API
curl -X POST http://localhost:3000/api/sdks/java/versions \
  -d '{"version": "4.64.0-beta.1", "source": "maven"}'

# Register local build
curl -X POST http://localhost:3000/api/sdks/java/versions \
  -d '{"version": "local-snapshot", "source": "local", "path": "/path/to/azure-sdk-for-java/sdk/cosmos"}'
```

### Version Comparison Mode

The UI supports running the same scenario against **multiple versions of the same SDK** to detect regressions:

```
┌─ Scenario 36: Lazy PKRange Fetch ─────────────────────────────────┐
│                                                                    │
│  Java 4.63.0  │  Java 4.62.0  │  Python 4.9.0  │  Python 4.8.0  │
│     ✅ 342ms  │     ✅ 338ms  │     ✅ 412ms   │     ❌ FAIL    │
│     5.2 RU    │     5.2 RU    │     5.8 RU     │     Error: ...  │
│                                                                    │
│  [Show Diff: 4.63.0 vs 4.62.0]  [Show Diff: 4.9.0 vs 4.8.0]     │
└────────────────────────────────────────────────────────────────────┘
```

---

## End-to-End Execution Flow

```
User clicks [▶] on scenario 36, Java column
        │
        ▼
Frontend → POST /api/run { scenarios: ["36"], sdks: [{"name":"java","version":"4.63.0"}] }
        │
        ▼
Orchestrator:
  1. Reads spec for scenario 36
  2. Checks preconditions (container exists with 5+ partitions)
  3. Activates fault profile (none for this scenario)
  4. Resolves Java runner for version 4.63.0 (cached JAR or local build)
  5. Spawns: java -cp runner.jar:cosmos-4.63.0.jar ... --scenario 36.json
  6. Streams status → WebSocket → Frontend shows ⏳
        │
        ▼
Java Runner:
  1. Creates CosmosAsyncClient (with network interceptor/diagnostics enabled)
  2. Asserts: no PKRange call in diagnostics yet ✓
  3. Runs cross-partition query
  4. Asserts: PKRange call visible in diagnostics ✓
  5. Runs second query
  6. Asserts: no new PKRange call ✓
  7. Outputs result JSON to stdout
        │
        ▼
Orchestrator:
  1. Collects result JSON
  2. Stores in database
  3. Pushes final status → WebSocket → Frontend shows ✅
        │
        ▼
Frontend: Cell turns green, click to expand metrics/diagnostics
```

---

## Directory Structure

```
cosmosdb-cross-sdk-tests/
├── README.md
├── ARCHITECTURE.md              ← This document
├── docker-compose.yaml          # Full stack: orchestrator + proxy + emulator
│
├── specs/                       # Shared YAML scenario definitions
│   ├── schema.yaml              # JSON Schema for scenario format validation
│   ├── phase01-bootstrap/
│   │   ├── 001-master-key-auth.yaml
│   │   ├── 002-aad-auth.yaml
│   │   ├── ...
│   │   └── 070-throughput-control-groups.yaml
│   ├── phase02-control-plane/
│   ├── phase03-crud/
│   ├── phase04-queries/
│   ├── phase05-change-feed/
│   ├── phase06-bulk-batch/
│   ├── phase07-sprocs-triggers/
│   ├── phase08-users-permissions/
│   ├── phase09-retry-errors/
│   ├── phase10-multi-region/
│   ├── phase11-consistency/
│   ├── phase12-diagnostics/
│   ├── phase13-throughput/
│   ├── phase14-performance/
│   ├── phase15-lifecycle/
│   └── phase16-edge-cases/
│
├── orchestrator/                # Backend API
│   ├── app/
│   │   ├── main.py              # FastAPI application entry point
│   │   ├── routers/
│   │   │   ├── scenarios.py     # Scenario CRUD endpoints
│   │   │   ├── runs.py          # Test execution endpoints
│   │   │   ├── sdks.py          # SDK registry & version management
│   │   │   └── proxy.py         # Fault injection control
│   │   ├── services/
│   │   │   ├── scenario_loader.py    # Parse YAML specs into catalog
│   │   │   ├── runner_dispatcher.py  # Spawn & manage SDK runners
│   │   │   ├── version_manager.py    # Download/resolve SDK versions
│   │   │   ├── proxy_manager.py      # Toxiproxy API integration
│   │   │   └── result_store.py       # Database operations
│   │   ├── models/
│   │   │   ├── scenario.py
│   │   │   ├── run.py
│   │   │   └── result.py
│   │   └── websocket/
│   │       └── run_stream.py    # WebSocket handler for live updates
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend/                    # Next.js web app
│   ├── app/
│   │   ├── page.tsx             # Main matrix view
│   │   ├── layout.tsx
│   │   └── runs/[id]/page.tsx   # Run detail view
│   ├── components/
│   │   ├── ScenarioGrid.tsx     # Virtualized scenario table
│   │   ├── ConfigPanel.tsx      # Backend, SDK version, mode selection
│   │   ├── SdkVersionSelector.tsx  # Version dropdown with local/released
│   │   ├── RunControls.tsx      # Play/stop/cancel buttons
│   │   ├── ResultDetail.tsx     # Expanded result view
│   │   ├── CrossSdkDiff.tsx     # Side-by-side comparison
│   │   ├── HistoryChart.tsx     # Trend visualization
│   │   └── StatusBadge.tsx      # ✅ ❌ ⏳ ⚠️ badges
│   ├── hooks/
│   │   ├── useWebSocket.ts      # Live update subscription
│   │   ├── useScenarios.ts      # Scenario data fetching
│   │   └── useRunState.ts       # Run management state
│   ├── package.json
│   ├── tailwind.config.ts
│   └── Dockerfile
│
├── harness/                     # Language-specific test runners
│   ├── java/
│   │   ├── pom.xml
│   │   ├── src/main/java/com/azure/cosmos/testrunner/
│   │   │   ├── Main.java             # CLI entry point
│   │   │   ├── ScenarioExecutor.java # Step-by-step execution engine
│   │   │   ├── StepHandlers.java     # Map actions → SDK calls
│   │   │   ├── AssertionEvaluator.java  # Evaluate assert conditions
│   │   │   ├── NetworkInterceptor.java  # Capture wire-level calls
│   │   │   └── ResultReporter.java      # Output JSON result
│   │   └── Dockerfile
│   └── python/
│       ├── cosmos_test_runner/
│       │   ├── __main__.py           # CLI entry point
│       │   ├── executor.py           # Step-by-step execution engine
│       │   ├── step_handlers.py      # Map actions → SDK calls
│       │   ├── assertions.py         # Evaluate assert conditions
│       │   ├── network_interceptor.py  # Capture wire-level calls
│       │   └── result_reporter.py      # Output JSON result
│       ├── requirements.txt
│       └── Dockerfile
│
├── proxy/                       # Fault injection infrastructure
│   ├── profiles/
│   │   ├── throttle-429.yaml
│   │   ├── partition-split-gone.yaml
│   │   ├── region-down.yaml
│   │   ├── session-token-strip.yaml
│   │   ├── connection-reset.yaml
│   │   └── slow-response.yaml
│   ├── cosmos_proxy_plugin.py   # mitmproxy addon for CosmosDB-specific faults
│   └── docker-compose.proxy.yaml
│
├── config/
│   ├── default.yaml             # Default configuration
│   ├── emulator.yaml            # Emulator-specific config
│   ├── live.yaml                # Live account config (secrets via env vars)
│   └── sdk-registry.yaml        # Available SDKs and their versions
│
└── scripts/
    ├── setup.sh                 # One-command local setup
    ├── add-sdk-version.sh       # Register new SDK version
    └── run-matrix.sh            # CLI-based full matrix run (for CI)
```

---

## Deployment Modes

### Local Development
```bash
docker-compose up
# Access dashboard at http://localhost:3000
# Orchestrator API at http://localhost:8000
# Toxiproxy admin at http://localhost:8474
```

### CI/CD Integration (Headless)
```bash
./scripts/run-matrix.sh \
  --sdks java:4.63.0,python:4.9.0 \
  --backend emulator \
  --output results/ \
  --format junit,json \
  --fail-on-regression
```

### Shared Team Lab
- Deploy to Azure Container Apps or AKS
- Persistent result database (PostgreSQL)
- Multiple engineers trigger runs from shared dashboard
- Webhook/Slack notifications on regressions

---

## Phased Implementation Plan

| Phase | Deliverable | Effort |
|-------|-------------|--------|
| **P0** | YAML spec schema + 10–15 sample scenarios (Phase 1 bootstrap) | 2 days |
| **P1** | Orchestrator API: scenario loader, run dispatch, WebSocket, SQLite store | 4 days |
| **P2** | Java test runner harness (CLI, reads spec JSON, executes via SDK, outputs result JSON) | 4 days |
| **P3** | Python test runner harness (same contract) | 3 days |
| **P4** | Web frontend: scenario grid, config panel with SDK version selector, play buttons, live status | 5 days |
| **P5** | SDK version management (Maven/PyPI download, venv isolation, local-snapshot support) | 3 days |
| **P6** | Fault injection: Toxiproxy integration + custom CosmosDB proxy plugin + profile system | 4 days |
| **P7** | Result history, cross-SDK diff view, version comparison, trend charts | 3 days |
| **P8** | Full 280-scenario YAML spec coverage | Ongoing |
| **P9** | CI integration (GitHub Actions workflow, headless mode, regression gating) | 2 days |
| **P10** | Docker Compose for one-command startup; documentation | 2 days |

**Total estimated effort: ~32 days for core platform + ongoing spec authoring**

---

## Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Orchestrator language | Python (FastAPI) | Matches Python SDK; excellent async/WebSocket support; fast development |
| Frontend framework | Next.js + React | Rich ecosystem for data tables, real-time updates, good DX |
| Runner isolation | Subprocess + Docker | Clean version isolation; no classpath/venv pollution between versions |
| Fault injection | Toxiproxy + custom plugin | Toxiproxy is battle-tested for TCP faults; custom plugin adds CosmosDB-specific HTTP response injection |
| Spec format | YAML | Human-readable, supports comments, multi-line descriptions; validated against JSON Schema |
| Result storage | SQLite (local) / PostgreSQL (shared) | Simple for dev, scales for team use |
| Version management | Download + cache | Maven JARs and pip packages cached locally; local builds supported via path reference |
| Communication | REST + WebSocket | REST for CRUD; WebSocket for real-time streaming during test runs |
| SDK version selector | Config panel dropdown | Quick switching between released and local-dev versions without restart |

---

## Option B: On-Demand Azure Pipeline (Zero-Setup Path)

The web dashboard provides rich interactivity but requires hosting. For a **zero-setup, low-maintenance** alternative, the same test infrastructure runs as an on-demand Azure DevOps Pipeline that any team member can trigger without local setup.


### Shared Components

Both approaches consume the same artifacts:

```
┌─────────────────────────────────────────────────────┐
│              Shared Components                        │
│                                                     │
│  • specs/*.yaml          (scenario definitions)     │
│  • harness/java/         (Java runner CLI)          │
│  • harness/python/       (Python runner CLI)        │
│  • proxy/profiles/       (fault injection configs)  │
│  • scripts/compare.py    (comparison logic)         │
└─────────────────┬───────────────────┬───────────────┘
                  │                   │
      ┌───────────▼──────┐   ┌───────▼────────────┐
      │  Azure Pipeline  │   │  Web Dashboard     │
      │  (zero-setup,    │   │  (rich UI,         │
      │   CI/CD gating,  │   │   interactive,     │
      │   team-wide)     │   │   real-time)       │
      └──────────────────┘   └────────────────────┘
```

### Multi Scenario

 # specs/workflows/001-app-lifecycle-basic.yaml
 ```
 id: "w-001"
 type: workflow  # vs "unit" for single-operation tests
 phase: "workflows"
 title: "Basic app lifecycle: init → create DB/container → seed → query → cleanup"
 description: |
   Simulates a typical application startup and steady-state operation.
   Validates that the full chain works end-to-end with no metadata 
   stalls, cache misses causing failures, or leaked state.
 
 tags: [workflow, e2e, happy-path, crud, query]
 
 steps:
   - id: init_client
     action: create_client
     params:
       connection_mode: direct
       preferred_regions: ["${config.region}"]
 
   - id: create_db
     action: create_database
     params:
       id: "workflow-test-db-${run.id}"
       throughput: 400
 
   - id: create_container
     action: create_container
     params:
       database: "${create_db.id}"
       id: "orders"
       partition_key: "/customerId"
       indexing_policy:
         included_paths: ["/orderDate/?", "/total/?"]
         excluded_paths: ["/*"]
 
   - id: seed_data
     action: bulk_create_items
     params:
       database: "${create_db.id}"
       container: "orders"
       count: 100
       template:
         id: "${uuid}"
         customerId: "${random.choice(['cust-1','cust-2','cust-3','cust-4','cust-5'])}"
         orderDate: "${random.date(2025-01-01, 2026-06-01)}"
         total: "${random.float(10.0, 500.0)}"
         status: "${random.choice(['pending','shipped','delivered'])}"
 ```

### Pipeline Definition

```yaml
# azure-pipelines-cosmos-sdk-tests.yml

trigger: none  # On-demand only

parameters:
  - name: sdkVersionJava
    displayName: "Java SDK Version"
    type: string
    default: "4.63.0"
    values:
      - "4.63.0"
      - "4.62.0"
      - "4.61.0"
      - "local-snapshot"

  - name: sdkVersionPython
    displayName: "Python SDK Version"
    type: string
    default: "4.9.0"
    values:
      - "4.9.0"
      - "4.8.0"
      - "4.7.0"
      - "local-dev"

  - name: scenarios
    displayName: "Scenarios to run"
    type: string
    default: "all"
    values:
      - "all"
      - "phase1-bootstrap"
      - "phase2-control-plane"
      - "phase3-crud"
      - "phase4-queries"
      - "phase5-change-feed"
      - "phase6-bulk-batch"
      - "phase9-retry-errors"
      - "phase10-multi-region"
      - "custom"

  - name: customScenarios
    displayName: "Custom scenario IDs (comma-separated, if 'custom' above)"
    type: string
    default: ""

  - name: backend
    displayName: "Backend target"
    type: string
    default: "emulator"
    values:
      - "emulator"
      - "live-test-account"

  - name: connectionMode
    displayName: "Connection mode"
    type: string
    default: "direct"
    values:
      - "direct"
      - "gateway"
      - "both"

  - name: enableFaultInjection
    displayName: "Enable fault injection scenarios"
    type: boolean
    default: true

variables:
  - group: cosmos-sdk-test-secrets
  - name: COSMOS_EMULATOR_ENDPOINT
    value: "https://localhost:8081"

stages:
  - stage: Setup
    displayName: "Environment Setup"
    jobs:
      - job: Infrastructure
        pool:
          vmImage: "ubuntu-latest"
        steps:
          - script: |
              # Start CosmosDB Emulator (Linux container)
              docker run -d --name cosmos-emulator \
                -p 8081:8081 -p 10251-10254:10251-10254 \
                -e AZURE_COSMOS_EMULATOR_PARTITION_COUNT=50 \
                -e AZURE_COSMOS_EMULATOR_ENABLE_DATA_PERSISTENCE=false \
                mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:latest
              
              # Start Toxiproxy
              docker run -d --name toxiproxy \
                --network host \
                -p 8474:8474 -p 18081:18081 \
                ghcr.io/shopify/toxiproxy:latest
              
              # Wait for emulator readiness
              ./scripts/wait-for-emulator.sh
              
              # Configure proxy upstream
              ./scripts/configure-proxy.sh ${{ parameters.backend }}
            displayName: "Start Emulator + Proxy"
            condition: eq('${{ parameters.backend }}', 'emulator')

  - stage: RunTests
    displayName: "Execute Test Matrix"
    dependsOn: Setup
    jobs:
      - job: JavaTests
        displayName: "Java SDK ${{ parameters.sdkVersionJava }}"
        pool:
          vmImage: "ubuntu-latest"
        steps:
          - task: JavaToolInstaller@0
            inputs:
              versionSpec: "17"

          - script: |
              if [ "${{ parameters.sdkVersionJava }}" = "local-snapshot" ]; then
                cd $(Build.SourcesDirectory)/azure-sdk-for-java/sdk/cosmos/azure-cosmos
                mvn install -DskipTests -q
              else
                mvn dependency:copy \
                  -Dartifact=com.azure:azure-cosmos:${{ parameters.sdkVersionJava }} \
                  -DoutputDirectory=./libs/
              fi
            displayName: "Resolve Java SDK"

          - script: |
              java -jar harness/java/cosmos-test-runner.jar \
                --specs specs/ \
                --filter "${{ parameters.scenarios }}" \
                --custom-ids "${{ parameters.customScenarios }}" \
                --sdk-version "${{ parameters.sdkVersionJava }}" \
                --backend "${{ parameters.backend }}" \
                --connection-mode "${{ parameters.connectionMode }}" \
                --proxy-endpoint "http://localhost:8474" \
                --fault-injection ${{ parameters.enableFaultInjection }} \
                --output $(Build.ArtifactStagingDirectory)/java/ \
                --format junit,json
            displayName: "Run Java Scenarios"

          - task: PublishTestResults@2
            inputs:
              testResultsFormat: "JUnit"
              testResultsFiles: "$(Build.ArtifactStagingDirectory)/java/**/*.xml"
              testRunTitle: "CosmosDB Java SDK ${{ parameters.sdkVersionJava }}"
            condition: always()

          - publish: $(Build.ArtifactStagingDirectory)/java/
            artifact: "results-java-${{ parameters.sdkVersionJava }}"
            condition: always()

      - job: PythonTests
        displayName: "Python SDK ${{ parameters.sdkVersionPython }}"
        pool:
          vmImage: "ubuntu-latest"
        steps:
          - task: UsePythonVersion@0
            inputs:
              versionSpec: "3.11"

          - script: |
              python -m venv .venv && source .venv/bin/activate
              if [ "${{ parameters.sdkVersionPython }}" = "local-dev" ]; then
                pip install -e ./azure-sdk-for-python/sdk/cosmos/azure-cosmos/
              else
                pip install azure-cosmos==${{ parameters.sdkVersionPython }}
              fi
              pip install -r harness/python/requirements.txt
            displayName: "Resolve Python SDK"

          - script: |
              source .venv/bin/activate
              python -m cosmos_test_runner \
                --specs specs/ \
                --filter "${{ parameters.scenarios }}" \
                --custom-ids "${{ parameters.customScenarios }}" \
                --sdk-version "${{ parameters.sdkVersionPython }}" \
                --backend "${{ parameters.backend }}" \
                --connection-mode "${{ parameters.connectionMode }}" \
                --proxy-endpoint "http://localhost:8474" \
                --fault-injection ${{ parameters.enableFaultInjection }} \
                --output $(Build.ArtifactStagingDirectory)/python/ \
                --format junit,json
            displayName: "Run Python Scenarios"

          - task: PublishTestResults@2
            inputs:
              testResultsFormat: "JUnit"
              testResultsFiles: "$(Build.ArtifactStagingDirectory)/python/**/*.xml"
              testRunTitle: "CosmosDB Python SDK ${{ parameters.sdkVersionPython }}"
            condition: always()

          - publish: $(Build.ArtifactStagingDirectory)/python/
            artifact: "results-python-${{ parameters.sdkVersionPython }}"
            condition: always()

  - stage: Compare
    displayName: "Cross-SDK Comparison"
    dependsOn: RunTests
    condition: always()
    jobs:
      - job: GenerateReport
        pool:
          vmImage: "ubuntu-latest"
        steps:
          - download: current
            patterns: "results-*/**/*.json"

          - script: |
              python scripts/compare-results.py \
                --java $(Pipeline.Workspace)/results-java-${{ parameters.sdkVersionJava }}/ \
                --python $(Pipeline.Workspace)/results-python-${{ parameters.sdkVersionPython }}/ \
                --output $(Build.ArtifactStagingDirectory)/comparison/
            displayName: "Generate Comparison Report"

          - task: PublishBuildArtifacts@1
            inputs:
              pathtoPublish: "$(Build.ArtifactStagingDirectory)/comparison/"
              artifactName: "comparison-report"

          # Post markdown summary to pipeline
          - script: |
              cat $(Build.ArtifactStagingDirectory)/comparison/summary.md \
                >> $(System.DefaultWorkingDirectory)/pipeline-summary.md
              echo "##vso[task.uploadsummary]$(System.DefaultWorkingDirectory)/pipeline-summary.md"
            displayName: "Post Summary"
```

### How Users Trigger It

#### Option 1: Azure DevOps UI (Simplest — Zero Setup)
```
Pipelines → "CosmosDB SDK Tests" → "Run pipeline"
  → Select Java version: [4.63.0 ▼]
  → Select Python version: [4.9.0 ▼]
  → Select scenarios: [phase1-bootstrap ▼]
  → Select backend: [emulator ▼]
  → Click "Run"
```

#### Option 2: Azure CLI (Power Users)
```bash
az pipelines run \
  --name "CosmosDB SDK Tests" \
  --parameters \
    sdkVersionJava=4.63.0 \
    sdkVersionPython=4.9.0 \
    scenarios=phase1-bootstrap \
    backend=emulator \
    connectionMode=direct
```

#### Option 3: PR Comment Trigger (Release Validation)
```
# Comment on a PR:
/cosmos-sdk-test --scenarios all --sdks java:local-snapshot,python:local-dev

# Bot responds:
🚀 CosmosDB SDK Test triggered: Run #342
   Java: local-snapshot | Python: local-dev | Scenarios: all | Backend: emulator
   Follow progress: https://dev.azure.com/org/project/_build/results?buildId=342
```

Implemented via a lightweight Azure Function or pipeline trigger with comment parsing.

#### Option 4: Scheduled Nightly Regression
```yaml
schedules:
  - cron: "0 2 * * *"    # 2 AM daily
    displayName: "Nightly regression"
    branches: { include: [main] }
    always: true
```

#### Option 5: Release Gate
```yaml
# In release pipeline, add as a gate:
- gate: CosmosDB SDK Validation
  pipeline: "CosmosDB SDK Tests"
  parameters:
    scenarios: all
    sdkVersionJava: $(release.javaVersion)
    sdkVersionPython: $(release.pythonVersion)
  successCriteria: "all scenarios pass"
```

### Results Experience in Azure DevOps

The native **Tests** tab provides:
- Pass/fail breakdown per SDK
- Flaky test detection (built-in)
- Historical trends (Test Analytics)
- Drill-down into individual test results with logs

The **Comparison Report** artifact provides:
```markdown
# Cross-SDK Test Comparison Report
Run: #342 | Date: 2026-06-03 | Backend: Emulator

## Summary
| SDK | Version | Passed | Failed | Skipped | Duration |
|-----|---------|--------|--------|---------|----------|
| Java | 4.63.0 | 68 | 2 | 0 | 3m 45s |
| Python | 4.9.0 | 65 | 3 | 2 | 4m 12s |

## Behavioral Divergences
| # | Scenario | Java | Python | Notes |
|---|----------|------|--------|-------|
| 39 | PKRange split retry | ❌ 3 retries, failed | ✅ 2 retries, passed | Possible stale cache in Java |
| 54 | RegionScoped session | ✅ passed | ❌ not implemented | Feature gap |
| 38 | Multi-page PKRange | ✅ 342ms | ⚠️ 1200ms | Python 3.5x slower |

## Regression Detection
No regressions vs previous run #341.
```

### Maintenance Model

| What | Maintenance | Frequency |
|------|-------------|-----------|
| Pipeline YAML | Add new SDK version to `values` list | Per SDK release (~monthly) |
| Scenario specs | Add new YAML files for new scenarios | As needed |
| Runner harnesses | Update when new step actions needed | Rare |
| Fault profiles | Add profiles for new fault types | Rare |
| Infrastructure | Emulator image pin bump | Quarterly |

**Net maintenance: ~1 line change per SDK release. New scenarios are just new YAML files.**

### Directory Addition for Pipeline Support

```
cosmosdb-cross-sdk-tests/
├── ...existing structure...
├── pipelines/
│   ├── azure-pipelines-cosmos-sdk-tests.yml   # Main on-demand pipeline
│   ├── azure-pipelines-nightly.yml            # Nightly regression schedule
│   └── templates/
│       ├── setup-emulator.yml                 # Reusable emulator setup
│       ├── run-java.yml                       # Java runner template
│       ├── run-python.yml                     # Python runner template
│       └── compare-results.yml                # Comparison stage template
└── scripts/
    ├── wait-for-emulator.sh
    ├── configure-proxy.sh
    ├── compare-results.py
    └── post-summary.py
```
