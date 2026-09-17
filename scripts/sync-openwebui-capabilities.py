#!/usr/bin/env python3
"""Sync llama-router capability metadata into Open WebUI's per-model admin config.

Source of truth: `GET <router>/v1/models` (default `http://127.0.0.1:4811`) — the capability
contract pinned in `docs/model-capabilities.md`, merged flat under `meta.llamaswap` on each
entry. `id` on that endpoint is the config model KEY (not the alias); that is also the id Open
WebUI stores its per-model rows under (see the consumer-registry row in `llama/config.yaml`'s
header).

Target: Open WebUI's admin "Models" API (`/api/v1/models/*`, live-verified 2026-08-24 against
v0.11.0). This script owns exactly three sub-fields of a model's stored `meta`, plus the
allowlisted numeric sampling keys inside `params` (added by
`plans/overnight-test-matrix-redesign.md` WP-5) — and only those. Everything else already
present on a row (name, the rest of `params`, access_grants, other capability toggles a human
set, tool ids, default features, ...) is read back and round-tripped unchanged, because Open
WebUI's update route replaces `name`/`meta`/`params` wholesale rather than patching:

  * `meta.capabilities.vision`      <- bool, `"image" in input_modalities`
  * `meta.description`              <- human summary (name + tool calling + reasoning level +
                                        quant/KV/flash-attention/MTP + context length)
  * `meta.llamaswap.context_length` <- int, `meta.llamaswap.context_length` on the router
  * `params.<SAMPLING_PARAM_KEYS>`  <- per-key merge from `meta.llamaswap.sampling` on the
                                        router (only the numeric allowlist below; `sampling.source`
                                        is router metadata only and is never written to `params`;
                                        every other `params` key on the row is human-owned and
                                        round-trips untouched)

It also owns `is_active` for every router-served id (user decision 2026-09-14,
`plans/bench-dashboard-serving-control.md` WP-3), driven by the visibility declaration
`router/visibility.json` (`--visibility`, default resolved from this file's own location, never
from the cwd). `is_active` is desired-true exactly when that model key's declared `consumers`
include `openwebui`. The route is a TOGGLE, not a setter, so the script compares the desired
value against the one it already read off `/api/v1/models/base` and posts only on a difference;
a row it had to create is RE-READ first, because a created row's default is not assumed. Every
other Open WebUI setting stays admin-owned.

Rows Open WebUI has never seen (no admin has opened/customized that model yet) don't exist in
its DB at all — this script creates them via `POST .../create` with a minimal body (id, the
router's display name, the three managed `meta` fields, and `params` seeded with whatever
allowlisted sampling keys the router model carries — `{}` for a model with no sampling block).
Existing rows are merge-updated via `POST .../model/update`. A router id with no live
counterpart (a retired/renamed model key — see the alias-contract discussion in
`llama/config.yaml`'s header) is reported and left untouched; this script never deletes —
hiding a model is deactivation, not deletion.

Three further concerns joined this script in `plans/owui-effort-rows-and-arming.md` WP-2, all
driven by repo-owned declarations and all subject to the same "own a named field, round-trip
everything else" rule:

  * **Arming.** Every row this script drives to `is_active: true` (base row or derived row) gets
    `meta.toolIds ⊇ arm_tool_ids` and `meta.defaultFeatureIds ⊇ arm_feature_ids` from
    `scripts/openwebui-policy.json` (`--policy`, default resolved from this file's location),
    minus that policy's `retire_tool_ids`/`retire_feature_ids`. The merge is append-only against
    what the row already carries, so a human-added extra id survives. A row driven INACTIVE gets
    no arming change at all — its lists round-trip untouched.
  * **Derived effort rows.** For every router model whose `router/visibility.json` `gateway_id`
    has an `effortKwargs` entry in `router/gateway.config.json` (`--gateway-config`), this script
    owns one Open WebUI row per reasoning level, id `<model key>--effort-<level>`, name
    `<router display name> · <level>`, `base_model_id` = the model key, `params` = the base's
    sampling keys plus `reasoning_effort: <level>`, `meta` = the same three managed fields as the
    base plus arming. The levels are the DISTINCT `reasoning_effort` values in that entry plus
    `none`, in the canonical order `none, low, medium, high, xhigh`; a gateway id with no entry
    gets no derived rows. `params.reasoning_effort` is sync-owned on DERIVED rows only — on a
    base row it stays human-owned and is never read or written.
  * **Retirement.** An Open WebUI row that matches no current router model key and is not a
    currently derivable derived id is a retirement CANDIDATE only if this script provably owns
    it: it carries the `meta.llamaswap` marker, or its id has the `--effort-` derived shape, or
    it is listed in the policy's `retire_legacy_ids`. An active candidate is retired (`RETIRE` —
    `is_active: false` via the toggle route, verified by re-read); an already-inactive one is
    just reported. Any other unmatched row is printed as `UNMANAGED` and never touched: an
    admin's or another connection's row is not this script's to deactivate. Retirement is
    computed against the UNFILTERED router listing even under `--only`, and `--only` never
    retires anything.

Row enumeration needs both admin routes: `GET /api/v1/models/base` returns only rows with
`base_model_id: null` (derived rows are invisible there) and `GET /api/v1/models/export` returns
every workspace row (those WITH a `base_model_id`), including inactive ones. The complete row set
is their union; a single row is re-read via `GET /api/v1/models/model?id=<id>`.

Credentials: `OPENWEBUI_URL` and `OPENWEBUI_API_KEY` come from the environment (or the
`--openwebui-url`/`--openwebui-key` flags) — there is no default and nothing is ever written to
a file or logged. The router URL is not a credential and defaults to the loopback publish.

Dry-run (the default) prints the intended per-model diff and makes no mutating call. For a
model with NO existing row the dry-run prints `CREATE` plus the INTENDED `is_active` — there is
no observed value to transition from, so it never prints a fabricated transition. `--apply`
performs the sync; running `--apply` twice in a row must show an empty diff on the second run
(the desired state is a pure function of the router's live capability block plus
`router/visibility.json`).

Fail-closed: a missing, unparseable or unknown-schema declaration (visibility OR policy), a
malformed gateway config, an entry carrying a consumer token outside the closed vocabulary, or a
router id no entry declares, all stop the run before any mutating call.

Module map (`scripts/openwebui_sync/`, one synchronization domain, four seams):

  * `declarations.py` -- the repo-owned inputs above (visibility, policy, effort ladder) and
                         their fail-closed loaders and default paths.
  * `planner.py`      -- pure decisions: the managed-field contract, the create/update/toggle
                         matrix, arming merges, derived ids, the retirement partition. No I/O.
  * `client.py`       -- Open WebUI/router endpoint shapes, auth, timeouts, read-after-write.
  * `runner.py`       -- one row (base or derived) planned, printed and, under `--apply`, done.
  * `types.py`        -- the shapes that cross those seams.

This file stays the stable entry path (`python3 scripts/sync-openwebui-capabilities.py ...`)
and owns the CLI coordination itself: argument parsing, fetch order, `--only`, dry-run/apply
selection, and the run header/summary.
"""


