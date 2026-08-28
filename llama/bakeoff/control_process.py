#!/usr/bin/env python3
"""Process/boot identity reads for the control plane (WP-R4 of
`plans/repository-tooling-structural-remediation.md`; review finding F9 -- file size).

Extracted verbatim from `control_dispatcher.py`'s "Process/boot identity" banner. These are the
only real `/proc` reads in the control plane, and they are the reason "is the run alive?" can be
answered from durable evidence rather than from elapsed time.

**Injectable everywhere they are used**: `dispatch_pass` takes `boot_id_fn`/`pgid_alive_fn`/
`pid_alive_fn` parameters that default to the functions here, and the whole test suite passes
fakes -- no test in `tests/` ever reads real `/proc`. Keep that seam: these functions are the
"real-but-never-exercised-by-tests defaults", same pattern as `batch.py`/`sweep.py`.

The liveness rule the plan pins: a wrapper pid + starttime alone is NOT run liveness. Group
liveness (`pgid_alive`) is, and a `boot_id` mismatch is dead by definition -- pid, starttime, and
pgid numbers all recur across boots, so without the boot id they are not identities at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "default_pid_alive",
    "group_alive",
    "parse_stat_fields",
    "pgid_alive",
    "read_boot_id",
    "read_pgid",
    "read_pid_starttime",
]


def read_boot_id(path: str = "/proc/sys/kernel/random/boot_id") -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def parse_stat_fields(text: str) -> Optional[List[str]]:
    """`/proc/<pid>/stat` is `pid (comm) state ppid pgrp ...` -- `comm` can itself contain spaces
    or parentheses, so field-splitting must anchor on the LAST `)` rather than naive whitespace
    splitting from the start."""
    rparen = text.rfind(")")
    if rparen == -1:
        return None
    rest = text[rparen + 2:].split()
    return rest if rest else None


def read_pid_starttime(pid: int) -> Optional[str]:
    """Field 22 (starttime) of `/proc/<pid>/stat` -- `rest[0]` is state (field 3), so starttime
    (field 22) is `rest[22 - 3]` = `rest[19]`."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = parse_stat_fields(text)
    if fields is None or len(fields) <= 19:
        return None
    return fields[19]


def read_pgid(pid: int) -> Optional[int]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    fields = parse_stat_fields(text)
    if fields is None or len(fields) <= 2:
        return None
    try:
        return int(fields[2])
    except ValueError:
        return None


def pgid_alive(pgid: int) -> bool:
    """True iff ANY process on this host currently reports `pgrp == pgid` (field 5 of its own
    `/proc/<pid>/stat`, i.e. `rest[2]` in `parse_stat_fields`'s indexing) -- the group-liveness
    test the plan pins ("a wrapper pid + starttime alone is NOT run liveness")."""
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        if read_pgid(int(entry.name)) == pgid:
            return True
    return False


def default_pid_alive(pid: Optional[int], expected_starttime: Optional[str]) -> bool:
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
    fields = parse_stat_fields(text)
    return bool(fields) and len(fields) > 19 and fields[19] == expected_starttime


def group_alive(raw: Optional[Dict[str, Any]], boot_id_fn: Callable[[], str], pgid_alive_fn: Callable[[int], bool]) -> bool:
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
