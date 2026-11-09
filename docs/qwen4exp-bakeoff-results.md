# qwen4exp bake-off campaign — results

WP-3 closeout of the `qwen4exp-bakeoff-campaign` plan (a viability probe, not a quant sweep — the campaign's own predecessor and design intent were "can this model class run here, and how does it serve"). See also `docs/qwen4exp-ple-ondisk-spike.md` (the predecessor spike measurement that unblocked this campaign) and the `qwen4exp-image-and-ple-spike` plan (the spike's own plan). Raw records: `llama/bakeoff/records/qwen4exp-viability-v1/`.

## What this is

Four cells, queue `qwen4exp-viability-v1`, run to a completed terminal state in one 44-minute router-outage window (2026-09-01, 06:14:36Z–06:58Z). Image `crucible-llamacpp-vulkan:b10664-swap243`, resolved image id `94399d86bbc5` (full digest `sha256:94399d86bbc53b198da109e90ce5870f60b776a4826c0607e20c2ac0de76b388`). Model `unsloth/Qwen3.8-Flash-Next-GGUF`, quant `UD-Q2_K_XL`, 73.45 GiB across 3 shards (`Qwen3.8-Flash-Next-UD-Q2_K_XL-0000{1,2,3}-of-00003.gguf`). Two placement arms (`-cmoe`, all MoE experts off GPU; `-ncmoe 38`, 10 expert layers on GPU) crossed with two context points (4096, 16384) — the campaign is capped at 16384 by user decision, so nothing above that context was run or is claimed here:

| Cell | `cell_id` | Arm | Context |
| --- | --- | --- | --- |
| q4x-cmoe-c4k-f16 | `q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c` | `-cmoe` | 4096 |
| q4x-ncmoe38-c4k-f16 | `q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295` | `-ncmoe 38` | 4096 |
| q4x-cmoe-c16k-f16 | `q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845` | `-cmoe` | 16384 |
| q4x-ncmoe38-c16k-f16 | `q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3` | `-ncmoe 38` | 16384 |

Every cell shares `--host 0.0.0.0`, `-lv 9`, `-ctk f16`, `-ctv f16`, `-fa auto`, `-ngl 99`, `-lm mmap`, `-ot 'per_layer_token_embd\.weight=CPU'`, `--jinja`, plus its own `-cmoe`/`-ncmoe 38` and `-c`. The c16k cells additionally carry a `needle` battery (`sampling_arm: capability`); the c4k cells carry only `vram`+`timing` (no needle battery — by construction, not omission).

## Methodology

Standard queue-runner execution (`batch.py run`), not a scratch-server probe: `run-window.sh` stopped the live router, ran the four-cell queue to `queue_state: completed` unattended, then restarted the router on its original image and re-ran `smoke-inference.sh` green by response body. `gpu-hang-watchdog.service` was active throughout and zero amdgpu ring/timeout/reset/MES signature appeared in `journalctl -k` across the window. Full timeline, every raw number, and the host-memory investigation are in the `qwen4exp-bakeoff-campaign-wp-2-report` dispatch record (gitignored, main-checkout only); this doc reproduces the load-bearing figures from that report and from the committed records rather than re-deriving them.

## VRAM — per-cell read-backs

Baseline (`vram.baseline_bytes`, pre-window) was **596,418,560 B (0.555 GiB)** at every cell. Card capacity: **17,095,983,104 B (15.92 GiB)**.

| Cell | `vram.loaded_bytes` | GiB | Headroom on the 15.92 GiB card |
| --- | ---: | ---: | ---: |
| q4x-cmoe-c4k-f16 | 5,437,435,904 | 5.06 | 10.86 GiB |
| q4x-ncmoe38-c4k-f16 | 15,021,449,216 | 13.99 | 1.93 GiB |
| q4x-cmoe-c16k-f16 | 6,344,466,432 | 5.91 | 10.01 GiB |
| q4x-ncmoe38-c16k-f16 | 15,928,823,808 | **14.83 (peak)** | **1.09 GiB** |

