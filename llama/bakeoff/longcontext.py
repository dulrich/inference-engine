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

**Module layout (WP-R2 of `plans/repository-tooling-structural-remediation.md`, review finding
F7).** This file was 1299 lines; it is now the instrument's offline CLI plus the public surface
every caller outside this directory has always reached for as `longcontext.<name>`. The four
extracted modules are, in dependency order:

  - `longcontext_corpus.py` -- manifest/book loading, deterministic haystack assembly, injection
  - `longcontext_tasks.py` -- the three battery tasks, `TASKS`, `POSITION_FRACTIONS`
  - `longcontext_classifier.py` -- degeneracy classifier, Wilson statistics, seed schedule
  - `longcontext_cell.py` -- prompt-fit shrink loop, `run_cell`, sweep + live battery drivers

Everything they own is re-exported below at its historical `longcontext.` name. Nothing about
the emitted sample shape, the prompt text, the classifier thresholds, or any deterministic
result changed.

**Binding design inputs honored here** (`docs/long-context-collapse.md` §"What the instrument
must therefore measure", requirements 1/2/3/5/6 -- 4/7/8/9 are live-run/campaign concerns, cited
in prose where relevant, no live machinery built for them in this WP):
  - Req 1: `PassageQATask` is deliberately NOT a NIAH-style literal-recall task -- its question
    never reuses the passage's answer-bearing term verbatim (NoLiMa-class non-literal recall),
    kept as a distinct battery task from `SingleNeedleTask`'s literal-match recall. See each
    task class's own docstring (`longcontext_tasks.py`).
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

Zero new runtime dependencies beyond `run_harness.py`'s own stdlib set (this module's family
additionally uses `functools`, `math`); no pip packages.
"""

from __future__ import annotations

import json
import random
import sys
from typing import Any

# `run_harness` lives beside this file with no package `__init__.py` under `llama/` (the same
# gotcha the gates section calls out for `unittest discover`) -- a plain top-level import here
# works whenever the importer (this module's own CLI, or a test file) has already put this
# directory on `sys.path`, which is true both when this file is run directly as a script
# (Python auto-adds its own containing directory) and when a test under `tests/` inserts this
# directory itself (see `tests/test_*.py`). Kept bound here (not only in the submodules) because
# `tests/test_haystack.py` reads `longcontext.run_harness.CHARS_PER_TOKEN_ESTIMATE`.
import run_harness  # noqa: F401
from longcontext_cell import (  # noqa: F401
    HAYSTACK_SHRINK_SAFETY_FACTOR,
    MAX_HAYSTACK_ASSEMBLIES,
    CellResult,
    HaystackFit,
    HaystackOverBudgetError,
    RequestFn,
    _assemble_haystack_fit,
    _require_pinned_sampling,
    build_live_request_fn,
    run_cell,
    run_live_battery,
    run_position_sweep,
)

# -- re-exports: the public surface `batch.py`, `cell_runner.py`, `verify_run.py`, and every
# -- `tests/test_*.py` have always reached for as `longcontext.<name>`. Kept at their historical
# -- home so the WP-R2 split is invisible to every caller outside this directory. The
# -- underscore-prefixed entries are deliberate: `tests/test_haystack.py` exercises
# -- `_select_contiguous_passages` directly and `cell_runner.py` documents
# -- `_assemble_haystack_fit` by name.
from longcontext_classifier import (  # noqa: F401
    CORE_FUNCTION_WORDS,
    DEGENERATE_SHAPES,
    MIN_CONTENT_TOKENS_FOR_HAYSTACK_OVERLAP,
    MIN_TOKENS_FOR_COHERENCE_CHECK,
    MIN_TOKENS_FOR_REPETITION_CHECK,
    MIN_TRIALS_FOR_WILSON,
    REPETITION_MAX_NGRAM_RATE_THRESHOLD,
    REPETITION_MIN_DISTINCT2_THRESHOLD,
    REPETITION_NGRAM_N,
    SPECIAL_TOKEN_MIN_COUNT,
    SPECIAL_TOKEN_PATTERN,
    VERDICTS,
    ClassifierResult,
    classify_trial,
    compute_flags,
    derive_trial_seed,
    wilson_interval,
)
from longcontext_corpus import (  # noqa: F401
    CORPUS_DIR,
    HAYSTACK_STRUCTURE_COHERENT,
    MANIFEST_PATH,
    MODULE_DIR,
    HaystackAssembly,
    _select_contiguous_passages,
    _snap_to_sentence_boundary,
    assemble_haystack,
    insert_at_fraction,
    insert_multiple,
    load_book_text,
    load_corpus_manifest,
)
from longcontext_tasks import (  # noqa: F401
    PASSAGE_QA_ITEMS,
    POSITION_FRACTIONS,
    TASKS,
    MultiNeedleTask,
    PassageQATask,
    SingleNeedleTask,
    Task,
    TrialSpec,
)

# ---------------------------------------------------------------------------------------------
# CLI (offline only -- touches no Docker/network/GPU, matching sweep.py's --dry-run convention)
# ---------------------------------------------------------------------------------------------


def _dry_run(target_tokens: int, seed: int) -> dict[str, Any]:
    manifest = load_corpus_manifest()
    out: dict[str, Any] = {"corpus_manifest_id": manifest.get("corpus_id"), "cells": []}
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


def main(argv: list[str] | None = None) -> int:
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
