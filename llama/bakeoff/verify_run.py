#!/usr/bin/env python3
"""Post-queue mechanical verification (plans/longcontext-pow2-overnight-campaign.md, WP-4).

`batch.py cmd_verify` dispatches to this module's `main(argv) -> int` (never edited here --
that seam is WP-1's contract, consumed as-is: `verify_run.main(["--run", <dir>])`). This module
has NO interpretation authority of its own -- it runs a fixed, named, machine-readable checklist
against a night's already-written records and a queue's already-persisted state file, and reports
what it found. It never re-scores a classifier verdict, never re-runs a battery, never decides
whether a "marginal" fit was actually acceptable -- see `.claude/agents/verifier-bakeoff.md` for
the worker-tier agent profile built on top of this module, which carries the same restriction.

**Run-directory convention (this WP's own design call -- WP-2's battery hookup, not yet landed,
is what will actually populate a directory shaped like this; this module just defines, documents,
and tests the shape it consumes)**: `--run <dir>` names a directory whose immediate
subdirectories are one-per-queue-cell, each holding the standard four-file
`run_harness.write_experiment_record` shape (`config.json`/`prompts.jsonl`/`completions.jsonl`/
`samples.jsonl`), with the subdirectory's own name equal to that cell's `cell_id`
(`sweep.compute_cell_id`) -- i.e. `experiment_id == cell_id` for every cell a batch queue runs.
A cell whose queue state never reached `completed` (health-timeout, OOM-refusal, an
`aborted-hang` cell that never got far enough to write anything) may have no subdirectory at all
-- that is expected, not a verification failure, UNLESS the state file claims `completed` for a
cell with no matching subdirectory (a real inconsistency: "records missing").

**Queue state discovery**: `--state <path>` is optional; when omitted this module tries, in
order, `<run-dir>/queue.state.json` (a copy/symlink an operator or a future WP places alongside
the run directory) then `<run-dir's parent>/<run-dir's name>.state.json` (sibling-of-run-dir,
the same "sibling file, `.state.json` suffix" shape `batch.default_state_path` uses for a queue
file, applied here to the run directory's own path) then gives up (state-derived checks --
abort/halt/skip surfacing, and the "records required" strictness for `completed` cells -- are
skipped and flagged `unknown`, not silently passed).

**Per-cell checklist** (plan WP-4; each item is one `{check, cell_id, expected, observed,
verdict}` unit, `verdict` in `VERDICTS = ("pass", "fail", "unknown", "informational")`):

1. `records_present` -- a subdirectory with a `config.json` exists for this cell (required for
   a `completed` cell; optional, and merely noted, for any other state).
2. `schema_conformant` -- `config.json` carries every frozen v2 top-level key
   (`REQUIRED_CONFIG_KEYS`) plus the full required lineage field set
   (`run_harness.REQUIRED_LINEAGE_FIELDS`), and every parsed `samples.jsonl` row carries its
   type's required v2 fields (`LONGCONTEXT_PROBE_REQUIRED_SAMPLE_FIELDS` for `longcontext_probe`,
   `NEEDLE_OR_QUALITY_V2_FIELDS` for `needle_probe`/`quality_corpus_result` -- `timing_round`
   never gained v2 fields per the README's Schema v2 section, so it's only checked for a `type`
   key). Never reimplements schema v2 itself -- every required-field list here is transcribed
   directly from the frozen contract (`README.md`'s "Schema v2 (FROZEN)" section and
   `longcontext.run_cell`'s own sample dict), not re-derived.
3. `vram_arithmetic` -- consumes `sweep.kv_bytes_per_token` (never reimplements the KV-bytes
   formula) against the cell's declared KV arm (`-ctk`/`-ctv`/`-c` from the recorded
   `sweep_cell.server_flags`) and geometry resolved either from a real, still-present
   `model_host_path` GGUF header (`sweep.read_gguf_metadata` +
   `sweep.gguf_attention_geometry`, the same live-header path `sweep.classify_fit` uses) or a
   recorded `sweep_cell.synthetic_geometry` fixture block (this module's own tests always use
   this path -- no GGUF file ever read in the test suite); compares
   `weights_bytes + kv_bytes_at_context` against the recorded `vram.loaded_bytes -
   vram.baseline_bytes` delta within `VRAM_ARITHMETIC_TOLERANCE` (a generous multiplicative
   band, not a tight percentage -- `kv_bytes_per_token`'s own docstring records a live MLA
   measurement off by ~1.7x from the plain per-layer-array formula, so a tight tolerance would
   false-positive on a known-approximate architecture class; this check exists to catch a
   GROSS mismatch -- e.g. a quantized-KV cell whose measured delta actually matches its f16
   sibling's, the exact "silent wrong region" shape the campaign hunts -- not to certify the
   formula to the byte). `unknown` (never `fail`) whenever geometry can't be resolved at all.
4. `prompt_tokens_tolerance` -- every `longcontext_probe`/`needle_probe` sample's
   `usage.prompt_tokens` (recorded as `prompt_tokens`) within ±10% of `target_tokens` (plan Test
   Plan's pinned tolerance).
5. `wilson_min_trials` -- whenever `config.json`'s `wilson_summary` is non-null (a verdict is
   being claimed), its `n` is >= `longcontext.MIN_TRIALS_FOR_WILSON` (consumed, never
   re-pinned as a second literal `10`).
6. `classifier_distribution` -- always `informational`: a per-cell count of
   `classifier_verdict` values across that cell's samples (`longcontext.VERDICTS`, plus the
   caller-assigned `"transport"` value `longcontext.run_cell` records directly for `success=
   False` trials -- outside `classify_trial`, hence outside `longcontext.VERDICTS` itself; this
   check's counting is fully generic over whatever `classifier_verdict` strings actually appear,
   so `transport` surfaces as its own bucket automatically, never folded into another bucket and
   never crashing on an unrecognized value) -- reported, never gated on, matching the README's
   "never silently trusted... a manual spot-check is expected practice" framing for classifier
   output.
7. `abort_state` -- one queue-level (`cell_id: null`) item: `fail` iff the queue state's
   `queue_state` is `batch.QUEUE_ABORTED_HANG`, `unknown` iff no state file was found, else
   `pass`; `observed` always lists every individual cell in `batch.ABORTED_HANG` state too, so
   an `aborted-hang` queue is unmissable in both the JSON and the markdown report even before a
   reader reaches the per-cell table (both report writers put this check first).

**Exit code** (matching `batch.py cmd_verify`'s contract: `verify_run.main([...]) -> int`,
threaded straight back out as `batch verify`'s own process exit code): `0` if every checklist
item's `verdict` is `pass`/`unknown`/`informational`; `1` if any item is `fail`
(this includes an `aborted-hang` queue, via the `abort_state` check); `2` if the run directory
does not exist, or exists but yields NOTHING to check (no discoverable queue state file and no
per-cell subdirectories at all) -- "cannot verify", never silently reported as a clean `0`.

**Outputs**: `verify_report.json` (machine-readable -- the full checklist item list plus a
summary) and `verify_report.md` (human-readable -- the abort banner first, then a per-cell
table), both written into `--run <dir>` itself. Neither is written when the run directory does
not exist at all (nowhere to write them; `main` prints the exit-2 explanation to stderr instead).

**Local-inference seam (designed, not built -- plan Decisions locked: "verification is
mechanical... the design must leave a clean seam for replacing that agent with a
local-inference verifier")**: every checklist item already IS the self-contained
`{check, cell_id, expected, observed, verdict}` unit a future local-model call would answer --
none of the five keys here are Claude-shaped prose. The only piece this module hands to a human
(or worker-tier agent) today rather than a local model is the FINAL triage judgment over the
whole checklist (see `.claude/agents/verifier-bakeoff.md`); a future local-inference verifier
(the GLiNER2-extract / bge-m3-similarity precedent from the Ling plan -- a small always-resident
CPU-tier model, not a frontier agent) would consume exactly this module's JSON report and answer
one narrow question per item ("does `observed` satisfy `expected`?") without ever touching the
underlying records itself. That successor is out of scope here -- this module produces the report
shape it would consume, nothing more.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Same "no package __init__.py under llama/" gotcha longcontext.py/batch.py's own comments
# document -- a plain top-level import works whenever the importer has already put this
# directory on `sys.path` (true when this file is run directly as a script, and when a test
# under `tests/` inserts this directory itself).
#
# Every one of these four is consumed READ-ONLY -- this module verifies a finished run against
# the production definitions, so re-pinning any of them here would make the check vacuous:
#   batch        -- constants + default_state_path only; never edited or mutated
#   longcontext  -- MIN_TRIALS_FOR_WILSON, VERDICTS: consumed, never re-pinned
#   run_harness  -- REQUIRED_LINEAGE_FIELDS: consumed, never re-pinned
#   sweep        -- kv_bytes_per_token / gguf geometry: consumed, never reimplemented
import batch
import longcontext
import run_harness
import sweep

VERDICTS = ("pass", "fail", "unknown", "informational")

CHECK_RECORDS_PRESENT = "records_present"
CHECK_SCHEMA_CONFORMANT = "schema_conformant"
CHECK_VRAM_ARITHMETIC = "vram_arithmetic"
CHECK_PROMPT_TOKENS_TOLERANCE = "prompt_tokens_tolerance"
CHECK_WILSON_MIN_TRIALS = "wilson_min_trials"
CHECK_CLASSIFIER_DISTRIBUTION = "classifier_distribution"
CHECK_ABORT_STATE = "abort_state"

CHECK_NAMES = (
    CHECK_RECORDS_PRESENT, CHECK_SCHEMA_CONFORMANT, CHECK_VRAM_ARITHMETIC,
    CHECK_PROMPT_TOKENS_TOLERANCE, CHECK_WILSON_MIN_TRIALS, CHECK_CLASSIFIER_DISTRIBUTION,
    CHECK_ABORT_STATE,
)

# Plan Test Plan's pinned tolerance for usage.prompt_tokens vs. the cell's target_tokens.
PROMPT_TOKENS_TOLERANCE = 0.10

# This WP's own calibrated discretion (see module docstring's vram_arithmetic section) -- a
# generous multiplicative band around the predicted weights+KV delta, wide enough to absorb the
# documented ~1.7x MLA per-layer-array approximation error without false-positiving, narrow
# enough to still catch a KV-type-scale-class mismatch (an f16-sized delta recorded against a
# quantized-KV cell, roughly a 2-4x gap depending on type).
VRAM_ARITHMETIC_LOW_RATIO = 0.4
VRAM_ARITHMETIC_HIGH_RATIO = 3.0

REQUIRED_CONFIG_KEYS = (
    "schema_version", "experiment_id", "timestamp", "lineage", "sweep_cell",
    "wilson_summary", "size_check",
)

# Transcribed verbatim from `longcontext.run_cell`'s own sample dict (this module's docstring
# points here rather than re-deriving the field list from the README prose alone).
LONGCONTEXT_PROBE_REQUIRED_SAMPLE_FIELDS = (
    "type", "schema_version", "task", "cell_id", "trial_index", "position_fraction",
    "target_tokens", "haystack_structure", "haystack_book_ids", "corpus_manifest_id",
    "haystack_seed", "sampler_seed", "sampling_arm", "temperature", "top_p", "top_k",
    "haystack_sha256", "expected", "trial_meta", "correct", "classifier_verdict",
    "classifier_shape", "classifier_flags", "finish_reason", "success", "error",
    "wall_seconds", "prompt_tokens", "timestamp",
)

# Transcribed from the README's Schema v2 section: the v2 gains common to `needle_probe` and
# `run_quality_corpus` samples (`type: "quality_corpus_result"`) -- `timing_round` is NOT in this
# list; it never gained these fields.
NEEDLE_OR_QUALITY_V2_FIELDS = (
    "sampling_arm", "temperature", "top_p", "top_k", "sampler_seed", "classifier_verdict",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _item(check: str, cell_id: Optional[str], expected: Any, observed: Any, verdict: str) -> Dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError(f"verify_run: bad verdict {verdict!r}, expected one of {VERDICTS}")
    return {"check": check, "cell_id": cell_id, "expected": expected, "observed": observed, "verdict": verdict}


# -------------------------------------------------------------------------------------------
# Queue state discovery + run-directory cell discovery.
# -------------------------------------------------------------------------------------------


def discover_state_path(run_dir: Path, state_path: Optional[Path] = None) -> Optional[Path]:
    """See module docstring's "Queue state discovery". Returns the first candidate that exists,
    or `None` if none do (never raises -- an absent state file degrades several checks to
    `unknown`, it never aborts verification outright by itself)."""
    if state_path is not None:
        return state_path if state_path.exists() else None
    candidates = [
        run_dir / "queue.state.json",
        run_dir.parent / (run_dir.name + ".state.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_queue_state(run_dir: Path, state_path: Optional[Path] = None) -> Tuple[Optional[Dict[str, Any]], Optional[Path]]:
    resolved = discover_state_path(run_dir, state_path)
    if resolved is None:
        return None, None
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            return json.load(f), resolved
    except (OSError, json.JSONDecodeError):
        return None, resolved


def discover_cell_dirs(run_dir: Path) -> Dict[str, Path]:
    """cell_id -> subdirectory path, for every immediate subdirectory of `run_dir` carrying a
    `config.json` (the `run_harness.write_experiment_record` marker file). Returns an empty dict
    for a non-existent or record-free `run_dir` -- never raises."""
    out: Dict[str, Path] = {}
    if not run_dir.is_dir():
        return out
    for child in sorted(run_dir.iterdir()):
        if child.is_dir() and (child / "config.json").exists():
            out[child.name] = child
    return out


def load_cell_records(
    cell_dir: Path,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Returns `(config, samples, completions, errors)`. Never raises -- a parse failure on any
    one of the four files is recorded as a human-readable string in `errors` instead."""
    errors: List[str] = []
    config: Optional[Dict[str, Any]] = None
    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []

    cfg_path = cell_dir / "config.json"
    try:
        config = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"config.json unreadable/invalid: {exc}")

    for name, sink in (("samples.jsonl", samples), ("completions.jsonl", completions)):
        path = cell_dir / name
        if not path.exists():
            errors.append(f"{name} missing")
            continue
        # `line_no` is bound BEFORE the try and re-bound per file: the read itself can raise
        # (an unreadable or undecodable file), which used to leave the name unbound and turn
        # this function's documented "never raises" contract into an UnboundLocalError -- and,
        # once the second file was reached, could report the FIRST file's stale line number.
        # Line 0 honestly means "failed before any line was read".
        line_no = 0
        try:
            for offset, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                line_no = offset
                if not line.strip():
                    continue
                sink.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{name} unreadable/invalid at line {line_no}: {exc}")

    if not (cell_dir / "prompts.jsonl").exists():
        errors.append("prompts.jsonl missing")

    return config, samples, completions, errors


