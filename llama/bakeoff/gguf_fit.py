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

import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Tuple, Union

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


# -------------------------------------------------------------------------------------------
# Tensor-info section reader (WP-4, `plans/moe-offload-load-mode-and-qwen4exp-fit-math.md`):
# walks the per-tensor directory that immediately follows the KV block `read_gguf_metadata`
# above already parses -- name (gguf string), n_dims (u32), dims (u64 x n_dims), ggml_type
# (u32), offset (u64) -- and, for a split model, every sibling shard named by `split.count`.
# Still stdlib `struct` only; still never reads a single byte of actual tensor DATA (only the
# small header/metadata/tensor-info prefix of each shard file).
# -------------------------------------------------------------------------------------------

# ggml per-block byte layout. Pinned against this HOST's own llama.cpp checkout
# (`<code-root>/apps/llama.cpp`), not copied from any prior script: type ids from
# `ggml/include/ggml.h`'s `enum ggml_type` (GGML_TYPE_F32=0 .. GGML_TYPE_BF16=30, confirmed by
# direct `grep` against that file this session); block byte sizes independently re-derived from
# `ggml/src/ggml-common.h`'s `block_q*`/`block_iq*` struct `static_assert(...)` lines, using
# `ggml_half` = `uint16_t` (2 B, `ggml-common.h:6`), `K_SCALE_SIZE` = 12 (`ggml-common.h:90`),
# `IQ3S_N_SCALE` = `QK_K/64` = 4 (`ggml-common.h:404`), `QK_K` = 256 (`ggml-common.h:89`).
#
# Every entry below was checked against a `static_assert` line, not assumed. The two entries the
# WP-4 brief flagged as needing re-verification -- IQ3_XXS and IQ2_S -- both came out matching the
# plan's rough session-script table (98 and 82 respectively). The entry that was ACTUALLY wrong in
# that rough table is IQ4_NL (type id 20): its true block size is 18 bytes/32 elements
# (`sizeof(ggml_half) + QK4_NL/2` = 2 + 16), byte-IDENTICAL to Q4_0 -- not the 20-byte Q4_1 layout
# the session script used for id 20. This table therefore reproduces
# `docs/qwen4exp-ple-ondisk-spike.md`'s 26.82 GiB PLE-table / 42.92 GiB routed-expert figures, not
# the plan's own Context-section "session" figures of 29.84/45.34 GiB (see
# `tests/test_gguf_tensor_classes.py` and the WP-4 report for the reconciliation).
GGML_TYPE_SIZES: Dict[int, Tuple[int, int]] = {
    0: (1, 4),  # F32
    1: (1, 2),  # F16
    2: (32, 18),  # Q4_0
    3: (32, 20),  # Q4_1
    6: (32, 22),  # Q5_0
    7: (32, 24),  # Q5_1
    8: (32, 34),  # Q8_0
    9: (32, 36),  # Q8_1
    10: (256, 84),  # Q2_K
    11: (256, 110),  # Q3_K
    12: (256, 144),  # Q4_K
    13: (256, 176),  # Q5_K
    14: (256, 210),  # Q6_K
    15: (256, 292),  # Q8_K
    16: (256, 66),  # IQ2_XXS
    17: (256, 74),  # IQ2_XS
    18: (256, 98),  # IQ3_XXS
    19: (256, 50),  # IQ1_S
    20: (32, 18),  # IQ4_NL
    21: (256, 110),  # IQ3_S
    22: (256, 82),  # IQ2_S
    23: (256, 136),  # IQ4_XS
    24: (1, 1),  # I8
    25: (1, 2),  # I16
    26: (1, 4),  # I32
    27: (1, 8),  # I64
    28: (1, 8),  # F64
    29: (256, 56),  # IQ1_M
    30: (1, 2),  # BF16
}


class TensorInfo(NamedTuple):
    """One tensor-info directory entry. `nbytes` is computed once, here, at parse time from
    `GGML_TYPE_SIZES` -- `prod(dims) // block_elems * block_bytes` -- so every downstream
    consumer (`classify_tensor`, `derive_placement_bytes`) works with plain byte counts, never
    raw ggml type ids."""

    name: str
    dims: Tuple[int, ...]
    ggml_type: int
    nbytes: int


