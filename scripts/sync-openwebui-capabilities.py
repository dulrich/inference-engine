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
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ROUTER_URL = "http://127.0.0.1:4811"

# The visibility declaration (`plans/bench-dashboard-serving-control.md` WP-3). Keyed by
# `llama/config.yaml` model key — which is also `/v1/models` `id` and the Open WebUI row id, so
# no crosswalk is needed on this side (`gateway_id` is the Claude Code picker's business).
VISIBILITY_SCHEMA = "serving-visibility-v1"
# Closed vocabulary, canonical order. There is deliberately no `router` token: "router-only" is
# the COMPLEMENT — an entry with `[]` is reachable by id through the gateway and appears in
# neither the Claude Code picker nor Open WebUI's active set.
CONSUMER_VOCABULARY = ("cc", "openwebui")
OPENWEBUI_CONSUMER = "openwebui"
REQUEST_TIMEOUT_S = 15

# The only `meta.llamaswap.sampling` keys this script ever writes into an Open WebUI model
# row's `params`. Explicit allowlist (plans/overnight-test-matrix-redesign.md WP-5, audit E-4
# pin): `sampling.source` is a router-metadata provenance citation, never a serving parameter,
# and must never appear here -- a literal "map every sampling key" implementation would write
# an invalid `params.source`, which is exactly the defect this allowlist exists to prevent.
SAMPLING_PARAM_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
)

# The arming/retirement policy (`plans/owui-effort-rows-and-arming.md` WP-2). It lives beside
# this script, NOT under `router/` (whose gate set is the gateway's), and every field is a list
# of strings -- the loader below rejects anything else rather than degrading to a default, since
# a silently-empty `arm_tool_ids` would un-arm every row on the next apply.
POLICY_SCHEMA = "openwebui-policy-v1"
POLICY_LIST_FIELDS = (
    "arm_tool_ids",
    "arm_feature_ids",
    "retire_tool_ids",
    "retire_feature_ids",
    "retire_legacy_ids",
)

# Canonical print/creation order for the derived effort rows. The ladder's CONTENT comes from
# `router/gateway.config.json`'s `effortKwargs` (nothing is forked here); this tuple only fixes
# the order, so the picker reads low-to-high instead of dict-insertion order. `none` is always
# present for a model that has a ladder at all -- it is the one level with a measured, binary
# effect (WP-1: 0 reasoning chars), so it is never conditional on the table listing it.
EFFORT_LEVEL_ORDER = ("none", "low", "medium", "high", "xhigh")
# Stored-identifier shape, append-only under the alias contract (`llama/config.yaml` header).
DERIVED_ID_MARKER = "--effort-"
# U+00B7 MIDDLE DOT with a space either side -- a display-name separator, never an id separator.
DERIVED_NAME_SEPARATOR = " · "


class SyncError(RuntimeError):
    """Raised for any non-recoverable discovery/apply failure; message is safe to print."""


def default_visibility_path() -> Path:
    """`<repo>/router/visibility.json`, resolved from THIS FILE, never from the cwd."""
    return Path(__file__).resolve().parent.parent / "router" / "visibility.json"


