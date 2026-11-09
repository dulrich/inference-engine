# qwen4exp on-disk PLE spike — measurement and verdict

*WP-3 deliverable of the `qwen4exp-image-and-ple-spike` plan. Measured 2026-08-31/09-01 against `crucible-llamacpp-vulkan:b10664-swap243` (image id `94399d86bbc5`) on `unsloth/Qwen3.8-Flash-Next-GGUF` `UD-Q2_K_XL` at `/models/unsloth/Qwen3.8-Flash-Next-GGUF/`.*

## Verdict

**On-disk PLE is viable. Do not buy the 32 GB DIMMs.** The 26.82 GiB per-layer-embedding table stays on `/models` NVMe as a `CPU_Mapped` mmap, and the model serves at **16–17 tok/s decode and 30–40 tok/s prefill** at `-c 4096` on the RX 9070, using **12.2 GiB of VRAM**. Under a sustained, topically diverse prompt stream the resident working set **converges at ~34 GB** — comfortably inside the 62 GiB of installed RAM — and disk I/O decays toward zero rather than thrashing. The 32 GB hardware decision does not need re-opening, and the `qwen4exp-bakeoff-campaign` plan is cleared to proceed on its own merits.

The premise that motivated the RAM question — "the n-gram embeddings should always live in RAM" — is not what the measurement shows. The table is accessed sparsely (one row per token), so it behaves like a paged lookup structure, not like resident weights.

## Where the 73.44 GiB actually is

Derived from the GGUF tensor directory across all three shards (1224 tensors), independent of any load. This is the fact the plan did not have, and it is why the plan's single-arm thesis could not have worked as written.

| Group | Tensors | Params | Bytes | GiB | Share |
| --- | ---: | ---: | ---: | ---: | ---: |
| MoE expert weights | 144 | 120,795,955,200 | 46,084,915,200 | 42.92 | 58.4% |
| **PLE table** (`per_layer_token_embd.weight`, the `-ot` target) | 1 | 51,200,245,760 | 28,800,138,240 | 26.82 | 36.5% |
| Dense body (attn / norm / embd / output) | 1073 | 4,914,858,880 | 3,937,948,160 | 3.67 | 5.0% |
| Other PLE tensors (`blk.1.ple_*`) | 6 | 32,839,680 | 35,102,720 | 0.03 | 0.0% |
| **Total** | **1224** | **176,943,899,520** | **78,858,104,320** | **73.44** | 100% |

Reconciles against the on-disk total of 78,869,128,864 bytes; the 11,024,544-byte difference is the metadata-only shard 1 plus per-shard headers. The PLE table is `IQ4_NL`, dims `[160, 320001536]`.

