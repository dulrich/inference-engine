#!/usr/bin/env python3
"""Host control-plane dispatcher (`plans/bakeoff-queue-control-plane.md` WP-3).

Files-only, minutely host dispatcher, no persistent supervisor daemon: the container writes
request-intent files into a drop-box, and this module (running as a host cron job, one pass per
invocation, under `flock -n dispatch.lock` for self-serialization hygiene only -- never
admission) is the sole authority that turns those requests into effects (arm/disarm markers,
target-bound stop markers, detached `run-window.sh` launches) and durable evidence (claims,
actions, dispositions, results, `live.json`, `last-run.json`, `status.json`).

**No live operation happens unless a caller explicitly supplies live-capable collaborators**,
same "injectable seam, real-but-never-exercised-by-tests default" pattern as `batch.py`/
`sweep.py`: `dispatch_pass`'s `launch_fn`/`find_pid_fn`/`teardown_fn`/`router_start_fn` all
default to real, Docker/process-touching implementations, but this module's own test suite
(`tests/test_control_dispatcher.py`) always injects fakes -- it never spawns a real process,
never calls `docker`, never reads real `/proc`.

Every entry point takes a state-dir override (env `BAKEOFF_STATE_DIR` and/or `--state-dir`) and
a queues-dir override (env `BAKEOFF_QUEUES_DIR` and/or `--queues-dir`) -- see `Paths` and
`_resolve_state_dir`/`_resolve_queues_dir` below -- so every test runs entirely inside a tempdir,
never touching the real `~/.local/state/inference-bakeoff/`.

**Filesystem contract** (plan "Filesystem contract" section; `Paths` below is the single source
of truth for every path this module touches):

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

**Ownership**: `requests/<id>.json` is the API's (this module never writes it, only reads +
eventually prunes by age); every other file under `control/` is host-written -- this module is
the sole writer of `claims/`, `actions/`, `dispositions/`, `last-run.json`, `status.json`, and
`armed`; `run-window.sh` (via this module's own `result-write`/`live-*` helper subcommands, the
SANCTIONED pattern for keeping schema knowledge in one Python module rather than reimplementing
JSON writing in bash) is the sole writer of `results/<inv-id>.json` and `live.json`.

**Wire-state precedence** (`status.json.requests[].state`, pinned enum `pending | claimed |
running | done | rejected | canceled | launch-failed`): disposition -> live -> claim -> pending.
`status.json` itself is read by the LANDED WP-4 API (`dashboard/apps/api/src/routes/queue.ts`
`buildControlSection` + `dashboard/packages/shared/src/control.ts`) -- its exact snake_case shape
(every listed key present, explicit `null` never omitted) is pinned by that reader, not
renegotiable here.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

# `sweep` lives beside this file with no package `__init__.py` under `llama/` -- see batch.py's
# own comment on this exact gotcha. A plain top-level import works whenever the importer has
# already put this directory on `sys.path` (true when this file is run directly as a script, and
# when a test under `tests/` inserts this directory itself).
import sweep

MODULE_DIR = Path(__file__).resolve().parent

# -------------------------------------------------------------------------------------------
# Schema names (module constants, per the WP-3 execution brief -- these name the on-disk shapes
# documented in the plan's "Request schema and lifecycle" section; none of the shapes below embed
# a literal "schema" field themselves, matching the plan's own JSON examples exactly -- the
# CONTAINER-written request file DOES carry `"schema": "control-request-v1"` (WP-4's
# `ControlRequestFileV1`), but this module never requires it, treating it as a tolerated extra key
# like any other unlisted field).
# -------------------------------------------------------------------------------------------

SCHEMA_REQUEST = "control-request-v1"
SCHEMA_CLAIM = "control-claim-v1"
SCHEMA_RESULT = "control-result-v1"
SCHEMA_ACTION = "control-action-v1"
SCHEMA_DISPOSITION = "control-disposition-v1"

# -------------------------------------------------------------------------------------------
# Grammars and bounds (plan Dispatcher phase 1 "Ingest -- hardened").
# -------------------------------------------------------------------------------------------

REQUEST_BASENAME_RE = re.compile(r"^req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.json$")
HOST_ID_RE = re.compile(r"^(req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}|nightly-[0-9]{8})$")
QUEUE_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

VALID_ACTIONS = ("arm", "disarm", "start", "stop", "cancel")
MAX_REQUEST_BYTES = 8192
MINUTES_MIN, MINUTES_MAX = 15, 480

# Terminalization grace: "measured from the claim's claimed_at timestamp being >=5 minutes old at
# pass time (minutely cadence proxy) -- the dispatcher is stateless across passes; no in-memory
# pass counters" (WP-3 brief, pinning cycle-1 B3/cycle-2 B1's "5 passes" as wall-clock minutes).
GRACE_SECONDS = 5 * 60

WIRE_STATES = ("pending", "claimed", "running", "done", "rejected", "canceled", "launch-failed")
LAST_RUN_DISPOSITIONS = ("clean", "needs-attention", "crashed")

PRUNE_AGE_SECONDS = 30 * 24 * 3600

DEFAULT_PORT = 8899
DEFAULT_SCRATCH_CONTAINER = "bakeoff-scratch"
DEFAULT_ROUTER_CONTAINER = sweep.DEFAULT_CONTAINER_NAME


def _iso(dt: datetime) -> str:
    """`...Z`-suffixed UTC ISO-8601, matching the plan's own JSON examples and the
    `req-<UTCcompact>-...` id grammar's own timestamp convention (never `+00:00`)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# -------------------------------------------------------------------------------------------
