#!/usr/bin/env python3
"""Typed, immutable domain values for one bake-off queue cell (WP-R1 of
`plans/repository-tooling-structural-remediation.md`; review findings F2/F3).

**What this module exists to make impossible.** Before WP-R1, `batch.resolve_cell` built one
plain dict, computed `cell_id` from it, and then bolted seven non-identity keys onto the *same*
dict afterwards -- four of them underscore-prefixed as a pseudo-private API
(`_queue_model_id`/`_sampling_arm`/`_trials`/`_chars_per_token`) and three un-prefixed
(`model`/`timing_rounds`/`spec_decode`). Nothing but comment discipline and reviewer vigilance
kept the next contributor from setting one of those *before* `compute_cell_id` and silently
forking every stored cell_id in every consumer.

The split here is structural instead of clerical:

- `CellIdentity` carries **exactly** the four fields `sweep.compute_cell_id` hashes, and nothing
  else. It is a frozen dataclass, so a non-identity field cannot be attached to it at all
  (`FrozenInstanceError`), and `cell_id` is a property computed *solely* from it.
- `ExecutionSettings` carries the normalized `server_flags`-derived run configuration (the
  context ceiling and the KV/flash-attention arm) plus the `battery` list.
- `QueueCellMetadata` carries what the underscore keys used to: the queue registry key, the
  sampling arm, the trial count, the measured chars-per-token knob, the per-cell timing-round
  override, and the base-vs-MTP arm.
- `RecordMetadata` carries exactly what `cell_record.write_cell_record` stamps into a record's
  `config.json` `sweep_cell` block.
- `ResolvedCell` composes the four and owns the **one** dict adapter (`as_sweep_cell`) at the
  `sweep.py` boundary. `sweep.normalize_cell`/`build_command`/`classify_fit`/`scratch_server`
  all take plain dicts and are deliberately not changed; typed values stop at that seam.

Cell identity is frozen by contract: the ordered cell_ids of every committed queue are pinned by
`tests/test_cell_identity.py`. Zero new runtime dependencies (stdlib only), matching the rest of
`llama/bakeoff/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# Same "no package `__init__.py` under `llama/`" gotcha every other module here documents: a
# plain top-level import works because the importer has already put this directory on sys.path.
import sweep

__all__ = [
    "CellIdentity",
    "ExecutionSettings",
    "QueueCellMetadata",
    "RecordMetadata",
    "ResolvedCell",
    "replace",
]

# The optional model-registry pass-throughs that are neither identity nor queue-only metadata:
# `sweep.py` reads them off the plain cell dict for its own fit math (`gpu_weights_bytes`,
# `kv_attention_layers`, `swa_layer_spec`, `kv_indexer_geometry`), and `model_family` is read by
# the live cell runner to pick a `sampling_table.production_sampling` entry. They ride through
# the adapter verbatim. `kv_indexer_geometry` (WP-1, qwen4exp-bakeoff-campaign, C4-B1) is the
# newest addition: a second-KV-cache (DSA indexer) geometry block `gguf_fit.kv_bytes_per_token`
# adds on top of the main cache's bytes/token, f16-scoped.
SWEEP_PASSTHROUGH_KEYS: Tuple[str, ...] = (
    "gpu_weights_bytes", "kv_attention_layers", "swa_layer_spec", "model_family",
    "kv_indexer_geometry",
)


@dataclass(frozen=True)
class CellIdentity:
    """The four fields -- and only the four fields -- that `sweep.compute_cell_id` hashes.

    `server_flags` must already be NORMALIZED (`sweep.normalize_cell` has filled its defaults):
    identity is a hash over the flags actually handed to `llama-server`, not over the abbreviated
    queue entry's partial overrides.
    """

    alias: str
    model_container_path: str
    server_flags: Dict[str, Any]
    model_host_path: Optional[str] = None

    def identity_payload(self) -> Dict[str, Any]:
        """The exact dict shape `sweep.compute_cell_id` reads. Kept as a method rather than
        inlined so the one place that defines "what identity means" is greppable."""
        return {
            "alias": self.alias,
            "model_container_path": self.model_container_path,
            "model_host_path": self.model_host_path,
            "server_flags": self.server_flags,
        }

    @property
    def cell_id(self) -> str:
        """Delegates to `sweep.compute_cell_id` verbatim -- never a reimplementation. The hash is
        over sorted-key canonical JSON, so the insertion order of `server_flags` is irrelevant."""
        return sweep.compute_cell_id(self.identity_payload())


@dataclass(frozen=True)
class ExecutionSettings:
    """Normalized run configuration derived from `server_flags`, plus the battery to run.

    `context`/`kv_key_type`/`kv_value_type` are the axes skip-propagation and the pre-enqueue
    size check reason about; reading them off named fields rather than re-indexing a flags dict
    at four call sites is the whole point.
    """

    battery: Tuple[str, ...]
    context: int
    kv_key_type: str
    kv_value_type: str
    flash_attention: Any

    @classmethod
    def from_flags(cls, flags: Mapping[str, Any], battery: Sequence[str]) -> "ExecutionSettings":
        return cls(
            battery=tuple(battery),
            context=flags["-c"],
            kv_key_type=flags.get("-ctk", "f16"),
            kv_value_type=flags.get("-ctv", "f16"),
            flash_attention=flags.get("-fa", "on"),
        )

    @property
    def quantized_kv(self) -> bool:
        """True when either KV arm is non-`f16` -- skip propagation's own gate (a quantized-KV
        correctness failure predicts failure at larger contexts on the same arm; an f16 one does
        not)."""
        return not (self.kv_key_type == "f16" and self.kv_value_type == "f16")


@dataclass(frozen=True)
class QueueCellMetadata:
    """Queue-only, non-identity facts about one cell -- the typed replacement for the former
    `_queue_model_id`/`_sampling_arm`/`_trials`/`_chars_per_token`/`model`/`timing_rounds`/
    `spec_decode` keys. None of these can perturb `cell_id`, because none of them live on
    `CellIdentity`.

    `model_id` is the queue registry KEY (the estimator's model-identity pin, recorded as
    `sweep_cell.model`). `chars_per_token` is a measurement-accuracy knob, `trials` and
    `timing_rounds` are measurement-depth knobs, and `spec_decode` records which base/MTP arm
    already shaped `server_flags` upstream -- recording it a second time is a record-carrying
    fact, not an identity fact.
    """

    model_id: str
    sampling_arm: Optional[str] = None
    trials: Optional[int] = None
    chars_per_token: Optional[float] = None
    timing_rounds: Optional[int] = None
    spec_decode: Optional[str] = None


@dataclass(frozen=True)
class RecordMetadata:
    """Exactly what a persisted record's `config.json` `sweep_cell` block stamps.

    `sampling_arm` is deliberately NOT a field: the persisted arm is the *effective* one the
    runner resolved (a `production` request can fall back to `capability`), which is a run-time
    fact supplied by the caller, not a property of the resolved cell.

    The four fields after `spec_decode` (WP-1, qwen4exp-bakeoff-campaign, cycle-2 blocking 2) are
    the fit-bearing `sweep_passthrough` facts -- `gpu_weights_bytes`, `kv_attention_layers`,
    `swa_layer_spec`, `kv_indexer_geometry` -- that `as_sweep_cell()` already reached but the
    persisted record never did (`verify_run.py` was silently falling back to `lineage.file_size`
    and main-cache-only geometry for every historical record). **Appended, never inserted**: key
    order below is load-bearing (`sweep_cell_block`'s own docstring) so a new record diffs
    cleanly against a historical one.
    """

    cell_id: str
    alias: Optional[str]
    server_flags: Optional[Dict[str, Any]]
    model_host_path: Optional[str]
    battery: Optional[List[str]]
    model_id: Optional[str]
    spec_decode: Optional[str]
    gpu_weights_bytes: Optional[int] = None
    kv_attention_layers: Optional[int] = None
    swa_layer_spec: Optional[List[bool]] = None
    kv_indexer_geometry: Optional[Dict[str, Any]] = None

    def sweep_cell_block(self, sampling_arm_effective: Optional[str]) -> Dict[str, Any]:
        """The `config.json` `sweep_cell` mapping, key order preserved from the pre-WP-R1
        `write_cell_record` literal so a newly written record diffs cleanly against a historical
        one. The four fit-passthrough keys are appended after `spec_decode`, always present
        (`null` when unset) -- a deterministic record shape; `verify_run.py` reads every one of
        them with `.get()`."""
        return {
            "cell_id": self.cell_id,
            "alias": self.alias,
            "server_flags": self.server_flags,
            "model_host_path": self.model_host_path,
            "battery": self.battery,
            "sampling_arm": sampling_arm_effective,
            "model": self.model_id,
            "spec_decode": self.spec_decode,
            "gpu_weights_bytes": self.gpu_weights_bytes,
            "kv_attention_layers": self.kv_attention_layers,
            "swa_layer_spec": self.swa_layer_spec,
            "kv_indexer_geometry": self.kv_indexer_geometry,
        }


@dataclass(frozen=True)
class ResolvedCell:
    """One fully resolved queue cell: identity + execution settings + queue metadata + the
    optional `sweep.py` fit pass-throughs.

    This is the value `resolve_queue_cells` returns, the state machine keys off, the cell runner
    receives, and the record writer stamps. It never grows a dict-shaped back door: the single
    adapter to the plain-dict world is `as_sweep_cell()`, and it is the only thing handed to
    `sweep.py`.
    """

    identity: CellIdentity
    execution: ExecutionSettings
    queue: QueueCellMetadata
    sweep_passthrough: Dict[str, Any] = field(default_factory=dict)

    # -- convenience readers (all derived; none of them is separate state) --------------------

    @property
    def cell_id(self) -> str:
        return self.identity.cell_id

    @property
    def alias(self) -> str:
        return self.identity.alias

    @property
    def server_flags(self) -> Dict[str, Any]:
        return self.identity.server_flags

    @property
    def battery(self) -> Tuple[str, ...]:
        return self.execution.battery

    @property
    def context(self) -> int:
        return self.execution.context

    @property
    def model_host_path(self) -> Optional[str]:
        return self.identity.model_host_path

    @property
    def model_family(self) -> Optional[str]:
        return self.sweep_passthrough.get("model_family")

    # -- the one dict adapter at the sweep.py boundary ---------------------------------------

    def as_sweep_cell(self) -> Dict[str, Any]:
        """The plain-dict cell `sweep.normalize_cell`/`classify_fit`/`build_command`/
        `scratch_server` consume. Byte-for-byte the pre-WP-R1 normalized cell MINUS the seven
        bolted-on metadata keys, plus `cell_id` (which `sweep.scratch_server` reads).

        `model_host_path` is omitted rather than set to `None` when absent, matching what
        `resolve_cell` always built -- `compute_cell_id` uses `.get()`, so both spellings hash
        identically, but the adapter reproduces the historical shape exactly.
        """
        cell: Dict[str, Any] = {
            "alias": self.identity.alias,
            "model_container_path": self.identity.model_container_path,
            "server_flags": dict(self.identity.server_flags),
            "battery": list(self.execution.battery),
        }
        if self.identity.model_host_path is not None:
            cell["model_host_path"] = self.identity.model_host_path
        for key in SWEEP_PASSTHROUGH_KEYS:
            if key in self.sweep_passthrough:
                cell[key] = self.sweep_passthrough[key]
        cell["cell_id"] = self.cell_id
        return cell

    def record_metadata(self) -> RecordMetadata:
        # `model_family` is deliberately NOT read here -- it's not fit-bearing, and
        # `cell_runner.py` already records it elsewhere in the live run path.
        return RecordMetadata(
            cell_id=self.cell_id,
            alias=self.identity.alias,
            server_flags=dict(self.identity.server_flags),
            model_host_path=self.identity.model_host_path,
            battery=list(self.execution.battery),
            model_id=self.queue.model_id,
            spec_decode=self.queue.spec_decode,
            gpu_weights_bytes=self.sweep_passthrough.get("gpu_weights_bytes"),
            kv_attention_layers=self.sweep_passthrough.get("kv_attention_layers"),
            swa_layer_spec=self.sweep_passthrough.get("swa_layer_spec"),
            kv_indexer_geometry=self.sweep_passthrough.get("kv_indexer_geometry"),
        )
