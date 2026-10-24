"""Per-row execution and operator output: the planner's decisions carried out through the client.

This is the only layer that both decides and acts, and it is deliberately thin: it plans one
row, prints the diff a human reads, and -- under `--apply` only -- hands the planned body to
the client. Base rows and derived effort rows go through exactly this path, so a derived row
can never drift onto a second, subtly different apply implementation.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from . import client, planner
from .declarations import desired_is_active, effort_levels_for
from .errors import SyncError
from .types import (
    DesiredRow,
    EffortLevels,
    OpenWebUIRow,
    OpenWebUIRows,
    Policy,
    RetirementPlan,
    RouterEntry,
    RowOutcome,
    VisibilityModels,
)

ACTION_LABELS = {
    "create": "CREATE",
    "derive": "DERIVE",
    "update": "UPDATE",
    "toggle": "TOGGLE",
    "noop": "no-op",
}


def process_row(
    row_id: str,
    row_name: str,
    desired: DesiredRow,
    existing: Optional[OpenWebUIRow],
    wanted_active: bool,
    policy: Policy,
    *,
    apply: bool,
    openwebui_url: str,
    api_key: str,
    label_create: str = "CREATE",
    base_model_id: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    quiet: bool = False,
) -> RowOutcome:
    """Plan, print and (under `--apply`) perform one row. Returns its action and failure count.

    The only per-kind inputs are the create label, `base_model_id` and `reasoning_effort`; the
    diff vocabulary and the "create, RE-READ, toggle only on an observed difference" ordering
    are identical for base and derived rows.
    """
    action, diffs, body = planner.plan_change(
        row_id,
        row_name,
        desired,
        existing,
        policy=policy,
        arm=wanted_active,
        base_model_id=base_model_id,
        reasoning_effort=reasoning_effort,
    )
    active_action, observed_active = planner.plan_is_active(wanted_active, existing)
    overall = planner.overall_action(action, active_action)

    # `--quiet` drops only a block whose whole outcome is a no-op (apply_model does nothing for
    # one); every block that changes, creates or fails prints exactly as without the flag.
    hush = quiet and overall == "noop"
    if not hush:
        label = label_create if overall == "create" else ACTION_LABELS[overall]
        print(f"[{row_id}] {label}")
        for field, old_value, new_value in diffs:
            print(f"    {field}: {old_value!r} -> {new_value!r}")
        if active_action == "create":
            # No observed value exists to transition from -- print the INTENT, never a
            # fabricated `True -> False`.
            print(f"    is_active: (no row yet) -> {wanted_active!r}")
        elif active_action == "toggle":
            print(f"    is_active: {observed_active!r} -> {wanted_active!r}")
        if overall == "noop":
            print("    (already in sync)")

    failures = 0
    if apply:
        failures = client.apply_model(
            openwebui_url,
            api_key,
            row_id,
            action,
            body,
            active_action,
            wanted_active,
        )
    if not hush:
        print()
    return RowOutcome(overall, failures)


def sync_model(
    entry: RouterEntry,
    *,
    visibility_models: VisibilityModels,
    effort_levels: EffortLevels,
    existing_rows: OpenWebUIRows,
    policy: Policy,
    apply: bool,
    openwebui_url: str,
    api_key: str,
    quiet: bool = False,
) -> Tuple[Dict[str, int], int]:
    """Sync one router model: its base row plus its whole derived effort ladder.

    Returns ({action: count}, failures). A router entry that is not a servable model is skipped
    with no call of any kind, and an id the visibility declaration does not carry is DRIFT: the
    model is skipped entirely and the run ends non-zero rather than guessing a default.
    """
    counts: Dict[str, int] = {}
    router_id = entry["id"]
    router_name = entry.get("name") or router_id
    llamaswap = entry.get("meta", {}).get("llamaswap", {})
    if llamaswap.get("type") not in (None, "model"):
        if quiet:
            return counts, 0
        print(f"[{router_id}] skip: meta.llamaswap.type={llamaswap.get('type')!r}, not a model")
        return counts, 0

    desired = planner.compute_desired(entry)
    try:
        wanted_active = desired_is_active(visibility_models, router_id)
    except SyncError as exc:
        print(f"[{router_id}] FAIL: {exc}")
        print()
        return counts, 1

    failures = 0
    outcome = process_row(
        router_id,
        router_name,
        desired,
        existing_rows.get(router_id),
        wanted_active,
        policy,
        apply=apply,
        openwebui_url=openwebui_url,
        api_key=api_key,
        quiet=quiet,
    )
    counts[outcome.action] = counts.get(outcome.action, 0) + 1
    failures += outcome.failures

    # Derived effort rows ride the base's desired visibility: a base the declaration hides takes
    # its whole effort ladder with it (a visible `· xhigh` row pointing at a hidden base is a
    # picker entry for a model the operator just turned off).
    for level in effort_levels_for(visibility_models, effort_levels, router_id):
        row_id = planner.derived_row_id(router_id, level)
        outcome = process_row(
            row_id,
            planner.derived_row_name(router_name, level),
            desired,
            existing_rows.get(row_id),
            wanted_active,
            policy,
            apply=apply,
            openwebui_url=openwebui_url,
            api_key=api_key,
            label_create="DERIVE",
            base_model_id=router_id,
            reasoning_effort=level,
            quiet=quiet,
        )
        key = "derive" if outcome.action == "create" else outcome.action
        counts[key] = counts.get(key, 0) + 1
        failures += outcome.failures

    return counts, failures


def retire_rows(
    plan: RetirementPlan,
    *,
    apply: bool,
    only: Optional[List[str]],
    openwebui_url: str,
    api_key: str,
    quiet: bool = False,
) -> int:
    """Report the unmanaged-row partition and, outside `--only`, retire the rows this sync owns.

    Nothing here ever deletes: hiding a model is deactivation. `--only` narrows the WALK, so it
    must not act on anything outside it -- the would-be retirements are printed and skipped.
    With `quiet`, the section is omitted unless it holds a row being retired (rows that are only
    already-inactive or UNMANAGED are standing state, not news).
    """
    if not plan:
        return 0

    if not (quiet and not plan.active):
        _print_unmatched(plan, only)

    failures = 0
    if apply and not only:
        for row_id in plan.active:
            print(f"[{row_id}] RETIRE")
            print("    is_active: True -> False")
            failures += client.retire_row(openwebui_url, api_key, row_id)
            print()
    return failures


def _print_unmatched(plan: RetirementPlan, only: Optional[List[str]]) -> None:
    print("Open WebUI rows with no current router match (never deleted -- deactivated only):")
    skipped = "  (skipped: --only)" if only else ""
    for row_id in plan.active:
        print(f"    RETIRE {row_id}{skipped}")
    for row_id in plan.already_inactive:
        print(f"    retired (already inactive) {row_id}")
    for row_id in plan.unmanaged:
        print(f"    UNMANAGED {row_id}")
    print()