# Paths -- the single source of truth for the filesystem contract above.
# -------------------------------------------------------------------------------------------


class Paths:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.control_dir = self.state_dir / "control"
        self.requests_dir = self.control_dir / "requests"
        self.host_dir = self.control_dir / "host"
        self.armed_marker = self.host_dir / "armed"
        self.claims_dir = self.host_dir / "claims"
        self.results_dir = self.host_dir / "results"
        self.actions_dir = self.host_dir / "actions"
        self.dispositions_dir = self.host_dir / "dispositions"
        self.live_path = self.host_dir / "live.json"
        self.last_run_path = self.host_dir / "last-run.json"
        self.status_path = self.host_dir / "status.json"
        self.cron_lock = self.state_dir / "cron.lock"
        self.dispatch_lock = self.state_dir / "dispatch.lock"

    def stop_marker(self, invocation_id: str) -> Path:
        return self.control_dir / f"stop-requested.{invocation_id}"

    def ensure_dirs(self) -> None:
        for d in (
            self.requests_dir, self.host_dir, self.claims_dir, self.results_dir,
            self.actions_dir, self.dispositions_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _resolve_state_dir(args: Optional[argparse.Namespace] = None) -> Path:
    override = getattr(args, "state_dir", None) if args is not None else None
    if override:
        return Path(override)
    env = os.environ.get("BAKEOFF_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local/state/inference-bakeoff"


def _resolve_queues_dir(args: Optional[argparse.Namespace] = None) -> Path:
    override = getattr(args, "queues_dir", None) if args is not None else None
    if override:
        return Path(override)
    env = os.environ.get("BAKEOFF_QUEUES_DIR")
    if env:
        return Path(env)
    return MODULE_DIR / "queues"


# -------------------------------------------------------------------------------------------
# Atomic writes -- temp file + os.replace in the SAME directory as the destination (never a
# cross-filesystem rename), matching batch.py's `save_state` convention exactly.
# -------------------------------------------------------------------------------------------


def _tmp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def _atomic_touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(path)
    tmp.touch()
    os.replace(tmp, path)


def _read_json_safe(
    path: Path, max_bytes: int = 1_048_576, anomalies: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Unparseable/oversized/missing control files are treated as absent, never trusted (plan
    crash-recovery matrix row 13) -- every dispatcher read of a host-written control file goes
    through this, never a bare `json.load`. A GENUINELY absent file (the common case -- most
    control files don't exist most of the time) is silent, never anomaly-worthy. A file that
    EXISTS but is oversized/unparseable/the wrong JSON shape is still treated as absent for every
    caller's control-flow purposes, but -- when `anomalies` is given -- also COUNTED there as
    `"unreadable-control-file: <basename>"` (row 13's "anomaly counter", surfaced end to end in
    `status.json.anomalies`). Callers that don't pass `anomalies` (most of them -- this file is
    read dozens of times per pass) get the original silent-absent behavior unchanged."""
    try:
        st = path.stat()
    except OSError:
        return None  # genuinely absent -- never an anomaly.
    if st.st_size > max_bytes:
        if anomalies is not None:
            anomalies.append(f"unreadable-control-file: {path.name}")
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if anomalies is not None:
            anomalies.append(f"unreadable-control-file: {path.name}")
        return None
    if not isinstance(data, dict):
        if anomalies is not None:
            anomalies.append(f"unreadable-control-file: {path.name}")
        return None
    return data


# -------------------------------------------------------------------------------------------
# Locking -- non-blocking flock, used both for `dispatch.lock` (self-serialization) and for
# probing `cron.lock` (admission probe -- acquire-then-immediately-release, never held by the
# dispatcher itself; the honest at-most-once seam is `run-window.sh`'s own hold of this same
# lock for the run's whole lifetime).
# -------------------------------------------------------------------------------------------


def _flock_nonblocking_acquire(lock_path: Path) -> Optional[int]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _flock_release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


def _probe_lock_free(lock_path: Path) -> bool:
    """Non-blocking probe: True iff the lock was free at probe time (acquired, then immediately
    released -- this function never holds the lock). The probe->flock gap this leaves is the
    plan's own honest at-most-once seam, not a bug: a losing race surfaces as the wrapper's own
    `stage:"busy"` result, dispositioned `launch-failed(busy)`."""
    fd = _flock_nonblocking_acquire(lock_path)
    if fd is None:
        return False
    _flock_release(fd)
    return True


# -------------------------------------------------------------------------------------------
# Process/boot identity -- real `/proc` reads, injectable everywhere they're used.
# -------------------------------------------------------------------------------------------


def _read_boot_id(path: str = "/proc/sys/kernel/random/boot_id") -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def _parse_stat_fields(text: str) -> Optional[List[str]]:
    """`/proc/<pid>/stat` is `pid (comm) state ppid pgrp ...` -- `comm` can itself contain spaces
    or parentheses, so field-splitting must anchor on the LAST `)` rather than naive whitespace
    splitting from the start."""
    rparen = text.rfind(")")
    if rparen == -1:
        return None
    rest = text[rparen + 2:].split()
    return rest if rest else None


def _read_pid_starttime(pid: int) -> Optional[str]:
    """Field 22 (starttime) of `/proc/<pid>/stat` -- `rest[0]` is state (field 3), so starttime
    (field 22) is `rest[22 - 3]` = `rest[19]`."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = _parse_stat_fields(text)
    if fields is None or len(fields) <= 19:
        return None
    return fields[19]


def _read_pgid(pid: int) -> Optional[int]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = _parse_stat_fields(text)
    if fields is None or len(fields) <= 2:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None


def _pgid_alive(pgid: int) -> bool:
    """True iff ANY process on this host currently reports `pgrp == pgid` (field 5 of its own
    `/proc/<pid>/stat`, i.e. `rest[2]` in `_parse_stat_fields`'s indexing) -- the group-liveness
    test the plan pins ("a wrapper pid + starttime alone is NOT run liveness")."""
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        if _read_pgid(int(entry.name)) == pgid:
            return True
    return False


def _default_pid_alive(pid: Optional[int], expected_starttime: Optional[str]) -> bool:
    """Specific-pid liveness (used for the wrapper-lost anomaly: pgid can still be alive via a
    surviving `batch.py` child while the wrapper itself is gone). Confirms the SAME process via
    starttime when it's available, defending (best-effort) against pid reuse."""
    if not isinstance(pid, int):
        return False
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    if expected_starttime is None:
        return True
    fields = _parse_stat_fields(text)
    return bool(fields) and len(fields) > 19 and fields[19] == expected_starttime


def _group_alive(raw: Optional[Dict[str, Any]], boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool]) -> bool:
    """Shared group-liveness test for both `live.json` and a `results/<inv-id>.json` record (same
    `pgid`/`boot_id` identity fields in both schemas). A `boot_id` mismatch is dead by definition
    (pid/starttime/pgid numbers recur across boots)."""
    if not isinstance(raw, dict):
        return False
    if raw.get("boot_id") != boot_id_fn():
        return False
    pgid = raw.get("pgid")
    if not isinstance(pgid, int):
        return False
    return pgid_alive_fn(pgid)


# -------------------------------------------------------------------------------------------
# Request writers (dispositions/actions/claims -- the dispatcher's own evidence channel).
# -------------------------------------------------------------------------------------------


def _write_disposition(paths: Paths, request_id: str, state: str, reason: Optional[str], now_fn: Optional[Callable[[], datetime]] = None) -> None:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    dst = paths.dispositions_dir / f"{request_id}.json"
    if dst.exists():
        return  # write-once, retained forever -- never silently overwritten.
    body = {"request_id": request_id, "state": state, "reason": reason, "decided_at": _iso(now_fn())}
    _atomic_write_json(dst, body)


def _write_action(
    paths: Paths, request_id: str, action: str,
    target_invocation_id: Optional[str], target_request_id: Optional[str],
    now_fn: Optional[Callable[[], datetime]] = None,
) -> None:
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    dst = paths.actions_dir / f"{request_id}.json"
    if dst.exists():
        return  # replay resumes from what's already recorded -- never re-derived/overwritten.
    body = {
        "request_id": request_id, "action": action,
        "target_invocation_id": target_invocation_id, "target_request_id": target_request_id,
        "recorded_at": _iso(now_fn()),
    }
    _atomic_write_json(dst, body)


# -------------------------------------------------------------------------------------------
# Phase 1: Ingest (hardened, fail-closed).
# -------------------------------------------------------------------------------------------


class IngestedRequest(NamedTuple):
    request_id: str
    action: str
    queue_stem: Optional[str]
    minutes: Optional[int]
    target_request_id: Optional[str]
    requested_at: str


def _safe_read_request_bytes(path: Path, max_bytes: int = MAX_REQUEST_BYTES) -> Optional[bytes]:
    """`lstat` regular-file check, `O_NOFOLLOW|O_NONBLOCK` open, hard size bound BEFORE parse --
    a request whose basename we trust (regex-matched) but whose content we do NOT (drop-box files
    are container-written, outside the trust boundary)."""
    try:
        lst = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(lst.st_mode):
        return None
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or fst.st_size > max_bytes:
            return None
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data
    finally:
        os.close(fd)


def _validate_request_body(basename_id: str, data: bytes, queues_dir: Path) -> Tuple[Optional[IngestedRequest], Optional[str]]:
    """Returns `(ingested, None)` on success or `(None, reason)` on any validation failure --
    every failure disposition's `reason` (fail-closed, per plan Dispatcher phase 1)."""
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "malformed: invalid JSON"
    if not isinstance(body, dict):
        return None, "malformed: not a JSON object"
    # Every host output path is derived from the validated BASENAME only, never from body
    # content -- this equality check is the one place body content is allowed to matter at all
    # (it must agree with the trusted basename, or the whole request is rejected).
    if body.get("request_id") != basename_id:
        return None, "malformed: request_id does not match filename"
    action = body.get("action")
    if action not in VALID_ACTIONS:
        return None, "malformed: invalid action"
    requested_at = body.get("requested_at")
    if not isinstance(requested_at, str) or not requested_at:
        return None, "malformed: missing requested_at"
    queue_stem = body.get("queue_stem")
    minutes = body.get("minutes")
    target_request_id = body.get("target_request_id")
    if action == "start":
        if not isinstance(queue_stem, str) or not QUEUE_STEM_RE.match(queue_stem):
            return None, "malformed: invalid queue_stem"
        if not (queues_dir / f"{queue_stem}.json").exists():
            return None, "malformed: unknown queue_stem"
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not (MINUTES_MIN <= minutes <= MINUTES_MAX):
            return None, "malformed: invalid minutes"
    if action == "cancel":
        if not isinstance(target_request_id, str) or not HOST_ID_RE.match(target_request_id):
            return None, "malformed: invalid target_request_id"
    return (
        IngestedRequest(
            request_id=basename_id, action=action,
            queue_stem=queue_stem if isinstance(queue_stem, str) else None,
            minutes=minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else None,
            target_request_id=target_request_id if isinstance(target_request_id, str) else None,
            requested_at=requested_at,
        ),
        None,
    )


def ingest_pass(paths: Paths, queues_dir: Path, now_fn: Optional[Callable[[], datetime]] = None) -> Tuple[List[IngestedRequest], int]:
    """Enumerates `requests/`, hardens+validates each unprocessed matching file, and returns
    `(pending, ignored_invalid_names)`. `pending` is sorted `(requested_at, request_id)` ascending
    (cycle-2 E1 -- directory enumeration order is never load-bearing). A basename that doesn't
    match the id grammar (including WP-4's transient `.`-prefixed `*.tmp` exclusive-creation
    files) is IGNORED, counted, never dispositioned -- there's no trustworthy id to disposition
    against. A matching basename whose content fails hardening or schema validation IS
    dispositioned `rejected(malformed)` using the basename-derived id. Idempotence: a basename
    whose `dispositions/<id>.json` already exists is skipped entirely -- never re-derived from an
    in-memory set."""
    ignored = 0
    pending: List[IngestedRequest] = []
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
        data = _safe_read_request_bytes(paths.requests_dir / name)
        if data is None:
            _write_disposition(paths, basename_id, "rejected", "malformed: hardening check failed", now_fn)
            continue
        ingested, reason = _validate_request_body(basename_id, data, queues_dir)
        if ingested is None:
            _write_disposition(paths, basename_id, "rejected", reason, now_fn)
            continue
        pending.append(ingested)
    pending.sort(key=lambda r: (r.requested_at, r.request_id))
    return pending, ignored


# -------------------------------------------------------------------------------------------
# Phase 2: Act -- evidence-before-effect, effect-before-disposition, idempotent replay.
# -------------------------------------------------------------------------------------------


def _act_arm(paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime]) -> None:
    _atomic_touch(paths.armed_marker)
    _write_disposition(paths, req.request_id, "done", None, now_fn)


def _act_disarm(paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime]) -> None:
    try:
        paths.armed_marker.unlink()
    except FileNotFoundError:
        pass
    _write_disposition(paths, req.request_id, "done", None, now_fn)


