#!/usr/bin/env python3
"""Queue-driven unattended batch runner (plans/longcontext-pow2-overnight-campaign.md, WP-1).

Turns a self-contained queue file (a `models:` registry + an ordered `cells:` list of
abbreviated `{model, alias, c, ctk, ctv, fa, battery, sampling_arm, trials}` entries) into a
durable, resumable run: cells resolve against the queue's own registry into normalized
`sweep.py` cells, execute serially through `sweep.py`'s scratch-server lifecycle (never the
router), and a per-cell/per-queue state machine persists to disk after every state transition so
a crashed or deadline-paused run resumes cleanly on the next invocation, and a terminal queue
(`completed`/`aborted-hang`) never silently re-runs.

Zero new runtime dependencies: Python 3 stdlib only, matching `sweep.py`/`run_harness.py`'s
existing stance.

**No live operation happens unless a caller explicitly supplies live-capable collaborators**
(`ContainerController`, a real hang-check function, `sweep.py`'s own real VRAM readers/scratch-
server context manager). `run_queue`'s `scratch_server_fn` parameter defaults to `sweep.
scratch_server` (the real Docker-touching spawn/health/verify/teardown context manager) but this
WP's own tests always inject a fake context-manager factory instead -- `batch run --dry-run` and
this module's own test suite never spawn a scratch server, never call `docker`, and never read
host sysfs.

**Battery-execution seam (WP-1 scaffolding; landed by WP-2)**: WP-1 owns the queue/state-machine/
safety-rail scaffolding, not the actual longcontext battery run -- `run_queue`'s `cell_runner`
parameter is the seam; `default_cell_runner` here is WP-1's placeholder, verifying only
scratch-server startup state (see its own docstring), and stays `run_queue`'s unchanged internal
default (every direct `run_queue(...)` caller that omits `cell_runner=`, including this module's
and WP-1's own tests, is unaffected by WP-2). WP-2 lands the real battery hookup as
`make_longcontext_cell_runner()` (below `default_cell_runner`) -- `cmd_run` (the CLI path) wires
it in explicitly; it drives `longcontext.py`'s live entry (`run_live_battery`) for any cell whose
`battery` includes `"needle"`, filling the `production` sampling arm from `sampling_table.py`,
and defers to `default_cell_runner` for every other cell. This module's OWN tests still inject
fake `cell_runner`s (both `default_cell_runner`-style stubs and `make_longcontext_cell_runner`'s
own injected `battery_fn`/`production_sampling_fn`/`vram_used_bytes_fn` fakes) to exercise every
disposition (completed / health-timeout / OOM / degenerate / needle-recall failure) without ever
touching a live scratch server.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional

# `sweep`/`run_harness`/`longcontext`/`sampling_table` live beside this file with no package
# `__init__.py` under `llama/` -- see longcontext.py's own comment on this exact gotcha. A plain
# top-level import works whenever the importer has already put this directory on `sys.path` (true
# when this file is run directly as a script, and when a test under `tests/` inserts this
# directory itself).
import longcontext
import run_harness
import sampling_table
import sweep

MODULE_DIR = Path(__file__).resolve().parent

QUEUE_FORMAT_VERSION = 1

# The only currently-implemented stop policy: pause (never abort) at the queue's `deadline`.
# A required field, not a hard-coded behavior -- kept explicit and validated so a queue author
# never wonders what happens at a missed deadline, and so a future policy (e.g. a hard stop that
# also kills an in-flight cell) has a place to be added without a queue-format bump.
STOP_POLICIES = ("pause-at-deadline",)

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED_PROPAGATED = "skipped-propagated"
ABORTED_HANG = "aborted-hang"

CELL_STATES = (PENDING, RUNNING, COMPLETED, FAILED, SKIPPED_PROPAGATED, ABORTED_HANG)

QUEUE_RUNNING = "running"
QUEUE_PAUSED_RESUMABLE = "paused-resumable"
QUEUE_COMPLETED = "completed"
QUEUE_ABORTED_HANG = "aborted-hang"

TERMINAL_QUEUE_STATES = (QUEUE_COMPLETED, QUEUE_ABORTED_HANG)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -------------------------------------------------------------------------------------------
# Queue file: load, validate, resolve (audit E3, pinned: the queue file is self-contained -- no
# external registry file; cells resolve against the queue's own `models:` map).
# -------------------------------------------------------------------------------------------


class QueueValidationError(ValueError):
    """Raised by `load_queue`/`validate_queue` on a structurally invalid queue file -- never a
    silent partial parse."""


_REQUIRED_ABBREVIATED_CELL_KEYS = (
    "model", "alias", "c", "ctk", "ctv", "fa", "battery", "sampling_arm", "trials",
)


def validate_queue(queue: Dict[str, Any]) -> None:
    if queue.get("queue_format") != QUEUE_FORMAT_VERSION:
        raise QueueValidationError(
            f"unsupported queue_format {queue.get('queue_format')!r}; expected {QUEUE_FORMAT_VERSION}"
        )
    models = queue.get("models")
    if not isinstance(models, dict) or not models:
        raise QueueValidationError("queue has no non-empty 'models' registry map")
    for model_id, entry in models.items():
        missing = [k for k in ("model_container_path", "server_flags") if k not in entry]
        if missing:
            raise QueueValidationError(f"models[{model_id!r}] missing required key(s): {missing}")
        # WP-6: optional per-model measured haystack-sizing ratio (WP-5 calibration finding).
        cpt = entry.get("chars_per_token")
        if cpt is not None and (not isinstance(cpt, (int, float)) or isinstance(cpt, bool) or cpt <= 0):
            raise QueueValidationError(
                f"models[{model_id!r}] chars_per_token {cpt!r} must be a positive number when present"
            )
    cells = queue.get("cells")
    if not isinstance(cells, list) or not cells:
        raise QueueValidationError("queue has no non-empty 'cells' list")
    vram_profile = queue.get("vram_profile")
    if vram_profile not in sweep.VRAM_PROFILES:
        raise QueueValidationError(
            f"queue vram_profile {vram_profile!r} must be one of {sweep.VRAM_PROFILES}"
        )
    if not queue.get("deadline"):
        raise QueueValidationError("queue missing required 'deadline' (HH:MM wall-clock cutoff)")
    _parse_deadline_time(queue["deadline"])  # raises on malformed HH:MM
    stop_policy = queue.get("stop_policy")
    if stop_policy not in STOP_POLICIES:
        raise QueueValidationError(f"queue stop_policy {stop_policy!r} must be one of {STOP_POLICIES}")
    for idx, cell in enumerate(cells):
        missing = [k for k in _REQUIRED_ABBREVIATED_CELL_KEYS if k not in cell]
        if missing:
            raise QueueValidationError(f"cells[{idx}] missing required key(s): {missing}")
        if cell["model"] not in models:
            raise QueueValidationError(
                f"cells[{idx}] references unknown model {cell['model']!r} (not in this queue's own 'models' registry)"
            )


def load_queue(queue_path: Path) -> Dict[str, Any]:
    with open(queue_path, "r", encoding="utf-8") as f:
        queue = json.load(f)
    validate_queue(queue)
    return queue


def resolve_cell(model_entry: Dict[str, Any], abbreviated: Dict[str, Any]) -> Dict[str, Any]:
    """Merges one queue model registry entry's base `server_flags` with one abbreviated cell's
    `c`/`ctk`/`ctv`/`fa` overrides into the normalized cell shape `sweep.py` expects (its own
    `normalize_cell`), then attaches the abbreviated cell's queue-only metadata under
    underscore-prefixed keys (`_queue_model_id`/`_sampling_arm`/`_trials`) -- private to this
    module, never passed to `sweep.py`'s own functions, which only look at the keys they already
    know about."""
    flags = dict(model_entry.get("server_flags") or {})
    flags["-c"] = abbreviated["c"]
    flags["-ctk"] = abbreviated["ctk"]
    flags["-ctv"] = abbreviated["ctv"]
    flags["-fa"] = abbreviated["fa"]

    raw: Dict[str, Any] = {
        "alias": abbreviated["alias"],
        "model_container_path": model_entry["model_container_path"],
        "server_flags": flags,
        "battery": abbreviated["battery"],
    }
    if model_entry.get("model_host_path"):
        raw["model_host_path"] = model_entry["model_host_path"]
    if model_entry.get("gpu_weights_bytes") is not None:
        raw["gpu_weights_bytes"] = model_entry["gpu_weights_bytes"]
    if model_entry.get("kv_attention_layers") is not None:
        raw["kv_attention_layers"] = model_entry["kv_attention_layers"]
    if model_entry.get("swa_layer_spec") is not None:
        raw["swa_layer_spec"] = model_entry["swa_layer_spec"]
    if model_entry.get("model_family") is not None:
        # WP-2 addition: an optional, plain pass-through (same shape as gpu_weights_bytes/
        # kv_attention_layers/swa_layer_spec above), NOT queue-only metadata -- a GGUF
        # `general.architecture` string (`gemma4`/`qwen35`/`qwen35moe`/`bailingmoe3`/...) the
        # queue author already knows when writing the model registry entry. This is how
        # `make_longcontext_cell_runner` selects a `production_sampling()` table entry without
        # re-parsing the GGUF header on every live cell run; a queue that omits it simply never
        # gets the `production` sampling arm resolved (falls back to `capability`, same as an
        # unrecognized/silent family -- see that function's docstring).
        raw["model_family"] = model_entry["model_family"]

    cell = sweep.normalize_cell(raw)
    cell["cell_id"] = sweep.compute_cell_id(cell)
    cell["_queue_model_id"] = abbreviated["model"]
    cell["_sampling_arm"] = abbreviated["sampling_arm"]
    cell["_trials"] = abbreviated["trials"]
    # WP-6: queue-only metadata (underscore-prefixed, same contract as `_sampling_arm` --
    # never passed to sweep.py, and deliberately NOT part of the normalized cell, so adding or
    # tuning a model's measured `chars_per_token` never changes any cell_id). A haystack-sizing
    # knob is a measurement-accuracy fact, not a cell-identity fact.
    cell["_chars_per_token"] = model_entry.get("chars_per_token")
    return cell


def resolve_queue_cells(queue: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Resolves every abbreviated cell in queue order. Duplicate `cell_id`s across the resolved
    list (two abbreviated cells that happen to render identically) raise -- a queue's cells are
    meant to be distinct measurements, and the state machine keys everything by `cell_id`, so a
    collision would silently merge two cells' state."""
    models = queue["models"]
    out = []
    seen_ids = set()
    for idx, abbreviated in enumerate(queue["cells"]):
        model_entry = models[abbreviated["model"]]
        cell = resolve_cell(model_entry, abbreviated)
        if cell["cell_id"] in seen_ids:
            raise QueueValidationError(
                f"cells[{idx}] (alias={abbreviated['alias']!r}) resolves to a cell_id "
                f"{cell['cell_id']!r} already used by an earlier cell in this queue -- "
                f"two distinct queue entries must never collide"
            )
        seen_ids.add(cell["cell_id"])
        out.append(cell)
    return out


