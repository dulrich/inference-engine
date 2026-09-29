# TTS engine bake-off findings (2026-09-29)

Outcome of `plans/tts-engine-bakeoff-spike.md`: which local TTS engine audio-gen starts on, and the design inputs for the inference-engine TTS tier. Raw evidence (untracked) lives in `runs/tts-bakeoff/`: `sweep/` and `sweep-r2/` hold results JSONs, WAVs and `scores.json`; `voices/` and `voices-r2/` hold the frozen references; `pack-combined/` holds the rated pack and `key.json`; `window/` and `window2/` hold the GPU-window journals and notes.

## Decision

- **Starting renderer for both narration and dialogue: Higgs TTS 3 4B** (`bosonai/higgs-tts-3-4b`). The user rated it strongest overall in the blind combined pack (engine B).
- **Runner-up: Chatterbox** (engine A). It is MIT-licensed, uses about half the VRAM, and clones more faithfully by the speaker-similarity proxy. It is the fallback if Higgs's licence, VRAM or speed becomes the constraint.
- **Not adopted:** Kokoro (engine C, "sounds overall robotic"; it has no emotion control, and its stock voices cannot be cloned), MOSS-TTS 8B GGUF (engine D, user-reported "volume glitches where certain lines are almost inaudible"; the only Vulkan-native candidate), IndexTTS-2.5 (first pass: "robot"; slowest, RTF 7.0) and MOSS-VoiceGenerator as a renderer (first pass: "sentence pauses too long", "odd gap"; poor voice stability, see the similarity table).
- **Voice design stays with MOSS-VoiceGenerator.** Designed-then-frozen worked: the user picked a take from 3 designs in window 2 (Patrik teen re-spec: take 1 best, take 3 runner-up, take 2 bad).
- Narration and dialogue do not need different engines: the user's ratings don't separate them.

## Blind ratings (combined pack, 12 clips, rated 2026-09-29)

Each engine got three clips (narration, emotion A/B, characters+dialogue), built by `tts_spike.pack --mode combined`. Patrik segments for the cloning engines come from `sweep-r2` (teen re-design). The user gave a narrative verdict per engine rather than per-clip scores:

| Label | Engine | User verdict |
| --- | --- | --- |
| B | higgs | strongest overall |
| A | chatterbox | possible runner-up |
| D | moss_tts | some volume glitches: certain lines almost inaudible |
| C | kokoro | overall robotic |

The loudness proxy did not reproduce the D glitch. Median segment level: moss_tts −14 to −24 dBFS, which is within the other engines' range. Share of 250 ms windows 12–25 dB below the segment median: moss_tts 0.051, versus 0.046–0.064 for the others. The only clearly quiet segment across all four engines is higgs `d02-patrik` in sweep-r2 (−35.5 dB, about 10 dB under its neighbours). The glitch stays a user observation. Per-segment loudness normalisation belongs in the tier or audio-gen's mastering pass whichever engine is used.

First-pass notes (96 single-segment takes, unblinded; kept as supplementary evidence): kokoro "robot" / "decent voice, sounds female" / "decent for neutral narrative"; indextts "robot"; moss_voicegen "sentence pauses too long", "odd gap"; moss_tts d03 "small child voice, decent as audio, doesn't fit for the character" (a voice-spec problem that led to the rev-2 Patrik re-design); higgs/chatterbox d04 "decent".

## Metrics

All numbers come from device-proven renders on the RX 9070 with `inference-engine-llama` stopped. **RTF here means synth seconds per audio second, so >1 is slower than real time.** Batch size is 1 throughout. VRAM is the card-level sysfs high-water (`mem_info_vram_used`), which includes the desktop baseline. Every engine rendered 16/16 segments with sanity checks ok.

| Engine | Backend | Load s | Load VRAM GiB | Render VRAM high-water GiB | RTF median | Mean WER |
| --- | --- | --- | --- | --- | --- | --- |
| higgs | torch-hip (ROCm 7.2) | 9.3 | 8.72 | 11.41 | 2.30 | 0.057 |
| chatterbox | torch-hip | 10.0 | 4.19 | 8.20 | 1.88 | 0.043 |
| moss_tts | llama.cpp Vulkan (OpenMOSS fork) | 13.3 | 6.22 | 6.30 | 1.12 | 0.013 |
| kokoro | torch-hip | 4.2 | 1.49 | 3.86 | 1.93 | 0.038 |
| indextts | torch-hip | 19.9 | 7.28 | 11.22 | 7.02 | 0.026 |
| moss_voicegen | torch-hip | 13.1 | 12.24 | 13.05 | 0.33 | 0.050 |

ASR is `Systran/faster-whisper-base.en` int8, beam 5, with normalisation recorded in `scores.json`. At this scale WER only catches failures; it does not measure quality. On the sweep-r2 Patrik re-render, mean WER was 0.010 for chatterbox, higgs and moss_tts alike.

Speaker similarity (ECAPA cosine against the frozen reference, emotion A/B takes; cross-chapter consistency = k01 vs k02):

