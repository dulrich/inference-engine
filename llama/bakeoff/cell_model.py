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
  override, the base-vs-MTP arm, and (WP-R2) the `model_family` string the live runner picks a
  sampling preset with.
- `FitMetadata` (WP-R2) carries the optional model-registry facts `sweep.py`'s fit math reads --
  `gpu_weights_bytes`, `kv_attention_layers`, `swa_layer_spec`, and an `IndexerGeometry` -- as
  named, typed fields. It replaced a `Dict[str, Any]` bag keyed by a tuple of magic strings.
- `RecordMetadata` carries exactly what `cell_record.write_cell_record` stamps into a record's
  `config.json` `sweep_cell` block.
- `ResolvedCell` composes the five and owns the **one** dict adapter (`as_sweep_cell`) at the
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

# `IndexerGeometry` is DEFINED in `gguf_fit` (the module whose KV arithmetic consumes it) and
# re-exported here so the domain-value surface is one import for every consumer. Defining it in
# this module instead would close an import cycle: `cell_model` -> `sweep` -> `gguf_fit`.
from gguf_fit import IndexerGeometry

__all__ = [
    "CellIdentity",
    "ExecutionSettings",
    "FitMetadata",
    "IndexerGeometry",
    "QueueCellMetadata",
    "RecordMetadata",
    "ResolvedCell",
    "replace",
]


