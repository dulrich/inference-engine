#!/usr/bin/env python3
"""Queue-file ownership: parse, validate, resolve (WP-R1 extraction from `batch.py`).

A queue file is self-contained (audit E3, pinned): a `models:` registry map plus an ordered
`cells:` list of abbreviated `{model, alias, c, ctk, ctv, fa, battery, sampling_arm, trials}`
entries -- there is no external registry file, and cells resolve against the queue's own map.
This module owns that format end to end and nothing else: it never touches the state file, never
executes a cell, and never writes a record.

`resolve_cell` returns a `cell_model.ResolvedCell` -- the typed value whose `identity` sub-value
is the sole input to `cell_id`. Pre-WP-R1 this function returned a plain dict and attached seven
non-identity keys to it *after* computing the id; see `cell_model`'s module docstring for why
that ordering discipline is now structural instead of clerical.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

import sweep
from cell_model import (
    SWEEP_PASSTHROUGH_KEYS,
    CellIdentity,
    ExecutionSettings,
    QueueCellMetadata,
    ResolvedCell,
)

QUEUE_FORMAT_VERSION = 1

# The only currently-implemented stop policy: pause (never abort) at the queue's `deadline`.
# A required field, not a hard-coded behavior -- kept explicit and validated so a queue author
# never wonders what happens at a missed deadline, and so a future policy (e.g. a hard stop that
# also kills an in-flight cell) has a place to be added without a queue-format bump.
STOP_POLICIES = ("pause-at-deadline",)

# WP-2 (overnight-test-matrix-redesign): the base-vs-MTP axis. Additive optional abbreviated-
# cell key, `queue_format` stays 1 -- `validate_queue` below only rejects a PRESENT-but-malformed
# value; absence means "registry server_flags as-is" (every pre-WP-2 queue file byte-identical in
# behavior). The actual strip/assert against a model's server_flags happens in `resolve_cell`,
# not there -- the module-level check has no registry to assert against yet.
SPEC_DECODE_VALUES = ("base", "mtp")

# WP-1 (bakeoff-timing-records-and-estimates): the abbreviated-cell key carrying the `"timing"`
# battery item's per-cell round-count override. Absent/None means `TIMING_DEFAULT_ROUNDS`.
TIMING_ROUNDS_CELL_KEY = "timing_rounds"

_REQUIRED_ABBREVIATED_CELL_KEYS = (
    "model", "alias", "c", "ctk", "ctv", "fa", "battery", "sampling_arm", "trials",
)


class QueueValidationError(ValueError):
    """Raised by `load_queue`/`validate_queue` on a structurally invalid queue file -- never a
    silent partial parse."""


# -------------------------------------------------------------------------------------------
# Deadline (a daily HH:MM wall-clock cutoff, recomputed fresh relative to each invocation's own
# start time -- a queue that spans multiple nights gets a fresh deadline each night, by design).
# Lives here because `deadline` is a queue-FILE field and `validate_queue` parses it.
# -------------------------------------------------------------------------------------------


def parse_deadline_time(deadline: str) -> Tuple[int, int]:
    try:
        hh, mm = deadline.split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except ValueError:
        # `from None`: the ValueError is this block's own sentinel (and int()'s parse failure),
        # an implementation detail -- chaining it would bury the domain error under noise.
        raise QueueValidationError(
            f"deadline {deadline!r} is not a valid 'HH:MM' wall-clock time"
        ) from None
    return hh, mm


def compute_deadline_dt(deadline: str, start_dt: datetime) -> datetime:
    """The next occurrence of `deadline` (HH:MM) at or after `start_dt` -- if the time-of-day has
    already passed today relative to `start_dt`, the deadline is tomorrow (the overnight-run
    case: start at 23:00, deadline '07:00' next morning); otherwise it's later today."""
    hh, mm = parse_deadline_time(deadline)
    candidate = start_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= start_dt:
        candidate += timedelta(days=1)
    return candidate


# -------------------------------------------------------------------------------------------
# Validation
# -------------------------------------------------------------------------------------------