def load_visibility(path: Path) -> Dict[str, Dict[str, Any]]:
    """Return the declaration's `models` map. Fail-closed on every malformed shape.

    Raises SyncError for: a missing or unreadable file, invalid JSON, a schema literal this
    script does not understand, a non-mapping `models`, an entry that is not a mapping, a
    `consumers` value that is not a list, and any consumer token outside CONSUMER_VOCABULARY.
    Treating any of these as "no declaration" would silently deactivate every model, which is
    why none of them degrades to a default.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"cannot read visibility declaration {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"visibility declaration {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SyncError(f"visibility declaration {path} is not a JSON object")
    if parsed.get("schema") != VISIBILITY_SCHEMA:
        raise SyncError(
            f"visibility declaration {path} carries schema {parsed.get('schema')!r}, "
            f"expected {VISIBILITY_SCHEMA!r}"
        )
    models = parsed.get("models")
    if not isinstance(models, dict):
        raise SyncError(f"visibility declaration {path} has no `models` mapping")
    for model_key, entry in models.items():
        if not isinstance(entry, dict):
            raise SyncError(
                f"visibility declaration {path}: entry {model_key!r} is not a mapping"
            )
        consumers = entry.get("consumers")
        if not isinstance(consumers, list):
            raise SyncError(
                f"visibility declaration {path}: {model_key!r} has no `consumers` list"
            )
        unknown = [c for c in consumers if c not in CONSUMER_VOCABULARY]
        if unknown:
            raise SyncError(
                f"visibility declaration {path}: {model_key!r} carries unknown consumer "
                f"token(s) {', '.join(repr(u) for u in unknown)} -- the closed vocabulary is "
                f"{', '.join(CONSUMER_VOCABULARY)}"
            )
    return models


def default_policy_path() -> Path:
    """`<repo>/scripts/openwebui-policy.json`, resolved from THIS FILE, never from the cwd."""
    return Path(__file__).resolve().parent / "openwebui-policy.json"


def load_policy(path: Path) -> Dict[str, List[str]]:
    """Return the arming/retirement policy. Fail-closed on every malformed shape.

    Raises SyncError for: a missing or unreadable file, invalid JSON, a non-object document, a
    schema literal this script does not understand, any of the five list fields missing or not a
    list of strings, and any unknown top-level key. None of these degrades to a default: an
    empty-by-accident `arm_tool_ids` would silently un-arm every active row, and an
    empty-by-accident `retire_legacy_ids` would silently leave a dead row live -- both are
    exactly the "healthy run that did the wrong thing" failure this repo gates against.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"cannot read Open WebUI policy {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Open WebUI policy {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SyncError(f"Open WebUI policy {path} is not a JSON object")
    if parsed.get("schema") != POLICY_SCHEMA:
        raise SyncError(
            f"Open WebUI policy {path} carries schema {parsed.get('schema')!r}, "
            f"expected {POLICY_SCHEMA!r}"
        )
    unknown_keys = sorted(set(parsed) - {"schema"} - set(POLICY_LIST_FIELDS))
    if unknown_keys:
        raise SyncError(
            f"Open WebUI policy {path} carries unknown top-level key(s) "
            f"{', '.join(repr(k) for k in unknown_keys)} -- the known fields are "
            f"{', '.join(POLICY_LIST_FIELDS)}"
        )
    policy: Dict[str, List[str]] = {}
    for field in POLICY_LIST_FIELDS:
        value = parsed.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SyncError(
                f"Open WebUI policy {path}: {field!r} must be a list of strings, got {value!r}"
            )
        policy[field] = list(value)
    return policy


def default_gateway_config_path() -> Path:
    """`<repo>/router/gateway.config.json`, resolved from THIS FILE, never from the cwd."""
    return Path(__file__).resolve().parent.parent / "router" / "gateway.config.json"


