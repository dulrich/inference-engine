#!/usr/bin/env python3
"""Durable per-cell/per-queue state machine (audit B6; WP-R1 extraction from `batch.py`).

Owns the `.state.json` sibling file and every transition on it: creation, atomic persistence, the
crash-recovery `interrupted` rule, both re-arm forms, and skip propagation. It never parses a
queue file, never executes a cell, and never writes a record -- it takes already-resolved
`cell_model.ResolvedCell` values and a plain state dict, and mutates the state dict in place.

The state dict shape is a wire format read by the dashboard and by `verify_run.py`, so it stays a
plain JSON-serializable dict rather than becoming a typed value: nothing about it is order- or
identity-sensitive, which is what WP-R1 was about.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cell_model import ResolvedCell
from queue_file import DEFAULT_REQUEST_TIMEOUT_S, QueueValidationError

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

# The failure reasons that arm skip propagation. `propagate_skips` itself keys off the
# `Disposition.correctness_failure` flag rather than these strings, but the vocabulary is pinned
# here for a downstream reader (`verify_run.py`) to match against.
CORRECTNESS_FAILURE_REASONS = ("degenerate", "needle_failure")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_state_path(queue_path: Path) -> Path:
    return queue_path.with_name(queue_path.stem + ".state.json")


def init_state(
    resolved_cells: Sequence[ResolvedCell],
    request_timeout_s: int = DEFAULT_REQUEST_TIMEOUT_S,
) -> dict[str, Any]:
    return {
        "queue_state": QUEUE_RUNNING,
        "created_at": utc_now_iso(),
        # WP-1 (qwen4exp-bakeoff-campaign, cycle-3 M1): the effective per-request transport
        # timeout this run started under, snapshotted at creation time -- `cell_id` excludes
        # `request_timeout_s` (it is not an identity input), so without this snapshot a queue
        # file edited after its state exists could change the timeout across a pause/resume
        # undetected. `_load_or_init_state` compares this against the queue's current effective
        # value on resume. A state file written before this key simply lacks it; absence reads
        # as DEFAULT_REQUEST_TIMEOUT_S, same as an unedited queue's own effective value.
        "request_timeout_s": request_timeout_s,
        # WP-B: each cell's already-resolved `alias` at the cell-entry TOP LEVEL (beside
        # `state`/`reason`), so a reader (the dashboard) can alias-join every cell -- including a
        # still-`pending` one -- without waiting for a `detail` to exist. An older state file
        # written before this WP simply lacks the key; nothing here or elsewhere requires it on
        # read.
        "cells": {
            c.cell_id: {"state": PENDING, "reason": None, "alias": c.alias}
            for c in resolved_cells
        },
    }


def load_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    with open(state_path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Atomic write (`os.replace` after a same-directory temp file) -- a crash mid-write never
    leaves a truncated/corrupt state file behind for the next invocation to trip over."""
    state["updated_at"] = utc_now_iso()
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp_path, state_path)


def apply_interrupted_rule(state: dict[str, Any]) -> list[str]:
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


def re_arm_state(state: dict[str, Any]) -> None:
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
    state: dict[str, Any], names: list[str], resolved_cells: Sequence[ResolvedCell]
) -> list[str]:
    """`--re-arm-cell <cell_id-or-alias>` (WP-6; WP-5 punt): the targeted, per-cell counterpart
    to the all-or-nothing `re_arm_state` -- exactly the surgical state-file hand-edit WP-5's
    attended run performed three times, made a first-class knob. Each named cell (matched by
    `cell_id` or queue `alias`) goes back to `pending` regardless of its current terminal state
    (including `completed` -- an operator explicitly naming a cell wants it re-run), and the
    queue state resets to `running`. Unknown names raise `QueueValidationError` -- a typo must
    never silently re-arm nothing. Returns the affected cell_ids."""
    by_alias = {c.alias: c.cell_id for c in resolved_cells}
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


def propagate_skips(
    resolved_cells: Sequence[ResolvedCell],
    state: dict[str, Any],
    failing_cell_id: str,
) -> list[str]:
    """Skip propagation (audit E2, pinned): when `failing_cell_id`'s KV arm is quantized
    (`-ctk`/`-ctv` != f16) and it failed for a correctness reason, mark every LATER `pending` cell
    (queue order) with the same `(queue model id, ctk, ctv)` and a larger `-c` as
    `skipped-propagated`, referencing the failing cell. Purely mechanical -- the caller decides
    whether `failing_cell_id`'s failure reason qualifies as a correctness failure at all; this
    function itself only re-checks the quantized-KV-arm half of the gate (defense in depth) and
    is otherwise unconditional given a failing_cell_id."""
    by_id = {c.cell_id: c for c in resolved_cells}
    failing = by_id[failing_cell_id]
    if not failing.execution.quantized_kv:
        return []
    f_exec = failing.execution
    f_model = failing.queue.model_id
    changed = []
    for cell in resolved_cells:
        if cell.cell_id == failing_cell_id:
            continue
        if cell.queue.model_id != f_model:
            continue
        if (cell.execution.kv_key_type != f_exec.kv_key_type
                or cell.execution.kv_value_type != f_exec.kv_value_type):
            continue
        if cell.execution.context <= f_exec.context:
            continue
        cs = state["cells"].get(cell.cell_id)
        if cs is not None and cs["state"] == PENDING:
            cs["state"] = SKIPPED_PROPAGATED
            cs["reason"] = f"propagated from failing cell {failing_cell_id}"
            changed.append(cell.cell_id)
    return changed