# Split-shard filename convention this repo's own `docs/qwen4exp-ple-ondisk-spike.md` and every
# committed qwen4exp queue already assumes: `<prefix>-NNNNN-of-MMMMM.gguf`, 5-digit zero-padded,
# 1-indexed.
_SPLIT_SHARD_RE = re.compile(r"^(?P<prefix>.*)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$")


def _sibling_shard_paths(gguf_path: Path, count: int) -> Optional[List[Path]]:
    """Derives every shard path for a `split.count`-carrying GGUF from ONE shard's own path --
    never from `split.no` (a shard's OWN index is not needed to name its siblings). Returns
    `None` -- never a guess -- if `gguf_path`'s name doesn't match the split-shard convention at
    all, or if the filename's own `-of-NNNNN` disagrees with the metadata's `split.count`."""
    m = _SPLIT_SHARD_RE.match(gguf_path.name)
    if not m:
        return None
    total_str = m.group("total")
    if int(total_str) != count:
        return None
    prefix = m.group("prefix")
    return [gguf_path.parent / f"{prefix}-{i:05d}-of-{total_str}.gguf" for i in range(1, count + 1)]


def _read_gguf_tensor_infos_one_shard(gguf_path: Path) -> Tuple[Dict[str, Any], List[TensorInfo]]:
    """Parses ONE GGUF file's KV metadata block AND its tensor-info array in a single sequential
    pass -- the tensor-info array immediately follows the KV block in the GGUF v3 layout
    `read_gguf_metadata` already documents. Raises `GGUFParseError` (bad magic, truncated file, or
    a ggml type id outside `GGML_TYPE_SIZES`) / `OSError` (path problems), exactly like
    `read_gguf_metadata`.

    Deliberately a SEPARATE helper from `read_gguf_metadata` rather than an extension of it: every
    existing caller of `read_gguf_metadata` wants the cheap KV-only read (a few KB to a few hundred
    KB) and must not pay for walking a tensor-info array that can run past a thousand entries for a
    model like qwen4exp.
    """
    with open(gguf_path, "rb") as f:
        magic = _read_exact(f, 4)
        if magic != b"GGUF":
            raise GGUFParseError(f"{gguf_path}: not a GGUF file (bad magic {magic!r})")
        struct.unpack("<I", _read_exact(f, 4))  # version -- not needed here either
        (tensor_count,) = struct.unpack("<Q", _read_exact(f, 8))
        (kv_count,) = struct.unpack("<Q", _read_exact(f, 8))
        metadata: Dict[str, Any] = {}
        for _ in range(kv_count):
            key = _read_gguf_string(f)
            (value_type,) = struct.unpack("<I", _read_exact(f, 4))
            metadata[key] = _read_gguf_value(f, value_type)
        tensor_infos: List[TensorInfo] = []
        for _ in range(tensor_count):
            name = _read_gguf_string(f)
            (n_dims,) = struct.unpack("<I", _read_exact(f, 4))
            dims = struct.unpack("<" + "Q" * n_dims, _read_exact(f, 8 * n_dims))
            (ggml_type,) = struct.unpack("<I", _read_exact(f, 4))
            (_offset,) = struct.unpack("<Q", _read_exact(f, 8))  # unused -- data is never read
            sizes = GGML_TYPE_SIZES.get(ggml_type)
            if sizes is None:
                raise GGUFParseError(
                    f"{gguf_path}: tensor {name!r} has unrecognised ggml type id {ggml_type}"
                )
            block_elems, block_bytes = sizes
            nelements = 1
            for d in dims:
                nelements *= d
            nbytes = (nelements // block_elems) * block_bytes
            tensor_infos.append(TensorInfo(name=name, dims=dims, ggml_type=ggml_type, nbytes=nbytes))
    return metadata, tensor_infos


def read_gguf_tensor_infos(gguf_path: Path) -> Optional[List[TensorInfo]]:
    """Returns every tensor's `(name, dims, ggml_type, nbytes)` for `gguf_path` -- and, when its
    own `split.count` metadata key is > 1, for every sibling shard too (all must exist; a missing
    shard fails the WHOLE read, never a partial one). `None` on any failure -- unknown ggml type,
    unparseable/truncated shard, a shard the split-naming convention can't derive, or a missing
    shard -- never a guess, matching this module's existing conservative direction
    (`gguf_fit.py:20-26`... see module docstring).

    Reads ALL shards named by `split.count` fresh (including `gguf_path` itself again if it is
    one of them) rather than trying to reuse the caller's own shard's already-parsed tensor list --
    simpler and just as cheap, since this never touches the (often tens-of-GiB) tensor DATA
    section of any shard, only its small header/metadata/tensor-info prefix.
    """
    try:
        metadata, tensor_infos = _read_gguf_tensor_infos_one_shard(gguf_path)
    except (GGUFParseError, OSError):
        return None
    split_count = metadata.get("split.count")
    if not isinstance(split_count, int) or isinstance(split_count, bool) or split_count <= 1:
        return tensor_infos
    shard_paths = _sibling_shard_paths(gguf_path, split_count)
    if shard_paths is None:
        return None
    all_infos: List[TensorInfo] = []
    for shard_path in shard_paths:
        if not shard_path.exists():
            return None
        try:
            _, infos = _read_gguf_tensor_infos_one_shard(shard_path)
        except (GGUFParseError, OSError):
            return None
        all_infos.extend(infos)
    return all_infos


# -------------------------------------------------------------------------------------------
# Tensor classification and placement derivation (WP-4). Classifies each tensor by NAME into one
# of five classes, then derives which class lands on GPU vs CPU vs "lazy" (cold, mmap-paged-in
# only on demand) from the normalized `-ngl`/`-ncmoe`/`-cmoe`/`-ot`/`-lm`/`--tensor-read-lazy`
# server flags. Both functions are pure and offline -- no I/O beyond what the caller already did
# to produce `tensor_infos`.
# -------------------------------------------------------------------------------------------

_ROUTED_EXPERT_RE = re.compile(r"^blk\.\d+\.ffn_(?:gate|up|down|gate_up)_exps\.weight$")
_PLE_BLOCK_RE = re.compile(r"^blk\.\d+\.ple_")
_BLK_INDEX_RE = re.compile(r"^blk\.(\d+)\.")


def classify_tensor(name: str) -> str:
    """Classifies one tensor by name into `"routed_expert"` (per-expert-index-selected FFN
    weights -- the tensors `-ncmoe`/`-cmoe` move), `"shared_expert"` (the always-active FFN that
    runs alongside the routed ones on every token -- name contains `shexp`), `"ple"` (the
    per-layer-embedding n-gram table and its small per-layer helper tensors), `"embd_output"`
    (token embedding / LM head / their norms), or `"dense"` (everything else -- attention, SSM,
    hyper-connection, and any other per-layer tensor with no special placement significance).
    The routed-expert form covers both the three-tensor split (`ffn_gate_exps`/`ffn_up_exps`/
    `ffn_down_exps`) and the FUSED gate+up form `blk.N.ffn_gate_up_exps.weight` that Gemma 4 MoE
    (`general.architecture=gemma4`) emits -- 7.98 GiB of the 26B-A4B QAT file, which classed as
    `dense` and made every fit verdict for that model wrong in the unsafe direction until this
    clause landed (`plans/gemma4-26b-a4b-qat-mtp-ladder.md`). The sibling `.scale` tensors
    (`ffn_down_exps.scale`, `ffn_gate_inp.scale`) deliberately stay `dense`/GPU rather than
    riding along with the experts `-ncmoe` moves: 0.35 MB total on that file (measured), too
    small to be worth a second placement rule, and the router gate they belong to is GPU-side
    work regardless -- a decision, not an omission.
    Never raises; an unrecognised name is `"dense"` by construction (the conservative default --
    it stays wherever `-ngl`/`-ot` would otherwise put a plain layer tensor)."""
    if _ROUTED_EXPERT_RE.match(name):
        return "routed_expert"
    if "shexp" in name:
        return "shared_expert"
    if "per_layer_token_embd" in name or _PLE_BLOCK_RE.match(name):
        return "ple"
    if name.startswith("token_embd") or name.startswith("output"):
        return "embd_output"
    return "dense"


class PlacementBytes(NamedTuple):
    gpu_bytes: int
    cpu_bytes: int
    lazy_bytes: int


def _parse_ngl_full_offload(ngl: Any, block_count: Optional[int]) -> Optional[bool]:
    """`True` when `-ngl`'s value means "every layer's weights start out GPU-resident" (this
    repo's own `99` convention, the literal string `"all"`, or an explicit int >=
    `block_count`); `False` for a genuine partial-layer offload (a smaller explicit int) --
    out of scope for this derivation, never guessed at. `None` when it can't be determined at all
    (missing/unparseable `-ngl`, or an explicit int that needs `block_count` to judge and
    `block_count` is unknown)."""
    if ngl is None or isinstance(ngl, bool):
        return None
    if isinstance(ngl, str):
        if ngl == "all":
            return True
        try:
            ngl = int(ngl)
        except ValueError:
            return None
    if not isinstance(ngl, int):
        return None
    if ngl == 99:
        return True
    if block_count is None:
        return None
    return ngl >= block_count


def _ot_cpu_patterns(value: Any) -> Optional[List[str]]:
    """Parses an `-ot` flag value -- a single string (optionally comma-joining several
    `<pattern>=<target>` entries) or a JSON list of such strings -- into the list of regex
    patterns that target `=CPU`. `[]` if the flag is absent/empty (a no-op, not an error). `None`
    -- never a guess -- if ANY entry targets a buffer other than `CPU` or fails to compile as a
    Python regex; a `-ot` this function can't fully account for must not silently derive a number
    from only the entries it understood."""
    if not value:
        return []
    entries = value if isinstance(value, list) else str(value).split(",")
    patterns: List[str] = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            return None
        pattern, target = entry.rsplit("=", 1)
        if target != "CPU":
            return None
        try:
            re.compile(pattern)
        except re.error:
            return None
        patterns.append(pattern)
    return patterns


# `-lm` values under which mmap is actually in force (the loader's own `--load-mode` enum,
# `absent` meaning the default `auto`, which resolves to mmap for every model this repo serves).
_MMAP_IN_FORCE_LM_VALUES = (None, "auto", "mmap", "mmap+mlock")

# Lazy-load threshold: `--tensor-read-lazy auto` (or its absence, same default) is lazy for a
# tensor only above this size -- `docs/qwen4exp-ple-ondisk-spike.md` / this plan's Context section
# both cite the loader's own 4 GiB-and-up rule.
_LAZY_AUTO_THRESHOLD_BYTES = 4 * GIB

# `token_embd.weight` (the INPUT token-embedding lookup table -- never `output.weight`/
# `output_norm.weight`, the output/LM-head side, which stay GPU as normal) is excluded from the
# GPU-resident figure under full `-ngl` offload, independent of `-ncmoe`/`-cmoe`/`-ot`. This is
# an empirical finding of this WP, not something stated by name in any `-ot`/`-ngl` flag
# semantics: reconciling ALL FOUR of the brief's live/hand-carried pins required it, and only it
# -- `derive_placement_bytes` without this exception overshoots every one of the four pins (+9.9%
# qwen35moe `-ncmoe 32`; +12.4% qwen4exp `-cmoe`; +3.3% qwen4exp `-ncmoe 38`), while WITH it every
# one lands within 0.0001% of its target (qwen35moe `-ncmoe 32`: -0.00005% vs. the live 6421.16
# MiB `Vulkan0 model buffer size`, +1.71% vs. the hand-carried 6,620,000,000 B; qwen4exp `-cmoe`:
# -0.0000009%; qwen4exp `-ncmoe 38`: -0.0000002% -- both against `docs/qwen4exp-ple-ondisk-spike.
# md`'s measured `gpu_weights_bytes`). Consistent across two unrelated architectures (qwen35moe,
# qwen4exp) and both qwen4exp arms -- not a coincidence of one measurement. See the WP-4 report
# for the full reconciliation arithmetic; this is exactly the kind of thing `classify_fit`'s own
# module docstring already warns about -- an ESTIMATE, calibrated against real measurements
# already on record in this repo, never a substitute for a live load-and-measure.
_TOKEN_EMBD_TENSOR_NAME = "token_embd.weight"


def derive_placement_bytes(
    tensor_infos: List[TensorInfo],
    server_flags: Dict[str, Any],
    metadata: Dict[str, Any],
) -> Optional[PlacementBytes]:
    """Derives GPU-resident / CPU-resident-pinned / CPU-resident-lazy weight byte totals from a
    model's tensor directory and its normalized `server_flags`. `metadata` here is the
    `gguf_attention_geometry`-SHAPED dict (top-level `architecture`/`block_count` keys) that
    `_resolve_geometry_and_weights_raw` already computed for this cell -- not the raw namespaced
    GGUF KV dict `read_gguf_metadata` returns -- so this function never re-derives the arch prefix
    itself.

    Returns `None` -- never a guess, matching this module's existing conservative direction -- for
    anything it can't fully account for: `-ngl` missing/unparseable/a genuine partial-layer
    offload (`_parse_ngl_full_offload` above), an `-ot` targeting a non-CPU buffer or a pattern
    that fails to compile (`_ot_cpu_patterns` above), or a cell with none of `-ncmoe`/`-cmoe`/a
    usable `-ot` actually present (the caller's activation gate should already have filtered this
    out, but this function re-checks so it is never called in isolation and silently returns a
    number from an -ngl-only cell).

    Placement rules, applied per tensor (a tensor may be moved to CPU by more than one rule --
    union, not precedence): `-ncmoe N` moves `routed_expert` tensors of `blk.i` with `i < N`;
    `-cmoe` moves EVERY `routed_expert` tensor; each `-ot` pattern moves any tensor whose name
    `re.search`-matches it, regardless of class. Everything not moved is GPU-resident (the
    `-ngl`-full-offload premise this function requires).

    A CPU-resident tensor counts as `lazy_bytes` (excluded from `cpu_bytes`) only when mmap is in
    force (`-lm` absent/`auto`/`mmap`/`mmap+mlock` -- `"none"` defeats mmap and makes EVERY
    CPU-resident tensor pinned, never lazy) AND EITHER `--tensor-read-lazy` is `"on"`, OR it is
    absent/`"auto"` and the tensor exceeds `_LAZY_AUTO_THRESHOLD_BYTES` -- audit E1's pin. The
    small `blk.*.ple_*` per-layer helper tensors (`_PLE_BLOCK_RE`) are NEVER lazy regardless of
    size or policy (also audit E1) -- unlike the (potentially huge) `per_layer_token_embd.weight`
    table itself, which IS eligible.

    One more tensor is ALWAYS CPU-resident regardless of any flag: `token_embd.weight` (see
    `_TOKEN_EMBD_TENSOR_NAME` above for the empirical grounding) -- it still goes through the same
    lazy-vs-pinned check as any other CPU tensor above, it just never has a chance to be GPU.
    """
    block_count = metadata.get("block_count")
    full_offload = _parse_ngl_full_offload(server_flags.get("-ngl"), block_count)
    if full_offload is not True:
        return None

    ncmoe = server_flags.get("-ncmoe")
    ncmoe_n = ncmoe if isinstance(ncmoe, int) and not isinstance(ncmoe, bool) else None
    cmoe_all = server_flags.get("-cmoe") is True
    ot_patterns = _ot_cpu_patterns(server_flags.get("-ot"))
    if ot_patterns is None:
        return None
    if ncmoe_n is None and not cmoe_all and not ot_patterns:
        return None
    compiled_ot = [re.compile(p) for p in ot_patterns]

    mmap_in_force = server_flags.get("-lm") in _MMAP_IN_FORCE_LM_VALUES
    tensor_read_lazy = server_flags.get("--tensor-read-lazy")

    gpu_bytes = 0
    cpu_bytes = 0
    lazy_bytes = 0
    for tensor in tensor_infos:
        category = classify_tensor(tensor.name)
        is_cpu = tensor.name == _TOKEN_EMBD_TENSOR_NAME
        if not is_cpu and category == "routed_expert":
            if cmoe_all:
                is_cpu = True
            elif ncmoe_n is not None:
                m = _BLK_INDEX_RE.match(tensor.name)
                if m is not None and int(m.group(1)) < ncmoe_n:
                    is_cpu = True
        if not is_cpu:
            for pattern in compiled_ot:
                if pattern.search(tensor.name):
                    is_cpu = True
                    break
        if not is_cpu:
            gpu_bytes += tensor.nbytes
            continue
        is_lazy = False
        if mmap_in_force and not _PLE_BLOCK_RE.match(tensor.name):
            lazy_by_policy = tensor_read_lazy == "on"
            lazy_by_size_default = (
                tensor_read_lazy in (None, "auto") and tensor.nbytes > _LAZY_AUTO_THRESHOLD_BYTES
            )
            is_lazy = lazy_by_policy or lazy_by_size_default
        if is_lazy:
            lazy_bytes += tensor.nbytes
        else:
            cpu_bytes += tensor.nbytes
    return PlacementBytes(gpu_bytes=gpu_bytes, cpu_bytes=cpu_bytes, lazy_bytes=lazy_bytes)


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

# The exact key set a DSA-indexer geometry block carries, in emission order (WP-1,
# qwen4exp-bakeoff-campaign, C4-B1). Named once, here, because `IndexerGeometry` below is the
# ONLY thing in this repo allowed to spell them -- there is no second registry of these strings.
INDEXER_GEOMETRY_KEYS: Tuple[str, ...] = ("layers", "key_length", "value_length")


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
    def from_mapping(cls, raw: Any, *, prefix: str = "") -> "IndexerGeometry":
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
    def coerce(cls, raw: Union[Mapping[str, Any], "IndexerGeometry"]) -> "IndexerGeometry":
        """The thin compatibility adapter at the legacy plain-dict seam: `verify_run` and
        `classify_fit` still hand this module a mapping read straight off a record/cell dict."""
        return raw if isinstance(raw, cls) else cls.from_mapping(raw)

    def to_mapping(self) -> Dict[str, int]:
        """The one JSON emission form -- ordered `layers`/`key_length`/`value_length`, which is
        the order every committed record's `sweep_cell.kv_indexer_geometry` block already has."""
        return {
            "layers": self.layers,
            "key_length": self.key_length,
            "value_length": self.value_length,
        }


def kv_bytes_per_token(
    geometry: Dict[str, Optional[int]],
    ctk: str,
    ctv: str,
    kv_attention_layers: Optional[int] = None,
    context: Optional[int] = None,
    swa_layer_spec: Optional[List[bool]] = None,
    kv_indexer_geometry: Optional[Union[Mapping[str, Any], IndexerGeometry]] = None,
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


# -------------------------------------------------------------------------------------------
# Fit classification -- resolves a normalized cell dict's weights + attention geometry (real
# GGUF header or `synthetic_geometry` fixture) and scores it against the VRAM budget.
# -------------------------------------------------------------------------------------------


def _resolve_geometry_and_weights(
    cell: Dict[str, Any],
) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, int]]]:
    """Wraps `_resolve_geometry_and_weights_raw` with placement-awareness (audit B2, extended by
    WP-4): when the cell carries a `gpu_weights_bytes` override, it REPLACES the raw file-size/
    synthetic `weights_bytes` figure -- a cell running under `-ncmoe` (or any other partial-offload
    flag) does not have the whole GGUF resident on the GPU, and counting the whole file
    overestimates VRAM pressure by exactly the CPU-resident fraction. `qwen36-a3b`'s config B
    (`-ngl 99 -ncmoe 32`, `llama/config.yaml` offload-placement comments ~721-745): 2.494 GiB
    non-expert weights (always GPU-resident regardless of `-ncmoe`) + 8 of 40 layers' routed-
    expert weights GPU-resident, vs. the full on-disk GGUF (~20.8 GiB) -- using the file size
    there would misclassify every cell of that model `excluded`. The override always wins when
    present (unchanged from audit B2).

    WP-4 (Decisions locked 8): absent an override, the tensor-class derivation
    (`derive_placement_bytes`) is the fallback -- but ONLY when (a) a real host GGUF resolved
    (`model_host_path`, never `synthetic_geometry`) AND (b) the normalized `server_flags` carry a
    recognised CPU-placement flag (`-ncmoe`, `-cmoe`, or an `-ot` targeting `=CPU`) -- an all-GPU
    cell with no placement flag keeps today's whole-file `weights_bytes` byte-for-byte, because
    `test_fit_lock.py`'s golden digest hashes every numeric fit field and every committed cell
    either carries an override or carries no placement flag (verified this WP; see the WP-4
    report). If the tensor-info read or the derivation itself returns `None` for any reason
    (unknown ggml type, unparseable/missing shard, an unrecognised placement-flag combination,
    partial layer offload), behaviour is unchanged from before this WP.

    Returns a 4th element, `derived` -- `{"derived_cpu_weights_bytes": ..., "derived_lazy_bytes":
    ...}` when the derivation activated, `None` otherwise -- `classify_fit` merges it into the fit
    dict only in the activated case, so no other cell's dict gains new keys.
    """
    weights_bytes, geometry, reason = _resolve_geometry_and_weights_raw(cell)
    override = cell.get("gpu_weights_bytes")
    if weights_bytes is not None and override is not None:
        return override, geometry, reason, None
    derived: Optional[Dict[str, int]] = None
    if weights_bytes is not None and geometry is not None:
        flags = cell.get("server_flags", {})
        host_path = cell.get("model_host_path")
        if host_path and _cell_has_recognized_placement_flag(flags):
            tensor_infos = read_gguf_tensor_infos(Path(host_path))
            if tensor_infos is not None:
                placement = derive_placement_bytes(tensor_infos, flags, geometry)
                if placement is not None:
                    weights_bytes = placement.gpu_bytes
                    derived = {
                        "derived_cpu_weights_bytes": placement.cpu_bytes,
                        "derived_lazy_bytes": placement.lazy_bytes,
                    }
    return weights_bytes, geometry, reason, derived