def load_effort_levels(path: Path) -> Dict[str, List[str]]:
    """Return {gateway id: [effort level, ...]} derived from the gateway's `effortKwargs`.

    The ladder is NOT re-declared for Open WebUI: it is read off the table the Claude Code leg
    already uses, keyed by gateway id, by walking every provider object for an `effortKwargs`
    mapping (the provider index is not hardcoded). A level's Open WebUI value is the
    `reasoning_effort` string that entry maps to -- so `high -> {reasoning_effort: medium}` and
    `max -> {reasoning_effort: xhigh}` collapse onto `medium`/`xhigh` rather than forking two
    more rows, and an entry carrying only `enable_thinking` contributes nothing (its effect is
    `none`, which is always present anyway).

    A file with no `effortKwargs` anywhere yields `{}` -- that means "no derived rows anywhere",
    which is a valid state, not an error. Unreadable or invalid JSON IS an error.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"cannot read gateway config {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"gateway config {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SyncError(f"gateway config {path} is not a JSON object")

    levels: Dict[str, List[str]] = {}
    providers = parsed.get("providers")
    for provider in providers if isinstance(providers, list) else []:
        if not isinstance(provider, dict):
            continue
        table = provider.get("effortKwargs")
        if not isinstance(table, dict):
            continue
        for gateway_id, by_level in table.items():
            if not isinstance(by_level, dict):
                continue
            seen: List[str] = []
            for kwargs in by_level.values():
                if not isinstance(kwargs, dict):
                    continue
                value = kwargs.get("reasoning_effort")
                if isinstance(value, str) and value not in seen:
                    seen.append(value)
            present = set(seen) | {"none"}
            ordered = [level for level in EFFORT_LEVEL_ORDER if level in present]
            ordered += [level for level in seen if level not in EFFORT_LEVEL_ORDER]
            levels[gateway_id] = ordered
    return levels


def derived_row_id(model_key: str, level: str) -> str:
    """The stored Open WebUI id for one derived effort row (append-only: never re-spell this)."""
    return f"{model_key}{DERIVED_ID_MARKER}{level}"


def derived_row_name(router_name: str, level: str) -> str:
    """The picker display name for one derived effort row: `<router display name> · <level>`."""
    return f"{router_name}{DERIVED_NAME_SEPARATOR}{level}"


def gateway_id_for(visibility_models: Dict[str, Dict[str, Any]], router_id: str) -> Optional[str]:
    """The declared `gateway_id` for a model key, or None (including for an undeclared key).

    Unlike `desired_is_active`, this never raises: it is called while enumerating the ladder for
    the UNFILTERED router listing, where an undeclared key is reported (and failed) by the
    per-model walk, not here.
    """
    entry = visibility_models.get(router_id)
    if not isinstance(entry, dict):
        return None
    gateway_id = entry.get("gateway_id")
    return gateway_id if isinstance(gateway_id, str) else None


def effort_levels_for(
    visibility_models: Dict[str, Dict[str, Any]],
    effort_levels: Dict[str, List[str]],
    router_id: str,
) -> List[str]:
    """The effort levels this model key gets derived rows for ([] when it has no ladder)."""
    gateway_id = gateway_id_for(visibility_models, router_id)
    if gateway_id is None:
        return []
    return list(effort_levels.get(gateway_id, []))


def merge_arming(existing: Any, arm_ids: List[str], retire_ids: List[str]) -> List[str]:
    """Append-only merge of one arming list: existing order first, then the policy's additions.

    Anything a human added to the row survives (this is a merge, not an overwrite); anything the
    policy retires is filtered out even if it was already there. A non-list existing value (a
    row an admin left in a shape Open WebUI no longer writes) is treated as absent rather than
    crashing the run.
    """
    merged = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    for item in arm_ids:
        if item not in merged:
            merged.append(item)
    return [item for item in merged if item not in retire_ids]


def desired_is_active(visibility_models: Dict[str, Dict[str, Any]], router_id: str) -> bool:
    """`is_active` this script drives the row to: `openwebui` in its declared consumers.

    An undeclared router id is drift between the declaration and what the router serves, not a
    default -- guessing here would either hide a model or expose one, so it raises.
    """
    entry = visibility_models.get(router_id)
    if entry is None:
        raise SyncError(
            f"router model id {router_id!r} has no entry in the visibility declaration -- "
            "the declaration must carry every served key (`bun run check:recipes` gates this)"
        )
    return OPENWEBUI_CONSUMER in entry["consumers"]


def plan_is_active(
    desired_active: bool, existing: Optional[Dict[str, Any]]
) -> Tuple[str, Optional[bool]]:
    """Return (action, observed) for one row's `is_active`.

    action is "create" (no row exists, so there is nothing to transition FROM -- the caller
    prints the intended value and, on apply, re-reads the created row), "toggle" (a row exists
    and its observed value differs from the desired one) or "noop" (already correct). The route
    is a TOGGLE, not a setter, so "already correct" must issue NO call at all: posting a toggle
    at a row that already matches would flip it to the wrong value.
    """
    if existing is None:
        return "create", None
    observed = bool(existing.get("is_active"))
    return ("toggle" if observed != desired_active else "noop"), observed


def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = REQUEST_TIMEOUT_S,
) -> Tuple[int, Any]:
    """Issue an HTTP request and parse the response body as JSON.

    Returns (status_code, parsed_body). Never raises on non-2xx — the caller decides what a
    given status+body combination means (this fleet's standing rule: probe by body, not status
    code, so callers must be able to inspect error responses too).
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise SyncError(f"{method} {url} failed: {exc.reason}") from exc
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return status, parsed


def fetch_router_models(router_url: str) -> List[Dict[str, Any]]:
    """GET <router>/v1/models, return the `data` list. Raises SyncError on any shape surprise."""
    status, body = _http_json("GET", router_url.rstrip("/") + "/v1/models")
    if status != 200 or not isinstance(body, dict) or "data" not in body:
        raise SyncError(
            f"router capability discovery failed: GET {router_url}/v1/models "
            f"returned status={status} body={body!r}"
        )
    return body["data"]