| Engine | c01 neutral | c02 afraid | c03 angry | c04 surprised | ch1~ch5 |
| --- | --- | --- | --- | --- | --- |
| higgs (r2 Patrik) | 0.642 | 0.648 | 0.476 | 0.327 | 0.689 |
| chatterbox (r2) | 0.646 | 0.738 | 0.743 | 0.760 | 0.839 |
| moss_tts (r2) | 0.643 | 0.756 | 0.687 | 0.777 | 0.785 |
| higgs (r1) | 0.466 | 0.448 | 0.262 | 0.506 | 0.816 |
| indextts (r1) | 0.557 | 0.541 | 0.520 | 0.589 | 0.727 |
| moss_voicegen (r1) | 0.197 | 0.073 | 0.049 | 0.251 | 0.031 |
| kokoro (r1, stock voice, no reference) | n/a | n/a | n/a | n/a | 0.848 |

Higgs drifts furthest from the reference under strong emotion (angry/surprised). That is partly the point of the emotion tags, but it is also where cloned-timbre stability gets tested; watch it at book length.

## Tag support (generic IR tag → native control)

| Engine | emotion | intensity | style | pace |
| --- | --- | --- | --- | --- |
| higgs | inline `<\|emotion:surprise/fear/anger/sadness/elation/contentment\|>`; no "ominous" | `<\|prosody:expressive_low/high\|>` | only `quiet` → `<\|style:whispering\|>` | `<\|prosody:speed_slow/fast\|>` |
| chatterbox | none (one scalar) | `exaggeration` scalar | none | `cfg_weight` (slow/normal only) |
| moss_tts | none | none | none | duration tokens |
| kokoro | none | none | none | `speed` |
| indextts | 8-dim `emo_vector` / `emo_text` | `emo_alpha` | none | `duration_factor` |
| moss_voicegen | instruction clause | none | instruction clause | instruction clause |

Higgs is the only candidate with inline categorical emotion tags, and that matches the `[surprised patrik]` goal. Its vocabulary is closed, so audio-gen's director must emit the supported set (or map onto it: "ominous" → e.g. a slow pace plus low expressiveness). Anything else becomes `unsupported` and is recorded, never silently dropped.

## Licence obligations

- **Higgs TTS 3** (Boson Higgs TTS 3 Research and Non-Commercial License, with a Creator Use Grant, README "Creator Use", full terms in LICENSE §II-A, quoted by the user 2026-09-29): the grant **explicitly covers audiobooks**, including monetized ones. The one requirement is an acknowledgment of Boson AI's Higgs Audio, **either in the audio or in the accompanying text displayed prominently** (clearly visible, "not hidden at the bottom of the credits or annotations"). Suggested credit: "This audio was created with Boson AI's Higgs Audio — https://www.boson.ai/higgs-audio". The grant does **not** cover hosting the model behind an API or as a service, redistributing, reselling or fine-tuning it for resale, or embedding it in a product or application; those need a separate commercial licence. The loopback TTS tier serving the user's own audio-gen is treated as personal infrastructure. Exposing it to others, or shipping audio-gen with the model, would cross that line. The use restrictions still apply: no non-consensual voice cloning or impersonation, no fraud, and AI-generated audio disclosed where required. audio-gen's designed-then-frozen voices are synthetic (text-designed, not cloned from real people), which keeps it clear of the cloning restriction; the cast bible should keep it that way.
- Chatterbox: MIT, no obligation.
- MOSS-VoiceGenerator (voice design): check its model licence before any publication; this spike did not audit it.

## Tier design inputs (for the inference-engine TTS tier plan)

- **Backend conflict: the chosen engine is ROCm-only here.** Higgs (and Chatterbox) ran on torch-hip / ROCm 7.2 inside this plan's watchdog-gated windows, so their results are **experimental** evidence. The root contract keeps production serving on Vulkan. A TTS tier on Higgs therefore needs the user's explicit root-contract decision to allow ROCm for this tier. The alternatives are CPU serving (unmeasured) or moss_tts, the only Vulkan-native candidate, which the user rated below both. Across two windows and roughly 60 GPU renders there were zero hazard-signature journal lines, and the watchdog stayed active throughout.
- **VRAM:** Higgs is about 8.7 GiB at load and about 11.4 GiB card high-water while rendering, against a ~12.8 GiB budget. It cannot co-reside with any serious llama-swap model. The tier design has to choose between an exclusive swap group with llama-swap and render windows. **Co-residency with llama-swap is unmeasured** and left to the tier plan.
- **Speed:** RTF ≈ 2.3 at batch size 1 means about 2.3 h of compute per audiobook hour. That fits batch or overnight rendering, not interactive use. Batching was not measured.
- **Segment granularity:** long input gets truncated. The ~60 s `l01-longform` passage came back as 18.4 s from Higgs and 40.0 s from Chatterbox (sanity checks still ok, so it was silent truncation). Requests must be sentence/paragraph-sized, roughly ≤15 s of speech per call. audio-gen's take auditor (ASR against the text) is the guard against this.
- **Cold load:** Higgs loads in about 9 s. Swap-on-demand is affordable if renders are batched per chapter.
- **Voice pipeline:** MOSS-VoiceGenerator (12.2 GiB, fastest at RTF 0.33) runs only at design time. It never needs to be resident next to the renderer.

## Follow-ups (named, not planned)

1. The inference-engine TTS tier plan: Higgs serving, the backend decision above, the swap-group/VRAM design, loudness normalisation, and a co-residency measurement.
2. The audio-gen director/bible/IR follow-on: map the director's emotion vocabulary onto Higgs's closed tag set, and add the Higgs credit to assembly.