**The consequence the plan missed: overriding only the PLE table still leaves 46.6 GiB to place, three times the card.** Arm 1 (the previous session's `-ot` + `-ngl auto` attempt) died of exactly this. A viable arm must move the *experts* off the GPU as well — that is what makes the dense body (3.67 GiB) fit trivially.

## Arms

All arms: `-lm mmap`, `-c 4096`, `--no-warmup`, `-ot 'per_layer_token_embd\.weight=CPU'`, ephemeral container, router models freed via `GET /unload` (no outage). Prefill/decode are `llama-server` `timings` from `POST /completion`.

| Arm | Placement | Load | Vulkan0 weights | Prefill tok/s | Decode tok/s |
| --- | --- | --- | ---: | ---: | ---: |
| A `-ngl 0` | everything `CPU_Mapped` | OK, ~150 s cold | 0 | 5.7 cold → 25.2 warm | 5.9 cold → 7.4 warm |
| B `-ngl 99 -cmoe` | dense on GPU; experts + PLE mmap | OK, 5 s warm | **3372.20 MiB** | 26.3 | 14.6 |
| C `-ngl 99 -ncmoe 38` | + 10 layers of experts on GPU | OK, 5 s warm | **12497.20 MiB** | 31.0–34.9 | 15.5–17.4 |
| (arm 1, prior session) `-ngl auto -ot` | fit aborted, nothing overridden | **FAILED** — `vk::Queue::submit: ErrorDeviceLost` at t+94 s | N/A | N/A | N/A |

Every arm that loaded reported load-success, RSS, page-cache residency, major-fault rate and tok/s; arm 1 is recorded as `N/A` with its failure evidence, per the plan's acceptance criterion.

### `gpu_weights_bytes` (finding B7, measurement half)

The measured GPU-resident **weight** footprint, for the campaign successor's queue field:

- `-cmoe` (all experts off GPU): **3,536,007,987 bytes** (3372.20 MiB)
- `-ncmoe 38` (10 expert layers on GPU): **13,104,263,987 bytes** (12497.20 MiB)

These are weights only. Add ~132 MiB KV (96 + 36, two caches) and ~634 MiB compute buffer at `-c 4096` for the full VRAM figure — arm C totals ~13.3 GiB of the card's 15.92 GiB.

## Sustained diverse-prompt behaviour (the load-bearing measurement)

Arm C config, eight topically unrelated prompts, 128 tokens each. If the on-disk thesis were going to fail, it would fail here — a diverse token stream touches more experts and more PLE rows than a repeated prompt.

| Prompt | Decode tok/s | New major faults | Disk read (MB) | Model RSS (GB) |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 14.29 | — | — | 28.31 |
| 2 | 15.35 | 13,629 | 1066 | 30.24 |
| 3 | 15.52 | 13,395 | 984 | 31.85 |
| 4 | 16.00 | 9,687 | 647 | 32.74 |
| 5 | 16.32 | 7,423 | 419 | 33.11 |
| 6 | 16.44 | 5,883 | 264 | 33.36 |
| 7 | 15.78 | 10,176 | 643 | 33.81 |
| 8 | 16.68 | 4,050 | 150 | 33.94 |

**Throughput rises and I/O decays.** Per-prompt disk reads fall 1066 MB → 150 MB and resident growth falls 1.93 GB → 0.12 GB while decode improves 14.29 → 16.68 tok/s. `VmSwap` stayed 0 throughout. On the repeated-prompt arms the convergence is total: after two repeats `majflt` and `read_bytes` stopped advancing entirely, i.e. **steady-state serving does zero disk I/O**.

## Corrections to the plan and to the prior session's findings

1. **Finding E1 is disproven, not "proven real".** The `-ot` pattern matches correctly. The `tensor … buffer type overridden to …` line is emitted at **debug** level and is invisible at the default verbosity 3 — it requires **`-lv 5`**. The prior session's `grep -c … = 0` was a verbosity artifact, not a match failure. Confirmed twice over: at `-lv 5` the line reads `tensor per_layer_token_embd.weight (27465 MiB iq4_nl) buffer type overridden to CPU`, and 27465 MiB matches the 26.82 GiB derived independently from the tensor directory. The mechanism was first isolated on a 67 MB model in seconds rather than on the 73 GiB one.
2. **The assertion string is build-specific and placement is two-stage.** `-ot …=CPU` is first resolved to `Vulkan_Host`, then downgraded to `CPU_Mapped` because mmap is enabled. Assert on the `CPU_Mapped model buffer size` lines, not on the debug target name.
3. **Readiness literal**: this build logs `llama_server: listening on http://…`, not `server is listening`. A poll loop on the old string hangs against a healthy server — the root contract's "a healthy service has told you nothing" family, in its inverse form.
4. **`-ngl auto` silently abandons fitting when `-ot` is set** (`common_fit_params: … tensor_buft_overrides already set by user, abort`) and continues as a *warning*. Every `-ot` arm must pass an explicit `-ngl`.
5. **`--load-mode none` is a trap on this model**, despite the loader's own hint recommending it against CPU overrides. It defeats mmap and would pull ~69.7 GiB of off-GPU tensors into anonymous memory against 62 GiB of RAM. The hint is written for models that fit; do not follow it here.
6. **The card is 15.92 GiB (17,095,983,104 B), not the ~12.8 GiB the plan assumes throughout.** The extra headroom is what makes arm C's 10 expert layers affordable.
7. **`pgrep` does not exist in this image** (a `! pgrep` guard is therefore always true). Scan `/proc` instead — see `probe.sh` in the spike scratch directory.

## Reproduction

Scratch artefacts live outside the repo at `~/.local/state/qwen4exp-download/spike/` (throwaway, per WP-2's precedent): `tensor_account.py` (tensor-directory accounting), `run-arm.sh` (arm runner), `probe.sh` (`/proc` sampler), `sustained.sh` + `prompts.txt` (diverse-load stress), and the per-arm `arm-*.log` / `arm-*.summary` captures. Container recipe: `docker run -d --name qwen4exp-spike --device /dev/dri --group-add 992 --group-add 44 -v /models:/models:ro -v <spikedir>:/spike crucible-llamacpp-vulkan:b10664-swap243 sleep infinity`.

## Follow-on

- The campaign successor should carry `gpu_weights_bytes` explicitly; without it `gguf_fit._resolve_geometry_and_weights_raw` stats shard 1 alone (10.4 MB of metadata) and classifies a 73.44 GiB model as fitting comfortably.
- `-ncmoe` is the tuning knob worth sweeping in the campaign; decode scaled 14.6 → 17.4 tok/s across 0 → 10 expert layers on the card, and the ceiling was not reached.