def fetch_openwebui_rows(openwebui_url: str, api_key: str) -> Dict[str, Dict[str, Any]]:
    """Every Open WebUI workspace row, keyed by id: `/api/v1/models/base` ∪ `/api/v1/models/export`.

    Both admin routes are needed and neither is a superset of the other (live-verified
    2026-09-14): `/base` returns ONLY rows with `base_model_id: null`, so the derived effort rows
    are invisible there, while `/export` returns only the rows that HAVE a `base_model_id` (the
    derived ones), including inactive ones. Using `/base` alone would make the sync re-CREATE
    every derived row on every pass -- a silently non-idempotent run against a route that does
    not error on a duplicate id.

    A row only exists here once an admin (or this script) has created/customized that model at
    least once; an id absent from this map needs a `create`, not an `update`.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    for route in ("/api/v1/models/base", "/api/v1/models/export"):
        status, body = _http_json(
            "GET",
            openwebui_url.rstrip("/") + route,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if status != 200 or not isinstance(body, list):
            raise SyncError(
                f"Open WebUI model discovery failed: GET {openwebui_url}{route} "
                f"returned status={status} body={body!r}"
            )
        for row in body:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                rows[row["id"]] = row
    return rows


def build_description(entry: Dict[str, Any]) -> str:
    """Compose the human summary description from name + capability facts. Pure function of
    the router entry — no timestamps, no external state, so re-running produces an identical
    string until the underlying serving facts change."""
    name = entry.get("name") or entry["id"]
    ls = entry.get("meta", {}).get("llamaswap", {})
    serving = ls.get("serving", {})

    bits = [f"{serving.get('quant', 'unknown quant')} quant", f"{serving.get('kv_cache_type', 'f16')} KV cache"]
    if serving.get("flash_attention"):
        bits.append("flash-attention")
    if serving.get("mtp"):
        bits.append("MTP speculative decoding")
    if "moe_cpu_offload_layers" in serving:
        bits.append(f"{serving['moe_cpu_offload_layers']} MoE layers offloaded to CPU")
    if "ubatch" in serving:
        bits.append(f"-ub {serving['ubatch']}")
    serving_str = ", ".join(bits)

    tool_str = "yes" if ls.get("tool_calling") else "no"
    reasoning = ls.get("reasoning", "none")
    ctx = ls.get("context_length")

    modality_bits = []
    if "image" in ls.get("input_modalities", []):
        modality_bits.append("vision (image input)")
    output_modalities = ls.get("output_modalities", [])
    if output_modalities and output_modalities != ["text"]:
        modality_bits.append("output: " + "/".join(output_modalities))
    modality_str = f"; {', '.join(modality_bits)}" if modality_bits else ""

    return (
        f"{name} — {serving_str}. Tool calling: {tool_str}; reasoning: {reasoning}; "
        f"context: {ctx} tokens{modality_str}. Synced from inference-engine capability "
        "metadata (llama router /v1/models)."
    )


def compute_desired(entry: Dict[str, Any]) -> Dict[str, Any]:
    ls = entry.get("meta", {}).get("llamaswap", {})
    sampling = ls.get("sampling") or {}
    return {
        "vision": "image" in ls.get("input_modalities", []),
        "description": build_description(entry),
        "context_length": ls.get("context_length"),
        # Allowlist filter: only the numeric sampling keys, never `source` (audit E-4 pin). A
        # model with no `sampling` block (embed/rerank) yields {} here, so it is a no-op on the
        # params side below.
        "sampling_params": {
            key: sampling[key] for key in SAMPLING_PARAM_KEYS if key in sampling
        },
    }


def plan_change(
    router_id: str,
    router_name: str,
    desired: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
    *,
    policy: Optional[Dict[str, List[str]]] = None,
    arm: bool = False,
    base_model_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Tuple[str, List[Tuple[str, Any, Any]], Dict[str, Any]]:
    """Return (action, field_diffs, request_body) for one model row (base or derived).

    action is "create", "update", or "noop". field_diffs is a list of (field, old, new) for
    only the managed sub-fields that differ: the three `meta` fields, plus one entry per
    allowlisted sampling key (`params.<key>`) whose value changed, plus (when this is a derived
    row) `params.reasoning_effort`, plus (when `arm` is set) `meta.toolIds` and
    `meta.defaultFeatureIds`. request_body is the full JSON body to send to Open WebUI's
    create/update route if applying (unmodified fields round-tripped verbatim from `existing`,
    including every non-allowlisted `params` key).

    `arm` is the caller's answer to "is this row being driven ACTIVE?" -- a row driven inactive
    gets no arming change, because arming a row nobody can select is noise, and un-arming one is
    not this script's call either. `reasoning_effort` is set for derived rows ONLY: on a base row
    that key stays human-owned and is never read or written, so passing None here is not "leave
    the default", it is "this key does not exist for this row".
    """
    sampling_params: Dict[str, Any] = desired["sampling_params"]
    arm_tools = policy["arm_tool_ids"] if (policy and arm) else None
    arm_features = policy["arm_feature_ids"] if (policy and arm) else None

    if existing is None:
        meta: Dict[str, Any] = {
            "description": desired["description"],
            "capabilities": {"vision": desired["vision"]},
            "llamaswap": {"context_length": desired["context_length"]},
        }
        params = dict(sampling_params)
        if reasoning_effort is not None:
            params["reasoning_effort"] = reasoning_effort
        body = {
            "id": router_id,
            "name": router_name,
            "meta": meta,
            "params": params,
        }
        if base_model_id is not None:
            body["base_model_id"] = base_model_id
        diffs = [
            ("meta.capabilities.vision", None, desired["vision"]),
            ("meta.description", None, desired["description"]),
            ("meta.llamaswap.context_length", None, desired["context_length"]),
        ]
        for key, value in params.items():
            diffs.append((f"params.{key}", None, value))
        if policy is not None and arm_tools is not None and arm_features is not None:
            tool_ids = merge_arming(None, arm_tools, policy["retire_tool_ids"])
            feature_ids = merge_arming(None, arm_features, policy["retire_feature_ids"])
            # An empty result means "the policy arms nothing" -- write no key at all rather
            # than seeding a row with an empty list Open WebUI would then persist.
            if tool_ids:
                meta["toolIds"] = tool_ids
                diffs.append(("meta.toolIds", None, tool_ids))
            if feature_ids:
                meta["defaultFeatureIds"] = feature_ids
                diffs.append(("meta.defaultFeatureIds", None, feature_ids))
        return "create", diffs, body

    # Round-trip every existing field; only touch the three `meta` fields and the allowlisted
    # sampling keys in `params` that we own.
    meta = dict(existing.get("meta") or {})
    capabilities = dict(meta.get("capabilities") or {})
    llamaswap = dict(meta.get("llamaswap") or {})
    params = dict(existing.get("params") or {})

    diffs: List[Tuple[str, Any, Any]] = []
    old_vision = capabilities.get("vision")
    if old_vision != desired["vision"]:
        diffs.append(("meta.capabilities.vision", old_vision, desired["vision"]))
    old_description = meta.get("description")
    if old_description != desired["description"]:
        diffs.append(("meta.description", old_description, desired["description"]))
    old_context_length = llamaswap.get("context_length")
    if old_context_length != desired["context_length"]:
        diffs.append(
            ("meta.llamaswap.context_length", old_context_length, desired["context_length"])
        )
    # Per-key merge: only keys present in the router's sampling block are compared/written;
    # every other existing `params` key (human-owned) is never inspected or touched.
    owned_params: Dict[str, Any] = dict(sampling_params)
    if reasoning_effort is not None:
        # Derived rows only. On a base row this key is invisible to the sync.
        owned_params["reasoning_effort"] = reasoning_effort
    for key, value in owned_params.items():
        old_value = params.get(key)
        if old_value != value:
            diffs.append((f"params.{key}", old_value, value))

    # Arming: append-only merge, and only on a row being driven active.
    new_tool_ids: Optional[List[str]] = None
    new_feature_ids: Optional[List[str]] = None
    if policy is not None and arm_tools is not None and arm_features is not None:
        old_tool_ids = meta.get("toolIds")
        merged_tools = merge_arming(old_tool_ids, arm_tools, policy["retire_tool_ids"])
        if merged_tools != old_tool_ids and (merged_tools or isinstance(old_tool_ids, list)):
            new_tool_ids = merged_tools
            diffs.append(("meta.toolIds", old_tool_ids, merged_tools))
        old_feature_ids = meta.get("defaultFeatureIds")
        merged_features = merge_arming(
            old_feature_ids, arm_features, policy["retire_feature_ids"]
        )
        if merged_features != old_feature_ids and (
            merged_features or isinstance(old_feature_ids, list)
        ):
            new_feature_ids = merged_features
            diffs.append(("meta.defaultFeatureIds", old_feature_ids, merged_features))

    if not diffs:
        return "noop", diffs, {}

    capabilities["vision"] = desired["vision"]
    meta["capabilities"] = capabilities
    meta["description"] = desired["description"]
    llamaswap["context_length"] = desired["context_length"]
    meta["llamaswap"] = llamaswap
    if new_tool_ids is not None:
        meta["toolIds"] = new_tool_ids
    if new_feature_ids is not None:
        meta["defaultFeatureIds"] = new_feature_ids
    params.update(owned_params)

    body = {
        "id": existing["id"],
        "name": existing.get("name"),
        "meta": meta,
        "params": params,
    }
    # Round-trip the stored `base_model_id`; fall back to the one the caller planned, so a
    # derived row can never be updated into a base row (the update route replaces wholesale).
    stored_base = existing.get("base_model_id")
    if stored_base is None:
        stored_base = base_model_id
    if stored_base is not None:
        body["base_model_id"] = stored_base
    if existing.get("access_grants") is not None:
        body["access_grants"] = existing["access_grants"]
    return "update", diffs, body


def apply_change(
    openwebui_url: str, api_key: str, action: str, body: Dict[str, Any]
) -> Tuple[int, Any]:
    if action == "create":
        url = openwebui_url.rstrip("/") + "/api/v1/models/create"
    elif action == "update":
        model_id = urllib.parse.quote(body["id"], safe="")
        url = openwebui_url.rstrip("/") + f"/api/v1/models/model/update?id={model_id}"
    else:
        raise SyncError(f"apply_change called with non-mutating action {action!r}")
    return _http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
    )


def fetch_openwebui_row(
    openwebui_url: str, api_key: str, model_id: str
) -> Optional[Dict[str, Any]]:
    """Re-read ONE row via `GET /api/v1/models/model?id=<id>` (works for base and derived rows).

    Used after a `create` and after a `toggle`: a created row's `is_active` default is not
    assumed, and a toggle is verified by reading the value back rather than by trusting a 200
    (repo-wide rule: probe by response body, never status code). Returns None when the route
    answers anything other than 200-with-an-object (404 for an id that does not exist, 401 for a
    key that cannot see it) -- the caller reads None as "not readable back", which is a failure
    after a create, never a silent success.
    """
    quoted = urllib.parse.quote(model_id, safe="")
    status, body = _http_json(
        "GET",
        openwebui_url.rstrip("/") + f"/api/v1/models/model?id={quoted}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if status != 200 or not isinstance(body, dict):
        return None
    return body


def toggle_is_active(openwebui_url: str, api_key: str, model_id: str) -> Tuple[int, Any]:
    """POST `/api/v1/models/model/toggle?id=...` -- a FLIP, never a setter.

    The caller must already have established that the observed value differs from the desired
    one; issuing this at a row that already matches would move it the wrong way. An empty JSON
    object is sent as the body so the request carries a Content-Length; the route takes its
    argument in the query string (the shape `docs/model-capabilities.md` names, confirmed by the
    user-present reversible probe in WP-3's hold).
    """
    quoted = urllib.parse.quote(model_id, safe="")
    return _http_json(
        "POST",
        openwebui_url.rstrip("/") + f"/api/v1/models/model/toggle?id={quoted}",
        headers={"Authorization": f"Bearer {api_key}"},
        body={},
    )


def apply_model(
    openwebui_url: str,
    api_key: str,
    router_id: str,
    action: str,
    body: Dict[str, Any],
    active_action: str,
    wanted_active: bool,
) -> int:
    """Perform one model's create/update and, if needed, its `is_active` toggle.

    Returns the number of failures (0 or 1) and prints what it did. Order is load-bearing: the
    row must exist before it can be toggled, and a row this pass created is RE-READ rather than
    assumed to have defaulted either way. After a toggle the value is read back and compared --
    a 200 from a route that did not move the flag is exactly the "healthy service has told you
    nothing" failure this repo gates against.
    """
    if action != "noop":
        status, resp_body = apply_change(openwebui_url, api_key, action, body)
        if status not in (200, 201):
            print(f"    APPLY FAILED status={status} body={resp_body!r}")
            return 1
        print(f"    applied (status={status})")

    if action == "create":
        created = fetch_openwebui_row(openwebui_url, api_key, router_id)
        if created is None:
            print("    APPLY FAILED: created row not readable back from /api/v1/models/model")
            return 1
        observed = bool(created.get("is_active"))
        print(f"    is_active on the created row: {observed!r} (re-read, not assumed)")
        needs_toggle = observed != wanted_active
    else:
        needs_toggle = active_action == "toggle"

    if not needs_toggle:
        return 0

    status, resp_body = toggle_is_active(openwebui_url, api_key, router_id)
    if status not in (200, 201):
        print(f"    APPLY FAILED (toggle) status={status} body={resp_body!r}")
        return 1
    print(f"    toggled is_active (status={status}) body={resp_body!r}")

    after = fetch_openwebui_row(openwebui_url, api_key, router_id)
    observed_after = None if after is None else bool(after.get("is_active"))
    if observed_after != wanted_active:
        print(
            f"    APPLY FAILED (toggle): is_active reads back {observed_after!r}, "
            f"wanted {wanted_active!r}"
        )
        return 1
    print(f"    is_active verified {observed_after!r} on re-read")
    return 0


def process_row(
    args: argparse.Namespace,
    row_id: str,
    row_name: str,
    desired: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
    wanted_active: bool,
    policy: Dict[str, List[str]],
    *,
    label_create: str = "CREATE",
    base_model_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Tuple[str, int]:
    """Plan, print and (under `--apply`) perform one row. Returns (overall action, failures).

    Base rows and derived effort rows go through exactly this path -- same diff vocabulary, same
    "create, RE-READ, toggle only on an observed difference" ordering -- so a derived row can
    never drift onto a second, subtly different apply implementation. The only per-kind inputs
    are the create label, `base_model_id` and `reasoning_effort`.
    """
    action, diffs, body = plan_change(
        row_id,
        row_name,
        desired,
        existing,
        policy=policy,
        arm=wanted_active,
        base_model_id=base_model_id,
        reasoning_effort=reasoning_effort,
    )
    active_action, observed_active = plan_is_active(wanted_active, existing)

    # "no-op" must mean BOTH planes are in sync; a row whose metadata matches but whose
    # visibility does not is a TOGGLE, not an unchanged row.
    overall = action if action != "noop" else ("toggle" if active_action == "toggle" else "noop")

    label = {
        "create": label_create,
        "update": "UPDATE",
        "toggle": "TOGGLE",
        "noop": "no-op",
    }[overall]
    print(f"[{row_id}] {label}")
    for field, old_value, new_value in diffs:
        print(f"    {field}: {old_value!r} -> {new_value!r}")
    if active_action == "create":
        # No observed value exists to transition from -- print the INTENT, never a fabricated
        # `True -> False`.
        print(f"    is_active: (no row yet) -> {wanted_active!r}")
    elif active_action == "toggle":
        print(f"    is_active: {observed_active!r} -> {wanted_active!r}")
    if overall == "noop":
        print("    (already in sync)")

    failures = 0
    if args.apply:
        failures = apply_model(
            args.openwebui_url,
            args.openwebui_key,
            row_id,
            action,
            body,
            active_action,
            wanted_active,
        )
    print()
    return overall, failures


def is_retirement_candidate(
    row_id: str, row: Dict[str, Any], retire_legacy_ids: List[str]
) -> bool:
    """Does this script provably OWN this unmatched row, i.e. may it deactivate it?

    Three proofs, any one of which is enough: the row carries the `meta.llamaswap` marker this
    script writes on every create/update; its id has the derived `--effort-` shape only this
    script mints; or it is named in the policy's `retire_legacy_ids` (the escape hatch for a row
    a RETIRED writer created without the marker). Everything else is somebody else's row and is
    reported as UNMANAGED instead -- deactivating an admin's or another connection's model
    because the router does not serve it is exactly the over-reach this predicate exists to
    prevent.
    """
    if row_id in retire_legacy_ids:
        return True
    if DERIVED_ID_MARKER in row_id:
        return True
    return isinstance(row.get("meta"), dict) and "llamaswap" in row["meta"]


def retire_row(openwebui_url: str, api_key: str, row_id: str) -> int:
    """Drive one orphaned row to `is_active: false` via the toggle route. Returns failures.

    Only ever called for a row observed ACTIVE (the route is a flip, so calling it on an already
    inactive row would REVIVE the orphan), and the result is read back rather than trusted from
    the status code. This never deletes: hiding a model is deactivation.
    """
    status, resp_body = toggle_is_active(openwebui_url, api_key, row_id)
    if status not in (200, 201):
        print(f"    APPLY FAILED (retire) status={status} body={resp_body!r}")
        return 1
    after = fetch_openwebui_row(openwebui_url, api_key, row_id)
    observed_after = None if after is None else bool(after.get("is_active"))
    if observed_after is not False:
        print(
            f"    APPLY FAILED (retire): is_active reads back {observed_after!r}, wanted False"
        )
        return 1
    print("    retired: is_active verified False on re-read")
    return 0


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

    # Every id this sync manages -- base keys plus derivable effort ids -- computed against the
    # UNFILTERED router listing and the full ladder, BEFORE `--only` narrows the walk. Computing
    # it after the filter would make every other model's rows look like orphans, which is how a
    # `--only` run could retire half the workspace.
    managed_ids = set()
    for entry in router_models:
        key = entry.get("id")
        if not isinstance(key, str):
            continue
        managed_ids.add(key)
        for level in effort_levels_for(visibility_models, effort_levels, key):
            managed_ids.add(derived_row_id(key, level))

    if args.only:
        wanted = set(args.only)
        router_models = [m for m in router_models if m["id"] in wanted]
        missing = wanted - {m["id"] for m in router_models}
        for mid in sorted(missing):
            print(f"warning: --only {mid} not found in router /v1/models", file=sys.stderr)

    router_models = sorted(router_models, key=lambda m: m["id"])

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== sync-openwebui-capabilities: {mode} ===")
    print(f"router:     {args.router_url}")
    print(f"open webui: {args.openwebui_url}")
    print(f"visibility: {visibility_path}")
    print(f"policy:     {policy_path}")
    print(f"gateway:    {gateway_config_path}")
    print(f"models:     {len(router_models)}")
    print()

    counts = {"create": 0, "derive": 0, "update": 0, "toggle": 0, "noop": 0}
    failures = 0

    for entry in router_models:
        router_id = entry["id"]
        router_name = entry.get("name") or router_id
        ls = entry.get("meta", {}).get("llamaswap", {})
        if ls.get("type") not in (None, "model"):
            print(f"[{router_id}] skip: meta.llamaswap.type={ls.get('type')!r}, not a model")
            continue

        desired = compute_desired(entry)
        existing = existing_rows.get(router_id)
        try:
            wanted_active = desired_is_active(visibility_models, router_id)
        except SyncError as exc:
            # Drift, not a default: this model is skipped entirely, no call of any kind, and
            # the run ends non-zero.
            failures += 1
            print(f"[{router_id}] FAIL: {exc}")
            print()
            continue

        overall, row_failures = process_row(
            args, router_id, router_name, desired, existing, wanted_active, policy
        )
        counts[overall] += 1
        failures += row_failures

        # Derived effort rows ride the base's desired visibility: a base the declaration hides
        # takes its whole effort ladder with it (a visible `· xhigh` row pointing at a hidden
        # base is a picker entry for a model the operator just turned off).
        for level in effort_levels_for(visibility_models, effort_levels, router_id):
            row_id = derived_row_id(router_id, level)
            overall, row_failures = process_row(
                args,
                row_id,
                derived_row_name(router_name, level),
                desired,
                existing_rows.get(row_id),
                wanted_active,
                policy,
                label_create="DERIVE",
                base_model_id=router_id,
                reasoning_effort=level,
            )
            counts["derive" if overall == "create" else overall] += 1
            failures += row_failures

    # Retirement. `unmatched` is every Open WebUI row this sync does not currently manage; only
    # the subset it provably owns may be deactivated, and only when it is currently active (the
    # route is a flip -- toggling an already-inactive orphan would revive it).
    unmatched = sorted(set(existing_rows) - managed_ids)
    retire_active: List[str] = []
    retire_already_inactive: List[str] = []
    unmanaged: List[str] = []
    for row_id in unmatched:
        row = existing_rows[row_id]
        if not is_retirement_candidate(row_id, row, policy["retire_legacy_ids"]):
            unmanaged.append(row_id)
        elif bool(row.get("is_active")):
            retire_active.append(row_id)
        else:
            retire_already_inactive.append(row_id)

    if unmatched:
        print("Open WebUI rows with no current router match (never deleted -- deactivated only):")
        # `--only` narrows the WALK, so it must not act on anything outside it: the would-be
        # retirements are printed and skipped.
        skipped = "  (skipped: --only)" if args.only else ""
        for row_id in retire_active:
            print(f"    RETIRE {row_id}{skipped}")
        for row_id in retire_already_inactive:
            print(f"    retired (already inactive) {row_id}")
        for row_id in unmanaged:
            print(f"    UNMANAGED {row_id}")
        print()
        if args.apply and not args.only:
            for row_id in retire_active:
                print(f"[{row_id}] RETIRE")
                print("    is_active: True -> False")
                failures += retire_row(args.openwebui_url, args.openwebui_key, row_id)
                print()

    print(
        f"summary: {counts['create']} create, {counts['derive']} derive, "
        f"{counts['update']} update, {counts['toggle']} visibility toggle, "
        f"{counts['noop']} unchanged, {len(retire_active)} retire, "
        f"{len(unmanaged)} unmanaged"
    )
    if failures:
        print(f"error: {failures} model(s) failed -- see the FAIL / APPLY FAILED lines above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
