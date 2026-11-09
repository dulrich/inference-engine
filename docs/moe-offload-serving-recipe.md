# MoE-offload serving recipe: `--load-mode none` for CPU-resident experts

**What.** An entry that places weight tensors on the CPU (`-ncmoe`, `-cmoe`, or an `-ot …=CPU` override) should run `--load-mode none` — *unless* the model's non-mmap resident set would exceed host RAM, in which case it keeps `-lm mmap` (+ the default `--tensor-read-lazy`) instead. **Why.** Under mmap, CPU-placed tensors land in a page-cache-backed `CPU_Mapped` buffer; under `--load-mode none` they land in an anonymous pinned `Vulkan_Host` buffer, and prefill — the batched, expert-dense phase — reads roughly 2× faster from the pinned buffer, both at scratch and through the bake-off harness. **When it applies.** Today two production entries qualify: `qwen36-a3b` and `gemma-4-26b-a4b` (added 2026-09-13, the `gemma4-26b-a4b-qat-mtp-ladder` plan WP-4). `qwen4exp`/Flash-Next does not, and stays on the mmap side of the rule (topic 1, below). Deliverable of the `moe-offload-load-mode-and-qwen4exp-fit-math` plan WP-5; all figures below are quoted from already-committed sources, cited in parentheses.

## 1. The rule, its evidence, and why qwen4exp doesn't take it

**The rule.** An entry with CPU-resident weight tensors runs `--load-mode none` unless the model's mmap-resident set exceeds host RAM, in which case it keeps `-lm mmap` (+ default `--tensor-read-lazy`) — the pinned/lazy split (experts pinned, PLE table lazy) is an open upstream item, not something expressible with today's flags (`docs/qwen4exp-ple-ondisk-spike.md:72`; `--tensor-read-lazy` is documented as requiring mmap, `the `moe-offload-load-mode-and-qwen4exp-fit-math` plan).

**Who qualifies today.** Two production entries:

- `qwen36-a3b` (`Qwen3.6-35B-A3B-UD-Q4_K_XL`, `llama/config.yaml:495-524`, landed `9b8a242`) — `-ncmoe 32` of 40 layers, 15,363 MiB of routed experts pinned host-side for the entry's 1800 s ttl on a 62 GB host.
- `gemma-4-26b-a4b` (`gemma-4-26B-A4B-it-qat-UD-Q4_K_XL`, its own `gemma-26b-a4b` group) — the QAT+MTP Gemma 4 26B A4B served at `-c 131072` with **`-ncmoe 18`**, i.e. the routed experts of 18 layers CPU-resident under `--load-mode none`, plus `-ub 2048`, f16 KV, the sidecar MTP draft and the vision mmproj. Live loaded 13.59 GiB against the ≈14.8 GiB headless budget; prefill ≈1.46k t/s, decode 51 t/s with a 125k-token KV attached (1.45× over the no-MTP control), no-think needle recall 22-26/27 at 128k and 27/27 at 32k/64k (`docs/test-matrix-results.md` "## gemma4-26b-a4b-ladder-v1"/"-v2"; recall from v2). Same RAM consequence as `qwen36-a3b`: those 18 layers' experts become an anonymous pinned `Vulkan_Host` buffer rather than evictable page cache, held for the entry's own **1800 s ttl** on the same 62 GB host — two such entries never coexist, because both groups are `exclusive: true`.

`qwen4exp`/Flash-Next keeps `-lm mmap` — its off-GPU footprint (69.7 GiB: 42.92 GiB routed experts + 26.82 GiB PLE, topic 1's fit-math footnote below) exceeds the 62 GiB host (`docs/qwen4exp-ple-ondisk-spike.md:72`).

**Scratch measurement** (`the `moe-offload-load-mode-and-qwen4exp-fit-math` plan, Context table; 2013-token prefill + 150-token decode, temp 0, two runs each):

