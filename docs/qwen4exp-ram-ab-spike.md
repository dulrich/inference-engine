# RAM A/B/C spike — closeout: EXPO helps, four DIMMs untested

WP-7 closeout of the `qwen4exp-ram-ab-spike` plan. Two states measured to completion (A: 2-DIMM JEDEC, B: 2-DIMM EXPO); the third (C: 4-DIMM) is **untested at JEDEC — deferred by the user**, not "not viable" (see "State C"). Raw records: `llama/bakeoff/records/qwen4exp-ram-2dimm-jedec-v1/`, `llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/`. Window narratives: the `ram-ab-wp-2-report` dispatch record (A), the `ram-ab-wp-4-report` dispatch record (B). State captures (outside the repo): `~/.local/state/inference-bakeoff/ram-ab/{a-2dimm-jedec,b-2dimm-expo,c-4dimm}/`.

## Verdict

1. **Faster RAM (A → B, JEDEC 4800 → EXPO 6000 MT/s) measurably helps.** Host memory bandwidth and gather rate both move a clean **+16%/+17%**, well outside their own run-to-run dispersion (±1% across three membench runs in either state). Decode at 16k context — the metric with the largest, most stable sample (9 needle trials per cell per state) — moves **+7.6% (`-cmoe`) and +8.0% (`-ncmoe 38`)**, also outside its own trial-to-trial spread (`-cmoe` 14.16–14.39 → 15.11–15.64 tok/s; `-ncmoe 38` 15.22–15.74 → 16.37–17.08 tok/s — the two ranges do not overlap in either case). The 64-token-cap "hot" timing rounds (n=2 per cell per state) point the same direction for three of four qwen4exp cells (`-cmoe` c4k +6.2%, `-ncmoe 38` c4k +5.1%, `-cmoe` c16k +4.3%) but are **inconclusive at this measurement depth** for `-ncmoe 38` c16k (−2.2%, but B's two hot rounds themselves span 16.91–18.71 tok/s — the "delta" sits inside a single cell's own round-to-round noise; that same cell's 9-trial needle figure is the one that resolves cleanly, +8.0%). TTFT does not move in a consistent direction (both states cluster 145–229 ms hot). **Net: EXPO is a real, modest win, clearest at 16k context and in raw bandwidth, for the cost of a BIOS toggle and a reboot.**
2. **More RAM (B → C, four DIMMs) is untested, at any clock.** The user's mixed-kit four-DIMM EXPO attempt failed to POST/train; the auto/JEDEC retry the plan's WP-5 protocol calls for before concluding anything was **not attempted** — the user exited BIOS with EXPO still enabled and pulled the spare DIMMs instead. Per the plan's own convergence rule (cycle-2 M1), an EXPO-training failure and a four-DIMM-topology failure are different verdicts and are never conflated: this is the former, not the latter. Every state-C field below is "not collected."
3. **The GPU-resident baselines are still comparable.** The control cell (`mxdp-qwen36-c128k-f16-timing-mtp-think`, the ordinary GPU-resident production shape) moved hot decode +0.9% (54.59 → 55.09 tok/s median), inside its own round-to-round spread in both states (A 51.5–57.7, B 52.2–58.0 tok/s) and inside the spread of its served-configuration predecessor record (`mxdp-qwen36-c128k-f16-mtp-prod-…-f330211a`, 2026-08-26: cold 52.5, hot 48.7/52.2 tok/s) — no downclock signal, no serving-relevant drift. **VRAM read-backs are not byte-identical, though**: every one of the five cells reads **~31.9 MiB higher in B than in A** (see "VRAM read-backs"), a small, uniform, cell-size-independent offset — consistent with a baseline-VRAM drift between reboots (compositor/driver), not anything RAM-clock- or model-driven. It is two to three orders of magnitude below anything that would change a fit decision, so existing `records/` remain a valid comparison baseline.

## Keep/remove decision (user, 2026-09-07 — recorded here per WP-7's override, not appended to the initiative backlog)

**Keep the 2 × 32 GB Kingston KF560C36-32 pair at EXPO 6000 MT/s (state B).** This is already the live BIOS configuration: the failed four-DIMM attempt's own recovery capture (`c-4dimm/dmidecode-attempt1-failed.txt`) shows the pair still trained at 6000 MT/s after the spares were pulled, so no further action is needed to "keep" it. **Adding the second pair (96 GB, state C) is deferred until a model makes it compelling.** The numbers behind the call:

- EXPO (B) is a banked, zero-risk, zero-further-effort win already measured above (+16%/+17% membench, +4–8% decode at 16k, no downclock on the GPU-resident control) — there is nothing left to do to realize it.
- The 96 GB path is neither measured nor free: the only attempt made (mixed-kit, EXPO-inherited) failed to POST, and reaching a fair test requires either a same-kit purchase or a still-untried auto/JEDEC boot — real further effort with an unmeasured payoff, for a model (`qwen4exp`, 73.45 GiB) whose own working set (state-A `RssFile` up to ~49 GiB observed this window, campaign precedent 45.4 GiB) does not obviously need 96 GB over 64 GB to stop swapping; the swap counters in both A and B windows (`pswpout` deltas of 1.48M and 2.75M pages respectively — see "Host memory") show this model still pages under 64 GB at either RAM clock, so more RAM is a plausible lever, just not yet a proven or urgent one.
- No model on this host today has demonstrated it is bound by anything four DIMMs would relieve; the spike's own framing was "does memory help *this* host serve *this* model," and the answer for the *clock* question is yes (modestly) while the *capacity* question remains open.

## State A — 2-DIMM JEDEC (as-shipped, EXPO off)

Source: `~/.local/state/inference-bakeoff/ram-ab/a-2dimm-jedec/` (`dmidecode.txt`, `free.txt`, `meminfo.txt`, `window-a-uptime.txt`, `window-a-docker-ps.diff`, `membench.jsonl`), plan "Measured inputs" (WP-0 capture).

| Field | Value |
| --- | --- |
| DIMM population | A1, B1 populated; A0, B0 empty |
| Part number | Kingston `KF560C36-32` × 2 (both slots) |
| Rated speed | DDR5-6000 CL36 (EXPO/XMP profile); SPD JEDEC default 4800 MT/s |
| Configured Memory Speed | **4800 MT/s** (both DIMMs) — EXPO off |
| DMI capacity sum | 64 GB |
| `MemTotal` | 65,439,192 kB (WP-0 capture, 2026-09-01, `meminfo.txt`/`dmidecode.txt`); 65,439,188 kB at the window-start reboot (`window-a-free.txt`, `memsample.log` first line, both 2026-09-06) — 4 kB noise across the 6-day gap, same physical value |
| Uptime at window start | 1 min (`window-a-uptime.txt`) |
| Compose roster at window start | 19 containers; diff vs the WP-0 21-container baseline: `bakeoff-scratch` and `home-assistant` absent (`window-a-docker-ps.diff`) — expected (scratch container not yet created; `home-assistant` restart timing), not a confound |
| Swap at window start | 0 used (`window-a-free.txt`) |

**Membench** (3 runs × 3 reps, `membench.jsonl`; swap counters flat, `pswpin_delta`/`pswpout_delta` 0/0/0 on all 3 runs):

| | Run 1 | Run 2 | Run 3 | All-rep min–max |
| --- | ---: | ---: | ---: | ---: |
| Copy median (GB/s) | 19.09 | 19.24 | 19.29 | 12.94–19.35 |
| Gather median (rows/s) | 1,115,483 | 1,115,878 | 1,114,234 | 1,101,251–1,160,637 |

Each run's low all-rep outlier (~13 GB/s) is the first of 3 reps within that run — a first-touch page-fault cost inside the tool itself, not a state effect; the two subsequent reps in every run cluster tightly (19.1–19.35 GB/s A, see B below for the same shape at a higher clock).

## State B — 2-DIMM EXPO (6000 MT/s)

Source: `~/.local/state/inference-bakeoff/ram-ab/b-2dimm-expo/` (`dmidecode.txt`, `free.txt`, `meminfo.txt`, `uptime.txt`, `window-b-uptime.txt`, `window-b-docker-ps.diff`, `membench.jsonl`, `window-b-interruptions.txt`); WP-3 capture landed `3594263`.

| Field | Value |
| --- | --- |
| DIMM population | A1, B1 populated (same slots); A0, B0 empty |
| Part number | Kingston `KF560C36-32` × 2 (unchanged hardware) |
| Configured Memory Speed | **6000 MT/s** (both DIMMs) — EXPO trained cleanly at F31 |
| DMI capacity sum | 64 GB (unchanged) |
| `MemTotal` | 65,439,192 kB (WP-3 capture, `meminfo.txt`) and 65,439,192 kB at the window-start reboot (`window-b-free.txt`, `memsample.log` first line) — **byte-identical to A's own WP-0 reading, 4 kB above A's own window-start reboot reading** (see State A; both deltas are reboot-to-reboot noise, not a RAM-clock effect) |
| Uptime at window start | 3 min (`window-b-uptime.txt`; the WP-3 dmidecode capture itself was at 1 min, `uptime.txt`) |
| Compose roster at window start | Identical to A's (`window-b-docker-ps.diff` is empty) |
| Swap at window start | 0 used (`window-b-free.txt`) |

**Membench** (3 runs × 3 reps, `membench.jsonl`; swap counters flat, 0/0/0 on all 3 runs):

| | Run 1 | Run 2 | Run 3 | All-rep min–max |
| --- | ---: | ---: | ---: | ---: |
| Copy median (GB/s) | 22.22 | 22.38 | 22.49 | 15.05–22.69 |
| Gather median (rows/s) | 1,307,400 | 1,301,306 | 1,296,388 | 1,288,384–1,346,080 |

Delta vs A (run-median average, 19.207 → 22.363 GB/s copy; 1,115,198 → 1,301,698 rows/s gather): **copy +16.4%, gather +16.7%** — both states' own run-to-run spread is ≤1%, so this delta is clean.

Window B carried two interruptions (harness low-memory kill at cell 4, a second wrapper invocation overlap) — see `window-b-interruptions.txt` and the WP-4 report's "Comparability caveats." Neither touched the membench or DMI/`MemTotal` captures above, which were all taken before the queue started.

## State C — 4-DIMM: untested at JEDEC, deferred by the user

Source: `~/.local/state/inference-bakeoff/ram-ab/c-4dimm/dmidecode-attempt1-failed.txt`, `free-attempt1-failed.txt`; plan Status line and commits `e62b709`, `87bd5c4`, `5f575b7`.

**What was attempted.** The user powered down, installed 2 × 16 GB Patriot Viper Venom `PVVR516G60C30` (DDR5-6000 CL30) in the empty A0/B0 slots beside the resident KF560C36-32 pair, and booted with the BIOS memory profile inherited from state B (EXPO on), per the plan's WP-5 protocol. **The board failed to POST/train with EXPO enabled at four DIMMs.** The plan's WP-5 protocol calls for one retry at auto/JEDEC before concluding anything (cycle-2 M1, precisely to avoid conflating an EXPO-training failure with a four-DIMM-topology failure) — **that retry was not attempted.** The user exited BIOS without resetting the EXPO setting and pulled the two spare DIMMs. The recovery capture confirms the board fell back to the KF560C36-32 pair alone, still configured at 6000 MT/s (`dmidecode-attempt1-failed.txt`: both installed-DIMM blocks show `Configured Memory Speed: 6000 MT/s`; the A0/B0 slots read `Size: No Module Installed`) — state B, intact.

**Why this is not a viability verdict.** `PVVR516G60C30` is a different vendor, IC, and timing set (CL30) from the resident `KF560C36-32` (CL36); a four-DIMM EXPO attempt necessarily applies one kit's XMP/EXPO profile to both pairs, which is not how the mismatched kits are individually rated. An EXPO-training failure under that mismatch says nothing about whether four DIMMs would train at auto/JEDEC — the only fair test of the *topology* (the board's stock 3600 MT/s four-dual-rank-DIMM spec) rather than of the *overclock profile*. Per the plan's cycle-2 M1 disposition, this spike therefore closes with the verdict:

