#!/usr/bin/env python3
"""Declarative sweep framework (plans/quant-sweep-harness-and-instrument-v2.md, WP-1).

Turns "one model, one hand-typed `docker exec`, one improvised candidate ladder" into a
declarative spec: a JSON list of candidate cells (`{model_container_path, alias, server_flags,
battery}`), a VRAM-fit pre-filter computed from GGUF header geometry (never a substitute for
measurement -- see `classify_fit()`), and a scratch-server lifecycle (spawn inside
`inference-engine-llama`, verify from its own startup log, teardown, VRAM read-back) for driving
one cell end-to-end.

Zero new runtime dependencies: Python 3 stdlib only (`struct`, `subprocess`, `urllib`, `json`,
`hashlib`, `argparse`, `re`, `os`, `signal`, `time`) -- matches `run_harness.py`'s existing
stance. No `gguf`, no `numpy`, no `requests`.

**`--dry-run` never touches Docker.** Cell expansion, fit classification, and the exact
llama-server command each cell would run are all computable from the spec file and (optionally)
a real host GGUF path alone. The scratch-server lifecycle functions in this module (spawn, health
poll, teardown, VRAM read-back) are only invoked by the `run` subcommand, which this WP's own
dispatch brief forbids running live -- the orchestrator exercises it post-merge as the plan's
one live smoke cell.

Container facts baked in (verified live 2026-08-23, this WP's brief):
  - `inference-engine-llama` has no `python3`, `kill`, or `pkill` inside the image -- scratch
    servers are spawned via `docker exec ... sh -c 'nohup ... & echo $!'` (POSIX `sh`/`nohup` are
    both present, ordinary coreutils/dash) and torn down from the HOST side, by signalling the
    host-visible PID `docker top` reports (see `find_host_pid_by_port()` /
    `teardown_scratch_server()`) -- never by asking the container to kill its own child.
  - Only the router's 4806(container)->4811(host) port is published. A scratch server's port is
    container-internal only, reachable from the host via the container's bridge IP
    (`discover_bridge_ip()`), never via `127.0.0.1:<port>`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MODULE_DIR = Path(__file__).resolve().parent
LLAMA_DIR = MODULE_DIR.parent
REPO_ROOT = LLAMA_DIR.parent

# Container facts (brief, verified live 2026-08-23).
DEFAULT_CONTAINER_NAME = "inference-engine-llama"

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
    return {
        "architecture": arch,
        "block_count": block_count,
        "head_count": head_count,
        "head_count_kv": head_count_kv,
        "key_length": key_length,
        "value_length": value_length,
        "context_length": context_length,
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
) -> float:
    """KV-cache bytes/token = attention_layers * (head_count_kv*key_length*bpe(ctk) +
    head_count_kv*value_length*bpe(ctv)) -- llama.cpp's own per-layer K+V cache tensor sizing.

    `kv_attention_layers` overrides `geometry["block_count"]` for hybrid architectures (see
    `gguf_attention_geometry`'s docstring); defaults to the GGUF's raw `block_count` when absent.
    """
    layers = kv_attention_layers if kv_attention_layers is not None else geometry.get("block_count")
    head_count_kv = geometry.get("head_count_kv")
    key_length = geometry.get("key_length")
    value_length = geometry.get("value_length")
    if not all([layers, head_count_kv, key_length, value_length]):
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
    return layers * (head_count_kv * key_length * bpe_k + head_count_kv * value_length * bpe_v)


# -------------------------------------------------------------------------------------------
# Sweep-cell normalization, deterministic IDs, fit classification, command building.
# -------------------------------------------------------------------------------------------

VALID_BATTERY_ITEMS = ("vram", "timing", "needle", "quality")

_DEFAULT_SERVER_FLAGS = {"-ctk": "f16", "-ctv": "f16", "-fa": "on", "-ngl": 99}


def normalize_cell(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Fills in defaults, validates required keys, and returns a plain-dict cell -- the shared
    normal form every other function in this module (`compute_cell_id`, `classify_fit`,
    `build_command`) expects. Never mutates `raw`.
    """
    cell = dict(raw)
    missing = [k for k in ("alias", "model_container_path", "server_flags", "battery") if k not in cell]
    if missing:
        raise ValueError(f"sweep cell missing required key(s): {missing} -- cell was: {raw!r}")
    flags = dict(_DEFAULT_SERVER_FLAGS)
    flags.update(cell["server_flags"])
    if "-c" not in flags:
        raise ValueError(f"sweep cell {cell.get('alias')!r} server_flags missing required '-c'")
    cell["server_flags"] = flags
    bad_battery = [b for b in cell["battery"] if b not in VALID_BATTERY_ITEMS]
    if bad_battery:
        raise ValueError(
            f"sweep cell {cell.get('alias')!r} has unknown battery item(s) {bad_battery}; "
            f"expected a subset of {VALID_BATTERY_ITEMS}"
        )
    return cell


def compute_cell_id(cell: Dict[str, Any]) -> str:
    """Deterministic per-cell record ID -- a pure function of the cell's identity-bearing fields
    (never a timestamp or random value, so the same spec always reproduces the same IDs). Shape:
    `<alias>-ctk<ctk>-ctv<ctv>-fa<fa>-c<context>-<8-hex-digest>`, where the digest is a sha256 of
    the cell's canonical (sorted-key) JSON over `{model reference, alias, server_flags}` -- the
    human-readable prefix is for operators; the digest is what actually guarantees uniqueness
    across cells whose flags render to a colliding-looking prefix.
    """
    flags = cell["server_flags"]
    identity = {
        "alias": cell["alias"],
        "model_container_path": cell.get("model_container_path"),
        "model_host_path": cell.get("model_host_path"),
        "server_flags": flags,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:8]
    ctk = flags.get("-ctk", "f16")
    ctv = flags.get("-ctv", "f16")
    fa = flags.get("-fa", "on")
    c = flags.get("-c")
    return f"{cell['alias']}-ctk{ctk}-ctv{ctv}-fa{fa}-c{c}-{digest}"


def _resolve_geometry_and_weights(cell: Dict[str, Any]) -> Tuple[Optional[int], Optional[Dict[str, Any]], Optional[str]]:
    """Returns `(weights_bytes, geometry, reason_unknown)`. Prefers a real host GGUF
    (`model_host_path`) when present; falls back to `synthetic_geometry` (fixture/testing
    override -- never used when a real host path is given, so a real cell can never silently run
    on stale/fabricated numbers). Returns `(None, None, reason)` if neither is available -- the
    caller classifies that cell `"unknown"`, not a guessed fit.
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
        }
        return weights_bytes, geometry, None
    return None, None, "cell has neither model_host_path nor synthetic_geometry"


def classify_fit(cell: Dict[str, Any]) -> Dict[str, Any]:
    """Pure-math VRAM-fit predictor. Returns a dict always carrying `classification` (one of
    `"fits"`, `"marginal"`, `"excluded"`, `"unknown"`) plus the numbers behind it -- never raises
    on missing geometry (that's the `"unknown"` case, with `reason` explaining why), since a
    dry-run over a whole spec should report every cell, not abort on the first one it can't
    estimate.
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
            geometry, ctk, ctv, kv_attention_layers=cell.get("kv_attention_layers")
        )
    except (GGUFParseError, ValueError) as exc:
        return {"classification": "unknown", "reason": str(exc)}

    kv_bytes = bytes_per_token * context
    overhead_bytes = NON_WEIGHT_OVERHEAD_GIB * GIB
    estimated_total_bytes = weights_bytes + kv_bytes + overhead_bytes
    budget_bytes = REALISTIC_FREE_VRAM_GIB * GIB
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
        "weights_bytes": weights_bytes,
        "kv_bytes_per_token": bytes_per_token,
        "kv_bytes_at_context": kv_bytes,
        "non_weight_overhead_bytes": overhead_bytes,
        "estimated_total_bytes": estimated_total_bytes,
        "realistic_free_vram_bytes": budget_bytes,
        "predicted_margin_bytes": predicted_margin_bytes,
        "predicted_margin_gib": predicted_margin_bytes / GIB,
        "architecture": geometry.get("architecture"),
    }


