#!/usr/bin/env python3
"""Live cell runners: the battery-execution seam `batch.run_queue` drives (WP-R1 extraction).

`run_queue` owns the queue loop and the state machine; this module owns what happens *inside* one
scratch-server lifetime. Two runners exist:

- `default_cell_runner` -- startup verification only, no battery. `run_queue`'s unchanged
  internal default.
- `make_longcontext_cell_runner(...)` -- the real live runner the CLI wires in. Its `_runner`
  follows one linear sequence and nothing else: **startup gate -> optional timing arm -> optional
  long-context measurement -> classify -> persist.** Each step is a named function; there is no
  dispatch table to chase.

Every live collaborator (`longcontext.run_live_battery`, `sampling_table.production_sampling`,
`sweep.read_vram_used_bytes`, `run_harness.run_round`) is an injectable parameter with a real
default, and the bake-off test suite always injects fakes -- no test in this repo opens a socket,
spawns a scratch server, or reads host sysfs through this module.

Outcome precedence is NOT decided here: it lives in `cell_outcome` as a pure function. This
module collects measurements and hands them over.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

import longcontext
import run_harness
import sampling_table
import sweep
from cell_model import ResolvedCell
from cell_outcome import (
    BATTERY_ERROR,
    COMPLETED,
    CellOutcome,
    classify_battery_samples,
    classify_sampling_arm,
    classify_startup,
)
from cell_record import cell_timing_fields, write_cell_record

# The abbreviated cell's `battery` vocabulary (`sweep.VALID_BATTERY_ITEMS`) is coarse-grained;
# `"needle"` is the one item the long-context runner understands -- any cell whose `battery` does
# NOT include it gets the startup-verification-only disposition, never silently dropped.
LONGCONTEXT_BATTERY_ITEM = "needle"

# -------------------------------------------------------------------------------------------
# `"timing"` battery item (plan `bakeoff-timing-records-and-estimates.md` WP-1). A small round
# pass whose `timing_round` samples are appended to the SAME `samples.jsonl`, in the exact pre-v2
# shape `run_harness.run_round` has always emitted (and that the dashboard's
# `packages/shared/src/metric.ts` already parses).
# -------------------------------------------------------------------------------------------

TIMING_BATTERY_ITEM = "timing"

# Brief-pinned (audit E1 disposition): 3 rounds TOTAL, `round_index` 0..2. Large-ctx cells are
# expensive and this pass is a decode-rate measurement, not a statistics exercise.
TIMING_DEFAULT_ROUNDS = 3

# A SHORT prompt on purpose: this arm measures decode throughput and first-token latency for the
# cell's model, not its prefill behavior at the cell's `-c` (the needle arm is what exercises
# long context). ~64 predicted tokens is enough decode window for `observed_decode_tok_s` to be
# meaningful while costing ~a second per round. Fixed text, so every round after the first is a
# genuine prefix-cache hit.
TIMING_PROMPT = (
    "Explain, in one short paragraph, why a warm cache makes a repeated request faster."
)
TIMING_MAX_TOKENS = 64

# Round 0 runs against a freshly started, just-verified scratch server with an empty KV cache;
# rounds 1+ resend the identical prompt, so they are cache-hitting `hot` rounds and `run_round`'s
# own hot cache assertion (`cached/prompt > 0.90`) is a real check on them. `run_round`'s
# `cache_assertion_passed` stays `None` for the `cold` round, as it always has.
TIMING_ROUND_STATES = ("cold", "hot")

# WP-6 (WP-5 punt, measured): the flat 256-token response cap is too tight for multi_needle under
# the verbose production arm (temp 1.0) -- gemma f16's multi_needle went 0/9 all-`truncated
# (benign)` in WP-5's attended run while single_needle at the same cap scored 8/9. multi_needle
# asks the model to enumerate SEVERAL recalled values, so it gets 3x the budget; every other task
# keeps the flat `max_tokens` default.
LONGCONTEXT_MAX_TOKENS_BY_TASK = {"multi_needle": 768}

# WP-2 (`plans/bakeoff-transport-taxonomy-and-prompt-fit.md`): fixed constant absorbing
# chat-template overhead PLUS needle/question insertion -- `run_harness.tokenize_count` counts the
# raw haystack text, never the chat-templated prompt that actually goes on the wire, so this
# margin stands in for that difference (plan Assumptions: "tens of tokens each; 512 is
# deliberately generous").
TEMPLATE_MARGIN = 512


def prompt_fit_budget(context: int, max_tokens: int) -> int:
    """One conservative scalar token budget for the live prompt-fit shrink loop
    (`longcontext._assemble_haystack_fit`): `context` (the cell's own `-c`) minus the LARGEST
    response cap this battery run could enforce (the flat `max_tokens` the runner was built with,
    or any `LONGCONTEXT_MAX_TOKENS_BY_TASK` override) minus `TEMPLATE_MARGIN`.

    One scalar budget, not a per-task one, is a deliberate simplification (WP-2 brief: "one
    conservative scalar budget is fine; per-task budgets are optional if the plumbing stays
    clean") -- using the LARGEST response cap across every task in the battery keeps the
    governing invariant true for every task, not just the flat-capped ones: no prompt this
    battery run sends may ever exceed `context - response_cap_for_its_task - TEMPLATE_MARGIN`.
    """
    response_cap = max([max_tokens, *LONGCONTEXT_MAX_TOKENS_BY_TASK.values()])
    return context - response_cap - TEMPLATE_MARGIN


# -------------------------------------------------------------------------------------------
# Timing arm
# -------------------------------------------------------------------------------------------


class TimingArm(NamedTuple):
    """The timing arm's output. `detail` is `None` for a cell whose battery has no `"timing"`
    item, which is how the runner knows to leave `CellOutcome.detail` byte-identical to its
    pre-timing-arm shape."""

    samples: List[Dict[str, Any]]
    completions: List[Dict[str, Any]]
    detail: Optional[Dict[str, Any]]


# The "this cell had no timing arm" value. Its empty lists are never mutated -- `write_cell_record`
# extends its own local list from them, never the other way round.
NO_TIMING = TimingArm([], [], None)


def resolve_timing_rounds(override: Optional[int]) -> int:
    """The per-cell `timing_rounds` override, else `TIMING_DEFAULT_ROUNDS`. A non-int or
    non-positive value falls back to the default rather than raising -- `validate_queue` already
    rejects a malformed override at load time, so reaching here with one means a hand-built cell,
    and a timing arm must never be the thing that fails an otherwise-good cell."""
    if isinstance(override, int) and not isinstance(override, bool) and override >= 1:
        return override
    return TIMING_DEFAULT_ROUNDS


def run_timing_rounds(
    handle: "sweep.ScratchServerHandle",
    cell: ResolvedCell,
    round_fn: Optional[Callable[..., Any]] = None,
    timeout: float = 180.0,
) -> TimingArm:
    """The `"timing"` battery item: `resolve_timing_rounds(...)` short-prompt generation rounds
    against `handle`'s live scratch server.

    `run_harness.run_round` is REUSED verbatim -- not reimplemented. It already emits exactly the
    pre-v2 `timing_round` sample shape (`model_load_seconds`, `first_generation_token_ms`,
    `decode_wall_ms`, `observed_decode_tok_s`, `server_*_tok_s`, `completion_tokens`,
    `cache_hit_ratio`, ...) that `dashboard/packages/shared/src/metric.ts` parses, and it never
    raises on a transport failure (it records `success: false` on the sample instead).

    Round schedule (brief-pinned): `round_index` 0 is the `cold` round -- the timing pass runs
    FIRST for a cell whose battery has both `"timing"` and `"needle"`, so round 0 genuinely hits a
    freshly started server with an empty KV cache. Rounds 1+ resend the identical prompt and are
    labeled `hot`, which arms `run_round`'s own real cache-evidence assertion for them.

    `model_load_seconds` stays `null` on every row here, deliberately and by the README timing
    contract: `poll_for_load` is left off because a scratch server is a bare `llama-server` with
    no llama-swap `/models` residency status to poll, AND the server was already health-checked
    and startup-verified before round 0 -- there is no residency transition left to OBSERVE. The
    honest field for the startup cost is `config.json`'s `scratch_startup_seconds`; nothing here
    subtracts an assumed constant from another timing field to manufacture a value.
    """
    round_fn = round_fn or run_harness.run_round
    rounds = resolve_timing_rounds(cell.queue.timing_rounds)
    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []
    error: Optional[str] = None
    for round_index in range(rounds):
        run_state = TIMING_ROUND_STATES[0] if round_index == 0 else TIMING_ROUND_STATES[1]
        try:
            sample, completion = round_fn(
                handle.base_url, cell.alias, run_state, round_index,
                TIMING_PROMPT, TIMING_MAX_TOKENS, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 -- a timing arm never fails an otherwise-good
            # cell: the partial rounds already collected are kept, the reason is surfaced in
            # `detail`, and the caller's own disposition logic is left completely untouched.
            error = f"{type(exc).__name__}: {exc}"
            break
        samples.append(sample)
        completions.append(completion)
    return TimingArm(samples, completions, {
        "rounds_requested": rounds,
        "rounds_recorded": len(samples),
        "rounds_succeeded": sum(1 for s in samples if s.get("success") is True),
        "error": error,
    })


# -------------------------------------------------------------------------------------------
# Startup-verification-only runner (`run_queue`'s internal default)
# -------------------------------------------------------------------------------------------


def default_cell_runner(
    handle: "sweep.ScratchServerHandle", cell: Optional[ResolvedCell] = None,
) -> CellOutcome:
    """Verifies the scratch server actually came up healthy and passed startup-state
    verification, and marks the cell `completed` on that basis alone -- it runs NO battery.
    `run_queue`'s unchanged internal default; the CLI path wires in
    `make_longcontext_cell_runner` instead."""
    gate = classify_startup(handle.healthy, handle.verified)
    return (gate or COMPLETED).with_detail({"startup_details": handle.details})


# -------------------------------------------------------------------------------------------
# Live long-context runner
# -------------------------------------------------------------------------------------------


class _SamplingChoice(NamedTuple):
    requested: str
    effective: str
    payload: Dict[str, Any]
    fallback_reason: Optional[str]


def _choose_sampling(
    cell: ResolvedCell, production_sampling_fn: Callable[[str], Dict[str, float]],
) -> _SamplingChoice:
    """Resolves the cell's requested sampling arm into a concrete payload, closing
    `run_harness`'s documented `SAMPLING_ARM_PRODUCTION` None-placeholder deferral. A
    `production` request for a family `sampling_table` has no entry for falls back to
    `capability` and records why -- never a silent substitution.

    The per-trial seed is `longcontext.run_cell`'s own `derive_trial_seed` schedule (an
    established invariant), so the seed placed here is always overwritten before any request
    goes out.
    """
    requested = cell.queue.sampling_arm
    effective = requested
    fallback_reason: Optional[str] = None
    if requested == "production":
        try:
            preset = production_sampling_fn(cell.model_family)
        except sampling_table.MissingProductionSamplingError as exc:
            effective = "capability"
            fallback_reason = str(exc)
            preset = run_harness.SAMPLING_ARM_CAPABILITY
    else:
        preset = run_harness.SAMPLING_ARM_CAPABILITY
    return _SamplingChoice(
        requested, effective, run_harness.sampling_arm_payload(preset, seed=0), fallback_reason,
    )


def _read_vram(vram_used_bytes_fn: Callable[[], int]) -> Optional[int]:
    try:
        return vram_used_bytes_fn()
    except OSError:
        return None


def _persist(
    records_dir: Optional[Path],
    cell: ResolvedCell,
    handle: "sweep.ScratchServerHandle",
    battery_results: Dict[str, Dict[float, Any]],
    vram_after_bytes: Optional[int],
    sampling_arm_effective: Optional[str],
    vram_profile: Optional[str],
    response_caps: Dict[str, Any],
    timing: TimingArm,
):
    """Writes the record and returns `(record_path, record_error)`. A persistence failure (size
    budget, disk) NEVER aborts the queue or changes the cell's disposition -- it is surfaced in
    `detail` and caught by the verifier's own `records_present` check."""
    if records_dir is None:
        return None, None
    try:
        path = write_cell_record(
            Path(records_dir), cell, battery_results,
            vram_baseline_bytes=handle.vram_before_bytes,
            vram_loaded_bytes=vram_after_bytes,
            sampling_arm_effective=sampling_arm_effective,
            vram_profile=vram_profile,
            response_caps=response_caps,
            extra_samples=timing.samples,
            extra_completions=timing.completions,
            **cell_timing_fields(handle),
        )
        return str(path), None
    except Exception as exc:  # noqa: BLE001 -- a record write must never fail a cell.
        return None, f"{type(exc).__name__}: {exc}"


def make_longcontext_cell_runner(
    battery_fn: Optional[Callable[..., Dict[str, Dict[float, Any]]]] = None,
    production_sampling_fn: Optional[Callable[[str], Dict[str, float]]] = None,
    vram_used_bytes_fn: Optional[Callable[[], int]] = None,
    max_tokens: int = 256,
    timeout: float = 300,
    records_dir: Optional[Path] = None,
    vram_profile: Optional[str] = None,
    timing_round_fn: Optional[Callable[..., Any]] = None,
) -> Callable[["sweep.ScratchServerHandle", ResolvedCell], CellOutcome]:
    """Builds the real live `cell_runner` for a queue cell.

    The returned `_runner` is one linear sequence:

    1. **startup gate** (`cell_outcome.classify_startup`) -- an unhealthy or unverified server is
       decided before a single request is sent.
    2. **optional timing arm** -- runs FIRST, before the needle arm, so its round 0 is genuinely
       cold against the freshly verified server. It NEVER changes a cell's disposition; its own
       outcome is reported under `detail["timing"]`.
    3. **optional long-context measurement** -- a cell whose `battery` omits
       `LONGCONTEXT_BATTERY_ITEM` takes the startup-only path (VRAM read-backs persisted so a
       fit-measurement cell's numbers do not evaporate with the scratch server).
    4. **classify** -- `cell_outcome.classify_battery_samples`, a pure function over the samples.
    5. **persist** -- `cell_record.write_cell_record`, whose failure is reported but never fatal.

    `battery_fn`/`production_sampling_fn`/`vram_used_bytes_fn`/`timing_round_fn` default to the
    real, live-capable collaborators; the test suite always injects fakes.

    `vram_profile` is the effective profile string for the run, threaded unchanged into every
    record this runner writes alongside `response_caps` (`max_tokens` + the per-task overrides).
    Both land as top-level `config.json` keys.
    """
    battery_fn = battery_fn or longcontext.run_live_battery
    production_sampling_fn = production_sampling_fn or sampling_table.production_sampling
    vram_used_bytes_fn = vram_used_bytes_fn or sweep.read_vram_used_bytes
    # Fixed for the lifetime of this factory's runner (one `max_tokens` per `batch run`
    # invocation) -- computed once here rather than per-cell inside `_runner`.
    response_caps = {"default": max_tokens, "by_task": LONGCONTEXT_MAX_TOKENS_BY_TASK}

    def _startup_only(
        handle: "sweep.ScratchServerHandle", cell: ResolvedCell, timing: TimingArm,
    ) -> CellOutcome:
        """A cell with no `"needle"` battery item: startup verification is the whole
        measurement. VRAM read-backs are persisted via the state file's `detail` AND as a
        four-file record with an empty sample set -- a `completed` cell with no records
        subdirectory fails `verify_run`'s `records_present` check, which would page the
        unattended loop on every fit-measurement cell."""
        vram_after = _read_vram(vram_used_bytes_fn)
        detail: Dict[str, Any] = {
            "startup_details": handle.details,
            "vram_before_bytes": handle.vram_before_bytes,
            "vram_after_bytes": vram_after,
        }
        if timing.detail is not None:
            detail["timing"] = timing.detail
        record_path, record_error = _persist(
            records_dir, cell, handle, {}, vram_after, None, vram_profile, response_caps, timing,
        )
        if records_dir is not None:
            detail["record_path"] = record_path
            detail["record_error"] = record_error
        return COMPLETED.with_detail(detail)

    def _measure_longcontext(
        handle: "sweep.ScratchServerHandle", cell: ResolvedCell, timing: TimingArm,
    ) -> CellOutcome:
        arm_gate = classify_sampling_arm(cell.queue.sampling_arm)
        if arm_gate is not None:
            return arm_gate.with_detail({"sampling_arm": cell.queue.sampling_arm})

        trials = cell.queue.trials or longcontext.MIN_TRIALS_FOR_WILSON
        context = cell.context
        # Leaves headroom for the question/completion budget under the server's own `-c` ceiling
        # -- the same established fraction `run_harness.needle_probe`'s "near_ceiling" size uses,
        # reused here rather than reimplemented.
        target_tokens = max(1, int(context * run_harness.NEEDLE_NEAR_CEILING_FRACTION))
        sampling = _choose_sampling(cell, production_sampling_fn)

        # A `tokenize_fn` bound to THIS cell's live scratch server. `run_harness.tokenize_count`
        # raises (never silently defaults) on a transport failure or a body carrying no `tokens`
        # list; the try/except below turns that -- or a `longcontext.HaystackOverBudgetError` --
        # into an honest `battery_error` failed cell, never a silently-sent over-ceiling prompt.
        def _tokenize_fn(text: str, _base_url: str = handle.base_url) -> int:
            return run_harness.tokenize_count(_base_url, text)

        try:
            battery_results = battery_fn(
                handle.base_url, cell.alias, cell.cell_id, target_tokens,
                sampling.effective, sampling.payload, n_trials=trials,
                max_tokens=max_tokens, timeout=timeout,
                # Per-model measured haystack-sizing ratio (WP-5 calibration finding -- the
                # static estimate oversizes real prompts by +11-17%; at 512k that would overflow
                # `-c`). None falls back to the static estimate inside longcontext.
                chars_per_token=cell.queue.chars_per_token,
                max_tokens_by_task=LONGCONTEXT_MAX_TOKENS_BY_TASK,
                tokenize_fn=_tokenize_fn,
                token_budget=prompt_fit_budget(context, max_tokens),
            )
        except Exception as exc:  # pragma: no cover -- defensive: an unexpected transport/battery
            # exception never propagates out and aborts the whole queue; it becomes an ordinary
            # failed-cell disposition, same as a health timeout.
            detail = {"error": str(exc)}
            if timing.detail is not None:
                detail["timing"] = timing.detail
            return BATTERY_ERROR.with_detail(detail)

        vram_after_bytes = _read_vram(vram_used_bytes_fn)
        record_path, record_error = _persist(
            records_dir, cell, handle, battery_results, vram_after_bytes,
            sampling.effective, vram_profile, response_caps, timing,
        )

        samples_by_task = {
            task_name: [s for r in per_position.values() for s in r.samples]
            for task_name, per_position in battery_results.items()
        }
        detail = {
            "sampling_arm_requested": sampling.requested,
            "sampling_arm_effective": sampling.effective,
            "production_sampling_fallback_reason": sampling.fallback_reason,
            "model_family": cell.model_family,
            "target_tokens": target_tokens,
            "chars_per_token": cell.queue.chars_per_token,
            "vram_before_bytes": handle.vram_before_bytes,
            "vram_after_bytes": vram_after_bytes,
            "sample_count": sum(len(s) for s in samples_by_task.values()),
            "summaries": {
                f"{task_name}@{position_fraction:.2f}": cell_result.summary
                for task_name, per_position in battery_results.items()
                for position_fraction, cell_result in per_position.items()
            },
            "record_path": record_path,
            "record_error": record_error,
        }
        # Additive, and only for a cell that actually has a `"timing"` battery item -- a
        # needle-only cell's `detail` stays byte-identical to its pre-timing-arm shape.
        if timing.detail is not None:
            detail["timing"] = timing.detail

        return classify_battery_samples(samples_by_task).with_detail(detail)

    def _runner(handle: "sweep.ScratchServerHandle", cell: ResolvedCell) -> CellOutcome:
        gate = classify_startup(handle.healthy, handle.verified)
        if gate is not None:
            return gate.with_detail({"startup_details": handle.details})

        timing = NO_TIMING
        if TIMING_BATTERY_ITEM in cell.battery:
            timing = run_timing_rounds(handle, cell, round_fn=timing_round_fn, timeout=timeout)

        if LONGCONTEXT_BATTERY_ITEM not in cell.battery:
            return _startup_only(handle, cell, timing)
        return _measure_longcontext(handle, cell, timing)

    return _runner
