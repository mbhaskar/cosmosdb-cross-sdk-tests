# Fault-injection proxy stack

This directory holds the fault-injection tier that the emulator/live scenarios
(`specs/phase06-fault-injection/`, ids `T-3xx`) run against. It is **not** used by
the offline mock tier — the mock (`backends: [mock]`, ids `C-2xx`) simulates
protocol faults deterministically in-process and needs no proxy.

## The two layers (this is the whole point)

Faults live at two different layers of the stack, and **no single tool covers
both**:

| Layer | Examples | Tool | Can it emit an HTTP 429? |
|-------|----------|------|--------------------------|
| **L4 — TCP transport** | latency, jitter, bandwidth cap, connect timeout, connection reset, region black-hole | **Toxiproxy** | ❌ No — it only sees bytes/sockets |
| **L7 — HTTP protocol** | 429 / 410 / wrong status, header/body tampering | **mitmproxy** addon | ✅ Yes |

> **Common misconception:** "configure Toxiproxy to return 429s." Toxiproxy
> operates purely at TCP and never parses HTTP, so it *cannot* produce a status
> code. Anything that is fundamentally an HTTP response (like a `429
> TooManyRequests`) is injected by the mitmproxy addon instead. See
> `mitm/throttle_window.py`.

The two compose into a chain, so a scenario can be *throttled **and** on a bad
network* at once:

```
SDK runner ─▶ mitmproxy (:18091, L7 429 window) ─▶ Toxiproxy (:18081, L4 faults) ─▶ Cosmos emulator (:8081)
```

- Point the SDK at **`:18091`** to get both tiers (L7 + L4).
- Point it at **`:18081`** for transport faults only.
- Toxiproxy admin API is **`:8474`**; secondary region proxy is **`:18082`**.

## Files

| File | Purpose |
|------|---------|
| `docker-compose.proxy.yaml` | Brings up emulator + Toxiproxy + mitmproxy |
| `toxiproxy.json` | Pre-registers the `cosmos` / `cosmos-secondary` proxy routes |
| `profiles/*.yaml` | Declarative **Toxiproxy** toxic profiles (L4 only) |
| `mitm/throttle_window.py` | **mitmproxy** addon: time-windowed 429 injection (L7) |

## Bring the stack up

The portal (`scripts/run-mvp.sh`) does **not** start this stack — it only serves
the dashboard and runs the mock (no-infra) scenarios. The T-3xx fault scenarios
need the Docker stack, started separately with the helper script:

```bash
scripts/run-fault-stack.sh up       # emulator + toxiproxy + mitmproxy
scripts/run-fault-stack.sh status    # container + Toxiproxy health
scripts/run-fault-stack.sh logs      # tail logs
scripts/run-fault-stack.sh down      # tear it all down
```

(Equivalent to `docker compose -f proxy/docker-compose.proxy.yaml up -d`, plus a
health-wait and an endpoint summary.)

Once it's up, you can run T-3xx **straight from the portal** — no env vars
needed. `config/default.yaml` already points the emulator backend at the proxy
chain (`proxy_endpoint` → Toxiproxy :18081, `mitm_endpoint` → mitmproxy :18091),
and the runner picks Toxiproxy vs mitmproxy per scenario automatically. If you
see `cannot reach Toxiproxy at ...`, the stack isn't up — run `run-fault-stack.sh up`.

### About the emulator image (vNext)

The stack uses the **vNext Linux emulator**
(`mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-latest`), which is
**multi-arch and runs natively on Apple Silicon** — no x86 emulation, so it's far
faster and more reliable than the legacy image. Notes specific to vNext:

- **Gateway mode only.** All scenarios run against the gateway endpoint; the
  harness already drives the Python SDK in gateway mode.
- **HTTP by default → we force HTTPS.** The compose file starts it with
  `--protocol https` so the whole proxy chain stays on TLS (the .NET/Java SDKs
  require HTTPS against the emulator). The SDK skips verification of the emulator's
  self-signed cert automatically.
- **Ports:** `8081` gateway (HTTPS), `8080` health probe (`/alive`, `/ready`,
  `/status` — always HTTP), `1234` Data Explorer UI.
- **Feature subset.** vNext supports a subset of Cosmos features; the T-3xx
  scenarios stick to CRUD + query, which are supported.

## Run the fault scenarios from the CLI

