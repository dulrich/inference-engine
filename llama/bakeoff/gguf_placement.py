#!/usr/bin/env python3
"""Tensor classification and CPU/GPU/lazy placement derivation (WP-R3 of
`plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding F5).

Extracted verbatim from `gguf_fit.py` (see `gguf_reader.py`'s docstring for the whole split).
Every symbol keeps its original name, signature, and behavior; `gguf_fit` re-exports
`classify_tensor`, `PlacementBytes`, and `derive_placement_bytes`, so
`gguf_fit.derive_placement_bytes` and every existing test target still resolve.

This is the layer that answers "given a model's tensor directory and its normalized server
flags, how many weight bytes actually land on the GPU?". It consumes `gguf_reader.TensorInfo`
and is consumed by `gguf_fit._resolve_geometry_and_weights`. Pure and offline: no I/O at all --
the caller has already done the reading.

All placement precedence lives here and is unchanged: `-ngl` full-offload is a precondition
(`None` otherwise, never a guess), `-ncmoe`/`-cmoe`/`-ot` move tensors to CPU by union rather
than precedence, `token_embd.weight` is always CPU, and the mmap/lazy thresholds decide whether
a CPU-resident tensor is pinned or lazy.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

from gguf_reader import GIB, TensorInfo

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


def _parse_ngl_full_offload(ngl: Any, block_count: int | None) -> bool | None:
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


def _ot_cpu_patterns(value: Any) -> list[str] | None:
    """Parses an `-ot` flag value -- a single string (optionally comma-joining several
    `<pattern>=<target>` entries) or a JSON list of such strings -- into the list of regex
    patterns that target `=CPU`. `[]` if the flag is absent/empty (a no-op, not an error). `None`
    -- never a guess -- if ANY entry targets a buffer other than `CPU` or fails to compile as a
    Python regex; a `-ot` this function can't fully account for must not silently derive a number
    from only the entries it understood."""
    if not value:
        return []
    entries = value if isinstance(value, list) else str(value).split(",")
    patterns: list[str] = []
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
    tensor_infos: list[TensorInfo],
    server_flags: dict[str, Any],
    metadata: dict[str, Any],
) -> PlacementBytes | None:
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
