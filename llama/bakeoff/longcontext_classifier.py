#!/usr/bin/env python3
"""Per-trial verdicts: the degeneracy classifier, Wilson-interval statistics, and the
deterministic per-trial seed schedule (WP-R2 of
`plans/repository-tooling-structural-remediation.md`; review finding F7).

The three pure, I/O-free functions the cell driver needs to turn a raw completion into a
recorded verdict:

  - `compute_flags` / `classify_trial` -- the degeneracy classifier, tagging SHAPE
    (`incoherent` / `repetition` / `special_token_spam`) and not just pass/fail
    (`docs/long-context-collapse.md` Req 6). Its thresholds are empirically calibrated against
    this repo's own captured fragments and clean fixture rows; changing one is a research
    decision, not a refactor.
  - `wilson_interval` -- 95% Wilson score intervals, with `sided` REQUIRED and no default, and a
    z-table that raises rather than ever guessing a value for an untabulated confidence level.
  - `derive_trial_seed` -- the `sha256(cell_id:trial_index:purpose)` schedule that keeps
    `haystack_seed` and `sampler_seed` independent for the same trial.

All three are pinned by `tests/test_pipeline_determinism.py` (flag digest, a 24-cell Wilson
table, an 8-trial seed schedule) on top of their existing behavioral tests.

Every public name here is re-exported from `longcontext` at its historical home --
`verify_run.py` consumes `longcontext.MIN_TRIALS_FOR_WILSON` and `longcontext.VERDICTS` that way.

Zero new runtime dependencies: Python 3 stdlib only (`hashlib`, `math`, `re`).
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------------------------
# Degeneracy classifier (doc Req 6: tag SHAPE, not just pass/fail)
# ---------------------------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-zA-Z']+")

# The most common English determiners/prepositions/conjunctions/copulas -- deliberately a SMALL
# "core" set (not a full stopword list) so the "incoherent" flag below has a wide, hard-to-hit-
# by-accident margin: any stream of >= MIN_TOKENS_FOR_COHERENCE_CHECK tokens of genuine fluent
# English (even a bullet-point list, even a truncated train of thought) reliably contains at
# least one of these; genuine word-salad/jargon-noun-concatenation streams reliably contain
# none. See the WP-3 report for the measured margin against this repo's own fixtures (23 clean
# gemma-qat rows: minimum 3 core-function-word occurrences; both real captured degenerate
# fragments: 0).
CORE_FUNCTION_WORDS = frozenset(
    "the a an of and or in on to for with is are this that".split()
)

# `<unused24>`-class special-token leakage (doc §6, upstream issue #21321) -- both the
# llama.cpp/Gemma-specific literal shape and a more general `<|...|>`-style special-marker
# pattern, in case a different model family leaks a differently-named special token.
SPECIAL_TOKEN_PATTERN = re.compile(
    r"<unused\d+>|<\|[a-zA-Z0-9_]{1,40}\|>|<pad>|<eos>|<bos>"
)

# Thresholds (this WP's discretion, per the plan's classifier spec -- "record chosen thresholds
# and their rationale in the report"; see the report for the full rationale and the measured
# values behind each of these against the fixture suite).
MIN_TOKENS_FOR_COHERENCE_CHECK = 15
MIN_TOKENS_FOR_REPETITION_CHECK = 20
REPETITION_NGRAM_N = 4
REPETITION_MAX_NGRAM_RATE_THRESHOLD = 0.20
REPETITION_MIN_DISTINCT2_THRESHOLD = 0.40
SPECIAL_TOKEN_MIN_COUNT = 3
MIN_CONTENT_TOKENS_FOR_HAYSTACK_OVERLAP = 5

VERDICTS = ("correct", "wrong", "degenerate", "truncated(benign)")
DEGENERATE_SHAPES = ("incoherent", "repetition", "special_token_spam")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _max_ngram_repetition_rate(tokens: Sequence[str], n: int) -> float:
    if len(tokens) < n:
        return 0.0
    counts: Dict[Tuple[str, ...], int] = {}
    grams = 0
    for i in range(len(tokens) - n + 1):
        gram = tuple(tokens[i:i + n])
        counts[gram] = counts.get(gram, 0) + 1
        grams += 1
    return max(counts.values()) / grams if grams else 0.0


def _distinct_ratio(tokens: Sequence[str], n: int) -> float:
    if len(tokens) < n:
        return 1.0
    grams = [tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]
    return len(set(grams)) / len(grams)


def compute_flags(
    reasoning_text: Optional[str],
    content_text: Optional[str],
    finish_reason: Optional[str],
    haystack_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Automated flags over the full `reasoning_text` + `content_text` stream (plan classifier
    spec, restated in the WP-3 brief): max n-gram repetition rate, distinct-2 ratio,
    haystack-vocabulary overlap, content-channel-never-reached, budget-exhausted-in-reasoning,
    plus special-token-spam match count and the core-function-word count the "incoherent" shape
    is keyed on (see `CORE_FUNCTION_WORDS`'s comment).

    `haystack_vocabulary_overlap` is `None` whenever `haystack_text` isn't supplied (the two
    real captured degenerate fragments have no recoverable generating haystack -- fixture-source
    notes: "can be exercised against these fragments only qualitatively") or the stream has
    fewer than `MIN_CONTENT_TOKENS_FOR_HAYSTACK_OVERLAP` non-core-function-word tokens to
    measure overlap over.
    """
    stream = f"{reasoning_text or ''} {content_text or ''}"
    tokens = _tokenize(stream)
    token_count = len(tokens)
    core_fw_count = sum(1 for w in tokens if w in CORE_FUNCTION_WORDS)
    max_rep = _max_ngram_repetition_rate(tokens, REPETITION_NGRAM_N)
    distinct2 = _distinct_ratio(tokens, 2)
    special_count = len(SPECIAL_TOKEN_PATTERN.findall(stream))

    content_stripped = (content_text or "").strip()
    reasoning_stripped = (reasoning_text or "").strip()
    content_channel_never_reached = not content_stripped and bool(reasoning_stripped)
    budget_exhausted_in_reasoning = (
        finish_reason == "length" and content_channel_never_reached
    )

    haystack_vocabulary_overlap: Optional[float] = None
    if haystack_text:
        haystack_words = set(_tokenize(haystack_text))
        content_tokens = {w for w in tokens if w not in CORE_FUNCTION_WORDS}
        if len(content_tokens) >= MIN_CONTENT_TOKENS_FOR_HAYSTACK_OVERLAP:
            overlap = sum(1 for w in content_tokens if w in haystack_words)
            haystack_vocabulary_overlap = overlap / len(content_tokens)

    return {
        "token_count": token_count,
        "core_function_word_count": core_fw_count,
        "max_ngram_repetition_rate": max_rep,
        "distinct_2_ratio": distinct2,
        "special_token_match_count": special_count,
        "haystack_vocabulary_overlap": haystack_vocabulary_overlap,
        "content_channel_never_reached": content_channel_never_reached,
        "budget_exhausted_in_reasoning": budget_exhausted_in_reasoning,
    }


