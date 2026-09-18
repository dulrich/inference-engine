#!/usr/bin/env python3
"""Host SERVING-control dispatcher (`plans/bench-dashboard-serving-control.md` WP-5) -- the leg
that turns a `serving-request-v1` drop-box file into a rewritten `router/visibility.json` plus a
re-run of the two generators that project it, and publishes `serving-status-v1` for the bench
dashboard's chips.

One pass per invocation, minutely from the operator's crontab through `serving-dispatch.sh`,
under its own non-blocking `flock` on `<state-dir>/serving-dispatch.lock`. It deliberately
MIRRORS `control_dispatcher.py` rather than extending it: a separate module and a separate
crontab line, because the two planes have different failure domains and folding serving applies
into the GPU-run pump would put the declaration edit behind the run lock.

**The generators are the reconciliation loop; the request is only the trigger.** A pass folds
every winning request into ONE in-memory declaration, publishes that declaration at most once,
and then runs `router/model-picker-lineup.mjs --write` and
`scripts/sync-openwebui-capabilities.py --apply` EXACTLY ONCE, which read the declaration and
project it into `~/.claude/settings.json` and Open WebUI's `is_active`. Each generator already
reconciles the whole declaration, so running them per request would be redundant serial
whole-world work, not extra safety.

**One reconciliation per valid pass, including an empty one.** The pair runs whenever the
declaration is readable -- with a burst of requests, with a single one, with only rejections, and
with no request at all. That is what the paragraph above actually promises: if a projection exits
non-zero the requests in that batch are `failed` and **the declaration edit is KEPT** -- it is the
declaration, not a side effect -- so the NEXT pass re-runs both idempotently from it *without* a
new request, which is what makes a failed sync self-healing with no retry queue. A per-request
projection could not do that: a failed request is terminal and its file is unlinked, so nothing
durable would remain to retry from (measured in `reviews/2026-09-14-tn-code-review.md` F2).

**The three exit codes.** `0` a reconciled pass; `1` the declaration is missing or unreadable --
nothing applied, nothing published; `EXIT_RECONCILIATION_FAILED` (3) the declaration was read and
`serving-status.json` was published, but a generator exited non-zero. 3 is the ONLY signal an
EMPTY failing pass has (there is no request to carry a `failed` record), so it is spelled
explicitly rather than folded into the counts; the wrapper's own `2` means the credential file was
refused before this module ever ran, so the two spaces do not collide.

**Every filesystem path this module writes** (`control_store.Paths` owns the state-dir half):

```
<state-dir>/
  serving-dispatch.lock                  # this module's own non-blocking pass lock
  control/
    requests/                            #   drop-box, SHARED with the bake-off plane
      srv-<UTCcompact>-<16 hex>.json     #     API-written intent; read + dispositioned + unlinked here
    host/
      serving-status.json                #   published EVERY pass; read :ro by GET /api/serving/status
<repo>/router/visibility.json            # the declaration -- the ONE file outside the state dir
```

The declaration lives in the repo, not under the state dir, so it is resolved independently of
`resolve_state_dir`: **repo root comes from `__file__`** (`Path(__file__).resolve().parents[2]`),
never from the cwd, because a cron line has no useful working directory. Every subprocess path is
built from that same root. `--visibility` and `--repo-root` exist so the suite can point the whole
pass at a tempdir; nothing in `tests/` ever runs a generator or touches the real state dir.

**Shared drop-box.** `control/requests/` carries both planes' files. `control_phases.ingest_pass`
matches `req-` basenames only, so `srv-*.json` is invisible to it at ingest -- but its
`prune_phase` globs `*.json` over that directory and unlinks anything older than 30 days. Under
this module's terminalization a request lives for at most one minute, so that is unreachable in
practice; it is nonetheless the one place the two planes touch, and it is written down in
`README.md`'s Serving-control subsection.

**Record-then-unlink.** A request's disposition is durable in `serving-status.json`'s `recent[]`
BEFORE its file is unlinked, so a crash between the two leaves a request that is re-read and
re-applied idempotently rather than one that vanished unrecorded. Hence the phase order below:
publish, THEN unlink.

**Reuse, not reimplementation** (plan cycle-1 E3): `control_store.safe_read_request_bytes` for
the non-following, size-bounded drop-box read, `read_json_safe` for the declaration and the
previous status file, `atomic_write_json`/`tmp_path_for` for both writes (same-directory temp +
`os.replace`), `flock_nonblocking_acquire`/`flock_release` for the pass lock, and
`control_protocol`'s timestamp spelling by way of `serving_protocol`. Only serving-specific paths
and protocol logic are new.

**Credentials.** `scripts/sync-openwebui-capabilities.py` needs `OPENWEBUI_URL` and
`OPENWEBUI_API_KEY`, which have no default by design. `serving-dispatch.sh` sources them from
`~/.config/inference-serving/env` (mode `0600`, outside the repo) and execs this module; the
subprocess inherits the environment untouched. This module never reads, logs, echoes, or passes
either value -- a flag value would be visible in process listings, so the env path is the only one.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from control_store import (
    Paths,
    atomic_write_json,
    flock_nonblocking_acquire,
    flock_release,
    read_json_safe,
    resolve_state_dir,
    safe_read_request_bytes,
)
from serving_protocol import (
    MAX_REQUEST_BYTES,
    PRUNE_AGE_SECONDS,
    REQUEST_BASENAME_RE,
    SCHEMA_VISIBILITY,
    ServingRequest,
    compose_status,
    declared_models,
    make_record,
    validate_request,
    validate_status_file,
)

__all__ = [
    "EXIT_DECLARATION_UNREADABLE",
    "EXIT_RECONCILIATION_FAILED",
    "MODULE_DIR",
    "PICKER_BASENAME",
    "REPO_ROOT",
    "SERVING_LOCK_BASENAME",
    "SERVING_STATUS_BASENAME",
    "SYNC_BASENAME",
    "build_arg_parser",
    "cmd_dispatch",
    "default_unlink",
    "default_visibility_path",
    "dispatch_pass",
    "execute_pass",
    "main",
    "picker_argv",
    "run_subprocess",
    "serving_lock_path",
    "serving_status_path",
    "sync_argv",
]

MODULE_DIR = Path(__file__).resolve().parent
# `llama/bakeoff/` -> `llama/` -> the repo root. From `__file__`, never the cwd (plan cycle-1 M5).
REPO_ROOT = MODULE_DIR.parents[1]

SERVING_STATUS_BASENAME = "serving-status.json"
SERVING_LOCK_BASENAME = "serving-dispatch.lock"

PICKER_BASENAME = "model-picker-lineup.mjs"
SYNC_BASENAME = "sync-openwebui-capabilities.py"

# Pass exit codes. 0 is a reconciled pass; the two below are the only non-zero ones this module
# produces, and they are distinct because they mean opposite things to an operator reading the
# cron log: nothing happened at all, versus the declaration is current but its projection is not.
EXIT_DECLARATION_UNREADABLE = 1
EXIT_RECONCILIATION_FAILED = 3


# -------------------------------------------------------------------------------------------
# Paths and the two generator argv defaults.
# -------------------------------------------------------------------------------------------


def default_visibility_path(repo_root: Optional[Path] = None) -> Path:
    return (Path(repo_root) if repo_root else REPO_ROOT) / "router" / "visibility.json"


def serving_status_path(paths: Paths) -> Path:
    """Beside the bake-off plane's `status.json`, in the same host dir the dashboard mounts
    `:ro` as `CONTROL_HOST_DIR`. Publishing anywhere else leaves the chips disabled forever."""
    return paths.host_dir / SERVING_STATUS_BASENAME


def serving_lock_path(paths: Paths) -> Path:
    return paths.state_dir / SERVING_LOCK_BASENAME


def picker_argv(repo_root: Path) -> List[str]:
    """Bare `--write`: the picker rewrites `modelPicker.options` and re-serialises the rest of
    `settings.json` unchanged, so an existing `--launch-profile` env block (which needs the
    gateway key from fleet-tools's `.env`) survives verbatim. Verified against a tempdir HOME
    at WP-5; do not add `--launch-profile` here."""
    return ["node", str(Path(repo_root) / "router" / PICKER_BASENAME), "--write"]


def sync_argv(repo_root: Path) -> List[str]:
    return ["python3", str(Path(repo_root) / "scripts" / SYNC_BASENAME), "--apply"]


def run_subprocess(argv: Sequence[str]) -> int:
    """The real default behind both generator seams -- the suite ALWAYS injects a fake and never
    runs a generator. The environment is passed through untouched (that is how the sync leg gets
    `OPENWEBUI_URL`/`OPENWEBUI_API_KEY` from the wrapper), and nothing about it is ever printed."""
    try:
        completed = subprocess.run(list(argv), check=False)
    except OSError as exc:
        # An unlaunchable generator (no `node` on cron's PATH, a moved script) is a FAILED
        # reconciliation, not a crashed pass: the pass must still publish its status file and
        # report the failure through the ordinary exit, or the chips go `stale` for an
        # environment problem. 127 is the shell's own "command not found".
        sys.stderr.write(f"serving_dispatcher: could not run {argv[0]} ({exc})\n")
        return 127
    return completed.returncode


def default_unlink(path: Path) -> None:
    path.unlink()


# -------------------------------------------------------------------------------------------
# The pass.
# -------------------------------------------------------------------------------------------


def _ingest(
    paths: Paths, declaration: Dict[str, Any],
) -> Tuple[List[ServingRequest], List[Tuple[str, str]], int]:
    """`(pending, rejections, ignored)`. `pending` is sorted `(requested_at, request_id)`
    ascending -- the same tuple `control_phases.ingest_pass` uses, and for the same reason:
    directory enumeration order is never load-bearing, and the id's whole-second stamp does not
    order two requests accepted in the same second. A basename that does not match the grammar
    (every `req-*.json` of the other plane, and the API's transient `.`-prefixed `*.tmp`
    exclusive-creation files) is ignored and counted, never dispositioned -- there is no
    trustworthy id to disposition against."""
    try:
        names = sorted(os.listdir(paths.requests_dir))
    except OSError:
        names = []
    ignored = 0
    pending: List[ServingRequest] = []
    rejections: List[Tuple[str, str]] = []
    for name in names:
        if not REQUEST_BASENAME_RE.match(name):
            ignored += 1
            continue
        basename_id = name[: -len(".json")]
        data = safe_read_request_bytes(paths.requests_dir / name, MAX_REQUEST_BYTES)
        if data is None:
            rejections.append((basename_id, "malformed: hardening check failed"))
            continue
        validation = validate_request(basename_id, data, declaration)
        if validation.request is None:
            rejections.append((basename_id, validation.reason or "malformed"))
            continue
        pending.append(validation.request)
    pending.sort(key=lambda request: (request.requested_at, request.request_id))
    return pending, rejections, ignored


def _stage_declaration(
    declaration: Dict[str, Any],
    visibility_path: Path,
    winners: Dict[str, ServingRequest],
) -> bool:
    """Fold EVERY winning request of the pass into the one in-memory declaration and publish it
    with a SINGLE atomic write -- `True` when anything was written. Only the targeted keys'
    `consumers` change: every `gateway_id` is untouched, every other key is untouched, and key
    order survives (`json.load` preserves insertion order and `atomic_write_json` writes
    `sort_keys=False`). A batch whose entries are all already current writes nothing at all; the
    generators still run afterwards, because they -- not the write -- are the reconciliation
    loop."""
    changed = False
    for request in winners.values():
        entry = declaration["models"][request.model_key]
        wanted = list(request.consumers)
        if entry.get("consumers") != wanted:
            entry["consumers"] = wanted
            changed = True
    if changed:
        atomic_write_json(visibility_path, declaration)
    return changed


def _reconcile(
    repo_root: Path,
    run_picker: Callable[[Sequence[str]], int],
    run_sync: Callable[[Sequence[str]], int],
) -> Tuple[str, Optional[str]]:
    """The pass's ONE projection -- picker, then sync, then `(state, reason)`. Called exactly once
    per valid pass regardless of how many requests it carried (including zero). A non-zero exit
    short-circuits the rest and names the step that failed; the declaration edit is KEPT either
    way, which is what the next pass re-runs from."""
    code = run_picker(picker_argv(repo_root))
    if code != 0:
        return "failed", f"{PICKER_BASENAME} exited {code}"
    code = run_sync(sync_argv(repo_root))
    if code != 0:
        return "failed", f"{SYNC_BASENAME} exited {code}"
    return "applied", None


def _previous_recent(status_path: Path) -> List[Dict[str, Any]]:
    """`recent[]` carried forward from the previous pass. An absent, unparseable, oversized or
    schema-invalid status file is treated as no history -- never partially trusted."""
    previous = read_json_safe(status_path)
    if previous is None or validate_status_file(previous) is not None:
        return []
    recent = previous.get("recent")
    return list(recent) if isinstance(recent, list) else []


def _prune(
    paths: Paths, now: datetime, unlink_fn: Callable[[Path], None],
) -> int:
    """Backstop for a surviving `srv-*.json` older than 30 days -- a file too malformed to
    disposition, or one whose unlink failed. Only this plane's basenames: the bake-off plane's
    own prune owns `req-*`."""
    pruned = 0
    try:
        survivors = sorted(paths.requests_dir.glob("srv-*.json"))
    except OSError:
        survivors = []
    for path in survivors:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if (now - mtime).total_seconds() <= PRUNE_AGE_SECONDS:
            continue
        try:
            unlink_fn(path)
        except OSError:
            continue
        pruned += 1
    return pruned


def execute_pass(
    paths: Paths,
    visibility_path: Path,
    now_fn: Optional[Callable[[], datetime]] = None,
    run_picker: Optional[Callable[[Sequence[str]], int]] = None,
    run_sync: Optional[Callable[[Sequence[str]], int]] = None,
    *,
    repo_root: Optional[Path] = None,
    unlink_fn: Optional[Callable[[Path], None]] = None,
) -> Tuple[int, Dict[str, int]]:
    """One serving pass, as ONE reconciliation transaction -- `(exit_code, counts)`;
    `dispatch_pass` is the printing wrapper.

    Phase order, and why it is this order:

    1. **read the declaration.** A missing/invalid one aborts the pass with
       `EXIT_DECLARATION_UNREADABLE` and publishes NOTHING: an empty `visibility` map would tell
       the dashboard every model has no consumers, which is wrong rather than merely absent. A
       status file that stops advancing goes `stale` at the API's 180 s threshold and disables the
       chips -- the fail-closed outcome. This is the ONLY phase that can skip the projection.
    2. **ingest** (sorted, fail-closed).
    3. **stage**, latest-per-`model_key` wins; every winner is folded into the one in-memory
       declaration and that declaration is written at most once.
    4. **reconcile** -- one picker run and one sync run, ALWAYS, even on a pass that ingested
       nothing. The generators reconcile the whole declaration, so one pair is sufficient for a
       burst and necessary for an empty pass: it is the self-heal the module contract promises.
    5. **disposition**, from that one projection result. Every request the batch represents --
       winners and the superseded requests they stand for -- takes the SAME `applied`/`failed`
       state and reason, because there is one projection per pass and no per-model side effect to
       distinguish them by. Rejected requests never reach the projection and keep their own
       reason.
    6. **publish** `serving-status.json` atomically, EVERY pass including passes with no pending
       request, so the chips are authoritative within a minute of install.
    7. **unlink** the dispositioned request files -- after the publish, never before.
    8. **prune** surviving `srv-*.json` older than 30 days.

    A failed projection on a pass with NO request has no `recent[]` record to carry it (there is
    no request to disposition): its whole signal is `EXIT_RECONCILIATION_FAILED` plus the stderr
    line below, which is why the pass has a distinct exit code at all.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    picker = run_picker or run_subprocess
    sync = run_sync or run_subprocess
    root = Path(repo_root) if repo_root else REPO_ROOT
    unlink = unlink_fn or default_unlink
    now = now_fn()
    counts = {"applied": 0, "rejected": 0, "failed": 0, "ignored": 0, "pruned": 0}

    status_path = serving_status_path(paths)
    declaration = read_json_safe(visibility_path)
    if declaration is None or declared_models(declaration) is None:
        sys.stderr.write(
            f"serving_dispatcher: {visibility_path} is missing or is not a {SCHEMA_VISIBILITY} "
            "declaration -- nothing applied, nothing published\n",
        )
        return EXIT_DECLARATION_UNREADABLE, counts

    paths.requests_dir.mkdir(parents=True, exist_ok=True)
    paths.host_dir.mkdir(parents=True, exist_ok=True)

    pending, rejections, counts["ignored"] = _ingest(paths, declaration)

    records: List[Dict[str, Any]] = []
    dispositioned: List[str] = []
    for request_id, rejection_reason in rejections:
        records.append(make_record(request_id, "rejected", rejection_reason, now))
        dispositioned.append(request_id)
        counts["rejected"] += 1

    # Latest request per `model_key` wins (`pending` is ascending, so the last one seen for a key
    # is the winner); a superseded request is represented by that winner, never applied separately.
    winners: Dict[str, ServingRequest] = {request.model_key: request for request in pending}
    _stage_declaration(declaration, visibility_path, winners)
    state, reason = _reconcile(root, picker, sync)
    if state == "failed":
        sys.stderr.write(
            f"serving_dispatcher: {reason} -- the declaration edit is KEPT; the next pass "
            "re-runs both generators from it, with or without a new request\n",
        )
    for request in pending:
        records.append(make_record(request.request_id, state, reason, now))
        dispositioned.append(request.request_id)
        counts[state] += 1

    recent = list(reversed(records)) + _previous_recent(status_path)
    atomic_write_json(status_path, compose_status(declaration, recent, now))

    for request_id in dispositioned:
        try:
            unlink(paths.requests_dir / f"{request_id}.json")
        except OSError as exc:
            sys.stderr.write(
                f"serving_dispatcher: could not unlink {request_id}.json ({exc}) -- its "
                "disposition is already recorded; the next pass re-applies it idempotently\n",
            )

    counts["pruned"] = _prune(paths, now, unlink)
    return (EXIT_RECONCILIATION_FAILED if state == "failed" else 0), counts


def dispatch_pass(
    paths: Paths,
    visibility_path: Path,
    now_fn: Optional[Callable[[], datetime]] = None,
    run_picker: Optional[Callable[[Sequence[str]], int]] = None,
    run_sync: Optional[Callable[[Sequence[str]], int]] = None,
    *,
    repo_root: Optional[Path] = None,
    unlink_fn: Optional[Callable[[Path], None]] = None,
) -> int:
    """`execute_pass` plus the one-line JSON pass summary on stdout (the cron log's whole
    content on a quiet pass). Never prints the environment."""
    code, counts = execute_pass(
        paths, visibility_path, now_fn, run_picker, run_sync,
        repo_root=repo_root, unlink_fn=unlink_fn,
    )
    print(json.dumps(counts, sort_keys=True))
    return code


# -------------------------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------------------------


def cmd_dispatch(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    repo_root = Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT
    visibility_path = (
        Path(args.visibility) if args.visibility else default_visibility_path(repo_root)
    )
    fd = flock_nonblocking_acquire(serving_lock_path(paths))
    if fd is None:
        # A prior pass is still running; quietly skip. Self-serialization hygiene, never
        # admission -- and nothing at all is written on this path.
        return 0
    try:
        return dispatch_pass(paths, visibility_path, repo_root=repo_root)
    finally:
        flock_release(fd)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serving_dispatcher.py",
        description="Host serving-control dispatcher (plans/bench-dashboard-serving-control.md "
        "WP-5). See llama/bakeoff/README.md's 'Serving control' subsection; the crontab line runs "
        "serving-dispatch.sh, not this file directly.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dispatch = sub.add_parser(
        "dispatch", help="run one serving pass under flock -n serving-dispatch.lock",
    )
    p_dispatch.add_argument(
        "--state-dir", default=None,
        help="override BAKEOFF_STATE_DIR / the default ~/.local/state/inference-bakeoff",
    )
    p_dispatch.add_argument(
        "--visibility", default=None,
        help="override the declaration path (default: <repo>/router/visibility.json)",
    )
    p_dispatch.add_argument(
        "--repo-root", default=None,
        help="TEST OVERRIDE for the root every generator argv is built from (default: resolved "
        "from this file, never the cwd)",
    )
    p_dispatch.set_defaults(func=cmd_dispatch)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = build_arg_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