**C: untested at JEDEC — deferred by the user.**

Every field below is **not collected**: DMI capacity sum at 4 DIMMs, four-DIMM `MemTotal`, four-DIMM configured speed, membench at C, and all five cells' timing/VRAM/needle figures at C. WP-6 (Window C) was never run; `llama/bakeoff/queues/qwen4exp-ram-4dimm-v1.json` exists (WP-1 onboarding) but has no corresponding `records/` directory.

## Per-cell measurements, A vs B

The four `qwen4exp` cells are copied verbatim from `qwen4exp-viability-v1` (state-A conditions, the campaign's own committed measurement — see `docs/qwen4exp-bakeoff-results.md`); the control cell's served-configuration predecessor is `records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-mtp-prod-…-f330211a/` (timing fields only, per plan cycle-1 M3 — different cell identity, `spec_decode: null` there vs `mtp` here).

### VRAM read-backs (`vram.loaded_bytes`, baseline before each cell's own load)

| Cell | A loaded | B loaded | Δ (B−A) |
| --- | ---: | ---: | ---: |
| `q4x-cmoe-c4k-f16` | 5.054 GiB | 5.085 GiB | +0.0311 GiB |
| `q4x-ncmoe38-c4k-f16` | 13.980 GiB | 14.011 GiB | +0.0311 GiB |
| `q4x-cmoe-c16k-f16` | 5.899 GiB | 5.930 GiB | +0.0312 GiB |
| `q4x-ncmoe38-c16k-f16` | 14.825 GiB | 14.856 GiB | +0.0312 GiB |
| control | 12.150 GiB | 12.181 GiB | +0.0312 GiB |

**Not byte-identical, as flagged in Verdict (3).** Every cell reads ~31.9 MiB (33,443,840–33,460,224 bytes) higher in B, a constant offset independent of cell/model size — i.e. it tracks the *baseline* VRAM-in-use reading before each load, not the model's own footprint (weights+KV delta per cell is within a few hundred KiB of identical A vs B; the `vram_arithmetic` verifier check passes on all 5 cells in both windows). One cell in B (`q4x-cmoe-c4k-f16`, the window's first cell) recorded a lower raw baseline (0.514 GiB, the true pre-window reading) than the other four B cells (0.577 GiB) — its own `loaded_bytes` still lands on the same +31.9 MiB offset from A, confirming the offset is a total resident-VRAM drift, not a per-cell baseline artifact. Most plausible source: compositor/driver VRAM claim varying slightly between the two reboots — three orders of magnitude below the card's 15.92 GiB capacity and below anything that would move a fit decision.