def _act_stop(
    paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool],
) -> None:
    existing_action = _read_json_safe(paths.actions_dir / f"{req.request_id}.json")
    if existing_action is not None:
        # Replay rule (matrix row 16): resume from the RECORDED target, never re-derive from
        # current live state -- the marker path is bound to the recorded invocation id, so a
        # replayed/stale marker can never retarget a later run.
        target_inv = existing_action.get("target_invocation_id")
        if target_inv:
            live = _read_json_safe(paths.live_path)
            still_live = (
                live is not None and live.get("invocation_id") == target_inv
                and _group_alive(live, boot_id_fn, pgid_alive_fn)
            )
            if still_live:
                _atomic_touch(paths.stop_marker(target_inv))
        _write_disposition(paths, req.request_id, "done", "stop-delivered", now_fn)
        return

    live = _read_json_safe(paths.live_path)
    admissible = (
        live is not None and live.get("phase") == "running"
        and _group_alive(live, boot_id_fn, pgid_alive_fn)
    )
    if not admissible:
        _write_disposition(paths, req.request_id, "rejected", "no-live-run", now_fn)
        return
    target_inv = live.get("invocation_id")
    target_req = live.get("request_id")
    # FIRST the action (evidence), THEN the marker (effect), THEN the disposition -- load-bearing
    # order (cycle-2 B2).
    _write_action(paths, req.request_id, "stop", target_inv, target_req, now_fn)
    _atomic_touch(paths.stop_marker(target_inv))
    _write_disposition(
        paths, req.request_id, "done", "stop-delivered", now_fn,
    )