# -------------------------------------------------------------------------------------------
# Per-cell checks.
# -------------------------------------------------------------------------------------------


def schema_conformance_errors(config: Optional[Dict[str, Any]], samples: List[Dict[str, Any]]) -> List[str]:
    """Pure function: `config`/`samples` already parsed (or `None`/`[]` on a parse failure the
    caller already recorded separately) -> a list of human-readable conformance error strings,
    empty when fully conformant. Every required-field list consumed here is transcribed from the
    frozen v2 contract (see module docstring), never re-derived."""
    errors: List[str] = []
    if config is None:
        return ["config.json missing/unparseable -- cannot check schema conformance"]

    if config.get("schema_version") != 2:
        errors.append(f"config.json schema_version={config.get('schema_version')!r}, expected 2")
    missing_top = [k for k in REQUIRED_CONFIG_KEYS if k not in config]
    if missing_top:
        errors.append(f"config.json missing required key(s): {missing_top}")

    lineage = config.get("lineage") or {}
    missing_lineage = [k for k in run_harness.REQUIRED_LINEAGE_FIELDS if k not in lineage]
    if missing_lineage:
        errors.append(f"config.json['lineage'] missing required field(s): {missing_lineage}")

    if not samples:
        # WP-5 (live-validation finding): a non-needle cell (e.g. a `battery: ["vram"]`
        # fit-measurement cell) legitimately records an EMPTY sample set -- its payload is
        # config['vram']'s baseline/loaded numbers, not trials. Empty samples are a schema
        # error only for records whose battery includes a sample-producing item.
        battery = (config.get("sweep_cell") or {}).get("battery") or []
        if "needle" in battery or "quality" in battery:
            errors.append("samples.jsonl has no parsed sample rows")

    for idx, sample in enumerate(samples):
        stype = sample.get("type")
        if stype is None:
            errors.append(f"samples.jsonl[{idx}] missing 'type' field")
        elif stype == "longcontext_probe":
            missing = [k for k in LONGCONTEXT_PROBE_REQUIRED_SAMPLE_FIELDS if k not in sample]
            if missing:
                errors.append(f"samples.jsonl[{idx}] (longcontext_probe) missing field(s): {missing}")
        elif stype in ("needle_probe", "quality_corpus_result"):
            missing = [k for k in NEEDLE_OR_QUALITY_V2_FIELDS if k not in sample]
            if missing:
                errors.append(f"samples.jsonl[{idx}] ({stype}) missing v2 field(s): {missing}")
        # timing_round: no v2 gains documented -- only the 'type' presence check above applies.

    return errors


