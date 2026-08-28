#!/usr/bin/env python3
"""Host control-plane dispatcher (`plans/bakeoff-queue-control-plane.md` WP-3) -- the CLI entry
point and the `run-window.sh` wrapper adapters.

Files-only, minutely host dispatcher, no persistent supervisor daemon: the container writes
request-intent files into a drop-box, and this control plane (running as a host cron job, one
pass per invocation, under `flock -n dispatch.lock` for self-serialization hygiene only -- never
admission) is the sole authority that turns those requests into effects (arm/disarm markers,
target-bound stop markers, detached `run-window.sh` launches) and durable evidence (claims,
actions, dispositions, results, `live.json`, `last-run.json`, `status.json`).

**Module map** (WP-R4 of `plans/repository-tooling-structural-remediation.md` split this file;
review findings F4/F9). Import direction is strictly one way -- protocol <- store <- phases:

| module | owns |
| --- | --- |
| `control_protocol.py` | the wire contract: schema names, grammars, bounds, timestamps, `IngestedRequest`, and `validate_request_body` (which REQUIRES `schema == "control-request-v1"`) |
| `control_store.py` | `Paths` (the filesystem contract below), atomic writes, non-blocking flock, hardened drop-box reads |
| `control_process.py` | `/proc` boot/pid/pgid identity -- the real defaults behind every injectable liveness seam |
| `control_phases.py` | the six phases, crash recovery, and the action -> effect -> disposition ordering that makes replay idempotent |
| this file | the CLI (`dispatch` + the wrapper helper subcommands) and the `results/`/`live.json` writers `run-window.sh` shells out to |

Every name the previous single-file version exported is re-exported here, so
`import control_dispatcher as cd` remains the one import a caller or test needs; the underscore
prefixes on cross-module helpers are gone (a name used from another module is not private).

**No live operation happens unless a caller explicitly supplies live-capable collaborators**,
same "injectable seam, real-but-never-exercised-by-tests default" pattern as `batch.py`/
`sweep.py`: `dispatch_pass`'s `launch_fn`/`find_pid_fn`/`teardown_fn`/`router_start_fn` all
default to real, Docker/process-touching implementations, but this control plane's own test suite
(`tests/test_control_dispatcher.py`, `tests/test_control_contract.py`) always injects fakes -- it
never spawns a real process, never calls `docker`, never reads real `/proc`.

Every entry point takes a state-dir override (env `BAKEOFF_STATE_DIR` and/or `--state-dir`) and
a queues-dir override (env `BAKEOFF_QUEUES_DIR` and/or `--queues-dir`) -- see `Paths` and
`resolve_state_dir`/`resolve_queues_dir` in `control_store.py` -- so every test runs entirely
inside a tempdir, never touching the real `~/.local/state/inference-bakeoff/`.

**Filesystem contract** (plan "Filesystem contract" section; `control_store.Paths` is the single
source of truth for every path this control plane touches):

```
<state-dir>/
  cron.lock                      # THE run lock (admission); flock target for run-window.sh
  dispatch.lock                  # this module's own self-serialization lock (hygiene, not admission)
  control/
    requests/                    #   drop-box: mounted rw in the dashboard container (API-written)
      <request-id>.json          #     intent, written ONCE by the API -- this module only READS it
    stop-requested.<inv-id>      #   TARGET-BOUND stop marker (dispatcher creates, wrapper/batch.py consume)
    host/                        #   host-written; mounted ro in the dashboard container
      armed                      #     marker file: presence = nightly armed (fail-closed)
      claims/<request-id>.json   #     durable claim, written BEFORE launch
      results/<invocation-id>.json # wrapper-owned launch/exit evidence
      actions/<request-id>.json  #     pre-effect action evidence for stop/cancel
      dispositions/<id>.json     #     terminal request record (retained indefinitely)
      live.json                  #     wrapper-published liveness + phase
      last-run.json              #     dispatcher-composed (sole writer) from results+janitor
      status.json                #     dispatcher digest each pass -- the container's read surface
```

**Ownership**: `requests/<id>.json` is the API's (this control plane never writes it, only reads
+ eventually prunes by age); every other file under `control/` is host-written -- the dispatcher
is the sole writer of `claims/`, `actions/`, `dispositions/`, `last-run.json`, `status.json`, and
`armed`; `run-window.sh` (via this file's own `result-write`/`live-*` helper subcommands, the
SANCTIONED pattern for keeping schema knowledge in one Python module rather than reimplementing
JSON writing in bash) is the sole writer of `results/<inv-id>.json` and `live.json`.

**Wire-state precedence** (`status.json.requests[].state`, pinned enum `pending | claimed |
running | done | rejected | canceled | launch-failed`): disposition -> live -> claim -> pending.
`status.json` itself is read by the LANDED WP-4 API (`dashboard/apps/api/src/routes/queue.ts`
`buildControlSection` + `dashboard/packages/shared/src/control.ts`) -- its exact snake_case shape
(every listed key present, explicit `null` never omitted) is pinned by that reader, not
renegotiable here, and by the committed cross-language fixtures under
`dashboard/apps/api/fixtures/control/contract-v1/`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

# `sweep` and the `control_*` modules live beside this file with no package `__init__.py` under
# `llama/` -- see batch.py's own comment on this exact gotcha. Plain top-level imports work
# whenever the importer has already put this directory on `sys.path` (true when this file is run
# directly as a script, and when a test under `tests/` inserts this directory itself).
import sweep
from control_phases import (
    DEFAULT_PORT,
    DEFAULT_ROUTER_CONTAINER,
    DEFAULT_SCRATCH_CONTAINER,
    act_phase,
    compose_last_run,
    compose_status,
    default_launch,
    dispatch_pass,
    ingest_pass,
    janitor_phase,
    map_last_run_disposition,
    project_request_state,
    prune_phase,
    terminalize_phase,
    write_action,
    write_disposition,
)
from control_process import (
    default_pid_alive,
    group_alive,
    parse_stat_fields,
    pgid_alive,
    read_boot_id,
    read_pgid,
    read_pid_starttime,
)
from control_protocol import (
    GRACE_SECONDS,
    HOST_ID_RE,
    LAST_RUN_DISPOSITIONS,
    MAX_REQUEST_BYTES,
    MINUTES_MAX,
    MINUTES_MIN,
    PRUNE_AGE_SECONDS,
    QUEUE_STEM_RE,
    REQUEST_BASENAME_RE,
    REQUEST_SCHEMA_REJECTION,
    SCHEMA_ACTION,
    SCHEMA_CLAIM,
    SCHEMA_DISPOSITION,
    SCHEMA_REQUEST,
    SCHEMA_RESULT,
    VALID_ACTIONS,
    WIRE_STATES,
    IngestedRequest,
    RequestValidation,
    iso_z,
    parse_iso,
    utc_now_iso,
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
    resolve_queues_dir,
    resolve_state_dir,
    safe_read_request_bytes,
    tmp_path_for,
)

__all__ = [
    # protocol
    "SCHEMA_REQUEST", "SCHEMA_CLAIM", "SCHEMA_RESULT", "SCHEMA_ACTION", "SCHEMA_DISPOSITION",
    "REQUEST_BASENAME_RE", "HOST_ID_RE", "QUEUE_STEM_RE", "VALID_ACTIONS", "MAX_REQUEST_BYTES",
    "MINUTES_MIN", "MINUTES_MAX", "GRACE_SECONDS", "WIRE_STATES", "LAST_RUN_DISPOSITIONS",
    "PRUNE_AGE_SECONDS", "REQUEST_SCHEMA_REJECTION", "IngestedRequest", "RequestValidation",
    "iso_z", "utc_now_iso", "parse_iso", "validate_request_body",
    # store
    "MODULE_DIR", "Paths", "resolve_state_dir", "resolve_queues_dir", "tmp_path_for",
    "atomic_write_json", "atomic_touch", "read_json_safe", "safe_read_request_bytes",
    "flock_nonblocking_acquire", "flock_release", "probe_lock_free",
    # process identity
    "read_boot_id", "parse_stat_fields", "read_pid_starttime", "read_pgid", "pgid_alive",
    "default_pid_alive", "group_alive",
    # phases
    "DEFAULT_PORT", "DEFAULT_SCRATCH_CONTAINER", "DEFAULT_ROUTER_CONTAINER",
    "write_disposition", "write_action", "ingest_pass", "act_phase", "terminalize_phase",
    "compose_last_run", "janitor_phase", "compose_status", "project_request_state",
    "map_last_run_disposition", "prune_phase", "default_launch", "dispatch_pass",
    # wrapper adapters + CLI (below)
    "write_result", "write_live", "set_live_phase", "clear_live", "orphan_recover",
    "clear_stop_marker", "build_arg_parser", "main",
]


# -------------------------------------------------------------------------------------------
# Wrapper (`run-window.sh`) helper subcommands -- the SANCTIONED pattern (plan WP-3 brief): JSON
# writing and process/docker inspection from bash is error-prone, so `run-window.sh` shells out to
# these for every JSON write and the orphan sweep, keeping schema knowledge in this one module.
# -------------------------------------------------------------------------------------------


def write_result(
    paths: Paths, invocation_id: str, stage: str, *,
    pid: Optional[int] = None, pgid: Optional[int] = None,
    pid_starttime: Optional[str] = None, boot_id: Optional[str] = None,
    run_exit: Optional[int] = None, verify_exit: Optional[int] = None,
    ended_at: Optional[str] = None, now_fn: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """`results/<inv-id>.json` -- wrapper-owned evidence channel, stage advances via repeated
    calls (each merges onto the existing file rather than replacing it, so identity fields
    written at `stage:"wrapper-started"` survive every later advance)."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    path = paths.results_dir / f"{invocation_id}.json"
    existing = read_json_safe(path) or {"invocation_id": invocation_id}
    existing["stage"] = stage
    for key, value in (
        ("pid", pid), ("pgid", pgid), ("pid_starttime", pid_starttime), ("boot_id", boot_id),
        ("run_exit", run_exit), ("verify_exit", verify_exit),
    ):
        if value is not None:
            existing[key] = value
    if stage == "exited":
        existing["ended_at"] = ended_at or iso_z(now_fn())
    atomic_write_json(path, existing)
    return existing