def _cell_has_recognized_placement_flag(flags: Dict[str, Any]) -> bool:
    """The WP-4 activation gate (Decisions locked 8 / plan hard constraint): the derivation is
    ATTEMPTED only when the normalized `server_flags` explicitly carry one of these three keys --
    none of `-ncmoe`/`-cmoe`/`-ot` is a `_DEFAULT_SERVER_FLAGS` (`cell_spec.py`) entry, so presence
    here reflects exactly what the queue author wrote, never a default. Whether the value is
    actually usable (a real int `-ncmoe`, an `-ot` that targets `=CPU` and compiles, ...) is
    decided by `derive_placement_bytes` itself, which returns `None` -- leaving `weights_bytes`
    unchanged -- for anything it cannot parse. This function only gates the ATTEMPT."""
    if "-ncmoe" in flags and flags["-ncmoe"] not in (None, False):
        return True
    if flags.get("-cmoe") is True:
        return True
    return bool(flags.get("-ot"))


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
    weights_bytes, geometry, reason, derived = _resolve_geometry_and_weights(cell)
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

    result = {
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
    # WP-4: `derived_cpu_weights_bytes`/`derived_lazy_bytes` appear ONLY on a cell where the
    # tensor-class placement derivation activated (`_resolve_geometry_and_weights`'s 4th return
    # value) -- every other cell's dict, including every cell committed before this WP, is
    # byte-for-byte unchanged, which is what keeps `test_fit_lock.py`'s golden digest stable.
    if derived is not None:
        result.update(derived)
    return result