| entry | recipe | prefill t/s | decode t/s |
| --- | --- | --- | --- |
| `qwen36-a3b` (32/40 layers' routed experts CPU-resident, MTP) | mmap (default) | 877 / 959 | 45.5 / 45.5 |
| same | `--load-mode none` | **1590 / 1738** | 44.6 / 44.4 |
| `qwen38-27b-iq3s` (dense, all-GPU) | mmap (default) | 771 / 812 | 34.8 / 34.9 |
| same | `--load-mode none` | 768 / 789 | 34.7 / 34.5 |

**Harness measurement** (WP-3, `docs/test-matrix-results.md` "## lm-ab-v1", req `req-20260908T182410Z-a5717fca30c169cb`, 5278/5320-token prompts, 3 timing rounds): MoE pair prefill **782.4 → 1889.2 t/s (2.415×)**, cold TTFT 6756 → 2805 ms. MoE decode **+19.5% hot** (mean 42.3 → 50.6 t/s; per-round 37.7/40.9/43.8 vs 58.5/44.0/57.2 t/s) — the pre-registered ±3% band was missed in the favourable direction, but the round-to-round spread is wide under MTP at n=3, so this is recorded as a finding, not part of the rule's justification (the rule rests on the prefill win, which is robust across both scratch and harness measurements). Dense control: prefill 1.000×, decode 1.002× — `-lm none` is inert for an all-GPU entry, confirming the recipe is MoE-offload-specific, not a fleet rollout.

**VRAM and RAM.** `-lm none` moves nothing between GPU and host: 12.19 vs 12.21 GiB loaded (`docs/test-matrix-results.md` "## lm-ab-v1"). The RAM-side consequence is that the 32 CPU layers' routed experts (15,363 MiB) become an anonymous pinned `Vulkan_Host` buffer instead of evictable page cache, for the entry's 1800 s ttl, on a 62 GB host (`llama/config.yaml:480-495`).

**What this does not say.** The decode gain is a single 3-round window under MTP speculation, not a repeated-measurement result — treat it as "at least not worse," not as a second load-bearing win alongside prefill.

**Fit-math footnote (WP-4, `de6faf0`).** `gguf_fit.py` now derives GPU/CPU/lazy placement from GGUF tensor classes under `-ngl`/`-ncmoe`/`-cmoe`/`-ot`, activating only when a CPU-placement flag is present and no `gpu_weights_bytes` override exists (`-lm` and `--tensor-read-lazy` are placement-neutral). **Authoritative tensor-class totals for Flash-Next UD-Q2_K_XL: routed experts 42.92 GiB / 144 tensors, PLE table 26.82 GiB** (`docs/qwen4exp-ple-ondisk-spike.md:15-23`, reproduced independently by WP-4's own bytes-per-type derivation, the `moe-offload-load-mode-and-qwen4exp-fit-math-wp-4-report` dispatch record "Class totals reconciliation"). This plan's own Context-table figures of **45.34 / 29.84 GiB are superseded** — that session's script sized ggml type 20 (`IQ4_NL`) as `Q4_1` (`the `moe-offload-load-mode-and-qwen4exp-fit-math` plan; root cause confirmed in the WP-4 report's "Which class table it reproduces"). One rule beyond the plan text, confirmed correct against llama.cpp behaviour: `token_embd.weight` is always CPU-resident regardless of `-ngl`. Pins: `qwen36-a3b` `-ncmoe 32` derives within +1.71% of the hand-carried 6,620,000,000 B; Flash-Next `-cmoe -ot per_layer_token_embd\.weight=CPU` and `-ncmoe 38` derive within 0.0001% of the spike's measured 3,536,007,987 B / 13,104,263,987 B (the `moe-offload-load-mode-and-qwen4exp-fit-math-wp-4-report` dispatch record "The four ±3% pins").

## 2. Verified-default premises

These are flags we deliberately do *not* set per-entry, because the default already measures as correct — recorded so a future change doesn't re-add them without re-measuring:

- **No `-t` flag.** `llama threadpool init, n_threads = 12` on both scratch entries with no `-t` flag — the build's default already resolves to one thread per physical core on the 7900X (`the `moe-offload-load-mode-and-qwen4exp-fit-math` plan; Decisions locked 4, `:34`). Pinning it adds identity churn for a hypothetical default change, not a measured benefit.
- **`-fa auto` ≡ `on` for this card.** No entry needs an explicit `-fa on` to get fused flash-attention; several production entries carry it explicitly for other reasons (`-ctk`/`-ctv` quantized-KV compatibility, `llama/config.yaml:583,657,691`), but the auto default already resolves to the same behaviour on this Vulkan/RADV card.
- **`-fit on` (the default) is a verified no-op** when every entry sets `-ngl`/`-c`/`-ncmoe`: it reports `projected to use 10922 MiB vs. 13556 MiB free … no changes needed` rather than altering anything (`the `moe-offload-load-mode-and-qwen4exp-fit-math` plan; Decisions locked 3, `:33`). **Rule:** an entry that deliberately leaves `-c` or `-ncmoe` unset carries `-fit off` explicitly, so our own fit math stays the single authority rather than silently deferring to llama.cpp's own fitter.
- **`-ub 512` is llama.cpp's own default** (`--help`, `b10534`: "physical maximum batch size (default: 512)") and is no longer stated in the chat macro (`llama/config.yaml:170-183`, landed `9b8a242`; Decisions locked 5, `:35`) — a consumer that does not override `-ub` still runs at 512. `qwen36-a3b` carries `-ub 2048` per-entry (a user-verified batch-size prefill gain — do not re-litigate) without double-specifying the macro's old `-ub 512`, which previously tripped a harmless `DEPRECATED … only last value will be used` warning.

## 3. RAM knees for on-disk-PLE MoE

Two independent RAM sweeps bound how much host memory an on-disk-PLE model like qwen4exp needs before it starts costing throughput:

- **Codacus sweep** (RTX 3060 / 61 GB, external reference cited in `the `moe-offload-load-mode-and-qwen4exp-fit-math` plan): decode is flat down to 24 GB of RAM; prefill degrades **115 → 34 → 15 t/s at 61 → 32 → 24 GB**.
- **Our own RAM A/B** (`docs/qwen4exp-ram-ab-spike.md`): **+7.6%/+8.0% decode** moving JEDEC 4800 → EXPO 6000 MT/s at 16k context, the cleanest-resolving figures in that spike (Verdict, line 7); `llama-server` peak `RssFile` **48–49 GiB** with **5.7–10.5 GiB paged out** per window against 62 GB installed (`docs/qwen4exp-ram-ab-spike.md:136-143`). The PLE spike's earlier **~34 GB converged working set** (`docs/qwen4exp-ple-ondisk-spike.md` "Sustained diverse-prompt behaviour") was a short-load convergence point, not the campaign's steady state — the RAM A/B's 48–49 GiB peak is the more complete figure. Four-DIMM (96 GB) capacity remains untested (`docs/qwen4exp-ram-ab-spike.md` "Keep/remove decision").
- **MTP caveat.** Codacus reports MTP buying under 1 t/s on their setup, against our own **+64%** (`docs/gfx1201-kv-fa-mtp-findings.md:96-99`: qwen36-a3b MTP decode 29.0 → 47.6 t/s). The difference is architectural, not a contradiction: MTP pays when decode is GPU-bound (our case — MTP verification batches ~3 tokens per pass, amortizing the CPU-offloaded-expert stream), not when decode is itself expert-stream-bound (theirs).

**What this does not say.** The Codacus figures are an external reference, not a measurement run on this fleet's hardware or model — they bound expectations for on-disk-PLE RAM behaviour in general, not a qwen4exp-specific prediction.

## 4. The GenerelSchwerz fork's recipe — what applies here

The GenerelSchwerz llama.cpp fork's "16GB-VRAM-Setup" wiki (external reference, `the `moe-offload-load-mode-and-qwen4exp-fit-math` plan) recommends `--moe-expert-cache-size` / `--load-mode none` / `--cache-ram 0` together. Only the load-mode half applies on this fleet: `--moe-expert-cache-size` and `--cache-ram` are CUDA-only, and production serving here is Vulkan by fleet policy (the root contract's "Quirks": production stays Vulkan, the Mesa version in the base image is load-bearing). `--load-mode none` alone is exactly topic 1's rule — this fleet already independently measured and adopted the flag that transfers, and left out the two that don't.