def _finish_cancel(paths: Paths, cancel_id: str, target_request_id: str, now_fn: Callable[[], datetime]) -> None:
    claim_exists = (paths.claims_dir / f"{target_request_id}.json").exists()
    if claim_exists:
        _write_disposition(paths, cancel_id, "rejected", "already-claimed", now_fn)
    else:
        _write_disposition(paths, target_request_id, "canceled", None, now_fn)
        _write_disposition(paths, cancel_id, "done", None, now_fn)


def _act_cancel(paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime]) -> None:
    existing_action = _read_json_safe(paths.actions_dir / f"{req.request_id}.json")
    if existing_action is not None:
        target_request_id = existing_action.get("target_request_id")
        if target_request_id:
            _finish_cancel(paths, req.request_id, target_request_id, now_fn)
        return

    target_request_id = req.target_request_id
    # `requests/` is the CONTAINER-WRITABLE drop-box, outside the trust boundary -- a plain
    # `_read_json_safe` (bare `open()`, follows symlinks) would let a symlink planted at the
    # target path read an arbitrary host path into this decision. Same hardened read every other
    # drop-box access uses (`_safe_read_request_bytes`: lstat regular-file, O_NOFOLLOW, 8 KiB
    # bound) -- None or a parse failure is treated as an invalid target, same fail-closed
    # `rejected(bad-target)` outcome as a missing/wrong-action/already-terminal target.
    target_body = None
    if target_request_id:
        target_data = _safe_read_request_bytes(paths.requests_dir / f"{target_request_id}.json")
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
        _write_disposition(paths, req.request_id, "rejected", "bad-target", now_fn)
        return
    _write_action(paths, req.request_id, "cancel", None, target_request_id, now_fn)
    _finish_cancel(paths, req.request_id, target_request_id, now_fn)


