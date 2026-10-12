#!/usr/bin/env python3
"""Declarative sweep framework (plans/quant-sweep-harness-and-instrument-v2.md, WP-1).

Turns "one model, one hand-typed `docker exec`, one improvised candidate ladder" into a
declarative spec: a JSON list of candidate cells (`{model_container_path, alias, server_flags,
battery}`), a VRAM-fit pre-filter computed from GGUF header geometry (never a substitute for
measurement -- see `classify_fit()`), and a scratch-server lifecycle (spawn inside
`inference-engine-llama`, verify from its own startup log, teardown, VRAM read-back) for driving
one cell end-to-end.

**Module layout (WP-R3 of `plans/repository-tooling-structural-remediation.md`, review finding
F8).** This file had grown to 1,265 lines holding three unrelated concerns. They now live in
three focused modules, and this file is the spec/CLI composition layer plus the stable public
facade every consumer already imports:

  - `gguf_fit.py` -- VRAM budget profiles, cell geometry/weight resolution, and `classify_fit`,
    plus the re-export facade for `gguf_reader.py` (GGUF header/metadata reading),
    `gguf_placement.py` (tensor placement), and `gguf_cache.py` (attention geometry, KV-cache
    byte math) -- split out by WP-R3 of
    `plans/post-audit-architecture-and-reconciliation-remediation.md`; every name still resolves
    off `gguf_fit`. No Docker, no clock, no subprocess.
  - `cell_spec.py` -- the plain-dict cell normal form: `normalize_cell`, the frozen-by-contract
    `compute_cell_id`, and `build_command`.
  - `scratch_lifecycle.py` -- the process layer: spawn/bridge-IP/health-poll/teardown, per-cell
    startup-state verification, and the `scratch_server()` context manager. The only module here
    that touches Docker.
  - this file -- ladder expansion, spec loading, the CLI, and the re-export facade below.

**The re-exports are load-bearing, not cosmetic.** `sweep.compute_cell_id`,
`sweep.classify_fit`, `sweep.build_command`, `sweep.scratch_server`,
`sweep.ScratchServerHandle`, `sweep.read_vram_used_bytes`, `sweep.VRAM_PROFILES`,
`sweep.DEFAULT_CONTAINER_NAME`, ... are the names `batch.py`, `queue_file.py`, `cell_model.py`,
`cell_runner.py`, `control_dispatcher.py`, `verify_run.py`, and the test suite spell today --
including `tests/test_cell_identity.py`, the identity lock, which is deliberately untouched by
the split. Nothing was renamed, re-signed, or migrated to a new call site.

Zero new runtime dependencies: Python 3 stdlib only (`struct`, `subprocess`, `urllib`, `json`,
`hashlib`, `argparse`, `re`, `os`, `signal`, `time`) -- matches `run_harness.py`'s existing
stance. No `gguf`, no `numpy`, no `requests`.

**`--dry-run` never touches Docker.** Cell expansion, fit classification, and the exact
llama-server command each cell would run are all computable from the spec file and (optionally)
a real host GGUF path alone. The scratch-server lifecycle (spawn, health poll, teardown, VRAM
read-back) now lives in `scratch_lifecycle.py` and is only reached by the `run` subcommand.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
LLAMA_DIR = MODULE_DIR.parent
REPO_ROOT = LLAMA_DIR.parent


# -------------------------------------------------------------------------------------------
# Public facade (WP-R3). Every name below was defined in this file before the split and is
# re-exported here unchanged -- the same objects, so `sweep.<name> is gguf_fit.<name>` and every
# consumer, test, and `mock.patch.object(sweep, ...)` target keeps resolving.
#
# The underscore-prefixed names are re-exported deliberately: `_ScratchServerContext`,
# `_STARTUP_PATTERNS`, `_DEFAULT_SERVER_FLAGS`, `_FLAG_ORDER`, `_flag_args`, `_utc_now_iso`, and
# the two `_resolve_geometry_and_weights*` helpers were all reachable as `sweep._name` before the
# split, and quietly narrowing the surface would not be behavior-preserving. The one deliberate
# exception is the GGUF binary parser's own internals (`_GGUF_TYPE_*`, `_GGUF_SCALAR_STRUCT`,
# `_read_exact`, `_read_gguf_string`, `_read_gguf_value`): those are struct-unpacking details of
# `read_gguf_metadata`, nothing in this repo references them through `sweep`, and they now live
# only in `gguf_fit` where they belong.
# -------------------------------------------------------------------------------------------

from cell_spec import (  # noqa: E402,F401
    _DEFAULT_SERVER_FLAGS,
    _FLAG_ORDER,
    VALID_BATTERY_ITEMS,
    _flag_args,
    build_command,
    compute_cell_id,
    normalize_cell,
)
from gguf_fit import (  # noqa: E402,F401
    CARD_NAMEPLATE_VRAM_GIB,
    FIT_MARGIN_GIB,
    GIB,
    HEADLESS_BASELINE_CEILING_GIB,
    HEADLESS_PINNED_MARGIN_GIB,
    KV_TYPE_BYTES_PER_ELEMENT,
    MARGINAL_MARGIN_GIB,
    NON_WEIGHT_OVERHEAD_GIB,
    REALISTIC_FREE_VRAM_GIB,
    VRAM_PROFILES,
    GGUFParseError,
    IndexerGeometry,
    VRAMProfileRefused,
    _resolve_geometry_and_weights,
    _resolve_geometry_and_weights_raw,
    classify_fit,
    compute_vram_budget_bytes,
    gguf_attention_geometry,
    kv_bytes_per_token,
    read_gguf_metadata,
    read_vram_total_bytes,
    read_vram_used_bytes,
)
from scratch_lifecycle import (  # noqa: E402,F401
    _STARTUP_PATTERNS,
    DEFAULT_CONTAINER_NAME,
    ScratchServerHandle,
    _ScratchServerContext,
    _utc_now_iso,
    discover_bridge_ip,
    find_host_pid_by_port,
    scratch_server,
    spawn_scratch_server,
    teardown_scratch_server,
    verify_startup_state,
    wait_for_health,
)

# -------------------------------------------------------------------------------------------
# Ladder expansion -- one model x KV-type ladder x context ladder.
# -------------------------------------------------------------------------------------------


def expand_ladder(
    base_cell: dict[str, Any],
    kv_type_ladder: list[tuple[str, str]],
    context_ladder: list[int],
    battery: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Cross-products one base cell (model + alias-prefix + any shared flags) against a
    `(ctk, ctv)` ladder and a `-c` ladder, producing one normalized cell per combination. Each
    cell's alias gets a `-ctk<ctk>-ctv<ctv>-c<c>` suffix so the ladder's cells never collide on
    alias -- the base cell's own `alias` is used as the prefix. `battery`, if given, overrides
    the base cell's `battery` for every expanded cell (the base cell need not carry one).
    """
    out: list[dict[str, Any]] = []
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