from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List

# Import safety (both entry paths, deliberately): when this file is RUN, `scripts/` is already
# `sys.path[0]`; when it is importlib-loaded from an absolute path (its hyphenated filename
# makes a normal `import` impossible, so `scripts/test_sync_openwebui_capabilities.py` loads it
# that way), it is not on the path at all. Putting its own directory on `sys.path` here makes
# the package resolvable from both, with no loader change in the test and no dependence on cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from openwebui_sync import client, planner, runner  # noqa: E402
from openwebui_sync.client import (  # noqa: E402  (re-exported: stable public names)
    DEFAULT_ROUTER_URL,
    apply_model,
    fetch_openwebui_row,
    fetch_openwebui_rows,
    fetch_router_models,
    retire_row,
    toggle_is_active,
)
from openwebui_sync.declarations import (  # noqa: E402
    CONSUMER_VOCABULARY,
    EFFORT_LEVEL_ORDER,
    OPENWEBUI_CONSUMER,
    POLICY_LIST_FIELDS,
    POLICY_SCHEMA,
    VISIBILITY_SCHEMA,
    default_gateway_config_path,
    default_policy_path,
    default_visibility_path,
    desired_is_active,
    effort_levels_for,
    gateway_id_for,
    load_effort_levels,
    load_policy,
    load_visibility,
)
from openwebui_sync.errors import SyncError  # noqa: E402
from openwebui_sync.planner import (  # noqa: E402
    DERIVED_ID_MARKER,
    DERIVED_NAME_SEPARATOR,
    SAMPLING_PARAM_KEYS,
    build_description,
    compute_desired,
    derived_row_id,
    derived_row_name,
    is_retirement_candidate,
    merge_arming,
    plan_change,
    plan_is_active,
)


