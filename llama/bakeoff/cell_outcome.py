#!/usr/bin/env python3
"""Cell disposition: the pure outcome-precedence table (WP-R1 extraction from `batch.py`).

Before WP-R1 the precedence that decides what a cell's run *means* was spread across four places
-- `CellOutcome`'s field comments, `default_cell_runner`'s health gate, the long ladder at the
bottom of `make_longcontext_cell_runner._runner`, and `run_queue`'s own hang/completed/failed
branch. Reading the rules required chasing all four, and the one real invariant among them (which
reasons arm skip propagation) was enforced by hand at each construction site.

This module makes the precedence a **pure function of the measurement results**. Every
disposition is constructed here, by name, and `correctness_failure` is not a parameter any caller
gets to pass -- it is a property of the reason, asserted by `CORRECTNESS_FAILURE_REASONS`.

**The invariant** (tested directly): `correctness_failure` is True for exactly `degenerate` and
`needle_failure`, and never for `health_timeout` / `invalid_config` / `invalid_sampling_arm` /
`battery_error` / `transport_error` / `interrupted` / `projected_size_exceeded` / any
`aborted-hang`. A transport fact says nothing about whether the model would have answered
correctly, and neither does a startup, size, or hang fact -- so none of them may arm the
skip-propagation cascade that suppresses later cells.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, NamedTuple, Optional, Sequence

from queue_state import CORRECTNESS_FAILURE_REASONS

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_ABORTED_HANG = "aborted-hang"

# The two NIAH-style literal-recall tasks. A `wrong` verdict is treated as a correctness failure
# only for these -- deliberately NOT `passage_qa`, whose NoLiMa-class non-literal recall does not
# support the same inference (this module's own interpretive call; see the WP-2 report of
# `plans/longcontext-pow2-overnight-campaign.md`).
NEEDLE_RECALL_TASKS = ("single_needle", "multi_needle")


class Disposition(NamedTuple):
    """What a cell's run MEANS, with no attached evidence -- the pure half of `CellOutcome`.

    `correctness_failure` is derived, never supplied: see `_failed` below.
    """

    status: str
    reason: Optional[str] = None
    correctness_failure: bool = False

    def with_detail(self, detail: Optional[Dict[str, Any]] = None) -> "CellOutcome":
        return CellOutcome(self.status, self.reason, self.correctness_failure, detail)


class CellOutcome(NamedTuple):
    """A `Disposition` plus the evidence the runner collected. The wire shape `run_queue`
    consumes and persists into the state file's per-cell `detail`."""

    status: str  # "completed" | "failed" | "aborted-hang"
    reason: Optional[str] = None
    correctness_failure: bool = False  # drives skip propagation -- only True for degenerate/
    #  needle-recall-class reasons, never for health-timeout/OOM/hang/interrupted/size-exceeded.
    detail: Optional[Dict[str, Any]] = None

    @property
    def disposition(self) -> Disposition:
        return Disposition(self.status, self.reason, self.correctness_failure)


def _failed(reason: str) -> Disposition:
    """The single construction site for every `failed` disposition. `correctness_failure` is
    computed from the reason vocabulary, so a new failure reason cannot accidentally arm skip
    propagation by a caller passing the wrong boolean."""
    return Disposition(STATUS_FAILED, reason, reason in CORRECTNESS_FAILURE_REASONS)


COMPLETED = Disposition(STATUS_COMPLETED, None, False)

HEALTH_TIMEOUT = _failed("health_timeout")
INVALID_CONFIG = _failed("invalid_config")
INVALID_SAMPLING_ARM = _failed("invalid_sampling_arm")
BATTERY_ERROR = _failed("battery_error")
TRANSPORT_ERROR = _failed("transport_error")
DEGENERATE = _failed("degenerate")
NEEDLE_FAILURE = _failed("needle_failure")
INTERRUPTED = _failed("interrupted")
PROJECTED_SIZE_EXCEEDED = _failed("projected_size_exceeded")


def aborted_hang(reason: Optional[str] = None) -> Disposition:
    """A kernel hang signature (or a runner reporting `aborted-hang` itself). Never a correctness
    failure: a wedged GPU says nothing about model quality, and propagating skips off it would
    suppress cells that were never measured."""
    return Disposition(STATUS_ABORTED_HANG, reason or "hang_signature", False)


def classify_startup(healthy: bool, verified: bool) -> Optional[Disposition]:
    """The startup gate, checked before any request is sent. `None` means "startup was clean,
    continue"; a `Disposition` means the cell is already decided.

    Precedence is health first, then startup-state verification: an unhealthy server was never
    verifiable in the first place, so reporting `invalid_config` for it would be a guess.
    """
    if not healthy:
        return HEALTH_TIMEOUT
    if not verified:
        return INVALID_CONFIG
    return None


def classify_sampling_arm(sampling_arm: Optional[str]) -> Optional[Disposition]:
    """`None` when the requested arm is one this harness can run, else the failed disposition."""
    if sampling_arm not in ("capability", "production"):
        return INVALID_SAMPLING_ARM
    return None


def classify_battery_samples(samples_by_task: Mapping[str, Sequence[Mapping[str, Any]]]) -> Disposition:
    """**The outcome precedence, as one pure function.** Input is the battery's samples grouped
    by task name; output is the cell's disposition. No I/O, no clock, no globals.

    Precedence, in order (reordered by `plans/bakeoff-transport-taxonomy-and-prompt-fit.md` WP-1
    -- see that plan's Context for the production miscount it fixes):

    1. **transport** -- ANY sample whose `success` is `False` anywhere in the battery is a
       transport/HTTP failure (`longcontext.run_cell` records `classifier_verdict: "transport"`
       for these directly, bypassing the classifier). Checked FIRST so a transport fact can
       never be shadowed by, or shadow into, a correctness verdict, and never arms
       skip-propagation.
    2. **degenerate** -- ANY `success is not False` sample classified `degenerate`. The more
       specific, higher-priority correctness signal.
    3. **needle_failure** -- ANY `success is not False` sample of a `NEEDLE_RECALL_TASKS` task
       whose `classifier_verdict` is `"wrong"`: a coherent-but-incorrect literal-recall miss.
       Deliberately NOT `truncated(benign)`, which `classify_trial`'s own docstring says is not
       degeneracy evidence.
    4. otherwise **completed**.

    Steps 2 and 3 re-scope to `success is not False` samples as defense in depth alongside step
    1: even an older-schema record (a `success=False` row still tagged `wrong`/`degenerate`) can
    never trip a correctness-failure outcome.
    """
    all_samples = [s for samples in samples_by_task.values() for s in samples]
    if any(s.get("success") is False for s in all_samples):
        return TRANSPORT_ERROR
    if any(
        s.get("classifier_verdict") == "degenerate"
        for s in all_samples
        if s.get("success") is not False
    ):
        return DEGENERATE
    if any(
        s.get("classifier_verdict") == "wrong"
        for task_name, samples in samples_by_task.items()
        if task_name in NEEDLE_RECALL_TASKS
        for s in samples
        if s.get("success") is not False
    ):
        return NEEDLE_FAILURE
    return COMPLETED
