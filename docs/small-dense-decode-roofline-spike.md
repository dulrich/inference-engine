# Small-dense decode roofline spike — verdict

Measurement window and verdict for the `small-dense-decode-roofline-spike` plan (WP-3, 2026-09-04). Question under test (the feedback log 2026-09-03): the fleet dashboard's `model_scoreboard` "Local workstation" decode estimator says small dense models decode at "multi hundreds of tok/s" (up to ~750 for a 0.5 GB file); the highest number ever measured on this card was Ling-3-tiny at ~175. Is the estimate real, and if not, where does it break?

**Verdict: the linear roofline holds down to ~1.1 GB of weights under the plan's `util ≥ 0.5` rule — implied utilization 0.50–0.78 across the eight cells at or above 1.15 GB (four of them, 0.50–0.59, sit slightly *below* the estimator's 0.6 constant; four sit above it) — so high tok/s is real in that range: 230 tok/s at 2.2 GB, 313 at 1.15 GB. Below ~1 GB utilization collapses (0.23–0.40 across the four cells at 0.51–0.81 GB) because per-token fixed cost dominates: decode floors at ~2.7–3.7 ms/token on these 24-layer models regardless of bytes. The "~750 tok/s" figure is an artifact of the missing floor; what this card actually delivered for 0.5–0.8 GB dense files under Vulkan was 269–292 tok/s (Qwen3.5-0.8B) and 370 tok/s (MiniCPM5-1B) — an observed range for these two architectures, not a hardware ceiling.** The proposed estimator form for the fleet-dashboard follow-on is a per-token floor added to the bandwidth term (`ms/token = weightsGB / 0.640 + floor`, model C in "Candidate models") — a hypothesis whose floor constant is architecture-dependent in this data, not a fitted universal.

Evidence is the record tree `llama/bakeoff/records/small-dense-roofline-v1/` (12 cells, `verify_report.md` beside them), the router-served control `llama/bakeoff/records/small-dense-roofline-v1-router-control/control.json`, and the MiniCPM5 load probe `records/small-dense-roofline-v1/probe-minicpm5/`. Records are evidence and are never rewritten; the per-cell table below was produced by `llama/bakeoff/records/small-dense-roofline-v1/sdr-analysis.py` from them.

## Setup

