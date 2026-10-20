"""Repo-owned declaration inputs: the model catalog (visibility + effort ladder) and the
arming/retirement policy.

Every loader here is FAIL-CLOSED by contract: a missing, unparseable or unknown-schema
document raises `SyncError` rather than yielding an empty default. An empty-by-accident
declaration would silently deactivate every model, and an empty-by-accident policy would
silently un-arm every row -- both are "the run was healthy and did the wrong thing".

Default paths are resolved from THIS PACKAGE's location on disk (which is
`<repo>/scripts/openwebui_sync/`), never from the cwd, so an operator invoking the script
from anywhere reads the same two files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import SyncError
from .types import EffortLevels, Policy, VisibilityModels

# `<repo>/scripts/` and `<repo>/`, resolved from this file: `.../scripts/openwebui_sync/x.py`.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent

# The model catalog (`router/catalog/catalog.json`, `plans/serving-control-redesign.md` WP-3)
# carries the visibility this sync reads: every `claude-local` entry names its `llama/config.yaml`
# `modelKey` (which is also `/v1/models` `id` and the Open WebUI row id) and its `consumers`, and
# every llama key the gateway does not route sits in the top-level `exposures` map. The loader
# projects both back into the `{model key: {gateway_id, consumers}}` shape the planner reads.
CATALOG_SCHEMA = "model-catalog-v1"
VISIBILITY_PROVIDER = "claude-local"


# Closed vocabulary for llama-key visibility, canonical order. The catalog's own vocabulary also
# carries the gateway-only `router` tag, which never belongs on a llama-key entry: "router-only"
# is the COMPLEMENT — an entry with `[]` is reachable by id through the gateway and appears in
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
# the catalog entries' `effortKwargs` (nothing is forked here); this tuple only fixes
# the order, so the picker reads low-to-high instead of dict-insertion order. `none` is always
# present for a model that has a ladder at all -- it is the one level with a measured, binary
# effect (WP-1: 0 reasoning chars), so it is never conditional on the table listing it.
EFFORT_LEVEL_ORDER = ("none", "low", "medium", "high", "xhigh")


def default_catalog_path() -> Path:
    """`<repo>/router/catalog/catalog.json`, resolved from THIS FILE, never from the cwd."""
    return REPO_ROOT / "router" / "catalog" / "catalog.json"


def default_policy_path() -> Path:
    """`<repo>/scripts/openwebui-policy.json`, resolved from THIS FILE, never from the cwd."""
    return SCRIPTS_DIR / "openwebui-policy.json"


def load_catalog(path: Path) -> Dict[str, Any]:
    """Return the parsed catalog document. Fail-closed on a missing or unreadable file, invalid
    JSON, a non-object document, a schema literal this script does not understand, and a
    missing or non-mapping `providers`/`exposures`."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"cannot read model catalog {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError(f"model catalog {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SyncError(f"model catalog {path} is not a JSON object")
    if parsed.get("schema") != CATALOG_SCHEMA:
        raise SyncError(
            f"model catalog {path} carries schema {parsed.get('schema')!r}, "
            f"expected {CATALOG_SCHEMA!r}"
        )
    if not isinstance(parsed.get("providers"), dict):
        raise SyncError(f"model catalog {path} has no `providers` mapping")
    if not isinstance(parsed.get("exposures", {}), dict):
        raise SyncError(f"model catalog {path} has a non-mapping `exposures`")
    return parsed


def _catalog_entries(catalog: Dict[str, Any], provider: str) -> List[Dict[str, Any]]:
    block = catalog["providers"].get(provider)
    models = block.get("models") if isinstance(block, dict) else None
    return [e for e in models if isinstance(e, dict)] if isinstance(models, list) else []


def _checked_consumers(path: Path, model_key: str, consumers: Any) -> List[str]:
    if not isinstance(consumers, list):
        raise SyncError(f"model catalog {path}: {model_key!r} has no `consumers` list")
    unknown = [c for c in consumers if c not in CONSUMER_VOCABULARY]
    if unknown:
        raise SyncError(
            f"model catalog {path}: {model_key!r} carries unknown consumer "
            f"token(s) {', '.join(repr(u) for u in unknown)} -- the closed vocabulary is "
            f"{', '.join(CONSUMER_VOCABULARY)}"
        )
    return list(consumers)


def load_visibility(path: Path) -> VisibilityModels:
    """Return `{model key: {gateway_id, consumers}}` projected from the catalog: every
    `claude-local` entry carrying a `modelKey` (gateway_id = its id), then every `exposures`
    record (gateway_id = None). Fail-closed on every malformed shape.

    Raises SyncError for everything `load_catalog` refuses, plus: an exposure that is not a
    mapping, a `consumers` value that is not a list, any consumer token outside
    CONSUMER_VOCABULARY, and a model key declared twice. Treating any of these as "no
    declaration" would silently deactivate every model, which is why none of them degrades to a
    default.
    """
    catalog = load_catalog(path)
    models: VisibilityModels = {}
    for entry in _catalog_entries(catalog, VISIBILITY_PROVIDER):
        model_key = entry.get("modelKey")
        if model_key is None:
            continue
        if not isinstance(model_key, str) or model_key in models:
            raise SyncError(f"model catalog {path}: bad or duplicate modelKey {model_key!r}")
        gateway_id = entry.get("id")
        models[model_key] = {
            "gateway_id": gateway_id if isinstance(gateway_id, str) else None,
            "consumers": _checked_consumers(path, model_key, entry.get("consumers")),
        }
    for model_key, record in catalog.get("exposures", {}).items():
        if not isinstance(record, dict):
            raise SyncError(f"model catalog {path}: exposure {model_key!r} is not a mapping")
        if model_key in models:
            raise SyncError(f"model catalog {path}: model key {model_key!r} declared twice")
        models[model_key] = {
            "gateway_id": None,
            "consumers": _checked_consumers(path, model_key, record.get("consumers")),
        }
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
    """Return {gateway id: [effort level, ...]} derived from the catalog entries' `effortKwargs`.

    The ladder is NOT re-declared for Open WebUI: it is read off the table the Claude Code leg
    already uses, keyed by gateway id, by walking every catalog provider's entries for an
    `effortKwargs` mapping (the provider is not hardcoded). A level's Open WebUI value is the
    `reasoning_effort` string that entry maps to -- so `high -> {reasoning_effort: medium}` and
    `max -> {reasoning_effort: xhigh}` collapse onto `medium`/`xhigh` rather than forking two
    more rows, and an entry carrying only `enable_thinking` contributes nothing (its effect is
    `none`, which is always present anyway).

    A catalog with no `effortKwargs` anywhere yields `{}` -- that means "no derived rows
    anywhere", which is a valid state, not an error. An unreadable or malformed catalog IS one.
    """
    catalog = load_catalog(path)
    levels: EffortLevels = {}
    for provider in catalog["providers"]:
        for entry in _catalog_entries(catalog, provider):
            gateway_id = entry.get("id")
            by_level = entry.get("effortKwargs")
            if not isinstance(gateway_id, str) or not isinstance(by_level, dict):
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
            f"router model id {router_id!r} has no entry in the visibility declaration "
            "(the model catalog's claude-local modelKeys + exposures) -- "
            "the declaration must carry every served key (`bun run check:recipes` gates this)"
        )
    return OPENWEBUI_CONSUMER in entry["consumers"]
