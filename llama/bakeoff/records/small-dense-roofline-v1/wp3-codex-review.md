codex-b — gpt-5.6-sol / medium — WP-3 verdict review — base bfc5af0

Verdict: **0 blocking / 5 material / 5 minor.**

## Material findings

### M1 — Model C is promoted from an in-sample hypothesis to a universal estimator without sufficient evidence

Evidence: `docs/small-dense-decode-roofline-spike.md:73-83`; the 12 record directories under `llama/bakeoff/records/small-dense-roofline-v1/`.

The reported 13.6% MAPE is arithmetically correct, but it conceals strong architecture-specific bias:

| family/cell | Model C signed error |
|---|---:|
| Qwen3.5-4B ladder | +3.14%, −0.10%, −0.38%, −0.73% |
| Qwen3.5-0.8B ladder | −6.62%, −9.53%, −10.39%, −12.38% |
| MiniCPM5 ladder | −31.61%, −31.84%, −30.82% |
| Nemotron control, assumed 40 layers | −26.19% |

The formula is effectively fitted to Qwen3.5 and then shown to miss every MiniCPM5 quant by about 31% and the control by 26%. The family fits themselves demonstrate the problem: two 24-layer families have fitted floors of 2.777 and 1.932 ms, a 44% difference despite identical layer counts. A universal `0.12 ms × layers` term therefore does not follow from these data.

The candidate comparison also excludes the five existing dense calibration cells that the replacement estimator must preserve, and the `layers unknown → 40` fallback is introduced only in the scoring prose; neither the records nor `throughput.ts` supply a basis for that default. Model C can be presented as a follow-on hypothesis, but the document does not support recommending it as the estimator form without evaluating the existing calibration cohort and defining evidence-based missing-layer behavior.

### M2 — The embedding-table explanation for the super-nameplate slopes is numerically wrong and physically unproven

Evidence: `docs/small-dense-decode-roofline-spike.md:63-69`; `/models/unsloth/Qwen3.5-0.8B-GGUF/Qwen3.5-0.8B-BF16.gguf`; `/models/openbmb/MiniCPM5-1B-GGUF/MiniCPM5-1B-F16.gguf`; `runs/dispatch/small-dense-decode-roofline-spike-wp-2-report.md`.

The Qwen3.5-0.8B GGUF has 248,320 vocabulary entries, not “~150k.” Its actual BF16 `token_embd.weight` tensor is 508,559,360 bytes, not approximately 0.31 GB. More importantly, that GGUF has no separate `output.weight`; the document supplies no evidence that the tied embedding/output matrix is only accessed as a one-row input lookup rather than also used for the full-vocabulary output projection.

MiniCPM5 F16 does have separate 401,080,320-byte `token_embd.weight` and `output.weight` tensors. That supports excluding at most the input embedding from full streaming for that architecture; it does not establish the same mechanism for Qwen.

With only three or four quant points per family, an OLS slope above 640 GB/s can also arise from quant-kernel differences, nonlinear costs, and slope/intercept confounding. The records contain no tensor-traffic measurement. The statement that the slopes “say” embedding bytes are not streamed is therefore not established and should not be used as the physical justification for Model C.

### M3 — Candidate D is written with the opposite operator from the one used to produce its error row

Evidence: `docs/small-dense-decode-roofline-spike.md:75-83`.

A throughput ceiling is `min(384/GB, 300)`, not `max(384/GB, 300)`. Independent recomputation gives:

| D expression | all-cell MAPE | small-cell MAPE | worst |
|---|---:|---:|---:|
| `min(384/GB, 300)` | 12.308% | 9.874% | 23.179% |
| documented `max(384/GB, 300)` | 128.386% | 73.593% | 406.361% |

The published 12.3% / 9.9% / 23% row was calculated with `min`, while the document specifies `max`. As written, the candidate comparison is not reproducible and the discussion of D being best-scoring is false for the documented formula.

### M4 — The headline verdict overstates both the ≥1.1 GB regime and the claimed ceiling

Evidence: `docs/small-dense-decode-roofline-spike.md:5,21-36,59`.

“0.50–0.78, i.e. at or above the estimator’s 0.6” is false. Above 1.1 GB, four cells are below 0.6 utilization:

- MiniCPM5 Q8: 0.564742
- Qwen3.5-0.8B BF16: 0.515193
- Qwen3.5-4B Q4_0: 0.496862
- Qwen3.5-4B Q6_K: 0.589888

The data support “approximately within the estimator’s regime” under the plan’s looser `util ≥ 0.5` decision rule, not “at or above 0.6.”

Likewise, four observations between 0.507 and 0.812 GB do not establish a hardware-wide “real ceiling” of 290–370 tok/s. The upper observation is 369.7 tok/s from MiniCPM5, while Qwen cells in the same size band run 269–292 tok/s. This establishes an observed range for these architectures, not a ceiling for arbitrary 0.5–0.8 GB dense models.

### M5 — Several WP-3 completion gates are asserted without durable evidence, and the verifier did not verify queue completion

Evidence: `docs/small-dense-decode-roofline-spike.md:85-93`; `plans/small-dense-decode-roofline-spike.md:57-60,80-84`; `llama/bakeoff/records/small-dense-roofline-v1/verify_report.md:1-15`; `llama/bakeoff/queues/small-dense-roofline-v1.state.json`.