def _act_start(
    paths: Paths, req: IngestedRequest, now_fn: Callable[[], datetime],
    probe_cron_lock_fn: Callable[[], bool], launch_fn: Callable[[IngestedRequest], None],
) -> None:
    claim_path = paths.claims_dir / f"{req.request_id}.json"
    if claim_path.exists():
        return  # already claimed+launched in a prior pass -- idempotent no-op.
    if not probe_cron_lock_fn():
        _write_disposition(paths, req.request_id, "rejected", "busy", now_fn)
        return
    claim = {
        "request_id": req.request_id, "claimed_at": _iso(now_fn()),
        "queue_stem": req.queue_stem, "minutes": req.minutes,
    }
    # Claim written BEFORE launch (req 1) -- the durable evidence a start was admitted exists
    # even if the launch itself fails outright.
    _atomic_write_json(claim_path, claim)
    launch_fn(req)


def act_phase(
    pending: List[IngestedRequest], paths: Paths, now_fn: Callable[[], datetime],
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
    live = _read_json_safe(paths.live_path)
    if live is None or live.get("request_id") != request_id:
        return False
    return _group_alive(live, boot_id_fn, pgid_alive_fn)


def terminalize_phase(
    paths: Paths, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool],
) -> Tuple[List[str], Set[str]]:
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
    anomalies: List[str] = []
    late_launch_ids: Set[str] = set()
    now = now_fn()
    try:
        claim_files = sorted(paths.claims_dir.glob("*.json"))
    except OSError:
        claim_files = []
    for claim_file in claim_files:
        request_id = claim_file.stem
        disp_path = paths.dispositions_dir / f"{request_id}.json"
        result = _read_json_safe(paths.results_dir / f"{request_id}.json")
        if disp_path.exists():
            disp = _read_json_safe(disp_path)
            if result is not None and disp is not None and disp.get("state") == "launch-failed":
                anomalies.append(f"late-launch-after-terminal: {request_id}")
                late_launch_ids.add(request_id)
            continue
        claim = _read_json_safe(claim_file)
        if claim is None:
            continue
        claimed_at = _parse_iso(claim.get("claimed_at"))
        grace_elapsed = claimed_at is not None and (now - claimed_at).total_seconds() >= GRACE_SECONDS
        if result is None:
            if grace_elapsed and not _claim_group_alive(paths, request_id, boot_id_fn, pgid_alive_fn):
                _write_disposition(paths, request_id, "launch-failed", "no-launch-evidence", now_fn)
            continue
        stage = result.get("stage")
        if stage == "busy":
            _write_disposition(paths, request_id, "launch-failed", "busy", now_fn)
        elif stage == "wrapper-started":
            if grace_elapsed and not _group_alive(result, boot_id_fn, pgid_alive_fn):
                _write_disposition(paths, request_id, "launch-failed", "pre-run-crash", now_fn)
        elif stage == "exited":
            _write_disposition(paths, request_id, "done", None, now_fn)
        # stage == "running": still genuinely in flight (or the janitor's crash-recovery
        # territory, phase 4) -- never terminalized here.
    return anomalies, late_launch_ids


