#!/usr/bin/env python3
"""Durable file primitives for the control plane (WP-R4 of
`plans/repository-tooling-structural-remediation.md`; review finding F9 -- file size).

Extracted verbatim from `control_dispatcher.py`'s "Paths", "Atomic writes", and "Locking"
banners plus its hardened drop-box read. Nothing here knows about phases, actions, or
dispositions -- this module owns only *where* control files live and *how* they are safely
written, read, and serialized against.

Three disciplines in here are load-bearing and must not be "simplified":

- **Atomic writes are temp-file + `os.replace` in the SAME directory as the destination**, never
  a cross-filesystem rename (which is not atomic and would leave a torn file on a crash). Every
  durable control file goes through `atomic_write_json`/`atomic_touch`.
- **Locking is non-blocking `flock`**, used for `dispatch.lock` (this dispatcher's own
  self-serialization hygiene) and for PROBING `cron.lock` (acquire-then-immediately-release; the
  dispatcher never holds the run lock -- `run-window.sh` does, for the run's whole lifetime).
- **Reads of control files never trust the bytes.** `read_json_safe` treats
  unparseable/oversized/wrong-shape files as absent (plan crash-recovery matrix row 13), and
  `safe_read_request_bytes` additionally refuses to follow symlinks, because `control/requests/`
  is the container-writable drop-box and sits outside the host trust boundary.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from control_protocol import MAX_REQUEST_BYTES

MODULE_DIR = Path(__file__).resolve().parent

__all__ = [
    "MODULE_DIR",
    "Paths",
    "atomic_touch",
    "atomic_write_json",
    "flock_nonblocking_acquire",
    "flock_release",
    "probe_lock_free",
    "read_json_safe",
    "resolve_queues_dir",
    "resolve_state_dir",
    "safe_read_request_bytes",
    "tmp_path_for",
]


# -------------------------------------------------------------------------------------------
# Paths -- the single source of truth for the filesystem contract documented in
# `control_dispatcher.py`'s module docstring.
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


def resolve_state_dir(args: Optional[argparse.Namespace] = None) -> Path:
    override = getattr(args, "state_dir", None) if args is not None else None
    if override:
        return Path(override)
    env = os.environ.get("BAKEOFF_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local/state/inference-bakeoff"


def resolve_queues_dir(args: Optional[argparse.Namespace] = None) -> Path:
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


def tmp_path_for(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")


def atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def atomic_touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path_for(path)
    tmp.touch()
    os.replace(tmp, path)


def read_json_safe(
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


def safe_read_request_bytes(path: Path, max_bytes: int = MAX_REQUEST_BYTES) -> Optional[bytes]:
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


# -------------------------------------------------------------------------------------------
# Locking -- non-blocking flock, used for `dispatch.lock` (self-serialization), for probing
# `cron.lock` (start admission -- acquire-then-immediately-release; the honest at-most-once seam
# is `run-window.sh`'s own hold of this same lock for the run's whole lifetime), and for HOLDING
# `cron.lock` across the janitor's crash-recovery effects (`control_phases.janitor_phase` --
# recovery is a GPU-ownership effect, so it is never a probe).
# -------------------------------------------------------------------------------------------


def flock_nonblocking_acquire(lock_path: Path) -> Optional[int]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def flock_release(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


def probe_lock_free(lock_path: Path) -> bool:
    """Non-blocking probe: True iff the lock was free at probe time (acquired, then immediately
    released -- this function never holds the lock). The probe->flock gap this leaves is the
    plan's own honest at-most-once seam, not a bug: a losing race surfaces as the wrapper's own
    `stage:"busy"` result, dispositioned `launch-failed(busy)`."""
    fd = flock_nonblocking_acquire(lock_path)
    if fd is None:
        return False
    flock_release(fd)
    return True