### Timing — 64-token-cap rounds (cold + 2 hot; TTFT is the hot rounds' `hot_ttft_ms`)

| Cell | A: cold / hot1 / hot2 (tok/s) | B: cold / hot1 / hot2 (tok/s) | Hot-median Δ | A hot TTFT (ms) | B hot TTFT (ms) |
| --- | --- | --- | ---: | --- | --- |
| `q4x-cmoe-c4k-f16` | 12.03 / 14.67 / 15.74 | 12.88 / 15.30 / 17.00 | +6.2% | 294 / 194 | 175 / 194 |
| `q4x-ncmoe38-c4k-f16` | 17.37 / 17.30 / 18.03 | 18.77 / 18.58 / 18.53 | +5.1% | 178 / 157 | 170 / 169 |
| `q4x-cmoe-c16k-f16` | 16.55 / 17.01 / 16.57 | 17.19 / 17.78 / 17.25 | +4.3% | 190 / 177 | 195 / 171 |
| `q4x-ncmoe38-c16k-f16` | 18.24 / 18.30 / 18.14 | **11.25\*** / 16.91 / 18.71 | −2.2%† | 187 / 164 | 229 / 145 |
| control | 50.77 / 57.68 / 51.51 | 52.92 / 52.22 / 57.96 | +0.9% | 160 / 171 | 150 / 162 |

