#!/usr/bin/env python3
"""Coherent-corpus loading, deterministic haystack assembly, and needle/passage injection
(WP-R2 of `plans/repository-tooling-structural-remediation.md`; review finding F7).

The haystack half of the long-context instrument: reading `corpus/manifest.json` and its books,
assembling a seeded, contiguous-passage haystack to a target token count, and injecting one or
several snippets at sentence boundaries.

**Determinism is this module's whole contract.** `assemble_haystack` is a pure function of
`(rng state, target_tokens, manifest, chars_per_token)` -- no wall-clock, no `os.urandom`, no
dict-order dependence. The 31k-token haystack's text digest, character count, and book-id tuple
are pinned byte-for-byte by `tests/test_pipeline_determinism.py`; the fair-share multi-book
selection rule in `_select_contiguous_passages` is pinned by `tests/test_haystack.py`.

Every public name here is re-exported from `longcontext` at its historical home, including the
underscore-prefixed `_select_contiguous_passages` that `tests/test_haystack.py` exercises
directly. `run_harness.CHARS_PER_TOKEN_ESTIMATE` is still read through the `run_harness` module
object at call time, so `tests/test_haystack.py`'s `longcontext.run_harness.CHARS_PER_TOKEN_
ESTIMATE` access path is unchanged.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import functools
import json
import random
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

# `run_harness` lives beside this file with no package `__init__.py` under `llama/` -- a plain
# top-level import works whenever the importer has already put this directory on `sys.path`.
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

_BOOK_TEXT_CACHE: dict[str, str] = {}


@functools.lru_cache(maxsize=1)
def load_corpus_manifest(path: str | None = None) -> dict[str, Any]:
    """Loads and caches `corpus/manifest.json` (module-level cache -- the manifest is static
    for the process lifetime; tests that need a different manifest pass one explicitly to the
    functions below instead of mutating this cache)."""
    manifest_path = Path(path) if path else MANIFEST_PATH
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_book_text(book_id: str, manifest: dict[str, Any] | None = None) -> str:
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
    book_ids: tuple[str, ...]
    char_count: int


def _select_contiguous_passages(
    book_texts: dict[str, str],
    book_order: Sequence[str],
    target_chars: int,
    rng: random.Random,
) -> tuple[str, tuple[str, ...]]:
    """Core, disk-free chunk-selection algorithm (separated from `assemble_haystack` so it's
    directly unit-testable against small synthetic texts, without needing the real corpus files
    on disk for every test).

    Cycles through `book_order` (already seeded/shuffled by the caller), taking one contiguous,
    randomly-positioned passage per book per pass, until the concatenated (`"\\n\\n"`-joined)
    text reaches `target_chars`. Documented band: realistic sweep targets up to and including
    the 512k-token-equivalent regime (tested directly against on-disk corpus schema v2 --
    `gutenberg-longcontext-haystack-v2`, 12 books / ~8.08M chars) rest on that corpus's size --
    a 524288-token haystack consumes <35% of it at `run_harness.CHARS_PER_TOKEN_ESTIMATE`'s
    sizing estimate, so book start offsets stay genuinely random rather than collapsing near-
    deterministic the way they did against the old, smaller corpus at that target (see the
    fair-share paragraph below). Whenever a SINGLE book can satisfy the whole remaining need on
    its own (`len(text) >= remaining_needed`), it does -- one book, one randomized offset, done
    in one pass; against corpus v2 this is still the common case for small-to-mid targets, and
    remains available at 512k too (a large single book can still cover the target alone,
    depending on shuffle order and target size).

    When no single book can finish the target alone, multi-book concatenation engages. Each
    pass's bite is capped to a FAIR SHARE of what's still needed --
    `ceil(remaining_needed / books_left_this_lap)`, recomputed every pass over the books not yet
    visited in the current lap through `book_order` -- rather than greedily grabbing as much as
    the book allows. This is the audit-M1 fix for the whole-book zero-offset re-emission bug:
    the old greedy `take = min(remaining_needed, len(text))` strategy front-loaded whole-book,
    offset-forced-to-zero takes into however many books of `book_order` the greedy walk needed
    to reach `target_chars`, so most books in a multi-book haystack degenerated to the SAME
    whole-book text every time regardless of seed (only the walk's final, leftover book ever got
    a randomized offset) -- exactly what made book-order shuffling alone, not real passage-
    content variation, responsible for cross-seed digest differences at the 512k target against
    the old (pre-v2, ~2.72M-char) corpus, where a haystack consumed ~85% of it. With a fair-share
    cap, a book still degrades gracefully to a deterministic whole-book take (offset forced to
    0 -- there is no unused content left to vary) exactly when the book is itself SHORTER than
    its fair share; every book long enough to exceed its slice keeps a genuinely `rng`-drawn
    start offset. Multiple laps through `book_order` (each recomputing the fair share over the
    books not yet visited THAT lap) make up any shortfall left by short books, up to `max_passes`
    below.
    """
    if target_chars <= 0 or not book_order:
        return "", ()
    parts: list[str] = []
    used: list[str] = []
    total = 0
    pass_num = 0
    # Guards against an unreachable target (e.g. every book exhausted at zero remaining length)
    # looping forever -- 50 full cycles through the book order is far more than any realistic
    # corpus/target combination needs (see docstring above: one pass normally suffices for a
    # single-book-covers-it target, and corpus v2 is sized so multi-book targets finish within a
    # small number of laps).
    max_passes = len(book_order) * 50
    while total < target_chars and pass_num < max_passes:
        book_id = book_order[pass_num % len(book_order)]
        text = book_texts[book_id]
        remaining_needed = target_chars - total
        if len(text) >= remaining_needed:
            # This book alone can finish the target -- take exactly what's needed (the common,
            # single-pass case for a realistic target against the real corpus).
            take = remaining_needed
        else:
            # This book can't finish the target alone; multi-book concatenation is unavoidable
            # regardless of how much of THIS book we take now, so don't front-load a whole-book
            # take just because this book happens to be early in the (seeded) shuffle order --
            # cap this pass's bite to a fair share of what's left, spread over the books not yet
            # visited in the current lap (audit M1 fix -- see docstring).
            books_left_this_lap = len(book_order) - (pass_num % len(book_order))
            fair_share = -(-remaining_needed // books_left_this_lap)  # ceil division
            take = min(len(text), fair_share)
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
    manifest: dict[str, Any] | None = None,
    chars_per_token: float | None = None,
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


def insert_at_fraction(haystack_text: str, fraction: float, injected: str) -> tuple[str, int]:
    """Inserts `injected` at the sentence boundary at-or-after `fraction` of `haystack_text`'s
    length. Returns `(assembled_text, insert_char_offset)`."""
    boundary = _snap_to_sentence_boundary(haystack_text, fraction)
    return haystack_text[:boundary] + injected + haystack_text[boundary:], boundary


def insert_multiple(
    haystack_text: str, fractions: Sequence[float], injections: Sequence[str]
) -> tuple[str, list[int]]:
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
