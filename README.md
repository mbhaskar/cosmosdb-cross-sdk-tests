# CosmosDB Cross-SDK Test Runner — MVP

A working vertical slice of the platform described in
[`ARCHITECTURE.md`](./ARCHITECTURE.md). It runs the **same scenario against
multiple CosmosDB SDKs** (Python + Java) and shows the results in a matrix
dashboard, with a cross-SDK comparison report.

> Full test-scenario catalog: [`plan.md`](./plan.md) (280 scenarios across 16
> phases). This MVP ships 10 of them end-to-end.

---

## What's in the MVP

| Piece | Status | Notes |
|-------|--------|-------|
| YAML scenario spec format | ✅ | [`specs/schema.yaml`](./specs/schema.yaml) + 10 scenarios |
| Python runner | ✅ | mock + real `azure-cosmos` backend |
| Java runner | ✅ | mock + real `azure-cosmos` backend (shaded jar) |
| Orchestrator API (FastAPI) | ✅ | load specs, dispatch runners, SQLite store |
| Dashboard | ✅ | single-file HTML matrix, run buttons, live polling, drill-down |
| Cross-SDK compare report | ✅ | [`scripts/compare.py`](./scripts/compare.py) |
| Backends | ✅ | `mock` (default, no infra), `emulator`, `live` |

### Deliberately deferred (post-MVP)
Fault injection (Toxiproxy/proxy), WebSocket streaming, SDK version
download/management, history charts, .NET runner, full 280-scenario coverage.

---

## The `mock` backend

The MVP defaults to an **in-memory fake Cosmos DB** so the whole pipeline runs
with no emulator or account. Both runners implement identical mock semantics
(409 conflict, 404 not-found, upsert, lazy `ReadCollection` / `ReadPartitionKeyRanges`
metadata accounting), which is what lets the cross-SDK matrix work anywhere.

### Single source of truth: `specs/mock-profile.json`

The mock's behavior is **not** hardcoded in each runner. All of it —  RU
charges, status codes, and the per-operation branching rules (when to return
409 vs 404, when to charge, when to count a metadata call) — lives in one
declarative file, [`specs/mock-profile.json`](./specs/mock-profile.json). Each
runner ships only a tiny interpreter + in-memory store and executes that file's
step machine. Edit the profile once and Python **and** Java change together, so
the SDKs can't drift. A parity check (status + full metrics, byte-for-byte
identical across both runners) guards this.

Each operation is an ordered list of steps:

```json
"create_item": [
  { "guard": ["container_missing"],
    "return": { "ok": false, "status": "not_found", "message": "Container not found" } },
  { "effect": "touch_collection" },
  { "effect": "assign_id" },
  { "guard": ["item_exists"],
    "return": { "ok": false, "status": "conflict", "message": "...", "charge": "read" } },
  { "effect": "put_item" },
  { "return": { "ok": true, "status": "created", "body": "item", "charge": "create" } }
]
```

> Design note: only **values and rules** are externalized. The interpreter
> primitives (predicates like `item_exists`, effects like `put_item`) stay in
> code — small, structurally identical in both runners, and not where drift
> happens. The profile is JSON so Java parses it with its existing Jackson
> dependency (no new Maven dep) and Python with the stdlib.

The orchestrator loads the profile once and injects it into each job; standalone
CLI runs fall back to reading `specs/mock-profile.json` from disk.

The **real SDK path** (`backend: emulator` or `live`) is fully implemented and
ready — it just needs a reachable endpoint. Bootstrap scenarios that assert on
mock-only metadata counters are tagged `backends: [mock]` and are auto-skipped
on other backends.

---

## Configuring live / emulator backends

The `mock` backend needs no configuration. For `emulator` or `live`, the
orchestrator resolves an **endpoint** and **key** using this precedence
(highest first):

1. **Request body** — fields on `config` in the `POST /api/runs` payload, or the
   **Endpoint / Key** inputs that appear in the dashboard when you pick a
   non-mock backend:
   ```json
   { "config": { "backend": "live",
                 "endpoint": "https://<acct>.documents.azure.com:443/",
                 "key": "<primary-key>" } }
   ```
2. **Environment variables** — `COSMOS_ENDPOINT` and `COSMOS_KEY` (applied to the
   `live` backend):
   ```bash
   export COSMOS_ENDPOINT="https://<acct>.documents.azure.com:443/"
   export COSMOS_KEY="<primary-key>"
   ```
3. **`config/default.yaml`** — per-backend blocks. The `emulator` block already
   contains the well-known emulator endpoint + public key, so the emulator
   "just works" when it is running. The `live` block reads `${COSMOS_ENDPOINT}` /
   `${COSMOS_KEY}` by default but you can hardcode values instead.