class ClassifierResult(NamedTuple):
    verdict: str
    shape: Optional[str]
    flags: Dict[str, Any]


def classify_trial(
    reasoning_text: Optional[str],
    content_text: Optional[str],
    finish_reason: Optional[str],
    correct: Optional[bool] = None,
    haystack_text: Optional[str] = None,
) -> ClassifierResult:
    """Verdict per trial: `correct / wrong / degenerate / truncated(benign)` (plan-pinned
    taxonomy), with a shape tag (`DEGENERATE_SHAPES`) whenever the verdict is `degenerate` (doc
    Req 6). Checked in priority order special_token_spam -> repetition -> incoherent, since a
    trial could in principle trip more than one flag and the earlier checks are the more
    specific signals.

    IMPORTANT, empirically load-bearing: `content_channel_never_reached` +
    `budget_exhausted_in_reasoning` (finish_reason == "length" with an empty content channel)
    is NOT by itself sufficient evidence of `degenerate` -- this repo's own committed clean
    gemma-qat fixture rows (`llama/bakeoff/records/gemma-4-12b-qat-full/completions.jsonl`, the cold
    timing rounds) exhibit exactly that shape while reasoning in genuinely fluent, on-topic
    prose that simply ran out of `max_tokens` before reaching an answer -- see this module's
    test suite and the WP-3 report for the measured near-miss. That combination is recorded
    (`truncated(benign)`) rather than treated as a degeneracy signal on its own; only the
    content-shape checks above (special-token spam, n-gram repetition, near-zero core-function-
    word density) classify `degenerate`.

    `correct`, when known (a caller with task ground truth, i.e. `run_cell` via
    `Task.score`), disambiguates a non-degenerate verdict between `correct` and `wrong`/
    `truncated(benign)`. When `None` (this module's own unit tests calling the classifier
    directly against fixture text with no task to grade), the non-degenerate branch falls back
    to the same `truncated(benign)`-vs-`wrong` distinction using only `finish_reason` +
    `content_channel_never_reached` -- it can never return `correct` without external
    confirmation that the trial's answer was actually right.

    NOTE ON TAXONOMY (transport-failure-taxonomy WP, `plans/bakeoff-transport-taxonomy-and-
    prompt-fit.md` WP-1): a fifth sample-level `classifier_verdict` value, `"transport"`, exists
    in the wire/sample schema but is NEVER produced by this function -- `VERDICTS` above stays
    exactly `("correct", "wrong", "degenerate", "truncated(benign)")`, the full set THIS
    function can return. `run_cell` assigns `"transport"` directly, bypassing `classify_trial`
    entirely, whenever a trial's `success` is `False`: an HTTP/transport failure (empty
    channels, `finish_reason: None`) carries no correctness information one way or the other,
    so it must never be run through the correctness/degeneracy classifier and must never be
    read back as a `wrong` (or, worse, `degenerate`) verdict -- exactly the miscount that armed
    skip-propagation on a transport fact in the investigation this WP fixes. This function's own
    behavior (its five checks above, its `VERDICTS` return set) is unchanged by that WP.
    """
    flags = compute_flags(reasoning_text, content_text, finish_reason, haystack_text=haystack_text)

    if flags["special_token_match_count"] >= SPECIAL_TOKEN_MIN_COUNT:
        return ClassifierResult("degenerate", "special_token_spam", flags)

    if flags["token_count"] >= MIN_TOKENS_FOR_REPETITION_CHECK and (
        flags["max_ngram_repetition_rate"] >= REPETITION_MAX_NGRAM_RATE_THRESHOLD
        or flags["distinct_2_ratio"] <= REPETITION_MIN_DISTINCT2_THRESHOLD
    ):
        return ClassifierResult("degenerate", "repetition", flags)

    if (
        flags["token_count"] >= MIN_TOKENS_FOR_COHERENCE_CHECK
        and flags["core_function_word_count"] == 0
    ):
        return ClassifierResult("degenerate", "incoherent", flags)

    if correct is True:
        return ClassifierResult("correct", None, flags)

    if flags["budget_exhausted_in_reasoning"] or (
        finish_reason == "length" and flags["content_channel_never_reached"]
    ):
        return ClassifierResult("truncated(benign)", None, flags)

    return ClassifierResult("wrong", None, flags)