# -------------------------------------------------------------------------------------------
# Durable per-cell/per-queue state machine (audit B6).
# -------------------------------------------------------------------------------------------


def default_state_path(queue_path: Path) -> Path:
    return queue_path.with_name(queue_path.stem + ".state.json")


def init_state(resolved_cells: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "queue_state": QUEUE_RUNNING,
        "created_at": _utc_now_iso(),
        # WP-B: each cell's already-resolved `alias` (present on `resolved_cells` since before
        # `compute_cell_id` ran, inside `resolve_cell` -- this is not new per-cell metadata, just
        # a copy of an existing field, so cell_id computation is untouched) at the cell-entry TOP
        # LEVEL (beside `state`/`reason`), so a reader (the dashboard) can alias-join every cell
        # -- including a still-`pending` one -- without waiting for a `detail` to exist. An older
        # state file written before this WP simply lacks the key; nothing here or elsewhere
        # requires it on read.
        "cells": {
            c["cell_id"]: {"state": PENDING, "reason": None, "alias": c["alias"]}
            for c in resolved_cells
        },
    }


def load_state(state_path: Path) -> Optional[Dict[str, Any]]:
    if not state_path.exists():
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state_path: Path, state: Dict[str, Any]) -> None:
    """Atomic write (`os.replace` after a same-directory temp file) -- a crash mid-write never
    leaves a truncated/corrupt state file behind for the next invocation to trip over."""
    state["updated_at"] = _utc_now_iso()
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp_path, state_path)


def apply_interrupted_rule(state: Dict[str, Any]) -> List[str]:
    """A `running` cell found at startup means the previous invocation crashed mid-cell (a clean
    exit always leaves a cell `completed`/`failed`/`aborted-hang`/`skipped-propagated`, never
    `running`) -- mark it `failed` with reason `interrupted`, never silently re-run it. Returns
    the list of cell_ids changed."""
    changed = []
    for cell_id, cs in state["cells"].items():
        if cs["state"] == RUNNING:
            cs["state"] = FAILED
            cs["reason"] = "interrupted"
            changed.append(cell_id)
    return changed


def re_arm_state(state: Dict[str, Any]) -> None:
    """`--re-arm`: every cell not already `completed` goes back to `pending` (this includes
    `failed`, `skipped-propagated`, and `aborted-hang` cells -- a full retry of everything that
    didn't succeed), and the queue state resets to `running`. A `completed` cell's record already
    exists and is never re-run by a re-arm. A `paused_reason` left over from a prior deadline/stop
    pause is cleared (popped) -- it no longer describes anything once the queue is re-armed."""
    for cs in state["cells"].values():
        if cs["state"] != COMPLETED:
            cs["state"] = PENDING
            cs["reason"] = None
    state["queue_state"] = QUEUE_RUNNING
    state.pop("paused_reason", None)