You can also run them without the portal. The emulator backend auto-resolves the
proxy endpoints from `config/default.yaml`, so no env vars are strictly required;
override per-run with `COSMOS_PROXY_ENDPOINT` / `MITM_ENDPOINT` / `TOXIPROXY_URL`
if your ports differ.

## Run the fault scenarios

Transport faults (T-301..T-306) — SDK talks to Toxiproxy:

```bash
COSMOS_ENDPOINT=https://localhost:18081 COSMOS_KEY=<emulator-or-account-key> \
TOXIPROXY_URL=http://localhost:8474 \
python scripts/run-matrix.py --backend emulator \
  --specs specs/phase06-fault-injection --sdks python
```

Protocol 429 window (T-307) — SDK talks to mitmproxy (front of the chain):

```bash
COSMOS_ENDPOINT=https://localhost:18091 COSMOS_KEY=<emulator-or-account-key> \
MITM_ENDPOINT=https://localhost:18091 TOXIPROXY_URL=http://localhost:8474 \
python scripts/run-matrix.py --backend emulator \
  --specs specs/phase06-fault-injection --sdks python
```

> The runner wires the scenario's timeline verbs to the right controller
> automatically: `net_*`/`region_*` → Toxiproxy, `net_throttle_window` /
> `throttle_window_clear` and the generic `inject_fault` / `fault_clear` →
> mitmproxy. You do not drive the proxies by hand when running scenarios.

## Driving protocol faults by hand

The mitmproxy addon exposes a control channel on the same port via a magic
`/__fault/*` path (never forwarded upstream). All fault *shapes* live in the
pluggable registry `proxy/mitm/fault_engine.py` (`FAULTS`) — adding a new fault
is one entry there, no addon logic changes.

```bash
# arm any registered fault by name, scoped by a time window OR a request count:
curl -k -X POST 'https://localhost:18091/__fault/arm?fault=throttle_429&seconds=120'
curl -k -X POST 'https://localhost:18091/__fault/arm?fault=gone_410&count=2'
curl -k -X POST 'https://localhost:18091/__fault/arm?fault=namecache_410&seconds=30'

# back-compat alias: /__fault/throttle == /__fault/arm?fault=throttle_429
curl -k -X POST 'https://localhost:18091/__fault/throttle?seconds=120&retry_after_ms=1000'

# ad-hoc override without a registry entry:
curl -k -X POST 'https://localhost:18091/__fault/arm?fault=x&status=408&substatus=9999&count=1'

# check state
curl -k 'https://localhost:18091/__fault/status'
#   {"armed":true,"fault":"gone_410","mode":"count","remaining_count":2,...}

# heal early (otherwise it auto-clears when the window/count is exhausted)
curl -k -X POST 'https://localhost:18091/__fault/clear'
```

Registered faults (`fault_engine.FAULTS`):

| name | status | x-ms-substatus | retry-after | SDK reaction |
| --- | --- | --- | --- | --- |
| `throttle_429` | 429 | 3200 | yes | back off, retry after delay |
| `gone_410` | 410 | 1002 (PKRangeGone) | no | refresh routing cache, retry |
| `namecache_410` | 410 | 1000 (NameCacheStale) | no | refresh collection cache, retry |
| `retrywith_449` | 449 | 0 | no | immediate retry |
| `unavailable_503` | 503 | 0 | yes | retry another replica |

While armed, every request is answered with the fault template; once the
window/count is exhausted the addon forwards traffic to Cosmos unchanged, so you
observe the SDK's real retry/backoff and recovery.

## Driving Toxiproxy toxics by hand

```bash
# apply a profile from profiles/ via the orchestrator API...
curl -X POST localhost:8077/api/proxy/activate -H 'content-type: application/json' \
  -d '{"profile":"latency-spike"}'
curl -X POST localhost:8077/api/proxy/clear

# ...or straight against the Toxiproxy admin API
curl -X POST localhost:8474/proxies/cosmos/toxics \
  -d '{"type":"latency","attributes":{"latency":800,"jitter":200}}'
```

## TLS note

Both the emulator and mitmproxy present self-signed certificates. The compose
passes `ssl_insecure=true` to mitmproxy for the upstream hop, and the harness
control client skips verification for the local `/__fault/*` calls. Your SDK
client must trust (or be told to skip) the proxy cert — for the emulator use its
well-known cert, and for mitmproxy install its CA (`~/.mitmproxy/mitmproxy-ca-cert.pem`)
or run the SDK with cert verification disabled in the test config.

## Tear down

```bash
docker compose -f proxy/docker-compose.proxy.yaml down
```