# -------------------------------------------------------------------------------------------
# Phase 4: Liveness/janitor -- process-group truth, workload-aware recovery.
# -------------------------------------------------------------------------------------------


def _map_last_run_disposition(run_exit: Optional[int], verify_exit: Optional[int], late_launch: bool) -> str:
    """Pinned (WP-3 brief): `clean` iff both exit codes are exactly 0 (a deadline/stop pause is a
    NORMAL `batch run` exit-0 outcome, not a needs-attention condition on its own -- queue state
    is surfaced separately); a `late-launch-after-terminal` anomaly attached to the run always
    forces `needs-attention` even over a clean exit pair; anything else needs attention.
    `crashed` is never returned here -- it's janitor-composed only, from a dead group with no
    `stage:"exited"` result at all."""
    if late_launch:
        return "needs-attention"
    if run_exit == 0 and verify_exit == 0:
        return "clean"
    return "needs-attention"


def compose_last_run(
    paths: Paths, now_fn: Callable[[], datetime],
    late_launch_ids: Set[str], janitor_crash_invocation_id: Optional[str] = None,
) -> None:
    """Composes `last-run.json` from the NEWEST terminal evidence across every
    `results/*.json` at `stage:"exited"` plus (if given) a freshly-janitor-detected crash --
    dispatcher-sole-writer, never overwritten with older/no-newer evidence."""
    existing = _read_json_safe(paths.last_run_path)
    candidates: List[Dict[str, Any]] = []
    try:
        result_files = list(paths.results_dir.glob("*.json"))
    except OSError:
        result_files = []
    for result_file in result_files:
        result = _read_json_safe(result_file)
        if result is None or result.get("stage") != "exited":
            continue
        inv_id = result.get("invocation_id") or result_file.stem
        run_exit, verify_exit = result.get("run_exit"), result.get("verify_exit")
        candidates.append({
            "invocation_id": inv_id, "request_id": inv_id,
            "ended_at": result.get("ended_at"),
            "run_exit": run_exit, "verify_exit": verify_exit,
            "disposition": _map_last_run_disposition(run_exit, verify_exit, inv_id in late_launch_ids),
        })
    if janitor_crash_invocation_id is not None:
        candidates.append({
            "invocation_id": janitor_crash_invocation_id, "request_id": janitor_crash_invocation_id,
            "ended_at": _iso(now_fn()),
            "run_exit": None, "verify_exit": None, "disposition": "crashed",
        })
    if not candidates:
        return
    newest = max(candidates, key=lambda c: c.get("ended_at") or "")
    if existing is not None and (newest.get("ended_at") or "") <= (existing.get("ended_at") or "") \
            and newest.get("invocation_id") == existing.get("invocation_id"):
        return
    _atomic_write_json(paths.last_run_path, newest)