- **Card / estimator profile.** RX 9070 (gfx1201), 16 GB VRAM, 640 GB/s nameplate — the scoreboard's seeded "Local workstation" profile. Estimator under test (`model_scoreboard/apps/api/src/throughput.ts:379-458`): `tok/s = 640 × 0.6 / weightsGB = 384 / weightsGB` (decimal GB, dense, `DECODE_BANDWIDTH_UTILIZATION = 0.6`, no per-quant factor, no ceiling, no floor term for dense models).
- **Serving.** Production image `crucible-llamacpp-vulkan:b10534-swap243` (llama.cpp commit `2b56210`), Vulkan/RADV, scratch `llama-server` per cell in the `bakeoff-scratch` sibling container (`-ngl 99 --jinja -c 16384 -ctk f16 -ctv f16 -fa auto`, server-default sampling — `sampling_arm_effective: null`), router stopped for the window. `--vram-profile desktop-shared` (baseline VRAM-used 2.25 GiB with the desktop up; every cell fits with ≥ 9 GiB predicted margin, so the profile only changes the budget check, not any measurement).
- **Geometry** (`config.timing_geometry` on every record): `prompt_tokens_target: 8000`, `max_tokens: 256`, `prompt_source: fixture-filler` (`harness_fixture._build_filler_text(8000, seed=20260821)`), `instruction_pinned: true` (the cap-exhausting "Continue the text above…" instruction). 3 rounds per cell: 1 cold (empty KV) + 2 hot (identical prompt, prefix-cache hit asserted, `cached/prompt > 0.90`).
- **On-wire depth (deviation from the plan's `≥ 7000` gate).** The 8000-token *target* lands at **5278** prompt tokens on the Qwen3.5 and MiniCPM5 tokenizers and **5329** on Nemotron — the filler's 4.5 chars/token estimate overshoots these tokenizers. The scoreboard's own adapter (`sources/local-capture/capture.ts:47`) documents that its calibration set "runs ~5.3k regardless of the fixture's target, always landing `8k`", and the principal dense calibration records confirm it (`gemma-4-12b-qat-full` 5283 and `qwen38-27b-*-full` 5319 prompt tokens). The queue was therefore left as committed — the *depth* matches the calibration set (5278 vs 5283/5319), which is the comparison the spike exists to make — and the admissibility floor was relaxed to `prompt_tokens ≥ 4000`. That 4000 is a local choice (`capture.ts` assigns every measured v2 depth from 1000 through 12000 to `8k`); it is the depth, not the bucket, that is being matched. Other geometry is *not* identical to the calibration records and is listed so nobody reads it as such: these cells run `-c 16384` (calibration records use each model's served context), the pinned cap-exhausting continuation instruction (calibration used the fixture's "name three topics" instruction), and 2-hot-round v2 aggregation (calibration is v1 warm/hot aggregation). Every other admissibility condition is the plan's: `finish_reason == "length"` and `completion_tokens ≥ 200`.
- **Aggregates.** *Verdict aggregate* (this doc): per cell, the median `observed_decode_tok_s` (`completion_tokens / (last_delta − first_delta)`, TTFT excluded) over the 2 admissible hot rounds; the cold round's decode and its `server_prompt_eval_tok_s` (the only true full-depth prefill reading) are reported beside it. *Scoreboard aggregate* (`capture.ts:328-335`) pools every successful timing round regardless of run state — not used here; the two must not be conflated.
- **Control.** The file the live `nemotron-4b` router alias serves (`NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf`, 3.133 GB) measured twice at identical geometry: through the router (llama-swap :4811, production config, immediately before the window) and as scratch cell `sdr-nemotron-4b-udq4xl-ctl`. **Router 140.15 tok/s / scratch 139.73 tok/s = 1.003** — the two measurement paths agree to 0.3%, so the scratch rows can be read directly against the router-served calibration set with no offset.
- **MiniCPM5 load probe.** `openbmb/MiniCPM5-1B-GGUF` is a `llama`-arch conversion; a pre-window scratch load returned a non-empty completion body (64 tokens, `finish_reason: length`), so no fallback was applied and the gate is 12/12.

## Per-cell results

Sorted by weights bytes. `est` is the current estimator `384 / GB`; `util` is the implied utilization `measured × bytes / 640e9`; `ms/tok` is `1000 / measured`. `prompt_tokens` / `compl, finish` / `adm` are per cell, identical across all three rounds of that cell (the admissibility evidence the plan requires per cell).

| cell | file | bytes | GB | layers | est 384/GB | **measured** (hot median) | ratio | util | ms/tok | `server_eval_tok_s` | cold decode | cold prefill tok/s | prompt_tokens | compl, finish | adm hot |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| `sdr-qwen35-08b-q40` | Qwen3.5-0.8B-Q4_0 | 507,154,688 | 0.507 | 24 | 757.2 | **291.6** | 0.39 | 0.231 | 3.43 | 290.4 | 249.8 | 11838 | 5278 | 256, length | 2/2 |
| `sdr-qwen35-08b-q6k` | Qwen3.5-0.8B-Q6_K | 639,029,504 | 0.639 | 24 | 600.9 | **285.0** | 0.47 | 0.285 | 3.51 | 283.8 | 256.2 | 10650 | 5278 | 256, length | 2/2 |
| `sdr-minicpm5-1b-q4km` | MiniCPM5-1B-Q4_K_M | 688,065,920 | 0.688 | 24 | 558.1 | **369.7** | 0.66 | 0.397 | 2.70 | 368.3 | 325.4 | 10164 | 5278 | 256, length | 2/2 |
| `sdr-qwen35-08b-q8` | Qwen3.5-0.8B-Q8_0 | 811,843,840 | 0.812 | 24 | 473.0 | **269.0** | 0.57 | 0.341 | 3.72 | 267.9 | 240.9 | 11557 | 5278 | 256, length | 2/2 |
| `sdr-minicpm5-1b-q8` | MiniCPM5-1B-Q8_0 | 1,153,529,216 | 1.154 | 24 | 332.9 | **313.3** | 0.94 | 0.565 | 3.19 | 312.1 | 288.6 | 11144 | 5278 | 256, length | 2/2 |
| `sdr-qwen35-08b-bf16` | Qwen3.5-0.8B-BF16 | 1,516,744,736 | 1.517 | 24 | 253.2 | **217.4** | 0.86 | 0.515 | 4.60 | 216.5 | 200.6 | 12255 | 5278 | 256, length | 2/2 |
| `sdr-minicpm5-1b-f16` | MiniCPM5-1B-F16 | 2,166,551,936 | 2.167 | 24 | 177.2 | **230.7** | 1.30 | 0.781 | 4.33 | 229.8 | 222.5 | 10434 | 5278 | 256, length | 2/2 |
| `sdr-qwen35-4b-q40` | Qwen3.5-4B-Q4_0 | 2,583,221,408 | 2.583 | 32 | 148.7 | **123.1** | 0.83 | 0.497 | 8.12 | 122.6 | 119.2 | 3743 | 5278 | 256, length | 2/2 |
| `sdr-nemotron-4b-udq4xl-ctl` | Nemotron-3-Nano-4B-UD-Q4_K_XL | 3,133,118,624 | 3.133 | — | 122.6 | **139.7** | 1.14 | 0.684 | 7.16 | 139.2 | 130.3 | 2569 | 5329 | 256, length | 2/2 |
| `sdr-qwen35-4b-q6k` | Qwen3.5-4B-Q6_K | 3,525,956,768 | 3.526 | 32 | 108.9 | **107.1** | 0.98 | 0.590 | 9.34 | 106.6 | 107.3 | 3392 | 5278 | 256, length | 2/2 |
| `sdr-qwen35-4b-q8` | Qwen3.5-4B-Q8_0 | 4,482,403,488 | 4.482 | 32 | 85.7 | **92.6** | 1.08 | 0.648 | 10.80 | 92.2 | 92.3 | 3821 | 5278 | 256, length | 2/2 |
| `sdr-qwen35-4b-bf16` | Qwen3.5-4B-BF16 | 8,424,393,632 | 8.424 | 32 | 45.6 | **59.2** | 1.30 | 0.780 | 16.88 | 59.0 | 59.2 | 3678 | 5278 | 256, length | 2/2 |

Router-path control (alias `nemotron-4b`, same geometry, 5329 prompt tokens, 3 × 256 `length`): hot median **140.15** tok/s (`server_eval` 139.6), cold 140.05, cold prefill 3147 tok/s.

Side readings: `observed_decode_tok_s` and the server's own `server_eval_tok_s` agree within 0.5% on every cell (the stream timer is not the limiting instrument at 370 tok/s over a 256-token window ≈ 0.7 s). Cold-round decode is 3–12% below hot on the sub-1 GB cells (first-request warmup), identical on the 4B cells.

## Utilization vs bytes

```
util = measured × bytes / 640e9        (0.6 is the estimator's constant)

 0.8 |                                   ·f16-1B                            ·4B-BF16
     |
 0.6 |                       ·1B-Q8              ·nemo    ·4B-Q8
     |                              ·0.8B-BF16       ·4B-Q6K
 0.5 |                                         ·4B-Q40
 0.4 |            ·1B-Q4KM
 0.3 |     ·0.8B-Q6K   ·0.8B-Q8
 0.2 | ·0.8B-Q40
     +----+----+----+----+----+----+----+----+----+----+----+----+----+----+----+----+
       0.5       1.0       1.5       2.0       2.5       3.0       3.5       4.5  ...  8.4  GB
```

Two regimes: above ~1.1 GB every cell sits in 0.50–0.78 (the estimator's 0.6 is a fair central value; BF16 files run *hotter* than 0.6, likely because a BF16 file's byte count is pure weights whereas a Q4 file spends a larger share of its bytes on scales/blocks and dequant work); below ~1 GB every cell is under 0.4, and within the Qwen3.5-0.8B family utilization falls monotonically with bytes (0.34 → 0.29 → 0.23) — the signature of a constant per-token cost, not of a bandwidth limit. (Across families it is not monotonic: MiniCPM5 Q4_K_M at 0.69 GB sits at 0.40, above Qwen3.5-0.8B Q8 at 0.81 GB.)

Fitting `ms/token = a × GB + floor` within each family (least squares):

| family | layers | slope a (ms/GB) | ⇒ effective bandwidth | floor (ms/token) | floor per layer |
|---|---:|---:|---:|---:|---:|
| Qwen3.5-4B (4 quants) | 32 | 1.513 | 661 GB/s | 4.09 | 0.128 ms |
| Qwen3.5-0.8B (4 quants) | 24 | 1.193 | 838 GB/s | 2.78 | 0.116 ms |
| MiniCPM5-1B (3 quants) | 24 | 1.106 | 904 GB/s | 1.93 | 0.080 ms |

The within-family slopes imply *more* than nameplate bandwidth. One hypothesis — **unproven here; no tensor-traffic measurement exists in these records** — is that file bytes overstate the bytes streamed per decoded token because the input embedding table is read one row per token. The tensor sizes are real (read from the GGUF headers): Qwen3.5-0.8B-BF16 has a 248,320-entry vocabulary and a 508,559,360-byte `token_embd.weight` (34% of the 1.52 GB file) with **no separate `output.weight`** — the matrix is tied, and a tied matrix is still streamed in full for the output projection unless the kernel does otherwise, so the hypothesis is weaker for Qwen than the slope suggests; MiniCPM5-1B-F16 has separate 401,080,320-byte `token_embd.weight` and `output.weight` tensors, so at most the input half is exempt there. With three or four quant points per family, slope/intercept confounding and quant-kernel differences can also push an OLS slope above 640 GB/s. Treat the >640 GB/s slopes as an open question for the follow-on, not as justification for any candidate below. What *is* robust is the intercept: 1.9–4.1 ms/token, 0.08–0.13 ms per layer, and notably **two 24-layer families differ by 44% (2.78 vs 1.93 ms)** — the Qwen3.5 (`qwen35`, gated-delta hybrid) layers cost more than MiniCPM5's plain `llama` layers, so a floor constant is architecture-dependent.

## Candidate models

Scored against all 12 cells (mean absolute *percentage* error, MAPE, of predicted tok/s; "small" = the 6 cells under 1.6 GB; `layers` unknown → 40 is an arbitrary scoring assumption used only for the Nemotron control here — `throughput.ts` has no layer-count input today, and the follow-on must define missing-layer behaviour on its own evidence). Produced by `llama/bakeoff/records/small-dense-roofline-v1/sdr-analysis.py`:

| model | mean abs err (all) | mean abs err (small) | worst |
|---|---:|---:|---:|
| current `384 / GB` | 42.4% | 70.0% | +160% (0.8B Q4_0) |
| A: `GB/0.384 + 3.0 ms` flat floor | 31.8% | 32.8% | −50% (MiniCPM5 F16) |
| B: `GB/0.384 + 0.10 ms × layers` | 27.8% | 23.9% | −46% (MiniCPM5 F16) |
| **C: `GB/0.640 + 0.12 ms × layers`** | **13.6%** | **17.1%** | −32% (MiniCPM5 Q8_0) |
| D: `min(384/GB, 300 tok/s ceiling)` | 12.3% | 9.9% | −23% (MiniCPM5 Q4_K_M) |

Model C's signed error is not noise, it is family bias: Qwen3.5-4B +3.1 / −0.1 / −0.4 / −0.7%; Qwen3.5-0.8B −6.6 / −9.5 / −10.4 / −12.4%; MiniCPM5 −31.6 / −31.8 / −30.8%; Nemotron control (40 layers assumed) −26.2%. The 0.12 ms/layer constant is effectively fitted to Qwen3.5 and under-predicts the cheaper `llama`-arch layers by a third.

C keeps the 0.6 utilization only as an *emergent* property (it reproduces the 4B ladder within 1–3%: 127/107/92/59 predicted vs 123/107/93/59 measured) while adding a physical floor. D is the best-scoring on this set but unphysical (a hard ceiling that would need re-measuring per GPU and per architecture). A/B keep the 0.6 constant *and* add a floor, which double-counts the fixed cost. **What this spike supports:** the estimator needs a per-token floor term, and the floor is of order 2–4 ms/token (0.08–0.13 ms/layer) on this card. **What it does not support:** a single universal constant — the two 24-layer families differ by 44% in floor, and none of the candidates was scored against the five existing dense calibration cells (2.8–13 GB) that the current 0.6 is fitted on and which any replacement must preserve. The follow-on plan in the fleet dashboard repo should take **C's *form*** (`ms/token = weightsGB / 0.640 + floor`) as its starting hypothesis, re-score it on this set plus the existing calibration cohort, and choose between a per-architecture floor and a conservative flat one on that evidence — the constant is not settled here.

## Verification gates (WP-3)

Captured outputs for every item below (re-run after the window, verbatim) are in `llama/bakeoff/records/small-dense-roofline-v1/wp3-gates.md`; the window's own stdout is `llama/bakeoff/records/small-dense-roofline-v1/window-a2.log` (and `window-a1-failed.log` for the failed first attempt).

- `gpu-hang-watchdog.service` active before and after the window; zero kernel-log hang-signature lines since the window opened.
- Dry-run: 12/12 `fits` under `desktop-shared`. Image identity: router and `bakeoff-scratch` both `crucible-llamacpp-vulkan:b10534-swap243`, `llama-server` commit `2b56210`.
- Window: 05:11:34–05:13:23Z (1 min 49 s for all 12 cells, cell wall 3–28 s; the first attempt without `--scratch-container` failed at `docker top` on the stopped router container before spawning anything — state re-armed with `batch.py re-arm`, no records written by it). Queue state `completed`, 12/12 `completed`.
- `verify_run.py`: exit 0 — 48 pass / 0 fail / 12 informational / 13 unknown. The verifier did **not** verify queue completion: its `abort_state` check looks for a state file inside the run dir and reports `State file: NOT FOUND` / `Queue state: UNKNOWN`, while this run's state lives at `queues/small-dense-roofline-v1.state.json`. Queue completion is therefore a manual reconciliation, captured in the gates file: `queue_state: completed`, 12 × `completed`. The 12 `prompt_tokens_tolerance` unknowns are the needle-arm check, which this `["vram","timing"]` battery never exercises.
- `llama/smoke-inference.sh`: all checks passed after the router came back.
- Dashboard: `docker exec inference-dashboard bun run apps/api/src/ingest/cli.ts` (36 v2 ingested; the 2 `invalid` dirs are the probe and router-control evidence, which are not cell records) and all 12 `sdr-*` experiment ids present in `GET /api/export/scoreboard-capture` with `config.timing_geometry`.
- `file`/NUL gate: every touched text file reports text and `LC_ALL=C grep -caP '\0'` exits 1 (captured in the gates file).
- Scoreboard ingestion is out of scope (crosswalk lacks the three repos, audit M2) — follow-on registered in the initiative backlog `# Blocked on User`.