# The names this file re-exports are a stable surface: `scripts/test_sync_openwebui_capabilities.py`
# reaches them through the importlib-loaded module, and an operator reading the entry script
# should see the whole vocabulary in one place. They are imports, not wrappers -- the owner
# module above is the single definition site for each.
__all__ = [
    "DEFAULT_ROUTER_URL",
    "DERIVED_ID_MARKER",
    "DERIVED_NAME_SEPARATOR",
    "CONSUMER_VOCABULARY",
    "EFFORT_LEVEL_ORDER",
    "OPENWEBUI_CONSUMER",
    "POLICY_LIST_FIELDS",
    "POLICY_SCHEMA",
    "SAMPLING_PARAM_KEYS",
    "VISIBILITY_SCHEMA",
    "SyncError",
    "apply_model",
    "build_description",
    "client",
    "compute_desired",
    "default_gateway_config_path",
    "default_policy_path",
    "default_visibility_path",
    "derived_row_id",
    "derived_row_name",
    "desired_is_active",
    "effort_levels_for",
    "fetch_openwebui_row",
    "fetch_openwebui_rows",
    "fetch_router_models",
    "gateway_id_for",
    "is_retirement_candidate",
    "load_effort_levels",
    "load_policy",
    "load_visibility",
    "main",
    "merge_arming",
    "parse_args",
    "plan_change",
    "plan_is_active",
    "planner",
    "retire_row",
    "runner",
    "toggle_is_active",
]


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Sync llama-router /v1/models capability metadata into Open WebUI per-model config."
        ),
        epilog=(
            "OPENWEBUI_URL and OPENWEBUI_API_KEY must come from the environment or the "
            "--openwebui-url/--openwebui-key flags -- there is no default. Prefer the "
            "environment variables: a flag value is visible in shell history and process "
            "listings on a shared host."
        ),
    )
    p.add_argument(
        "--router-url",
        default=os.environ.get("LLAMA_ROUTER_URL", DEFAULT_ROUTER_URL),
        help=f"llama router base URL (default: {DEFAULT_ROUTER_URL}, or $LLAMA_ROUTER_URL)",
    )
    p.add_argument(
        "--openwebui-url",
        default=os.environ.get("OPENWEBUI_URL"),
        help="Open WebUI base URL (default: $OPENWEBUI_URL; required)",
    )
    p.add_argument(
        "--openwebui-key",
        default=os.environ.get("OPENWEBUI_API_KEY"),
        help="Open WebUI admin API key (default: $OPENWEBUI_API_KEY; required, never logged)",
    )
    p.add_argument(
        "--visibility",
        default=None,
        metavar="PATH",
        help=(
            "visibility declaration path (default: <repo>/router/visibility.json, resolved "
            "from this script's own location, never from the cwd)"
        ),
    )
    p.add_argument(
        "--policy",
        default=None,
        metavar="PATH",
        help=(
            "arming/retirement policy path (default: <repo>/scripts/openwebui-policy.json, "
            "resolved from this script's own location, never from the cwd)"
        ),
    )
    p.add_argument(
        "--gateway-config",
        default=None,
        metavar="PATH",
        help=(
            "gateway config carrying the `effortKwargs` ladder (default: "
            "<repo>/router/gateway.config.json, resolved from this script's own location)"
        ),
    )
    p.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="MODEL_ID",
        help="restrict the sync to this router model id (repeatable); default is all models",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="perform the mutating sync; default is dry-run (print the diff, change nothing)",
    )
    return p.parse_args(argv)


