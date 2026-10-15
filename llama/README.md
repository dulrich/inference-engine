# crucible-inference — llama.cpp on Vulkan + llama-swap

One always-on GPU inference router serving the fleet's embedding, reranking, chat and vision
models from a single container on **127.0.0.1:4811** (host publish; the
container listens on :4806 internally — the transitional 4806 host publish was retired
2026-07-27).

This replaces the old shape — two separate `llama-server` containers, each behind its own
systemd socket that started/stopped the container on demand — with **one router process**
(`llama-swap`) that itself spawns/swaps/unloads the actual `llama-server` children per-model,
in-container, per its `config.yaml`. The socket-activation apparatus (`systemd/*.socket`,
`*.service`, `crucible-inference-ctl`, `install.sh`) was deleted at the 2026-07-26 cutover —
it lives on in git history if a per-model socket-activated deployment is ever wanted again.

| | Infinity (CPU) | this (Vulkan GPU) | |
|---|---|---|---|
| Embedding `bge-m3`, batch 96 | 3.7 chunks/s | **95.0 chunks/s** | 25.7× |
| Reranking `bge-reranker-v2-m3`, 30 docs | 9.48 s | **0.280 s** | 34× |
| Cold start after idle | n/a (always resident) | **1.3 s** | |
| Warm request | | 67 ms | |

Measured 2026-07-25 on an RX 9070 (gfx1201, RDNA4) via RADV, against the single-model shape.
The reranking figure is the one that matters in use: at Crucible's `searchRerankTopN` of 30 it
is the difference between a button people avoid and one they press without thinking.

## What is here

```
Dockerfile          multi-stage build: llama.cpp b10534 (Vulkan) + llama-swap v243, debian:sid-slim
entrypoint.sh       refuses to start on a software rasteriser (see "The llvmpipe trap") or on a
                    glibc-skewed image (see "The ABI trap"); dual-use exec tail — runs whatever
                    command it's given (llama-swap or a bare llama-server)
config.yaml         llama-swap router config: models, aliases, ttl, groups — bind-mounted, not baked in
smoke-inference.sh  host-run smoke test against a running router (see "Smoke testing" below)
```

Tag: **`crucible-llamacpp-vulkan:b10534-swap243`** — llama.cpp tag + llama-swap tag, both baked
into the name so it's visible without opening the Dockerfile.

