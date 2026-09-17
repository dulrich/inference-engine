"""Repo-owned declaration inputs: visibility, arming/retirement policy, effort ladder.

Every loader here is FAIL-CLOSED by contract: a missing, unparseable or unknown-schema
document raises `SyncError` rather than yielding an empty default. An empty-by-accident
declaration would silently deactivate every model, and an empty-by-accident policy would
silently un-arm every row -- both are "the run was healthy and did the wrong thing".

Default paths are resolved from THIS PACKAGE's location on disk (which is
`<repo>/scripts/openwebui_sync/`), never from the cwd, so an operator invoking the script
from anywhere reads the same three files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from .errors import SyncError
from .types import EffortLevels, Policy, VisibilityModels

# `<repo>/scripts/` and `<repo>/`, resolved from this file: `.../scripts/openwebui_sync/x.py`.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

# The visibility declaration (`plans/bench-dashboard-serving-control.md` WP-3). Keyed by
# `llama/config.yaml` model key — which is also `/v1/models` `id` and the Open WebUI row id, so
# no crosswalk is needed on this side (`gateway_id` is the Claude Code picker's business).
VISIBILITY_SCHEMA = "serving-visibility-v1"


# Closed vocabulary, canonical order. There is deliberately no `router` token: "router-only" is
# the COMPLEMENT — an entry with `[]` is reachable by id through the gateway and appears in
# neither the Claude Code picker nor Open WebUI's active set.
CONSUMER_VOCABULARY = ("cc", "openwebui")


OPENWEBUI_CONSUMER = "openwebui"


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


def default_visibility_path() -> Path:
    """`<repo>/router/visibility.json`, resolved from THIS FILE, never from the cwd."""
    return REPO_ROOT / "router" / "visibility.json"


def default_policy_path() -> Path:
    """`<repo>/scripts/openwebui-policy.json`, resolved from THIS FILE, never from the cwd."""
    return SCRIPTS_DIR / "openwebui-policy.json"


def default_gateway_config_path() -> Path:
    """`<repo>/router/gateway.config.json`, resolved from THIS FILE, never from the cwd."""
    return REPO_ROOT / "router" / "gateway.config.json"


def load_visibility(path: Path) -> VisibilityModels:
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


def load_policy(path: Path) -> Policy:
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
    policy: Policy = {}
    for field in POLICY_LIST_FIELDS:
        value = parsed.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise SyncError(
                f"Open WebUI policy {path}: {field!r} must be a list of strings, got {value!r}"
            )
        policy[field] = list(value)
    return policy


def load_effort_levels(path: Path) -> EffortLevels:
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

    levels: EffortLevels = {}
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


def gateway_id_for(visibility_models: VisibilityModels, router_id: str) -> Optional[str]:
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
    visibility_models: VisibilityModels,
    effort_levels: EffortLevels,
    router_id: str,
) -> List[str]:
    """The effort levels this model key gets derived rows for ([] when it has no ladder)."""
    gateway_id = gateway_id_for(visibility_models, router_id)
    if gateway_id is None:
        return []
    return list(effort_levels.get(gateway_id, []))


def desired_is_active(visibility_models: VisibilityModels, router_id: str) -> bool:
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
