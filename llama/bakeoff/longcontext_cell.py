#!/usr/bin/env python3
"""Live cell execution: the prompt-fit shrink loop, the n-trial cell driver, the position sweep,
and the live entry point (WP-R2 of `plans/repository-tooling-structural-remediation.md`; review
finding F7).

This is where a haystack, a task, and a sampling arm become schema-v2 `longcontext_probe`
samples. It composes the other three long-context modules and adds nothing to their contracts:

  - `_assemble_haystack_fit` -- the bounded (<= `MAX_HAYSTACK_ASSEMBLIES`) deterministic shrink
    loop that measures against the live server's own tokenizer and re-assembles from a FRESH
    `random.Random(haystack_seed)` rather than ever silently truncating or sending over-ceiling.
  - `run_cell` -- one (task, position) cell for n trials, with `_require_pinned_sampling`
    enforcing the explicit-sampling rule mechanically, `"transport"` assigned directly (never via
    the correctness classifier) whenever a trial's `success` is `False`, and the Wilson summary
    emitted only at `n >= MIN_TRIALS_FOR_WILSON`.
  - `run_position_sweep` / `run_live_battery` -- the sweep and battery drivers, which never merge
    or average across positions or tasks.
  - `build_live_request_fn` -- the one thin closure that actually reaches the network, via
    `run_harness._execute_streaming_request`.

**No network call happens at import or construction time**, and this module never calls the
network on its own behalf: `run_cell` takes an injected `request_fn`, and every test supplies a
stub or rebinds `run_harness._execute_streaming_request` (which still works across this split --
that name is resolved through the `run_harness` module object at call time, never bound early).

Every public name here is re-exported from `longcontext` at its historical home;
`batch.py` / `cell_runner.py` reach `run_live_battery`, `run_cell`, `CellResult`,
`HaystackOverBudgetError`, and `_assemble_haystack_fit` that way.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence

import run_harness
from longcontext_classifier import (
    MIN_TRIALS_FOR_WILSON,
    ClassifierResult,
    classify_trial,
    derive_trial_seed,
    wilson_interval,
)
from longcontext_corpus import (
    HAYSTACK_STRUCTURE_COHERENT,
    HaystackAssembly,
    assemble_haystack,
    load_corpus_manifest,
)
from longcontext_tasks import POSITION_FRACTIONS, TASKS, Task


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


# ---------------------------------------------------------------------------------------------
# Prompt-fit shrink loop (plan `bakeoff-transport-taxonomy-and-prompt-fit.md` WP-2). Even with a
# per-model measured `chars_per_token` (WP-6), a single haystack's real on-wire token count still
# varies trial to trial -- the residual this WP closes by measuring against the LIVE server's own
# tokenizer and deterministically re-assembling (never silently truncating or sending over-
# ceiling) whenever a caller opts in by supplying both `tokenize_fn` and `token_budget`.
# ---------------------------------------------------------------------------------------------

# Plan-pinned bound: the first assembly plus up to 2 shrink retries, never more (WP-2 brief,
# Change 2: "bounded loop, <= 3 total assemblies").
MAX_HAYSTACK_ASSEMBLIES = 3

# Scale the next attempt's target slightly UNDER the arithmetic budget/measured ratio so ordinary
# assembly-to-assembly noise (a different book mix at the smaller target, sentence-boundary
# snapping, etc.) doesn't land the very next attempt right back over budget.
HAYSTACK_SHRINK_SAFETY_FACTOR = 0.98


class HaystackOverBudgetError(RuntimeError):
    """Raised by `_assemble_haystack_fit` when a haystack is still measured over `token_budget`
    after `MAX_HAYSTACK_ASSEMBLIES` deterministic shrink attempts. The caller (`run_cell`) lets
    this propagate -- a cell must fail loudly (surfacing as `batch.py`'s `battery_error` failed-
    cell disposition) rather than ever silently send an over-ceiling prompt to the server."""


class HaystackFit(NamedTuple):
    haystack: HaystackAssembly
    rng: random.Random
    measured_tokens: Optional[int]
    shrink_attempts: int


def _assemble_haystack_fit(
    haystack_seed: int,
    target_tokens: int,
    manifest: Optional[Dict[str, Any]],
    chars_per_token: Optional[float],
    tokenize_fn: Optional[Callable[[str], int]],
    token_budget: Optional[int],
) -> HaystackFit:
    """Assembles one trial's haystack and, when `tokenize_fn`/`token_budget` are both supplied,
    measures it against the live server's own tokenizer and deterministically shrinks it until it
    fits.

    When either `tokenize_fn` or `token_budget` is `None`, this is exactly today's single-
    assembly behavior byte-for-byte: `assemble_haystack` is called once, and the returned `rng`
    is the SAME instance it consumed -- untouched and ready for `task.build_trial` to keep
    consuming in the established order. `measured_tokens`/`shrink_attempts` come back `None`/`0`
    so `run_cell` knows not to record the new `trial_meta` keys at all in that mode.

    When both are supplied and the first assembly measures over `token_budget`, this
    re-assembles from a FRESH `random.Random(haystack_seed)` (never continuing to draw from the
    already-consumed rng) with the target token count scaled by
    `(token_budget / measured) * HAYSTACK_SHRINK_SAFETY_FACTOR`, then re-measures -- up to
    `MAX_HAYSTACK_ASSEMBLIES` total assemblies. Determinism is preserved because every attempt
    re-seeds from the identical `haystack_seed`: replaying the same `(cell_id, trial_index)`
    against the same tokenize responses always performs the identical attempt sequence and lands
    on the identical final haystack text, live server or offline test alike -- no wall-clock,
    `os.urandom`, or dict-order dependence anywhere in this loop.

    Raises `HaystackOverBudgetError` if the haystack is still over `token_budget` after
    `MAX_HAYSTACK_ASSEMBLIES` assemblies. A `tokenize_fn` transport failure (or any other
    exception it raises) propagates as-is -- the same raise-don't-default posture
    `run_harness.tokenize_count`/`calibrate_chars_per_token` already carry.
    """
    rng = random.Random(haystack_seed)
    haystack = assemble_haystack(rng, target_tokens, manifest=manifest, chars_per_token=chars_per_token)
    if tokenize_fn is None or token_budget is None:
        return HaystackFit(haystack, rng, None, 0)

    measured = tokenize_fn(haystack.text)
    assemblies = 1
    current_target_tokens = target_tokens
    while measured > token_budget:
        if assemblies >= MAX_HAYSTACK_ASSEMBLIES:
            raise HaystackOverBudgetError(
                f"haystack still over budget after {assemblies} assemblies "
                f"(measured={measured}, token_budget={token_budget}, haystack_seed={haystack_seed})"
            )
        scale = (token_budget / measured) * HAYSTACK_SHRINK_SAFETY_FACTOR
        current_target_tokens = max(1, int(current_target_tokens * scale))
        rng = random.Random(haystack_seed)
        haystack = assemble_haystack(
            rng, current_target_tokens, manifest=manifest, chars_per_token=chars_per_token
        )
        measured = tokenize_fn(haystack.text)
        assemblies += 1
    return HaystackFit(haystack, rng, measured, assemblies - 1)


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
    chars_per_token: Optional[float] = None,
    tokenize_fn: Optional[Callable[[str], int]] = None,
    token_budget: Optional[int] = None,
) -> CellResult:
    """Runs one (task, position_fraction) cell for `n_trials` distinct `haystack_seed`s,
    emitting schema-v2 `longcontext_probe` samples/completions and (for `n_trials >=
    MIN_TRIALS_FOR_WILSON`) a per-cell Wilson-interval failure-rate summary.

    `request_fn(prompt, max_tokens, sampling) -> result_dict` is the caller's live-request
    hook, matching `run_harness._execute_streaming_request`'s return shape (`content_text`,
    `reasoning_text`, `finish_reason`, `usage`, `success`, `error`, `wall_seconds`, `t0_iso`,
    and -- WP-1 of `bakeoff-timing-records-and-estimates.md` -- the optional `timings` block).
    Every one of those keys is read with `.get(...)`, so a stub that models only a subset stays
    valid; a missing `timings` simply records `null` rate fields.
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

    `tokenize_fn`/`token_budget` (plan `bakeoff-transport-taxonomy-and-prompt-fit.md` WP-2):
    optional prompt-fit collaborators -- see `_assemble_haystack_fit`. When either is omitted
    (the default), haystack assembly is exactly today's single-attempt behavior, byte-for-byte.
    When both are supplied, every trial's haystack is measured against the live server's own
    tokenizer before being sent and deterministically shrunk if it's over `token_budget`; the
    effective measured token count and shrink-attempt count are recorded as additive
    `trial_meta` keys (`haystack_measured_tokens`/`haystack_shrink_attempts`) only in that mode.
    """
    _require_pinned_sampling(sampling_arm, sampling_payload)
    manifest = manifest or load_corpus_manifest()
    corpus_manifest_id = manifest.get("corpus_id")

    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []

    for trial_index in range(n_trials):
        haystack_seed = derive_trial_seed(cell_id, trial_index, "haystack")
        sampler_seed = derive_trial_seed(cell_id, trial_index, "sampler")

        # WP-6 recalibration (WP-5 finding: the static CHARS_PER_TOKEN_ESTIMATE oversizes real
        # Gutenberg-corpus prompts by a measured +11-17% per family -- at 512k that overshoot
        # would overflow `-c`). `chars_per_token`, when supplied by the caller (a queue's
        # per-model measured value), replaces the static estimate for haystack sizing only; the
        # real on-wire count is still always read back from `usage.prompt_tokens`, never assumed.
        # WP-2 (transport-taxonomy-and-prompt-fit): when `tokenize_fn`/`token_budget` are both
        # supplied, `_assemble_haystack_fit` additionally measures against the live server's own
        # tokenizer and deterministically shrinks until the haystack fits -- see that function's
        # docstring for the determinism argument. `fit.rng` is the SAME instance the accepted
        # assembly consumed (fresh-seeded for that attempt), continued below exactly as the
        # single-attempt convention always has.
        fit = _assemble_haystack_fit(
            haystack_seed, target_tokens, manifest, chars_per_token, tokenize_fn, token_budget,
        )
        haystack = fit.haystack
        rng = fit.rng
        trial = task.build_trial(haystack.text, position_fraction, rng)

        trial_sampling = dict(sampling_payload)
        trial_sampling["seed"] = sampler_seed

        result = request_fn(trial.prompt, max_tokens, trial_sampling)
        content_text = result.get("content_text") or ""
        reasoning_text = result.get("reasoning_text") or ""
        finish_reason = result.get("finish_reason")

        if result.get("success") is False:
            # A `success=False` trial is a TRANSPORT FACT, never a correctness verdict (plan
            # Decisions locked) -- `classify_trial` is never invoked, and the reasoning-channel
            # fallback below never fires. An HTTP-failed trial's empty channels / null
            # `finish_reason` trip no genuine degeneracy signal, and running it through the
            # classifier is exactly what produced the miscount this WP fixes: a 400 falling
            # through to `wrong` (and, via `batch.py`'s cell-outcome mapping, `needle_failure`).
            # `correct` stays `False` (there is no channel to score); `classifier_shape`/
            # `classifier_flags` stay null/empty -- the sample's own `success`/`error` fields
            # already carry the transport detail.
            correct = False
            scored_on_reasoning = False
            classifier = ClassifierResult("transport", None, {})
        else:
            correct = task.score(content_text, trial)
            # WP-5 attended-validation finding #7 (root AGENTS.md "wire fields not matching docs"
            # family): a reasoning-model chat template (nemotron_h, live-measured) can route the
            # ENTIRE answer to the reasoning channel and finish with `stop` on an empty content
            # channel -- nemotron recalled every needle perfectly at 55k prompt tokens while all 27
            # trials scored `wrong` against the empty `content_text`. When the model finished
            # cleanly, produced no content, but did produce reasoning, grade the reasoning channel
            # (there IS no other final answer) and flag the sample so analysis can always
            # distinguish channel-fallback scores from ordinary content-channel scores.
            scored_on_reasoning = False
            if not correct and not content_text.strip() and reasoning_text.strip() and finish_reason == "stop":
                correct = task.score(reasoning_text, trial)
                scored_on_reasoning = True
            classifier = classify_trial(
                reasoning_text, content_text, finish_reason, correct=correct, haystack_text=haystack.text
            )
            classifier.flags["scored_on_reasoning_channel"] = scored_on_reasoning
        usage = result.get("usage") or {}
        # WP-1 (plan `bakeoff-timing-records-and-estimates.md`): the server's OWN reported
        # prompt-eval / decode rates, retained verbatim and nullable. `timings` is
        # `_execute_streaming_request`'s pass-through of llama.cpp's final-chunk `timings` block
        # (live-verified present on this build, 2026-08-25); any response that omits it -- or any
        # injected `request_fn` test double that doesn't model it -- records `None`, never a
        # substitute. Decode tok/s is NEVER derived from `wall_seconds` here: a needle probe's
        # wall time is prefill-dominated, so dividing tokens by it would report a number that
        # answers a different question (plan Decisions locked).
        timings = result.get("timings") or {}
        prompt_sha256 = hashlib.sha256(trial.prompt.encode("utf-8")).hexdigest()

        # WP-2 (transport-taxonomy-and-prompt-fit): additive-only `trial_meta` keys, recorded ONLY
        # when the fit-check path actually ran (`tokenize_fn`/`token_budget` both supplied) --
        # `trial.meta` is passed through completely unmodified otherwise, keeping today's emitted
        # `trial_meta` byte-for-byte when this WP's collaborators aren't in play.
        trial_meta = trial.meta
        if tokenize_fn is not None and token_budget is not None:
            trial_meta = dict(trial.meta)
            trial_meta["haystack_measured_tokens"] = fit.measured_tokens
            trial_meta["haystack_shrink_attempts"] = fit.shrink_attempts

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
            "trial_meta": trial_meta,
            "correct": correct,
            "classifier_verdict": classifier.verdict,
            "classifier_shape": classifier.shape,
            "classifier_flags": classifier.flags,
            "finish_reason": finish_reason,
            "success": result.get("success"),
            "error": result.get("error"),
            "wall_seconds": result.get("wall_seconds"),
            "prompt_tokens": usage.get("prompt_tokens"),
            # WP-1 (bakeoff-timing-records-and-estimates), all three ADDITIVE and nullable:
            # `completion_tokens` was already parsed into `usage` here and simply never
            # projected; the two rate fields come only from the server's own `timings` block.
            "completion_tokens": usage.get("completion_tokens"),
            "server_prompt_eval_tok_s": timings.get("prompt_per_second"),
            "server_eval_tok_s": timings.get("predicted_per_second"),
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
    chars_per_token: Optional[float] = None,
    max_tokens_by_task: Optional[Dict[str, int]] = None,
    tokenize_fn: Optional[Callable[[str], int]] = None,
    token_budget: Optional[int] = None,
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

    `tokenize_fn`/`token_budget` (plan `bakeoff-transport-taxonomy-and-prompt-fit.md` WP-2):
    forwarded verbatim to every `run_position_sweep`/`run_cell` call this function makes -- see
    `run_cell`'s docstring for the fit-check behavior. Both default to `None` (today's behavior,
    unmodified); `batch.make_longcontext_cell_runner` is the real caller that supplies both, built
    from `run_harness.tokenize_count` bound to the live scratch server and a budget derived from
    the cell's own `-c` and response caps.

    Returns `{task_name: {position_fraction: CellResult}}` -- never merges/averages across task
    or position (Req 5, inherited unchanged from `run_position_sweep`); a caller wanting one
    pass/fail number per cell is doing something this function deliberately does not do (see
    `batch.make_longcontext_cell_runner` for how ONE `CellOutcome` is derived from this shape).
    """
    effective_request_fn = request_fn or build_live_request_fn(base_url, alias, timeout=timeout)
    tasks = tasks or TASKS
    results: Dict[str, Dict[float, CellResult]] = {}
    for task_name, task in tasks.items():
        # WP-6 (WP-5 punt): a per-task response-token override -- the verbose production arm
        # (temp 1.0) needs more than the flat 256-token cap to enumerate multi_needle's several
        # answers (gemma f16's multi_needle went 0/9 all-`truncated(benign)` in WP-5). The flat
        # `max_tokens` stays the default for any task without an override.
        task_max_tokens = (max_tokens_by_task or {}).get(task_name, max_tokens)
        results[task_name] = run_position_sweep(
            effective_request_fn, task, f"{cell_id_prefix}-{task_name}", target_tokens,
            sampling_arm, sampling_payload, positions=positions, n_trials=n_trials,
            max_tokens=task_max_tokens, manifest=manifest, chars_per_token=chars_per_token,
            tokenize_fn=tokenize_fn, token_budget=token_budget,
        )
    return results