def re_arm_cells(
    state: Dict[str, Any], names: List[str], resolved_cells: List[Dict[str, Any]]
) -> List[str]:
    """`--re-arm-cell <cell_id-or-alias>` (WP-6; WP-5 punt): the targeted, per-cell counterpart
    to the all-or-nothing `re_arm_state` -- exactly the surgical state-file hand-edit WP-5's
    attended run performed three times, made a first-class knob. Each named cell (matched by
    `cell_id` or queue `alias`) goes back to `pending` regardless of its current terminal state
    (including `completed` -- an operator explicitly naming a cell wants it re-run), and the
    queue state resets to `running`. Unknown names raise `QueueValidationError` -- a typo must
    never silently re-arm nothing. Returns the affected cell_ids."""
    by_alias = {c["alias"]: c["cell_id"] for c in resolved_cells}
    changed = []
    for name in names:
        cell_id = name if name in state["cells"] else by_alias.get(name)
        if cell_id is None or cell_id not in state["cells"]:
            raise QueueValidationError(
                f"--re-arm-cell {name!r} matches no cell_id or alias in this queue's state"
            )
        cs = state["cells"][cell_id]
        cs["state"] = PENDING
        cs["reason"] = None
        changed.append(cell_id)
    if changed:
        state["queue_state"] = QUEUE_RUNNING
    return changed


CORRECTNESS_FAILURE_REASONS = ("degenerate", "needle_failure")


def propagate_skips(
    resolved_cells: List[Dict[str, Any]],
    state: Dict[str, Any],
    failing_cell_id: str,
) -> List[str]:
    """Skip propagation (audit E2, pinned): when `failing_cell_id`'s KV arm is quantized
    (`-ctk`/`-ctv` != f16) and it failed for a correctness reason, mark every LATER `pending` cell
    (queue order) with the same `(_queue_model_id, ctk, ctv)` and a larger `-c` as
    `skipped-propagated`, referencing the failing cell. Purely mechanical -- the caller decides
    whether `failing_cell_id`'s failure reason qualifies as a correctness failure at all; this
    function itself only re-checks the quantized-KV-arm half of the gate (defense in depth) and
    is otherwise unconditional given a failing_cell_id."""
    by_id = {c["cell_id"]: c for c in resolved_cells}
    failing = by_id[failing_cell_id]
    f_flags = failing["server_flags"]
    f_ctk, f_ctv, f_c = f_flags.get("-ctk", "f16"), f_flags.get("-ctv", "f16"), f_flags["-c"]
    if f_ctk == "f16" and f_ctv == "f16":
        return []
    f_model = failing["_queue_model_id"]
    changed = []
    for cell in resolved_cells:
        if cell["cell_id"] == failing_cell_id:
            continue
        flags = cell["server_flags"]
        if cell["_queue_model_id"] != f_model:
            continue
        if flags.get("-ctk", "f16") != f_ctk or flags.get("-ctv", "f16") != f_ctv:
            continue
        if flags["-c"] <= f_c:
            continue
        cs = state["cells"].get(cell["cell_id"])
        if cs is not None and cs["state"] == PENDING:
            cs["state"] = SKIPPED_PROPAGATED
            cs["reason"] = f"propagated from failing cell {failing_cell_id}"
            changed.append(cell["cell_id"])
    return changed


# -------------------------------------------------------------------------------------------
# Deadline (a daily HH:MM wall-clock cutoff, recomputed fresh relative to each invocation's own
# start time -- a queue that spans multiple nights gets a fresh deadline each night, by design).
# -------------------------------------------------------------------------------------------


def _parse_deadline_time(deadline: str) -> tuple:
    try:
        hh, mm = deadline.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        raise QueueValidationError(f"deadline {deadline!r} is not a valid 'HH:MM' wall-clock time")
    return hh, mm


def compute_deadline_dt(deadline: str, start_dt: datetime) -> datetime:
    """The next occurrence of `deadline` (HH:MM) at or after `start_dt` -- if the time-of-day has
    already passed today relative to `start_dt`, the deadline is tomorrow (the overnight-run
    case: start at 23:00, deadline '07:00' next morning); otherwise it's later today."""
    hh, mm = _parse_deadline_time(deadline)
    candidate = start_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= start_dt:
        candidate += timedelta(days=1)
    return candidate


# -------------------------------------------------------------------------------------------
# Pre-enqueue size check (audit E4, pinned): before running any cell with c >= 262144, project
# the record size data-driven from the ACTUAL record shape (run_harness.needle_probe's sample/
# completion dicts), never a hand-guessed number.
# -------------------------------------------------------------------------------------------

LARGE_CONTEXT_SIZE_CHECK_THRESHOLD = 262144
SIZE_PROJECTION_HEADROOM = 1.25

# Conservative worst-case field-length assumptions for the projection -- deliberately larger than
# any real value this harness produces (never used to WRITE a real record, only to bound the
# projection above what a real cell of this shape could possibly serialize to).
_MAX_COMPLETION_TOKENS = 2048
_MAX_STRING_FIELD_CHARS = 4000
_CHARS_PER_TOKEN_CONSERVATIVE = 5  # generous vs. this repo's own measured ~3.7-4.2 chars/token


