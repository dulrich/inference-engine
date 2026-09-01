# llama.cpp `llama-server` on Vulkan + llama-swap, for the fleet's GPU inference services.
#
# Resulting tag: crucible-llamacpp-vulkan:b10534-swap243 (llama.cpp tag + llama-swap tag baked
# into this image, so the tag alone tells you both pins without opening the Dockerfile).
#
# As of the llama-swap addition this image is dual-use, and that is deliberate — it is mid
# migration from "one model, one process, one systemd socket" to "one always-on multi-model
# router" (see README.md). `ENTRYPOINT` proves the GPU is real and that `llama-server` can link
# (see entrypoint.sh — both assertions belong to the container, not to a command); whatever you
# pass after that (`llama-swap -config ... -listen ...` — the image's `CMD` default — or a bare
# `llama-server -m ... --embeddings ...` for the old single-model shape) is exec'd unchanged.
#
# Why this image exists at all, when the search companion's Dockerfile sits at the repo root:
# that one ships Crucible's own code, this one ships a third-party inference server. Its build
# context is this directory (not the repo root), so it shares nothing with the companion image —
# in particular it does not inherit the root `.dockerignore`, which is load-bearing there and
# irrelevant here.
#
# ── The base image is the entire point, so do not "modernise" it downward ────────────────────
#
# The GPU is an RX 9070 (gfx1201, RDNA4). RADV drives it, but only on a recent Mesa: this box's
# host runs 26.1.5 from the kisak PPA, which is *why* it works. `debian:sid-slim` is used here
# because it independently ships that same 26.1.5, verified inside a container with only
# /dev/dri passed through:
#
#   deviceName = AMD Radeon RX 9070 (RADV GFX1201)   deviceType = DISCRETE_GPU
#   driverName = radv                                driverInfo = Mesa 26.1.5-1
#
# A stable base (Debian trixie, Ubuntu 24.04) ships Mesa 24.x/25.0.x, and the failure is silent
# rather than loud: `llvmpipe` — a software rasteriser — is *also* enumerated as a Vulkan device
# (see the probe output above; it is there even on a working host). So an image whose RADV is
# too old to claim gfx1201 does not fail to start. It runs on the CPU, passes any healthcheck
# that only asks "is the server up", and is *slower* than the Infinity CPU containers it was
# meant to replace. This is the same shape as the `torch.cuda.is_available()` trap already
# recorded for the ROCm attempt, and it is why entrypoint.sh asserts the selected device rather
# than trusting that the GPU was picked.
#
# The cost of sid is that it moves, and nothing pins the Mesa version — not here, and not in the
# compose healthcheck (an earlier version of this comment claimed the latter; it was never true).
# What guards the Mesa side is the runtime assertion in entrypoint.sh, which refuses to start on
# a software rasteriser, so a regression shows up as a stopped container rather than a slow one.
# `docker compose build --no-cache` is the way to pick up a Mesa fix.
#
# The same movement is what makes the libc6 pin in the runtime stage necessary — read that
# comment before editing any apt line in this file.

# ── Build stage ──────────────────────────────────────────────────────────────────────────────
FROM debian:sid-slim AS build

# Pinned to a release tag, not master: an inference server that silently changes its wire format
# under a rebuild is exactly what the embedding-space work exists to make impossible elsewhere.
# Bump deliberately, and re-run the agreement harness when you do — a llama.cpp change to pooling
# or normalisation would move every vector this serves. That harness lives in the consumer that
# owns the embedding space, not here: `obsidian-crucible/scripts/embedding-agreement.mjs`.
ARG LLAMA_CPP_TAG=b10534