\* B's cold round on `q4x-ncmoe38-c16k-f16` (r3, after two window interruptions) was a true cold load — the control cell's 35B mmap had displaced the expert pages in between (WP-4 report). Not comparable to A's cold round, which followed its predecessor cell directly from a warm page cache. † Inside this cell's own hot-round spread in B (16.91–18.71 tok/s spans the −2.2% delta) — **inconclusive at this measurement depth** for the 64-token-cap metric specifically; see the same cell's 16k needle-trial figure below, which resolves cleanly in the other direction.

### Timing — 16k needle-trial decode/prefill (9 trials per cell per state: 3 tasks × 3 positions)

| Cell | A decode (min–median–max) | B decode (min–median–max) | Δ (median) | A prefill (min–median–max) | B prefill (min–median–max) |
| --- | --- | --- | ---: | --- | --- |
| `q4x-cmoe-c16k-f16` | 14.16–14.34–14.39 | 15.11–15.43–15.64 | **+7.6%** | 99.5–112.8–116.8 | 104.5–119.0–122.9 |
| `q4x-ncmoe38-c16k-f16` | 15.22–15.56–15.74 | 16.37–16.80–17.08 | **+8.0%** | 126.1–130.0–137.6 | 115.8–134.9–139.9 |

Both deltas sit outside either state's own trial range — the cleanest-resolving figures in the spike. Needle recall: **9/9 correct on both cells, both states** (single_needle, multi_needle, passage_qa × positions 0.1/0.45/0.85, capability arm, temp 0) — unchanged by RAM clock.

