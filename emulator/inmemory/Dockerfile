# Dockerized build+run of the Azure Cosmos DB Rust in-memory emulator host
# (azure_data_cosmos_emulator, from Azure/azure-sdk-for-rust PR #4786).
#
# The host is a thin out-of-process shell over the driver's
# `in_memory_emulator` (behind the `__internal_in_memory_emulator` feature) that
# serves the deterministic, memory-backed emulator over real network ports, so
# non-Rust clients (our Python + Java SDK runners) can exercise it over the
# Cosmos DB wire protocol.
#
# IMPORTANT (loopback design): the host hard-binds every listener to 127.0.0.1
# and advertises regional gateway URLs as http://127.0.0.1:<port>/. It is meant
# to run co-located with the client over loopback. A container that binds only
# 127.0.0.1 is NOT reachable via Docker published ports, so the runtime stage
# runs `socat` bridges (0.0.0.0:<port> -> 127.0.0.1:<port>) for each listener
# and we publish the SAME port numbers 1:1 on the host. Host 127.0.0.1:<port>
# then resolves to the published port, so the advertised 127.0.0.1 URLs (used by
# gateway-mode SDK location routing) keep working on the host machine.

# ---------------------------------------------------------------------------
# Builder: compile the emulator host from a pinned azure-sdk-for-rust commit.
# ---------------------------------------------------------------------------
FROM rust:1-bookworm AS builder

# aws-lc-rs / ring (pulled in via the driver's `rustls` feature) need a C
# toolchain + cmake to build their native crates.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake clang \
    && rm -rf /var/lib/apt/lists/*

# Pin to the PR #4786 merge commit for reproducible builds.
ARG AZURE_SDK_RUST_REF=97e0d63c68dae79552990cce63b47fb152fde697

WORKDIR /src
RUN git init -q \
    && git remote add origin https://github.com/Azure/azure-sdk-for-rust.git \
    && git fetch --depth 1 origin "${AZURE_SDK_RUST_REF}" \
    && git checkout -q FETCH_HEAD

# The crate's Cargo.toml already enables the driver's
# `__internal_in_memory_emulator` + `rustls` features, so a plain build works.
RUN cargo build -p azure_data_cosmos_emulator --release

# ---------------------------------------------------------------------------
# Runtime: run the host + socat loopback bridges.
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends socat curl ca-certificates python3 openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/target/release/azure_data_cosmos_emulator /usr/local/bin/azure_data_cosmos_emulator
COPY config/account.json /etc/cosmos-emulator/account.json
COPY cosmos_gateway_proxy.py /usr/local/bin/cosmos_gateway_proxy.py
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Fixed ports (must match config/account.json and the compose port publishing).
EXPOSE 49150 49151

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
