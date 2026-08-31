#!/usr/bin/env python3
"""Record persistence for one live queue cell (WP-R1 extraction from `batch.py`).

Everything about turning a finished cell into bytes on disk lives here: the four-file
`run_harness.write_experiment_record` directory under `records_dir/<cell_id>/` that `verify_run.py`
consumes, the lineage derivation, the cell-clock fields, and the pre-enqueue size projection that
decides whether a cell may be enqueued at all. Nothing here executes a cell, talks to a scratch
server, or decides a disposition.

The record format is FROZEN at `schema_version` 2. Every field added since is additive and
null-tolerated, and historical records are never rewritten -- a consumer reading an old record
recovers what it can from what is there.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import run_harness
from cell_model import ResolvedCell
from queue_state import utc_now_iso

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


def project_cell_record_size_bytes(context: Optional[int], trials: int) -> int:
    """Conservative pre-enqueue size projection for one cell's contribution to
    `llama/bakeoff/records/<experiment-id>/{samples,completions}.jsonl` (the two per-trial files;
    `run_harness.write_experiment_record`'s `SIZE_LIMIT_BYTES` budget covers the whole
    experiment directory, but a single near-ceiling cell's own trials are what could blow it).

    Builds one synthetic WORST-CASE sample + completion pair matching the real
    `needle_probe`/`longcontext.py` record shape (every string field at
    `_MAX_STRING_FIELD_CHARS`, a `_MAX_COMPLETION_TOKENS`-sized completion text), serializes it
    exactly as `write_experiment_record` would (`json.dumps` + trailing newline per JSONL line),
    multiplies the per-trial total by `trials`, then by `SIZE_PROJECTION_HEADROOM` (25% headroom
    against the projection's own inevitable drift from a real record's exact field lengths).
    Never writes anything to disk.

    Takes the cell's context ceiling as a plain scalar (WP-R1): the projection is arithmetic over
    one number, and passing a whole cell in only to re-index `server_flags["-c"]` obscured that.
    """
    filler = "x" * _MAX_STRING_FIELD_CHARS
    completion_text = "y" * (_MAX_COMPLETION_TOKENS * _CHARS_PER_TOKEN_CONSERVATIVE)

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
# Lineage derivation
# -------------------------------------------------------------------------------------------

_SHARED_MODELS_ORG_REPO_RE = re.compile(r"^/(?:home/_shared_models|models)/([^/]+)/([^/]+)/[^/]+$")


def hf_repo_from_host_path(host_path: Optional[str]) -> Optional[str]:
    """Derives `"<org>/<repo>"` from a `model_host_path` under the shared-models mount, else
    `None`. `host_path` must match `^/(?:home/_shared_models|models)/([^/]+)/([^/]+)/[^/]+$`
    exactly -- either accepted mount prefix (`/srv/models` is the frozen pre-migration
    identity token, kept forever for existing queues/records; `/models` is the current
    partition), then org, then repo, then a single filename segment. `None` input, a path
    outside both accepted prefixes, or a path with too few/many segments below the prefix (e.g.
    only `org/file` with no repo directory) all return `None` rather than guessing; this is a
    display-side convenience derivation, never a substitute for real HF lineage capture."""
    if not host_path:
        return None
    match = _SHARED_MODELS_ORG_REPO_RE.match(host_path)
    if not match:
        return None
    org, repo = match.group(1), match.group(2)
    return f"{org}/{repo}"


def _lineage_fields(host_path: Optional[str]) -> Dict[str, Any]:
    """`filename`/`file_size` come from the cell's own `model_host_path` (stat'd live). `hf_repo`
    is derived from that same path -- hashing a multi-GiB GGUF per cell is not worth the
    wall-clock, but the registry's own directory layout already encodes the repo for free.
    `llama_cpp_build` comes from `run_harness.resolve_llama_cpp_build()` (the pinned
    `llama/Dockerfile` `ARG LLAMA_CPP_TAG=`, or its fallback constant) -- the build actually
    serving this cell, always resolvable, never `None`. `sha256`/`hf_revision` stay explicit
    `None`: `REQUIRED_LINEAGE_FIELDS` requires every key present and permits `None` for
    genuinely unresolved values, and neither has a cheap derivation the way repo/build do."""
    filename = None
    file_size = None
    if host_path:
        filename = Path(host_path).name
        try:
            file_size = Path(host_path).stat().st_size
        except OSError:
            file_size = None
    return {
        "hf_repo": hf_repo_from_host_path(host_path), "hf_revision": None, "filename": filename,
        "file_size": file_size, "sha256": None,
        "llama_cpp_build": run_harness.resolve_llama_cpp_build(),
    }


def cell_timing_fields(handle: Any) -> Dict[str, Any]:
    """The four additive `config.json` timing keys for the cell `handle` belongs to, read at the
    moment the record is about to be written (WP-1, `bakeoff-timing-records-and-estimates.md`).

    Every read is a `getattr(..., None)`, so a duck-typed stand-in handle (this repo's fake
    context managers, and any future caller's) degrades to explicit nulls instead of raising --
    the same null-not-guessed stance the rest of the record format takes. `cell_wall_seconds`
    comes from the monotonic clock the handle started, never from subtracting ISO strings.

    `scratch_startup_seconds` is deliberately NOT named `model_load_seconds` -- README.md's
    honest-timing-semantics contract reserves that name for a `ResidencyPoller`-OBSERVED
    residency transition, which this measurement is not.
    """
    started_monotonic = getattr(handle, "started_monotonic", None)
    wall_seconds: Optional[float] = None
    if isinstance(started_monotonic, (int, float)) and not isinstance(started_monotonic, bool):
        wall_seconds = time.monotonic() - started_monotonic
    return {
        "cell_started_at": getattr(handle, "started_at", None),
        "cell_ended_at": utc_now_iso(),
        "cell_wall_seconds": wall_seconds,
        "scratch_startup_seconds": getattr(handle, "scratch_startup_seconds", None),
    }


def _flatten_battery(battery_results: Dict[str, Dict[float, Any]]):
    """`{task: {position_fraction: CellResult}}` -> (samples, completions, first Wilson summary).

    `wilson_summary` is the first non-null per-task/position summary from the battery (Wilson
    intervals exist only where `n_trials >= longcontext.MIN_TRIALS_FOR_WILSON`); `None` for
    smoke-depth cells -- `write_experiment_record` keeps the key present either way.
    """
    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []
    wilson_summary: Optional[Dict[str, Any]] = None
    for per_position in battery_results.values():
        for cell_result in per_position.values():
            samples.extend(cell_result.samples)
            completions.extend(cell_result.completions)
            if wilson_summary is None and cell_result.summary is not None:
                wilson_summary = cell_result.summary
    return samples, completions, wilson_summary


def write_cell_record(
    records_dir: Path,
    cell: ResolvedCell,
    battery_results: Dict[str, Dict[float, Any]],
    vram_baseline_bytes: Optional[int],
    vram_loaded_bytes: Optional[int],
    sampling_arm_effective: Optional[str],
    vram_profile: Optional[str] = None,
    response_caps: Optional[Dict[str, Any]] = None,
    cell_started_at: Optional[str] = None,
    cell_ended_at: Optional[str] = None,
    cell_wall_seconds: Optional[float] = None,
    scratch_startup_seconds: Optional[float] = None,
    extra_samples: Optional[List[Dict[str, Any]]] = None,
    extra_completions: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Persists one live cell's battery output as a standard four-file
    `run_harness.write_experiment_record` directory under `records_dir/<cell_id>/` -- exactly the
    run-directory convention `verify_run.py` consumes (`experiment_id == cell_id`, one
    subdirectory per queue cell).

    The `sweep_cell` block comes from `cell.record_metadata()` (`cell_model.RecordMetadata`), so
    what a record stamps is a declared value rather than a scatter of `cell.get(...)` reads with
    fallbacks. `sampling_arm_effective` is a run-time argument, not a cell property: a
    `production` request that fell back to `capability` records what actually ran.

    `vram_profile` and `response_caps` are the effective `--vram-profile` string and the
    response-length budget the battery actually enforced. `cell_started_at`/`cell_ended_at`/
    `cell_wall_seconds`/`scratch_startup_seconds` are the cell's own clock (see
    `cell_timing_fields`). `extra_samples`/`extra_completions` are rows appended to
    `samples.jsonl`/`completions.jsonl` AFTER the battery's own -- how the `"timing"` battery
    item's `timing_round` rows reach the same record as the needle arm's `longcontext_probe`
    rows, without either arm needing to know the other's result shape.

    Every argument from `vram_profile` onward defaults to `None`, so a pre-existing positional
    caller writes exactly what it wrote before; `schema_version` stays 2.
    """
    meta = cell.record_metadata()
    samples, completions, wilson_summary = _flatten_battery(battery_results)
    samples.extend(extra_samples or [])
    completions.extend(extra_completions or [])
    config = {
        "lineage": _lineage_fields(meta.model_host_path),
        "sweep_cell": meta.sweep_cell_block(sampling_arm_effective),
        "vram": {
            "baseline_bytes": vram_baseline_bytes,
            "loaded_bytes": vram_loaded_bytes,
            "after_teardown_bytes": None,
        },
        "wilson_summary": wilson_summary,
        "vram_profile": vram_profile,
        "response_caps": response_caps,
        "cell_started_at": cell_started_at,
        "cell_ended_at": cell_ended_at,
        "cell_wall_seconds": cell_wall_seconds,
        "scratch_startup_seconds": scratch_startup_seconds,
    }
    run_harness.write_experiment_record(
        meta.cell_id, config, [], completions, samples, base_dir=records_dir,
    )
    return records_dir / meta.cell_id