def write_live(
    paths: Paths, invocation_id: str, request_id: str, queue_stem: str,
    pid: int, pgid: int, pid_starttime: Optional[str], boot_id: str,
    started_at: Optional[str] = None, now_fn: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """Publishes `live.json` in full (wrapper step 5) -- always `phase:"running"` at publish
    time; use `set_live_phase` to advance it afterward (step 7's `"verifying"`)."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    body = {
        "invocation_id": invocation_id, "request_id": request_id, "queue_stem": queue_stem,
        "pid": pid, "pgid": pgid, "pid_starttime": pid_starttime, "boot_id": boot_id,
        "phase": "running", "started_at": started_at or iso_z(now_fn()),
    }
    atomic_write_json(paths.live_path, body)
    return body


def set_live_phase(paths: Paths, phase: str) -> Optional[Dict[str, Any]]:
    existing = read_json_safe(paths.live_path)
    if existing is None:
        return None
    existing["phase"] = phase
    atomic_write_json(paths.live_path, existing)
    return existing


def clear_live(paths: Paths) -> None:
    try:
        paths.live_path.unlink()
    except FileNotFoundError:
        pass


def orphan_recover(
    port: int, container: str,
    find_pid_fn: Optional[Callable[..., Optional[int]]] = None,
    teardown_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Wrapper step 4's orphan-workload recovery: detect a surviving `llama-server` from a prior
    crashed run (via `sweep.find_host_pid_by_port`) and tear it down (`sweep.
    teardown_scratch_server`, host `os.kill`) before any new run is admitted against an uncleared
    prior workload -- admission is symmetric with the dispatcher's own janitor (phase 4), so a new
    run can never start against an uncleared prior workload even if the janitor hasn't fired yet."""
    find_pid_fn = find_pid_fn or sweep.find_host_pid_by_port
    teardown_fn = teardown_fn or sweep.teardown_scratch_server
    pid = find_pid_fn(port, container=container)
    if pid is None:
        return {"found_pid": None, "torn_down": False}
    detail = teardown_fn(port, container=container)
    return {"found_pid": pid, "torn_down": True, "detail": detail}


def clear_stop_marker(paths: Paths, invocation_id: str) -> bool:
    """Idempotent unlink of `stop-requested.<invocation_id>` -- used at BOTH wrapper step 4
    (window-start stale-clear hygiene) and step 7 (post-batch-run belt-and-suspenders clear).
    Returns whether a marker was actually present and removed."""
    marker = paths.stop_marker(invocation_id)
    try:
        marker.unlink()
        return True
    except FileNotFoundError:
        return False


# -------------------------------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=None, help="override BAKEOFF_STATE_DIR / the default ~/.local/state/inference-bakeoff")
    parser.add_argument("--queues-dir", default=None, help="override BAKEOFF_QUEUES_DIR / the default llama/bakeoff/queues")


def cmd_dispatch(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    queues_dir = resolve_queues_dir(args)
    paths.ensure_dirs()
    fd = flock_nonblocking_acquire(paths.dispatch_lock)
    if fd is None:
        # Self-serialization hygiene only -- a busy dispatch.lock means another pass is already
        # running; quietly skip, never an error (NOT admission).
        return 0
    try:
        dispatch_pass(paths, queues_dir)
    finally:
        flock_release(fd)
    return 0


def cmd_result_write(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    pid_starttime = args.pid_starttime
    if pid_starttime is None and args.pid is not None:
        pid_starttime = read_pid_starttime(args.pid)
    boot_id = args.boot_id
    if boot_id is None and args.stage in ("wrapper-started",):
        boot_id = read_boot_id()
    write_result(
        paths, args.invocation_id, args.stage,
        pid=args.pid, pgid=args.pgid, pid_starttime=pid_starttime, boot_id=boot_id,
        run_exit=args.run_exit, verify_exit=args.verify_exit, ended_at=args.ended_at,
    )
    return 0


def cmd_live_publish(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    pid_starttime = args.pid_starttime or read_pid_starttime(args.pid)
    boot_id = args.boot_id or read_boot_id()
    write_live(
        paths, args.invocation_id, args.request_id, args.queue_stem,
        args.pid, args.pgid, pid_starttime, boot_id, started_at=args.started_at,
    )
    return 0


def cmd_live_set_phase(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    result = set_live_phase(paths, args.phase)
    return 0 if result is not None else 1


def cmd_live_clear(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    clear_live(paths)
    return 0


def cmd_orphan_recover(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))  # unused directly, kept for CLI symmetry/future logging
    del paths
    result = orphan_recover(args.port, args.container)
    print(json.dumps(result))
    return 0


def cmd_clear_stop_marker(args: argparse.Namespace) -> int:
    paths = Paths(resolve_state_dir(args))
    clear_stop_marker(paths, args.invocation_id)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="control_dispatcher.py",
        description="Host control-plane dispatcher (plans/bakeoff-queue-control-plane.md WP-3). "
        "See llama/bakeoff/README.md's Scheduling section.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dispatch = sub.add_parser("dispatch", help="run one dispatcher pass under flock -n dispatch.lock")
    _add_common_args(p_dispatch)
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_result = sub.add_parser("result-write", help="wrapper helper: write/advance results/<inv-id>.json")
    _add_common_args(p_result)
    p_result.add_argument("--invocation-id", required=True)
    p_result.add_argument("--stage", required=True, choices=("wrapper-started", "busy", "running", "exited"))
    p_result.add_argument("--pid", type=int, default=None)
    p_result.add_argument("--pgid", type=int, default=None)
    p_result.add_argument("--pid-starttime", default=None, help="default: real /proc read for the given --pid")
    p_result.add_argument("--boot-id", default=None, help="default: real /proc/sys/kernel/random/boot_id read")
    p_result.add_argument("--run-exit", type=int, default=None)
    p_result.add_argument("--verify-exit", type=int, default=None)
    p_result.add_argument("--ended-at", default=None, help="default: now, only meaningful at --stage exited")
    p_result.set_defaults(func=cmd_result_write)

    p_live_publish = sub.add_parser("live-publish", help="wrapper helper: publish live.json (step 5)")
    _add_common_args(p_live_publish)
    p_live_publish.add_argument("--invocation-id", required=True)
    p_live_publish.add_argument("--request-id", required=True)
    p_live_publish.add_argument("--queue-stem", required=True)
    p_live_publish.add_argument("--pid", type=int, required=True)
    p_live_publish.add_argument("--pgid", type=int, required=True)
    p_live_publish.add_argument("--pid-starttime", default=None, help="default: real /proc read for the given --pid")
    p_live_publish.add_argument("--boot-id", default=None, help="default: real /proc/sys/kernel/random/boot_id read")
    p_live_publish.add_argument("--started-at", default=None)
    p_live_publish.set_defaults(func=cmd_live_publish)

    p_live_phase = sub.add_parser("live-set-phase", help="wrapper helper: advance live.json's phase (step 7)")
    _add_common_args(p_live_phase)
    p_live_phase.add_argument("--phase", required=True)
    p_live_phase.set_defaults(func=cmd_live_set_phase)

    p_live_clear = sub.add_parser("live-clear", help="wrapper helper: remove live.json (step 8)")
    _add_common_args(p_live_clear)
    p_live_clear.set_defaults(func=cmd_live_clear)

    p_orphan = sub.add_parser("orphan-recover", help="wrapper helper: tear down a surviving scratch llama-server (step 4)")
    _add_common_args(p_orphan)
    p_orphan.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_orphan.add_argument("--container", default=DEFAULT_SCRATCH_CONTAINER)
    p_orphan.set_defaults(func=cmd_orphan_recover)

    p_clear_marker = sub.add_parser("clear-stop-marker", help="wrapper helper: idempotent unlink of stop-requested.<inv-id>")
    _add_common_args(p_clear_marker)
    p_clear_marker.add_argument("--invocation-id", required=True)
    p_clear_marker.set_defaults(func=cmd_clear_stop_marker)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