If no endpoint/key can be resolved for a non-mock backend, the API returns a
`400` with a message naming all three options. **Keys are never stored in
plaintext**: the copy persisted to `results.db` (and returned by
`GET /api/runs/<id>`) has the key replaced with `***redacted***`.

### Metrics fidelity (what's real vs. assumed)

For the `live` / `emulator` backends the metrics block is now populated from real
SDK telemetry where the SDK exposes it:

| Metric | Source |
| --- | --- |
| `ru_consumed` | **Real** — server-reported. Java sums `getRequestCharge()` across every op (and query page); Python accumulates the `x-ms-request-charge` response header. Failed requests (e.g. a 409 conflict) are charged too. |
| `retries` | **Real on Java** — parsed from the `retryCount` field of `CosmosDiagnostics`. **0 on Python** — the Python SDK's response hook only exposes response headers, which carry no retry count, so there is nothing to parse. |
| `connections_opened`, `get_database_account` | Assumed (set to 1 at connect time), not server-reported. |
| `connection_mode` | Echoes the **requested** mode; not verified against the SDK. |

For the `mock` backend every metric comes from `specs/mock-profile.json`, so both
runners stay byte-for-byte identical.

---

## Quick start

```bash
./scripts/run-mvp.sh
# open http://127.0.0.1:8077
```

The script installs Python deps, builds the Java runner (if Maven is present),
and starts the orchestrator + dashboard. Click **Run All**.

### Run from the CLI (no UI)

```bash
# One scenario through the Python runner, mock backend:
python3 -c "import yaml,json;print(json.dumps({'scenario':yaml.safe_load(open('specs/phase03-crud/100-create-item-explicit-id.yaml')),'config':{'backend':'mock'},'sdk_version':'4.9.0'}))" \
  | PYTHONPATH=harness/python python3 -m cosmos_test_runner

# Generate a comparison report for the latest orchestrator run:
python3 scripts/compare.py --run <run-id> --db orchestrator/results.db -o report.md
```

---

## Architecture (MVP)

```
Browser (static HTML matrix)
        │  REST (poll)
        ▼
Orchestrator API (FastAPI)  ──reads──►  specs/*.yaml
        │  subprocess (job JSON on stdin → result JSON on stdout)
        ├──────────────► Python runner (cosmos_test_runner)
        └──────────────► Java runner   (cosmos-test-runner.jar)
                                 │
                          mock | emulator | live
```

The **runner JSON contract** is the keystone: adding an SDK = a new runner that
reads the same job and emits the same result; adding a test = a new YAML file.

### Runner I/O contract

Input (stdin): `{ "scenario": {...}, "config": {...}, "sdk_version": "..." }`
Output (stdout): a result document with `status`, `metrics`, `assertions[]`,
`logs[]`, `duration_ms` (see any result in the dashboard drawer).

---

## Continuous integration

CI runs the same matrix the dashboard does — headlessly. The keystone is
`scripts/run-matrix.py`, which calls the orchestrator's own
`runner_dispatcher.dispatch()` for every `specs/*.yaml` × selected SDK, writes
one result JSON per job to `results/`, and **exits non-zero on any failure**.
`scripts/compare.py --fail-on-divergence` then fails the build if the SDKs
disagree on any scenario.

### Run it locally (exactly what CI runs)

```bash
# Full matrix against the deterministic mock backend:
python scripts/run-matrix.py --backend mock --sdks both --out results/

# Just one language (divergence comparison is then moot):
python scripts/run-matrix.py --backend mock --sdks python --out results/

# Run against a locally-built SDK (a variant jar / venv built from an SDK branch):
python scripts/run-matrix.py --backend emulator --sdks java --source local --out results/

# Cross-SDK divergence gate (no-op when fewer than two SDKs are present):
python scripts/compare.py results/ -o report.md --fail-on-divergence
```

### GitHub Actions

| Workflow | Trigger | Backend | Notes |
| --- | --- | --- | --- |
| `.github/workflows/ci.yml` | push / PR (auto) + manual | `mock` (default), `live` | The PR gate. Auto-runs **both** SDKs + divergence gate. |
| `.github/workflows/nightly-emulator.yml` | nightly cron + manual | `emulator` | Spins up the Cosmos Linux emulator service container. |
| `.github/workflows/sdk-from-source.yml` | manual | `emulator` / `live` | Builds the Cosmos SDK from a chosen **branch/ref** of an Azure SDK monorepo, then runs the matrix with `--source local`. |

**Manual runs are parameterized** (Actions → Run workflow):

- **`languages`** — `both` / `python` / `java`. Selecting one language runs only
  that runner and **skips the JDK/Maven setup** (for `python`) so the job is
  faster; the divergence step is skipped automatically (nothing to compare).
