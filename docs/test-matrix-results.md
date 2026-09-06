# Test matrix results — pointer (overnight-test-matrix-redesign, 2026-08-26)

One-pager for `plans/overnight-test-matrix-redesign.md` WP-6. Records live in `llama/bakeoff/records/matrix-v1-dedicated/` (Tier E, capability arm) and `llama/bakeoff/records/matrix-v1-dedicated-prod/` (Tier E-prod, production arm + reasoning variants); every cell is schema-v2 and ingested into the bench dashboard (`:4810`, `/api/experiments`, aliases `mxd-*` / `mxdp-*`). Queue definitions and tier rationale: `llama/bakeoff/README.md` "Test matrix". All cells: `c 131072`, `battery [timing, needle]`, 3 trials, headless profile (desktop quiesced, VRAM baseline 0.38–0.41 GiB).

## Per-cell verdicts (11 landed cells)

Needle counts are over 27 probes (single + multi needle, positions 0.10/0.45/0.85). `trunc` = `truncated(benign)`: the response cap (256 tokens; 768 for `multi_needle`) was exhausted — for thinking models this is thinking tokens, not a wrong answer. Decode/prefill are medians of the 3 timing rounds / all probes, `server_*` counters.

| Cell | Arm / reasoning | Wall | VRAM loaded | Decode | Prefill | correct / trunc / wrong / degenerate | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gemma-4-12b-qat f16 + MTP | greedy | 59 m | 11.1 GiB | 117 t/s | 903 t/s | 19 / 6 / 0 / **2** | failed (degenerate) |
| gemma-4-12b-qat f16 + MTP | production (1.0/0.95/64) | 60 m | 11.1 GiB | 111 t/s | 898 t/s | 8 / 16 / 0 / **3** | failed (degenerate) |
| qwen36-a3b f16 + MTP | greedy, thinking | 83 m | 12.0 GiB | 54 t/s | 617 t/s | 10 / 17 / 0 / 0 | completed |
| qwen36-a3b f16 + MTP | production, thinking | 85 m | 12.0 GiB | 53 t/s | 616 t/s | 10 / 17 / 0 / 0 | completed |
| qwen36-a3b f16 + MTP | production, `--reasoning off` | 82 m | 12.0 GiB | 37 t/s | 618 t/s | **27 / 0 / 0 / 0** | completed |
| qwen38-27b-iq3s q4_0 KV, fa | greedy, thinking | 135 m | 13.9 GiB | 36 t/s | 377 t/s | 26 / 1 / 0 / 0 | completed |
| qwen38-27b-iq3s q4_0 KV, fa | production, thinking | 132 m | 13.9 GiB | 36 t/s | 381 t/s | 23 / 3 / **1** / 0 | completed |
| nemotron-4b-ud f16 | greedy (no production entry) | 23 m | 5.6 GiB | 126 t/s | 2376 t/s | 25 / 0 / **2** / 0 | failed (needle_failure) |
| ling-3-tiny UD-Q4_K_XL f16 | production (1.0/0.95/20) | 31 m | 6.3 GiB | 131 t/s | 1714 t/s | 25 / 2 / 0 / 0 | completed |
| ling-3-tiny UD-Q6_K_XL f16 | production | 32 m | 8.1 GiB | 132 t/s | 1693 t/s | 25 / 2 / 0 / 0 | completed |
| ling-3-tiny Q8_0 f16 | production | 31 m | 9.1 GiB | 129 t/s | 1723 t/s | 24 / 3 / 0 / 0 | completed |

## Notable verdicts