_FLAG_ORDER = ["-m", "--port", "-c", "-ctk", "-ctv", "-fa", "-ngl"]


def build_command(cell: Dict[str, Any], port: int) -> List[str]:
    """Builds the exact `llama-server` argv this cell would run, in a fixed, deterministic flag
    order (`_FLAG_ORDER` first, any remaining `server_flags` keys after, sorted) -- two calls on
    the same cell+port always produce byte-identical output.
    """
    flags = dict(cell["server_flags"])
    flags["-m"] = cell["model_container_path"]
    flags["--port"] = port

    argv = ["llama-server"]
    seen = set()
    for key in _FLAG_ORDER:
        if key in flags:
            argv += [key, str(flags[key])]
            seen.add(key)
    for key in sorted(k for k in flags if k not in seen):
        argv += [key, str(flags[key])]
    return argv


# -------------------------------------------------------------------------------------------
# Ladder expansion -- one model x KV-type ladder x context ladder.
# -------------------------------------------------------------------------------------------


def expand_ladder(
    base_cell: Dict[str, Any],
    kv_type_ladder: List[Tuple[str, str]],
    context_ladder: List[int],
    battery: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Cross-products one base cell (model + alias-prefix + any shared flags) against a
    `(ctk, ctv)` ladder and a `-c` ladder, producing one normalized cell per combination. Each
    cell's alias gets a `-ctk<ctk>-ctv<ctv>-c<c>` suffix so the ladder's cells never collide on
    alias -- the base cell's own `alias` is used as the prefix. `battery`, if given, overrides
    the base cell's `battery` for every expanded cell (the base cell need not carry one).
    """
    out: List[Dict[str, Any]] = []
    for ctk, ctv in kv_type_ladder:
        for context in context_ladder:
            cell = dict(base_cell)
            flags = dict(cell.get("server_flags", {}))
            flags["-ctk"] = ctk
            flags["-ctv"] = ctv
            flags["-c"] = context
            cell["server_flags"] = flags
            cell["alias"] = f"{base_cell['alias']}-ctk{ctk}-ctv{ctv}-c{context}"
            if battery is not None:
                cell["battery"] = battery
            out.append(normalize_cell(cell))
    return out


# -------------------------------------------------------------------------------------------
# Spec loading.
# -------------------------------------------------------------------------------------------


def load_spec(spec_path: Path) -> List[Dict[str, Any]]:
    """Loads a sweep spec JSON file: `{"cells": [ {...}, ... ]}`. Every cell is passed through
    `normalize_cell()` before being returned, so callers never see a raw/under-defaulted cell.
    """
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    raw_cells = spec.get("cells")
    if not raw_cells:
        raise ValueError(f"{spec_path}: spec has no non-empty 'cells' list")
    return [normalize_cell(c) for c in raw_cells]


# -------------------------------------------------------------------------------------------
# Scratch-server lifecycle -- docker exec + internal nohup, bridge-IP discovery, health poll by
# response body, teardown via host-visible PID from `docker top`, VRAM read-back. NONE of these
# are called by `--dry-run`; only the `run` subcommand touches Docker (brief: "No live
# Docker/GPU execution" for this WP -- the orchestrator exercises this live, post-merge).
# -------------------------------------------------------------------------------------------


def discover_bridge_ip(container: str = DEFAULT_CONTAINER_NAME) -> str:
    """Container-internal bridge IP for `container`, via `docker inspect` -- scratch-server ports
    are container-internal only (brief: "only 4806->4811 is published; scratch ports are
    container-internal, reachable from the host via the bridge IP")."""
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    result = subprocess.run(
        ["docker", "inspect", "-f", fmt, container],
        capture_output=True, text=True, check=True,
    )
    ip = result.stdout.strip()
    if not ip:
        raise RuntimeError(f"docker inspect returned no bridge IP for container {container!r}")
    return ip


def spawn_scratch_server(
    argv: List[str],
    log_path: str,
    container: str = DEFAULT_CONTAINER_NAME,
) -> None:
    """Spawns `argv` (from `build_command()`) inside `container` via `docker exec` + an internal
    `nohup ... &`, redirecting stdout/stderr to `log_path` (a path inside the container -- the
    per-cell startup-state verification log). Does not itself wait for health or capture a PID:
    correlate the running process afterward via `find_host_pid_by_port()`, which is robust to
    `docker exec`'s own transient shell PID being irrelevant (no `python3`/`kill`/`pkill` in the
    image, so a container-internal `echo $!` PID is not something we could signal from the host
    reliably anyway -- `docker top`'s host-visible PIDs are the mechanism this module commits to).
    """
    inner = " ".join(shlex.quote(a) for a in argv)
    shell_cmd = f"nohup {inner} > {shlex.quote(log_path)} 2>&1 < /dev/null &"
    subprocess.run(
        ["docker", "exec", "-d", container, "sh", "-c", shell_cmd],
        check=True,
    )


def find_host_pid_by_port(port: int, container: str = DEFAULT_CONTAINER_NAME) -> Optional[int]:
    """Finds the HOST-visible PID of the `llama-server --port <port>` process running inside
    `container`, via `docker top` (which reports real host PIDs, not container-namespace ones --
    this is what makes host-side `os.kill()` teardown possible without any kill/pkill binary
    inside the image). Returns `None` if no matching process is found (already exited, or never
    started)."""
    result = subprocess.run(
        ["docker", "top", container, "-eo", "pid,args"],
        capture_output=True, text=True, check=True,
    )
    needle = f"--port {port}"
    for line in result.stdout.splitlines()[1:]:  # skip header row
        line = line.strip()
        if not line or needle not in line:
            continue
        pid_str = line.split(None, 1)[0]
        try:
            return int(pid_str)
        except ValueError:
            continue
    return None


def wait_for_health(base_url: str, timeout: float = 120.0, poll_interval: float = 0.5) -> bool:
    """Polls `GET {base_url}/health` by RESPONSE BODY, never by HTTP status code alone (root
    `AGENTS.md` Quirks: "Probe by response body, never status code" -- a healthy-looking response
    with the wrong body is exactly the failure family this fleet has been burned by repeatedly).
    llama-server's own `/health` returns a JSON body with a `status` field; this treats
    `status == "ok"` as ready and anything else (including a non-JSON body, a connection refused
    while the process is still starting, or a transport error) as not-yet-ready and keeps polling
    until `timeout`. Returns `True`/`False`; never raises on a slow-starting server.
    """
    deadline = time.monotonic() + timeout
    url = f"{base_url}/health"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("status") == "ok":
                return True
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(poll_interval)
    return False


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


def teardown_scratch_server(
    port: int,
    container: str = DEFAULT_CONTAINER_NAME,
    grace_seconds: float = 5.0,
) -> Dict[str, Any]:
    """Finds the host-visible PID for `port` (via `find_host_pid_by_port`) and signals it
    directly from the host (`os.kill` -- SIGTERM, then SIGKILL if it's still alive after
    `grace_seconds`). Never asks the container to kill its own child (no `kill`/`pkill` inside the
    image). Returns a dict describing what happened, including a pre/post VRAM read-back
    (`read_vram_used_bytes()`) so the caller can confirm VRAM actually returned to baseline, not
    merely that the process is gone.
    """
    vram_before = read_vram_used_bytes()
    pid = find_host_pid_by_port(port, container=container)
    if pid is None:
        return {
            "found_pid": None, "signalled": False,
            "vram_before_bytes": vram_before, "vram_after_bytes": vram_before,
        }
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    alive = True
    while time.monotonic() < deadline:
        if find_host_pid_by_port(port, container=container) is None:
            alive = False
            break
        time.sleep(0.25)
    if alive:
        os.kill(pid, signal.SIGKILL)
    vram_after = read_vram_used_bytes()
    return {
        "found_pid": pid, "signalled": True, "force_killed": alive,
        "vram_before_bytes": vram_before, "vram_after_bytes": vram_after,
    }


# -------------------------------------------------------------------------------------------
# Per-cell startup-state verification -- "a healthy endpoint has told you nothing" (llama/
# AGENTS.md). Before a cell's measurements count, assert from its own redirected stdout/stderr
# log that the requested model path, -c, -ctk/-ctv, flash-attention state, and offload placement
# were ACTUALLY activated, not merely requested. A mismatch (or a log too sparse to check, e.g.
# because -v wasn't passed) marks the cell `invalid_config`, never silently measured.
#
# These patterns are written against llama-server build b10534's known console format (per
# docs/qwen38-27b-bakeoff-results.md:174-176: "-v does make llama-server itself emit the
# type_k/type_v/flash_attn lines"). They are NOT live-verified by this WP (no live Docker/GPU
# execution) -- the orchestrator's one live smoke cell (plan Test Plan) is where a pattern
# mismatch would first surface, and a spurious `invalid_config` from a drifted log format is the
# intended, safe failure direction (never a silent pass).
# -------------------------------------------------------------------------------------------

_STARTUP_PATTERNS = {
    "model_path": re.compile(
        r"model(?:\s+loaded from|\s*(?:path)?\s*[=:])\s*'?([^\s'\"]+\.gguf)'?", re.I
    ),
    "n_ctx": re.compile(r"n_ctx\s*=\s*(\d+)"),
    "type_k": re.compile(r"type_k\s*=\s*'?(\w+)'?"),
    "type_v": re.compile(r"type_v\s*=\s*'?(\w+)'?"),
    "flash_attn": re.compile(r"flash_attn\s*=\s*(\d+|enabled|disabled|auto)", re.I),
    "offloaded_layers": re.compile(r"offloaded\s+(\d+)/(\d+)\s+layers\s+to\s+GPU", re.I),
}


def verify_startup_state(log_text: str, cell: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Parses a scratch server's redirected startup log and asserts it matches what `cell`
    requested. Returns `(ok, details)`: `ok` is `True` only if every checkable field was found in
    the log AND matched; any field the log doesn't mention at all (e.g. because `-v` wasn't
    passed, so `type_k`/`type_v`/`flash_attn` never print) counts as a MISMATCH, not a silent
    pass -- an unverifiable cell is not a verified one.
    """
    flags = cell["server_flags"]
    found: Dict[str, Optional[str]] = {}
    for key, pattern in _STARTUP_PATTERNS.items():
        m = pattern.search(log_text)
        found[key] = m.group(0) if m else None

    checks: Dict[str, bool] = {}

    m = _STARTUP_PATTERNS["model_path"].search(log_text)
    expected_model = Path(cell["model_container_path"]).name
    checks["model_path"] = bool(m and expected_model in m.group(1))

    m = _STARTUP_PATTERNS["n_ctx"].search(log_text)
    checks["n_ctx"] = bool(m and int(m.group(1)) == int(flags["-c"]))

    m = _STARTUP_PATTERNS["type_k"].search(log_text)
    checks["type_k"] = bool(m and m.group(1).lower() == str(flags.get("-ctk", "f16")).lower())

    m = _STARTUP_PATTERNS["type_v"].search(log_text)
    checks["type_v"] = bool(m and m.group(1).lower() == str(flags.get("-ctv", "f16")).lower())

    m = _STARTUP_PATTERNS["flash_attn"].search(log_text)
    requested_fa = str(flags.get("-fa", "on")).lower()
    fa_active = bool(m and m.group(1).lower() in ("1", "enabled", "auto") and requested_fa in ("on", "auto"))
    fa_inactive = bool(m and m.group(1).lower() in ("0", "disabled") and requested_fa == "off")
    checks["flash_attn"] = fa_active or fa_inactive

    m = _STARTUP_PATTERNS["offloaded_layers"].search(log_text)
    requested_ngl = int(flags.get("-ngl", 99))
    checks["offload_placement"] = bool(
        m and (int(m.group(1)) == int(m.group(2)) or int(m.group(1)) >= min(requested_ngl, int(m.group(2))))
    )

    ok = all(checks.values())
    return ok, {"checks": checks, "matches": found}


# -------------------------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------------------------


def cmd_dry_run(args: argparse.Namespace) -> int:
    cells = load_spec(Path(args.spec))
    out = []
    for cell in cells:
        cell_id = cell.get("cell_id") or compute_cell_id(cell)
        fit = classify_fit(cell)
        argv = build_command(cell, port=args.port)
        out.append({
            "cell_id": cell_id,
            "alias": cell["alias"],
            "battery": cell["battery"],
            "server_flags": cell["server_flags"],
            "fit": fit,
            "command": argv,
            "command_str": " ".join(shlex.quote(a) for a in argv),
        })
    print(json.dumps({"spec": str(args.spec), "cell_count": len(out), "cells": out}, indent=2, sort_keys=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Live path: spawn one cell's scratch server, verify startup state, poll health, read VRAM,
    tear down. Not exercised by this WP (no live Docker/GPU execution) -- wired for the
    orchestrator's post-merge live smoke cell."""
    cells = load_spec(Path(args.spec))
    target = None
    for cell in cells:
        cell_id = cell.get("cell_id") or compute_cell_id(cell)
        if cell_id == args.cell_id:
            target = cell
            break
    if target is None:
        print(f"no cell with cell_id={args.cell_id!r} in {args.spec}", file=sys.stderr)
        return 2

    vram_before = read_vram_used_bytes()
    argv = build_command(target, port=args.port)
    log_path = f"/tmp/sweep-cell-{args.cell_id}.log"
    spawn_scratch_server(argv, log_path, container=args.container)

    bridge_ip = discover_bridge_ip(args.container)
    base_url = f"http://{bridge_ip}:{args.port}"
    healthy = wait_for_health(base_url, timeout=args.health_timeout)

    log_result = subprocess.run(
        ["docker", "exec", args.container, "cat", log_path],
        capture_output=True, text=True,
    )
    log_text = log_result.stdout
    verified, details = verify_startup_state(log_text, target)

    teardown_result = teardown_scratch_server(args.port, container=args.container)

    result = {
        "cell_id": args.cell_id,
        "healthy": healthy,
        "startup_verified": verified,
        "startup_details": details,
        "vram_before_bytes": vram_before,
        "teardown": teardown_result,
        "invalid_config": not verified,
    }
    print(json.dumps(result, indent=2))
    return 0 if (healthy and verified) else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sweep.py",
        description="Declarative sweep framework "
        "(plans/quant-sweep-harness-and-instrument-v2.md WP-1). See llama/bakeoff/README.md.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dry = sub.add_parser(
        "dry-run", help="expand a spec, print per-cell fit classification and exact command"
    )
    p_dry.add_argument("spec", help="path to a sweep spec JSON file")
    p_dry.add_argument("--port", type=int, default=8899, help="port used when rendering commands")
    p_dry.set_defaults(func=cmd_dry_run)

    p_run = sub.add_parser(
        "run", help="LIVE: spawn one cell's scratch server, verify, teardown (not for this WP)"
    )
    p_run.add_argument("spec", help="path to a sweep spec JSON file")
    p_run.add_argument("--cell-id", required=True, help="cell_id (from dry-run output) to run")
    p_run.add_argument("--container", default=DEFAULT_CONTAINER_NAME)
    p_run.add_argument("--port", type=int, default=8899)
    p_run.add_argument("--health-timeout", type=float, default=120.0)
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # `--dry-run llama/bakeoff/examples/sweep-example.json` (brief's exact gate invocation) is a
    # single flag, not a subcommand -- accept both spellings so the gate's literal wording works
    # unchanged, ahead of handing off to the `dry-run`/`run` subcommand parser below.
    if argv and argv[0] == "--dry-run":
        args = argparse.Namespace(spec=argv[1], port=8899, func=cmd_dry_run)
        return args.func(args)
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