def project_cell_record_size_bytes(cell: Dict[str, Any], trials: int) -> int:
    """Conservative pre-enqueue size projection for one cell's contribution to
    `runs/bakeoff/<experiment-id>/{samples,completions}.jsonl` (the two per-trial files;
    `run_harness.write_experiment_record`'s `SIZE_LIMIT_BYTES` budget covers the whole
    experiment directory, but a single near-ceiling cell's own trials are what could blow it).
    Builds one synthetic WORST-CASE sample + completion pair matching the real
    `needle_probe`/`longcontext.py` record shape (every string field at
    `_MAX_STRING_FIELD_CHARS`, a `_MAX_COMPLETION_TOKENS`-sized completion text), serializes it
    exactly as `write_experiment_record` would (`json.dumps` + trailing newline per JSONL line),
    multiplies the per-trial total by `trials`, then by `SIZE_PROJECTION_HEADROOM` (25%
    headroom against the projection's own inevitable drift from a real record's exact field
    lengths). Never writes anything to disk."""
    filler = "x" * _MAX_STRING_FIELD_CHARS
    completion_text = "y" * (_MAX_COMPLETION_TOKENS * _CHARS_PER_TOKEN_CONSERVATIVE)
    context = cell["server_flags"].get("-c")

    sample = {
        "type": "needle_probe",
        "schema_version": 2,
        "size": "near_ceiling",
        "target_tokens": context,
        "context_ceiling": context,
        "position_fraction": 0.45,
        "seed_used": 0,
        "haystack_seed": 0,
        "sampler_seed": 1,
        "sampling_arm": "production",
        "temperature": 1.0,
        "top_p": 1.0,
        "top_k": 50,
        "corpus_manifest_id": "gutenberg-longcontext-haystack-v2",
        "haystack_sha256": "0" * 64,
        "classifier_verdict": "correct",
        "prompt_tokens": context,
        "expected_code": filler[:64],
        "correct": True,
        "success": True,
        "error": filler,  # worst case: an error string present even on a "successful" projection
        "finish_reason": "stop",
        "wall_seconds": 999.999,
        "timestamp": "2026-08-23T00:00:00+00:00",
    }
    completion = {
        "type": "needle_probe",
        "size": "near_ceiling",
        "expected_code": filler[:64],
        "content_text": completion_text,
        "reasoning_text": completion_text,
        "finish_reason": "stop",
    }
    one_sample_bytes = len(json.dumps(sample, ensure_ascii=False).encode("utf-8")) + 1
    one_completion_bytes = len(json.dumps(completion, ensure_ascii=False).encode("utf-8")) + 1
    projected = (one_sample_bytes + one_completion_bytes) * max(trials, 1)
    return int(projected * SIZE_PROJECTION_HEADROOM)


# -------------------------------------------------------------------------------------------
# Unattended safety rail -- hang-signature detection (audit, brief's own text).
#
# Provenance (verbatim signature, copied deliberately rather than re-derived): both lines are
# quoted directly from fleet-tools/references/rdna4-gpu-hang.md ("The signature" section,
# and "Automated recovery" section: "either `MES might be in unrecoverable state` or `Failed to
# evict process queues` alone is enough -- journald delivers lines one at a time, so the full
# cascade never arrives as one matchable unit") and match
# fleet-tools/scripts/gpu-hang-watchdog.sh's own `SIGNATURE_RE` byte-for-byte.
# -------------------------------------------------------------------------------------------

HANG_SIGNATURE_RE = re.compile(r"MES might be in unrecoverable state|Failed to evict process queues")


def detect_hang_signature(lines: Iterable[str]) -> Optional[str]:
    """Scans `lines` (any iterable of decoded kernel-log lines -- a finite test list, or a real
    live source) and returns the first line matching the hang signature, or `None` if the
    iterable is exhausted with no match. Pure function, no I/O -- the piece this WP's tests
    exercise directly."""
    for line in lines:
        if HANG_SIGNATURE_RE.search(line):
            return line
    return None


