#!/usr/bin/env python3
"""The dispatcher's six phases and its crash recovery (WP-R4 of
`plans/repository-tooling-structural-remediation.md`; review finding F9 -- file size).

One pass = ingest -> act -> terminalize -> janitor -> publish -> prune, in that order, driven by
`dispatch_pass` at the bottom of this file. Extracted verbatim from `control_dispatcher.py`'s
phase banners; `control_dispatcher.py` keeps the filesystem-contract docstring, the wrapper
helper subcommands, and the CLI.

**Why the evidence writers live HERE and not in `control_store.py`.** The core safety property of
this module is a three-step ordering that has to be readable in one place:

    action (evidence)  ->  effect  ->  disposition

`write_action` records what the dispatcher is ABOUT to do, before it does it. Then the effect
lands (a stop marker, an armed marker, a launch). Only then does `write_disposition` record the
outcome. A crash between any two steps is recoverable precisely because the earlier steps are
already durable: a replayed pass reads the recorded action and resumes from THAT, never
re-deriving the target from whatever the live state happens to look like now. Splitting the two
writers into a "file primitives" module would have left the reader of `_act_stop` looking at
three imported names with nothing local to tell them the order matters. Both writers are defined
immediately below, both are write-once (`if dst.exists(): return`), and every call site that
orders them is in this same file -- `_act_stop`, `_act_cancel`, `_act_start`, `terminalize_phase`,
`janitor_phase`. Read them top to bottom and the ordering is the shape of the code.

The same reasoning keeps `janitor_phase`'s recovery sequence intact and local: orphan scratch
teardown -> router restart -> compose `last-run.json` `disposition:"crashed"` -> remove
`live.json`, in that order, so a crash mid-recovery re-runs a prefix of it rather than losing the
evidence that a run died at all. That whole sequence runs under `cron.lock` -- the single
GPU-ownership lock `run-window.sh` and the TTS window (`tts/tier/window.sh`) hold -- acquired
non-blocking and held until `live.json` is gone; a busy lock defers recovery to a later pass with
the evidence intact, so the janitor never `docker start`s llama on a GPU another owner holds.

**No live operation happens unless a caller explicitly supplies live-capable collaborators.**
`dispatch_pass`'s `launch_fn`/`find_pid_fn`/`teardown_fn`/`router_start_fn`/`boot_id_fn`/
`pgid_alive_fn`/`pid_alive_fn` all default to real, Docker/process-touching implementations, and
the test suite always injects fakes -- it never spawns a real process, never calls `docker`,
never reads real `/proc`, and never leaves a tempdir. `acquire_cron_lock_fn` defaults to a real
non-blocking flock on `paths.cron_lock`; tests exercise it against a tempdir lock file only.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sweep
from control_process import default_pid_alive, group_alive, pgid_alive, read_boot_id
from control_protocol import (
    GRACE_SECONDS,
    PRUNE_AGE_SECONDS,
    REQUEST_BASENAME_RE,
    VALID_ACTIONS,
    WIRE_STATES,
    IngestedRequest,
    iso_z,
    parse_iso,
    validate_request_body,
)
from control_store import (
    MODULE_DIR,
    Paths,
    atomic_touch,
    atomic_write_json,
    flock_nonblocking_acquire,
    flock_release,
    probe_lock_free,
    read_json_safe,
    safe_read_request_bytes,
)

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_ROUTER_CONTAINER",
    "DEFAULT_SCRATCH_CONTAINER",
    "act_phase",
    "compose_last_run",
    "compose_status",
    "dispatch_pass",
    "ingest_pass",
    "janitor_phase",
    "map_last_run_disposition",
    "project_request_state",
    "prune_phase",
    "terminalize_phase",
    "write_action",
    "write_disposition",
]

DEFAULT_PORT = 8899
DEFAULT_SCRATCH_CONTAINER = "bakeoff-scratch"
DEFAULT_ROUTER_CONTAINER = sweep.DEFAULT_CONTAINER_NAME


# -------------------------------------------------------------------------------------------
# The dispatcher's own evidence channel. `write_action` is step 1 of the action -> effect ->
# disposition ordering; `write_disposition` is step 3. Both are write-once by construction, which
# is what makes replay idempotent: a second pass over the same request finds the file already
# there and returns without re-deriving anything.
# -------------------------------------------------------------------------------------------


def write_disposition(paths: Paths, request_id: str, state: str, reason: str | None, now_fn: Callable[[], datetime] | None = None) -> None:
    now_fn = now_fn or (lambda: datetime.now(UTC))
    dst = paths.dispositions_dir / f"{request_id}.json"
    if dst.exists():
        return  # write-once, retained forever -- never silently overwritten.
    body = {"request_id": request_id, "state": state, "reason": reason, "decided_at": iso_z(now_fn())}
    atomic_write_json(dst, body)


def write_action(
    paths: Paths, request_id: str, action: str,
    target_invocation_id: str | None, target_request_id: str | None,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    now_fn = now_fn or (lambda: datetime.now(UTC))
    dst = paths.actions_dir / f"{request_id}.json"
    if dst.exists():
        return  # replay resumes from what's already recorded -- never re-derived/overwritten.
    body = {
        "request_id": request_id, "action": action,
        "target_invocation_id": target_invocation_id, "target_request_id": target_request_id,
        "recorded_at": iso_z(now_fn()),
    }
    atomic_write_json(dst, body)


# -------------------------------------------------------------------------------------------
# Phase 1: Ingest (hardened, fail-closed).
# -------------------------------------------------------------------------------------------


def ingest_pass(paths: Paths, queues_dir: Path, now_fn: Callable[[], datetime] | None = None) -> tuple[list[IngestedRequest], int]:
    """Enumerates `requests/`, hardens+validates each unprocessed matching file, and returns
    `(pending, ignored_invalid_names)`. `pending` is sorted `(requested_at, request_id)` ascending
    (cycle-2 E1 -- directory enumeration order is never load-bearing). A basename that doesn't
    match the id grammar (including WP-4's transient `.`-prefixed `*.tmp` exclusive-creation
    files) is IGNORED, counted, never dispositioned -- there's no trustworthy id to disposition
    against. A matching basename whose content fails hardening or schema validation IS
    dispositioned `rejected(malformed)` using the basename-derived id -- including WP-R4's
    version check (`control_protocol.REQUEST_SCHEMA_REJECTION`), which is an ordinary rejection
    on this same path, never a crash and never a silent skip. Idempotence: a basename whose
    `dispositions/<id>.json` already exists is skipped entirely -- never re-derived from an
    in-memory set."""
    ignored = 0
    pending: list[IngestedRequest] = []
    try:
        names = sorted(os.listdir(paths.requests_dir))
    except OSError:
        names = []
    for name in names:
        if not REQUEST_BASENAME_RE.match(name):
            ignored += 1
            continue
        basename_id = name[: -len(".json")]
        if (paths.dispositions_dir / f"{basename_id}.json").exists():
            continue
        data = safe_read_request_bytes(paths.requests_dir / name)
        if data is None:
            write_disposition(paths, basename_id, "rejected", "malformed: hardening check failed", now_fn)
            continue
        validation = validate_request_body(basename_id, data, queues_dir)
        if validation.request is None:
            write_disposition(paths, basename_id, "rejected", validation.reason, now_fn)
            continue
        pending.append(validation.request)
    pending.sort(key=lambda r: (r.requested_at, r.request_id))
    return pending, ignored


# -------------------------------------------------------------------------------------------
# Phase 2: Act -- evidence-before-effect, effect-before-disposition, idempotent replay.
# -------------------------------------------------------------------------------------------


def _act_arm(paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime]) -> None:
    atomic_touch(paths.armed_marker)                                  # effect
    write_disposition(paths, req.request_id, "done", None, now_fn)    # disposition


def _act_disarm(paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime]) -> None:
    try:
        paths.armed_marker.unlink()                                   # effect
    except FileNotFoundError:
        pass
    write_disposition(paths, req.request_id, "done", None, now_fn)    # disposition


def _act_stop(
    paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool],
) -> None:
    existing_action = read_json_safe(paths.actions_dir / f"{req.request_id}.json")
    if existing_action is not None:
        # Replay rule (matrix row 16): resume from the RECORDED target, never re-derive from
        # current live state -- the marker path is bound to the recorded invocation id, so a
        # replayed/stale marker can never retarget a later run.
        target_inv = existing_action.get("target_invocation_id")
        if target_inv:
            live = read_json_safe(paths.live_path)
            still_live = (
                live is not None and live.get("invocation_id") == target_inv
                and group_alive(live, boot_id_fn, pgid_alive_fn)
            )
            if still_live:
                atomic_touch(paths.stop_marker(target_inv))
        write_disposition(paths, req.request_id, "done", "stop-delivered", now_fn)
        return

    live = read_json_safe(paths.live_path)
    admissible = (
        live is not None and live.get("phase") == "running"
        and group_alive(live, boot_id_fn, pgid_alive_fn)
    )
    if not admissible:
        write_disposition(paths, req.request_id, "rejected", "no-live-run", now_fn)
        return
    target_inv = live.get("invocation_id")
    target_req = live.get("request_id")
    # FIRST the action (evidence), THEN the marker (effect), THEN the disposition -- load-bearing
    # order (cycle-2 B2). Crash after line 1: the replay branch above recreates the marker from
    # the recorded target. Crash after line 2: the replay branch re-runs a no-op touch and then
    # dispositions. Never the reverse, or a lost pass would strand a live run with no stop.
    write_action(paths, req.request_id, "stop", target_inv, target_req, now_fn)   # 1. evidence
    atomic_touch(paths.stop_marker(target_inv))                                   # 2. effect
    write_disposition(paths, req.request_id, "done", "stop-delivered", now_fn)    # 3. disposition


def _finish_cancel(paths: Paths, cancel_id: str, target_request_id: str, now_fn: Callable[[], datetime]) -> None:
    claim_exists = (paths.claims_dir / f"{target_request_id}.json").exists()
    if claim_exists:
        write_disposition(paths, cancel_id, "rejected", "already-claimed", now_fn)
    else:
        write_disposition(paths, target_request_id, "canceled", None, now_fn)
        write_disposition(paths, cancel_id, "done", None, now_fn)


def _act_cancel(paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime]) -> None:
    existing_action = read_json_safe(paths.actions_dir / f"{req.request_id}.json")
    if existing_action is not None:
        target_request_id = existing_action.get("target_request_id")
        if target_request_id:
            _finish_cancel(paths, req.request_id, target_request_id, now_fn)
        return

    target_request_id = req.target_request_id
    # `requests/` is the CONTAINER-WRITABLE drop-box, outside the trust boundary -- a plain
    # `read_json_safe` (bare `open()`, follows symlinks) would let a symlink planted at the
    # target path read an arbitrary host path into this decision. Same hardened read every other
    # drop-box access uses (`safe_read_request_bytes`: lstat regular-file, O_NOFOLLOW, 8 KiB
    # bound) -- None or a parse failure is treated as an invalid target, same fail-closed
    # `rejected(bad-target)` outcome as a missing/wrong-action/already-terminal target.
    #
    # This read deliberately does NOT re-run `validate_request_body`: it needs exactly two facts
    # (the target names a `start`, and the target is not already terminal). A target carrying an
    # unknown `schema` cannot slip through here, because phase 1 runs before phase 2 in the same
    # pass and will already have written its `rejected` disposition -- which this function's own
    # `target_terminal` check then reads as `bad-target`.
    target_body = None
    if target_request_id:
        target_data = safe_read_request_bytes(paths.requests_dir / f"{target_request_id}.json")
        if target_data is not None:
            try:
                parsed = json.loads(target_data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            target_body = parsed if isinstance(parsed, dict) else None
    target_terminal = (
        (paths.dispositions_dir / f"{target_request_id}.json").exists() if target_request_id else True
    )
    valid_target = (
        target_request_id is not None
        and target_body is not None
        and target_body.get("action") == "start"
        and not target_terminal
    )
    if not valid_target:
        write_disposition(paths, req.request_id, "rejected", "bad-target", now_fn)
        return
    # Same ordering as `_act_stop`: the action is durable BEFORE `_finish_cancel` writes either
    # disposition, so a replayed pass resumes from the recorded target rather than re-reading the
    # (container-writable) drop-box a second time.
    write_action(paths, req.request_id, "cancel", None, target_request_id, now_fn)  # 1. evidence
    _finish_cancel(paths, req.request_id, target_request_id, now_fn)                # 2+3.


def _act_start(
    paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime],
    probe_cron_lock_fn: Callable[[], bool], launch_fn: Callable[[IngestedRequest], None],
) -> None:
    claim_path = paths.claims_dir / f"{req.request_id}.json"
    if claim_path.exists():
        return  # already claimed+launched in a prior pass -- idempotent no-op.
    if not probe_cron_lock_fn():
        write_disposition(paths, req.request_id, "rejected", "busy", now_fn)
        return
    claim = {
        "request_id": req.request_id, "claimed_at": iso_z(now_fn()),
        "queue_stem": req.queue_stem, "minutes": req.minutes,
    }
    # The claim is this action's evidence, and it is written BEFORE the launch (req 1) for the
    # same reason `_act_stop` writes its action first: the durable record that a start was
    # admitted must exist even if the launch itself dies outright. `start` is the one action with
    # no immediate disposition -- `terminalize_phase` (phase 3) writes it later, from the
    # wrapper's own result evidence rather than from this pass's optimism.
    atomic_write_json(claim_path, claim)    # 1. evidence
    launch_fn(req)                          # 2. effect


def act_phase(
    pending: list[IngestedRequest], paths: Paths, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool],
    probe_cron_lock_fn: Callable[[], bool], launch_fn: Callable[[IngestedRequest], None],
) -> None:
    for req in pending:
        if req.action == "arm":
            _act_arm(paths, req, now_fn)
        elif req.action == "disarm":
            _act_disarm(paths, req, now_fn)
        elif req.action == "stop":
            _act_stop(paths, req, now_fn, boot_id_fn, pgid_alive_fn)
        elif req.action == "cancel":
            _act_cancel(paths, req, now_fn)
        elif req.action == "start":
            _act_start(paths, req, now_fn, probe_cron_lock_fn, launch_fn)


# -------------------------------------------------------------------------------------------
# Phase 3: Terminalize from evidence, never from time alone.
# -------------------------------------------------------------------------------------------


def _claim_group_alive(paths: Paths, request_id: str, boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool]) -> bool:
    """No result file exists at all for this claim yet -- the only place process identity could
    plausibly have landed is `live.json`, so this checks whether it (by some race) already names
    this exact request."""
    live = read_json_safe(paths.live_path)
    if live is None or live.get("request_id") != request_id:
        return False
    return group_alive(live, boot_id_fn, pgid_alive_fn)


def terminalize_phase(
    paths: Paths, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool],
) -> tuple[list[str], set[str]]:
    """Walks every claim without a disposition yet and dispositions it from EVIDENCE (never bare
    elapsed time alone): `busy` result -> `launch-failed(busy)` immediately (no grace -- the
    wrapper already reported it); no result after grace AND no live group for it ->
    `launch-failed(no-launch-evidence)`; `wrapper-started` result whose recorded group is dead
    after grace -> `launch-failed(pre-run-crash)` (using the result's OWN identity fields); a
    result at `exited` -> `done` (the run's internal quality is a separate concern, surfaced via
    `last-run.json`'s disposition, not this request-lifecycle disposition). Evidence arriving for
    an ALREADY-terminalized (specifically already `launch-failed`) id is never silently
    reconciled -- it's returned as a `late-launch-after-terminal` anomaly instead. Returns
    `(anomaly_strings, late_launch_request_ids)`."""
    anomalies: list[str] = []
    late_launch_ids: set[str] = set()
    now = now_fn()
    try:
        claim_files = sorted(paths.claims_dir.glob("*.json"))
    except OSError:
        claim_files = []
    for claim_file in claim_files:
        request_id = claim_file.stem
        disp_path = paths.dispositions_dir / f"{request_id}.json"
        result = read_json_safe(paths.results_dir / f"{request_id}.json")
        if disp_path.exists():
            disp = read_json_safe(disp_path)
            if result is not None and disp is not None and disp.get("state") == "launch-failed":
                anomalies.append(f"late-launch-after-terminal: {request_id}")
                late_launch_ids.add(request_id)
            continue
        claim = read_json_safe(claim_file)
        if claim is None:
            continue
        claimed_at = parse_iso(claim.get("claimed_at"))
        grace_elapsed = claimed_at is not None and (now - claimed_at).total_seconds() >= GRACE_SECONDS
        if result is None:
            if grace_elapsed and not _claim_group_alive(paths, request_id, boot_id_fn, pgid_alive_fn):
                write_disposition(paths, request_id, "launch-failed", "no-launch-evidence", now_fn)
            continue
        stage = result.get("stage")
        if stage == "busy":
            write_disposition(paths, request_id, "launch-failed", "busy", now_fn)
        elif stage == "wrapper-started":
            if grace_elapsed and not group_alive(result, boot_id_fn, pgid_alive_fn):
                write_disposition(paths, request_id, "launch-failed", "pre-run-crash", now_fn)
        elif stage == "exited":
            write_disposition(paths, request_id, "done", None, now_fn)
        # stage == "running": still genuinely in flight (or the janitor's crash-recovery
        # territory, phase 4) -- never terminalized here.
    return anomalies, late_launch_ids


# -------------------------------------------------------------------------------------------
# Phase 4: Liveness/janitor -- process-group truth, workload-aware recovery.
# -------------------------------------------------------------------------------------------


def map_last_run_disposition(
    run_exit: int | None, verify_exit: int | None, late_launch: bool, restart_failed: str | None = None,
) -> str:
    """Pinned (WP-3 brief): `clean` iff both exit codes are exactly 0 (a deadline/stop pause is a
    NORMAL `batch run` exit-0 outcome, not a needs-attention condition on its own -- queue state
    is surfaced separately); a `late-launch-after-terminal` anomaly attached to the run always
    forces `needs-attention` even over a clean exit pair; a recorded router-restart failure
    (`restart_failed`, plan restart-failure-surfacing WP-1) likewise forces `needs-attention`
    over a clean exit pair -- the GPU tier is down and `restart: unless-stopped` will not bring
    it back; anything else needs attention. `crashed` is never returned here -- it's
    janitor-composed only, from a dead group with no `stage:"exited"` result at all."""
    if late_launch or restart_failed:
        return "needs-attention"
    if run_exit == 0 and verify_exit == 0:
        return "clean"
    return "needs-attention"


def compose_last_run(
    paths: Paths, now_fn: Callable[[], datetime],
    late_launch_ids: set[str], janitor_crash_invocation_id: str | None = None,
    janitor_restart_failed: str | None = None,
) -> None:
    """Composes `last-run.json` from the NEWEST terminal evidence across every
    `results/*.json` at `stage:"exited"` plus (if given) a freshly-janitor-detected crash --
    dispatcher-sole-writer, never overwritten with older/no-newer evidence."""
    existing = read_json_safe(paths.last_run_path)
    candidates: list[dict[str, Any]] = []
    try:
        result_files = list(paths.results_dir.glob("*.json"))
    except OSError:
        result_files = []
    for result_file in result_files:
        result = read_json_safe(result_file)
        if result is None or result.get("stage") != "exited":
            continue
        inv_id = result.get("invocation_id") or result_file.stem
        run_exit, verify_exit = result.get("run_exit"), result.get("verify_exit")
        restart_failed = result.get("restart_failed")
        restart_failed = restart_failed if isinstance(restart_failed, str) and restart_failed else None
        candidates.append({
            "invocation_id": inv_id, "request_id": inv_id,
            "ended_at": result.get("ended_at"),
            "run_exit": run_exit, "verify_exit": verify_exit,
            "restart_failed": restart_failed,
            "disposition": map_last_run_disposition(run_exit, verify_exit, inv_id in late_launch_ids, restart_failed),
        })
    if janitor_crash_invocation_id is not None:
        candidates.append({
            "invocation_id": janitor_crash_invocation_id, "request_id": janitor_crash_invocation_id,
            "ended_at": iso_z(now_fn()),
            "run_exit": None, "verify_exit": None,
            "restart_failed": janitor_restart_failed, "disposition": "crashed",
        })
    if not candidates:
        return
    newest = max(candidates, key=lambda c: c.get("ended_at") or "")
    if existing is not None and (newest.get("ended_at") or "") <= (existing.get("ended_at") or "") \
            and newest.get("invocation_id") == existing.get("invocation_id"):
        return
    atomic_write_json(paths.last_run_path, newest)


_LIVE_IDENTITY_KEYS = ("invocation_id", "request_id", "pid", "pgid", "pid_starttime", "boot_id")


def _live_identity(live: dict[str, Any]) -> tuple[Any, ...]:
    """The fields that name ONE invocation's process identity -- `phase` is excluded on purpose
    (`set_live_phase` rewrites it in place during a healthy run)."""
    return tuple(live.get(k) for k in _LIVE_IDENTITY_KEYS)


def janitor_phase(
    paths: Paths, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool], pid_alive_fn: Callable[[int | None, str | None], bool],
    find_pid_fn: Callable[..., int | None], teardown_fn: Callable[..., dict[str, Any]], router_start_fn: Callable[[str], None],
    port: int, scratch_container: str, router_container: str,
    late_launch_ids: set[str],
    acquire_cron_lock_fn: Callable[[], int | None] | None = None,
) -> list[str]:
    """Controller-liveness truth + workload-aware crash recovery. Wrapper-dead-but-group-alive is
    an anomaly only (stop stays admissible -- `batch.py`, the marker consumer, is the survivor);
    a `boot_id` mismatch is dead by definition; a genuinely empty group triggers the FULL
    recovery sequence -- orphan scratch-server teardown, THEN router restart, THEN compose
    `last-run.json` `disposition:"crashed"`, THEN remove `live.json` (in that order -- the plan's
    "only then compose ... and remove"). Matrix row 13: an unparseable/oversized `live.json`
    (exists but unreadable, as opposed to genuinely absent) is treated as absent for every
    control-flow purpose below but is ALSO counted as an `"unreadable-control-file: live.json"`
    anomaly. Matrix row 12: a verified-alive `live.json` naming a manual-start request id with no
    corresponding `claims/<id>.json` on disk is surfaced as a `"claim-live-id-mismatch: ..."`
    anomaly -- liveness still wins for display, no disposition change.

    GPU ownership (review F1 of `reviews/2026-10-11-tn-code-review.md`): every recovery effect
    runs while holding `cron.lock`, acquired non-blocking via `acquire_cron_lock_fn` (an open,
    flocked fd, or `None` when busy) and released in `finally` only after `live.json` is gone.
    Busy (a TTS window, or a fresh run that has not yet published its own `live.json`) defers the
    whole recovery to a later pass with `live.json` and every piece of evidence intact, recorded
    as a `"recovery-deferred: ..."` anomaly. Once held, `live.json` is re-read: if it no longer
    names the same invocation (removed, unreadable, or replaced by a new run), nothing destructive
    happens this pass. Never an acquire-then-release probe -- that reopens the race."""
    anomalies: list[str] = []
    live = read_json_safe(paths.live_path, anomalies=anomalies)
    if live is None:
        return anomalies

    boot_ok = live.get("boot_id") == boot_id_fn()
    pgid = live.get("pgid")
    is_group_alive = boot_ok and isinstance(pgid, int) and pgid_alive_fn(pgid)

    if is_group_alive:
        if not pid_alive_fn(live.get("pid"), live.get("pid_starttime")):
            anomalies.append("running (wrapper lost)")
        live_request_id = live.get("request_id")
        if isinstance(live_request_id, str) and REQUEST_BASENAME_RE.match(f"{live_request_id}.json"):
            # A manual-start invocation (never the nightly, which has no claim file by design --
            # it isn't request-drop-box-driven) -- its live-named request should always have a
            # durable claim on disk; a missing one is exactly the pinned "claim id != live.json
            # id" case (no claim id AT ALL is the degenerate case of a mismatch).
            if not (paths.claims_dir / f"{live_request_id}.json").exists():
                anomalies.append(f"claim-live-id-mismatch: none vs {live_request_id}")
        return anomalies

    # Group empty (or dead-by-boot-mismatch) -- full crash recovery, in this exact order: the two
    # effects first (teardown, router restart), THEN the durable evidence that the run died
    # (`last-run.json`), and only THEN the removal of `live.json`. `live.json` is the one thing
    # that says "there was a run here at all", so it is removed last: a crash anywhere above
    # leaves it in place and the next pass re-runs the whole sequence from the top.
    # All of it under `cron.lock`, held from before the first effect until after the unlink.
    acquire = acquire_cron_lock_fn or (lambda: flock_nonblocking_acquire(paths.cron_lock))
    lock_fd = acquire()
    if lock_fd is None:
        anomalies.append(f"recovery-deferred: cron.lock busy ({live.get('invocation_id')})")
        return anomalies
    try:
        # Revalidate under the lock: the observation above was lock-free, so a new run may have
        # replaced `live.json` (or another recoverer removed it) in between.
        current = read_json_safe(paths.live_path)
        if current is None or _live_identity(current) != _live_identity(live):
            return anomalies
        try:
            found_pid = find_pid_fn(port, container=scratch_container)
        except Exception:  # noqa: BLE001 -- a docker-touching seam must never abort the pass
            found_pid = None
        if found_pid is not None:
            try:
                teardown_fn(port, container=scratch_container)
            except Exception:  # noqa: BLE001
                pass
        # Never block recovery on a failed restart attempt -- but record it: the default
        # `router_start_fn` is `subprocess.run([...], check=False)`, so failure arrives as a nonzero
        # `returncode`, not an exception; a custom seam may raise instead. Both become one anomaly
        # line and ride into `last-run.json` (plan restart-failure-surfacing WP-1).
        restart_failed: str | None = None
        try:
            started = router_start_fn(router_container)
            returncode = getattr(started, "returncode", 0)
            if isinstance(returncode, int) and returncode != 0:
                restart_failed = f"docker start {router_container} exited {returncode}"
        except Exception as exc:  # noqa: BLE001
            restart_failed = f"docker start {router_container}: {type(exc).__name__}: {exc}"
        if restart_failed is not None:
            anomalies.append(f"router-restart-failed: {restart_failed}")
        compose_last_run(
            paths, now_fn, late_launch_ids,
            janitor_crash_invocation_id=current.get("invocation_id"), janitor_restart_failed=restart_failed,
        )
        try:
            paths.live_path.unlink()
        except FileNotFoundError:
            pass
    finally:
        flock_release(lock_fd)
    return anomalies


# -------------------------------------------------------------------------------------------
# Phase 5: Publish status.json.
# -------------------------------------------------------------------------------------------


def project_request_state(paths: Paths, request_id: str) -> tuple[str, str | None]:
    """Wire-state precedence: disposition -> live -> claim -> pending. `live.json`'s presence is
    trusted as-is here -- the janitor (phase 4, already run this pass) is what keeps it accurate,
    removing it whenever the group is genuinely dead."""
    disp = read_json_safe(paths.dispositions_dir / f"{request_id}.json")
    if disp is not None:
        state = disp.get("state")
        return (state if state in WIRE_STATES else "rejected"), disp.get("reason")
    live = read_json_safe(paths.live_path)
    if live is not None and live.get("request_id") == request_id:
        return "running", None
    if (paths.claims_dir / f"{request_id}.json").exists():
        return "claimed", None
    return "pending", None


def _build_requests_digest(paths: Paths) -> list[dict[str, Any]]:
    """The digest deliberately does NOT re-run `validate_request_body`: a request rejected for
    any reason (including WP-R4's schema check) still belongs on the dashboard, showing the
    `rejected` state and the reason its disposition recorded. This function only needs enough of
    the body to label the row -- an `action` in the pinned vocabulary and a `requested_at` to
    sort by; every verdict comes from `project_request_state` reading durable host evidence."""
    digest: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(paths.requests_dir))
    except OSError:
        names = []
    for name in names:
        if not REQUEST_BASENAME_RE.match(name):
            continue
        request_id = name[: -len(".json")]
        data = safe_read_request_bytes(paths.requests_dir / name)
        if data is None:
            continue
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(body, dict):
            continue
        action = body.get("action")
        requested_at = body.get("requested_at")
        if action not in VALID_ACTIONS or not isinstance(requested_at, str):
            continue
        state, reason = project_request_state(paths, request_id)
        digest.append({
            "request_id": request_id, "action": action, "state": state,
            "reason": reason, "requested_at": requested_at,
        })
    digest.sort(key=lambda d: (d["requested_at"], d["request_id"]))
    return digest


def compose_status(
    paths: Paths, now_fn: Callable[[], datetime],
    ignored_invalid_names: int, anomalies: list[str],
) -> dict[str, Any]:
    armed = paths.armed_marker.exists()
    live_raw = read_json_safe(paths.live_path)
    live = None
    if live_raw is not None:
        live = {
            "request_id": live_raw.get("request_id"), "queue_stem": live_raw.get("queue_stem"),
            "phase": live_raw.get("phase"), "started_at": live_raw.get("started_at"),
        }
    last_run_raw = read_json_safe(paths.last_run_path)
    last_run = None
    if last_run_raw is not None:
        last_run = {
            "request_id": last_run_raw.get("request_id"), "ended_at": last_run_raw.get("ended_at"),
            "run_exit": last_run_raw.get("run_exit"), "verify_exit": last_run_raw.get("verify_exit"),
            "restart_failed": last_run_raw.get("restart_failed"),
            "disposition": last_run_raw.get("disposition"),
        }
    status = {
        "generated_at": iso_z(now_fn()),
        "armed": armed,
        "live": live,
        "last_run": last_run,
        "anomalies": list(anomalies),
        "requests": _build_requests_digest(paths),
        "ignored_invalid_names": ignored_invalid_names,
    }
    atomic_write_json(paths.status_path, status)
    return status


# -------------------------------------------------------------------------------------------
# Phase 6: Prune.
# -------------------------------------------------------------------------------------------


def _prune_if_old(path: Path, now: datetime) -> None:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return
    if (now - mtime).total_seconds() > PRUNE_AGE_SECONDS:
        try:
            path.unlink()
        except OSError:
            pass


def _invocation_currently_live(paths: Paths, invocation_id: str) -> bool:
    live = read_json_safe(paths.live_path)
    return live is not None and live.get("invocation_id") == invocation_id


def prune_phase(paths: Paths, now_fn: Callable[[], datetime]) -> None:
    """Drop-box request files, `results/`, `actions/` older than 30 days; stop markers naming
    dead (not-currently-live) invocations. `claims/` and `dispositions/` are retained forever --
    never touched here."""
    now = now_fn()
    for d in (paths.requests_dir, paths.results_dir, paths.actions_dir):
        try:
            files = list(d.glob("*.json"))
        except OSError:
            files = []
        for f in files:
            _prune_if_old(f, now)
    try:
        markers = list(paths.control_dir.glob("stop-requested.*"))
    except OSError:
        markers = []
    for marker in markers:
        invocation_id = marker.name[len("stop-requested."):]
        if not _invocation_currently_live(paths, invocation_id):
            try:
                marker.unlink()
            except OSError:
                pass


# -------------------------------------------------------------------------------------------
# Launch seam -- `setsid`-detach `run-window.sh`, real default, injected fake in every test.
# -------------------------------------------------------------------------------------------


def default_launch(req: IngestedRequest, paths: Paths, queues_dir: Path, run_window_path: Path) -> None:
    queue_path = queues_dir / f"{req.queue_stem}.json"
    argv = ["setsid", str(run_window_path), str(queue_path), "--request-id", req.request_id]
    if req.minutes is not None:
        argv += ["--minutes", str(req.minutes)]
    env = dict(os.environ)
    env["BAKEOFF_STATE_DIR"] = str(paths.state_dir)
    # The real, live-capable launch path; never invoked by tests.
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, env=env,
    )


# -------------------------------------------------------------------------------------------
# Orchestration -- one full dispatcher pass.
# -------------------------------------------------------------------------------------------


def dispatch_pass(
    paths: Paths,
    queues_dir: Path,
    now_fn: Callable[[], datetime] | None = None,
    boot_id_fn: Callable[[], str] | None = None,
    pgid_alive_fn: Callable[[int], bool] | None = None,
    pid_alive_fn: Callable[[int | None, str | None], bool] | None = None,
    probe_cron_lock_fn: Callable[[], bool] | None = None,
    launch_fn: Callable[[IngestedRequest], None] | None = None,
    find_pid_fn: Callable[..., int | None] | None = None,
    teardown_fn: Callable[..., dict[str, Any]] | None = None,
    router_start_fn: Callable[[str], None] | None = None,
    acquire_cron_lock_fn: Callable[[], int | None] | None = None,
    port: int = DEFAULT_PORT,
    scratch_container: str = DEFAULT_SCRATCH_CONTAINER,
    router_container: str = DEFAULT_ROUTER_CONTAINER,
    run_window_path: Path | None = None,
) -> dict[str, Any]:
    """Runs the six phases in order (ingest -> act -> terminalize -> janitor -> publish -> prune)
    and returns the composed `status.json` dict. Every collaborator that could touch a real
    process/Docker is injectable with a safe real-but-never-exercised-by-tests default, same
    pattern as `batch.py.run_queue` -- see the module docstring."""
    now_fn = now_fn or (lambda: datetime.now(UTC))
    boot_id_fn = boot_id_fn or read_boot_id
    pgid_alive_fn = pgid_alive_fn or pgid_alive
    pid_alive_fn = pid_alive_fn or default_pid_alive
    probe_cron_lock_fn = probe_cron_lock_fn or (lambda: probe_lock_free(paths.cron_lock))
    run_window_path = run_window_path or (MODULE_DIR / "run-window.sh")
    launch_fn = launch_fn or (lambda req: default_launch(req, paths, queues_dir, run_window_path))
    find_pid_fn = find_pid_fn or sweep.find_host_pid_by_port
    teardown_fn = teardown_fn or sweep.teardown_scratch_server
    router_start_fn = router_start_fn or (lambda name: subprocess.run(["docker", "start", name], check=False))

    paths.ensure_dirs()
    pending, ignored = ingest_pass(paths, queues_dir, now_fn)
    act_phase(pending, paths, now_fn, boot_id_fn, pgid_alive_fn, probe_cron_lock_fn, launch_fn)
    term_anomalies, late_launch_ids = terminalize_phase(paths, now_fn, boot_id_fn, pgid_alive_fn)
    # Non-crash "done" dispositions from phase 3 don't feed last-run composition directly --
    # `compose_last_run` independently scans every `stage:"exited"` result (covers the nightly,
    # which has no claim file at all) so it's called once here, unconditionally.
    compose_last_run(paths, now_fn, late_launch_ids)
    janitor_anomalies = janitor_phase(
        paths, now_fn, boot_id_fn, pgid_alive_fn, pid_alive_fn,
        find_pid_fn, teardown_fn, router_start_fn, port, scratch_container, router_container,
        late_launch_ids, acquire_cron_lock_fn,
    )
    status = compose_status(paths, now_fn, ignored, term_anomalies + janitor_anomalies)
    prune_phase(paths, now_fn)
    return status