def janitor_phase(
    paths: Paths, now_fn: Callable[[], datetime],
    boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool], pid_alive_fn: Callable[[Optional[int], Optional[str]], bool],
    find_pid_fn: Callable[..., Optional[int]], teardown_fn: Callable[..., Dict[str, Any]], router_start_fn: Callable[[str], None],
    port: int, scratch_container: str, router_container: str,
    late_launch_ids: Set[str],
) -> List[str]:
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
    anomaly -- liveness still wins for display, no disposition change."""
    anomalies: List[str] = []
    live = _read_json_safe(paths.live_path, anomalies=anomalies)
    if live is None:
        return anomalies

    boot_ok = live.get("boot_id") == boot_id_fn()
    pgid = live.get("pgid")
    group_alive = boot_ok and isinstance(pgid, int) and pgid_alive_fn(pgid)

    if group_alive:
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

    # Group empty (or dead-by-boot-mismatch) -- full crash recovery.
    try:
        found_pid = find_pid_fn(port, container=scratch_container)
    except Exception:  # noqa: BLE001 -- a docker-touching seam must never abort the pass
        found_pid = None
    if found_pid is not None:
        try:
            teardown_fn(port, container=scratch_container)
        except Exception:  # noqa: BLE001
            pass
    try:
        router_start_fn(router_container)
    except Exception:  # noqa: BLE001 -- never block termination on a failed restart attempt
        pass
    compose_last_run(paths, now_fn, late_launch_ids, janitor_crash_invocation_id=live.get("invocation_id"))
    try:
        paths.live_path.unlink()
    except FileNotFoundError:
        pass
    return anomalies


# -------------------------------------------------------------------------------------------
# Phase 5: Publish status.json.
# -------------------------------------------------------------------------------------------


def _project_request_state(paths: Paths, request_id: str) -> Tuple[str, Optional[str]]:
    """Wire-state precedence: disposition -> live -> claim -> pending. `live.json`'s presence is
    trusted as-is here -- the janitor (phase 4, already run this pass) is what keeps it accurate,
    removing it whenever the group is genuinely dead."""
    disp = _read_json_safe(paths.dispositions_dir / f"{request_id}.json")
    if disp is not None:
        state = disp.get("state")
        return (state if state in WIRE_STATES else "rejected"), disp.get("reason")
    live = _read_json_safe(paths.live_path)
    if live is not None and live.get("request_id") == request_id:
        return "running", None
    if (paths.claims_dir / f"{request_id}.json").exists():
        return "claimed", None
    return "pending", None


def _build_requests_digest(paths: Paths) -> List[Dict[str, Any]]:
    digest: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(paths.requests_dir))
    except OSError:
        names = []
    for name in names:
        if not REQUEST_BASENAME_RE.match(name):
            continue
        request_id = name[: -len(".json")]
        data = _safe_read_request_bytes(paths.requests_dir / name)
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
        state, reason = _project_request_state(paths, request_id)
        digest.append({
            "request_id": request_id, "action": action, "state": state,
            "reason": reason, "requested_at": requested_at,
        })
    digest.sort(key=lambda d: (d["requested_at"], d["request_id"]))
    return digest


def compose_status(
    paths: Paths, now_fn: Callable[[], datetime],
    ignored_invalid_names: int, anomalies: List[str],
) -> Dict[str, Any]:
    armed = paths.armed_marker.exists()
    live_raw = _read_json_safe(paths.live_path)
    live = None
    if live_raw is not None:
        live = {
            "request_id": live_raw.get("request_id"), "queue_stem": live_raw.get("queue_stem"),
            "phase": live_raw.get("phase"), "started_at": live_raw.get("started_at"),
        }
    last_run_raw = _read_json_safe(paths.last_run_path)
    last_run = None
    if last_run_raw is not None:
        last_run = {
            "request_id": last_run_raw.get("request_id"), "ended_at": last_run_raw.get("ended_at"),
            "run_exit": last_run_raw.get("run_exit"), "verify_exit": last_run_raw.get("verify_exit"),
            "disposition": last_run_raw.get("disposition"),
        }
    status = {
        "generated_at": _iso(now_fn()),
        "armed": armed,
        "live": live,
        "last_run": last_run,
        "anomalies": list(anomalies),
        "requests": _build_requests_digest(paths),
        "ignored_invalid_names": ignored_invalid_names,
    }
    _atomic_write_json(paths.status_path, status)
    return status


# -------------------------------------------------------------------------------------------
# Phase 6: Prune.
# -------------------------------------------------------------------------------------------


def _prune_if_old(path: Path, now: datetime) -> None:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return
    if (now - mtime).total_seconds() > PRUNE_AGE_SECONDS:
        try:
            path.unlink()
        except OSError:
            pass


def _invocation_currently_live(paths: Paths, invocation_id: str) -> bool:
    live = _read_json_safe(paths.live_path)
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


def _default_launch(req: IngestedRequest, paths: Paths, queues_dir: Path, run_window_path: Path) -> None:
    queue_path = queues_dir / f"{req.queue_stem}.json"
    argv = ["setsid", str(run_window_path), str(queue_path), "--request-id", req.request_id]
    if req.minutes is not None:
        argv += ["--minutes", str(req.minutes)]
    env = dict(os.environ)
    env["BAKEOFF_STATE_DIR"] = str(paths.state_dir)
    subprocess.Popen(  # noqa: S603 -- the real, live-capable launch path; never invoked by tests
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, env=env,
    )


# -------------------------------------------------------------------------------------------
# Orchestration -- one full dispatcher pass.
# -------------------------------------------------------------------------------------------


def dispatch_pass(
    paths: Paths,
    queues_dir: Path,
    now_fn: Optional[Callable[[], datetime]] = None,
    boot_id_fn: Optional[Callable[[], str]] = None,
    pgid_alive_fn: Optional[Callable[[int], bool]] = None,
    pid_alive_fn: Optional[Callable[[Optional[int], Optional[str]], bool]] = None,
    probe_cron_lock_fn: Optional[Callable[[], bool]] = None,
    launch_fn: Optional[Callable[[IngestedRequest], None]] = None,
    find_pid_fn: Optional[Callable[..., Optional[int]]] = None,
    teardown_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    router_start_fn: Optional[Callable[[str], None]] = None,
    port: int = DEFAULT_PORT,
    scratch_container: str = DEFAULT_SCRATCH_CONTAINER,
    router_container: str = DEFAULT_ROUTER_CONTAINER,
    run_window_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Runs the six phases in order (ingest -> act -> terminalize -> janitor -> publish -> prune)
    and returns the composed `status.json` dict. Every collaborator that could touch a real
    process/Docker is injectable with a safe real-but-never-exercised-by-tests default, same
    pattern as `batch.py.run_queue` -- see the module docstring."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    boot_id_fn = boot_id_fn or _read_boot_id
    pgid_alive_fn = pgid_alive_fn or _pgid_alive
    pid_alive_fn = pid_alive_fn or _default_pid_alive
    probe_cron_lock_fn = probe_cron_lock_fn or (lambda: _probe_lock_free(paths.cron_lock))
    run_window_path = run_window_path or (MODULE_DIR / "run-window.sh")
    launch_fn = launch_fn or (lambda req: _default_launch(req, paths, queues_dir, run_window_path))
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
        late_launch_ids,
    )
    status = compose_status(paths, now_fn, ignored, term_anomalies + janitor_anomalies)
    prune_phase(paths, now_fn)
    return status


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
    existing = _read_json_safe(path) or {"invocation_id": invocation_id}
    existing["stage"] = stage
    for key, value in (
        ("pid", pid), ("pgid", pgid), ("pid_starttime", pid_starttime), ("boot_id", boot_id),
        ("run_exit", run_exit), ("verify_exit", verify_exit),
    ):
        if value is not None:
            existing[key] = value
    if stage == "exited":
        existing["ended_at"] = ended_at or _iso(now_fn())
    _atomic_write_json(path, existing)
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
        "phase": "running", "started_at": started_at or _iso(now_fn()),
    }
    _atomic_write_json(paths.live_path, body)
    return body


def set_live_phase(paths: Paths, phase: str) -> Optional[Dict[str, Any]]:
    existing = _read_json_safe(paths.live_path)
    if existing is None:
        return None
    existing["phase"] = phase
    _atomic_write_json(paths.live_path, existing)
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
    paths = Paths(_resolve_state_dir(args))
    queues_dir = _resolve_queues_dir(args)
    paths.ensure_dirs()
    fd = _flock_nonblocking_acquire(paths.dispatch_lock)
    if fd is None:
        # Self-serialization hygiene only -- a busy dispatch.lock means another pass is already
        # running; quietly skip, never an error (NOT admission).
        return 0
    try:
        dispatch_pass(paths, queues_dir)
    finally:
        _flock_release(fd)
    return 0


def cmd_result_write(args: argparse.Namespace) -> int:
    paths = Paths(_resolve_state_dir(args))
    pid_starttime = args.pid_starttime
    if pid_starttime is None and args.pid is not None:
        pid_starttime = _read_pid_starttime(args.pid)
    boot_id = args.boot_id
    if boot_id is None and args.stage in ("wrapper-started",):
        boot_id = _read_boot_id()
    write_result(
        paths, args.invocation_id, args.stage,
        pid=args.pid, pgid=args.pgid, pid_starttime=pid_starttime, boot_id=boot_id,
        run_exit=args.run_exit, verify_exit=args.verify_exit, ended_at=args.ended_at,
    )
    return 0


def cmd_live_publish(args: argparse.Namespace) -> int:
    paths = Paths(_resolve_state_dir(args))
    pid_starttime = args.pid_starttime or _read_pid_starttime(args.pid)
    boot_id = args.boot_id or _read_boot_id()
    write_live(
        paths, args.invocation_id, args.request_id, args.queue_stem,
        args.pid, args.pgid, pid_starttime, boot_id, started_at=args.started_at,
    )
    return 0


def cmd_live_set_phase(args: argparse.Namespace) -> int:
    paths = Paths(_resolve_state_dir(args))
    result = set_live_phase(paths, args.phase)
    return 0 if result is not None else 1


def cmd_live_clear(args: argparse.Namespace) -> int:
    paths = Paths(_resolve_state_dir(args))
    clear_live(paths)
    return 0


def cmd_orphan_recover(args: argparse.Namespace) -> int:
    paths = Paths(_resolve_state_dir(args))  # unused directly, kept for CLI symmetry/future logging
    del paths
    result = orphan_recover(args.port, args.container)
    print(json.dumps(result))
    return 0


def cmd_clear_stop_marker(args: argparse.Namespace) -> int:
    paths = Paths(_resolve_state_dir(args))
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
