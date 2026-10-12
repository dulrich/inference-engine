#!/usr/bin/env python3
"""The GGUF binary format: header/metadata parsing, the tensor-info directory, ggml block sizes,
and split-shard discovery (WP-R3 of
`plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding F5).

Extracted verbatim from `gguf_fit.py`, which had grown to 1,152 lines by carrying four separable
domains in one file: this binary-format layer, tensor placement (`gguf_placement.py`),
attention/KV-cache geometry (`gguf_cache.py`), and the VRAM-budget/fit-policy owner that keeps
the `gguf_fit.py` name. Every symbol here keeps its original name, signature, and behavior, and
`gguf_fit` re-exports the public ones, so `gguf_fit.read_gguf_metadata` /
`gguf_fit.GGML_TYPE_SIZES` / `gguf_fit.TensorInfo` / `sweep.read_gguf_metadata` / ... all still
resolve for every consumer and every `mock.patch.object` target.

This is the BASE module of the `gguf_*` family: it imports no sibling, so the others can import
it without closing a cycle. `GIB` lives here for exactly that reason -- it is the one byte-unit
constant `gguf_placement` and `gguf_fit` both need, and defining it upward would make
`gguf_placement` import its own importer.

**This module is pure**, and its only I/O is reading a GGUF file's header/metadata/tensor-info
prefix off disk -- never a single byte of the (often tens-of-GiB) tensor DATA section, no
`docker`, no network, no clock, no subprocess. Zero new runtime dependencies: Python 3 stdlib
only. The GGUF reader is deliberately hand-rolled on `struct` -- no `gguf`, no `numpy`.

Failure direction throughout: an unrecognised ggml type id, a bad magic, a truncated file, or a
shard the split-naming convention cannot derive FAILS (`GGUFParseError`) or returns `None` -- it
never silently defaults to a guessed number.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any, NamedTuple

GIB = 1024 ** 3


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
_GGUF_SCALAR_STRUCT: dict[int, tuple[str, int]] = {
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


def read_gguf_metadata(gguf_path: Path) -> dict[str, Any]:
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
        metadata: dict[str, Any] = {}
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
GGML_TYPE_SIZES: dict[int, tuple[int, int]] = {
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
    dims: tuple[int, ...]
    ggml_type: int
    nbytes: int


# Split-shard filename convention this repo's own `docs/qwen4exp-ple-ondisk-spike.md` and every
# committed qwen4exp queue already assumes: `<prefix>-NNNNN-of-MMMMM.gguf`, 5-digit zero-padded,
# 1-indexed.
_SPLIT_SHARD_RE = re.compile(r"^(?P<prefix>.*)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$")


def _sibling_shard_paths(gguf_path: Path, count: int) -> list[Path] | None:
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


def _read_gguf_tensor_infos_one_shard(gguf_path: Path) -> tuple[dict[str, Any], list[TensorInfo]]:
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
        metadata: dict[str, Any] = {}
        for _ in range(kv_count):
            key = _read_gguf_string(f)
            (value_type,) = struct.unpack("<I", _read_exact(f, 4))
            metadata[key] = _read_gguf_value(f, value_type)
        tensor_infos: list[TensorInfo] = []
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


def read_gguf_tensor_infos(gguf_path: Path) -> list[TensorInfo] | None:
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
    all_infos: list[TensorInfo] = []
    for shard_path in shard_paths:
        if not shard_path.exists():
            return None
        try:
            _, infos = _read_gguf_tensor_infos_one_shard(shard_path)
        except (GGUFParseError, OSError):
            return None
        all_infos.extend(infos)
    return all_infos
