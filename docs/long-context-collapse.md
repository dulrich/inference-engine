# Long-context collapse: causes and remediations

Sourced reference for `plans/quant-sweep-harness-and-instrument-v2.md` WP-2, written against the
2026-08-23 finding that live-measured `gemma-4-12b-qat` at `-c 65536` (scratch server, production
`llama_server_chat_common` flags — no `-ctk`/`-ctv`/`-fa` overrides, so f16 KV cache and
`--flash-attn auto`) produced correct needle recall up to ~42K real prompt tokens, then a
stochastic degenerate-reasoning cliff (incoherent `reasoning_content`, `content` never reached,
`finish_reason: "length"` even at `max_tokens` 4096) in 2/4 trials at ~43–45K and 2/2 at ~55K,
with 49.6K passing 3/3 (see the plan's Context). This doc does not re-run that measurement or
attribute the finding — WP-3's instrument and the deferred `gemma-qat-longcontext-campaign` do
that. It establishes what is known from primary sources about *why* long-context collapse
happens in general and in this fleet's specific architecture/backend combination, and what an
instrument must therefore be able to distinguish.

Citation convention: every claim below is either **measured** (a cited paper/benchmark ran the
experiment) or **hypothesized** (plausible mechanism, not confirmed for this model/config) —
marked explicitly. Claims with no source found are listed as open questions, not stated as fact.

## 1. Benchmark-methodology findings: NIAH weaknesses, RULER, NoLiMa

**NIAH's original design and its known weakness.** The needle-in-a-haystack (NIAH) test —
insert one fact ("needle") into filler text ("haystack") at a controlled position, ask a
targeted question — originates from Greg Kamradt's
[`gkamradt/LLMTest_NeedleInAHaystack`](https://github.com/gkamradt/LLMTest_NeedleInAHaystack)
(2023) and is now the default long-context smoke test across the industry, including this
fleet's own `run_harness.py`. Its central weakness, **measured** by the NoLiMa authors: NIAH
needles typically share lexical/literal tokens with the question, so a model can pattern-match
on surface overlap rather than genuinely attending across the full context — NIAH is
"indicative of only a superficial form of long-context understanding" (RULER's own framing,
[Hsieh et al., arXiv:2404.06654](https://arxiv.org/abs/2404.06654), COLM 2024).

**RULER — effective vs. claimed context, measured.** RULER
([Hsieh et al., arXiv:2404.06654](https://arxiv.org/abs/2404.06654), NVIDIA, COLM 2024) extends
NIAH with multi-needle variants plus multi-hop tracing and aggregation tasks, evaluated across 17
long-context LMs. Its headline **measured** result: despite near-perfect vanilla-NIAH accuracy,
"almost all models exhibit large performance drops as the context length increases," and of
models claiming ≥32K context, only half maintain acceptable accuracy *at* 32K. RULER defines
"effective context length" operationally — the longest length at which a model still beats a
fixed accuracy threshold (Llama2-7B's own 4K score, 85.6%) — which is the right shape of metric
for this fleet's own "real ceiling vs. nameplate context" framing (`gemma-4-12b-qat` claims
128K–256K per [Unsloth's Gemma 4 QAT docs](https://unsloth.ai/docs/models/gemma-4/qat), but the
2026-08-23 pre-plan run already saw stochastic collapse at ~43K, ~17–34% of the claimed floor —
this specific number is this fleet's own unpublished measurement, not RULER's, and carries no
attribution weight per the plan's Assumptions).

**NoLiMa — literal-match confound, measured.** NoLiMa
([Modarressi et al., arXiv:2502.05167](https://arxiv.org/abs/2502.05167), ICML 2025;
[repo](https://github.com/adobe-research/NoLiMa)) constructs needles with **minimal lexical
overlap** with their questions, forcing latent semantic association instead of literal matching.
Measured result across 13 models claiming ≥128K context: near-perfect at <1K tokens, but even the
best model (GPT-4o) degrades from 99.3% to 69.7% as length grows, and most others degrade further
— "the analysis suggests these declines stem from the increased difficulty the attention
mechanism faces in longer contexts when literal matches are absent." Directly actionable for this
fleet: **`run_harness.py`'s haystack is uniform random-word filler (`_build_filler_text`,
`llama/bakeoff/run_harness.py:119-143`), and its needle sentences are hand-authored, literal**,
so every NIAH-class probe run to date sits on the easy (literal-match) side of the NoLiMa
spectrum — a pass on this instrument does not rule out the failure mode NoLiMa measures.

**Context rot — measured, broader than needle recall.** Chroma's technical report
([Hong, Troynikov, Huber, 2025-07-14](https://www.trychroma.com/research/context-rot)) tested 18
models (GPT-4.1, Claude 4, Gemini 2.5, Qwen3, others) across NIAH variants, LongMemEval
conversational QA, a repeated-words replication task, and semantic-similarity analysis. Measured
findings: performance is non-uniform even on simple tasks as input length grows; lower
needle-question semantic similarity accelerates degradation; even one distractor measurably hurts
recall; and — counterintuitively — **models perform worse when the haystack has logical narrative
flow than when it is shuffled**, which cuts directly against WP-3's planned coherent-Gutenberg-
prose haystack (Decisions locked in the plan) and is worth the instrument keeping shuffle-vs-flow
as a documented, not assumed-safe, variable rather than a silent constant.

**Lost in the middle — measured, positional.** Liu et al.
([TACL 12:157–173, 2024](https://arxiv.org/abs/2307.03172)) established the U-shaped positional
bias: retrieval accuracy is highest for information at the start or end of context and drops for
the middle, holding model architecture constant. WP-3's position sweep (`{0.10, 0.45, 0.85}`) is
consistent with this literature's expectation of a middle trough, but this doc found no source
establishing where that trough falls for a hybrid SWA/global architecture like Gemma's — an open
question (see §3).

## 2. Positional mechanisms: RoPE extension behavior

**The mechanism, measured.** Plain positional interpolation (PI) — linearly stretching every RoPE
dimension by a factor to fit a longer context into the trained rotary range — degrades
high-frequency (short-range) position sensitivity, and the degradation worsens as the stretch
factor grows ([NTK-Aware Scaled RoPE](https://blog.eleuther.ai/yarn/) background, EleutherAI
blog; formalized in [YaRN, Peng et al., arXiv:2309.00071](https://arxiv.org/abs/2309.00071)).
NTK-aware scaling instead scales high frequencies less and low frequencies more, preserving local
token distinctions; YaRN combines NTK-by-parts interpolation with an attention-temperature term
and is reported to extend context to 128K+ "with minimal perplexity degradation" (same source).
[Understanding the RoPE Extensions of Long-Context LLMs](https://arxiv.org/abs/2406.13282) (an
attention-perspective analysis) corroborates that extension methods reshape the attention
distribution rather than leaving it untouched, which is the mechanistic link between "how a model
was extended" and "how it behaves near the edge of that extension."

**Whether this applies to Gemma 4, unclear — open question.** Gemma 3 (the architecture family
gemma-4-12b-qat shares its SWA/RoPE design with, per `llama/config.yaml`'s own comment that
gemma-4-12B is "same base architecture" across quants) reaches its documented context via
**RoPE base-frequency separation, not post-hoc PI/YaRN/NTK rescaling**: global attention layers
use RoPE base 1,000,000 and local (SWA) layers keep base 10,000
([Gemma 3 technical overview, Hugging Face blog](https://huggingface.co/blog/gemma3); corroborated
by [Google's own Gemma 3 explainer](https://developers.googleblog.com/gemma-explained-whats-new-in-gemma-3/)).
This doc found no primary source analyzing whether this split-base design shares YaRN/NTK's known
degradation-vs-extension-factor tradeoff, or is immune to it by construction (global layers are
never "stretched" past a training-time base) — an open question **specific to this model family**,
not resolved by the general RoPE-extension literature above, which studies single-base models.

## 3. SWA interactions — Gemma's 8-global/40-SWA-layer split (the live case)

**This fleet's own measured geometry.** `llama/config.yaml` (lines ~493–506) parsed the live
`gemma-4-12B` GGUF header directly (`attention.head_count_kv`, `attention.sliding_window`, no
model load required) and found: 48 total attention layers, 40 "local" layers with
`sliding_window=1024` tokens and `head_count_kv=8`, and 8 "global" layers with `head_count_kv=1`
that scale with the *full* requested context — a **5:1 local:global ratio**, matching the
independently-documented Gemma 3 architecture ("5 local attention layers with a sliding window of
1024 and 1 global attention layer" per repeating block,
[Hugging Face's Gemma 3 writeup](https://huggingface.co/blog/gemma3)). `gemma-4-12b-qat` is the
same base architecture (QAT weights only), so this geometry applies to the live-measured cliff
directly — the plan's framing of this as "our live case" is accurate.

**Why this matters for a long haystack, measured elsewhere.** Only 8 of 48 layers ever see
position-distant tokens at all; the other 40 physically cannot attend past 1024 tokens back,
regardless of `-c`. Two independent, **measured** findings bear on what a heavy local:global split
does to long-context quality: (1) [Short window attention enables long-term memorization
(arXiv:2509.24552)](https://arxiv.org/abs/2509.24552) found — counterintuitively — that *smaller*
sliding windows can *improve* long-context retrieval in local-global hybrids, because a model that
cannot lean on local attention for retrieval is forced to route retrieval-relevant signal through
the global layers during training; but the same paper also notes the reverse failure mode is real:
"overly aggressive use of sliding window attention, such as very small sliding window sizes or
high SWA:GA ratios, can lead to substantial degradation in model performance, especially in
long-context tasks." (2) The paper explicitly flags that "the impact of window length and the
interplay between local layers and global layers remain under-studied" — i.e. there is no
established curve mapping SWA:GA ratio to a *specific* failure length for *this* class of
architecture. Gemma 4's ratio (5:1, matching the cited high end) is consistent with the
degradation risk category described, not proof that a given ratio produces a cliff at a given
token count.

**Open question — does the 43–55K cliff correlate with the global layers' own effective range?**
This doc found no source (Google's own Gemma 3 paper/blog, or third-party analysis) stating a
measured effective range for Gemma's 8 global layers independent of RoPE base separation (§2).
Whether the observed cliff is a global-layer RoPE-extension effect, an SWA:GA-ratio effect, or
unrelated to either is exactly the attribution question WP-3's instrument and the deferred
campaign exist to answer — not resolved here.

## 4. KV-cache quantization × long context

**General finding, measured.** Long-context tasks are *more* sensitive to KV-cache quantization
than to weight-only quantization at the same bit-width — "long texts (≥4k) are more sensitive to
Weight-only and KV Cache Quantization than short texts (<4k)... most LLMs are more sensitive to
KV Cache Quantization than Weight-only and Weight-Activation Quantization" (survey summarized from
[arXiv:2402.18158, *Evaluating Quantized Large Language Models*](https://arxiv.org/abs/2402.18158)).
A specific low-bit measurement: aggressive (1-bit) KV quantization dropped Llama-3.1-8B's RULER
accuracy from 84.2% to 47.8%, and the degradation was traced to **local candidate-distribution
reordering rather than raw logit-magnitude error** — i.e. quantization doesn't just add noise, it
can flip which token is most-likely
([arXiv:2607.16248](https://arxiv.org/abs/2607.16248)). Error accumulates because every decode
step re-reads the same quantized cache, compounding across a long generation
([arXiv:2607.16248](https://arxiv.org/abs/2607.16248)).

**A specific, silent-failure precedent — measured, different architecture.** DeepSeek-V4's
quantized K-cache path produces "confident gibberish" (e.g. `" newcom wrt wrtêu newcom dialog…"`
in place of `" Paris."`) with **the server reporting healthy the entire time — no error, no
abort** ([`ggml-org/llama.cpp` issue #25382](https://github.com/ggml-org/llama.cpp/issues/25382),
open as of 2026-07-07; root cause identified as quantized-K-cache triggering a Hadamard-rotation
fallback path, `build_raw_attention`, that mishandles the model's sparse-attention specialization;
confirmed on both CPU and CUDA backends). This is the exact "guard passes and the thing is wrong"
shape `llama/AGENTS.md` already curates for this stack — but it is **architecture-specific**
(DeepSeek's sparse-attention + Hadamard-rotation combination), not evidence that Gemma 4's
dense-per-local-window SWA architecture has the same fragility. **Not directly applicable to
the observed cliff**: `gemma-4-12b-qat`'s config carries no `-ctk`/`-ctv` override
(`llama/config.yaml:297-306`), so it runs f16 KV, unquantized — the 2026-08-23 cliff happened
*without* KV quantization in the picture at all. This rules KV-cache quantization out as the
cause of *that specific observation*, but it remains a real, separately-gated risk for any future
sweep cell that does quantize `gemma-4-12b-qat`'s cache.

**A related, first-party finding from this repo.** `plans/ling-3-tiny-context-ceiling.md` (lines
79–97, its own primary-source research) found that **asymmetric K/V quant types silently fall back
to the slower non-fused attention path — no warning, no error logged** — sourced from
`llama.cpp` PR #26608 (BailingMoE3's own merge) and
[discussion #22411](https://github.com/ggml-org/llama.cpp/discussions/22411); that plan's landed
qwen38-27b config avoided the risk entirely by choosing symmetric `q4_0/q4_0` and confirming fused
attention live rather than assuming it (`llama/config.yaml:384-417`). This is the same failure
*shape* as the DeepSeek finding above (silent degradation, healthy-looking server) but from a
confirmed, cited mechanism (fusion fallback) rather than output corruption — a second, independent
reason any KV-quant sweep cell needs an explicit fused-attention assertion, not just a load-success
check.

## 5. Sampling effects on degeneration

**The general mechanism, measured.** Holtzman et al.'s foundational analysis
([*The Curious Case of Neural Text Degeneration*, arXiv:1904.09751](https://arxiv.org/abs/1904.09751))
established that pure maximum-likelihood decoding (greedy/beam) produces degenerate repetition —
"the loop is a high-probability region of the distribution itself" — while naive temperature
sampling trades repetition for incoherence as temperature rises. The Stable Entropy Hypothesis
paper ([arXiv:2302.06784](https://arxiv.org/abs/2302.06784)) measured that healthy generation
holds entropy within a stable band; degeneration into repetition tracks entropy **collapsing**
below that band, while excessively high temperature with weak truncation (e.g. top-p at
temperature 1.5–2.0) admits too many low-quality tokens and produces the opposite failure —
"a vicious cycle of increasing verbosity and high entropy" ending in incoherence. Both failure
directions are candidate explanations for what was observed in the 2026-08-23 run
(incoherent word-salad `reasoning_content`) — this doc cannot distinguish which, because the
run's sampling was **entirely unpinned** (plan Context: no `temperature`/`top_p`/`top_k`/`seed`
in `run_harness.py`'s request payload, `run_harness.py:255-262`), so llama-server's own defaults
applied.

**What those defaults actually were, primary source.** llama-server's default sampler chain,
absent request overrides, is `temperature=0.80, top_k=40, top_p=0.95, min_p=0.05,
repeat_penalty=1.00` in the order `penalties → dry → top_n_sigma → top_k → typ_p → top_p → min_p →
xtc → temperature`
([Debian manpage for `llama-server(1)`](https://manpages.debian.org/unstable/llama.cpp-tools/llama-server.1.en.html),
tracking the pinned upstream build family). **Gemma 4's own vendor-recommended sampling is
different and hotter**: `temperature=1.0, top_p=0.95, top_k=64`
([Unsloth's Gemma 4 QAT docs](https://unsloth.ai/docs/models/gemma-4/qat); corroborated by a
[community HF discussion](https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF/discussions/21)
independently reporting Gemma 4 "seems to work best with high temperature"). Because the harness
sent neither set of values explicitly, the 2026-08-23 run's actual effective sampling
configuration was **not the llama-server default (0.80) nor the vendor recommendation (1.0)
verified — it was whatever llama-server resolved without confirmation**, an unconfirmed variable
the plan's Context already flags as a confound. The plan's fix (Decisions locked: pinned
capability/production sampling arms with explicit seed) directly closes this gap; this doc's
contribution is documenting *why* it matters — degeneration mode (repetition vs. incoherence)
is sampling-parameter-dependent by the cited mechanism, so an unpinned run cannot separate
"this model degenerates at long context" from "this run happened to sample badly."

**Low-information context and sampling, hypothesized (no direct source found).** Whether sampling
becomes *more* prone to entropy collapse specifically when the context is low-information (e.g.
uniform-random-word filler, `run_harness.py`'s current haystack) versus coherent prose was not
found addressed by any source consulted — this is a plausible mechanism (a model conditioned on
statistically flat context may have flatter next-token distributions, amplifying whatever the
sampler does) but is stated here as **hypothesized, not measured**. This is precisely the
literal-filler-vs-coherent-prose distinction WP-3's corpus swap is designed to test.

## 6. llama.cpp-specific issues

**Context-shift.** Current default, primary source: context-shift is **disabled by default**
(`llama-server(1)` manpage: "whether to use context shift on infinite text generation (default:
disabled)"). This repo's `llama_server_chat_common` macro and both `gemma-4-12B` config entries
never pass `--context-shift`/`--no-context-shift`
(`llama/config.yaml:140-143`, `:301-306`), so the 2026-08-23 run had it off by default — and the
observed cliff (43–55K real tokens against `-c 65536`) never approached the configured ceiling, so
context-shift **cannot be the mechanism** for that specific finding (ruled out, not merely
untested). It remains a real risk category for other Gemma-family runs: Gemma 3 has a documented,
now-fixed history of emitting gibberish specifically when context-shift *is* enabled and fires on
an SWA architecture (`ggml-org/llama.cpp`
[issue #12357](https://github.com/ggml-org/llama.cpp/issues/12357), fixed via PR #12373), and the
feature has had at least two further correctness regressions since — a disabled-by-a-later-commit
bug ([issue #16983](https://github.com/ggml-org/llama.cpp/issues/16983), opened/closed within a
day, 2025-11-03/04) and persistent user confusion about whether the flag does what its name
implies ([issue #12038](https://github.com/ggml-org/llama.cpp/issues/12038), closed stale/
unconfirmed). **Remediation lever, verified default-safe**: leave context-shift off (the default)
for every correctness-bearing run, and treat any future run that needs it as requiring its own
explicit verification, not an assumed-safe flag.

**Gemma-4-specific bugs, pinned.** (1)
[Issue #21434](https://github.com/ggml-org/llama.cpp/issues/21434) — Gemma 4 26B-A4B failed to
*load* entirely due to `gemma4.attention.sliding_window_pattern` being read as `uint32_t` instead
of the GGUF's actual `bool[]` encoding, corrupting the SWA-layer map; fixed via PR #21428
(opened 2026-04-04, fixed shortly after). Confirmed **not live in this fleet's build**: this
repo's `llama/Dockerfile` pins `LLAMA_CPP_TAG=b10534`
([published 2026-08-21](https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/b10534)),
well after the fix — the observed cliff is not this bug (also inconsistent in shape: #21434 was a
hard load failure, not a stochastic mid-generation cliff). (2)
[Issue #21321](https://github.com/ggml-org/llama.cpp/issues/21321) — Gemma 4 26B-A4B (f16, CUDA)
observed spamming an unbounded stream of `<unused24>` special tokens mid-response; opened
2026-04-02, closed 2026-04-07 with **no documented root-cause fix visible in the closed issue** —
open question whether this recurs on the pinned build. Not directly reproduced in this fleet
(the 2026-08-23 cliff produced word-salad `reasoning_content`, not `<unused24>` token spam), but
the same general class (uncontrolled special-token sampling) is worth the degeneracy classifier
checking for explicitly. (3)
[Issue #24637](https://github.com/ggml-org/llama.cpp/issues/24637) — Gemma 4 + Eagle3 speculative
decoding crashed llama-server on longer prompts (~1500–2000 tokens, well under the configured
8192 ceiling), closed via PR #24707; logs implicated "sliding window attention (SWA) or
hybrid/recurrent memory" cache-invalidation during draft-model checkpointing. Not applicable to
this fleet's current gemma-4-12b-qat config (no speculative decoding configured), but a second
data point that Gemma's SWA cache-state handling has been a recurring source of length-dependent,
not context-limit-dependent, failures — matching the *shape* (not the mechanism) of the observed
cliff, which also fired well under the configured ceiling.

**Batch/ubatch effects, measured.** llama.cpp does not guarantee bit-identical output across
different `-b`/`-ub` values — floating-point reduction order in batched matrix multiplication
differs by batch size, so "developers don't want to maintain guarantees for bit-for-bit identical
results as the batch size is varied"
([PR #16016, deterministic-inference-mode discussion](https://github.com/ggml-org/llama.cpp/pull/16016),
which adds an *opt-in* batch-invariant mode explicitly because the non-invariant default is the
status quo). This repo's `llama_server_chat_common` macro fixes `-b 2048 -ub 512` uniformly
(`llama/config.yaml:140-143`), so this is not a live confound for the 2026-08-23 run specifically,
but it is a real confound for any future sweep cell that varies batch/ubatch — a difference in
observed degeneration rate between two cells that also differ in `-b`/`-ub` cannot be
attributed to context length alone without holding batch parameters fixed.

**Flash attention on this hardware, verified current (with a caution about stale sources).**
Multiple 2025-era community reports found Vulkan flash attention on AMD/RADV fell back silently to
CPU computation, with severe throughput collapse
([discussion #12629](https://github.com/ggml-org/llama.cpp/discussions/12629)). This is now
**stale for RDNA4**: a 2026 RDNA4-specific measurement thread found flash attention "fully
supported and recommended" via RADV, consistently ~2–3% faster than FA-off, coopmat correctly
supported, and **no quality degradation** across `Q4_0`/`Q5_0`/`Q5_1`/`Q8_0` KV-quant
configurations tested with FA on
([discussion #21043, "RDNA4 Llama Experiments"](https://github.com/ggml-org/llama.cpp/discussions/21043)).
This is corroborated by this fleet's own prior work: `qwen38-27b-iq3s` landed `-fa on` with
`q4_0/q4_0` KV on this exact GPU family with a confirmed-live fused-attention path and zero
correctness regression (`llama/config.yaml:418-428`, `docs/qwen38-27b-bakeoff-results.md`). Both
`gemma-4-12B` config entries currently omit `-fa` entirely (server default `auto`), so whether FA
was engaged during the 2026-08-23 cliff run is **not directly confirmed** — `llama-swap` does not
forward child stdout for router-managed processes (`llama/config.yaml:400-408`'s own prior
finding), so the only way to confirm FA state for a *router-managed* alias is the VRAM-arithmetic
technique already used for `qwen38-27b` (`llama/config.yaml:409-417`), not a log grep. Open
question, answerable but not answered by this doc: was flash attention actually engaged for the
gemma-4-12b-qat cliff run.

## 7. Remediation levers: fleet-controlled vs. not

**Controlled by this fleet, directly actionable:**
- **Context caps.** Setting `-c` below any measured or suspected collapse point is the only
  remediation that provably eliminates the failure mode for that alias (a model cannot degenerate
  at a token count it never reaches). Costless for aliases whose real use case never needs long
  context; a real capability loss otherwise.
- **Sampling.** Pin `temperature`/`top_p`/`top_k`/`seed` per request (plan Decisions locked); use
  Gemma's own vendor-recommended production sampling (`temperature=1.0, top_p=0.95, top_k=64`,
  §5) rather than an unconfirmed server default, and a `temperature=0` capability arm to isolate
  sampling variance from architectural collapse (§5, Holtzman).
- **Prompt placement.** Given lost-in-the-middle (§1) and Chroma's structural-coherence finding
  (§1), where a needle/instruction sits in the prompt is a lever independent of context length —
  front- or end-loading task-critical instructions is free and doesn't require a smaller `-c`.
- **KV cache type, per-model-verified.** `q4_0/q4_0` symmetric with `-fa on` is a confirmed-safe
  lever for this hardware on at least one model family (`qwen38-27b-iq3s`, §4/§6) but must be
  re-verified per model — asymmetric pairs risk silent fusion fallback (§4), and at least one
  architecture family (DeepSeek-V4) has a confirmed silent-corruption mode under KV quantization
  that this doc found no reason to assume is absent from every other architecture, Gemma included
  (§4, not yet tested).
- **`--context-shift` left off (the default).** Verified as not the mechanism behind the observed
  cliff and carries a real correctness-bug history when enabled on Gemma/SWA architectures (§6).
- **`-b`/`-ub` held fixed across a comparison.** Not a quality fix, but a confound-elimination
  lever: varying batch parameters between cells being compared for degeneration rate is a design
  error, not a wash (§6).

**Not controlled by this fleet:**
- **The SWA:GA ratio and RoPE base-split design itself** — architectural choices made upstream by
  Google/the Gemma team (§2, §3); this fleet can measure their effect but not change them without
  retraining or a different model family.
- **Whether NoLiMa-class literal-vs-semantic degradation exists in this model** — a property of
  the trained model, only *measurable* by an instrument built to detect it (§1), not fixable by a
  serving-side flag.
- **Upstream llama.cpp bug-fix timing** — this fleet can pin a build and verify a fix landed
  (§6, `#21434`), but cannot fix an open upstream bug (§6, `#21321`) faster than upstream does;
  the only fleet-side lever is re-verifying pinned-build status before trusting a "should be fixed
  by now" assumption.

## 8. Corroboration/divergence with the user's reference doc

`references/consumer-gpu-moe-inference-and-long-context-recovery.md` (a search report over the
user's personal semantic index, practitioner-sourced, 2026-08-23) is not peer-reviewed research
and none of its throughput numbers are independently verified in this doc — it is cited here only
for the parts its own text already flags as a genuine finding vs. a single-practitioner claim.
Three points of alignment and one point of productive tension with this doc's independent
research:

- **Corroborates:** its "three failure modes" framing (resource collapse / runtime collapse /
  quality collapse, its §"Long-context collapse") lands on the same conclusion this doc reaches
  independently — capacity to hold more tokens (KV memory, quantization, TurboQuant) is a
  *different* axis from *quality* at those tokens, and the reference doc is explicit that it found
  **no quantified experiment restoring semantic quality after long-context collapse** — consistent
  with this doc's finding that the mainstream literature (RULER, NoLiMa, Chroma) treats quality
  degradation as a property to *measure*, not a serving-side parameter to *fix*.
- **Corroborates:** its KV-cache-precision-is-model-specific caution ("the Gemma MTP experiment
  kept F16 KV because quantized KV reduced draft acceptance") is the same shape of finding as this
  doc's §4/§6 (asymmetric-quant fusion fallback; DeepSeek's silent-corruption precedent) —
  independently-sourced agreement that KV quantization is never a blanket-safe optimization.
- **Corroborates, narrowly:** its `--fit-ctx`/context-first-placement guidance and its
  recommendation to "reduce `--ctx-size` until stable, confirm memory vs. quality" as the first
  diagnostic step matches this doc's §7 framing that a context cap is the only remediation that
  *provably* eliminates a quality collapse (as opposed to merely making more room for one).
- **Diverges/untested — the reference doc's practitioner ladder is throughput-and-VRAM-focused,
  not correctness-focused, and its own single strongest quality-relevant claim (TurboQuant
  4×-context recovery on a 6 GB card) is explicitly caveated by the reference doc itself as "a
  resource-recovery claim, not proof that quality remains constant at every position" — this doc
  found no independent corroboration or refutation of TurboQuant specifically (no primary source
  consulted; out of scope, since it is not part of this fleet's stack). Flagged as unverified by
  both documents, not resolved by either.
- **Not covered by the reference doc at all:** llama.cpp/Gemma-specific upstream bugs (§6), RoPE
  base-split mechanics (§2), sampling-driven entropy collapse (§5), and the NIAH/RULER/NoLiMa
  benchmark-methodology literature (§1) — the reference doc is a practitioner-throughput index, not
  a correctness-research survey, so its silence on these is expected, not a disagreement.

## What the instrument must therefore measure

Concrete, testable requirements for WP-3's instrument and the deferred
`gemma-qat-longcontext-campaign`, derived from §1–§7:

1. **Separate literal-match recall from semantic/non-literal recall as distinct battery tasks**,
   not one NIAH-style task — a pass on literal recall (the current instrument's only mode) does
   not bound performance on the NoLiMa-class failure this doc found is empirically distinct (§1).
2. **Use coherent, non-uniform-random haystack text for every correctness-scored trial**, and
   keep the existing uniform-word-salad fixture strictly as a timing-only fixture (already locked
   in the plan's Decisions) — the current instrument cannot distinguish "collapsed due to context
   length" from "collapsed due to out-of-distribution filler input" (plan Context; §5).
3. **Pin every sampling parameter explicitly on every correctness-bearing request** — `temperature`
   , `top_p`, `top_k`, and `seed` — with at least two arms: a `temperature=0` capability arm
   (isolates architectural collapse from sampling variance, §5) and a production arm at Gemma's
   own vendor-recommended values (`temperature=1.0, top_p=0.95, top_k=64`, §5), never an unpinned
   server-default request.
4. **Record and verify, per cell, whether flash attention and the requested KV-cache type were
   actually engaged** — via the VRAM-arithmetic technique already proven for `qwen38-27b`
   (`llama/config.yaml:409-417`), not a log grep (llama-swap does not forward router-managed
   child stdout, §6) and not an assumed-from-flags state (per-cell startup-state verification is
   already a WP-1 requirement; this doc adds that FA/KV-type verification specifically needs the
   VRAM-math technique, not the `-v` flag, for router-managed aliases).
5. **Position-sweep independently from task type, and report per-cell, never averaged** — the
   lost-in-the-middle literature (§1) and the SWA:GA-ratio literature (§3) both predict
   *architecture-dependent* position sensitivity that a single-position or averaged-across-position
   result would hide; this is already the plan's Decisions locked (n≥10/cell, never averaged
   across position or task) — this doc's contribution is the citation-backed reason why that rule
   matters specifically for a hybrid SWA/global model.
6. **Classify degeneration mode, not just pass/fail** — repetition/entropy-collapse (§5, Holtzman/
   stable-entropy) is mechanistically distinct from incoherent-high-entropy output (§5) and from
   uncontrolled special-token sampling (§6, `<unused24>`, issue #21321) and from a
   silent-wrong-but-fluent answer (§4, DeepSeek `#25382`'s "confident gibberish" shape) — WP-3's
   degeneracy classifier taxonomy (`correct / wrong / degenerate / truncated(benign)`) should be
   able to tag *which* of these shapes a failing trial exhibits, since each shape implicates a
   different candidate cause and therefore a different remediation lever (§7).
7. **Verify the pinned llama.cpp build against known Gemma-family issue fix status before every
   campaign run**, not once — `#21434`'s fix is confirmed present in `b10534` (§6), but a future
   image rebuild could regress it, and `#21321` has no confirmed-fixed status at all; a campaign
   that silently rebuilds the image between measurement rounds without re-checking this is
   comparing two different systems, not one.
8. **Hold `-b`/`-ub` fixed across any comparison that attributes a quality difference to context
   length, KV type, or sampling** — batch/ubatch changes are an independent, measured source of
   output variation (§6, PR #16016) that a campaign design must not let vary incidentally between
   cells being compared.
9. **Treat context-shift as off-by-default and verify it stays off** for every correctness-bearing
   cell — its Gemma/SWA-specific correctness-bug history (§6) makes accidental activation a risk
   the instrument should assert against, not merely assume.