- **Ceilings re-validated machine-readably.** Every served `-c 131072` config loaded and answered 98–128k-token prompts on the dedicated GPU; peak loaded VRAM (qwen38-iq3s 13.9 GiB) leaves ~1.5 GiB on the 15.6 GiB card. No hang, OOM, or watchdog event across 7 windows / ~14 GPU-hours.
- **Gemma degenerates at ~120k under both arms.** Repetition loops at the 0.85 multi-needle position (2 greedy, 3 production), always inside the thinking channel. Production sampling is *worse* for gemma here: T 1.0 lets thinking run the entire cap on 16/27 probes (`content_channel_never_reached`), vs 6/27 greedy.
- **The response cap, not recall, is what fails the thinking runs.** qwen36 with thinking: 17/27 truncations, every answered probe correct, identical under greedy and production. `--reasoning off` → 27/27 with no truncation. A *higher* thinking budget cannot be tested until the cap is a queue-level key (`max_tokens` counts thinking tokens; cap is fixed per `batch run`) — follow-up plan candidate.
- **MTP throughput depends on thinking.** qwen36 decode drops 53 → 37 t/s with reasoning off (same VRAM, same prefill) — draft acceptance appears higher on reasoning text than on terse answers. Worth a `spec_decode: base` counterpart (Tier B in the shared queue) before drawing conclusions.
- **q4_0 KV on qwen38-iq3s is clean under greedy** (26/27, 1 cap-truncation); production sampling produced the first wrong answer (single needle @0.45, n=1 — sampling noise not excluded).
- **nemotron multi-needle 7/9** (misses @0.10 and @0.85) under greedy; it has no production sampling entry (card-silent `top_k`), so `sampling_arm: production` would silently re-run the greedy cell — deliberately not queued.
- **Ling ×3 at 131072 in production sampling: 24–25/27 correct, zero wrong**, 129–132 t/s decode and ~1.7k t/s prefill regardless of quant; the 2–3 truncations per quant are cap hits on multi-needle. Quant choice costs 6.3 → 9.1 GiB for no measurable recall or speed difference at this context.
- **Verifier calibration (not data issues), carried from Tier E:** `prompt_tokens_tolerance` fails every cell (prompts 98–128k vs the 111,411 target, ±10% band too tight for the haystack builder) and `vram_arithmetic` fails both qwen36 cells (estimator assumes full 22.8 GB weights resident; `-ncmoe 32` measured 12.0 GiB). Both are follow-up candidates.

## Operator notes learned

- Cell records are written only at cell end; the only live signal is the scratch container's `/slots` at its bridge IP (`docker inspect bakeoff-scratch`), never host `:8899`.
- The dashboard `cancel` route is pre-claim only; to free the GPU mid-cell send SIGTERM to the `batch.py run` process (graceful: cell → `failed/battery_error`, container torn down, queue `paused-resumable`; reset the cell to `pending` in the state file to re-run it). `stop` is pause-after-current-cell and is the right tool when the current cell may finish.
- `pgrep -f '^python3 .*batch.py run'` — an unanchored pattern matches the probing shell itself.
- A lingering router model (e.g. `bge-m3` after an Obsidian session, ttl 1800 s) holds ~1 GiB; `GET :4811/unload` before a headless window.

## Not run / resumable (deferred at closeout, user decision 2026-08-26)

- `matrix-v1-dedicated-prod`: 7 pending cells — four timing-only qwen36 cells (mtp|base × thinking|no-think, ~5–8 min each; answers the "serve no-think without MTP?" question raised by the 53 → 37 t/s observation above), then `qwen38-iq3s-prod-nothink` (interrupted, reset), `qwen36-prod-rb128`, `qwen38-iq3s-prod-rb128` (`--reasoning-budget 128`). Resume: `POST :4810/api/control/start {"queueStem":"matrix-v1-dedicated-prod","minutes":360}` on a quiesced desktop (~5.5 h).
- `matrix-v1-shared` (18 cells, Tiers A→B→F→C→D, desktop-shared profile) — never started; the base-vs-MTP (Tier B) and quantized-KV (Tier C) comparisons live there. Start explicitly per window (never the cron default); results append to this table.

## matrix-v1-shared window 1 (`761d080`, req `req-20260906T004242Z-ec6a979eb8684d07`, desktop-shared, 00:43–07:19Z)

Window 1 of `plans/measurement-backlog-recalibration.md` WP-1 against `matrix-v1-shared` (18 cells, Tiers A→B→F→C→D). All 7 terminal cells below ran under the **greedy (capability) arm** — `temperature 0.0 / top_p 1.0 / top_k 1` — not the production-sampling arm reported at lines 19–21 above. The window went `paused-resumable` after cell 7 against a 270-min deadline (total elapsed 00:43–07:19Z = 6h36m). Records: `llama/bakeoff/records/matrix-v1-shared/<cell>/`; state: `llama/bakeoff/queues/matrix-v1-shared.state.json`.