### Control cell — timing battery only (`battery: ["timing"]`, no 16k/needle by design, plan cycle-2 M2)

| | A | B | Predecessor (`…-f330211a`, 2026-08-26) |
| --- | --- | --- | --- |
| Cold decode (tok/s) | 50.77 | 52.92 | 52.54 |
| Hot decode (tok/s) | 57.68 / 51.51 | 52.22 / 57.96 | 48.73 / 52.22 |
| Hot TTFT (ms) | 160 / 171 | 150 / 162 | 193 / 189 |
| `vram.loaded_bytes` | 12.150 GiB | 12.181 GiB | 12.015 GiB (differs: `spec_decode: null` there vs `mtp` here — different cell identity, timing-comparable only) |

All three cluster in the same 48.7–58.0 tok/s band with no systematic ordering by RAM state — consistent with Verdict (3): no downclock, existing baselines still comparable.

## Host memory (sampler logs)

Source: `memsample.log` in each state directory (10 s cadence, `MemTotal`/`MemAvailable`/`Cached`/`Mapped`/`SwapFree` from `/proc/meminfo`, `pswpin`/`pswpout` from `/proc/vmstat`, per-`llama-server`-process `VmRSS`/`RssFile`/`RssAnon`/`VmSwap` scanned from `/proc`).

| | A (02:36:23–03:24:45Z, 260 samples) | B (03:38:59–05:16:45Z, 315 samples, gap 04:13:51–04:55:51 during the harness-kill interruption) |
| --- | --- | --- |
| `MemAvailable` minimum | 51.11 GiB | 49.37 GiB |
| `llama-server` peak `VmRSS` | 56.52 GiB (03:03:04Z, mid-load into `q4x-ncmoe38-c16k-f16`) | 51.74 GiB (03:57:17Z, late in `q4x-cmoe-c16k-f16`) |
| — peak `RssFile` / `RssAnon` at that sample | 49.08 GiB / 7.43 GiB | 48.10 GiB / 3.67 GiB |
| — peak `VmSwap` at that sample | 0 GiB | 2.97 GiB |
| `pswpout` delta (window start → end) | 1,484,015 pages (~5.66 GiB) | 2,746,117 pages (~10.5 GiB) |
| `pswpin` delta | 205,448 pages | 333,235 pages |