def _validate_models(models: Any) -> None:
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


def _validate_cell(idx: int, cell: Dict[str, Any], models: Dict[str, Any]) -> None:
    missing = [k for k in _REQUIRED_ABBREVIATED_CELL_KEYS if k not in cell]
    if missing:
        raise QueueValidationError(f"cells[{idx}] missing required key(s): {missing}")
    if cell["model"] not in models:
        raise QueueValidationError(
            f"cells[{idx}] references unknown model {cell['model']!r} (not in this queue's own 'models' registry)"
        )
    # WP-1 (bakeoff-timing-records-and-estimates): optional per-cell override for the `"timing"`
    # battery item's round count -- same optional-and-validated-when-present shape as
    # `models[*].chars_per_token` above. Absent means TIMING_DEFAULT_ROUNDS.
    rounds = cell.get(TIMING_ROUNDS_CELL_KEY)
    if rounds is not None and (not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 1):
        raise QueueValidationError(
            f"cells[{idx}] {TIMING_ROUNDS_CELL_KEY} {rounds!r} must be a positive integer when present"
        )
    # WP-2 (overnight-test-matrix-redesign): reject only a malformed PRESENT value -- unlike
    # `timing_rounds` above, an explicit JSON `null` counts as "present but not base/mtp" and is
    # rejected too (checked via key membership, not `.get()`, so a present-and-None value is
    # distinguished from an absent key -- both `.get()` to `None` in Python, but only the absent
    # case is the documented "registry flags as-is" no-op).
    if "spec_decode" in cell and cell["spec_decode"] not in SPEC_DECODE_VALUES:
        raise QueueValidationError(
            f"cells[{idx}] spec_decode {cell['spec_decode']!r} must be one of "
            f"{SPEC_DECODE_VALUES} when present"
        )


def validate_queue(queue: Dict[str, Any]) -> None:
    if queue.get("queue_format") != QUEUE_FORMAT_VERSION:
        raise QueueValidationError(
            f"unsupported queue_format {queue.get('queue_format')!r}; expected {QUEUE_FORMAT_VERSION}"
        )
    models = queue.get("models")
    _validate_models(models)
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
    parse_deadline_time(queue["deadline"])  # raises on malformed HH:MM
    stop_policy = queue.get("stop_policy")
    if stop_policy not in STOP_POLICIES:
        raise QueueValidationError(f"queue stop_policy {stop_policy!r} must be one of {STOP_POLICIES}")
    for idx, cell in enumerate(cells):
        _validate_cell(idx, cell, models)


def load_queue(queue_path: Path) -> Dict[str, Any]:
    with open(queue_path, "r", encoding="utf-8") as f:
        queue = json.load(f)
    validate_queue(queue)
    return queue


# -------------------------------------------------------------------------------------------
# Resolution: abbreviated cell + model registry entry -> typed ResolvedCell
# -------------------------------------------------------------------------------------------


def _merge_server_flags(model_entry: Dict[str, Any], abbreviated: Dict[str, Any]) -> Dict[str, Any]:
    """The registry entry's base `server_flags` with this cell's `c`/`ctk`/`ctv`/`fa` overrides,
    then the base-vs-MTP arm applied.

    The spec-decode strip/assert happens HERE, before normalization and before identity is
    computed, and that is deliberate: a base vs mtp arm of the same served config is a genuinely
    different served configuration, so it must flow into `cell_id` naturally (identity is a hash
    over `server_flags`, among other things). `validate_queue` already rejected any value other
    than "base"/"mtp"/absent by the time a queue reaches here.
    """
    flags = dict(model_entry.get("server_flags") or {})
    flags["-c"] = abbreviated["c"]
    flags["-ctk"] = abbreviated["ctk"]
    flags["-ctv"] = abbreviated["ctv"]
    flags["-fa"] = abbreviated["fa"]
    spec_decode = abbreviated.get("spec_decode")
    if spec_decode == "mtp":
        if "--spec-type" not in flags:
            raise QueueValidationError(
                f"cell {abbreviated.get('alias')!r} asks for spec_decode='mtp' but its "
                f"registry entry's server_flags carry no '--spec-type' -- a cell cannot ask "
                f"for MTP a registry entry doesn't define"
            )
    elif spec_decode == "base":
        flags.pop("-md", None)
        flags.pop("--spec-type", None)
    # else: absent/None -- no-op, registry server_flags as-is (every pre-WP-2 queue unaffected).
    return flags