`tools/hf-gguf-fetch.sh <org/repo> <file> [<file>...]` — resumable GGUF fetcher: downloads from Hugging Face into `/models/<org>/<repo>/<file>`, byte-size-verified against the HF tree API, idempotent (see the script's header for details).

The compose service itself (`crucible-inference`, port mapping, `/models` and `config.yaml`
mounts, `/dev/dri` passthrough, `mem_limit`) lives in the fleet repo,
`fleet-tools/fleet/compose.fleet.yml`.

## Dual-use image, on purpose

`entrypoint.sh` proves two things about the *container* — that the GPU is real, and that
`llama-server` can dynamically link — and nothing about the command; everything after that is
`exec "$@"` — whatever the container is given, unchanged. Both assertions therefore apply to
both shapes below, including the bare-`llama-server` one. Two supported shapes:

```bash
# The new default (this image's CMD): llama-swap owns the router, spawns llama-server children
# per config.yaml.
docker run ... crucible-llamacpp-vulkan:b10534-swap243
# equivalent to:
docker run ... crucible-llamacpp-vulkan:b10534-swap243 \
  llama-swap -config /app/config.yaml -listen :4806

# The old single-model shape still works unchanged, for anything not yet migrated:
docker run ... crucible-llamacpp-vulkan:b10534-swap243 \
  llama-server -m /models/CompendiumLabs/bge-m3-gguf/bge-m3-f16.gguf --embeddings -ngl 99 ...
```

Both paths pass through the same GPU assertion; there is no separate "router mode" build.

## `config.yaml` — the router's model list

Bind-mounted **read-only** at `/app/config.yaml`; the Dockerfile does not `COPY` it, so editing
it (adding a model, changing a `ttl`) needs no image rebuild — only a container restart. See the
file itself for the full annotated shape; the essentials:

The live alias set, as of 2026-09-13 (refreshed from `config.yaml` itself — the retired
`gemma-4-12b` row is gone; see the tombstone in that file's consumer-registry header):

| Alias | Group | Role | ttl | Notes |
|---|---|---|---|---|
| `bge-m3` | `retrieval` | embedding | 1800s | f16, 1024d, `--embeddings` |
| `bge-reranker-v2` | `retrieval` | rerank | 1800s | Q8_0, `/rerank` only |
| `bge-small` | `retrieval` | embedding | 1800s | f16, 384d; news-ingestion stores it as `bge-small-en-v1.5` |
| `nemotron-4b` | `chat` | chat | 1800s | UD-Q4_K_XL, `-c 131072`, `--jinja`; small/fast option |
| `ling-3-tiny-ud-q4-xl` | `ling-bakeoff` | chat | 1800s | Ling-3.0-tiny UD-Q4_K_XL, `-c 131072` (its real ceiling) |
| `ling-3-tiny-ud-q6-xl` | `ling-bakeoff` | chat | 1800s | same model, UD-Q6_K_XL |
| `ling-3-tiny-q8` | `ling-bakeoff` | chat | 1800s | same model, Q8_0 reference quant |
| `gemma-4-12b-qat` | `gemma-qat-bakeoff` | chat + vision | 1800s | QAT UD-Q4_K_XL, `-c 131072`, `--mmproj`, MTP sidecar (`--spec-type draft-mtp`) |
| `qwen36-a3b` | `qwen36-a3b-bakeoff` | chat | 1800s | MoE UD-Q4_K_XL, `-c 131072`, `-ncmoe 32` + `--load-mode none`, `-ub 2048`, embedded MTP heads |
| `gemma-4-26b-a4b` | `gemma-26b-a4b` | chat + vision | 1800s | MoE QAT UD-Q4_K_XL, `-c 131072`, `-ncmoe 18` + `--load-mode none`, `-ub 2048`, `--mmproj`, MTP sidecar; served rung of the measured context ladder |
| `qwen38-27b-iq3xxs` | `qwen38-27b-bakeoff` | chat | 1800s | dense UD-IQ3_XXS, `-c 131072`, q4_0/q4_0 KV, `-fa on` |
| `qwen38-27b-iq3s` | `qwen38-27b-bakeoff` | chat | 1800s | dense UD-IQ3_S, same KV recipe; `--reasoning-budget 8192` backstop; the Claude Code picker row |
| `qwen38-27b-q3kxl` | `qwen38-27b-bakeoff` | chat | 1800s | dense UD-Q3_K_XL, `-c 65536` (its max-safe rung) |
| `qwen38-27b-iq3s-off` / `-low` / `-medium` / `-high` | `qwen38-27b-bakeoff` | chat | 1800s | the IQ3_S weights again, differing only in their server-enforced reasoning hard cap (off / 1024 / 4096 / unbudgeted); router-internal consumer only |

- **Aliases are the API surface, append-only.** `bge-m3` and `bge-reranker-v2` are the exact
  `modelId` values Crucible's live plugin settings (`data.json`) already store for the embed and
  rerank provider entries — that's what makes pointing a vault at this service a base-URL-only
  change instead of a model-id rewrite. Never rename or delete an alias once a vault may be
  using it; only add new ones. `gemma-4-12b` and `nemotron-4b` were the first two additions
  (`gemma-4-12b` has since been removed from serving — the alias stays reserved forever).
- **`retrieval` group** (`swap: false, exclusive: false, persistent: false`) holds all three
  retrieval models — small enough, and used closely enough together by one search/rerank flow, that
  neither should evict the other. `ttl: 1800` (30 min) unloads an idle model and returns its
  VRAM; `persistent` staying `false` is what lets that `ttl` actually apply.
- **`chat` group** (`swap: true, exclusive: true, persistent: false`) today holds `nemotron-4b`
  alone (it held `gemma-4-12b` too until that alias was removed from serving 2026-08-25); every
  later chat model got its own same-shaped group instead, one per family — the opposite policy
  from `retrieval`, because a chat model is large enough
  that it cannot safely coexist with another chat model or the retrieval pair under this GPU's
  VRAM. `exclusive: true` means a chat request evicts `bge-m3`/`bge-reranker-v2` first; a
  retrieval request afterward reloads without evicting the chat model. The eviction mechanics
  were verified at cutover (two full evict→reload cycles, byte-identical embeddings, no amdgpu
  errors); the specific model list above is new and gets the live smoke test (including the
  chat-completion checks below) before being treated as validated in production. See
  `config.yaml`'s `chat` group comment for the full VRAM fit math — measured host GPU VRAM,
  per-model weight sizes, and why `gemma-4-26B-A4B`/27B/31B/35B-A3B are excluded.
- `-b/-ub 8192`, `-ngl 99`, and the **absence** of `--pooling` on the retrieval macro are carried
  over verbatim from the compose invocations this service replaces — don't add `--pooling` back;
  the GGUF metadata and `--embeddings`/`--reranking` already select the right pooling. The chat
  macro (`llama_server_chat_common`) deliberately uses smaller `-b 2048 -ub 512` (llama.cpp's own
  defaults) instead — a large ubatch helps batch-embedding throughput but only costs VRAM for a
  single-conversation chat decode, and that VRAM matters more here given the fit math above.
  `--jinja` renders each chat model's own embedded GGUF chat template instead of a hardcoded
  built-in one — required for gemma4's reasoning-style template.

## Smoke testing

```bash
llama/smoke-inference.sh                       # default target :4811
llama/smoke-inference.sh --url http://127.0.0.1:4811
llama/smoke-inference.sh --wait-ttl            # also proves ttl-unload (~30 min sleep)
```

Requires `curl` and `jq` on the host running it; runs entirely against the already-running
router over HTTP — it never touches Docker/systemd itself. Checks, in order: `/health` is 200;
`/v1/models` lists both `bge-m3` and `bge-reranker-v2`; `/v1/embeddings` for `bge-m3` returns a
well-formed numeric vector (its length is reported, not asserted — dimension is a model fact,
not a script fact); `/rerank` for `bge-reranker-v2` against 3 documents returns exactly 3 results
with a unique index in `[0,2]` and a numeric `relevance_score` each; `/api/v0/models` (an LM
Studio-only endpoint llama-swap doesn't implement) fails fast — any 4xx/5xx within ~5s, not a
hang — per the fleet's standing "an endpoint that never fails loudly can still be silently
broken" lesson; `/v1/chat/completions` for each configured chat alias (`gemma-4-12b`,
`nemotron-4b`) with `max_tokens: 256` returns a non-empty `content` (the first request evicts
the retrieval group, per the `chat` group's `exclusive: true`, so this check budgets a longer
timeout for the cold spawn). The `ttl`-unload check is opt-in and slow (`--wait-ttl`) because the
retrieval group's `ttl` is 1800s; without the flag it's reported as skipped, not silently
omitted.

Exits non-zero on any failed check.

## Scheduling — nightly bake-off queue window (first fleet scheduling install)

The fleet's first standing schedule, set by `plans/longcontext-pow2-overnight-campaign.md`
WP-6 as a **user-confirmed convention**: cron, user crontab (the glibc-upgrade plan left
cron-vs-timers-vs-sidecar unresolved; this decided it). The mechanism was superseded by the
control plane (`plans/bakeoff-queue-control-plane.md`, 2026-08-24): the canonical crontab
lines — a minutely dispatcher plus the nightly, which **drops the old outer `flock`**
(`run-window.sh` now owns `cron.lock` for the run's whole process-group lifetime) — live in
`llama/bakeoff/README.md`'s "Scheduling" section, the single home for them. Times are the
**host's local** clock; the queue file's own `deadline` is **UTC**.

**Current install state (2026-08-24)**: the dispatcher minutely line is installed; the
`0 23` nightly line is **deliberately not installed** — pending manual runs through the
dashboard's control panel first — and is doubly gated even once installed: `cron-nightly.sh`
is fail-closed on the `control/host/armed` marker, currently absent (arm/disarm via the
dashboard, `POST /api/control/{arm,disarm}`). The ladder queue itself is untouched and fully
pending; the wrapper resumes it whenever a window next fires.

- `llama/bakeoff/cron-nightly.sh` owns the whole night: creates/starts the `bakeoff-scratch`
  sibling container if needed, runs `batch run` on `llama/bakeoff/queues/ladder-v1.json`
  (batch.py itself stops the router for the window and restarts it on every exit path), copies
  the queue state into the run dir, then runs `batch verify` — persisting
  `verify_report.{json,md}` (audit M2's run-then-verify pairing). The queue spans multiple
  nights by design: each night resumes pending cells, pauses at the queue's UTC deadline
  (`13:00` UTC = 07:00 local — this host is `America/Mexico_City`, permanent UTC-6, no DST),
  and no-ops once terminal.
- The wrapper's `cron.lock` flock serializes the runner against itself only; **GPU
  exclusivity against the router is batch.py's own container stop/start**, a deliberately
  separate mechanism. (`deep-health-check.sh` reads that same lock as its stand-down
  signal — see below.)
- The log's `NEEDS-ATTENTION:` lines are the operator contract: a hang-abort, a
  failed/interrupted cell (crash recovery marks these and NEVER silently re-runs them — re-arm
  deliberately with `batch run --re-arm-cell <cell>`), or verify findings. `batch verify`
  exiting 1 means honest findings in the reports, not breakage.

## Deep health check

`smoke-inference.sh` is a point-in-time check you run by hand. `deep-health-check.sh` is a
narrower, guardrail-shaped sibling meant to run unattended and repeatedly, to catch what the
compose healthcheck structurally cannot: a total serving outage behind a green router.

The split exists because one probe cannot both detect an outage promptly and exercise a cold
model load. The compose healthcheck (`compose.fleet.yml`) hits llama-swap's own `/health` every
30s — fast enough to be a useful "is the router up" signal, but llama-swap is a static Go
binary, so it kept answering `OK` for ~30 hours in August 2026 while every `llama-server` child
was dead at dynamic-link time (the ABI trap, above). `/health` proves the router process is up;
it has never proved a model can spawn and serve, and now says so in its own comment. A 30s
probe also *cannot* be turned into a spawn test: `bge-small-en-v1.5-f16` has `ttl: 1800` in
`config.yaml`, so any interval under 1800s keeps resetting the idle timer and the model never
actually unloads — the probe would only ever observe an already-warm process.

`deep-health-check.sh` is the other half: it `POST`s `/v1/embeddings` for `bge-small` (the
cheapest model in the retrieval group) and asserts the response **body** — `data[0].embedding`
must be an array of exactly 384 finite numbers — never a status code, for the same reason
`smoke-inference.sh` does: LM Studio answers unknown endpoints with HTTP 200 and an error body,
and a status-code check would have reported the August outage as healthy the entire time. It
tolerates one transient failure before declaring FAIL, because the `chat` group's
`exclusive: true` can legitimately evict `bge-small` mid-probe.

```bash
llama/deep-health-check.sh                                   # default target :4811, model bge-small
llama/deep-health-check.sh --url http://127.0.0.1:4811 --model bge-small
```

**Installed 2026-08-24** (user crontab), closing the outage-detection gap the August
incident exposed:

```
19 * * * * mkdir -p $HOME/.local/state/inference-engine && llama/deep-health-check.sh >>$HOME/.local/state/inference-engine/deep-health-check.log 2>&1
```

Hourly — the interval must exceed the 1800s `ttl` above so every run is a genuine cold
spawn. `:19`, not the originally-recommended `:17`, because the container-image security
scan fires at `:17` (user decision 2026-08-24 — don't stack an image scan and a cold GGUF
spawn on the same minute); still off the top of the hour to avoid herding. It logs under
`$HOME/.local/state` rather than `/var/log`, which is root-owned on this box: a user
crontab redirecting into an unwritable directory fails *before* the script runs, which
would leave this guardrail silently dead — the same shape of bug it exists to detect. The
script is **bake-off-window aware**: a live run window `docker stop`s the router for the
whole window, so the check test-and-releases the window's own `cron.lock` first and SKIPs
(exit 0, no alarm) while it's held — an intentionally-stopped router is not an outage.

**Alerting (2026-09-14):** nothing reads the log, so every run also writes a timestamped JSON
artifact — `$HOME/.local/state/inference-engine/deep-health-check.json` (override with
`DEEP_HEALTH_ARTIFACT_PATH`), atomically replaced, `status` ∈ `pass|fail|skip|fatal` with
`observed_at`, `attempts` and `detail`. Command Center (fleet-tools) bind-mounts that
directory read-only and renders the verdict as a pill on the `inference-engine-router` service
card; a missing, malformed, or stale artifact (older than `DEEP_HEALTH_STALE_MS`, default
150 min = 2.5× the hourly cadence) renders non-healthy rather than retaining the last PASS —
so a dead cron is visible too. SKIP is written on purpose: a multi-hour bake-off window keeps
the timestamp fresh instead of reading as stale.

## The llvmpipe trap

This is the failure mode `entrypoint.sh`'s GPU assertion exists for, and it is worth
understanding before changing the base image — the assertion is unchanged by the llama-swap
addition; it fires the same way whether the container ultimately runs `llama-swap` or a bare
`llama-server`.

A Vulkan loader with no usable hardware driver still enumerates **`llvmpipe`**, a software
rasteriser, as a completely valid Vulkan device — it is present even on this working host,
alongside the real GPU. So an image whose Mesa is too old to claim this GPU does not fail
loudly. `llama-server` (whether run directly or spawned as a llama-swap child) starts, answers
every request correctly, passes any healthcheck that only asks "is the port open", and runs
**several times slower than the CPU containers it replaced**. A silent CPU fallback is not a
degraded service; it is a regression wearing the costume of a working one.

The base image is therefore load-bearing. `debian:sid-slim` ships Mesa 26.1.5, which is what
gfx1201 (RDNA4) needs — the host runs the same version from the kisak PPA, and that is why the
GPU works here at all. Debian trixie and Ubuntu 24.04 ship Mesa 24.x/25.0.x. **Do not move the
base image to a stable release**, and if you do, expect the entrypoint to refuse to start rather
than let it degrade quietly.

`entrypoint.sh` asserts `PHYSICAL_DEVICE_TYPE_DISCRETE_GPU` or `INTEGRATED_GPU` before exec'ing,
and logs what `llama-server --list-devices` resolves so `docker logs` answers "did it use the
GPU?" without anyone reproducing a benchmark. `CRUCIBLE_ALLOW_CPU_VULKAN=1` bypasses the check
and should never be set in the fleet.

(As it happens ggml filters llvmpipe out on its own — it resolves only `Vulkan0: AMD Radeon
RX 9070 (RADV GFX1201)`. The assertion does not depend on that continuing to be true.)

## The ABI trap

The llvmpipe trap's sibling, and the one that actually took the service down for ~30 hours in
August 2026. Same shape: the guard passed and the thing was wrong.

All three build stages start `FROM debian:sid-slim`, which rolls forward. The build stage
installs `build-essential`, which drags in `libc6-dev` and upgrades `libc6` as a side effect;
the runtime stage's package list never mentioned `libc6`, so it stayed on the base layer's
older one. `COPY --from=build` then dropped binaries linked against the newer glibc onto the
older loader, and every `llama-server` process died at dynamic-link time with
``version `GLIBC_2.43' not found`` before it could open a port.

What made it expensive to notice: **`llama-swap` is a static Go binary**, so the router itself
was unaffected. It kept answering `/health` with `OK`, Docker kept reporting `(healthy)`, and
`/v1/models` kept listing every alias. Only an actual inference request failed —
`500 {"error":"unspecific error: upstream command exited prematurely"}` in ~0.25 s. That
latency is the tell: a startup death, not a load timeout. And it hit *every* model, so a report
that "embeddings are down" understates it.

Two guards now exist, because either alone is insufficient:

- **Build time** — the build stage records its resolved `libc6` version to
  `/usr/local/share/llama-abi/libc6.version`, and the runtime stage `COPY`s that marker *before*
  its `apt-get` line and installs that exact version. The COPY placement is not cosmetic: it
  creates the BuildKit dependency edge that forces the build stage to finish first. Without it
  the two `apt` transactions are independent graph vertices and may resolve different snapshots
  of a moving index — which is why simply adding `libc6` to the runtime package list would not
  have been enough.
- **Boot time** — `entrypoint.sh` runs `llama-server --list-devices` and *fails closed* on a
  non-zero exit, printing the loader error plus a diagnostic. It captures the output into a
  variable before testing, because a pipeline reports its last command's status: the original
  `llama-server ... | sed` returned 0 even as `llama-server` returned 1.

Verify the pin from a running container — these two must agree:

```bash
docker exec inference-engine-llama dpkg-query -W -f='${Version}' libc6
docker exec inference-engine-llama cat /usr/local/share/llama-abi/libc6.version
docker exec inference-engine-llama ldd -r /usr/local/bin/llama-server   # no unresolved symbols
```

The durable lesson is the one in `AGENTS.md`: a green healthcheck is not evidence. Run
`smoke-inference.sh` after every rebuild.

## A reranker is not an embedding model

The most expensive mistake available here, because every structural guard passes it.

LM Studio serves cross-encoder rerankers through `/v1/embeddings` as `type: embeddings`, and
they return properly normalised vectors at exactly the widths real embedders use — 1024d for
`bge-reranker-v2-m3` (colliding with `bge-m3`) and 768d for `bge-reranker-base` (colliding with
`nomic-embed-text`). Measured (61 parallel Wikipedia articles, en/fr/ja): cross-lingual P@1
collapses to 19.7%/13.1% against 100%/98.4% for the same model family used correctly, and
ordering is inverted — see `docs/local-inference.md` for the full numbers.

The same weights score correctly through a real rerank endpoint — this container's `/v1/rerank`
puts an on-topic document at a strongly positive logit and irrelevant ones strongly negative. It
is the endpoint, not the model. Mark rerankers **Rerank** in Crucible's model capabilities so
they stay out of the embedding picker — `config.yaml`'s `bge-reranker-v2-m3-Q8_0` model is
reachable only via `--reranking` + `/rerank`, never through `/v1/embeddings`.

## Wire contract note

llama.cpp's `/v1/rerank` returns **raw logits** — negative, unbounded (e.g. `+1.66`, `−11.03`).
Infinity (the CPU reranker) returns **sigmoid-normalised 0–1** (e.g. `0.6047`). Higher is better
in both, so ordering logic and Crucible's strict parser are unaffected, but anything asserting a
0–1 range will be wrong against llama.cpp, and scores are not comparable across servers.

## Vector-space warning

The f16 GGUF served here is **not the same vector space** as the fp32 ONNX weights Infinity
serves, even though both are "bge-m3" at 1024d. Measured: mean cosine 1.0000 and minimum 0.9991
— which looks negligible — while top-10 rank overlap falls to **0.8182**, i.e. one result in ten
changes place. Cosine badly understates this.

Crucible models that explicitly: `chunks.embedding_space` keys on model id *plus* normalised
precision, the vector scan filters by it, and mixing two spaces in one vault is refused at
upsert. So switching the embedding provider to this service is a deliberate act that re-embeds
the vault.

Verify agreement rather than assuming it, with `scripts/embedding-agreement.mjs`. This image's
output was checked against the LM Studio llama.cpp build at the same precision: **MIN cosine
0.999983** across ten texts with a 0.309 control, i.e. the same space, so measurements taken
through LM Studio transfer to this container. This did not change with the llama-swap addition —
the inference binary and its flags are byte-identical to the single-model shape; only the
process that launches it is new.

## Rebuilding / upgrading llama.cpp

```bash
docker build --build-arg LLAMA_CPP_TAG=bXXXXX -t crucible-llamacpp-vulkan:bXXXXX-swap243 \
  llama
```

Bump `LLAMA_CPP_TAG` deliberately and re-run the agreement harness afterwards. A llama.cpp
change to pooling or normalisation would move every vector this serves, and the symptom would be
gradually worse search rather than an error. llama.cpp stays pinned at `b10534` as of this
writing — the llama-swap addition is not a reason to bump it.

An image rebuild is never complete on a green healthcheck — run `llama/smoke-inference.sh`
after every rebuild, not only after a config change. The smoke script is the only thing that
proves the rebuilt image can actually serve; the healthcheck only proves the router process
started.

## The bake-off image (`b10664-swap243`)

`crucible-llamacpp-vulkan:b10664-swap243` is a second, additive image sitting alongside the production tag, built for the qwen4exp bake-off (`plans/qwen4exp-image-and-ple-spike.md`). `unsloth/Qwen3.8-Flash-Next-GGUF` declares `general.architecture: qwen4exp`, and upstream support for that architecture first appears in build `b10664` — at the pinned `b10534` the model cannot be loaded at all. Build it through the documented rebuild path above, overriding only the llama.cpp tag and letting `LLAMA_SWAP_TAG` / `LLAMA_SWAP_COMMIT` default, so the router half of the image stays identical to production's:

```bash
docker build --build-arg LLAMA_CPP_TAG=b10664 -t crucible-llamacpp-vulkan:b10664-swap243 llama
```

A future rebuild of that tag through this same build-arg path inherits the source-build `swap` stage automatically; the existing `b10664-swap243` image is deliberately left as built rather than rebuilt now (`plans/container-critical-high-cve-remediation.md`, "Decisions locked").

**The `Dockerfile` default deliberately stays `b10534`, and the compose service is not repointed.** The new tag exists only as a build-arg override, reachable by the bake-off harness through `BAKEOFF_SCRATCH_IMAGE` (`bakeoff/run-window.sh`). Moving the default instead would drag all eight production models, the embedding agreement harness, and the whole glibc/Mesa ABI surface 130 upstream builds forward for the benefit of one experiment — and both traps above are on record from exactly that class of change. Keeping the experiment in its own tag is what contains the *image* risk. It does not contain the *availability* cost: a bake-off window stops the router regardless of which image it runs.

One sharp edge on that seam: `run-window.sh` reads `BAKEOFF_SCRATCH_IMAGE` only on its `docker run` path. If a `bakeoff-scratch` container already exists but is stopped, the script `docker start`s it with the image and mounts it was *created* with, and the env var is silently ignored. Setting the variable is therefore not enough — `docker rm -f bakeoff-scratch` first, or inspect it and recreate it on an image mismatch.

### Verifying a newly built image — retarget the ABI commands

The ABI commands under "The ABI trap" are `docker exec inference-engine-llama …`. That is the right target for "is the running service pinned correctly" and the wrong one for "did the image I just built come out sound": run verbatim after a build they interrogate the live `b10534` service and pass green no matter how broken the new tag is. Point them at a container created from the tag under test, and assert the image id before believing anything it reports:

```bash
docker images crucible-llamacpp-vulkan --format '{{.Repository}}:{{.Tag}} {{.ID}}'   # the two ids must differ
docker run -d --name qwen4exp-verify --device /dev/dri --group-add 992 --group-add 44 \
  crucible-llamacpp-vulkan:b10664-swap243 sleep infinity
docker logs qwen4exp-verify                                                 # entrypoint's GPU + ABI assertions
docker exec qwen4exp-verify dpkg-query -W -f='${Version}' libc6
docker exec qwen4exp-verify cat /usr/local/share/llama-abi/libc6.version    # must equal the line above
docker exec qwen4exp-verify ldd -r /usr/local/bin/llama-server              # no unresolved symbols
docker exec qwen4exp-verify llama-server --list-devices                     # must exit 0 and name the real GPU
docker rm -f qwen4exp-verify
```

`sleep infinity` is not a way around the guards, it is the way through them: `entrypoint.sh` ends in `exec "$@"`, so the `PHYSICAL_DEVICE_TYPE_(DISCRETE|INTEGRATED)_GPU` assertion and the fail-closed `--list-devices` check both run first and land in `docker logs` — the container only reaches `sleep` if the image passed them. The device and group flags mirror the compose service (host `render`=992, `video`=44; re-read them with `getent group render video`), and are the same shape `run-window.sh` gives `bakeoff-scratch`. Never set `CRUCIBLE_ALLOW_CPU_VULKAN=1` to make this pass — that is the llvmpipe trap wearing a bypass flag. None of this replaces `smoke-inference.sh`, which remains the normative post-rebuild gate and is router-scoped by construction (`127.0.0.1:4811`, the `bge-m3` / `bge-reranker-v2` aliases through llama-swap); a scratch container cannot satisfy it, so a new bake-off tag earns a response-body probe of its own *in addition to* that smoke, not instead of it.

### Confirming an architecture is actually compiled in

Grep the library, never the binary. `/usr/local/bin/llama-server` is a ~17 KB ELF shim, and grepping *it* for an architecture name returns zero for every architecture, including ones that demonstrably work — measured here, `qwen3next`, `qwen3moe`, `nextn` and `gemma3n` all come back 0 from the shim on an image that serves them fine. `ldd` names three `libllama*` libraries, so match `libllama\.so` specifically; a looser pattern captures all three, and every read after it fails on a filename that is really three filenames. And always ask about a known-good architecture in the same invocation, because a probe that finds nothing is the expected output of a broken probe:

```bash
docker exec qwen4exp-verify sh -c '
  lib=$(ldd /usr/local/bin/llama-server | awk "/libllama\.so/ {print \$3}")
  for a in qwen4exp qwen3next qwen3moe nextn gemma3n; do
    printf "%-10s %s\n" "$a" "$(LC_ALL=C grep -acF "$a" "$lib")"
  done'
```

Measured: `b10534` reports `qwen4exp` 0 against controls `qwen3next` 5, `qwen3moe` 5, `nextn` 15, `gemma3n` 5; `b10664` reports `qwen4exp` 6 against 5, 5, 16, 5. If the controls come back 0, the probe is broken and the image is not what has been measured.

The same technique answers finer questions than "is the architecture present", because llama.cpp's MTP assertions name their architecture inside the string literal (`"QWEN3NEXT MTP requires n_layer_nextn > 0"` and its siblings). At `b10664` no such string mentions `QWEN4EXP`, and the architecture's own symbols carry no NextN member — so the NextN/MTP draft head for qwen4exp is **not** in this build, and `--spec-type draft-mtp` is not an available arm for it. What the qwen4exp symbols do carry is `build_ple`, alongside the `per_layer_token_embd` tensor name and the `%s.ple.*` metadata keys — which is exactly the handle the on-disk PLE measurement needs.

## Upgrading llama-swap

```bash
docker build --build-arg LLAMA_SWAP_TAG=vNNN --build-arg LLAMA_SWAP_COMMIT=<full 40-char sha> \
  -t crucible-llamacpp-vulkan:b10534-swapNNN llama
```

Pinned to ≥v242 (the release that fixed a TTL-unload deadlock) by tag *and* commit SHA. Bump `LLAMA_SWAP_TAG` and `LLAMA_SWAP_COMMIT` together, deliberately, never independently — resolve the new tag's full commit SHA via `git ls-remote https://github.com/mostlygeek/llama-swap.git refs/tags/vNNN` or the GitHub refs API; if `vNNN` is an annotated tag, the SHA to pin is the *commit that tag points at*, not the tag object itself, so use `git ls-remote --tags` and take the peeled `^{}` line for that tag rather than its own line. There is no checksum argument any more — the stage builds llama-swap from source under a pinned Go toolchain rather than downloading and verifying a release asset, so the audited full commit SHA is what pins the build, never a hash.

An image rebuild is never complete on a green healthcheck — run `llama/smoke-inference.sh`
after every rebuild, not only after a config change. The smoke script is the only thing that
proves the rebuilt image can actually serve; the healthcheck only proves the router process
started.

## Migration status

**Cutover complete (2026-07-26).** `crucible-inference` is the live and only inference path:
the compose service runs always-on in `fleet-tools/fleet/compose.fleet.yml`, Crucible's two
retrieval provider entries pointed at `127.0.0.1:4806` (repointed to `4811` on 2026-07-27,
when the transitional 4806 publish was retired), and the smoke test plus the
chat-evicts-retrieval interleave (two evict→reload cycles, byte-identical embeddings after each
reload, no amdgpu errors) passed against the real service before anything was retired. That
interleave test used gemma-4-12B as a throwaway chat member to prove the eviction mechanics —
`config.yaml` did not yet carry a real `chat` group.

**Chat models added, not yet part of "cutover complete" above.** `config.yaml` now configures
`gemma-4-12b` and `nemotron-4b` in a real `chat` group (see the model table and VRAM fit math
earlier in this file); this authoring pass did not start the container, load either model, or
touch the live service — the orchestrator reloads `crucible-inference` and runs
`smoke-inference.sh`'s new chat-completion checks against it separately. Once that passes,
adding a plugin-side chat provider entry (`openai-compatible`, base URL
`http://127.0.0.1:4811/v1`, model id `gemma-4-12b` or `nemotron-4b`) is a Crucible settings
change, not a code change.

Retired in the same cutover, all recoverable from git history if ever needed:

- The `systemd/` socket-activation apparatus in this directory (unit files,
  `crucible-inference-ctl`, `install.sh`).
- The per-model compose services `crucible-embed-gpu`/`crucible-rerank-gpu` (4804/4805).
- The Infinity CPU pair `crucible-embedder`/`crucible-reranker` (4802/4803) and their HF cache
  volumes. For a host with no usable GPU, the recipe survives as route (d) in
  [`docs/local-inference.md`](../../docs/local-inference.md).