The verifier report explicitly says `State file: NOT FOUND` and `Queue state: UNKNOWN`. The separate queue-state file does establish `queue_state: completed` and 12 completed cells, but that is a manual reconciliation, not a verifier pass over queue state.

The durable evidence named by the document does not contain outputs for:

- watchdog active/no hang signature;
- dry-run 12/12 `fits`;
- production image identity;
- post-window `llama/smoke-inference.sh`;
- dashboard ingest count and exported experiment-ID check;
- the required `file`/NUL gate.

The document merely asserts these outcomes. The file/NUL gate is omitted even from the verification list. WP-3’s closeout evidence needs paths or captured output sufficient to audit these claims.

## Minor findings

### m1 — The depth deviation does not hide the principal calibration-depth mismatch, but its stated justification is inaccurate

Evidence: `docs/small-dense-decode-roofline-spike.md:14`; `capture.ts:28-37,44-74`; calibration records `gemma-4-12b-qat-full` and `qwen38-27b-*-full`.

The new records’ 5,278-token prompts closely match the principal dense calibration records at 5,283 and 5,319 tokens. The old Nemotron record is the known 233–272-token exception, and the new matched control replaces that comparison.

However, `prompt_tokens ≥ 4000` is not “the scoreboard’s 8k bucket floor.” `capture.ts` assigns every measured v2 depth from 1,000 through 12,000 to `8k`; 4,000 is an additional local admissibility choice. Also, the new scratch records use `-c 16384`, a new continuation instruction, two-hot-round aggregation, and v2 measured-depth bucketing, whereas the calibration records use differing served contexts and v1 warm/hot aggregation. The actual depth comparison is sound; “geometry is identical” is too broad.

### m2 — The claimed monotonic sub-1 GB utilization trend is false across the displayed cells

Evidence: `docs/small-dense-decode-roofline-spike.md:25-28,59`.

Sorted by bytes, utilization is 0.231, 0.285, 0.397, then 0.341. It is monotonic within the three Qwen3.5-0.8B sub-1 GB quants, not across the mixed Qwen/MiniCPM set. The broader conclusion that all four are below 0.5 survives, but the stated signature does not.

### m3 — Model C’s worst-error cell is misidentified

Evidence: `docs/small-dense-decode-roofline-spike.md:80`; raw records.

The largest exact Model C error is MiniCPM5 Q8 at −31.8398%, not MiniCPM5 F16 at −30.8199%. Both round to −32%, but the named worst cell is wrong. MiniCPM5 Q4_K_M is −31.6111%.

The table is also reporting mean absolute **percentage** error, not generic “mean absolute error.”

### m4 — The per-cell table omits fields explicitly required by WP-3

Evidence: `plans/small-dense-decode-roofline-spike.md:60`; `docs/small-dense-decode-roofline-spike.md:21-36`.

The plan requires per-cell `prompt_tokens` and `completion_tokens`/`finish_reason` admissibility in the table. The generated analysis output includes those columns, but the document removes them and replaces them with one global assertion. The raw records confirm the assertion—5,278 or 5,329 prompts and 256/`length` for every round—but the contracted per-cell evidence is absent.

### m5 — The analysis script’s scope and side-effect claims are false

Evidence: `runs/dispatch/sdr-analysis.py:2-4,53-65`.

The script claims “Reads records only; never writes them,” then writes `/tmp/sdr-analysis.json`. It also generates only the per-cell/control table; it does not calculate the least-squares fits or any candidate-model errors. Consequently, the most consequential analytical tables are not reproducible from the cited analysis script.

## Independent numerical audit

All 12 per-cell table rows reproduce from `config.json` plus admissible hot `timing_round` samples at the document’s displayed precision. Representative recomputations:

| cell | raw hot tok/s | recomputed median | `384/GB` | utilization |
|---|---|---:|---:|---:|
| Qwen3.5-0.8B Q4_0 | 282.840100, 300.338390 | 291.589245 | 757.165435 | 0.231064 |
| MiniCPM5 Q4_K_M | 366.838653, 372.573986 | 369.706319 | 558.086062 | 0.397472 |
| MiniCPM5 Q8_0 | 311.636290, 315.023390 | 313.329840 | 332.891438 | 0.564742 |
| Qwen3.5-4B Q4_0 | 123.079470, 123.118154 | 123.098812 | 148.651602 | 0.496862 |
| Nemotron scratch control | 139.584050, 139.874282 | 139.729166 | 122.561590 | 0.684044 |
| Qwen3.5-4B BF16 | 59.278767, 59.213875 | 59.246321 | 45.581916 | 0.779866 |

The independent family OLS fits also reproduce:

| family | slope ms/GB | effective GB/s | intercept ms | intercept/layer |
|---|---:|---:|---:|---:|
| Qwen3.5-4B | 1.513402 | 660.763 | 4.091513 | 0.127860 |
| Qwen3.5-0.8B | 1.193368 | 837.965 | 2.777304 | 0.115721 |
| MiniCPM5-1B | 1.106310 | 903.906 | 1.932141 | 0.080506 |

Model C recomputes to **13.6433% all-cell MAPE** and **17.0607% small-cell MAPE**.

The router control hot median is `(140.147144 + 140.160906) / 2 = 140.154025 tok/s`; scratch is `139.729166 tok/s`; router/scratch is **1.003040**, matching the documented 1.003 ratio.