Both windows show the same shape as the campaign precedent (`docs/qwen4exp-bakeoff-results.md` "Host-memory profile"): the 73.45 GiB GGUF against 62 GiB of installed RAM pages several GiB out during the largest cells' loads, steady-state decode does not swap, and `RssFile` (mmap'd expert/PLE pages) dominates `VmRSS` in both states. B's larger `pswpout` total reflects its two interruption-driven reloads (two extra cold starts against the same working set), not the RAM clock — the per-cell timing figures above are the RAM-clock-controlled comparison, not this window-aggregate figure.

**Control-cell process memory: not collected for A, partially collected for B.** A's control cell ran 03:23:52–03:24:00Z (7.4 s wall) — inside the sampler's 10 s cadence, so no sample landed while its `llama-server` process was alive (the three surrounding samples all read `llama-server=0`); this is a genuine gap, not a zero reading. B's control cell (r2, 04:56:02–04:56:23Z) was caught twice: `VmRSS` 10.21 GiB at 04:56:13Z rising to 14.69 GiB (`RssFile` 14.60 GiB) at 04:56:23Z — a mid-load snapshot, not necessarily its peak, since the next sample (04:56:34Z) shows the process already gone (`llama-server=0`, wrapper torn the cell down before the next 10 s tick).

## Verifier summary (both windows)

`qwen4exp-ram-2dimm-jedec-v1`: 22 pass, 1 fail (`prompt_tokens_tolerance`, `q4x-ncmoe38-c16k`, one trial 12,320 vs 13,926 target — the carried calibration caveat from the campaign, see `docs/qwen4exp-bakeoff-results.md` "Measured `chars_per_token`"), 3 unknown (no needle battery on the three timing-only cells), 5 informational (`classifier_distribution`, both c16k cells 9/9). `qwen4exp-ram-2dimm-expo-v1`: identical shape (22 pass, 1 fail on the same cell/trial, 3 unknown, 5 informational). `records_present`, `schema_conformant`, `vram_arithmetic`, `wilson_min_trials` pass on all 5 cells in both windows. Full detail: the `ram-ab-wp-2-report` dispatch record, the `ram-ab-wp-4-report` dispatch record, and each window's `verify_report.md`.

## Figures that could not be traced further than stated

- **A's control-cell process memory** (peak `VmRSS`/`RssFile`/`RssAnon`/`VmSwap`): no sampler sample falls inside its 7.4 s wall-clock window — reported above as a gap, not invented.
- **B's control-cell peak process memory**: the 14.69 GiB `VmRSS` sample at 04:56:23Z is the highest captured value, but the process was torn down before the next 10 s sample, so this may undercount the true peak by up to one sampler interval.
- **State C**: every field is "not collected" by design (see "State C" above) — not an omission.

## Quirk added

One quirk added to the llama area contract (generalizable — a mixed-kit four-DIMM EXPO attempt is not a fair topology test on this board family, and the distinction matters beyond this one spike): "mixed-kit four-DIMM EXPO training failures are not four-DIMM-topology failures — the JEDEC/auto retry is the only fair test."

## Plan status

the `qwen4exp-ram-ab-spike` plan Status set to **IMPLEMENTED**, closing on states A and B; state C recorded as untested/deferred, not failed. Commit list: WP-1 `5bbbaff` (queue onboarding), WP-2 `a50e8d2` (window A), WP-4 `2f0f59b` (window B), plan-record commits `95408bf`, `e62b709`, `87bd5c4`, `5f575b7` (WP-4 landing through the WP-5 outcome and mixed-kit note). Deregistered from the initiative backlog `pending-plans`.
