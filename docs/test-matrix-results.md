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