def print_header(
    args: argparse.Namespace,
    visibility_path: Path,
    policy_path: Path,
    gateway_config_path: Path,
    model_count: int,
) -> None:
    """The run banner: mode plus every input this run resolved, before the first row."""
    print(f"=== sync-openwebui-capabilities: {'APPLY' if args.apply else 'DRY-RUN'} ===")
    print(f"router:     {args.router_url}")
    print(f"open webui: {args.openwebui_url}")
    print(f"visibility: {visibility_path}")
    print(f"policy:     {policy_path}")
    print(f"gateway:    {gateway_config_path}")
    print(f"models:     {model_count}")
    print()


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    if not args.openwebui_url:
        print(
            "error: Open WebUI base URL not set -- pass --openwebui-url or set $OPENWEBUI_URL",
            file=sys.stderr,
        )
        return 2
    if not args.openwebui_key:
        print(
            "error: Open WebUI API key not set -- pass --openwebui-key or set "
            "$OPENWEBUI_API_KEY",
            file=sys.stderr,
        )
        return 2

    visibility_path = (
        Path(args.visibility) if args.visibility else default_visibility_path()
    )
    policy_path = Path(args.policy) if args.policy else default_policy_path()
    gateway_config_path = (
        Path(args.gateway_config) if args.gateway_config else default_gateway_config_path()
    )

    # Fail-closed, and deliberately BEFORE the first HTTP call: a malformed declaration, policy
    # or gateway config must stop the run without a single mutating request having been possible.
    try:
        visibility_models = load_visibility(visibility_path)
        policy = load_policy(policy_path)
        effort_levels = load_effort_levels(gateway_config_path)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        router_models = fetch_router_models(args.router_url)
        existing_rows = fetch_openwebui_rows(args.openwebui_url, args.openwebui_key)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Computed against the UNFILTERED listing, BEFORE `--only` narrows the walk (see
    # `planner.managed_row_ids`: after the filter, every other model's rows look like orphans).
    managed_ids = planner.managed_row_ids(router_models, visibility_models, effort_levels)

    if args.only:
        wanted = set(args.only)
        router_models = [m for m in router_models if m["id"] in wanted]
        missing = wanted - {m["id"] for m in router_models}
        for mid in sorted(missing):
            print(f"warning: --only {mid} not found in router /v1/models", file=sys.stderr)

    router_models = sorted(router_models, key=lambda m: m["id"])
    print_header(args, visibility_path, policy_path, gateway_config_path, len(router_models))

    counts: Dict[str, int] = {"create": 0, "derive": 0, "update": 0, "toggle": 0, "noop": 0}
    failures = 0
    for entry in router_models:
        model_counts, model_failures = runner.sync_model(
            entry,
            visibility_models=visibility_models,
            effort_levels=effort_levels,
            existing_rows=existing_rows,
            policy=policy,
            apply=args.apply,
            openwebui_url=args.openwebui_url,
            api_key=args.openwebui_key,
        )
        for action, count in model_counts.items():
            counts[action] = counts.get(action, 0) + count
        failures += model_failures

    retirement = planner.classify_unmatched_rows(
        existing_rows, managed_ids, policy["retire_legacy_ids"]
    )
    failures += runner.retire_rows(
        retirement,
        apply=args.apply,
        only=args.only,
        openwebui_url=args.openwebui_url,
        api_key=args.openwebui_key,
    )

    print(
        f"summary: {counts['create']} create, {counts['derive']} derive, "
        f"{counts['update']} update, {counts['toggle']} visibility toggle, "
        f"{counts['noop']} unchanged, {len(retirement.active)} retire, "
        f"{len(retirement.unmanaged)} unmanaged"
    )
    if failures:
        print(f"error: {failures} model(s) failed -- see the FAIL / APPLY FAILED lines above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