`gpu_weights_bytes` (the measured GPU-resident weight footprint, carried explicitly per WP-1's onboarding) is 3,536,007,987 B for `-cmoe` and 13,104,263,987 B for `-ncmoe 38` — persisted in each cell's `sweep_cell` block and used by the verifier's `vram_arithmetic` check (all four cells pass, ratios 1.32/1.41 for `-cmoe` and 1.09/1.12 for `-ncmoe 38`, per `llama/bakeoff/records/qwen4exp-viability-v1/verify_report.md`).

## Needle-in-haystack correctness

**9/9 correct on both arms at 16384** (`single_needle`, `multi_needle`, `passage_qa` × needle positions 0.1/0.45/0.85 — capability arm, temp 0, every trial `finish_reason: stop`). On-wire `prompt_tokens` ranged **12,320–14,875** across both cells (`q4x-cmoe-c16k-f16`: 13,327–14,842; `q4x-ncmoe38-c16k-f16`: 12,320–14,875). The c4k cells carry no needle battery, so there is no needle result at 4096 for either arm — this is by design (only the two 16k cells were given the `needle` battery in the queue), not a gap.

## Timing — prefill, decode, TTFT

Server-side `server_prompt_eval_tok_s` (prefill) and `server_eval_tok_s` (decode) from the needle trials (~13.2k–14.9k prompt tokens) and from the 64-token-cap timing rounds:

| | `-cmoe` | `-ncmoe 38` |
| --- | --- | --- |
| Prefill at 16k (needle trials, ~13.9k tok prompts) | 92.2–111.1 tok/s | 117.1–123.3 tok/s |
| Decode at 16k (needle trials) | 12.9–13.4 tok/s | 14.3–14.9 tok/s |
| Decode hot, 64-tok-cap rounds (both cells per arm, c4k and c16k) | 13.7–15.4 tok/s | 16.8–17.3 tok/s |
| Hot TTFT (64-tok cap, cached prefix) | ~183–205 ms | ~179–200 ms |
| Cold-round prefill, first server load of the session (`-cmoe` c4k, cell 1) | 5.2 tok/s | n/a — cell 1 was `-cmoe` |

The 5.2 tok/s cold-round prefill was measured only on cell 1 (`q4x-cmoe-c4k-f16`, the very first `llama-server` process the session started) — a genuine shader-compilation/page-cache-cold penalty. Every later cell's own "cold" round (first request against *that* cell's freshly-started server process) already lands at 22.8–25.7 tok/s prefill, because the GPU shaders and much of the shared dense-body weight data were already warmed by the preceding cell; only the session's true first load pays the full cold cost.

## Measured `chars_per_token` — a live tokenizer calibration, not the queue's pinned estimate

The queue pins `chars_per_token: 4.0` for haystack construction; the haystack's char count is fixed by construction (`_select_contiguous_passages` cuts at `int(target_tokens × chars_per_token)` = 55,704 characters for the 13,926-token target, zero shrink attempts on all 9 of the `-cmoe` probes tabulated here; the campaign's single shrink was on the `-ncmoe 38` cell's passage_qa @0.85), so dividing that fixed character count by each probe's live-tokenizer `trial_meta.haystack_measured_tokens` gives a genuine post-hoc calibration for this tokenizer against this haystack corpus (Gutenberg haystack v2):

| Probe | `haystack_measured_tokens` | ch/tok (55,704 ÷ measured) | on-wire `prompt_tokens` |
| --- | ---: | ---: | ---: |
| single_needle @0.1 | 13,236 | 4.209 | 13,327 |
| single_needle @0.45 | 13,407 | 4.155 | 13,499 |
| single_needle @0.85 | 13,684 | 4.071 | 13,774 |
| multi_needle @0.1 | 14,121 | 3.945 | 14,261 |
| multi_needle @0.45 | 13,655 | 4.079 | 13,795 |
| multi_needle @0.85 | 13,801 | 4.036 | 13,941 |
| passage_qa @0.1 | 13,235 | 4.209 | 13,357 |
| passage_qa @0.45 | 14,198 | 3.923 | 14,329 |
| passage_qa @0.85 | 14,720 | 3.784 | 14,842 |

Pooled over all 9 probes (501,336 characters / 124,057 tokens): **`chars_per_token` = 4.041**, range 3.784–4.209. The queue's pinned 4.0 therefore undershoots the 13,926-token construction target by ~1%, which is what pushes on-wire `prompt_tokens` slightly above `target_tokens` on most probes (13,327–14,842, before needle/question text and chat-template overhead are added on top).

## Host-memory profile — the `-lm mmap` operating cost