| Cell | Arm / reasoning | Wall | VRAM loaded | Decode | Prefill | correct / trunc / wrong / degenerate | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |
| gemma-4-12b-qat f16 + MTP | greedy | 59 m | 11.7 GiB | 84 t/s | 913 t/s | 19 / 7 / 0 / **1** | failed (degenerate) |
| qwen36-a3b f16 + MTP | greedy, thinking | 86 m | 12.9 GiB | 48 t/s | 607 t/s | 14 / 13 / 0 / 0 | completed |
| nemotron-4b-ud f16 | greedy | 22 m | 6.6 GiB | 98 t/s | 2366 t/s | 25 / 0 / **2** / 0 | failed (needle_failure) |
| ling-3-tiny UD-Q4_K_XL f16 | greedy | 30 m | 7.3 GiB | 135 t/s | 1731 t/s | 26 / 1 / 0 / 0 | completed |
| ling-3-tiny UD-Q6_K_XL f16 | greedy | 33 m | 9.1 GiB | 128 t/s | 1681 t/s | 26 / 0 / 0 / **1** | failed (degenerate) |
| ling-3-tiny Q8_0 f16 | greedy | 31 m | 10.1 GiB | 128 t/s | 1743 t/s | 25 / 1 / **1** / 0 | failed (needle_failure) |
| qwen38-xxs q4_0 KV, fa | greedy | 134 m | 13.8 GiB | 27 t/s | 380 t/s | **27 / 0 / 0 / 0** | completed |

Cell ids (in table order): `mx-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-1cd55be3`, `mx-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8d32c2d0`, `mx-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-74439058`, `mx-ling-ud-q4-c128k-f16-ctkf16-ctvf16-faauto-c131072-de5d379e`, `mx-ling-ud-q6-c128k-f16-ctkf16-ctvf16-faauto-c131072-7a1d7924`, `mx-ling-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-2291772d`, `mx-qwen38-xxs-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c2573bb7`.

Set against the production-sampling ling rows at lines 19–21: **ling UD-Q6 under greedy hits the reasoning cap** — `degenerate`, `finish_reason: length`, 768/768 completion tokens — at multi-needle 0.45, where the production-sampling cell on the same quant was clean (25/2/0/0 there vs 26/0/0/1 here). **Ling Q8 under greedy has 1 wrong** (multi-needle 0.45, `finish_reason: stop`), where the production-sampling cell was 24/3/0/0. **qwen38-xxs q4q4 is a new cell with no prior production-arm counterpart and went 27/27** at a median ~380 t/s prefill, but alone ran ~2h14m wall, which is what carried the window's total elapsed past the 270-min deadline and forced the pause after this cell.

11/18 `matrix-v1-shared` cells remain pending (Tiers F/C/D — the quantized-KV comparisons and the remaining gemma/nemotron/qwen36/ling sweep cells) awaiting further shared windows.