# ---------------------------------------------------------------------------------------------
# Statistics: 95% Wilson intervals on failure rates
# ---------------------------------------------------------------------------------------------

# Convention (deferred audit pins, binding -- see module docstring / plan): this module supports
# exactly the two z-values the plan and its cycle-2 audit actually use, keyed by (sided,
# confidence) so a caller can never accidentally get a z-value for a confidence level nobody
# asked for. "two" = standard two-sided Wilson score interval (both `lower`/`upper` populated).
# "one" = one-sided upper bound only (`lower` is always `None` in that mode) -- the convention
# used for e.g. "0/30 Wilson upper = 8.27%" in the plan's Split provenance section. A caller
# MUST state which convention it's using at every use site (plan Statistics pin) -- this
# module enforces that by making `sided` a required-with-no-default parameter, never silently
# defaulting to one or the other.
_Z_TABLE: Dict[Tuple[str, float], float] = {
    ("two", 0.95): 1.959963985,
    ("one", 0.95): 1.644853627,
}

MIN_TRIALS_FOR_WILSON = 10  # plan's n>=10-per-cell statistics floor


def wilson_interval(k: int, n: int, sided: str, confidence: float = 0.95) -> Dict[str, Any]:
    """95% Wilson score interval on a failure (or any binomial) rate `k / n`.

    `sided` is REQUIRED (`"two"` or `"one"`) -- no default, per the plan's binding "state the
    convention explicitly at every use site" pin (the cycle-2 audit's exact confusion this
    corrects: mixing one-sided and two-sided bounds in one comparison). `"one"` returns only an
    upper bound (`lower` is `None`); this is what a directional "is the failure rate below X%"
    claim should use. `"two"` returns both bounds.

    Raises `ValueError` for any `(sided, confidence)` pair not in `_Z_TABLE` -- this module
    implements exactly the 95% one-sided/two-sided cases the plan actually needs, never a
    guessed/approximated z-value for an untested confidence level.
    """
    if sided not in ("one", "two"):
        raise ValueError(f"wilson_interval: sided must be 'one' or 'two', got {sided!r}")
    if n <= 0:
        raise ValueError("wilson_interval: n must be positive")
    if not (0 <= k <= n):
        raise ValueError(f"wilson_interval: k={k} must satisfy 0 <= k <= n={n}")
    key = (sided, confidence)
    if key not in _Z_TABLE:
        raise ValueError(
            f"wilson_interval: no z-value tabulated for sided={sided!r}, confidence={confidence!r} "
            f"-- only {sorted(_Z_TABLE)} are supported (never guess a z-value)"
        )
    z = _Z_TABLE[key]
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = phat + z2 / (2 * n)
    half = z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))
    lower = (center - half) / denom
    upper = (center + half) / denom
    lower = max(0.0, lower)
    upper = min(1.0, upper)
    return {
        "k": k,
        "n": n,
        "rate": phat,
        "sided": sided,
        "confidence": confidence,
        "lower": None if sided == "one" else lower,
        "upper": upper,
    }


# ---------------------------------------------------------------------------------------------
# Deterministic per-trial seed schedule (haystack_seed / sampler_seed, always distinct)
# ---------------------------------------------------------------------------------------------


def derive_trial_seed(cell_id: str, trial_index: int, purpose: str) -> int:
    """Deterministic per-trial seed schedule: `sha256(f"{cell_id}:{trial_index}:{purpose}")`
    truncated to a positive 31-bit int. `purpose` (`"haystack"` / `"sampler"`) keeps the two
    seed streams independent even for the same `(cell_id, trial_index)` pair -- `haystack_seed`
    and `sampler_seed` are always distinct values on every emitted sample, never one shared
    derivation (the separate-seed-fields deferred audit pin `run_harness.needle_probe` already
    carries; this module's cell driver reuses the same invariant). The schedule is a pure
    function of its three inputs -- re-running the same cell reproduces the identical trial
    sequence, never a source of new randomness across runs.
    """
    digest = hashlib.sha256(f"{cell_id}:{trial_index}:{purpose}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) & 0x7FFFFFFF