def resolve_cell(model_entry: Dict[str, Any], abbreviated: Dict[str, Any]) -> ResolvedCell:
    """Merges one queue model registry entry with one abbreviated cell into a `ResolvedCell`.

    The flags are normalized through `sweep.normalize_cell` (the shared normal form every
    `sweep.py` function expects, defaults filled and battery items validated) and only THEN split
    into typed values. `CellIdentity` gets the four identity-bearing fields; every non-identity
    fact lands on `ExecutionSettings`/`QueueCellMetadata`/`sweep_passthrough`, where it is
    physically incapable of perturbing `cell_id`.
    """
    flags = _merge_server_flags(model_entry, abbreviated)

    raw: Dict[str, Any] = {
        "alias": abbreviated["alias"],
        "model_container_path": model_entry["model_container_path"],
        "server_flags": flags,
        "battery": abbreviated["battery"],
    }
    if model_entry.get("model_host_path"):
        raw["model_host_path"] = model_entry["model_host_path"]
    # Optional pass-throughs `sweep.py` reads for its own fit math, plus `model_family` -- a GGUF
    # `general.architecture` string the queue author already knows when writing the registry
    # entry, which is how the live runner selects a `sampling_table.production_sampling` entry
    # without re-parsing the GGUF header on every cell. A queue that omits it simply never gets
    # the `production` arm resolved (falls back to `capability`).
    for key in SWEEP_PASSTHROUGH_KEYS:
        if model_entry.get(key) is not None:
            raw[key] = model_entry[key]

    normalized = sweep.normalize_cell(raw)
    normalized_flags = normalized["server_flags"]

    return ResolvedCell(
        identity=CellIdentity(
            alias=normalized["alias"],
            model_container_path=normalized["model_container_path"],
            server_flags=normalized_flags,
            model_host_path=normalized.get("model_host_path"),
        ),
        execution=ExecutionSettings.from_flags(normalized_flags, normalized["battery"]),
        queue=QueueCellMetadata(
            model_id=abbreviated["model"],
            sampling_arm=abbreviated["sampling_arm"],
            trials=abbreviated["trials"],
            chars_per_token=model_entry.get("chars_per_token"),
            timing_rounds=abbreviated.get(TIMING_ROUNDS_CELL_KEY),
            spec_decode=abbreviated.get("spec_decode"),
        ),
        sweep_passthrough={k: normalized[k] for k in SWEEP_PASSTHROUGH_KEYS if k in normalized},
    )


def resolve_queue_cells(queue: Dict[str, Any]) -> List[ResolvedCell]:
    """Resolves every abbreviated cell in queue order. Duplicate `cell_id`s across the resolved
    list (two abbreviated cells that happen to render identically) raise -- a queue's cells are
    meant to be distinct measurements, and the state machine keys everything by `cell_id`, so a
    collision would silently merge two cells' state."""
    models = queue["models"]
    out: List[ResolvedCell] = []
    seen_ids = set()
    for idx, abbreviated in enumerate(queue["cells"]):
        model_entry = models[abbreviated["model"]]
        cell = resolve_cell(model_entry, abbreviated)
        cell_id = cell.cell_id
        if cell_id in seen_ids:
            raise QueueValidationError(
                f"cells[{idx}] (alias={abbreviated['alias']!r}) resolves to a cell_id "
                f"{cell_id!r} already used by an earlier cell in this queue -- "
                f"two distinct queue entries must never collide"
            )
        seen_ids.add(cell_id)
        out.append(cell)
    return out