**Verifier caveats:** exit 1, solely from the carried `prompt_tokens_tolerance` calibration check, which fails on **all 7 recorded cells** (prompts measured 98k–130k vs the fixed 111,411 target — the haystack builder's tolerance band is still too tight, a known Tier E carryover). `vram_arithmetic`, `schema_conformant`, `records_present` and `wilson_min_trials` pass on all 7. Report totals: 98 checks, 40 pass / 7 fail / 33 unknown (the 11 pending cells have no records) / 18 informational. Source: `llama/bakeoff/records/matrix-v1-shared/verify_report.md`.

## matrix-v1-dedicated-prod resume (`77e9f86`, req `req-20260906T073557Z-93de0186a0b1391f`, headless, 07:36–13:15Z)

WP-2 resumed the 7 cells left pending at the 2026-08-26 closeout (the "Not run / resumable" bullet above); the queue is now `completed` (14/14 terminal). The four timing-only cells run 3 timing rounds each on a fixed short prompt (28–30 tokens) — no needle probes, so they carry no correct/trunc/wrong/degenerate counts; Decode is reported per round (cold, hot, hot). Records: `llama/bakeoff/records/matrix-v1-dedicated-prod/<cell>/`; state: `llama/bakeoff/queues/matrix-v1-dedicated-prod.state.json`.

| Cell | Arm / reasoning | Wall | VRAM loaded | Decode | Prefill | correct / trunc / wrong / degenerate | Outcome |
| --- | --- | --- | --- | --- | --- | --- | --- |
| qwen36-a3b f16, base (no MTP), no-think — timing only | n/a | ~10 s | 10.0 GiB | 32 / 32 / 32 t/s (cold, hot, hot) | n/a (fixed 30-tok prompt) | n/a (timing only) | completed |
| qwen36-a3b f16, base (no MTP), thinking — timing only | n/a | ~10 s | 10.0 GiB | 32 / 32 / 31 t/s (cold, hot, hot) | n/a (fixed 28-tok prompt) | n/a (timing only) | completed |
| qwen36-a3b f16 + MTP, no-think — timing only | n/a | ~8 s | 12.2 GiB | 44 / 40 / 41 t/s (cold, hot, hot) | n/a (fixed 30-tok prompt) | n/a (timing only) | completed |
| qwen36-a3b f16 + MTP, thinking — timing only | n/a | ~15 s | 12.2 GiB | 48 / 52 / 57 t/s (cold, hot, hot) | n/a (fixed 28-tok prompt) | n/a (timing only) | completed |
| qwen38-27b-iq3s q4_0 KV, fa | production (1.0/0.95/20), no-think | 129 m | 14.1 GiB | 26 t/s | 376 t/s | **27 / 0 / 0 / 0** | completed |
| qwen36-a3b f16 + MTP | production (1.0/0.95/20), reasoning-budget 128 | 78 m | 12.2 GiB | 47 t/s | 649 t/s | 24 / 0 / **3** / 0 | failed (needle_failure) |
| qwen38-27b-iq3s q4_0 KV, fa | production (1.0/0.95/20), reasoning-budget 128 | 131 m | 14.1 GiB | 26 t/s | 380 t/s | 24 / 0 / **3** / 0 | failed (needle_failure) |

Cell ids (in table order): `mxdp-qwen36-c128k-f16-timing-base-nothink-ctkf16-ctvf16-faauto-c131072-60601124`, `mxdp-qwen36-c128k-f16-timing-base-think-ctkf16-ctvf16-faauto-c131072-447e38a2`, `mxdp-qwen36-c128k-f16-timing-mtp-nothink-ctkf16-ctvf16-faauto-c131072-27ef8a46`, `mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5`, `mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-nothink-ctkq4_0-ctvq4_0-faon-c131072-41f57002`, `mxdp-qwen36-c128k-f16-mtp-prod-rb128-ctkf16-ctvf16-faauto-c131072-feb547bf`, `mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-rb128-ctkq4_0-ctvq4_0-faon-c131072-62da3ac9`.

**"Serve no-think without MTP?" — answered: no, keep MTP.** base-nothink decode holds at 32 t/s across all 3 rounds (same as base-think's 31–32 t/s — thinking on/off doesn't move base's number); mtp-nothink decode is 40–44 t/s, a ~25–35% gain over base that survives with reasoning off, even though it's below mtp-think's 48–57 t/s. MTP's no-think floor (40–44 t/s) still clears base's ceiling either way (31–32 t/s) — the answer to the question raised by the earlier 53→37 t/s thinking-vs-no-think MTP drop is that MTP is still worth serving in the no-think configuration.

`mxdp-qwen36-…-mtp-prod-rb128` misses one `multi_needle` probe at position 0.10 (`finish_reason: stop`) and two `passage_qa` probes exhaust the 256-token response cap at positions 0.10 and 0.85 (`finish_reason: length`, 256/256 completion tokens) — for two of the three, an answer-length interaction with `--reasoning-budget 128`, not a recall miss. `mxdp-qwen38-iq3s-…-prod-rb128` misses three probes, all clustered at position 0.45 (two `multi_needle`, one `passage_qa`, all `finish_reason: stop`, well under the cap) — a position-specific failure, not a length-cap artifact.

**Verifier caveats:** exit 1, from carried calibration checks only — `prompt_tokens_tolerance` fails on **all 10 probe cells** in the full 14-cell queue (prompts measured 97k–128k vs the 111,411 target). It is `unknown` on the 4 timing-only cells (no probes by design, so no `target_tokens`/`prompt_tokens` pair exists to check). `vram_arithmetic` fails on the **2 qwen36 MTP production cells** (`mtp-prod` and `mtp-prod-nothink`; the estimator assumes the full ~22.85 GB weight file resident, giving `ratio` ≈ 0.37 against the [0.4x, 3.0x] band, vs `-ncmoe 32`'s measured ~12.5 GiB delta — the same Tier E estimator gap carried forward, not new data). `records_present`, `schema_conformant`, `wilson_min_trials` and `abort_state` pass for all 14 cells. Report totals: 85 checks, 55 pass / 12 fail / 4 unknown / 14 informational. Source: `llama/bakeoff/records/matrix-v1-dedicated-prod/verify_report.md`.

The RAM A/B/C spike (`plans/qwen4exp-ram-ab-spike.md`) remains parked at WP-2, pending a user-named quiet active period with no other agents running against the fleet.
