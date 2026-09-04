#!/usr/bin/env python3
"""Queue-driven unattended batch runner (plans/longcontext-pow2-overnight-campaign.md, WP-1).

Turns a self-contained queue file (a `models:` registry + an ordered `cells:` list of
abbreviated `{model, alias, c, ctk, ctv, fa, battery, sampling_arm, trials}` entries) into a
durable, resumable run: cells resolve against the queue's own registry into typed
`cell_model.ResolvedCell` values, execute serially through `sweep.py`'s scratch-server lifecycle
(never the router), and a per-cell/per-queue state machine persists to disk after every state
transition so a crashed or deadline-paused run resumes cleanly on the next invocation, and a
terminal queue (`completed`/`aborted-hang`) never silently re-runs.

**Module map** (WP-R1 of `plans/repository-tooling-structural-remediation.md` split this file
along the seams its own comment banners already marked; findings F2/F3/F6). This file now owns
only the run loop and the CLI:

- `cell_model.py` -- the typed immutable cell values. `CellIdentity` carries exactly the four
  fields `sweep.compute_cell_id` hashes and physically cannot carry anything else;
  `ExecutionSettings`/`QueueCellMetadata`/`RecordMetadata` carry every non-identity fact.
  `ResolvedCell.as_sweep_cell()` is the ONE dict adapter at the `sweep.py` boundary.
- `queue_file.py` -- queue-file parse/validate/resolve, and the HH:MM deadline.
- `queue_state.py` -- the durable `.state.json` machine: init/load/atomic-save, the
  `interrupted` rule, both re-arm forms, skip propagation.
- `cell_outcome.py` -- the outcome precedence, as a pure table-tested function.
- `cell_record.py` -- record persistence and the pre-enqueue size projection.
- `cell_runner.py` -- the live runners: startup gate -> optional timing -> optional
  long-context measurement -> classify -> persist.
- `hang_rail.py` -- the kernel hang-signature safety rail.

Every public name those modules define is re-exported below, so `batch.<name>` keeps working for
`verify_run.py` and every existing caller.

Zero new runtime dependencies: Python 3 stdlib only, matching `sweep.py`/`run_harness.py`'s
existing stance.

**No live operation happens unless a caller explicitly supplies live-capable collaborators**
(`ContainerController`, a real hang-check function, `sweep.py`'s own real VRAM readers/scratch-
server context manager). `run_queue`'s `scratch_server_fn` parameter defaults to
`sweep.scratch_server` (the real Docker-touching spawn/health/verify/teardown context manager)
but the test suite always injects a fake context-manager factory instead -- `batch run --dry-run`
and this repo's bake-off tests never spawn a scratch server, never call `docker`, and never read
host sysfs.

**Battery-execution seam**: `run_queue`'s `cell_runner` parameter. Its internal default stays
`cell_runner.default_cell_runner` (startup verification only, no battery) so every direct
`run_queue(...)` caller that omits `cell_runner=` is unaffected; `cmd_run` (the CLI path) wires
in `cell_runner.make_longcontext_cell_runner()` explicitly.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# `sweep`/`run_harness`/`longcontext`/`sampling_table` and this WP's own sibling modules live
# beside this file with no package `__init__.py` under `llama/` -- see longcontext.py's own
# comment on this exact gotcha. A plain top-level import works whenever the importer has already
# put this directory on `sys.path` (true when this file is run directly as a script, and when a
# test under `tests/` inserts this directory itself).
import run_harness
import sweep

# -- re-exports: the public surface `verify_run.py`, the bake-off tests, and the dashboard's own
# -- tooling have always reached for as `batch.<name>`. Kept at their historical home so the
# -- WP-R1 split is invisible to every caller outside this directory.
from cell_model import (  # noqa: F401
    CellIdentity,
    ExecutionSettings,
    QueueCellMetadata,
    RecordMetadata,
    ResolvedCell,
)
from cell_outcome import (  # noqa: F401
    STATUS_ABORTED_HANG,
    STATUS_COMPLETED,
    STATUS_FAILED,
    CellOutcome,
    Disposition,
    aborted_hang,
    classify_battery_samples,
    classify_sampling_arm,
    classify_startup,
)
from cell_record import (  # noqa: F401
    LARGE_CONTEXT_SIZE_CHECK_THRESHOLD,
    SIZE_PROJECTION_HEADROOM,
    cell_timing_fields,
    hf_repo_from_host_path,
    project_cell_record_size_bytes,
    write_cell_record,
)
from cell_runner import (  # noqa: F401
    LONGCONTEXT_BATTERY_ITEM,
    LONGCONTEXT_MAX_TOKENS_BY_TASK,
    TEMPLATE_MARGIN,
    TIMING_BATTERY_ITEM,
    TIMING_DEFAULT_ROUNDS,
    TIMING_MAX_TOKENS,
    TIMING_PROMPT,
    TIMING_ROUND_STATES,
    default_cell_runner,
    make_longcontext_cell_runner,
    prompt_fit_budget,
    resolve_timing_rounds,
    run_timing_rounds,
)
from hang_rail import (  # noqa: F401
    HANG_SIGNATURE_RE,
    detect_hang_signature,
    make_live_hang_check_fn,
    poll_journal_for_hang,
)
from queue_file import (  # noqa: F401
    DEFAULT_REQUEST_TIMEOUT_S,
    QUEUE_FORMAT_VERSION,
    SPEC_DECODE_VALUES,
    STOP_POLICIES,
    TIMING_ROUNDS_CELL_KEY,
    QueueValidationError,
    ResolvedQueue,
    compute_deadline_dt,
    effective_request_timeout_s,
    load_queue,
    load_resolved_queue,
    resolve_cell,
    resolve_queue_cells,
    validate_queue,
)
from queue_state import (  # noqa: F401
    ABORTED_HANG,
    CELL_STATES,
    COMPLETED,
    CORRECTNESS_FAILURE_REASONS,
    FAILED,
    PENDING,
    QUEUE_ABORTED_HANG,
    QUEUE_COMPLETED,
    QUEUE_PAUSED_RESUMABLE,
    QUEUE_RUNNING,
    RUNNING,
    SKIPPED_PROPAGATED,
    TERMINAL_QUEUE_STATES,
    apply_interrupted_rule,
    default_state_path,
    init_state,
    load_state,
    propagate_skips,
    re_arm_cells,
    re_arm_state,
    save_state,
)

MODULE_DIR = Path(__file__).resolve().parent


# -------------------------------------------------------------------------------------------
# GPU ownership / maintenance window (audit B3): stop the router container for the queue
# lifetime, restart on every exit path. `ContainerController` is injectable; the real one is
# never invoked by this repo's tests or by any of this module's own default code paths.
# -------------------------------------------------------------------------------------------


class ContainerController:
    """Real `docker stop`/`docker start` against the router container. Only ever constructed and
    invoked by `cmd_run` for a non-dry-run CLI invocation -- `run_queue` itself just calls
    whatever `container_controller` object it's given (or does nothing if `None`), so a test
    passing a fake object (or no controller at all) never touches Docker."""

    def __init__(self, container: str = sweep.DEFAULT_CONTAINER_NAME) -> None:
        self.container = container

    def stop(self) -> None:
        subprocess.run(["docker", "stop", self.container], check=True)

    def start(self) -> None:
        subprocess.run(["docker", "start", self.container], check=True)


class _GracefulExit(Exception):
    """Raised from the SIGTERM/SIGINT handler installed around the main run loop -- turns an
    external signal into an ordinary Python exception so the surrounding `try/finally`
    (container restart, signal-handler restoration) runs, same as any other exception unwind.
    SIGTERM does not raise anything by default; only this installed handler makes it
    `finally`-catchable ("trap-guaranteed" per the brief)."""


def _install_signal_traps():
    def _handler(signum, frame):
        raise _GracefulExit(f"batch run received signal {signum}")

    old_term = signal.signal(signal.SIGTERM, _handler)
    old_int = signal.signal(signal.SIGINT, _handler)
    return old_term, old_int


def _restore_signal_traps(old_handlers) -> None:
    old_term, old_int = old_handlers
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGINT, old_int)


# -------------------------------------------------------------------------------------------
# Cell execution seam (scratch-server lifecycle reused from sweep.py -- audit E1, single
# implementation, no duplication).
# -------------------------------------------------------------------------------------------


def _execute_cell(
    cell: ResolvedCell,
    cell_runner: Callable[["sweep.ScratchServerHandle", ResolvedCell], CellOutcome],
    port: int,
    container: str,
    scratch_server_fn: Callable[..., Any],
) -> CellOutcome:
    """`scratch_server_fn` is `sweep.scratch_server` by default (the real spawn/health/verify/
    teardown context manager -- Docker-touching) but is ALWAYS explicitly threaded through from
    `run_queue`, which itself defaults it the same way; every one of this repo's bake-off tests
    passes a fake context-manager factory instead (a plain `@contextlib.contextmanager` generator
    yielding a stand-in handle), so `run_queue`'s full cell loop is exercised without ever
    importing Docker into the test process.

    The scratch server gets the plain-dict adapter (`as_sweep_cell()`) because `sweep.py` speaks
    dicts; the cell RUNNER gets the typed value, because it needs the queue metadata that has no
    business being on a dict handed to `sweep.py`.
    """
    with scratch_server_fn(cell.as_sweep_cell(), port=port, container=container) as handle:
        outcome = cell_runner(handle, cell)
    return outcome


# -------------------------------------------------------------------------------------------
# Main run loop.
# -------------------------------------------------------------------------------------------


def _load_or_init_state(
    queue_path: Path,
    state_path: Path,
    resolved_cells: List[ResolvedCell],
    re_arm: bool,
    re_arm_cell_names: Optional[List[str]],
    request_timeout_s: int = DEFAULT_REQUEST_TIMEOUT_S,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Returns `(state, noop_result)`: exactly one is not `None`. A queue whose state file is
    already terminal (and that was not re-armed) yields the `noop_result` `run_queue` returns
    verbatim without running anything -- a terminal queue must never silently re-run.

    WP-6 (WP-2 punt): the state machine keys everything by cell_id, so a queue file edited AFTER
    its state file exists (cell added/removed, or any server-flag change -- cell_ids hash the
    flags dict) would otherwise surface as a bare KeyError deep in the cell loop. Fail loudly, up
    front, with the operator's actual options.

    `request_timeout_s` (WP-1, qwen4exp-bakeoff-campaign, cycle-3 M1) is the queue's current
    EFFECTIVE per-request transport timeout -- passed to `init_state` on first run, and compared
    against the existing state's own snapshot on resume: `cell_id` excludes this field, so a
    queue file edited after its state exists (changing only `request_timeout_s`) would otherwise
    resume undetected under a different timeout than the one the state file's evidence was
    captured under.
    """
    existing = load_state(state_path)
    if existing is None:
        return init_state(resolved_cells, request_timeout_s=request_timeout_s), None

    state = existing
    state_ids = set(state["cells"])
    queue_ids = {c.cell_id for c in resolved_cells}
    if state_ids != queue_ids:
        raise QueueValidationError(
            f"queue {queue_path} no longer matches its state file {state_path} "
            f"(cells only in state: {sorted(state_ids - queue_ids)}; only in queue: "
            f"{sorted(queue_ids - state_ids)}) -- a queue file is immutable once its state "
            f"exists; move the edited queue to a new file (fresh state) or delete/archive "
            f"the stale state file deliberately"
        )
    state_request_timeout_s = state.get("request_timeout_s", DEFAULT_REQUEST_TIMEOUT_S)
    if state_request_timeout_s != request_timeout_s:
        raise QueueValidationError(
            f"queue {queue_path}'s effective request_timeout_s ({request_timeout_s}) no longer "
            f"matches its state file {state_path}'s snapshot ({state_request_timeout_s}) -- a "
            f"queue file is immutable once its state exists; move the edited queue to a new "
            f"file (fresh state) or delete/archive the stale state file deliberately"
        )
    if state["queue_state"] in TERMINAL_QUEUE_STATES and not re_arm and not re_arm_cell_names:
        return None, {
            "noop": True,
            "queue_state": state["queue_state"],
            "message": (
                f"queue {queue_path} is already terminal ({state['queue_state']!r}); "
                f"pass --re-arm (or --re-arm-cell) to run it again"
            ),
        }
    if re_arm:
        re_arm_state(state)
    if re_arm_cell_names:
        re_arm_cells(state, list(re_arm_cell_names), resolved_cells)
    return state, None


