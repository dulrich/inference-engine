#!/usr/bin/env python3
"""Attention/indexer geometry and KV-cache byte math (WP-R3 of
`plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding F5).

Extracted verbatim from `gguf_fit.py` (see `gguf_reader.py`'s docstring for the whole split).
Every symbol keeps its original name, signature, and behavior; `gguf_fit` re-exports
`gguf_attention_geometry`, `KV_TYPE_BYTES_PER_ELEMENT`, `INDEXER_GEOMETRY_KEYS`,
`IndexerGeometry`, and `kv_bytes_per_token`, so `gguf_fit.kv_bytes_per_token`,
`sweep.IndexerGeometry`, and `cell_model.IndexerGeometry` (which imports it from `gguf_fit`) all
still resolve unchanged.

`IndexerGeometry` still lives beside the arithmetic that consumes it, for the same structural
reason it was not put in `cell_model.py`: `cell_model` already imports `sweep`, which imports
`gguf_fit`, which imports this module -- defining it upward would close an import cycle.

Pure: no I/O of any kind. The SWA and MLA caveats documented on `gguf_attention_geometry` and
`kv_bytes_per_token` are unchanged and remain the whole story on when these numbers are
approximate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gguf_reader import GGUFParseError


def gguf_attention_geometry(metadata: dict[str, Any]) -> dict[str, int | None]:
    """Extracts the handful of `<arch>.*` metadata keys the KV-cache-size formula needs.

    `key_length`/`value_length` fall back to `embedding_length // head_count` when the GGUF
    doesn't carry them explicitly (older conversions predate those keys; the fallback matches
    llama.cpp's own default when they're absent).

    **Hybrid-architecture caveat** (documented, not silently wrong): `block_count` here is the
    GGUF's *total* transformer-block count. Some architectures interleave real-attention blocks
    with recurrent/state-space blocks that carry no standard KV cache at all --
    `docs/qwen38-27b-bakeoff-results.md:12-15` documents exactly this for `qwen35`
    (`full_attention_interval=4`, 65 blocks -> only 16 real-attention layers). Using raw
    `block_count` for such an architecture overestimates KV bytes/token by the same factor the
    real attention-layer fraction differs from 1.0. `Cell.kv_attention_layers` (sweep-spec field)
    is the explicit override for exactly this case -- callers who know their architecture's real
    attention-layer count should set it; this function does not attempt to auto-detect hybrid
    architectures (no general, header-only signal for it across llama.cpp's whole architecture
    zoo).
    """
    arch = metadata.get("general.architecture")
    if not arch:
        raise GGUFParseError("GGUF metadata has no general.architecture key")

    def _get(suffix: str, default: Any = None) -> Any:
        return metadata.get(f"{arch}.{suffix}", default)

    embedding_length = _get("embedding_length")
    block_count = _get("block_count")
    head_count = _get("attention.head_count")
    head_count_kv = _get("attention.head_count_kv", head_count)
    key_length = _get("attention.key_length")
    value_length = _get("attention.value_length")
    if key_length is None and head_count and embedding_length:
        key_length = embedding_length // head_count
    if value_length is None:
        value_length = key_length
    context_length = _get("context_length")
    # SWA-awareness (audit B2, plan longcontext-pow2-overnight-campaign.md WP-1). Two more
    # `<arch>.attention.*` keys, pinned here by name (brief: "pin the chosen key names in code
    # comments") -- both verified live against the real gemma-4-12B-it-qat-UD-Q4_K_XL.gguf header
    # this WP grounds against:
    #   `attention.sliding_window` (scalar): the window size in tokens for whichever layers are
    #   window-capped (gemma4: 1024).
    #   `attention.sliding_window_pattern` (per-layer BOOL array, same length as the per-layer
    #   `attention.head_count_kv` array when that key is itself an array): `True` at a layer index
    #   means that layer is SWA/window-capped, `False` means it's a "global" full-context layer.
    #   Live-verified exact match against gemma4's own head_count_kv array (2026-08-23 grounding):
    #   48 entries, `sliding_window_pattern[i] == True` at every one of the 40 indices where
    #   `head_count_kv[i] == 8` (the SWA layers) and `== False` at every one of the 8 indices
    #   where `head_count_kv[i] == 1` (the global layers) -- zero mismatches. This is the ONLY
    #   signal `kv_bytes_per_token` trusts to split SWA vs. global layers; it never infers the
    #   split from `head_count_kv` magnitude alone (that heuristic is explicitly rejected -- see
    #   `kv_bytes_per_token`'s docstring).
    sliding_window = _get("attention.sliding_window")
    sliding_window_pattern = _get("attention.sliding_window_pattern")
    return {
        "architecture": arch,
        "block_count": block_count,
        "head_count": head_count,
        "head_count_kv": head_count_kv,
        "key_length": key_length,
        "value_length": value_length,
        "context_length": context_length,
        "sliding_window": sliding_window,
        "sliding_window_pattern": sliding_window_pattern,
    }


# -------------------------------------------------------------------------------------------
# KV-cache-type byte-per-element table (`-ctk`/`-ctv` values) and the bytes/token formula.
# -------------------------------------------------------------------------------------------

# ggml block-quant layout, block size 32 elements: block_bytes / 32 = effective bytes/element.
# f16/f32/bf16 are exact (no block quantization). Quantized block sizes are ggml's own
# `block_q*_0`/`block_q*_1` struct layouts (scale(s) stored per 32-element block):
#   q8_0: 32x int8 (32B) + 1x f16 scale (2B)                = 34B / 32 elem
#   q8_1: 32x int8 (32B) + 1x f16 scale (2B) + 1x f16 min (2B) = 36B / 32 elem
#   q5_0: 32x5-bit (20B) + 1x f16 scale (2B)                 = 22B / 32 elem
#   q5_1: 32x5-bit (20B) + 1x f16 scale (2B) + 1x f16 min (2B) = 24B / 32 elem
#   q4_0: 32x4-bit (16B) + 1x f16 scale (2B)                 = 18B / 32 elem
#   q4_1: 32x4-bit (16B) + 1x f16 scale (2B) + 1x f16 min (2B) = 20B / 32 elem
# Cross-checked against this repo's own live measurement: docs/qwen38-27b-bakeoff-results.md:158
# reports q4_0 K "42.0 KB/cell" vs q8_0 K+V "34.8 KB/cell" for its specific head geometry -- both
# numbers are this same block-size arithmetic scaled by that model's head_count_kv*key_length,
# not independently re-derived here.
KV_TYPE_BYTES_PER_ELEMENT: dict[str, float] = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 34 / 32,
    "q8_1": 36 / 32,
    "q5_0": 22 / 32,
    "q5_1": 24 / 32,
    "q4_0": 18 / 32,
    "q4_1": 20 / 32,
}

# The exact key set a DSA-indexer geometry block carries, in emission order (WP-1,
# qwen4exp-bakeoff-campaign, C4-B1). Named once, here, because `IndexerGeometry` below is the
# ONLY thing in this repo allowed to spell them -- there is no second registry of these strings.
INDEXER_GEOMETRY_KEYS: tuple[str, ...] = ("layers", "key_length", "value_length")


@dataclass(frozen=True)
class IndexerGeometry:
    """The second (DSA indexer) KV cache's own geometry, as a frozen value (WP-R2 of
    `plans/queue-contract-and-test-decomposition-remediation.md`).

    Lives HERE rather than in `cell_model.py` for one structural reason: `kv_bytes_per_token`
    below consumes it, and `cell_model` already imports `sweep`, which imports this module --
    defining it upward would close an import cycle. `cell_model` re-exports the name, so
    `cell_model.IndexerGeometry` / `batch.IndexerGeometry` / `sweep.IndexerGeometry` all resolve.

    `from_mapping` owns the validation that used to sit inline in `queue_file._validate_models`,
    message text included, so the queue-file gate and the fit math can never drift apart on what
    a well-formed geometry is. `to_mapping` is the one JSON emission form, key order fixed.
    """

    layers: int
    key_length: int
    value_length: int

    @classmethod
    def from_mapping(cls, raw: Any, *, prefix: str = "") -> IndexerGeometry:
        """Parses (and fully validates) a plain geometry mapping. `prefix` is prepended to the
        message of every `ValueError` raised here -- `queue_file` passes `"models[<id>] "` so the
        queue-validation text stays byte-identical to the pre-WP-R2 inline check."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(INDEXER_GEOMETRY_KEYS):
            raise ValueError(
                f"{prefix}kv_indexer_geometry {raw!r} must be a dict "
                f"with exactly the keys {INDEXER_GEOMETRY_KEYS} when present"
            )
        for key in INDEXER_GEOMETRY_KEYS:
            value = raw[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(
                    f"{prefix}kv_indexer_geometry[{key!r}] {value!r} must be "
                    f"a positive int"
                )
        return cls(
            layers=raw["layers"], key_length=raw["key_length"], value_length=raw["value_length"],
        )

    @classmethod
    def coerce(cls, raw: Mapping[str, Any] | IndexerGeometry) -> IndexerGeometry:
        """The thin compatibility adapter at the legacy plain-dict seam: `verify_run` and
        `classify_fit` still hand this module a mapping read straight off a record/cell dict."""
        return raw if isinstance(raw, cls) else cls.from_mapping(raw)

    def to_mapping(self) -> dict[str, int]:
        """The one JSON emission form -- ordered `layers`/`key_length`/`value_length`, which is
        the order every committed record's `sweep_cell.kv_indexer_geometry` block already has."""
        return {
            "layers": self.layers,
            "key_length": self.key_length,
            "value_length": self.value_length,
        }


def kv_bytes_per_token(
    geometry: dict[str, int | None],
    ctk: str,
    ctv: str,
    kv_attention_layers: int | None = None,
    context: int | None = None,
    swa_layer_spec: list[bool] | None = None,
    kv_indexer_geometry: Mapping[str, Any] | IndexerGeometry | None = None,
) -> float:
    """KV-cache bytes/token = attention_layers * (head_count_kv*key_length*bpe(ctk) +
    head_count_kv*value_length*bpe(ctv)) -- llama.cpp's own per-layer K+V cache tensor sizing.

    `kv_attention_layers` overrides `geometry["block_count"]` for hybrid architectures (see
    `gguf_attention_geometry`'s docstring); defaults to the GGUF's raw `block_count` when absent.

    **Per-layer-array `head_count_kv`** (orchestrator fix, first surfaced by the WP-1 live smoke
    cell against the real BailingMoE3 GGUF header): several live architectures store
    `attention.head_count_kv` as a per-layer ARRAY, not a scalar -- BailingMoE3 (24 entries,
    nonzero at exactly the 6 MLA layers) and gemma4 (48 entries: 8 for the 40 SWA layers, 1 for
    the 8 global layers) both do. In that case the array is itself the ground truth for which
    layers carry KV and how many heads each has, so bytes/token is the per-layer sum and both
    `kv_attention_layers` and `block_count` are ignored (zero entries contribute nothing --
    the hybrid-architecture problem the scalar-path override exists for never arises).
    Caveat shared with the scalar path: this is standard K/V-tensor arithmetic. For MLA layers
    it can mis-estimate in EITHER direction (live check, Ling-3.0-tiny Q4_K_M: predicted
    8448 B/token vs ~14.4 KB/token measured at -c 8192 -- llama.cpp's MLA cache layout is not
    plain head_count_kv*key/value_length) -- treat per-layer-array MLA fits as approximate and
    lean on the live VRAM read-back, not this number, near any margin boundary.

    **SWA-awareness (audit B2, longcontext-pow2-overnight-campaign.md WP-1)**: the pre-existing
    per-layer-array path above scaled EVERY array entry with the full requested `context` -- wrong
    for an architecture like gemma4, where only the minority "global" layers actually scale with
    context; the majority "SWA" layers are capped at a fixed window regardless of `-c`. When
    `context` is given AND a per-layer SWA signal is available -- either the caller-supplied
    `swa_layer_spec` override (same shape as `head_count_kv`, `True` = window-capped) or, absent
    that, `geometry["sliding_window_pattern"]` read straight from the GGUF (see
    `gguf_attention_geometry`) -- AND `geometry["sliding_window"]` is known, each layer
    contributes `min(context, sliding_window)` tokens' worth of bytes if it's SWA, or `context`
    tokens' worth if it's global; this function still returns a bytes/TOKEN rate (not a total),
    computed as `total_bytes_at_context / context`, so `classify_fit`'s existing
    `bytes_per_token * context` formula recovers the exact same total unchanged. **No heuristic
    fallback**: if no per-layer SWA signal is available at all (no override, no
    `sliding_window_pattern` in the header, or a length mismatch against `head_count_kv`), this
    silently reverts to the pre-existing conservative behavior -- every array entry scales with
    the full context, i.e. today's (over-estimating, not under-estimating) behavior -- rather than
    ever guessing which layers are SWA from `head_count_kv` magnitude alone.

    **`kv_indexer_geometry` (WP-1, qwen4exp-bakeoff-campaign, C4-B1)**: some architectures (qwen4exp)
    allocate a SECOND KV cache -- a DSA indexer cache -- this module otherwise has no
    representation for at all. When given, `{"layers": int, "key_length": int, "value_length":
    int}` describes that cache's own geometry (distinct from the main cache's `head_count_kv`/
    `key_length`/`value_length` -- the indexer's K and V dims routinely differ from the main
    cache's, so it cannot be modeled by inflating `kv_attention_layers`), and this function adds
    a FLAT per-token rate, `layers * (key_length + value_length) * KV_TYPE_BYTES_PER_ELEMENT
    ["f16"]`, on top of the main-cache rate above -- independent of `context` and of the
    SWA path (the indexer cache is not window-capped; measured: 36 MiB at 4096 cells with
    `layers=12, key_length=128, value_length=256` is exactly `12 * (128*2 + 256*2) = 9,216`
    B/token * 4096). **Scoped to measured f16 behaviour**: nothing in this repo establishes that
    the indexer cache honours the main cache's own `ctk`/`ctv` at a non-f16 KV type, so when
    `kv_indexer_geometry` is given and `ctk`/`ctv` are not BOTH `"f16"`, this raises `ValueError`
    (which `classify_fit` turns into `{"classification": "unknown", ...}`, never a guessed
    number) rather than silently misclassifying a quantized-KV cell. Absent `kv_indexer_geometry`
    is byte-identical to pre-C4-B1 behavior. Either an `IndexerGeometry` or the plain mapping
    every legacy caller passes is accepted; the mapping is parsed (and validated) at this seam.
    """
    layers = kv_attention_layers if kv_attention_layers is not None else geometry.get("block_count")
    head_count_kv = geometry.get("head_count_kv")
    key_length = geometry.get("key_length")
    value_length = geometry.get("value_length")
    per_layer_kv = isinstance(head_count_kv, (list, tuple))
    required = [head_count_kv, key_length, value_length] if per_layer_kv else [layers, head_count_kv, key_length, value_length]
    if not all(required):
        raise GGUFParseError(
            f"incomplete attention geometry for KV bytes/token: {geometry!r} "
            f"(kv_attention_layers override: {kv_attention_layers!r})"
        )
    try:
        bpe_k = KV_TYPE_BYTES_PER_ELEMENT[ctk]
        bpe_v = KV_TYPE_BYTES_PER_ELEMENT[ctv]
    except KeyError as exc:
        raise ValueError(
            f"unknown KV cache type {exc}; known types: {sorted(KV_TYPE_BYTES_PER_ELEMENT)}"
        ) from exc
    # `all(required)` above already raised on a missing/zero dimension.
    assert key_length is not None and value_length is not None
    per_token_both = key_length * bpe_k + value_length * bpe_v
    if per_layer_kv:
        spec = swa_layer_spec if swa_layer_spec is not None else geometry.get("sliding_window_pattern")
        window = geometry.get("sliding_window")
        if (
            context is not None
            and window is not None
            and isinstance(spec, (list, tuple))
            and len(spec) == len(head_count_kv)
        ):
            capped_tokens = min(context, window)
            total_bytes = sum(
                h * per_token_both * (capped_tokens if is_swa else context)
                for h, is_swa in zip(head_count_kv, spec)
                if h
            )
            main_rate = total_bytes / context
        else:
            main_rate = float(sum(h * per_token_both for h in head_count_kv if h))
    else:
        # Non-per-layer path: `required` included both, so `all(required)` guarantees them.
        assert layers is not None and head_count_kv is not None
        main_rate = layers * head_count_kv * per_token_both

    if kv_indexer_geometry is None:
        return main_rate
    if ctk != "f16" or ctv != "f16":
        raise ValueError(
            f"kv_indexer_geometry given but the indexer cache's storage type at ctk={ctk!r}/"
            f"ctv={ctv!r} is unmeasured (C4-B1 scopes the indexer term to f16/f16 only) -- "
            f"measure the indexer's storage type at this KV type before classifying this cell"
        )
    # Typed from here down: the arithmetic reads named fields, never string keys. `coerce` is
    # a no-op for a caller that already holds the typed value (`verify_run`) and the parse for
    # one that hands over a plain mapping off a record/cell dict (`classify_fit`). It runs AFTER
    # the f16 check above so a quantized-KV cell still gets the indexer-naming message first.
    indexer = IndexerGeometry.coerce(kv_indexer_geometry)
    indexer_rate = (
        indexer.layers
        * (indexer.key_length + indexer.value_length)
        * KV_TYPE_BYTES_PER_ELEMENT["f16"]
    )
    return main_rate + indexer_rate