def poll_journal_for_hang(since_iso: str, timeout: float = 5.0) -> Optional[str]:
    """LIVE: `journalctl -k --since <since_iso>` (bounded, not `-f`) -- run_queue's per-cell
    `hang_check_fn` calling this repeatedly achieves the same never-miss-a-line property as a
    persistent `-f` follower (each poll's `--since` covers everything since the last check)
    without a background thread/subprocess-lifecycle to manage. NEVER invoked by this WP's own
    tests -- `detect_hang_signature` above is the tested, pure piece; this function only wraps it
    with the real subprocess call, same "real function exists, only ever invoked live" pattern as
    `sweep.py`'s own `discover_bridge_ip`/`read_vram_used_bytes`."""
    try:
        # errors='replace' (WP-5 finding #5, same class as sweep.py's startup-log read): kernel
        # log lines are not guaranteed UTF-8, and a strict decode would crash the hang rail
        # mid-run. The hang signatures are all ASCII -- lossy replacement never masks one.
        result = subprocess.run(
            ["journalctl", "-k", "--since", since_iso],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return detect_hang_signature(result.stdout.splitlines())


# -------------------------------------------------------------------------------------------
# GPU ownership / maintenance window (audit B3): stop the router container for the queue
# lifetime, restart on every exit path. `ContainerController` is injectable; the real one is
# never invoked by this WP's tests or by anything in this module's own default code paths.
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


class CellOutcome(NamedTuple):
    status: str  # "completed" | "failed" | "aborted-hang"
    reason: Optional[str] = None
    correctness_failure: bool = False  # drives skip propagation -- only True for degenerate/
    #  needle-recall-class reasons, never for health-timeout/OOM/hang/interrupted/size-exceeded.
    detail: Optional[Dict[str, Any]] = None


def default_cell_runner(handle: "sweep.ScratchServerHandle", cell: Dict[str, Any]) -> CellOutcome:
    """WP-1's own placeholder cell runner (see module docstring's "Battery-execution seam").
    Verifies the scratch server actually came up healthy and passed startup-state verification,
    and marks the cell `completed` on that basis alone -- it runs NO battery. WP-2 replaces this
    via `run_queue`'s `cell_runner` parameter."""
    if not handle.healthy:
        return CellOutcome("failed", "health_timeout", False, {"startup_details": handle.details})
    if not handle.verified:
        return CellOutcome("failed", "invalid_config", False, {"startup_details": handle.details})
    return CellOutcome("completed", None, False, {"startup_details": handle.details})


# -------------------------------------------------------------------------------------------
# Live longcontext battery hookup (plan `longcontext-pow2-overnight-campaign.md`, WP-2). This is
# the real `cell_runner` `run_queue`'s "Battery-execution seam" docstring names -- `cmd_run` wires
# it in (see below); `run_queue`'s own internal default stays `default_cell_runner` UNCHANGED (so
# every WP-1 test that omits `cell_runner=` and relies on that default keeps passing byte-for-byte
# -- this WP touches no state-machine code).
# -------------------------------------------------------------------------------------------

# The abbreviated cell's `battery` vocabulary (`sweep.VALID_BATTERY_ITEMS`) is coarse-grained;
# `"needle"` is the one item this WP's runner understands -- any cell whose `battery` does NOT
# include it defers entirely to `default_cell_runner` (startup-verification only), never silently
# dropped.
LONGCONTEXT_BATTERY_ITEM = "needle"

# WP-6 (WP-5 punt, measured): the flat 256-token response cap is too tight for multi_needle
# under the verbose production arm (temp 1.0) -- gemma f16's multi_needle went 0/9
# all-`truncated(benign)` in WP-5's attended run while single_needle at the same cap scored
# 8/9. multi_needle asks the model to enumerate SEVERAL recalled values, so it gets 3x the
# budget; every other task keeps the flat `max_tokens` default.
LONGCONTEXT_MAX_TOKENS_BY_TASK = {"multi_needle": 768}

# WP-2 (transport-taxonomy-and-prompt-fit, plan `plans/bakeoff-transport-taxonomy-and-prompt-fit.
# md`): fixed constant absorbing chat-template overhead PLUS needle/question insertion --
# `run_harness.tokenize_count` counts the raw haystack text, never the chat-templated prompt that
# actually goes on the wire, so this margin stands in for that difference (plan Assumptions:
# "tens of tokens each; 512 is deliberately generous").
TEMPLATE_MARGIN = 512


def _prompt_fit_budget(context: int, max_tokens: int) -> int:
    """One conservative scalar token budget for the live prompt-fit shrink loop
    (`longcontext._assemble_haystack_fit`, wired in by `make_longcontext_cell_runner`'s
    `_runner`): `context` (the cell's own `-c`) minus the LARGEST response cap this battery run
    could enforce (the flat `max_tokens` this factory was built with, or any
    `LONGCONTEXT_MAX_TOKENS_BY_TASK` override) minus `TEMPLATE_MARGIN`.

    One scalar budget, not a per-task one, is a deliberate simplification (WP-2 brief: "one
    conservative scalar budget is fine; per-task budgets are optional if the plumbing stays
    clean") -- using the LARGEST response cap across every task in the battery keeps the
    governing invariant true for every task, not just the flat-capped ones: no prompt this
    battery run sends may ever exceed `context - response_cap_for_its_task - TEMPLATE_MARGIN`.
    """
    response_cap = max([max_tokens, *LONGCONTEXT_MAX_TOKENS_BY_TASK.values()])
    return context - response_cap - TEMPLATE_MARGIN


def write_cell_record(
    records_dir: Path,
    cell: Dict[str, Any],
    battery_results: Dict[str, Dict[float, Any]],
    vram_baseline_bytes: Optional[int],
    vram_loaded_bytes: Optional[int],
    sampling_arm_effective: str,
    vram_profile: Optional[str] = None,
    response_caps: Optional[Dict[str, Any]] = None,
) -> Path:
    """WP-5 glue: persists one live cell's battery output as a standard four-file
    `run_harness.write_experiment_record` directory under `records_dir/<cell_id>/` -- exactly the
    run-directory convention `verify_run.py` (WP-4) consumes (`experiment_id == cell_id`, one
    subdirectory per queue cell). WP-2 deliberately left disk persistence out of the cell runner
    (its `CellOutcome.detail` carries only a compact summary); this closes that gap for the CLI
    path.

    Lineage: `filename`/`file_size` come from the cell's own `model_host_path` (stat'd live);
    `sha256`/`hf_repo`/`hf_revision`/`llama_cpp_build` are recorded as explicit `None` --
    `REQUIRED_LINEAGE_FIELDS` requires every key present, and permits `None` for genuinely
    unresolved values (hashing a multi-GiB GGUF per cell is not worth the wall-clock; the host
    path + size pin the artifact well enough for this campaign's records).

    `wilson_summary`: the first non-null per-task/position summary from the battery (Wilson
    intervals exist only where `n_trials >= longcontext.MIN_TRIALS_FOR_WILSON`); `None` for
    smoke-depth cells -- `write_experiment_record` keeps the key present either way.

    WP-B (additive; `run_harness.write_experiment_record`'s `schema_version` stays 2): two
    top-level self-description keys, both optional and defaulting to `None` for a caller that
    doesn't supply them (`TestWriteCellRecord`'s direct positional calls, pre-WP-B) --
    `vram_profile`, the effective `--vram-profile`/queue `vram_profile` string this cell ran
    under, and `response_caps`, the response-length budget the battery actually enforced
    (`{"default": <flat max_tokens>, "by_task": LONGCONTEXT_MAX_TOKENS_BY_TASK}`).
    """
    cell_id = cell["cell_id"]
    host_path = cell.get("model_host_path")
    file_size = None
    filename = None
    if host_path:
        filename = Path(host_path).name
        try:
            file_size = Path(host_path).stat().st_size
        except OSError:
            file_size = None
    lineage = {
        "hf_repo": None, "hf_revision": None, "filename": filename,
        "file_size": file_size, "sha256": None, "llama_cpp_build": None,
    }
    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []
    wilson_summary: Optional[Dict[str, Any]] = None
    for per_position in battery_results.values():
        for cell_result in per_position.values():
            samples.extend(cell_result.samples)
            completions.extend(cell_result.completions)
            if wilson_summary is None and cell_result.summary is not None:
                wilson_summary = cell_result.summary
    config = {
        "lineage": lineage,
        "sweep_cell": {
            "cell_id": cell_id,
            "alias": cell.get("alias"),
            "server_flags": cell.get("server_flags"),
            "model_host_path": host_path,
            "battery": cell.get("battery"),
            "sampling_arm": sampling_arm_effective,
        },
        "vram": {
            "baseline_bytes": vram_baseline_bytes,
            "loaded_bytes": vram_loaded_bytes,
            "after_teardown_bytes": None,
        },
        "wilson_summary": wilson_summary,
        "vram_profile": vram_profile,
        "response_caps": response_caps,
    }
    run_harness.write_experiment_record(
        cell_id, config, [], completions, samples, base_dir=records_dir,
    )
    return records_dir / cell_id


def make_longcontext_cell_runner(
    battery_fn: Optional[Callable[..., Dict[str, Dict[float, Any]]]] = None,
    production_sampling_fn: Optional[Callable[[str], Dict[str, float]]] = None,
    vram_used_bytes_fn: Optional[Callable[[], int]] = None,
    max_tokens: int = 256,
    timeout: float = 300,
    records_dir: Optional[Path] = None,
    vram_profile: Optional[str] = None,
) -> Callable[["sweep.ScratchServerHandle", Dict[str, Any]], CellOutcome]:
    """Builds the real live `cell_runner` for a `battery: [..., "needle"]` queue cell:
    `longcontext.run_live_battery`'s full task x position x trial sweep against `handle`'s live
    scratch server, with the production sampling arm (when requested) filled from
    `sampling_table.production_sampling` -- the harness's documented `SAMPLING_ARM_PRODUCTION`
    None-placeholder deferral, closed here via `run_harness.sampling_arm_payload`'s existing
    contract (`run_harness.py` itself is not edited).

    `battery_fn`/`production_sampling_fn`/`vram_used_bytes_fn` default to the real, live-capable
    collaborators (`longcontext.run_live_battery`, `sampling_table.production_sampling`,
    `sweep.read_vram_used_bytes`) -- same "real default, fake injected in tests" pattern this
    module already uses for `container_controller`/`scratch_server_fn`/`hang_check_fn`; this WP's
    own tests always inject fakes, so this factory's unit tests never touch a live scratch server
    or host sysfs.

    A cell whose `battery` does not include `"needle"` (`LONGCONTEXT_BATTERY_ITEM`) defers
    entirely to `default_cell_runner` -- this runner only OWNS the longcontext-battery cells; any
    other battery item (`vram`/`timing`/`quality`) gets the WP-1 startup-verification-only
    disposition, same as before this WP existed.

    **CellOutcome mapping** (brief-pinned, exact strings; REORDERED by the transport-failure-
    taxonomy WP -- `plans/bakeoff-transport-taxonomy-and-prompt-fit.md` WP-1 -- see that plan's
    Context for the production miscount this fixes): ANY sample whose `success` is `False`
    anywhere in the battery (a transport/HTTP failure -- `longcontext.run_cell` records
    `classifier_verdict: "transport"` for these directly, bypassing the classifier, never a
    correctness verdict) -> `failed`, `correctness_failure=False`, `reason="transport_error"`.
    Checked FIRST, ahead of the correctness checks below (defense in depth: a transport fact
    says nothing about whether the model would have answered correctly, so it must never be
    shadowed by -- or itself shadow into -- a `degenerate`/`needle_failure` verdict, and must
    never arm skip-propagation). Otherwise, `has_degenerate`/`has_needle_failure` below are each
    computed only over samples with `success is not False` (defense in depth again, same
    invariant from the predicate side): ANY such sample classified `degenerate` -> `failed`,
    `correctness_failure=True`, `reason="degenerate"` (the more specific, higher-priority
    correctness signal). Otherwise, ANY `success`-true `single_needle`/`multi_needle` sample
    whose `classifier_verdict == "wrong"` (a coherent-but-incorrect literal-recall miss --
    deliberately NOT `truncated(benign)`, which `classify_trial`'s own docstring says is not
    degeneracy evidence, and deliberately scoped to the two NIAH-style needle tasks, not
    `passage_qa`'s NoLiMa-class non-literal recall -- this module's own interpretive call, see the
    WP-2 report) -> `failed`, `correctness_failure=True`, `reason="needle_failure"`. None of the
    above -> `completed`. `"degenerate"`/`"needle_failure"` (never `"transport_error"`) are
    `CORRECTNESS_FAILURE_REASONS` above -- `propagate_skips` (WP-1, unmodified) keys off
    `correctness_failure`, not the reason string itself, but the string is kept in that pinned
    vocabulary for a downstream reader (verify_run.py, WP-4) to match against.

    Health/verification failure (`not handle.healthy` / `not handle.verified`) is checked FIRST,
    before the battery ever runs -- `failed`, `correctness_failure=False`, matching
    `default_cell_runner`'s existing health-gate behavior exactly (never sends a single request to
    an unhealthy/unverified server).

    WP-B: `vram_profile` (default `None`, matching every other optional collaborator here) is
    the effective profile string for the run -- `cmd_run` resolves it the same way `run_queue`
    does (`--vram-profile` override, else the queue's own `vram_profile`) before building this
    runner, and it's threaded through unchanged into every `write_cell_record` call this runner
    makes, alongside `response_caps` (`max_tokens` above + `LONGCONTEXT_MAX_TOKENS_BY_TASK`) --
    both land as top-level `config.json` keys for a persisted record.
    """
    battery_fn = battery_fn or longcontext.run_live_battery
    production_sampling_fn = production_sampling_fn or sampling_table.production_sampling
    vram_used_bytes_fn = vram_used_bytes_fn or sweep.read_vram_used_bytes
    # WP-B: fixed for the lifetime of this factory's runner (one `max_tokens` per `batch run`
    # invocation) -- computed once here rather than per-cell inside `_runner`.
    response_caps = {"default": max_tokens, "by_task": LONGCONTEXT_MAX_TOKENS_BY_TASK}

    def _runner(handle: "sweep.ScratchServerHandle", cell: Dict[str, Any]) -> CellOutcome:
        if not handle.healthy:
            return CellOutcome("failed", "health_timeout", False, {"startup_details": handle.details})
        if not handle.verified:
            return CellOutcome("failed", "invalid_config", False, {"startup_details": handle.details})

        if LONGCONTEXT_BATTERY_ITEM not in (cell.get("battery") or []):
            outcome = default_cell_runner(handle, cell)
            # WP-5: augment the startup-only disposition with VRAM read-backs so a
            # vram-battery fit-measurement cell (e.g. the gemma 262144 f16 cell whose measured
            # margin is audit B1's WP-6 admission condition) persists its numbers via the
            # state file's `detail` instead of evaporating with the scratch server.
            try:
                vram_after = vram_used_bytes_fn()
            except OSError:
                vram_after = None
            augmented = dict(outcome.detail or {})
            augmented["vram_before_bytes"] = handle.vram_before_bytes
            augmented["vram_after_bytes"] = vram_after
            # WP-5 (verifier finding): a completed non-needle cell with NO records subdirectory
            # fails verify_run's `records_present` check -- the unattended loop (batch run ->
            # batch verify, audit M2) would page on every fit-measurement cell. Write the same
            # four-file record shape with an empty sample/completion set; the config's vram
            # numbers are the cell's actual payload.
            if records_dir is not None and outcome.status == "completed":
                try:
                    augmented["record_path"] = str(write_cell_record(
                        records_dir, cell, {},
                        vram_baseline_bytes=handle.vram_before_bytes,
                        vram_loaded_bytes=vram_after,
                        sampling_arm_effective=None,
                        vram_profile=vram_profile,
                        response_caps=response_caps,
                    ))
                    augmented["record_error"] = None
                except Exception as exc:  # noqa: BLE001 -- record write must never fail a cell
                    augmented["record_path"] = None
                    augmented["record_error"] = f"{type(exc).__name__}: {exc}"
            return CellOutcome(outcome.status, outcome.reason, outcome.correctness_failure, augmented)

        sampling_arm = cell.get("_sampling_arm")
        if sampling_arm not in ("capability", "production"):
            return CellOutcome(
                "failed", "invalid_sampling_arm", False, {"sampling_arm": sampling_arm},
            )
        trials = cell.get("_trials") or longcontext.MIN_TRIALS_FOR_WILSON
        context = cell["server_flags"]["-c"]
        # Leaves headroom for the question/completion budget under the server's own `-c` ceiling
        # -- the same established fraction `run_harness.needle_probe`'s "near_ceiling" size uses,
        # reused here rather than reimplemented (module docstring: "never a substitute", same
        # spirit).
        target_tokens = max(1, int(context * run_harness.NEEDLE_NEAR_CEILING_FRACTION))
        family = cell.get("model_family")

        # WP-2 (transport-taxonomy-and-prompt-fit): live prompt-fit budget + a tokenize_fn bound
        # to THIS cell's live scratch server (`handle.base_url`) -- wired through the same
        # battery-call seam that already passes `chars_per_token` below. `run_harness.tokenize_
        # count` raises (never silently defaults) on a transport failure or a body that doesn't
        # carry a `tokens` list; the try/except around `battery_fn` just below is what turns that
        # -- or a `longcontext.HaystackOverBudgetError` -- into an honest `battery_error` failed
        # cell, never a silently-sent over-ceiling prompt.
        token_budget = _prompt_fit_budget(context, max_tokens)

        def _tokenize_fn(text: str, _base_url: str = handle.base_url) -> int:
            return run_harness.tokenize_count(_base_url, text)

        effective_arm = sampling_arm
        fallback_reason: Optional[str] = None
        if sampling_arm == "production":
            try:
                preset = production_sampling_fn(family)
            except sampling_table.MissingProductionSamplingError as exc:
                effective_arm = "capability"
                fallback_reason = str(exc)
                preset = run_harness.SAMPLING_ARM_CAPABILITY
        else:
            preset = run_harness.SAMPLING_ARM_CAPABILITY

        # Per-trial seed is `longcontext.run_cell`'s own `derive_trial_seed` schedule (established
        # invariant) -- this placeholder seed is always overwritten before any request goes out.
        sampling_payload = run_harness.sampling_arm_payload(preset, seed=0)

        try:
            battery_results = battery_fn(
                handle.base_url, cell["alias"], cell["cell_id"], target_tokens,
                effective_arm, sampling_payload, n_trials=trials,
                max_tokens=max_tokens, timeout=timeout,
                # WP-6: per-model measured haystack-sizing ratio (WP-5 calibration finding --
                # the static estimate oversizes real prompts by +11-17%; at 512k that would
                # overflow `-c`). None falls back to the static estimate inside longcontext.
                chars_per_token=cell.get("_chars_per_token"),
                # WP-6 (WP-5 punt): multi_needle needs more response budget than the flat cap
                # under the verbose production arm -- see LONGCONTEXT_MAX_TOKENS_BY_TASK.
                max_tokens_by_task=LONGCONTEXT_MAX_TOKENS_BY_TASK,
                # WP-2 (transport-taxonomy-and-prompt-fit): live prompt-fit shrink loop inputs,
                # passed unconditionally through this seam exactly like `chars_per_token` above --
                # any `battery_fn` test double this module's own tests inject must accept these
                # two keyword args (default-valued, so a fake free to ignore them never breaks).
                tokenize_fn=_tokenize_fn,
                token_budget=token_budget,
            )
        except Exception as exc:  # pragma: no cover -- defensive: an unexpected transport/battery
            # exception never propagates out and aborts the whole queue; it becomes an ordinary
            # failed-cell disposition, same as a health-timeout.
            return CellOutcome("failed", "battery_error", False, {"error": str(exc)})

        try:
            vram_after_bytes = vram_used_bytes_fn()
        except OSError:
            vram_after_bytes = None

        record_path: Optional[str] = None
        record_error: Optional[str] = None
        if records_dir is not None:
            # WP-5 glue: persist the battery output as a verify_run.py-consumable record
            # directory. A persistence failure (size budget, disk) never aborts the queue --
            # it is surfaced in `detail` and by the verifier's own records_present check.
            try:
                record_path = str(write_cell_record(
                    Path(records_dir), cell, battery_results,
                    handle.vram_before_bytes, vram_after_bytes, effective_arm,
                    vram_profile=vram_profile,
                    response_caps=response_caps,
                ))
            except Exception as exc:
                record_error = f"{type(exc).__name__}: {exc}"

        all_samples = [
            sample
            for per_position in battery_results.values()
            for cell_result in per_position.values()
            for sample in cell_result.samples
        ]
        has_transport_failure = any(s.get("success") is False for s in all_samples)
        # Transport-failure-taxonomy WP (defense in depth -- either this filter or the
        # transport-precedence reorder in the return ladder below already fixes the observed
        # miscount on its own; both land): a transport/HTTP-failed sample (`success is False`)
        # never carries a correctness-taxonomy verdict (`longcontext.run_cell` records
        # `classifier_verdict: "transport"` for it directly, bypassing the classifier), but
        # these two predicates are re-scoped to `success is not False` samples anyway, so even
        # an older-schema record (pre-this-WP, a success=False row still tagged `wrong`/
        # `degenerate`) can never trip a correctness-failure outcome.
        has_degenerate = any(
            s.get("classifier_verdict") == "degenerate"
            for s in all_samples
            if s.get("success") is not False
        )
        has_needle_failure = any(
            s.get("classifier_verdict") == "wrong"
            for task_name, per_position in battery_results.items()
            if task_name in ("single_needle", "multi_needle")
            for cell_result in per_position.values()
            for s in cell_result.samples
            if s.get("success") is not False
        )

        summaries = {
            f"{task_name}@{position_fraction:.2f}": cell_result.summary
            for task_name, per_position in battery_results.items()
            for position_fraction, cell_result in per_position.items()
        }
        detail = {
            "sampling_arm_requested": sampling_arm,
            "sampling_arm_effective": effective_arm,
            "production_sampling_fallback_reason": fallback_reason,
            "model_family": family,
            "target_tokens": target_tokens,
            "chars_per_token": cell.get("_chars_per_token"),
            "vram_before_bytes": handle.vram_before_bytes,
            "vram_after_bytes": vram_after_bytes,
            "sample_count": len(all_samples),
            "summaries": summaries,
            "record_path": record_path,
            "record_error": record_error,
        }

        if has_transport_failure:
            return CellOutcome("failed", "transport_error", False, detail)
        if has_degenerate:
            return CellOutcome("failed", "degenerate", True, detail)
        if has_needle_failure:
            return CellOutcome("failed", "needle_failure", True, detail)
        return CellOutcome("completed", None, False, detail)

    return _runner


def _execute_cell(
    cell: Dict[str, Any],
    cell_runner: Callable[["sweep.ScratchServerHandle", Dict[str, Any]], CellOutcome],
    port: int,
    container: str,
    scratch_server_fn: Callable[..., Any],
) -> CellOutcome:
    """`scratch_server_fn` is `sweep.scratch_server` by default (the real spawn/health/verify/
    teardown context manager -- Docker-touching) but is ALWAYS explicitly threaded through from
    `run_queue`, which itself defaults it the same way; every one of this WP's own tests passes a
    fake context-manager factory instead (a plain `@contextlib.contextmanager` generator yielding
    a stand-in handle), so `run_queue`'s full cell loop is exercised without ever importing
    Docker into the test process."""
    with scratch_server_fn(cell, port=port, container=container) as handle:
        outcome = cell_runner(handle, cell)
    return outcome


# -------------------------------------------------------------------------------------------
# Main run loop.
# -------------------------------------------------------------------------------------------


def run_queue(
    queue_path: Path,
    state_path: Optional[Path] = None,
    vram_profile: Optional[str] = None,
    dry_run: bool = False,
    re_arm: bool = False,
    re_arm_cell_names: Optional[List[str]] = None,
    cell_runner: Optional[Callable[["sweep.ScratchServerHandle", Dict[str, Any]], CellOutcome]] = None,
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
    """
    queue_path = Path(queue_path)
    queue = load_queue(queue_path)
    resolved_cells = resolve_queue_cells(queue)
    effective_vram_profile = vram_profile or queue["vram_profile"]

    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    vram_total_bytes_fn = vram_total_bytes_fn or sweep.read_vram_total_bytes
    vram_used_bytes_fn = vram_used_bytes_fn or sweep.read_vram_used_bytes
    cell_runner = cell_runner or default_cell_runner
    hang_check_fn = hang_check_fn or (lambda: None)
    scratch_server_fn = scratch_server_fn or sweep.scratch_server

    if dry_run:
        return _dry_run_report(queue, resolved_cells, effective_vram_profile, vram_total_bytes_fn, vram_used_bytes_fn)

    state_path = Path(state_path) if state_path else default_state_path(queue_path)
    stop_marker_path = Path(stop_marker_path) if stop_marker_path is not None else None

    existing = load_state(state_path)
    if existing is not None:
        state = existing
        # WP-6 (WP-2 punt): the state machine keys everything by cell_id, so a queue file
        # edited AFTER its state file exists (cell added/removed, or any server-flag change --
        # cell_ids hash the flags dict) would otherwise surface as a bare KeyError deep in the
        # cell loop. Fail loudly, up front, with the operator's actual options.
        state_ids = set(state["cells"])
        queue_ids = {c["cell_id"] for c in resolved_cells}
        if state_ids != queue_ids:
            raise QueueValidationError(
                f"queue {queue_path} no longer matches its state file {state_path} "
                f"(cells only in state: {sorted(state_ids - queue_ids)}; only in queue: "
                f"{sorted(queue_ids - state_ids)}) -- a queue file is immutable once its state "
                f"exists; move the edited queue to a new file (fresh state) or delete/archive "
                f"the stale state file deliberately"
            )
        if state["queue_state"] in TERMINAL_QUEUE_STATES and not re_arm and not re_arm_cell_names:
            return {
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
    else:
        state = init_state(resolved_cells)

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
            cs = state["cells"][cell["cell_id"]]
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

            c_value = cell["server_flags"]["-c"]
            if c_value >= LARGE_CONTEXT_SIZE_CHECK_THRESHOLD:
                projected = project_cell_record_size_bytes(cell, cell.get("_trials", 1))
                if projected > run_harness.SIZE_LIMIT_BYTES:
                    cs["state"] = FAILED
                    cs["reason"] = "projected_size_exceeded"
                    cs["detail"] = {"projected_bytes": projected, "limit_bytes": run_harness.SIZE_LIMIT_BYTES}
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
            # home. `detail` is JSON-serializable by construction in every runner in this module.
            cs["detail"] = outcome.detail

            if hang_line is not None or outcome.status == "aborted-hang":
                cs["state"] = ABORTED_HANG
                cs["reason"] = hang_line or outcome.reason or "hang_signature"
                state["queue_state"] = QUEUE_ABORTED_HANG
                save_state(state_path, state)
                break

            if outcome.status == "completed":
                cs["state"] = COMPLETED
                cs["reason"] = None
            else:
                cs["state"] = FAILED
                cs["reason"] = outcome.reason
                if outcome.correctness_failure:
                    propagate_skips(resolved_cells, state, cell["cell_id"])
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
            except Exception:
                pass  # never block termination on a failed restart attempt
        _restore_signal_traps(old_handlers)

    return {"noop": False, "queue_state": state["queue_state"], "state": state}


def _dry_run_report(
    queue: Dict[str, Any],
    resolved_cells: List[Dict[str, Any]],
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
            cell, vram_profile=vram_profile,
            vram_total_bytes_fn=vram_total_bytes_fn, vram_used_bytes_fn=vram_used_bytes_fn,
        )
        out.append({
            "cell_id": cell["cell_id"],
            "alias": cell["alias"],
            "model": cell["_queue_model_id"],
            "server_flags": cell["server_flags"],
            "battery": cell["battery"],
            "sampling_arm": cell.get("_sampling_arm"),
            "trials": cell.get("_trials"),
            "fit": fit,
        })
    return {
        "noop": False, "dry_run": True, "vram_profile": vram_profile,
        "cell_count": len(out), "cells": out,
    }


# -------------------------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------------------------


def make_live_hang_check_fn() -> Callable[[], Optional[str]]:
    """WP-5 glue: the real per-cell hang check for the CLI path. `run_queue`'s `hang_check_fn`
    default is a no-op (correct for tests), but `cmd_run` never wired the live implementation --
    `poll_journal_for_hang` existed as dead code from the CLI (found in WP-5's attended
    validation). Each call polls `journalctl -k` since the previous call (first call: since this
    factory was built), so repeated per-cell polling never misses a line."""
    state = {"since": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}

    def _check() -> Optional[str]:
        since = state["since"]
        state["since"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return poll_journal_for_hang(since)

    return _check


def cmd_run(args: argparse.Namespace) -> int:
    controller = None
    if not args.dry_run and not args.no_container_control:
        controller = ContainerController(container=args.container)
    # WP-B: `make_longcontext_cell_runner` needs the effective `vram_profile` string to stamp
    # into every record it writes, but `run_queue` (below) only resolves that value internally
    # (`--vram-profile` override, else the queue's own `vram_profile`) after it has already
    # loaded the queue file -- this cell_runner is built BEFORE that call, so the same
    # resolution is repeated here from the queue file this CLI path is about to hand to
    # `run_queue` anyway. A malformed queue file raises `QueueValidationError` here exactly as
    # it would have inside `run_queue`, just slightly earlier.
    effective_vram_profile = args.vram_profile or load_queue(Path(args.queue))["vram_profile"]
    try:
        result = run_queue(
            Path(args.queue),
            state_path=Path(args.state) if args.state else None,
            vram_profile=args.vram_profile,
            dry_run=args.dry_run,
            re_arm=args.re_arm,
            re_arm_cell_names=args.re_arm_cell or None,
            # WP-2: the real CLI-driven path always runs the longcontext-aware cell runner (it
            # defers to `default_cell_runner` per-cell for anything not a `"needle"`-battery
            # cell -- see `make_longcontext_cell_runner`'s docstring). `run_queue`'s own internal
            # default stays `default_cell_runner` -- untouched -- so every direct `run_queue(...)`
            # caller (including this WP's and WP-1's own tests) that doesn't pass `cell_runner=`
            # is unaffected.
            cell_runner=make_longcontext_cell_runner(
                records_dir=Path(args.records_dir) if args.records_dir else None,
                vram_profile=effective_vram_profile,
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
    """`batch verify --run <dir>`: dispatch seam only in this WP -- `verify_run.py` is WP-4's
    module. A clean, explicit error (never a traceback) if it isn't present yet."""
    try:
        import verify_run  # type: ignore  # noqa: E402  (WP-4 module, may not exist yet)
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
        "(runs/bakeoff/<run-name>); no records are written when omitted",
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