def _size_check_failure(cell: ResolvedCell) -> Optional[Dict[str, Any]]:
    """Pre-enqueue size check (audit E4, pinned): a cell at or above the large-context threshold
    whose projected record would blow `run_harness.SIZE_LIMIT_BYTES` never runs. Returns the
    state-file `detail` for the failure, or `None` when the cell may be enqueued."""
    if cell.context < LARGE_CONTEXT_SIZE_CHECK_THRESHOLD:
        return None
    trials = cell.queue.trials if cell.queue.trials is not None else 1
    projected = project_cell_record_size_bytes(cell.context, trials)
    if projected <= run_harness.SIZE_LIMIT_BYTES:
        return None
    return {"projected_bytes": projected, "limit_bytes": run_harness.SIZE_LIMIT_BYTES}


def run_queue(
    queue_path: Path,
    state_path: Optional[Path] = None,
    vram_profile: Optional[str] = None,
    dry_run: bool = False,
    re_arm: bool = False,
    re_arm_cell_names: Optional[List[str]] = None,
    cell_runner: Optional[Callable[["sweep.ScratchServerHandle", ResolvedCell], CellOutcome]] = None,
    container_controller: Optional[ContainerController] = None,
    hang_check_fn: Optional[Callable[[], Optional[str]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    vram_total_bytes_fn: Optional[Callable[[], int]] = None,
    vram_used_bytes_fn: Optional[Callable[[], int]] = None,
    scratch_server_fn: Optional[Callable[..., Any]] = None,
    port: int = 8899,
    container: str = sweep.DEFAULT_CONTAINER_NAME,
    stop_marker_path: Optional[Path] = None,
    deadline_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """Runs (or resumes, or no-ops on, or re-arms) one queue. Returns a plain result dict; never
    raises for an ordinary cell failure (that's recorded in the state file) -- it DOES raise
    `QueueValidationError` for a malformed queue and `sweep.VRAMProfileRefused` for a mischosen
    `headless` profile (both are hold-for-user conditions, not per-cell dispositions).

    Every collaborator is injectable with a safe (offline, no-op, or real-but-never-exercised-by-
    tests) default -- see each parameter's own default and the module docstring's "no live
    operation happens unless explicitly supplied" invariant.

    `stop_marker_path` (control-plane WP-2): a wrapper/dispatcher-owned marker file checked at
    each cell boundary, beside the deadline check -- present -> consumed (unlinked) and the
    queue pauses resumable with `paused_reason:"stop-requested"`. Marker-absent is zero behavior
    change; `stop_marker_path=None` makes zero stat calls (no start-of-run stale-clear here --
    that's the wrapper's job, plan cycle-1 B1). `deadline_minutes` (also WP-2): when given,
    overrides the queue file's HH:MM `deadline` with `start_dt + timedelta(minutes=N)` for this
    invocation only -- the queue file itself stays immutable (req 7).

    WP-R1: loads and resolves `queue_path` through `load_resolved_queue` (one `load_queue` call,
    one `resolve_queue_cells` call) and delegates the rest to `_run_resolved_queue`, the internal
    run core `cmd_run` also drives directly off its own already-loaded `ResolvedQueue` -- so this
    wrapper and the CLI path never resolve the same queue file twice.
    """
    resolved = load_resolved_queue(Path(queue_path), vram_profile)
    return _run_resolved_queue(
        resolved,
        state_path=state_path,
        dry_run=dry_run,
        re_arm=re_arm,
        re_arm_cell_names=re_arm_cell_names,
        cell_runner=cell_runner,
        container_controller=container_controller,
        hang_check_fn=hang_check_fn,
        now_fn=now_fn,
        vram_total_bytes_fn=vram_total_bytes_fn,
        vram_used_bytes_fn=vram_used_bytes_fn,
        scratch_server_fn=scratch_server_fn,
        port=port,
        container=container,
        stop_marker_path=stop_marker_path,
        deadline_minutes=deadline_minutes,
    )


def _run_resolved_queue(
    resolved: ResolvedQueue,
    *,
    state_path: Optional[Path] = None,
    dry_run: bool = False,
    re_arm: bool = False,
    re_arm_cell_names: Optional[List[str]] = None,
    cell_runner: Optional[Callable[["sweep.ScratchServerHandle", ResolvedCell], CellOutcome]] = None,
    container_controller: Optional[ContainerController] = None,
    hang_check_fn: Optional[Callable[[], Optional[str]]] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    vram_total_bytes_fn: Optional[Callable[[], int]] = None,
    vram_used_bytes_fn: Optional[Callable[[], int]] = None,
    scratch_server_fn: Optional[Callable[..., Any]] = None,
    port: int = 8899,
    container: str = sweep.DEFAULT_CONTAINER_NAME,
    stop_marker_path: Optional[Path] = None,
    deadline_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """The once-loaded run core (WP-R1): everything `run_queue` used to do after its own
    `load_queue`/`resolve_queue_cells` calls, now driven from a single already-resolved
    `ResolvedQueue` so `run_queue` and `cmd_run` build cells, the effective VRAM profile/request
    timeout, the runner, and the state snapshot from the exact same loaded bytes -- never two
    independent reads/resolutions of the same queue file per invocation. Every parameter here is
    the same injectable seam `run_queue` takes (same defaults, same semantics); see that
    function's docstring for what each one does and raises.
    """
    queue_path = resolved.queue_path
    queue = resolved.queue
    resolved_cells = resolved.cells
    effective_vram_profile = resolved.vram_profile
    effective_timeout = resolved.request_timeout_s

    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    vram_total_bytes_fn = vram_total_bytes_fn or sweep.read_vram_total_bytes
    vram_used_bytes_fn = vram_used_bytes_fn or sweep.read_vram_used_bytes
    cell_runner = cell_runner or default_cell_runner
    hang_check_fn = hang_check_fn or (lambda: None)
    scratch_server_fn = scratch_server_fn or sweep.scratch_server

    if dry_run:
        return _dry_run_report(
            resolved_cells, effective_vram_profile, vram_total_bytes_fn, vram_used_bytes_fn,
        )

    state_path = Path(state_path) if state_path else default_state_path(queue_path)
    stop_marker_path = Path(stop_marker_path) if stop_marker_path is not None else None

    state, noop_result = _load_or_init_state(
        queue_path, state_path, resolved_cells, re_arm, re_arm_cell_names,
        request_timeout_s=effective_timeout,
    )
    if noop_result is not None:
        return noop_result

    apply_interrupted_rule(state)
    save_state(state_path, state)

    # Budget resolution up front -- raises VRAMProfileRefused for a mischosen headless profile
    # before anything (container stop, first cell) happens.
    sweep.compute_vram_budget_bytes(effective_vram_profile, vram_total_bytes_fn, vram_used_bytes_fn)

    if container_controller is not None:
        container_controller.stop()
    old_handlers = _install_signal_traps()
    try:
        start_dt = now_fn()
        if deadline_minutes is not None:
            deadline_dt = start_dt + timedelta(minutes=deadline_minutes)
        else:
            deadline_dt = compute_deadline_dt(queue["deadline"], start_dt)

        for cell in resolved_cells:
            cs = state["cells"][cell.cell_id]
            if cs["state"] != PENDING:
                continue

            if stop_marker_path is not None and stop_marker_path.exists():
                stop_marker_path.unlink()
                state["queue_state"] = QUEUE_PAUSED_RESUMABLE
                state["paused_reason"] = "stop-requested"
                save_state(state_path, state)
                break

            if now_fn() >= deadline_dt:
                state["queue_state"] = QUEUE_PAUSED_RESUMABLE
                state["paused_reason"] = "deadline"
                save_state(state_path, state)
                break

            size_failure = _size_check_failure(cell)
            if size_failure is not None:
                cs["state"] = FAILED
                cs["reason"] = "projected_size_exceeded"
                cs["detail"] = size_failure
                save_state(state_path, state)
                continue

            cs["state"] = RUNNING
            cs["reason"] = None
            save_state(state_path, state)

            outcome = _execute_cell(
                cell, cell_runner, port=port, container=container, scratch_server_fn=scratch_server_fn,
            )
            hang_line = hang_check_fn()

            # WP-5: persist the runner's compact detail (VRAM read-backs, sampling-arm
            # provenance, per-task Wilson summaries, record path) into the state file -- WP-2's
            # runner populated `CellOutcome.detail` but nothing wrote it durably, so the 262144
            # fit-measurement margin (audit B1's admission condition for WP-6) had no on-disk
            # home. `detail` is JSON-serializable by construction in every runner.
            cs["detail"] = outcome.detail

            if hang_line is not None or outcome.status == STATUS_ABORTED_HANG:
                cs["state"] = ABORTED_HANG
                cs["reason"] = hang_line or outcome.reason or "hang_signature"
                state["queue_state"] = QUEUE_ABORTED_HANG
                save_state(state_path, state)
                break

            if outcome.status == STATUS_COMPLETED:
                cs["state"] = COMPLETED
                cs["reason"] = None
            else:
                cs["state"] = FAILED
                cs["reason"] = outcome.reason
                if outcome.correctness_failure:
                    propagate_skips(resolved_cells, state, cell.cell_id)
            save_state(state_path, state)
        else:
            # Loop completed without `break` -- every cell reached a terminal per-cell state. A
            # stop marker that arrived during the final cell is consumed here (unlinked) but
            # otherwise inert -- the run was already finishing, so the queue still completes
            # (req 6: "pause-after-current-cell or no-op if the run completes first").
            if stop_marker_path is not None and stop_marker_path.exists():
                stop_marker_path.unlink()
            state["queue_state"] = QUEUE_COMPLETED
            save_state(state_path, state)
    finally:
        if container_controller is not None:
            try:
                container_controller.start()
            except Exception:  # noqa: BLE001 -- see below
                pass  # never block termination on a failed restart attempt
        _restore_signal_traps(old_handlers)

    return {"noop": False, "queue_state": state["queue_state"], "state": state}


def _dry_run_report(
    resolved_cells: List[ResolvedCell],
    vram_profile: str,
    vram_total_bytes_fn: Callable[[], int],
    vram_used_bytes_fn: Callable[[], int],
) -> Dict[str, Any]:
    """`batch run --dry-run`: resolves every cell and classifies its fit, without touching
    Docker, the state file, or the container. Same intent as `sweep.py --dry-run`, one level up
    (queue-level, not spec-level)."""
    out = []
    for cell in resolved_cells:
        fit = sweep.classify_fit(
            cell.as_sweep_cell(), vram_profile=vram_profile,
            vram_total_bytes_fn=vram_total_bytes_fn, vram_used_bytes_fn=vram_used_bytes_fn,
        )
        out.append({
            "cell_id": cell.cell_id,
            "alias": cell.alias,
            "model": cell.queue.model_id,
            "server_flags": cell.server_flags,
            "battery": list(cell.battery),
            "sampling_arm": cell.queue.sampling_arm,
            "trials": cell.queue.trials,
            "fit": fit,
        })
    return {
        "noop": False, "dry_run": True, "vram_profile": vram_profile,
        "cell_count": len(out), "cells": out,
    }


# -------------------------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    controller = None
    if not args.dry_run and not args.no_container_control:
        controller = ContainerController(container=args.container)
    # WP-R1 (once-loaded queue contract): `load_resolved_queue` reads and resolves this CLI
    # invocation's queue file exactly once, `--vram-profile` override applied here. `resolved.
    # vram_profile`/`resolved.request_timeout_s` are the SAME effective values stamped into
    # `make_longcontext_cell_runner` below and threaded into the state snapshot by
    # `_run_resolved_queue` (the internal run core, called directly -- not the public `run_queue`
    # wrapper, which would load and resolve the same file a second time). A malformed queue file
    # raises `QueueValidationError` here, before anything is spawned.
    resolved = load_resolved_queue(Path(args.queue), args.vram_profile)
    try:
        result = _run_resolved_queue(
            resolved,
            state_path=Path(args.state) if args.state else None,
            dry_run=args.dry_run,
            re_arm=args.re_arm,
            re_arm_cell_names=args.re_arm_cell or None,
            # The real CLI-driven path always runs the longcontext-aware cell runner (it takes
            # the startup-only path per-cell for anything without a `"needle"` battery item).
            # `run_queue`'s own internal default stays `default_cell_runner` -- untouched -- so
            # every direct `run_queue(...)` caller that omits `cell_runner=` is unaffected.
            cell_runner=make_longcontext_cell_runner(
                records_dir=Path(args.records_dir) if args.records_dir else None,
                vram_profile=resolved.vram_profile,
                timeout=resolved.request_timeout_s,
            ),
            container_controller=controller,
            # WP-5: wire the real kernel-log hang rail for live (non-dry-run) invocations;
            # dry-run keeps the no-op default (it never spawns anything to hang).
            hang_check_fn=None if args.dry_run else make_live_hang_check_fn(),
            port=args.port,
            # WP-5 (attended validation finding): the maintenance window `docker stop`s the
            # ROUTER container (`--container`), but `sweep.scratch_server` `docker exec`s into
            # its target -- a stopped container cannot host an exec. Live runs therefore spawn
            # scratch servers in a SIBLING container (`--scratch-container`: same image, same
            # GPU devices and model mounts, no port publish) while the router stays stopped for
            # the whole window; the default (scratch == router container) survives only for
            # dry-run/test paths that never stop anything.
            container=args.scratch_container or args.container,
            stop_marker_path=Path(args.stop_marker) if args.stop_marker else None,
            deadline_minutes=args.deadline_minutes,
        )
    except _GracefulExit as exc:
        print(f"batch run interrupted: {exc}", file=sys.stderr)
        return 130
    print(json.dumps(result, indent=2, sort_keys=False, default=str))
    if result.get("dry_run") or result.get("noop"):
        return 0
    return 1 if result["queue_state"] == QUEUE_ABORTED_HANG else 0


def cmd_re_arm(args: argparse.Namespace) -> int:
    """`batch.py re-arm`: standalone, state-only re-arm (transport-failure-taxonomy WP,
    `plans/bakeoff-transport-taxonomy-and-prompt-fit.md` WP-1 -- Decisions locked: "a new
    standalone re-arm path that mutates queue state WITHOUT starting a window"). Reuses the
    existing `re_arm_state` (all-non-completed)/`re_arm_cells` (targeted) helpers `run_queue`'s
    own `--re-arm`/`--re-arm-cell` flags already call, persisted via the same `save_state` atomic-
    write path `cmd_run`'s `run_queue` call uses. Deliberately never touches containers, locks,
    marker files, or `journalctl` -- no `ContainerController`, no `scratch_server_fn`, no
    `hang_check_fn` are constructed or invoked anywhere in this path; it only loads the queue
    (to resolve `--cell` aliases), loads the state file, mutates it in memory, and writes it back.

    `--cell` (repeatable): targeted form, exactly the named cells (`re_arm_cells` -- raises
    `QueueValidationError`, reported here as a clean exit-2 error, on an unknown cell_id/alias;
    nothing is re-armed or written on that path -- the raise happens before `save_state`).
    Omitted: the all-non-completed form (`re_arm_state`) -- every cell not already `completed`
    goes back to `pending`; "affected" here is computed from the PRE-mutation snapshot (every
    cell whose state was not `COMPLETED`), matching `re_arm_state`'s own documented behavior.

    Prints a small JSON object (`{"re_armed": [...], "queue_state": ...}`, matching `cmd_run`'s
    own `json.dumps(result, indent=2, ...)` output style) and exits 0. A missing state file (no
    `batch run` has ever been invoked for this queue) is a clean, explicit stderr message and
    exit 2, never a bare traceback.
    """
    queue_path = Path(args.queue)
    queue = load_queue(queue_path)
    resolved_cells = resolve_queue_cells(queue)

    state_path = Path(args.state) if args.state else default_state_path(queue_path)
    state = load_state(state_path)
    if state is None:
        print(
            f"batch re-arm: no state file found at {state_path} -- nothing to re-arm yet "
            f"(a queue's state file is created by its first 'batch run' invocation)",
            file=sys.stderr,
        )
        return 2

    if args.cell:
        try:
            affected = re_arm_cells(state, list(args.cell), resolved_cells)
        except QueueValidationError as exc:
            print(f"batch re-arm: {exc}", file=sys.stderr)
            return 2
    else:
        affected = [cid for cid, cs in state["cells"].items() if cs["state"] != COMPLETED]
        re_arm_state(state)

    save_state(state_path, state)
    print(json.dumps({"re_armed": affected, "queue_state": state["queue_state"]}, indent=2, sort_keys=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """`batch verify --run <dir>`: dispatch seam only in this module -- `verify_run.py` owns the
    verification. A clean, explicit error (never a traceback) if it isn't present."""
    try:
        import verify_run  # type: ignore
    except ImportError:
        print(
            f"verify_run.py not found (WP-4 not landed yet) -- 'batch verify' is a dispatch "
            f"seam only in WP-1; nothing to verify against {args.run!r} yet",
            file=sys.stderr,
        )
        return 2
    if not hasattr(verify_run, "main"):
        print("verify_run module has no main() entrypoint -- cannot delegate", file=sys.stderr)
        return 2
    return verify_run.main(["--run", args.run])


def _positive_int(value: str) -> int:
    """`argparse` `type=` for `--deadline-minutes`: rejects N <= 0 with the standard argparse
    error path (usage message + exit 2), never a bare `ValueError` traceback."""
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return n


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch.py",
        description="Queue-driven unattended batch runner "
        "(plans/longcontext-pow2-overnight-campaign.md WP-1). See llama/bakeoff/README.md.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run (or resume, or re-arm) a queue")
    p_run.add_argument("--queue", required=True)
    p_run.add_argument("--vram-profile", choices=sweep.VRAM_PROFILES, default=None)
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--re-arm", action="store_true")
    p_run.add_argument(
        "--re-arm-cell", action="append", default=[], metavar="CELL_ID_OR_ALIAS",
        help="re-arm exactly this cell (repeatable) -- the targeted counterpart to the "
        "all-or-nothing --re-arm; matches a state cell_id or a queue alias, errors on a "
        "name that matches neither",
    )
    p_run.add_argument("--state", default=None, help="override the default sibling state-file path")
    p_run.add_argument("--container", default=sweep.DEFAULT_CONTAINER_NAME)
    p_run.add_argument(
        "--scratch-container", default=None,
        help="container scratch servers exec into (a running SIBLING of the stopped router "
        "container for live runs -- see cmd_run's comment); defaults to --container",
    )
    p_run.add_argument(
        "--records-dir", default=None,
        help="run directory for verify_run.py-consumable per-cell records "
        "(llama/bakeoff/records/<run-name>); no records are written when omitted",
    )
    p_run.add_argument("--port", type=int, default=8899)
    p_run.add_argument(
        "--no-container-control", action="store_true",
        help="skip stop/start of the llama container around the run (manual/testing use only)",
    )
    p_run.add_argument(
        "--stop-marker", default=None, metavar="PATH",
        help="control-plane WP-2: path to a marker file checked at each cell boundary -- if "
        "present, it's consumed (unlinked) and the queue pauses resumable with "
        "paused_reason='stop-requested'; absent by default (zero behavior change)",
    )
    p_run.add_argument(
        "--deadline-minutes", type=_positive_int, default=None, metavar="N",
        help="control-plane WP-2: override the queue file's HH:MM deadline with "
        "start + N minutes for this invocation only (the manual-window mechanism; the queue "
        "file's own deadline stays untouched); N must be a positive integer",
    )
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser("verify", help="delegate to verify_run.py (WP-4)")
    p_verify.add_argument("--run", required=True)
    p_verify.set_defaults(func=cmd_verify)

    p_re_arm = sub.add_parser(
        "re-arm",
        help="state-only re-arm (transport-failure-taxonomy WP): mutate a queue's state file "
        "back to pending WITHOUT starting a window -- never touches containers/locks/markers",
    )
    p_re_arm.add_argument("--queue", required=True)
    p_re_arm.add_argument("--state", default=None, help="override the default sibling state-file path")
    p_re_arm.add_argument(
        "--cell", action="append", default=[], metavar="CELL_ID_OR_ALIAS",
        help="re-arm exactly this cell (repeatable) -- matches a state cell_id or a queue "
        "alias, errors on a name that matches neither; omit for the all-non-completed form "
        "(every cell not already completed goes back to pending)",
    )
    p_re_arm.set_defaults(func=cmd_re_arm)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
