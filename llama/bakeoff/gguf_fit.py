#!/usr/bin/env python3
"""VRAM budget profiles and the fit pre-filter -- the high-level owner of the `gguf_*` family
(WP-R3 of `plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding F5).

Originally extracted verbatim from `sweep.py` (WP-R3 of
`plans/repository-tooling-structural-remediation.md`, review finding F8), this file then grew to
1,152 lines by carrying four separable domains at once. Three of them now live in focused
siblings, and this file keeps the name, the policy layer, and the facade:

  - `gguf_reader.py` -- the GGUF binary format: header/metadata parsing, the tensor-info
    directory, ggml block sizes, split-shard discovery, `GGUFParseError`, and `GIB`.
  - `gguf_placement.py` -- tensor classification and CPU/GPU/lazy placement derivation.
  - `gguf_cache.py` -- attention/indexer geometry and KV-cache bytes/token.
  - this file -- the VRAM profiles and budget arithmetic, the sysfs readers, cell geometry/weight
    resolution, `classify_fit`, and the re-export facade below.

**The re-exports are load-bearing, not cosmetic.** `gguf_fit.read_gguf_metadata`,
`gguf_fit.GGML_TYPE_SIZES`, `gguf_fit.classify_tensor`, `gguf_fit.derive_placement_bytes`,
`gguf_fit.kv_bytes_per_token`, `gguf_fit.IndexerGeometry`, `gguf_fit.GIB`, ... are the names
`sweep.py` (which re-exports them onward to `batch.py`, `queue_file.py`, `cell_runner.py`,
`verify_run.py`, ...), `cell_model.py`, `scratch_lifecycle.py`, and the test suite spell today,
including the `mock.patch.object(gguf_fit, "read_gguf_metadata", ...)` targets in
`tests/test_record_contract.py` and `tests/test_fit_lock.py` -- which keep working because
`_resolve_geometry_and_weights_raw` below still resolves that name through THIS module's
globals. Nothing was renamed, re-signed, or migrated to a new call site. The GGUF parser's own
struct-unpacking internals (`_read_exact`, `_read_gguf_string`, `_read_gguf_value`, the
`_GGUF_TYPE_*` ids) are deliberately NOT re-exported -- same stance `sweep.py` already took on
them -- and now live only in `gguf_reader`.

**This module is pure.** Its only I/O is the two host sysfs VRAM counters and (via
`gguf_reader`) a GGUF file's header prefix -- no `docker`, no network, no clock, no subprocess.
The two sysfs readers (`read_vram_total_bytes`/`read_vram_used_bytes`) are the real,
live-capable defaults of injectable parameters; every test passes a fake and none of them touch
sysfs (same "real controller, fake in tests" pattern as `ContainerController` in `batch.py`).

Zero new runtime dependencies: Python 3 stdlib only.

The fit numbers this module computes are ESTIMATES that gate what gets attempted; they never
substitute for a real load-and-measure (house rule, `plans/quant-sweep-harness-and-instrument-
v2.md` Key Changes/WP-1). See `classify_fit`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gguf_cache import (  # noqa: F401 -- re-exported facade, see the module docstring
    INDEXER_GEOMETRY_KEYS,
    KV_TYPE_BYTES_PER_ELEMENT,
    IndexerGeometry,
    gguf_attention_geometry,
    kv_bytes_per_token,
)
from gguf_placement import (  # noqa: F401 -- re-exported facade, see the module docstring
    PlacementBytes,
    classify_tensor,
    derive_placement_bytes,
)
from gguf_reader import (  # noqa: F401 -- re-exported facade, see the module docstring
    GGML_TYPE_SIZES,
    GIB,
    GGUFParseError,
    TensorInfo,
    read_gguf_metadata,
    read_gguf_tensor_infos,
)

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
    with open(sysfs_path, encoding="utf-8") as f:
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
    with open(sysfs_path, encoding="utf-8") as f:
        return int(f.read().strip())


def compute_vram_budget_bytes(
    vram_profile: str,
    vram_total_bytes_fn: Callable[[], int] = read_vram_total_bytes,
    vram_used_bytes_fn: Callable[[], int] | None = None,
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
# Fit classification -- resolves a normalized cell dict's weights + attention geometry (real
# GGUF header or `synthetic_geometry` fixture) and scores it against the VRAM budget.
# -------------------------------------------------------------------------------------------


def _resolve_geometry_and_weights(
    cell: dict[str, Any],
) -> tuple[int | None, dict[str, Any] | None, str | None, dict[str, int] | None]:
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
    derived: dict[str, int] | None = None
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


def _cell_has_recognized_placement_flag(flags: dict[str, Any]) -> bool:
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


def _resolve_geometry_and_weights_raw(cell: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, str | None]:
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
    cell: dict[str, Any],
    vram_profile: str = "desktop-shared",
    vram_total_bytes_fn: Callable[[], int] = read_vram_total_bytes,
    vram_used_bytes_fn: Callable[[], int] | None = None,
) -> dict[str, Any]:
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
