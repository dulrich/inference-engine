#!/usr/bin/env python3
"""Long-context correctness instrument v2 (`plans/quant-sweep-harness-and-instrument-v2.md`,
WP-3). Coherent haystack corpus + task battery + position sweep + statistics + degeneracy
classifier, built on WP-1's sweep framework (`sweep.py`) and pinned-sampling plumbing
(`run_harness.py`) and WP-2's research (`docs/long-context-collapse.md`).

**Why this module exists, not another `_build_filler_text` mode.** The pre-WP-3 instrument
(`run_harness.needle_probe`) draws its haystack from `_build_filler_text` -- uniform-random
words from a fixed word bank, explicitly a *timing* fixture ("Content is meaningless by
design"). A degenerate completion after that input cannot be attributed to genuine long-context
collapse versus out-of-distribution-input mirroring (plan Context; `docs/long-context-
collapse.md` §5; this WP's own fixture-source captures show the two captured degenerate
fragments' word-salad vocabulary matching that exact old word bank). This module replaces the
haystack source for every CORRECTNESS-scored trial with coherent public-domain prose
(`corpus/`), while `_build_filler_text` stays exactly as it was -- timing-only, never touched
here (Decisions locked: "The old word-soup haystack is retained, renamed as a timing fixture
only").

**Binding design inputs honored here** (`docs/long-context-collapse.md` §"What the instrument
must therefore measure", requirements 1/2/3/5/6 -- 4/7/8/9 are live-run/campaign concerns, cited
in prose where relevant, no live machinery built for them in this WP):
  - Req 1: `PassageQATask` is deliberately NOT a NIAH-style literal-recall task -- its question
    never reuses the passage's answer-bearing term verbatim (NoLiMa-class non-literal recall),
    kept as a distinct battery task from `SingleNeedleTask`'s literal-match recall. See each
    task class's own docstring.
  - Req 2: every haystack a battery task scores against comes from `assemble_haystack()` /
    `corpus/` -- never `run_harness._build_filler_text`. `haystack_structure` is recorded on
    every emitted sample (`"coherent"` for this module's default, always-coherent assembly) as
    a documented variable field, not an assumed-safe constant -- Chroma's measured shuffle-vs-
    flow effect (doc §1) means a future shuffled-haystack mode is a config value, not a rewrite.
  - Req 3: `run_cell()` REQUIRES a filled sampling payload (`temperature`/`top_p`/`top_k`/
    `seed` all non-`None`) and a named sampling arm (`"capability"`/`"production"`) -- it raises
    `ValueError` rather than silently sending an unpinned request. Build the payload with
    `run_harness.sampling_arm_payload(SAMPLING_ARM_CAPABILITY, seed)` or the production arm
    with the specific model's recommended values filled in.
  - Req 5: `run_cell()` returns one cell's samples/completions/summary; `run_position_sweep()`
    returns a dict keyed by position fraction, never merged or averaged across positions --
    and never across tasks (each `Task` is scored independently). A caller that wants a single
    number across positions/tasks is doing something this module deliberately does not do.
  - Req 6: `classify_trial()` tags degeneration SHAPE (`incoherent` / `repetition` /
    `special_token_spam`) whenever the verdict is `degenerate`, not just pass/fail -- see that
    function's docstring for the flags and thresholds, and the WP-3 report for the rationale
    against the two real captured fragments and the 23 clean gemma-qat rows.

Zero new runtime dependencies beyond `run_harness.py`'s own stdlib set (this module additionally
uses `functools`, `math`); no pip packages.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import random
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

# `run_harness` lives beside this file with no package `__init__.py` under `llama/` (the same
# gotcha the gates section calls out for `unittest discover`) -- a plain top-level import here
# works whenever the importer (this module's own CLI, or a test file) has already put this
# directory on `sys.path`, which is true both when this file is run directly as a script
# (Python auto-adds its own containing directory) and when a test under `tests/` inserts this
# directory itself (see `tests/test_*.py`).
import run_harness

# ---------------------------------------------------------------------------------------------
# Paths and corpus manifest loading
# ---------------------------------------------------------------------------------------------

MODULE_DIR = Path(__file__).resolve().parent
CORPUS_DIR = MODULE_DIR / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"

# Default haystack-structure label for every sample this module emits (doc Req 2: a documented
# variable field, not an assumed-safe constant -- a future shuffled-haystack arm would pass a
# different value through `run_cell(..., haystack_structure=...)`, not require a rewrite here).
HAYSTACK_STRUCTURE_COHERENT = "coherent"

_BOOK_TEXT_CACHE: Dict[str, str] = {}


@functools.lru_cache(maxsize=1)
def load_corpus_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    """Loads and caches `corpus/manifest.json` (module-level cache -- the manifest is static
    for the process lifetime; tests that need a different manifest pass one explicitly to the
    functions below instead of mutating this cache)."""
    manifest_path = Path(path) if path else MANIFEST_PATH
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_book_text(book_id: str, manifest: Optional[Dict[str, Any]] = None) -> str:
    """Reads one corpus book's full text by its manifest `id`, cached by file name (a book's
    text is immutable for the process lifetime -- re-reading the same file on every trial would
    be wasteful for a near-ceiling sweep with dozens of trials)."""
    manifest = manifest or load_corpus_manifest()
    book = next((b for b in manifest["books"] if b["id"] == book_id), None)
    if book is None:
        raise KeyError(f"load_book_text: unknown corpus book id {book_id!r}")
    cache_key = book["file"]
    if cache_key not in _BOOK_TEXT_CACHE:
        _BOOK_TEXT_CACHE[cache_key] = (CORPUS_DIR / book["file"]).read_text(encoding="utf-8")
    return _BOOK_TEXT_CACHE[cache_key]


# ---------------------------------------------------------------------------------------------
# Deterministic haystack assembly (doc Req 2: coherent corpus, never `_build_filler_text`, for
# every correctness-scored trial)
# ---------------------------------------------------------------------------------------------


class HaystackAssembly(NamedTuple):
    text: str
    book_ids: Tuple[str, ...]
    char_count: int


def _select_contiguous_passages(
    book_texts: Dict[str, str],
    book_order: Sequence[str],
    target_chars: int,
    rng: random.Random,
) -> Tuple[str, Tuple[str, ...]]:
    """Core, disk-free chunk-selection algorithm (separated from `assemble_haystack` so it's
    directly unit-testable against small synthetic texts, without needing the real ~1-6 MiB
    corpus files on disk for every test).

    Cycles through `book_order` (already seeded/shuffled by the caller), taking one contiguous,
    randomly-positioned passage per book per pass, until the concatenated (`"\\n\\n"`-joined)
    text reaches `target_chars`. For every real corpus book and realistic sweep target (up to
    the near-ceiling 0.85 * 65536-token band), a single book's ~300K-900K chars covers the
    target in ONE pass -- multi-book concatenation only engages for a target larger than the
    largest single book, exercised directly by this function's own unit tests against a tiny
    synthetic corpus, not by the real corpus in normal use.
    """
    if target_chars <= 0 or not book_order:
        return "", ()
    parts: List[str] = []
    used: List[str] = []
    total = 0
    pass_num = 0
    # Guards against an unreachable target (e.g. every book exhausted at zero remaining length)
    # looping forever -- 50 full cycles through the book order is far more than any realistic
    # corpus/target combination needs (see docstring above: one pass normally suffices).
    max_passes = len(book_order) * 50
    while total < target_chars and pass_num < max_passes:
        book_id = book_order[pass_num % len(book_order)]
        text = book_texts[book_id]
        remaining_needed = target_chars - total
        take = min(remaining_needed, len(text))
        if take <= 0:
            pass_num += 1
            continue
        max_start = max(0, len(text) - take)
        start = rng.randint(0, max_start) if max_start > 0 else 0
        passage = text[start:start + take]
        parts.append(passage)
        used.append(book_id)
        total += len(passage)
        pass_num += 1
    return "\n\n".join(parts), tuple(used)


def assemble_haystack(
    rng: random.Random,
    target_tokens: int,
    manifest: Optional[Dict[str, Any]] = None,
    chars_per_token: Optional[float] = None,
) -> HaystackAssembly:
    """Deterministic, seeded haystack assembly from the coherent corpus.

    `rng` drives ALL haystack randomness (book shuffle order, per-book passage start offsets) --
    callers pass a `random.Random(haystack_seed)` instance and, by convention, continue
    consuming the SAME instance afterward to build the trial's needle/question content (see
    `run_cell` and each `Task.build_trial`), matching `run_harness.needle_probe`'s existing
    single-rng-instance-consumed-in-order pattern. `sampler_seed` (the wire-level sampling
    seed) is a completely separate value and NEVER drives this function -- see the
    separate-seed-fields contract in `run_harness.needle_probe`'s docstring, which this module
    also honors (`haystack_seed`/`sampler_seed` are always distinct sample fields).

    `chars_per_token`, if omitted, uses `run_harness.CHARS_PER_TOKEN_ESTIMATE` -- the same
    sizing-only estimate `_build_filler_text` uses. As with that function, this is a TARGET for
    how much text to assemble; the real on-wire token count is always read back from the
    server's own `usage.prompt_tokens` by the caller, never assumed from this estimate (same
    honest-timing-semantics-adjacent invariant `run_harness.py` already carries).
    """
    manifest = manifest or load_corpus_manifest()
    if chars_per_token is None:
        chars_per_token = run_harness.CHARS_PER_TOKEN_ESTIMATE
    target_chars = max(1, int(target_tokens * chars_per_token))

    book_texts = {b["id"]: load_book_text(b["id"], manifest) for b in manifest["books"]}
    order = list(book_texts.keys())
    rng.shuffle(order)

    text, book_ids = _select_contiguous_passages(book_texts, order, target_chars, rng)
    return HaystackAssembly(text=text, book_ids=book_ids, char_count=len(text))


# ---------------------------------------------------------------------------------------------
# Needle/passage injection (single insertion point, and multi-point for the multi-needle task)
# ---------------------------------------------------------------------------------------------


def _snap_to_sentence_boundary(text: str, fraction: float) -> int:
    """Same snap-to-next-`". "`-boundary rule as `run_harness.needle_probe`'s existing
    injection point (kept identical for consistency across the two haystack sources -- a
    reviewer comparing the old and new instruments should see the same injection convention,
    not a silently different one)."""
    insert_at = int(len(text) * fraction)
    boundary = text.find(". ", insert_at)
    return boundary + 2 if boundary != -1 else insert_at


def insert_at_fraction(haystack_text: str, fraction: float, injected: str) -> Tuple[str, int]:
    """Inserts `injected` at the sentence boundary at-or-after `fraction` of `haystack_text`'s
    length. Returns `(assembled_text, insert_char_offset)`."""
    boundary = _snap_to_sentence_boundary(haystack_text, fraction)
    return haystack_text[:boundary] + injected + haystack_text[boundary:], boundary


def insert_multiple(
    haystack_text: str, fractions: Sequence[float], injections: Sequence[str]
) -> Tuple[str, List[int]]:
    """Inserts multiple snippets at their respective fractions, each snapped to its own sentence
    boundary computed against the ORIGINAL (pre-insertion) text. `fractions` MUST be given in
    ascending order (raises `ValueError` otherwise) -- `.find(". ", insert_at)` with a
    non-decreasing `insert_at` always returns a non-decreasing boundary, so ascending fractions
    guarantee non-decreasing boundaries.

    Insertions are applied in DESCENDING INDEX order (last snippet first, first snippet last).
    This keeps every not-yet-processed boundary's original offset valid (nothing at or before
    it has shifted yet), AND correctly resolves the case where two fractions snap to the exact
    same sentence boundary (a real tie, not just a close call -- verified live against this
    module's own corpus at a 0.03 fraction spread): inserting the higher-index snippet at a
    shared offset first, then the lower-index snippet at that SAME original offset second, pushes
    the higher-index snippet to the right of the lower-index one -- i.e. ascending index ends up
    leftmost, matching callers' intended left-to-right order, exactly where a naive
    boundary-value sort (with stable tie order) would silently invert two tied insertions.

    The returned offset list is in the CALLER's `fractions` order, not insertion-application
    order.
    """
    if len(fractions) != len(injections):
        raise ValueError("insert_multiple: fractions and injections must be the same length")
    if list(fractions) != sorted(fractions):
        raise ValueError("insert_multiple: fractions must be passed in ascending order")
    boundaries = [_snap_to_sentence_boundary(haystack_text, f) for f in fractions]
    text = haystack_text
    for i in range(len(boundaries) - 1, -1, -1):
        text = text[:boundaries[i]] + injections[i] + text[boundaries[i]:]
    return text, boundaries


# ---------------------------------------------------------------------------------------------
# Task battery: single-needle literal recall, multi-needle ordered readback, passage-anchored QA
# ---------------------------------------------------------------------------------------------

_MULTI_NEEDLE_SPREAD = 0.03  # fraction-of-haystack spacing between the 3 multi-needle markers
_MAX_INJECTION_FRACTION = 0.98  # keeps every injection point (and its spread siblings) in-bounds


class TrialSpec(NamedTuple):
    prompt: str
    expected: Any
    task: str
    position_fraction: float
    meta: Dict[str, Any]


class Task:
    """Base class for one battery task. `build_trial` consumes `rng` (continuing the SAME
    instance `assemble_haystack` was just called with -- see that function's docstring) to
    generate any needle/question content; `score` grades a completion's `content_text` against
    the `TrialSpec` `build_trial` returned."""

    name: str = "base_task"

    def build_trial(
        self, haystack_text: str, position_fraction: float, rng: random.Random
    ) -> TrialSpec:
        raise NotImplementedError

    def score(self, content_text: str, trial: TrialSpec) -> bool:
        raise NotImplementedError


class SingleNeedleTask(Task):
    """Literal-match recall (NIAH-style): a unique reference code is injected at
    `position_fraction`; the model must reproduce it verbatim. This is the LITERAL-recall half
    of doc Req 1's "separate literal-match recall from non-literal recall as distinct battery
    tasks" -- `PassageQATask` below is the deliberately-distinct non-literal half. A pass here
    does not bound performance on `PassageQATask` (doc §1's NoLiMa-class finding), which is
    exactly why the plan pins both as separate battery entries rather than one NIAH-only task.
    """

    name = "single_needle"

    def build_trial(self, haystack_text, position_fraction, rng):
        code = run_harness._generate_reference_code(rng)
        injected = f"Reference code for this section: {code}. "
        assembled, _ = insert_at_fraction(haystack_text, position_fraction, injected)
        question = (
            "\n\nQuestion: what is the exact reference code given above? "
            "Reply with only the code, no other text."
        )
        return TrialSpec(
            prompt=assembled + question,
            expected=code,
            task=self.name,
            position_fraction=position_fraction,
            meta={"expected_code": code},
        )

    def score(self, content_text, trial):
        return bool(content_text) and trial.expected in content_text


class MultiNeedleTask(Task):
    """Multi-needle recall: 3 distinct reference codes injected in ascending order, clustered
    near `position_fraction` (spaced `_MULTI_NEEDLE_SPREAD` apart) -- `position_fraction`
    remains the single independent variable per cell (plan's position-sweep dimension); the
    spread tests ORDERED readback within that neighborhood, not a second independent position
    axis. Correct requires all three codes present AND recovered in their original order.
    """

    name = "multi_needle"

    def build_trial(self, haystack_text, position_fraction, rng):
        fractions = [
            min(_MAX_INJECTION_FRACTION, position_fraction + i * _MULTI_NEEDLE_SPREAD)
            for i in range(3)
        ]
        codes = [run_harness._generate_reference_code(rng) for _ in range(3)]
        injections = [f"Checkpoint {i + 1} marker: {c}. " for i, c in enumerate(codes)]
        assembled, _ = insert_multiple(haystack_text, fractions, injections)
        question = (
            "\n\nQuestion: list the three checkpoint markers above, in the exact order they "
            "appeared in the text, separated by commas. Reply with only the three codes in "
            "order, no other text."
        )
        return TrialSpec(
            prompt=assembled + question,
            expected=tuple(codes),
            task=self.name,
            position_fraction=position_fraction,
            meta={"expected_codes": list(codes), "insertion_fractions": fractions},
        )

    def score(self, content_text, trial):
        if not content_text:
            return False
        positions = []
        for code in trial.expected:
            idx = content_text.find(code)
            if idx == -1:
                return False
            positions.append(idx)
        return positions == sorted(positions)


# Passage-anchored QA items (doc Req 1's NoLiMa-class task): each passage states a fact using an
# invented, distinctive multi-word answer term; each question asks about that fact via
# paraphrase (occupation/role/context), deliberately never repeating the answer term itself --
# the model must locate the right passage semantically, then recall a specific term it cannot
# find by literal string-matching the question against the haystack. Kept as a small, fixed,
# versioned set (this WP's own call, matching quality_corpus.jsonl's precedent of a small fixed
# battery rather than a growing one).
PASSAGE_QA_ITEMS: Tuple[Dict[str, str], ...] = (
    {
        "id": "qa-organ-restorer",
        "passage": (
            "Petra Lindqvist spent four decades restoring pipe organs in unheated parish naves "
            "across northern Sweden; her workshop kept a single brass instrument on the wall, a "
            "devotional relic she called the Vintergatan Bell, which she rang each solstice."
        ),
        "question": (
            "What did the organ restorer in Sweden call the brass object she rang every "
            "solstice?"
        ),
        "answer_keyword": "Vintergatan Bell",
    },
    {
        "id": "qa-astronomer-route",
        "passage": (
            "Ravi Chandrasekaran catalogued double stars for the district almanac from an "
            "observatory reachable only by an eleven-kilometer walk along a dry riverbed; "
            "colleagues nicknamed the route the Amber Path."
        ),
        "question": (
            "What name did Ravi's colleagues give to the long walking route to his observatory?"
        ),
        "answer_keyword": "Amber Path",
    },
    {
        "id": "qa-clockmaker-gift",
        "passage": (
            "The apprentice clockmaker Tomas Arrieta spent six years in a Zaragoza workshop "
            "tightening the gears of pocket watches; his master eventually gifted him a "
            "prototype timepiece known only as the Iron Sparrow."
        ),
        "question": (
            "What was the nickname of the prototype timepiece given to the young apprentice in "
            "Spain?"
        ),
        "answer_keyword": "Iron Sparrow",
    },
    {
        "id": "qa-glassworker-ledger",
        "passage": (
            "Mira Voss restored stained-glass windows for three cathedrals near Lake Geneva; "
            "the smallest chapel kept a hidden ledger of her repairs, a notebook the sacristan "
            "privately called the Cobalt Register."
        ),
        "question": (
            "What did the sacristan call the notebook that recorded the glassworker's repairs?"
        ),
        "answer_keyword": "Cobalt Register",
    },
)


class PassageQATask(Task):
    """Passage-anchored QA (doc Req 1's non-literal-recall half -- see module docstring and
    `SingleNeedleTask`). Injects one of `PASSAGE_QA_ITEMS`' short synthetic passages at
    `position_fraction`; the question shares minimal lexical overlap with the passage's
    answer-bearing term specifically (the exact `answer_keyword` string never appears in any
    `question` text above -- verified by this module's own test suite, not merely by eye).
    """

    name = "passage_qa"

    def build_trial(self, haystack_text, position_fraction, rng):
        item = rng.choice(PASSAGE_QA_ITEMS)
        assembled, _ = insert_at_fraction(haystack_text, position_fraction, item["passage"] + " ")
        question = f"\n\nQuestion: {item['question']} Answer in one short phrase."
        return TrialSpec(
            prompt=assembled + question,
            expected=item["answer_keyword"],
            task=self.name,
            position_fraction=position_fraction,
            meta={"qa_id": item["id"]},
        )

    def score(self, content_text, trial):
        if not content_text:
            return False
        return trial.expected.lower() in content_text.lower()


TASKS: Dict[str, Task] = {
    "single_needle": SingleNeedleTask(),
    "multi_needle": MultiNeedleTask(),
    "passage_qa": PassageQATask(),
}

POSITION_FRACTIONS: Tuple[float, ...] = (0.10, 0.45, 0.85)


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
    gemma-qat fixture rows (`runs/bakeoff/gemma-4-12b-qat-full/completions.jsonl`, the cold
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


# ---------------------------------------------------------------------------------------------
# Position-sweep + n-trial cell driver
# ---------------------------------------------------------------------------------------------

RequestFn = Callable[[str, int, Dict[str, Optional[float]]], Dict[str, Any]]


class CellResult(NamedTuple):
    samples: List[Dict[str, Any]]
    completions: List[Dict[str, Any]]
    summary: Optional[Dict[str, Any]]


def _require_pinned_sampling(sampling_arm: Optional[str], sampling_payload: Optional[Dict[str, Any]]) -> None:
    """Doc Req 3 / plan Decisions locked, enforced mechanically, not just documented: every
    correctness-bearing request this module drives must carry an explicit, filled sampling
    payload and a named arm. Raises `ValueError` rather than silently sending an unpinned
    request."""
    if sampling_arm not in ("capability", "production"):
        raise ValueError(
            f"run_cell: sampling_arm must be 'capability' or 'production', got {sampling_arm!r} "
            "-- every correctness-bearing trial goes through a named WP-1 sampling arm "
            "(plan Decisions locked / doc Req 3), never unpinned."
        )
    if not sampling_payload:
        raise ValueError("run_cell: sampling_payload is required (build it with run_harness.sampling_arm_payload)")
    for field in ("temperature", "top_p", "top_k", "seed"):
        if sampling_payload.get(field) is None:
            raise ValueError(
                f"run_cell: sampling_payload[{field!r}] is None -- every correctness-bearing "
                "request must carry explicit temperature/top_p/top_k/seed (doc Req 3). If using "
                "SAMPLING_ARM_PRODUCTION, fill in the model's own recommended values before "
                "calling run_cell (run_harness.SAMPLING_ARM_PRODUCTION's docstring)."
            )


def run_cell(
    request_fn: RequestFn,
    task: Task,
    cell_id: str,
    position_fraction: float,
    target_tokens: int,
    sampling_arm: str,
    sampling_payload: Dict[str, Any],
    n_trials: int = MIN_TRIALS_FOR_WILSON,
    max_tokens: int = 256,
    manifest: Optional[Dict[str, Any]] = None,
    haystack_structure: str = HAYSTACK_STRUCTURE_COHERENT,
    wilson_sided: str = "two",
) -> CellResult:
    """Runs one (task, position_fraction) cell for `n_trials` distinct `haystack_seed`s,
    emitting schema-v2 `longcontext_probe` samples/completions and (for `n_trials >=
    MIN_TRIALS_FOR_WILSON`) a per-cell Wilson-interval failure-rate summary.

    `request_fn(prompt, max_tokens, sampling) -> result_dict` is the caller's live-request
    hook, matching `run_harness._execute_streaming_request`'s return shape (`content_text`,
    `reasoning_text`, `finish_reason`, `usage`, `success`, `error`, `wall_seconds`, `t0_iso`).
    This module never calls the network itself -- no live Docker/GPU execution happens here;
    a live orchestrator wires `request_fn` to the real router, and this module's own tests pass
    a fixture-returning stub. Example live wiring (NOT executed by this module or its tests):
        request_fn = lambda prompt, mt, sampling: run_harness._execute_streaming_request(
            base_url, alias, [{"role": "user", "content": prompt}], mt,
            temperature=sampling["temperature"], top_p=sampling["top_p"],
            top_k=sampling["top_k"], seed=sampling["seed"],
        )

    `sampling_arm`/`sampling_payload`: REQUIRED, validated by `_require_pinned_sampling` (doc
    Req 3) -- build `sampling_payload` with `run_harness.sampling_arm_payload(SAMPLING_ARM_
    CAPABILITY, seed)` or a filled production-arm dict, and pass the matching `"capability"`/
    `"production"` label. `sampling_payload["seed"]` is IGNORED per-trial in favor of this
    driver's own deterministic `derive_trial_seed(cell_id, trial_index, "sampler")` schedule
    (documented in that function) -- every trial gets its own distinct sampler seed, never one
    shared seed across all `n_trials` of a cell.

    `haystack_structure` (doc Req 2): recorded verbatim on every sample; defaults to
    `HAYSTACK_STRUCTURE_COHERENT` (`"coherent"`), this module's only implemented assembly mode.

    Per Req 5, this function reports ONE cell only -- never averages across positions or tasks;
    see `run_position_sweep` for driving the 3-position sweep without merging the results.
    """
    _require_pinned_sampling(sampling_arm, sampling_payload)
    manifest = manifest or load_corpus_manifest()
    corpus_manifest_id = manifest.get("corpus_id")

    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []

    for trial_index in range(n_trials):
        haystack_seed = derive_trial_seed(cell_id, trial_index, "haystack")
        sampler_seed = derive_trial_seed(cell_id, trial_index, "sampler")
        rng = random.Random(haystack_seed)

        haystack = assemble_haystack(rng, target_tokens, manifest=manifest)
        trial = task.build_trial(haystack.text, position_fraction, rng)

        trial_sampling = dict(sampling_payload)
        trial_sampling["seed"] = sampler_seed

        result = request_fn(trial.prompt, max_tokens, trial_sampling)
        content_text = result.get("content_text") or ""
        reasoning_text = result.get("reasoning_text") or ""
        finish_reason = result.get("finish_reason")

        correct = task.score(content_text, trial)
        classifier = classify_trial(
            reasoning_text, content_text, finish_reason, correct=correct, haystack_text=haystack.text
        )
        usage = result.get("usage") or {}
        prompt_sha256 = hashlib.sha256(trial.prompt.encode("utf-8")).hexdigest()

        sample = {
            "type": "longcontext_probe",
            "schema_version": 2,
            "task": task.name,
            "cell_id": cell_id,
            "trial_index": trial_index,
            "position_fraction": position_fraction,
            "target_tokens": target_tokens,
            "haystack_structure": haystack_structure,
            "haystack_book_ids": list(haystack.book_ids),
            "corpus_manifest_id": corpus_manifest_id,
            "haystack_seed": haystack_seed,
            "sampler_seed": sampler_seed,
            "sampling_arm": sampling_arm,
            "temperature": trial_sampling.get("temperature"),
            "top_p": trial_sampling.get("top_p"),
            "top_k": trial_sampling.get("top_k"),
            "haystack_sha256": prompt_sha256,
            "expected": trial.expected if isinstance(trial.expected, (str, int, float, type(None))) else list(trial.expected),
            "trial_meta": trial.meta,
            "correct": correct,
            "classifier_verdict": classifier.verdict,
            "classifier_shape": classifier.shape,
            "classifier_flags": classifier.flags,
            "finish_reason": finish_reason,
            "success": result.get("success"),
            "error": result.get("error"),
            "wall_seconds": result.get("wall_seconds"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "timestamp": result.get("t0_iso"),
        }
        completion = {
            "type": "longcontext_probe",
            "task": task.name,
            "cell_id": cell_id,
            "trial_index": trial_index,
            "expected": sample["expected"],
            "content_text": content_text,
            "reasoning_text": reasoning_text,
            "finish_reason": finish_reason,
        }
        samples.append(sample)
        completions.append(completion)

    summary: Optional[Dict[str, Any]] = None
    if len(samples) >= MIN_TRIALS_FOR_WILSON:
        # Failure convention (this WP's own call, documented here and in the report): a trial
        # "fails" for this summary whenever its classifier_verdict is anything other than
        # "correct" -- wrong, degenerate, AND truncated(benign) all count as failures for the
        # cell's failure-rate statistic (plan: "pass/fail per the taxonomy"). A caller that
        # wants the degenerate-only rate or the wrong-only rate can recompute it from `samples`
        # directly; this summary is the one blanket pass/fail number the plan's Wilson-interval
        # pin is about.
        failures = sum(1 for s in samples if s["classifier_verdict"] != "correct")
        summary = wilson_interval(failures, len(samples), sided=wilson_sided)
        summary["cell_id"] = cell_id
        summary["task"] = task.name
        summary["position_fraction"] = position_fraction

    return CellResult(samples=samples, completions=completions, summary=summary)


def run_position_sweep(
    request_fn: RequestFn,
    task: Task,
    cell_id_prefix: str,
    target_tokens: int,
    sampling_arm: str,
    sampling_payload: Dict[str, Any],
    positions: Sequence[float] = POSITION_FRACTIONS,
    n_trials: int = MIN_TRIALS_FOR_WILSON,
    **run_cell_kwargs: Any,
) -> Dict[float, CellResult]:
    """Runs `task` at every position in `positions` (default `POSITION_FRACTIONS`, the plan's
    {0.10, 0.45, 0.85} first-class cells) as INDEPENDENT cells -- returns a dict keyed by
    `position_fraction`, each value a standalone `CellResult`. Never merges, sums, or averages
    across positions (doc Req 5 / plan Decisions locked: "cells never averaged across positions
    or tasks"); a caller that wants a rollup number is doing something this function
    deliberately does not do.
    """
    results: Dict[float, CellResult] = {}
    for position_fraction in positions:
        cell_id = f"{cell_id_prefix}-pos{position_fraction:.2f}"
        results[position_fraction] = run_cell(
            request_fn, task, cell_id, position_fraction, target_tokens,
            sampling_arm, sampling_payload, n_trials=n_trials, **run_cell_kwargs,
        )
    return results


# ---------------------------------------------------------------------------------------------
# Live entry point (plan `longcontext-pow2-overnight-campaign.md`, WP-2). Wires the battery above
# (untouched -- `run_cell`/`run_position_sweep`/`classify_trial`/passage selection all unchanged)
# to a real scratch server via `run_harness._execute_streaming_request`, the same call shape this
# module's own `run_cell` docstring already documented as the intended live wiring (that docstring
# example is now made real here instead of only prose). Never invoked by this module's own test
# suite's DEFAULT collaborators -- `run_live_battery`'s `request_fn` parameter is always injected
# by this WP's own tests (same "real default, fake in tests" pattern `sweep.py`/`batch.py` use for
# `scratch_server_fn`/`container_controller`), so no test here ever opens a socket.
# ---------------------------------------------------------------------------------------------


def build_live_request_fn(base_url: str, alias: str, timeout: float = 300) -> RequestFn:
    """Builds a `RequestFn` (this module's `run_cell`/`run_position_sweep` contract) bound to one
    live scratch server (`base_url`) and `alias`, via `run_harness._execute_streaming_request`.
    A thin closure only -- no battery logic lives here. Calling the RETURNED function performs a
    real HTTP request; calling THIS function does not (building the closure is pure)."""

    def request_fn(prompt: str, max_tokens: int, sampling: Dict[str, Any]) -> Dict[str, Any]:
        return run_harness._execute_streaming_request(
            base_url,
            alias,
            [{"role": "user", "content": prompt}],
            max_tokens,
            timeout=timeout,
            temperature=sampling.get("temperature"),
            top_p=sampling.get("top_p"),
            top_k=sampling.get("top_k"),
            seed=sampling.get("seed"),
        )

    return request_fn


def run_live_battery(
    base_url: str,
    alias: str,
    cell_id_prefix: str,
    target_tokens: int,
    sampling_arm: str,
    sampling_payload: Dict[str, Any],
    n_trials: int = MIN_TRIALS_FOR_WILSON,
    tasks: Optional[Dict[str, Task]] = None,
    positions: Sequence[float] = POSITION_FRACTIONS,
    max_tokens: int = 256,
    timeout: float = 300,
    manifest: Optional[Dict[str, Any]] = None,
    request_fn: Optional[RequestFn] = None,
) -> Dict[str, Dict[float, CellResult]]:
    """The live entry point: drives the FULL battery -- every task in `tasks` (default `TASKS`,
    all three battery tasks) x every position in `positions` (default `POSITION_FRACTIONS`) x
    `n_trials` -- against one live scratch server, matching the plan's "position sweep x tasks x
    trials per the existing cell contract" (Key Changes, WP-2). Battery logic itself
    (`run_cell`/`run_position_sweep`) is completely untouched; this function's only job is
    building the live request_fn once (`build_live_request_fn`) and driving it across the task
    set -- every `usage.prompt_tokens` read-back and every schema-v2 field is `run_cell`'s own
    existing, unmodified behavior.

    `request_fn`, if given, OVERRIDES the live request_fn entirely (this WP's own tests always
    supply one -- a fixture-returning stub, never real -- to exercise this function's task/
    position loop and its returned shape without a network call; the real live default is built
    from `base_url`/`alias`/`timeout` via `build_live_request_fn` exactly as an orchestrator would
    call this live).

    Returns `{task_name: {position_fraction: CellResult}}` -- never merges/averages across task
    or position (Req 5, inherited unchanged from `run_position_sweep`); a caller wanting one
    pass/fail number per cell is doing something this function deliberately does not do (see
    `batch.make_longcontext_cell_runner` for how ONE `CellOutcome` is derived from this shape).
    """
    effective_request_fn = request_fn or build_live_request_fn(base_url, alias, timeout=timeout)
    tasks = tasks or TASKS
    results: Dict[str, Dict[float, CellResult]] = {}
    for task_name, task in tasks.items():
        results[task_name] = run_position_sweep(
            effective_request_fn, task, f"{cell_id_prefix}-{task_name}", target_tokens,
            sampling_arm, sampling_payload, positions=positions, n_trials=n_trials,
            max_tokens=max_tokens, manifest=manifest,
        )
    return results


# ---------------------------------------------------------------------------------------------
# CLI (offline only -- touches no Docker/network/GPU, matching sweep.py's --dry-run convention)
# ---------------------------------------------------------------------------------------------


def _dry_run(target_tokens: int, seed: int) -> Dict[str, Any]:
    manifest = load_corpus_manifest()
    out: Dict[str, Any] = {"corpus_manifest_id": manifest.get("corpus_id"), "cells": []}
    for task_name, task in TASKS.items():
        for position_fraction in POSITION_FRACTIONS:
            # `seed` folds into the derivation so the CLI's `--seed` flag is honored (default
            # reproduces a fixed output); it is never used as a raw rng seed directly.
            rng = random.Random(derive_trial_seed(f"dry-run-{seed}-{task_name}", 0, "haystack"))
            haystack = assemble_haystack(rng, target_tokens, manifest=manifest)
            trial = task.build_trial(haystack.text, position_fraction, rng)
            out["cells"].append({
                "task": task_name,
                "position_fraction": position_fraction,
                "haystack_char_count": haystack.char_count,
                "haystack_book_ids": list(haystack.book_ids),
                "prompt_char_count": len(trial.prompt),
                "expected": trial.expected if isinstance(trial.expected, (str, int, float, type(None))) else list(trial.expected),
            })
    return out


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="longcontext.py",
        description="Long-context correctness instrument v2 -- offline corpus/battery sanity "
        "checks only; no Docker/network/GPU (see llama/bakeoff/README.md for live usage via "
        "run_cell/run_position_sweep, which requires a caller-supplied request_fn).",
    )
    parser.add_argument("--dry-run", action="store_true", help="assemble one trial per task/position, print JSON")
    parser.add_argument("--target-tokens", type=int, default=31000)
    parser.add_argument("--seed", type=int, default=20260823)
    args = parser.parse_args(argv)

    if args.dry_run:
        print(json.dumps(_dry_run(args.target_tokens, args.seed), indent=2, default=str))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