- **`divergence_check`** — toggle whether SDK disagreement fails the build. Only
  applies when `languages = both`.
- **`backend`** (ci.yml) — `mock` / `emulator` / `live`. `live` reads
  `COSMOS_ENDPOINT` / `COSMOS_KEY` from repo **secrets**; keys never appear in
  logs (the store redacts them).

Both runs upload `results/*.json` + `report.md` as artifacts.

### Testing an unreleased SDK branch (registry vs. local source)

By default both runners consume **released** artifacts — Java from Maven Central
(`${azure.cosmos.version}` in `harness/java/pom.xml`), Python from PyPI
(`azure-cosmos`). To validate an unmerged SDK branch you can build it from source
and run the matrix against that build instead:

- **In the orchestrator UI** — each SDK has a version dropdown (the `SDKs` row).
  It lists the published registry version plus a **local build** entry; the
  selection is remembered in `localStorage`. The result drawer shows the
  **Resolved SDK** version actually loaded at runtime, so you can confirm which
  build ran.
- **Headless** — `scripts/run-matrix.py --source local` points the dispatcher at
  the locally-built artifacts (`harness/java/variants/local/cosmos-test-runner.jar`
  for Java, `harness/python/.venv-local` for Python). The catalog of versions /
  sources lives in `config/default.yaml` under the `sdks` block.
- **In CI** — `sdk-from-source.yml` (Actions → Run workflow) takes `language`,
  `sdk_repo`, `sdk_ref`, `sdk_ref_custom`, and `backend`. **`sdk_ref` is a
  dropdown of active Cosmos branches from `Azure/azure-sdk-for-java`** (GitHub
  caps a workflow dropdown at a static, short list, so it can't show all ~1000
  branches live); for any other branch/tag/commit — or a `python` repo — type it
  into **`sdk_ref_custom`**, which overrides the dropdown. The effective ref is
  validated against the chosen repo (`git ls-remote`) before the build, so a typo
  fails fast with close matches. The workflow then checks out the Azure SDK
  monorepo at that ref, builds `azure-cosmos` (Java → into `.m2`, then a variant
  jar with `-Dazure.cosmos.version=<branch-version>`; Python → into a dedicated
  venv), and runs the matrix with `--source local`. Source builds only make sense
  against a real backend (`emulator` / `live`) — the `mock` backend never loads
  the SDK.

### Validating the pipeline on GitHub

1. **Get it onto GitHub** — `git init && git add -A && git commit` then
   `gh repo create … --source=. --push` (this folder isn't a git repo yet).
2. **Auto gate** — open a PR; `ci.yml` runs on `pull_request`. Watch with
   `gh run watch`. Confirm green + artifacts uploaded.
3. **Negative test** — push a commit that breaks one scenario and confirm the
   check goes red; revert. (Locally proven: forcing a status mismatch makes
   `compare.py --fail-on-divergence` exit 1.)
4. **Exercise the inputs**:
   ```bash
   gh workflow run ci.yml -f languages=python                       # Python only, no Java setup, divergence skipped
   gh workflow run ci.yml -f languages=java                         # Java only
   gh workflow run ci.yml -f languages=both -f divergence_check=false  # both, gate off
   ```
5. **Make it a gate** — add `ci.yml` as a required status check on the default
   branch so it must pass before merge.
6. **Integration / live** — let `nightly-emulator.yml` run (or dispatch it); for
   live, add `COSMOS_ENDPOINT`/`COSMOS_KEY` secrets and dispatch `ci.yml` with
   `backend=live` against a throwaway database.

---

## Layout

```
specs/            YAML scenarios (+ schema.yaml, mock-profile.json)
orchestrator/     FastAPI app, dispatcher, SQLite store, static dashboard
harness/python/   Python runner (cosmos_test_runner)
harness/java/     Java runner (Maven, shaded jar)
scripts/          run-mvp.sh, run-matrix.py (CI), compare.py
config/           default.yaml
.github/workflows/ ci.yml, nightly-emulator.yml, sdk-from-source.yml
```

---

## Extending

- **Add a scenario:** drop a YAML file in `specs/` (see `specs/schema.yaml` for
  supported actions and assertion types). It appears in the matrix on restart.
- **Add an SDK:** add a runner that honors the JSON contract and register it in
  `orchestrator/app/runner_dispatcher.py::RUNNERS`.
- **Use a real backend:** start the Cosmos emulator (or point at a live
  account), then run with `config.backend = "emulator"` / `"live"` and supply
  `endpoint`/`key`.

---

## Environment notes

Verified on macOS arm64 with Python 3.9 and Java 21 + Maven. Node and Docker
were not required: the dashboard is dependency-free static HTML, and the `mock`
backend removes the need for the Cosmos emulator to demo the platform.