def _resolve_geometry(sweep_cell: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns `(geometry, reason_unknown)`. Prefers a recorded `synthetic_geometry` fixture
    block (this module's own tests always use this path); falls back to a still-present
    `model_host_path` GGUF header (the real, live-capable path -- never exercised by this
    module's tests, same "real function exists, only ever invoked against a real host path"
    pattern as `sweep.py`'s own live readers)."""
    synthetic = sweep_cell.get("synthetic_geometry")
    if synthetic:
        return synthetic, None
    host_path = sweep_cell.get("model_host_path")
    if not host_path:
        return None, "no geometry available (neither synthetic_geometry nor model_host_path recorded for this cell)"
    path = Path(host_path)
    if not path.exists():
        return None, f"model_host_path does not exist: {host_path}"
    try:
        metadata = sweep.read_gguf_metadata(path)
        geometry = sweep.gguf_attention_geometry(metadata)
    except (sweep.GGUFParseError, OSError) as exc:
        return None, f"GGUF header parse failed for {host_path}: {exc}"
    return geometry, None


def check_vram_arithmetic(cell_id: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    expected = f"estimated weights+KV delta within [{VRAM_ARITHMETIC_LOW_RATIO}x, {VRAM_ARITHMETIC_HIGH_RATIO}x] of measured vram.loaded_bytes - vram.baseline_bytes"
    if config is None:
        return _item(CHECK_VRAM_ARITHMETIC, cell_id, expected, "config.json missing/unparseable", "unknown")

    sweep_cell = config.get("sweep_cell") or {}
    vram = config.get("vram") or {}
    flags = sweep_cell.get("server_flags") or {}
    context = flags.get("-c")
    ctk = flags.get("-ctk", "f16")
    ctv = flags.get("-ctv", "f16")
    loaded = vram.get("loaded_bytes")
    baseline = vram.get("baseline_bytes")

    if context is None or loaded is None or baseline is None:
        return _item(
            CHECK_VRAM_ARITHMETIC, cell_id, expected,
            {"reason": "record carries no -c/vram.loaded_bytes/vram.baseline_bytes to check against",
             "server_flags": flags, "vram": vram},
            "unknown",
        )

    geometry, reason = _resolve_geometry(sweep_cell)
    if geometry is None:
        return _item(CHECK_VRAM_ARITHMETIC, cell_id, expected, {"reason": reason}, "unknown")

    try:
        bytes_per_token = sweep.kv_bytes_per_token(
            geometry, ctk, ctv,
            kv_attention_layers=sweep_cell.get("kv_attention_layers"),
            context=context,
            swa_layer_spec=sweep_cell.get("swa_layer_spec"),
            kv_indexer_geometry=sweep_cell.get("kv_indexer_geometry"),
        )
    except (sweep.GGUFParseError, ValueError) as exc:
        return _item(CHECK_VRAM_ARITHMETIC, cell_id, expected, {"reason": f"kv_bytes_per_token failed: {exc}"}, "unknown")

    weights_bytes = (
        sweep_cell.get("gpu_weights_bytes")
        or geometry.get("weights_bytes")
        or (config.get("lineage") or {}).get("file_size")
    )
    if not weights_bytes:
        return _item(
            CHECK_VRAM_ARITHMETIC, cell_id, expected,
            {"reason": "no weights_bytes available (no gpu_weights_bytes, synthetic weights_bytes, or lineage.file_size)"},
            "unknown",
        )

    kv_bytes_at_context = bytes_per_token * context
    estimated_delta = weights_bytes + kv_bytes_at_context
    measured_delta = loaded - baseline

    observed = {
        "ctk": ctk, "ctv": ctv, "context": context,
        "weights_bytes": weights_bytes, "kv_bytes_per_token": bytes_per_token,
        "kv_bytes_at_context": kv_bytes_at_context, "estimated_delta_bytes": estimated_delta,
        "measured_delta_bytes": measured_delta,
    }
    if estimated_delta <= 0 or measured_delta <= 0:
        observed["reason"] = "non-positive estimated or measured delta -- cannot form a ratio"
        return _item(CHECK_VRAM_ARITHMETIC, cell_id, expected, observed, "unknown")

    ratio = measured_delta / estimated_delta
    observed["ratio"] = ratio
    verdict = "pass" if VRAM_ARITHMETIC_LOW_RATIO <= ratio <= VRAM_ARITHMETIC_HIGH_RATIO else "fail"
    return _item(CHECK_VRAM_ARITHMETIC, cell_id, expected, observed, verdict)


def check_prompt_tokens_tolerance(cell_id: str, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    expected = f"usage.prompt_tokens within ±{PROMPT_TOKENS_TOLERANCE:.0%} of target_tokens"
    relevant = [
        s for s in samples
        if s.get("type") in ("longcontext_probe", "needle_probe")
        and s.get("target_tokens") is not None
        and s.get("prompt_tokens") is not None
    ]
    if not relevant:
        return _item(CHECK_PROMPT_TOKENS_TOLERANCE, cell_id, expected, {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"}, "unknown")

    violations = []
    for s in relevant:
        target = s["target_tokens"]
        actual = s["prompt_tokens"]
        if not target or abs(actual - target) > PROMPT_TOKENS_TOLERANCE * target:
            violations.append({"trial_index": s.get("trial_index"), "target_tokens": target, "prompt_tokens": actual})

    verdict = "pass" if not violations else "fail"
    return _item(CHECK_PROMPT_TOKENS_TOLERANCE, cell_id, expected, {"n": len(relevant), "violations": violations}, verdict)


def check_wilson_min_trials(cell_id: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    expected = f">= {longcontext.MIN_TRIALS_FOR_WILSON} trials whenever a wilson_summary verdict is claimed"
    wilson = (config or {}).get("wilson_summary")
    if wilson is None:
        return _item(CHECK_WILSON_MIN_TRIALS, cell_id, expected, {"wilson_summary": None, "reason": "no wilson_summary claimed for this cell"}, "pass")
    n = wilson.get("n")
    verdict = "pass" if isinstance(n, int) and n >= longcontext.MIN_TRIALS_FOR_WILSON else "fail"
    return _item(CHECK_WILSON_MIN_TRIALS, cell_id, expected, {"n": n}, verdict)


def check_classifier_distribution(cell_id: str, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Informational-only per-cell count of `classifier_verdict` values. Deliberately generic --
    counts whatever verdict strings actually appear rather than enumerating a fixed taxonomy, so
    `"transport"` (assigned by `longcontext.run_cell` directly for `success=False` trials, never
    by `classify_trial`, hence not in `longcontext.VERDICTS`) surfaces as its own bucket exactly
    like `correct`/`wrong`/`degenerate`/`truncated(benign)` -- never folded, never a KeyError."""
    counts: Dict[str, int] = {}
    for s in samples:
        verdict = s.get("classifier_verdict")
        if verdict is None:
            continue
        counts[verdict] = counts.get(verdict, 0) + 1
    return _item(CHECK_CLASSIFIER_DISTRIBUTION, cell_id, None, counts, "informational")


def check_abort_state(queue_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    expected = f"queue_state != {batch.QUEUE_ABORTED_HANG!r}"
    if queue_state is None:
        return _item(CHECK_ABORT_STATE, None, expected, {"reason": "no queue state file found"}, "unknown")
    qs = queue_state.get("queue_state")
    aborted_cells = [cid for cid, cs in (queue_state.get("cells") or {}).items() if cs.get("state") == batch.ABORTED_HANG]
    observed = {"queue_state": qs, "aborted_hang_cells": aborted_cells}
    verdict = "fail" if qs == batch.QUEUE_ABORTED_HANG else "pass"
    return _item(CHECK_ABORT_STATE, None, expected, observed, verdict)


def check_records_present_and_conformant(
    cell_id: str, cell_state: Optional[str], cell_dir: Optional[Path],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Returns `(items, config, samples)` -- `config`/`samples` are handed back so the caller's
    other per-cell checks (vram/prompt-tokens/wilson/classifier) don't re-parse the same files."""
    must_have_records = cell_state == batch.COMPLETED
    if cell_dir is None:
        verdict = "fail" if must_have_records else "unknown"
        expected = "records subdirectory present (state=completed)" if must_have_records else f"records optional for state {cell_state!r}"
        item = _item(CHECK_RECORDS_PRESENT, cell_id, expected, "no records subdirectory found", verdict)
        return [item], None, []

    config, samples, _completions, errors = load_cell_records(cell_dir)
    items = [_item(CHECK_RECORDS_PRESENT, cell_id, "records subdirectory present", str(cell_dir), "pass")]

    conform_errors = list(errors) + schema_conformance_errors(config, samples)
    conform_verdict = "pass" if not conform_errors else "fail"
    items.append(_item(
        CHECK_SCHEMA_CONFORMANT, cell_id,
        "schema-v2-conformant records (4-file split + required config/sample fields)",
        conform_errors or "conformant", conform_verdict,
    ))
    return items, config, samples


# -------------------------------------------------------------------------------------------
# Whole-run verification.
# -------------------------------------------------------------------------------------------


def verify_run(run_dir: Path, state_path: Optional[Path] = None) -> Dict[str, Any]:
    """Runs the full checklist over `run_dir` (see module docstring for the run-directory and
    state-file conventions). Never raises for an ordinary "nothing to check" or "records
    missing" condition -- those are `exit_code: 2` / individual `fail` items respectively; this
    only returns a report dict, callers decide what to do with `exit_code`."""
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return {
            "run_dir": str(run_dir), "run_dir_exists": False, "state_path": None,
            "generated_at": _utc_now_iso(), "checks": [], "cells": {},
            "summary": {"total": 0, "pass": 0, "fail": 0, "unknown": 0, "informational": 0},
            "exit_code": 2,
        }

    queue_state, resolved_state_path = load_queue_state(run_dir, state_path)
    cell_dirs = discover_cell_dirs(run_dir)

    checks: List[Dict[str, Any]] = [check_abort_state(queue_state)]
    cells_report: Dict[str, Any] = {}

    state_cells = (queue_state or {}).get("cells") or {}
    # Union of every cell this run knows about: the queue state's own cell registry (authoritative
    # for "what state is this cell in") plus any subdirectory found on disk with no matching state
    # entry (an inconsistency worth surfacing, not silently dropping).
    all_cell_ids = sorted(set(state_cells) | set(cell_dirs))

    for cell_id in all_cell_ids:
        cell_state = (state_cells.get(cell_id) or {}).get("state")
        cell_dir = cell_dirs.get(cell_id)
        record_items, config, samples = check_records_present_and_conformant(cell_id, cell_state, cell_dir)
        checks.extend(record_items)
        checks.append(check_vram_arithmetic(cell_id, config))
        checks.append(check_prompt_tokens_tolerance(cell_id, samples))
        checks.append(check_wilson_min_trials(cell_id, config))
        checks.append(check_classifier_distribution(cell_id, samples))
        cells_report[cell_id] = {
            "state": cell_state,
            "reason": (state_cells.get(cell_id) or {}).get("reason"),
            "has_records": cell_dir is not None,
        }

    summary = {"total": len(checks), "pass": 0, "fail": 0, "unknown": 0, "informational": 0}
    for item in checks:
        summary[item["verdict"]] += 1

    if not checks or (queue_state is None and not cell_dirs):
        exit_code = 2
    elif summary["fail"] > 0:
        exit_code = 1
    else:
        exit_code = 0

    return {
        "run_dir": str(run_dir),
        "run_dir_exists": True,
        "state_path": str(resolved_state_path) if resolved_state_path else None,
        "queue_state": (queue_state or {}).get("queue_state"),
        "generated_at": _utc_now_iso(),
        "checks": checks,
        "cells": cells_report,
        "summary": summary,
        "exit_code": exit_code,
    }


# -------------------------------------------------------------------------------------------
# Report writers.
# -------------------------------------------------------------------------------------------

JSON_REPORT_NAME = "verify_report.json"
MARKDOWN_REPORT_NAME = "verify_report.md"


def write_json_report(run_dir: Path, result: Dict[str, Any]) -> Path:
    path = run_dir / JSON_REPORT_NAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=False)
        f.write("\n")
    return path


def _render_markdown(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Verifier report -- `{result['run_dir']}`")
    lines.append("")
    lines.append(f"Generated: {result['generated_at']}  ")
    lines.append(f"State file: {result.get('state_path') or '**NOT FOUND**'}  ")
    lines.append(f"Exit code: **{result['exit_code']}**")
    lines.append("")

    abort_items = [c for c in result["checks"] if c["check"] == CHECK_ABORT_STATE]
    abort = abort_items[0] if abort_items else None
    if abort and abort["verdict"] == "fail":
        lines.append("## ⚠ QUEUE ABORTED-HANG ⚠")
        lines.append("")
        lines.append(f"`queue_state` = `{abort['observed'].get('queue_state')}`. "
                      f"Aborted-hang cells: `{abort['observed'].get('aborted_hang_cells')}`.")
        lines.append("")
    elif abort and abort["verdict"] == "unknown":
        lines.append("## Queue state: UNKNOWN (no state file found)")
        lines.append("")

    summary = result["summary"]
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- total checks: {summary['total']}")
    lines.append(f"- pass: {summary['pass']}")
    lines.append(f"- fail: {summary['fail']}")
    lines.append(f"- unknown: {summary['unknown']}")
    lines.append(f"- informational: {summary['informational']}")
    lines.append("")

    lines.append("## Cells")
    lines.append("")
    for cell_id, cell in sorted(result["cells"].items()):
        flag = " **ABORTED-HANG**" if cell["state"] == batch.ABORTED_HANG else ""
        lines.append(f"### `{cell_id}`{flag}")
        lines.append("")
        lines.append(f"- state: `{cell['state']}`" + (f" (reason: `{cell['reason']}`)" if cell["reason"] else ""))
        lines.append(f"- has_records: `{cell['has_records']}`")
        lines.append("")
        lines.append("| check | verdict | expected | observed |")
        lines.append("| --- | --- | --- | --- |")
        for item in result["checks"]:
            if item["cell_id"] != cell_id:
                continue
            lines.append(f"| {item['check']} | {item['verdict']} | {item['expected']} | {json.dumps(item['observed'], default=str)} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_markdown_report(run_dir: Path, result: Dict[str, Any]) -> Path:
    path = run_dir / MARKDOWN_REPORT_NAME
    path.write_text(_render_markdown(result), encoding="utf-8")
    return path


# -------------------------------------------------------------------------------------------
# CLI -- the exact seam `batch.py cmd_verify` calls: `verify_run.main(["--run", <dir>])`.
# -------------------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify_run.py",
        description="Post-queue mechanical verification "
        "(plans/longcontext-pow2-overnight-campaign.md WP-4). See llama/bakeoff/README.md.",
    )
    parser.add_argument("--run", required=True, help="run directory (see module docstring for its shape)")
    parser.add_argument("--state", default=None, help="explicit queue state-file path (default: auto-discovered)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    run_dir = Path(args.run)
    state_path = Path(args.state) if args.state else None

    result = verify_run(run_dir, state_path)

    if not result["run_dir_exists"]:
        print(f"verify_run: run directory {run_dir} does not exist -- nothing to verify", file=sys.stderr)
        return result["exit_code"]

    write_json_report(run_dir, result)
    write_markdown_report(run_dir, result)
    print(json.dumps(result["summary"], indent=2, sort_keys=True))
    if result["exit_code"] == 2:
        print("verify_run: nothing to verify (no queue state file and no per-cell records found)", file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