@dataclass(frozen=True)
class FitMetadata:
    """The optional model-registry facts `sweep.py`'s fit math reads, as one frozen value
    (WP-R2 of `plans/queue-contract-and-test-decomposition-remediation.md`; review finding F2).

    Before WP-R2 these rode in one untyped `Dict[str, Any]` pass-through bag on `ResolvedCell`,
    keyed by a module-level tuple of five wire-name strings -- names spelled in three places,
    with nothing but that tuple keeping the queue reader, the `sweep.py` adapter and the record
    writer agreeing on them, and no type on any value. The fields below are the same five facts
    minus `model_family` (which is not fit-bearing and now lives explicitly on
    `QueueCellMetadata`, where the live runner reads it).

    **`None` means "unset", and unset is the only absence there is.** Both an omitted registry
    key and an explicit JSON `null` parse to `None` -- exactly what the pre-WP-R2
    `if model_entry.get(key) is not None` filter did (`queue_file.resolve_cell`, lines 281-283 at
    69593b3): an explicit `null` never reached the passthrough dict, so it was indistinguishable
    from absence. That equivalence is what lets `from_mapping` below read a queue registry entry
    and a persisted `sweep_cell` block with the same code.
    """

    gpu_weights_bytes: Optional[int] = None
    kv_attention_layers: Optional[int] = None
    swa_layer_spec: Optional[Tuple[bool, ...]] = None
    indexer_geometry: Optional[IndexerGeometry] = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "FitMetadata":
        """The ONE JSON -> typed adapter. Two callers, both reading a plain mapping whose keys
        are the wire names: `queue_file.resolve_cell` (a queue's `models[*]` registry entry) and
        `verify_run.check_vram_arithmetic` (a committed record's `config.json` `sweep_cell`
        block, which for historical records omits these keys entirely and for current ones
        carries them as explicit `null` -- `.get()` maps both to `None`, so the two record
        generations verify identically).
        """
        spec = raw.get("swa_layer_spec")
        geometry = raw.get("kv_indexer_geometry")
        return cls(
            gpu_weights_bytes=raw.get("gpu_weights_bytes"),
            kv_attention_layers=raw.get("kv_attention_layers"),
            swa_layer_spec=None if spec is None else tuple(spec),
            indexer_geometry=None if geometry is None else IndexerGeometry.coerce(geometry),
        )

    # -- JSON emission forms (both directions of the dict boundary use these, never a cast) ---

    def swa_layer_spec_json(self) -> Optional[List[bool]]:
        """A JSON list, never a tuple -- `json.dumps` cannot tell the two apart, but the record
        writer's `config.json` and every dict-equality assertion downstream can."""
        return None if self.swa_layer_spec is None else list(self.swa_layer_spec)

    def indexer_geometry_json(self) -> Optional[Dict[str, int]]:
        return None if self.indexer_geometry is None else self.indexer_geometry.to_mapping()


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

    `model_family` (WP-R2) is the GGUF `general.architecture` string the queue author writes into
    the model registry, which `cell_runner` uses to pick a `sampling_table.production_sampling`
    entry without re-parsing a GGUF header per cell. It sits here rather than on `FitMetadata`
    because nothing about it is fit-bearing -- it selects a sampling preset, not a byte count.
    """

    model_id: str
    sampling_arm: Optional[str] = None
    trials: Optional[int] = None
    chars_per_token: Optional[float] = None
    timing_rounds: Optional[int] = None
    spec_decode: Optional[str] = None
    model_family: Optional[str] = None


@dataclass(frozen=True)
class RecordMetadata:
    """Exactly what a persisted record's `config.json` `sweep_cell` block stamps.

    `sampling_arm` is deliberately NOT a field: the persisted arm is the *effective* one the
    runner resolved (a `production` request can fall back to `capability`), which is a run-time
    fact supplied by the caller, not a property of the resolved cell.

    The four fields after `spec_decode` (WP-1, qwen4exp-bakeoff-campaign, cycle-2 blocking 2) are
    the `FitMetadata` facts -- `gpu_weights_bytes`, `kv_attention_layers`, `swa_layer_spec`,
    `kv_indexer_geometry` -- that `as_sweep_cell()` already reached but the persisted record
    never did (`verify_run.py` was silently falling back to `lineage.file_size` and
    main-cache-only geometry for every historical record). They stay FLAT and JSON-shaped here
    (a list, an ordered plain mapping) rather than holding a `FitMetadata`, because this value's
    entire job is "exactly what the record stamps"; `ResolvedCell.record_metadata()` is the one
    place that converts. **Appended, never inserted**: key order below is load-bearing
    (`sweep_cell_block`'s own docstring) so a new record diffs cleanly against a historical one.
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
    optional typed fit metadata.

    This is the value `resolve_queue_cells` returns, the state machine keys off, the cell runner
    receives, and the record writer stamps. Every one of its four sub-values is a frozen
    dataclass with named fields -- there is no dict-shaped back door left (WP-R2 closed the last
    one, an untyped pass-through bag). The single adapter down to the plain-dict world is
    `as_sweep_cell()`, and it is the only thing handed to `sweep.py`.
    """

    identity: CellIdentity
    execution: ExecutionSettings
    queue: QueueCellMetadata
    fit: FitMetadata = field(default_factory=FitMetadata)

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
        return self.queue.model_family

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
        # Each optional fact is emitted ONLY when set, and in the pre-WP-R2 key order -- the
        # key-is-in-the-bag rule the old string-keyed loop implemented. "Unset" is `None` here
        # and was "the key never entered the bag" there; they mean the same thing, because
        # `resolve_cell` only ever put a non-`None` registry value in (see
        # `FitMetadata.from_mapping`). `record_metadata()` below is the OTHER rule: all four
        # keys always present, `null` when unset.
        if self.fit.gpu_weights_bytes is not None:
            cell["gpu_weights_bytes"] = self.fit.gpu_weights_bytes
        if self.fit.kv_attention_layers is not None:
            cell["kv_attention_layers"] = self.fit.kv_attention_layers
        if self.fit.swa_layer_spec is not None:
            cell["swa_layer_spec"] = self.fit.swa_layer_spec_json()
        if self.queue.model_family is not None:
            cell["model_family"] = self.queue.model_family
        if self.fit.indexer_geometry is not None:
            cell["kv_indexer_geometry"] = self.fit.indexer_geometry_json()
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
            gpu_weights_bytes=self.fit.gpu_weights_bytes,
            kv_attention_layers=self.fit.kv_attention_layers,
            swa_layer_spec=self.fit.swa_layer_spec_json(),
            kv_indexer_geometry=self.fit.indexer_geometry_json(),
        )