def load_spec(spec_path: Path) -> list[dict[str, Any]]:
    """Loads a sweep spec JSON file: `{"cells": [ {...}, ... ]}`. Every cell is passed through
    `normalize_cell()` before being returned, so callers never see a raw/under-defaulted cell.
    """
    with open(spec_path, encoding="utf-8") as f:
        spec = json.load(f)
    raw_cells = spec.get("cells")
    if not raw_cells:
        raise ValueError(f"{spec_path}: spec has no non-empty 'cells' list")
    return [normalize_cell(c) for c in raw_cells]


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
    tear down -- via the shared `scratch_server()` context manager (audit E1; see its own
    docstring). Not exercised by this WP (no live Docker/GPU execution) -- wired for the
    orchestrator's post-merge live smoke cell. Output shape and exit code are byte-for-byte
    unchanged from before this WP's refactor."""
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

    with scratch_server(
        target, port=args.port, container=args.container, health_timeout=args.health_timeout,
    ) as handle:
        healthy = handle.healthy
        verified = handle.verified
        details = handle.details
        vram_before = handle.vram_before_bytes

    result = {
        "cell_id": args.cell_id,
        "healthy": healthy,
        "startup_verified": verified,
        "startup_details": details,
        "vram_before_bytes": vram_before,
        "teardown": handle.teardown_result,
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


def main(argv: list[str] | None = None) -> int:
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
