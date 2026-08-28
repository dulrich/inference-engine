#!/usr/bin/env python3
"""Schema-v2 experiment-record serialization (WP-R2 of
`plans/repository-tooling-structural-remediation.md`; review finding F7).

The JSON-per-experiment record writer and the two constants that define what a conformant record
must carry (`REQUIRED_LINEAGE_FIELDS`) and how large a record directory may get
(`SIZE_LIMIT_BYTES`), plus the default records root. Those three moved here with the writer
because they are the writer's own contract, not general harness configuration -- nothing else in
the family reads them except to quote them back to a user.

**Record schema v2 is FROZEN.** `write_experiment_record` emits the identical bytes it emitted
before this split: same key insertion order, same `indent=2, ensure_ascii=False` serialization,
same two-pass `size_check` computation against a placeholder-sized provisional config, same
pre-write budget check. The only field that varies between two calls with identical inputs is
`config["timestamp"]`, exactly as before.

Every public name here is re-exported from `run_harness` at its historical home
(`run_harness.write_experiment_record`, `run_harness.SIZE_LIMIT_BYTES`,
`run_harness.REQUIRED_LINEAGE_FIELDS`, `run_harness.DEFAULT_RUNS_DIR`) -- `batch.py`,
`cell_record.py`, `verify_run.py`, and the test suite all reach them that way.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness_fixture import (
    DEFAULT_RUNS_DIR,
    REQUIRED_LINEAGE_FIELDS,
    SIZE_LIMIT_BYTES,
    _utc_now_iso,
)

# ---------------------------------------------------------------------------------------------
# JSON-per-experiment record writer
# ---------------------------------------------------------------------------------------------


class SizeBudgetExceeded(Exception):
    """Raised by `write_experiment_record` when the projected on-disk size of
    `llama/bakeoff/records/<experiment-id>/` exceeds `SIZE_LIMIT_BYTES`, computed BEFORE anything is
    written to disk. Cycle-2 audit finding (material) -- a hold-for-user signal, never a silent
    over-budget commit."""

    def __init__(self, total_bytes: int, limit_bytes: int):
        super().__init__(
            f"projected llama/bakeoff/records/<experiment-id>/ size {total_bytes} bytes exceeds the "
            f"{limit_bytes}-byte (25 MiB) budget -- hold for user before writing anything"
        )
        self.total_bytes = total_bytes
        self.limit_bytes = limit_bytes


def _jsonl(records: List[Dict[str, Any]]) -> str:
    if not records:
        return ""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def write_experiment_record(
    experiment_id: str,
    config: Dict[str, Any],
    prompts: List[Dict[str, Any]],
    completions: List[Dict[str, Any]],
    samples: List[Dict[str, Any]],
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Writes `llama/bakeoff/records/<experiment-id>/{config.json,samples.jsonl,prompts.jsonl,
    completions.jsonl}`.

    Field-to-file split (this WP's own call -- the plan pins only the four-file split itself):
      - config.json: run-level metadata + the artifact-lineage field set (REQUIRED_LINEAGE_FIELDS)
      - prompts.jsonl: the quality-corpus prompt text (one line per corpus entry)
      - completions.jsonl: every raw completion verbatim (timing rounds, needle probes, quality
        corpus answers) -- never truncated or summarized
      - samples.jsonl: numeric timing/sample statistics per round, plus needle-probe pass/fail
        and cache-assertion results, plus quality-corpus per-answer records (scores as null
        placeholders this round)

    Requires `config["lineage"]` to contain all of REQUIRED_LINEAGE_FIELDS (values may be `None`
    if genuinely unresolvable -- e.g. `hf_revision` when only a ref string, not a commit SHA, is
    known -- but every key must be present).

    Computes the total projected on-disk size BEFORE writing anything; raises
    `SizeBudgetExceeded` instead of writing an over-budget experiment directory.

    **Schema v2** (plan `quant-sweep-harness-and-instrument-v2.md` WP-1: "Record schema v2 in the
    producer, from the start ... every record this plan's WPs write is v2-conformant from day
    one"; frozen and documented as the versioned DB contract by WP-4, not here). Every call
    unconditionally stamps `config["schema_version"] = 2` -- this producer no longer emits v1
    records (pre-existing v1 records on disk are untouched; there is no version toggle). Two
    record-level fields are guaranteed present via `setdefault` (so a caller that doesn't know
    about them yet, e.g. a non-sweep `cmd_full` invocation, still gets a v2-conformant record):
      - `config["sweep_cell"]`: the sweep-cell identity (cell_id/alias/server_flags snapshot) a
        sweep-driven caller supplies directly in `config` before calling this; `None` for a record
        this WP-1's own CLI (`full`) writes outside a sweep run.
      - `config["wilson_summary"]`: **deferred audit item, pinned nullable-with-explicit-null**
        (this WP's execution brief) -- always present as a key, `None` whenever the record has no
        replicated correctness cells (n>=10 trials) to summarize with a Wilson interval. Never
        omitted, and never silently absent.
    Per-trial classifier-verdict and sampling-arm fields are NOT this function's concern -- they
    live on the individual `samples` entries (`needle_probe`/`run_quality_corpus` already write
    them per-sample; this function writes `samples` through to `samples.jsonl` unchanged).
    """
    base_dir = Path(base_dir) if base_dir else DEFAULT_RUNS_DIR
    lineage = config.get("lineage") or {}
    missing = [k for k in REQUIRED_LINEAGE_FIELDS if k not in lineage]
    if missing:
        raise ValueError(f"config['lineage'] is missing required field(s): {missing}")

    config = dict(config)
    config.setdefault("experiment_id", experiment_id)
    config.setdefault("timestamp", _utc_now_iso())
    config["schema_version"] = 2
    config.setdefault("sweep_cell", None)
    config.setdefault("wilson_summary", None)

    prompts_text = _jsonl(prompts)
    completions_text = _jsonl(completions)
    samples_text = _jsonl(samples)

    # size_check describes its own file's contribution; computed against a placeholder-sized
    # provisional serialization first, then baked into the real config.json (the byte-count
    # of the placeholder vs. the final integer differs by at most a few bytes -- negligible
    # against the 25 MiB budget).
    provisional = dict(config)
    provisional["size_check"] = {
        "total_bytes": 0, "limit_bytes": SIZE_LIMIT_BYTES, "within_budget": True,
    }
    provisional_text = json.dumps(provisional, indent=2, ensure_ascii=False) + "\n"
    total_bytes = sum(
        len(t.encode("utf-8"))
        for t in (provisional_text, prompts_text, completions_text, samples_text)
    )
    within_budget = total_bytes <= SIZE_LIMIT_BYTES

    config["size_check"] = {
        "total_bytes": total_bytes, "limit_bytes": SIZE_LIMIT_BYTES, "within_budget": within_budget,
    }
    config_text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"

    if not within_budget:
        raise SizeBudgetExceeded(total_bytes, SIZE_LIMIT_BYTES)

    exp_dir = base_dir / experiment_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.json").write_text(config_text, encoding="utf-8")
    (exp_dir / "prompts.jsonl").write_text(prompts_text, encoding="utf-8")
    (exp_dir / "completions.jsonl").write_text(completions_text, encoding="utf-8")
    (exp_dir / "samples.jsonl").write_text(samples_text, encoding="utf-8")

    actual_bytes = sum(
        (exp_dir / name).stat().st_size
        for name in ("config.json", "prompts.jsonl", "completions.jsonl", "samples.jsonl")
    )
    return {
        "experiment_dir": str(exp_dir),
        "projected_bytes": total_bytes,
        "actual_bytes": actual_bytes,
        "limit_bytes": SIZE_LIMIT_BYTES,
    }
