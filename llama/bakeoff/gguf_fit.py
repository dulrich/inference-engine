#!/usr/bin/env python3
"""Pure GGUF geometry, KV-cache byte math, VRAM budgets, and the fit pre-filter (WP-R3 of
`plans/repository-tooling-structural-remediation.md`; review finding F8).

Extracted verbatim from `sweep.py`, which had grown to 1,265 lines by carrying three unrelated
concerns in one file: this pure-math layer, the scratch-server process lifecycle
(`scratch_lifecycle.py`), and spec/CLI composition (`sweep.py`). Every symbol here keeps its
original name, signature, and behavior, and `sweep.py` re-exports all of them, so
`sweep.classify_fit`/`sweep.kv_bytes_per_token`/`sweep.GIB`/... all still resolve for every
consumer (`batch.py`, `queue_file.py`, `cell_model.py`, `longcontext.py`, the test suite).

**This module is pure.** Its only I/O is reading a GGUF file's header/metadata block off disk
and reading the two host sysfs VRAM counters -- no `docker`, no network, no clock, no
subprocess. The two sysfs readers (`read_vram_total_bytes`/`read_vram_used_bytes`) are the real,
live-capable defaults of injectable parameters; every test passes a fake and none of them touch
sysfs (same "real controller, fake in tests" pattern as `ContainerController` in `batch.py`).

Zero new runtime dependencies: Python 3 stdlib only (`struct`, `json`-free, `hashlib`-free here).
The GGUF reader is deliberately hand-rolled on `struct` -- no `gguf`, no `numpy`.

The fit numbers this module computes are ESTIMATES that gate what gets attempted; they never
substitute for a real load-and-measure (house rule, `plans/quant-sweep-harness-and-instrument-
v2.md` Key Changes/WP-1). See `classify_fit`.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

GIB = 1024 ** 3


# -------------------------------------------------------------------------------------------
# VRAM-fit pre-filter constants -- every number here is cited to a live measurement already on
# record in this repo's docs/, never invented for this WP. These are pure-math *predictors*
# that gate what gets attempted; they never substitute for a real load (house rule -- plan
# Key Changes/WP-1: "Estimates gate what gets attempted, never substitute for measurement").
# -------------------------------------------------------------------------------------------

# Card nameplate, repeated verbatim across every prior bake-off doc this repo has written:
# docs/qwen38-27b-bakeoff-results.md:51,182,200; docs/ling-3-tiny-bakeoff-results.md:29.
CARD_NAMEPLATE_VRAM_GIB = 15.92

# The plan's own stated sizing budget for this exact framework, verbatim: "enumerate quant
# configurations (model x weight-quant x KV-cache type x -c) that physically fit the RX 9070's
# ~12.8 GiB realistic free VRAM" (plans/quant-sweep-harness-and-instrument-v2.md:50-51; repeated
# in root CLAUDE.md: "VRAM is ~12.8 GiB and contended"). Deliberately more conservative than the
# card nameplate (15.92 GiB): prior bake-off docs computed their own accept/reject margins
# directly against nameplate (e.g. docs/qwen38-27b-bakeoff-results.md:61's -c 40960 f16/f16
# landing measured only a 1.85 GiB *nameplate* margin -- this pre-filter, sized against 12.8 GiB
# instead, would predict that exact cell "excluded", not "fits"). That is the intended, plan-
# directed failure direction for an ESTIMATE gating what gets attempted -- a false-negative
# "excluded"/"marginal" cell costs nothing but a skipped pre-filter recommendation; the "marginal"
# tier exists precisely so a close call isn't collapsed into a hard binary. Real free VRAM at
# sweep-run time should still be read back live (`read_vram_used_bytes()`) before trusting a
# borderline classification -- this constant is a planning number, not a live reading.
REALISTIC_FREE_VRAM_GIB = 12.8

# Non-weight, non-KV-cache overhead (compute/graph buffers llama.cpp allocates beyond the raw
# weights + KV cache tensors). Cited directly: docs/qwen38-27b-bakeoff-results.md:177-179 found a
# fresh cold load of the landed qwen38-27b-iq3s q4_0/q4_0 config measured 13.720 GiB total against
# 11.214 GiB weights + 2110.50 MiB (measured, scratch-log-verbatim) KV cache -- leaving ~456 MiB
# unaccounted for, described there as "a plausible compute/graph buffer". Rounded up to 0.5 GiB
# for a conservative (over-, not under-, estimating) pre-filter band.
NON_WEIGHT_OVERHEAD_GIB = 0.5

# The "~1.5-2 GiB safety-margin convention" this repo's bake-offs have applied repeatedly when
# accepting/rejecting a candidate context ceiling: docs/qwen38-27b-bakeoff-results.md:59-61 (0.32
# GiB and 1.34 GiB margins REJECTED, 1.83-1.85 GiB LANDED) and :200 (2.14-2.21 GiB margin, "meets
# and slightly exceeds" the convention). Modeled here as two thresholds against the *predicted*
# margin (REALISTIC_FREE_VRAM_GIB - estimated_total): >= FIT_MARGIN_GIB classifies "fits", between
# the two classifies "marginal" (attempt-with-caution), below MARGINAL_MARGIN_GIB classifies
# "excluded". A predicted "marginal" cell that would, in reality, have landed (like the 1.83-1.85
# GiB examples above) is the intended conservative failure direction for an *estimate* whose whole
# job is to gate attempts, not replace the real load-and-measure step.
FIT_MARGIN_GIB = 2.0
MARGINAL_MARGIN_GIB = 1.5

# -------------------------------------------------------------------------------------------
# VRAM budget profiles (audit B4, plan longcontext-pow2-overnight-campaign.md WP-1). Two named
# profiles a queue selects between, never a bare constant: `desktop-shared` is
# REALISTIC_FREE_VRAM_GIB above, unchanged in meaning (this machine's normal, desktop-sharing-the-
# GPU state); `headless` is computed LIVE at queue-start time from the actual measured VRAM
# state, for an operator who has confirmed no other GPU client is running.
# -------------------------------------------------------------------------------------------

VRAM_PROFILES = ("desktop-shared", "headless")

# Subtracted from the live-measured `mem_info_vram_total` for the `headless` profile's admissible
# budget -- a small reserve for whatever isn't captured by `mem_info_vram_used` at the instant of
# reading (e.g. driver/firmware reservations). Card nameplate 15.92 GiB (CARD_NAMEPLATE_VRAM_GIB)
# minus the plan's own stated "headless" usable figure, "measured mem_info_vram_total minus
# margin, ~15.4 GiB usable" (Decisions locked) = ~0.52 GiB; rounded to a clean 0.5 GiB.
HEADLESS_PINNED_MARGIN_GIB = 0.5

# `headless` REFUSES outright (raises VRAMProfileRefused) when the measured baseline VRAM-used
# figure exceeds this ceiling -- the profile's whole premise is "nothing else is on the GPU", and
# a live desktop session invalidates it. Pinned per this WP's brief ("pick and document a value
# ~1.5 GiB -- a live desktop holds ~3.1 GiB, so it must trip then"): `llama/config.yaml`'s gemma
# fit-math comment measured `mem_info_vram_used` = 3,327,463,424 B (~3.1 GiB) on this exact
# desktop-shared host with ghostty/firefox/LM Studio/obsidian/nvtop all holding the render node --
# well above 1.5 GiB, so that live baseline correctly trips this refusal.
HEADLESS_BASELINE_CEILING_GIB = 1.5


class VRAMProfileRefused(RuntimeError):
    """Raised by `compute_vram_budget_bytes('headless', ...)` when the measured VRAM-used
    baseline exceeds HEADLESS_BASELINE_CEILING_GIB -- a mischosen `headless` profile on a live
    desktop session fails loudly here instead of silently overcommitting VRAM."""


def read_vram_total_bytes(sysfs_path: str = "/sys/class/drm/card1/device/mem_info_vram_total") -> int:
    """Reads `mem_info_vram_total` directly from host sysfs -- same mechanism and same host-path
    rationale as `read_vram_used_bytes()` below (this repo's established VRAM read-back
    technique). Real, live-capable; never invoked by this WP's own tests (`compute_vram_budget_
    bytes`'s `vram_total_bytes_fn` parameter is what tests fake)."""
    with open(sysfs_path, "r", encoding="utf-8") as f:
        return int(f.read().strip())


# WP-R3 note: this reader sat down in `sweep.py`'s scratch-server section, one screen away from
# `read_vram_total_bytes` and from `compute_vram_budget_bytes`, whose `headless` branch is its
# main caller. It is a pure sysfs read with no process-lifecycle content, so it belongs with its
# twin here; `scratch_lifecycle.teardown_scratch_server` imports it from this module, and
# `sweep.read_vram_used_bytes` still resolves to this exact function object.
def read_vram_used_bytes(sysfs_path: str = "/sys/class/drm/card1/device/mem_info_vram_used") -> int:
    """Reads `mem_info_vram_used` directly from host sysfs -- this repo's established VRAM
    read-back mechanism (`docs/ling-3-tiny-bakeoff-results.md:33`, `docs/gemma-4-12b-qat-bakeoff-
    results.md:26`, `docs/qwen36-35b-a3b-bakeoff-results.md:40`: "read directly, never assumed
    from weight size"). Read from the HOST path rather than via `docker exec`: `/dev/dri` is
    passed through to the container as the same physical device, so the host and container see
    identical sysfs VRAM counters -- a host-side read avoids depending on a scratch server (or any
    container) being reachable at all, which matters for a pre-load baseline read.
    """
    with open(sysfs_path, "r", encoding="utf-8") as f:
        return int(f.read().strip())


def compute_vram_budget_bytes(
    vram_profile: str,
    vram_total_bytes_fn: Callable[[], int] = read_vram_total_bytes,
    vram_used_bytes_fn: Optional[Callable[[], int]] = None,
) -> float:
    """Resolves `vram_profile` ('desktop-shared'|'headless') to an admissible cell-footprint
    budget in bytes, audit B4's formula: `min(profile_cap, mem_info_vram_total -
    measured_used_at_queue_start - pinned_margin)`.

    `desktop-shared`'s cap is the pre-existing static REALISTIC_FREE_VRAM_GIB figure -- this
    branch touches NEITHER reader function at all (no live I/O for the profile this repo's
    existing dry-run/classify_fit callers have always used, so nothing about their behavior
    changes by this feature existing).

    `headless` is live: reads `vram_used_bytes_fn()` first and REFUSES
    (`VRAMProfileRefused`) if it exceeds `HEADLESS_BASELINE_CEILING_GIB`; otherwise returns
    `vram_total_bytes_fn() - used - HEADLESS_PINNED_MARGIN_GIB*GIB` (unbounded above by any
    static cap -- the live measurement IS the whole story for this profile).

    Both VRAM readers are injectable; the module-level `read_vram_total_bytes`/
    `read_vram_used_bytes` are the real, live-capable defaults -- this WP's own tests always pass
    fakes and never exercise the real sysfs reads (same "real controller, fake in tests" pattern
    as `ContainerController` in `batch.py`).
    """
    if vram_used_bytes_fn is None:
        vram_used_bytes_fn = read_vram_used_bytes
    if vram_profile == "desktop-shared":
        return REALISTIC_FREE_VRAM_GIB * GIB
    if vram_profile == "headless":
        used = vram_used_bytes_fn()
        if used > HEADLESS_BASELINE_CEILING_GIB * GIB:
            raise VRAMProfileRefused(
                f"headless VRAM profile refused: measured baseline VRAM-used "
                f"({used / GIB:.2f} GiB) exceeds the pinned ceiling "
                f"({HEADLESS_BASELINE_CEILING_GIB} GiB) -- this looks like a live desktop "
                f"session sharing the GPU, not a headless box; choose desktop-shared instead"
            )
        total = vram_total_bytes_fn()
        return total - used - HEADLESS_PINNED_MARGIN_GIB * GIB
    raise ValueError(f"unknown vram_profile {vram_profile!r}; expected one of {VRAM_PROFILES}")


# -------------------------------------------------------------------------------------------
# Minimal GGUF header/metadata reader -- stdlib `struct` only, no `gguf`/`numpy` dependency.
# Layout per the GGUF v3 spec: magic(4) + version(u32) + tensor_count(u64) + metadata_kv_count
# (u64), then metadata_kv_count key/value pairs. Metadata always precedes the per-tensor info
# array, so this never needs to read anywhere near the (often multi-GB) tensor-data section --
# only the first few KB to a few hundred KB of the file, however many metadata keys the model
# carries.
# -------------------------------------------------------------------------------------------


class GGUFParseError(Exception):
    """Raised when a file doesn't parse as a GGUF header/metadata block."""


_GGUF_TYPE_UINT8 = 0
_GGUF_TYPE_INT8 = 1
_GGUF_TYPE_UINT16 = 2
_GGUF_TYPE_INT16 = 3
_GGUF_TYPE_UINT32 = 4
_GGUF_TYPE_INT32 = 5
_GGUF_TYPE_FLOAT32 = 6
_GGUF_TYPE_BOOL = 7
_GGUF_TYPE_STRING = 8
_GGUF_TYPE_ARRAY = 9
_GGUF_TYPE_UINT64 = 10
_GGUF_TYPE_INT64 = 11
_GGUF_TYPE_FLOAT64 = 12

# (struct format char, byte size) for every fixed-size scalar type. STRING/ARRAY are handled
# separately (variable length).
_GGUF_SCALAR_STRUCT: Dict[int, Tuple[str, int]] = {
    _GGUF_TYPE_UINT8: ("B", 1),
    _GGUF_TYPE_INT8: ("b", 1),
    _GGUF_TYPE_UINT16: ("H", 2),
    _GGUF_TYPE_INT16: ("h", 2),
    _GGUF_TYPE_UINT32: ("I", 4),
    _GGUF_TYPE_INT32: ("i", 4),
    _GGUF_TYPE_FLOAT32: ("f", 4),
    _GGUF_TYPE_BOOL: ("?", 1),
    _GGUF_TYPE_UINT64: ("Q", 8),
    _GGUF_TYPE_INT64: ("q", 8),
    _GGUF_TYPE_FLOAT64: ("d", 8),
}


def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise GGUFParseError(f"unexpected EOF reading {n} bytes")
    return data


def _read_gguf_string(f) -> str:
    (length,) = struct.unpack("<Q", _read_exact(f, 8))
    return _read_exact(f, length).decode("utf-8", errors="replace")


def _read_gguf_value(f, value_type: int) -> Any:
    if value_type == _GGUF_TYPE_STRING:
        return _read_gguf_string(f)
    if value_type == _GGUF_TYPE_ARRAY:
        (elem_type,) = struct.unpack("<I", _read_exact(f, 4))
        (count,) = struct.unpack("<Q", _read_exact(f, 8))
        return [_read_gguf_value(f, elem_type) for _ in range(count)]
    fmt_size = _GGUF_SCALAR_STRUCT.get(value_type)
    if fmt_size is None:
        raise GGUFParseError(f"unknown GGUF metadata value type: {value_type}")
    fmt, size = fmt_size
    (val,) = struct.unpack("<" + fmt, _read_exact(f, size))
    return val


def read_gguf_metadata(gguf_path: Path) -> Dict[str, Any]:
    """Parses a GGUF file's magic/version/counts header plus its metadata key/value block only --
    never the tensor-info array or the tensor data itself. Raises `GGUFParseError` on a bad magic
    or a truncated/malformed metadata block; raises `OSError` (uncaught) if the path can't be
    opened, same as any other file read in this module.
    """
    with open(gguf_path, "rb") as f:
        magic = _read_exact(f, 4)
        if magic != b"GGUF":
            raise GGUFParseError(f"{gguf_path}: not a GGUF file (bad magic {magic!r})")
        struct.unpack("<I", _read_exact(f, 4))  # version -- not currently needed by callers
        struct.unpack("<Q", _read_exact(f, 8))  # tensor_count -- ditto
        (kv_count,) = struct.unpack("<Q", _read_exact(f, 8))
        metadata: Dict[str, Any] = {}
        for _ in range(kv_count):
            key = _read_gguf_string(f)
            (value_type,) = struct.unpack("<I", _read_exact(f, 4))
            metadata[key] = _read_gguf_value(f, value_type)
    return metadata


def gguf_attention_geometry(metadata: Dict[str, Any]) -> Dict[str, Optional[int]]:
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
KV_TYPE_BYTES_PER_ELEMENT: Dict[str, float] = {
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


def kv_bytes_per_token(
    geometry: Dict[str, Optional[int]],
    ctk: str,
    ctv: str,
    kv_attention_layers: Optional[int] = None,
    context: Optional[int] = None,
    swa_layer_spec: Optional[List[bool]] = None,
    kv_indexer_geometry: Optional[Dict[str, Any]] = None,
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
    is byte-identical to pre-C4-B1 behavior.
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
    per_token_both = key_length * bpe_k + value_length * bpe_v
    if per_layer_kv:
        spec = swa_layer_spec if swa_layer_spec is not None else geometry.get("sliding_window_pattern")
        window = geometry.get("sliding_window")
        if (
            context is not None
            and window is not None
            and spec is not None
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
        main_rate = layers * head_count_kv * per_token_both

    if kv_indexer_geometry is None:
        return main_rate
    if ctk != "f16" or ctv != "f16":
        raise ValueError(
            f"kv_indexer_geometry given but the indexer cache's storage type at ctk={ctk!r}/"
            f"ctv={ctv!r} is unmeasured (C4-B1 scopes the indexer term to f16/f16 only) -- "
            f"measure the indexer's storage type at this KV type before classifying this cell"
        )
    indexer_rate = (
        kv_indexer_geometry["layers"]
        * (kv_indexer_geometry["key_length"] + kv_indexer_geometry["value_length"])
        * KV_TYPE_BYTES_PER_ELEMENT["f16"]
    )
    return main_rate + indexer_rate


# -------------------------------------------------------------------------------------------
# Fit classification -- resolves a normalized cell dict's weights + attention geometry (real
# GGUF header or `synthetic_geometry` fixture) and scores it against the VRAM budget.
# -------------------------------------------------------------------------------------------


def _resolve_geometry_and_weights(cell: Dict[str, Any]) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[str]]:
    """Wraps `_resolve_geometry_and_weights_raw` with placement-awareness (audit B2): when the
    cell carries a `gpu_weights_bytes` override, it REPLACES the raw file-size/synthetic
    `weights_bytes` figure -- a cell running under `-ncmoe` (or any other partial-offload flag)
    does not have the whole GGUF resident on the GPU, and counting the whole file overestimates
    VRAM pressure by exactly the CPU-resident fraction. `qwen36-a3b`'s config B (`-ngl 99 -ncmoe
    32`, `llama/config.yaml` offload-placement comments ~721-745): 2.494 GiB non-expert weights
    (always GPU-resident regardless of `-ncmoe`) + 8 of 40 layers' routed-expert weights
    GPU-resident, vs. the full on-disk GGUF (~20.8 GiB) -- using the file size there would
    misclassify every cell of that model `excluded`. Never applied when the cell carries no such
    override (the pre-existing whole-file/whole-synthetic-geometry behavior is unchanged).
    """
    weights_bytes, geometry, reason = _resolve_geometry_and_weights_raw(cell)
    override = cell.get("gpu_weights_bytes")
    if weights_bytes is not None and override is not None:
        weights_bytes = override
    return weights_bytes, geometry, reason


def _resolve_geometry_and_weights_raw(cell: Dict[str, Any]) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[str]]:
    """Returns `(weights_bytes, geometry, reason_unknown)`. Prefers a real host GGUF
    (`model_host_path`) when present; falls back to `synthetic_geometry` (fixture/testing
    override -- never used when a real host path is given, so a real cell can never silently run
    on stale/fabricated numbers). Returns `(None, None, reason)` if neither is available -- the
    caller classifies that cell `"unknown"`, not a guessed fit. `weights_bytes` here is always the
    whole-file/whole-synthetic-geometry figure -- `_resolve_geometry_and_weights` (the wrapper
    every other function in this module calls) is where the `gpu_weights_bytes` placement
    override, if any, gets applied.
    """
    host_path = cell.get("model_host_path")
    if host_path:
        path = Path(host_path)
        if not path.exists():
            return None, None, f"model_host_path does not exist: {host_path}"
        weights_bytes = path.stat().st_size
        try:
            metadata = read_gguf_metadata(path)
            geometry = gguf_attention_geometry(metadata)
        except (GGUFParseError, OSError) as exc:
            return None, None, f"GGUF header parse failed for {host_path}: {exc}"
        return weights_bytes, geometry, None
    synthetic = cell.get("synthetic_geometry")
    if synthetic:
        required = (
            "weights_bytes", "block_count", "head_count_kv", "key_length", "value_length",
        )
        missing = [k for k in required if k not in synthetic]
        if missing:
            return None, None, f"synthetic_geometry missing key(s): {missing}"
        weights_bytes = synthetic["weights_bytes"]
        geometry = {
            "architecture": synthetic.get("architecture", "synthetic"),
            "block_count": synthetic["block_count"],
            "head_count": synthetic.get("head_count"),
            "head_count_kv": synthetic["head_count_kv"],
            "key_length": synthetic["key_length"],
            "value_length": synthetic["value_length"],
            "context_length": synthetic.get("context_length"),
            # SWA-awareness (audit B2): a fixture can shape a synthetic geometry as SWA-capable
            # too, same two keys as the real-header path (`gguf_attention_geometry`) -- absent by
            # default (`None`), which is exactly the "no per-layer SWA signal" case
            # `kv_bytes_per_token` falls back conservatively from.
            "sliding_window": synthetic.get("sliding_window"),
            "sliding_window_pattern": synthetic.get("sliding_window_pattern"),
        }
        return weights_bytes, geometry, None
    return None, None, "cell has neither model_host_path nor synthetic_geometry"


def classify_fit(
    cell: Dict[str, Any],
    vram_profile: str = "desktop-shared",
    vram_total_bytes_fn: Callable[[], int] = read_vram_total_bytes,
    vram_used_bytes_fn: Optional[Callable[[], int]] = None,
) -> Dict[str, Any]:
    """Pure-math VRAM-fit predictor. Returns a dict always carrying `classification` (one of
    `"fits"`, `"marginal"`, `"excluded"`, `"unknown"`) plus the numbers behind it -- never raises
    on missing geometry (that's the `"unknown"` case, with `reason` explaining why), since a
    dry-run over a whole spec should report every cell, not abort on the first one it can't
    estimate. **Except** `VRAMProfileRefused` (a mischosen `vram_profile='headless'` on a live
    desktop) -- that's a loud, unrecoverable-for-this-run refusal, not a per-cell "unknown".

    `vram_profile` (audit B4, default `"desktop-shared"` -- the pre-existing, always-static
    behavior, unchanged for every caller that doesn't pass this) selects the admissible-footprint
    budget via `compute_vram_budget_bytes()`; both VRAM reader functions are threaded straight
    through to it (see that function's docstring -- `desktop-shared` never invokes either).
    SWA-awareness (kv_bytes_per_token's `context`/`swa_layer_spec`) is applied automatically here
    by always passing this cell's own `-c` as `context` and `cell.get("swa_layer_spec")` as the
    override.
    """
    weights_bytes, geometry, reason = _resolve_geometry_and_weights(cell)
    if weights_bytes is None or geometry is None:
        return {"classification": "unknown", "reason": reason}

    flags = cell["server_flags"]
    context = flags["-c"]
    ctk = flags.get("-ctk", "f16")
    ctv = flags.get("-ctv", "f16")
    try:
        bytes_per_token = kv_bytes_per_token(
            geometry, ctk, ctv,
            kv_attention_layers=cell.get("kv_attention_layers"),
            context=context,
            swa_layer_spec=cell.get("swa_layer_spec"),
            kv_indexer_geometry=cell.get("kv_indexer_geometry"),
        )
    except (GGUFParseError, ValueError) as exc:
        return {"classification": "unknown", "reason": str(exc)}

    kv_bytes = bytes_per_token * context
    overhead_bytes = NON_WEIGHT_OVERHEAD_GIB * GIB
    estimated_total_bytes = weights_bytes + kv_bytes + overhead_bytes
    budget_bytes = compute_vram_budget_bytes(vram_profile, vram_total_bytes_fn, vram_used_bytes_fn)
    predicted_margin_bytes = budget_bytes - estimated_total_bytes

    if predicted_margin_bytes >= FIT_MARGIN_GIB * GIB:
        classification = "fits"
    elif predicted_margin_bytes >= MARGINAL_MARGIN_GIB * GIB:
        classification = "marginal"
    else:
        # Below MARGINAL_MARGIN_GIB is "excluded" even when the cell would technically fit
        # (margin >= 0): the repo's own accept/reject record treats sub-1.5 GiB margins as
        # rejections (docs/qwen38-27b-bakeoff-results.md:59-61 -- 0.32 GiB and 1.34 GiB margins
        # both REJECTED), and this pre-filter's contract is that band, not bare fit.
        classification = "excluded"

    return {
        "classification": classification,
        "reason": None,
        "vram_profile": vram_profile,
        "weights_bytes": weights_bytes,
        "kv_bytes_per_token": bytes_per_token,
        "kv_bytes_at_context": kv_bytes,
        "non_weight_overhead_bytes": overhead_bytes,
        "estimated_total_bytes": estimated_total_bytes,
        "vram_budget_bytes": budget_bytes,
        "predicted_margin_bytes": predicted_margin_bytes,
        "predicted_margin_gib": predicted_margin_bytes / GIB,
        "architecture": geometry.get("architecture"),
    }
