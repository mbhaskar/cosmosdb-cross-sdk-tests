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

```bash
docker compose -f proxy/docker-compose.proxy.yaml up -d
# wait for the emulator health check to go healthy (~1-2 min on first boot)
docker compose -f proxy/docker-compose.proxy.yaml ps
```

⚠️ **Apple Silicon:** the Linux Cosmos emulator image is x86 and runs under
emulation on M-series Macs — it can be slow/flaky. If it misbehaves, target a
real Cosmos account with `--backend live` instead of the emulator; the proxy
chain is identical.

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
> `throttle_window_clear` → mitmproxy. You do not drive the proxies by hand when
> running scenarios.

## Driving the 429 window by hand

The mitmproxy addon exposes a control channel on the same port via a magic
`/__fault/*` path (never forwarded upstream):

```bash
# throttle everything for 2 minutes with a 1s retry-after hint
curl -k -X POST 'https://localhost:18091/__fault/throttle?seconds=120&retry_after_ms=1000'

# check state
curl -k 'https://localhost:18091/__fault/status'
#   {"armed":true,"remaining_s":118.4,"injected":37}

# heal early (otherwise it auto-clears when the window expires)
curl -k -X POST 'https://localhost:18091/__fault/clear'
```

While armed, every request is answered `429` with `x-ms-retry-after-ms`; once the
window elapses the addon forwards traffic to Cosmos unchanged, so you observe the
SDK's real retry/backoff and recovery.

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
