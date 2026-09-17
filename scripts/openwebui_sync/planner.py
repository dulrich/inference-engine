"""Pure planning: what a row SHOULD look like, and what changing it would take.

Nothing in this module performs I/O, reads a file, or knows an endpoint shape. Every function
here is a pure function of its arguments, which is what makes the create/update/toggle/retire
decision matrix and the managed-field merge semantics testable against fixtures alone.

The managed-field contract lives here and is a cross-consumer API: this sync owns exactly the
three `meta` sub-fields, the allowlisted sampling keys inside `params`, `reasoning_effort` on
DERIVED rows only, and the two arming lists on rows driven ACTIVE. Every other field on an
existing row round-trips verbatim, because Open WebUI's update route replaces `name`/`meta`/
`params` wholesale rather than patching.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .declarations import effort_levels_for
from .types import (
    DesiredRow,
    EffortLevels,
    FieldDiff,
    OpenWebUIRow,
    OpenWebUIRows,
    Policy,
    RequestBody,
    RetirementPlan,
    RouterEntry,
    VisibilityModels,
)

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


# Stored-identifier shape, append-only under the alias contract (`llama/config.yaml` header).
DERIVED_ID_MARKER = "--effort-"


# U+00B7 MIDDLE DOT with a space either side -- a display-name separator, never an id separator.
DERIVED_NAME_SEPARATOR = " · "


def derived_row_id(model_key: str, level: str) -> str:
    """The stored Open WebUI id for one derived effort row (append-only: never re-spell this)."""
    return f"{model_key}{DERIVED_ID_MARKER}{level}"


def derived_row_name(router_name: str, level: str) -> str:
    """The picker display name for one derived effort row: `<router display name> · <level>`."""
    return f"{router_name}{DERIVED_NAME_SEPARATOR}{level}"


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


def plan_is_active(
    desired_active: bool, existing: Optional[OpenWebUIRow]
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


def build_description(entry: RouterEntry) -> str:
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


def compute_desired(entry: RouterEntry) -> DesiredRow:
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
    desired: DesiredRow,
    existing: Optional[OpenWebUIRow],
    *,
    policy: Optional[Policy] = None,
    arm: bool = False,
    base_model_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
) -> Tuple[str, List[FieldDiff], RequestBody]:
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

    diffs: List[FieldDiff] = []
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


def is_retirement_candidate(
    row_id: str, row: OpenWebUIRow, retire_legacy_ids: List[str]
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


def managed_row_ids(
    router_models: Iterable[RouterEntry],
    visibility_models: VisibilityModels,
    effort_levels: EffortLevels,
) -> Set[str]:
    """Every Open WebUI id this sync manages: base keys plus every derivable effort id.

    The caller MUST compute this against the UNFILTERED router listing and the full ladder,
    before `--only` narrows the walk: computed after the filter, every other model's rows would
    look like orphans, which is how an `--only` run could retire half the workspace.
    """
    managed: Set[str] = set()
    for entry in router_models:
        key = entry.get("id")
        if not isinstance(key, str):
            continue
        managed.add(key)
        for level in effort_levels_for(visibility_models, effort_levels, key):
            managed.add(derived_row_id(key, level))
    return managed


def classify_unmatched_rows(
    existing_rows: OpenWebUIRows, managed: Set[str], retire_legacy_ids: List[str]
) -> RetirementPlan:
    """Partition every unmanaged Open WebUI row into retire / already-inactive / not-ours.

    Only the subset this sync provably owns may ever be deactivated, and only while it is
    currently ACTIVE -- the Open WebUI route is a flip, so toggling an already-inactive orphan
    would revive it. Everything else is reported and left alone.
    """
    plan = RetirementPlan()
    for row_id in sorted(set(existing_rows) - managed):
        row = existing_rows[row_id]
        if not is_retirement_candidate(row_id, row, retire_legacy_ids):
            plan.unmanaged.append(row_id)
        elif bool(row.get("is_active")):
            plan.active.append(row_id)
        else:
            plan.already_inactive.append(row_id)
    return plan


def overall_action(action: str, active_action: str) -> str:
    """Fold the metadata plan and the visibility plan into the one action the operator sees.

    "no-op" must mean BOTH planes are in sync; a row whose metadata matches but whose
    visibility does not is a TOGGLE, not an unchanged row.
    """
    if action != "noop":
        return action
    return "toggle" if active_action == "toggle" else "noop"