# `glslc` alone is not enough: ggml's vulkan-shaders-gen resolves SPIRV-Headers through CMake's
# find_package, and Debian ships those headers in their own package rather than pulling them in
# as a glslc dependency. Without spirv-headers the configure step fails with
# "Could not find a package configuration file provided by SPIRV-Headers" before a single shader
# is compiled.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates \
      libvulkan-dev glslc spirv-headers spirv-tools \
    && rm -rf /var/lib/apt/lists/*

# Record the glibc this stage actually resolved, for the runtime stage to pin itself to.
#
# `build-essential` pulls in `libc6-dev`, which upgrades `libc6` itself to whatever sid holds on
# build day — so this stage routinely ends up on a newer glibc than a stage that never installs a
# toolchain. Everything built below links against that newer glibc. The single quotes around the
# dpkg-query format are load-bearing: the builder does not expand variables in RUN, but the shell
# does, and `${Version}` unquoted would expand to the empty string and write an empty marker.
RUN dpkg-query -W -f='${Version}' libc6 > /libc6.version

WORKDIR /src
RUN git clone --depth 1 --branch "${LLAMA_CPP_TAG}" https://github.com/ggml-org/llama.cpp.git .

# GGML_VULKAN=ON is the whole build. GGML_NATIVE=OFF keeps the binary from baking in this CPU's
# exact instruction set — the GPU does the work, and a portable binary means the image can be
# built on one fleet host and run on another without an illegal-instruction crash.
RUN cmake -B build \
      -DCMAKE_BUILD_TYPE=Release \
      -DGGML_VULKAN=ON \
      -DGGML_NATIVE=OFF \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DLLAMA_BUILD_SERVER=ON \
      -DLLAMA_CURL=OFF \
    && cmake --build build --config Release --target llama-server -j"$(nproc)"

# ── llama-swap source-build stage ───────────────────────────────────────────────────────────
#
# llama-swap turns this image from "one model, one process" into a multi-model router: it spawns
# llama-server children on demand per request (holding the client's request until the child
# reports healthy on its `checkEndpoint`), swaps/unloads them per `ttl`, and exposes one stable
# set of aliases across `/v1/chat/completions`, `/v1/embeddings`, `/v1/models` (aggregated), and
# `/rerank` + `/v1/rerank`.
#
# Built from source, not downloaded as a GitHub release asset, because the release binary's Go
# stdlib version is not something a tag bump can fix: upstream's release CI (`.goreleaser.yaml`)
# pins its own toolchain via `actions/setup-go`'s `go-version-file: go.mod`, and `go.mod`'s `go
# 1.26.1` line is a *minimum*, not a `toolchain` pin — every prebuilt release, including ones
# newer than this fleet's scan, links against that same go1.26.1 stdlib. All 20 HIGH `gobinary`
# findings in `fleet-tools/data/security-scan/inference-engine.images.json` are CVEs fixed
# somewhere between go1.25.9 and go1.26.6/1.27.0-rc.3 — i.e. closed by any newer toolchain, with
# zero code or config change, since llama-swap's own module carries no `toolchain` directive that
# would override it. Building here under `golang:1.26.7-trixie` closes all 20.
#
# Pinned to v243 specifically, not upstream's current v251: measured, the `v243..v251` diff
# touches `config-schema.json` and `internal/config/load.go`/`matrix.go` — real config-parsing
# surface this fleet's `config.yaml` has never run against. v243 is the release this fleet was
# authored against and is already validated; v242 fixed a TTL-unload deadlock and llama-swap has
# no stability guarantee below that. Bump `LLAMA_SWAP_TAG` and `LLAMA_SWAP_COMMIT` together,
# deliberately, per "Upgrading llama-swap" in README.md — never independently.
#
# No `-tags embed_ui`: the ldflags below mirror `.goreleaser.yaml`'s own release recipe (as
# checked out at LLAMA_SWAP_COMMIT) minus that one tag. `embed_ui` requires `make ui` (Node/npm)
# to populate `ui_dist` for `//go:embed`; llama-swap's own `internal/server/embed_notag.go` is
# the documented `!embed_ui` fallback that serves no UI files and needs no Node toolchain. This
# fleet never calls llama-swap's own web UI (nothing in README.md/AGENTS.md/config.yaml
# references it; the fleet has its own bench dashboard on :4810), so building without it is a
# lower-complexity substitute for the release binary, not a functionality cut.
FROM golang:1.26.7-trixie AS swap

ARG LLAMA_SWAP_TAG=v243
# Full commit SHA the v243 tag points at (a commit object, not an annotated tag), resolved
# 2026-09-01 via the GitHub refs API. Bump LLAMA_SWAP_TAG and this SHA together — see
# "Upgrading llama-swap" in README.md for how to resolve the next one.
ARG LLAMA_SWAP_COMMIT=4c50064e3d7ff44834cf1b12ab529befa79f22d6

# git and ca-certificates already ship in golang:1.26.7-trixie (verified via `docker run
# golang:1.26.7-trixie sh -c 'which git; git --version'` before writing this stage) — no
# apt-get install needed here, unlike router/Dockerfile's node:24-slim build stage.
WORKDIR /src
# Full SHA fetch, never the tag or an abbreviation: GitHub's fetch-by-commit protocol
# (allowReachableSHA1InWant) only resolves a full 40-character SHA — the same constraint
# router/Dockerfile's AI_GATEWAY_COMMIT comment records, and it applies identically here.
RUN git init -q \
    && git remote add origin https://github.com/mostlygeek/llama-swap.git \
    && git fetch --depth 1 origin "${LLAMA_SWAP_COMMIT}" \
    && git checkout -q FETCH_HEAD

# ldflags variable names verified against the checked-out source (repo-root `llama-swap.go`,
# package main: `var ( version = "0"; commit = "abcd1234"; date = "unknown" )`) and against
# `.goreleaser.yaml`'s own release recipe, which sets exactly these three via `-X
# main.version=... -X main.commit=... -X main.date=...`. Short SHA and RFC3339 date are computed
# in-shell: Dockerfile ARG interpolation has no bash substring expansion, and .goreleaser.yaml's
# own `{{ .ShortCommit }}` / `{{ .Date }}` are the short SHA and RFC3339 UTC respectively.
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
      -ldflags="-s -w -X main.version=${LLAMA_SWAP_TAG} -X main.commit=$(echo "${LLAMA_SWAP_COMMIT}" | cut -c1-7) -X main.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      -o /src/llama-swap .

# ── Runtime stage ────────────────────────────────────────────────────────────────────────────
FROM debian:sid-slim

# ── The libc6 pin, and why this COPY sits ABOVE the apt line instead of with its siblings ────
#
# This COPY looks misplaced. It is not, and moving it down re-arms a 30-hour outage (2026-08).
#
# All three stages start FROM the same rolling `debian:sid-slim`, but the build stage's
# `build-essential` upgrades its `libc6` and the list below never mentions `libc6` at all — so
# the runtime stage sits on the base layer's older glibc, and the `COPY --from=build` of the
# binaries a few lines down lands newer-glibc objects on an older loader. Every `llama-server`
# then dies before `main()` with "version `GLIBC_x.yz' not found". `llama-swap` is a static Go
# binary, so the router survives, keeps answering `/health` with `OK`, and the container reports
# `(healthy)` while returning 500 to every request routed through it.
#
# Adding a bare `libc6` to the list below does NOT fix this. BuildKit schedules stages as a
# dependency graph, and the runtime stage has no edge to the build stage until its first
# `COPY --from=build` — so the two apt transactions are free to run as parallel vertices against
# different snapshots of a moving index. THIS COPY IS THE EDGE. Placing it above the apt line is
# what forces the builder to finish the build stage first; pinning to the exact recorded version
# is what makes the outcome deterministic rather than merely likely.
#
# If that version is not installable the build fails loudly. That is the design — a hard failure
# at build time is the cheap version of this bug. Do not "fix" such a failure by loosening the
# pin; rebuild with --no-cache so both stages resolve against one index.
COPY --from=build /libc6.version /usr/local/share/llama-abi/libc6.version

# mesa-vulkan-drivers is RADV itself; vulkan-tools provides `vulkaninfo`, which entrypoint.sh
# uses to prove a discrete GPU was found before the server is allowed to start. Keeping
# vulkan-tools in the runtime image is deliberate: the assertion is worth more than the ~15MB.
# The marker is kept in the final image rather than deleted, so the pin can be verified from a
# running container (`dpkg-query -W libc6` must equal it) and entrypoint.sh can cite it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      "libc6=$(cat /usr/local/share/llama-abi/libc6.version)" \
      libvulkan1 mesa-vulkan-drivers vulkan-tools libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=build /src/build/bin/*.so /usr/local/lib/
RUN ldconfig

COPY --from=swap /src/llama-swap /usr/local/bin/llama-swap

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Read-only mount point for the GGUF library. Nothing in this image ever writes to it.
VOLUME ["/models"]

# /app/config.yaml is where the compose service (fleet-tools, WP-6) bind-mounts
# config.yaml read-only. It is NEVER COPYed into the image — llama-swap's config must be
# editable without a rebuild, and baking it in would defeat that. This just ensures the mount
# point directory exists.
RUN mkdir -p /app

RUN groupadd -g 1001 app && useradd -m -u 1001 -g app app
USER app

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# Dual-use default: the router is the new normal, but the image still runs a bare
# `llama-server ...` command unchanged (`entrypoint.sh`'s tail is `exec "$@"`) — see README.md.
CMD ["llama-swap", "-config", "/app/config.yaml", "-listen", ":4806"]
