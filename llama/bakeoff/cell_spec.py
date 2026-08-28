#!/usr/bin/env python3
"""The plain-dict sweep-cell normal form: defaults, validation, deterministic identity, and the
exact `llama-server` argv a cell renders to (WP-R3 of
`plans/repository-tooling-structural-remediation.md`; review finding F8).

Extracted verbatim from `sweep.py`. `sweep.py` re-exports every symbol here, so
`sweep.normalize_cell`/`sweep.compute_cell_id`/`sweep.build_command`/
`sweep.VALID_BATTERY_ITEMS` all still resolve -- **`tests/test_cell_identity.py` calls
`sweep.compute_cell_id` and is not touched by this move.**

**Not to be confused with `cell_model.py`.** That module holds WP-R1's TYPED, immutable domain
values (`CellIdentity`/`ResolvedCell`) and owns the one adapter (`ResolvedCell.as_sweep_cell()`)
down to the plain dict. This module holds the plain-dict layer itself -- the normal form that
adapter targets, and that `gguf_fit.classify_fit` and `scratch_lifecycle.scratch_server` both
consume. Typed values deliberately stop at that seam; nothing here speaks dataclasses.

This module exists as its own file rather than staying in `sweep.py` for a structural reason:
`scratch_lifecycle.py` needs `compute_cell_id` and `build_command`, and `sweep.py` imports
`scratch_lifecycle`. Keeping these two functions in `sweep.py` would have made that a circular
import. The dependency edges now run one way only: `sweep` -> {`scratch_lifecycle`, `gguf_fit`,
`cell_spec`} and `scratch_lifecycle` -> {`cell_spec`, `gguf_fit`}.

`compute_cell_id` is the fleet's cell-identity function: its output names record directories
under `llama/bakeoff/records/`, keys every entry in a `*.state.json`, and is the dashboard's
`experiments.id`. Changing it does not error -- it silently forks every consumer's state. It is
frozen by `tests/test_cell_identity.py`.

Pure and offline: no Docker, no network, no sysfs, no clock. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

# -------------------------------------------------------------------------------------------
# Sweep-cell normalization and deterministic IDs.
# -------------------------------------------------------------------------------------------

VALID_BATTERY_ITEMS = ("vram", "timing", "needle", "quality")

# --host 0.0.0.0 is load-bearing (orchestrator fix, first surfaced by the WP-1 live smoke
# cell): llama-server defaults to 127.0.0.1, which inside the container is container-internal
# loopback -- the scratch server comes up healthy and logs "listening on http://127.0.0.1:<port>"
# while being unreachable from the host via the bridge IP, so the health poll times out against
# a perfectly healthy server. The router's own `llama_server_chat_common` macro carries the same
# flag for the same reason (llama/config.yaml).
# -lv 9 is load-bearing too (same smoke cell): the startup-state verification lines
# (llama_context/llama_kv_cache) never print at default verbosity -- see _STARTUP_PATTERNS.
_DEFAULT_SERVER_FLAGS = {"--host": "0.0.0.0", "-lv": 9, "-ctk": "f16", "-ctv": "f16", "-fa": "on", "-ngl": 99}


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


# -------------------------------------------------------------------------------------------
# Command building -- the exact llama-server argv a normalized cell renders to.
# -------------------------------------------------------------------------------------------

_FLAG_ORDER = ["-m", "--port", "-c", "-ctk", "-ctv", "-fa", "-ngl"]


def _flag_args(key: str, value: Any) -> List[str]:
    """Serializes one server flag for the argv. Booleans mean flag PRESENCE, not a literal
    argument: JSON `true` emits the bare flag (`--jinja`), `false`/`None` omits it entirely.
    WP-5 attended-validation finding #3: `str(True)` produced `--jinja True`, which llama-server
    rejects at parse time (`error: invalid argument: True`) -- the server died instantly, the
    health poll timed out against a corpse, and every cell failed `health_timeout` while the
    queue exited 0 "completed". Valueless flags are a real llama-server class and queue authors
    write JSON booleans for them; this keeps cell ids stable (the hash covers the flags dict,
    not the argv)."""
    if value is True:
        return [key]
    if value is False or value is None:
        return []
    return [key, str(value)]


def build_command(cell: Dict[str, Any], port: int) -> List[str]:
    """Builds the exact `llama-server` argv this cell would run, in a fixed, deterministic flag
    order (`_FLAG_ORDER` first, any remaining `server_flags` keys after, sorted) -- two calls on
    the same cell+port always produce byte-identical output. Boolean flag values are
    presence/absence markers, never literal arguments (`_flag_args`).
    """
    flags = dict(cell["server_flags"])
    flags["-m"] = cell["model_container_path"]
    flags["--port"] = port

    argv = ["llama-server"]
    seen = set()
    for key in _FLAG_ORDER:
        if key in flags:
            argv += _flag_args(key, flags[key])
            seen.add(key)
    for key in sorted(k for k in flags if k not in seen):
        argv += _flag_args(key, flags[key])
    return argv