Measured mid-window on cell 3 (`q4x-cmoe-c16k-f16`). The 73.45 GiB GGUF (3 shards) sits against 62 GiB of installed RAM; VRAM for this cell was 6.34 GB decimal (0.6 GB baseline + 3.54 GB non-expert weights + 0.53 GB KV + compute buffer). `llama-server`'s `VmRSS` was 49.1 GiB, split as **`RssFile` 45.4 GiB** (the mmap'd expert-weight and PLE-table pages resident in page cache — `/proc/meminfo` showed `Mapped` 47.9 GB, `Cached` 48.9 GB) plus **`RssAnon` 3.7 GiB**; `free`'s "used" figure of 13 GB reflects only the anonymous portion, not the mmap'd file pages. **Side effect: 12.2 GiB of swap was in use** during the window (`llama-server` 2.9 GiB, an unrelated Python service 2.7 GiB, a `model-scoreboard` bun job 2.1 GiB, `open-webui` 0.6 GiB, the remainder other processes) — the memory-pressure cost of running an `-lm mmap` arm alongside the rest of the host's normal resident load, not a defect in the campaign's own cells. Watch `free`'s buff/cache and swap columns for this family of model, not "used".

## Wall vs plan

The plan derived ~3–4 h from an assumed ~26 tok/s prefill. Measured prefill was **4× higher** (92–123 tok/s on both arms at ~13.9k tokens), so the two needle cells — the dominant cost — completed in 22 min (`q4x-cmoe-c16k-f16`) and 19 min (`q4x-ncmoe38-c16k-f16`) instead of the assumed ~44 min each. Total window: **44 minutes**, one pass, no pause/resume, `queue_state: completed`.

## Verifier summary

`verify_run.py` against the completed run: **25 checks — 18 pass, 1 fail, 2 unknown, 4 informational** (`llama/bakeoff/records/qwen4exp-viability-v1/verify_report.md`). The 1 fail is `prompt_tokens_tolerance` on `q4x-ncmoe38-c16k-f16`'s `multi_needle @0.1` trial: `prompt_tokens` 12,320 vs `target_tokens` 13,926 (−11.5%, outside the ±10% tolerance) — that particular haystack draw's live tokenizer count came in denser than the pinned 4.0 ch/tok estimate predicted (a sizing-variance miss in haystack construction, not a serving defect; the probe itself was `correct`). The 2 unknowns are `prompt_tokens_tolerance` on the two c4k cells, which carry no needle battery and so have `n=0` trials with both `target_tokens` and `prompt_tokens` recorded — expected, not a gap. The 4 informational checks are `classifier_distribution` (9/9 correct reported for both c16k cells; empty for both c4k cells, which is again the absence of a needle battery, not a failure).

## Dry-run fit classifications at the 0.555 GiB baseline

The pre-window dry run (`batch.py run --dry-run --no-container-control`) against the headless budget classified:

| Cell | Classification | Predicted margin |
| --- | --- | --- |
| q4x-cmoe-c4k-f16 | `fits` | 10.94 GiB |
| q4x-ncmoe38-c4k-f16 | `fits` | 2.03 GiB |
| q4x-cmoe-c16k-f16 | `fits` | 10.56 GiB |
| q4x-ncmoe38-c16k-f16 | `marginal` | 1.65 GiB |

A `fits` verdict for an `-ncmoe 38` cell is **not an anomaly** — it is the plan's own low-baseline arithmetic (Context ladder / Assumption 4: "at a zero baseline the margin is 2.202 GiB — `fits`, by 0.2 GiB over `FIT_MARGIN_GIB = 2.0`") landing exactly where predicted. That night's live baseline (0.555 GiB) is lower than the 0.793 GiB smoke-record baseline the plan's worked example used, and the classifier's predicted margin moves inversely with baseline, so `q4x-ncmoe38-c4k-f16` crosses the 2.0 GiB `fits` threshold by 0.03 GiB. The queue runner (`run_queue`) never consults this classification — `classify_fit` is called only from `_dry_run_report` — so admission was never gated by it; sufficiency is established by each cell's own live `vram` read-back above, not by the dry-run prediction.

## What this does and does not establish

**Establishes**: both arms serve real 16384-token requests on this card with full needle recall (9/9 on both), `-cmoe` leaves roughly 10 GiB of VRAM free for co-resident models at the cost of ~10% lower decode throughput than `-ncmoe 38` at the same context, and `-ncmoe 38` at 16384 is effectively exclusive-card (1.09 GiB peak headroom on a 15.92 GiB card). The `-lm mmap` PLE-table strategy holds up under this campaign's real needle-probe token stream, not just the spike's synthetic sustained-load test.

**Does not establish**: nothing above 16384 tokens was measured or is claimed — 65536 and 262144 are explicitly out of scope for this campaign (user decision, 2026-09-01) and remain reference arithmetic in the plan's Context ladder for a successor campaign. No serving alias or `llama/config.yaml` entry results from this work — that is a separate decision, not made here. The `chars_per_token` = 4.041 calibration is specific to this tokenizer and this Gutenberg haystack corpus, not a general claim about the model's token density on arbitrary text.
