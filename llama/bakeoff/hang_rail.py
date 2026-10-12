#!/usr/bin/env python3
"""Unattended safety rail: kernel hang-signature detection (WP-R1 extraction from `batch.py`).

An RDNA4 GPU hang is the one failure this harness cannot measure its way out of -- the card is
wedged and every subsequent cell would produce garbage. `run_queue` polls this rail at each cell
boundary and stops the whole queue `aborted-hang` on the first match.

Provenance (verbatim signature, copied deliberately rather than re-derived): both lines are
quoted directly from `fleet-tools/references/rdna4-gpu-hang.md` ("The signature" section, and
"Automated recovery": "either `MES might be in unrecoverable state` or `Failed to evict process
queues` alone is enough -- journald delivers lines one at a time, so the full cascade never
arrives as one matchable unit") and match `fleet-tools/scripts/gpu-hang-watchdog.sh`'s own
`SIGNATURE_RE` byte-for-byte.

`detect_hang_signature` is the pure, tested piece. `poll_journal_for_hang` and
`make_live_hang_check_fn` wrap it with the real `journalctl` subprocess call and are NEVER
invoked by this repo's tests -- the same "real function exists, only ever invoked live" pattern
as `sweep.py`'s `discover_bridge_ip`/`read_vram_used_bytes`.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

HANG_SIGNATURE_RE = re.compile(r"MES might be in unrecoverable state|Failed to evict process queues")


def detect_hang_signature(lines: Iterable[str]) -> str | None:
    """Scans `lines` (any iterable of decoded kernel-log lines -- a finite test list, or a real
    live source) and returns the first line matching the hang signature, or `None` if the
    iterable is exhausted with no match. Pure function, no I/O."""
    for line in lines:
        if HANG_SIGNATURE_RE.search(line):
            return line
    return None


def poll_journal_for_hang(since_iso: str, timeout: float = 5.0) -> str | None:
    """LIVE: `journalctl -k --since <since_iso>` (bounded, not `-f`) -- `run_queue`'s per-cell
    `hang_check_fn` calling this repeatedly achieves the same never-miss-a-line property as a
    persistent `-f` follower (each poll's `--since` covers everything since the last check)
    without a background thread/subprocess lifecycle to manage."""
    try:
        # errors='replace' (WP-5 finding #5, same class as sweep.py's startup-log read): kernel
        # log lines are not guaranteed UTF-8, and a strict decode would crash the hang rail
        # mid-run. The hang signatures are all ASCII -- lossy replacement never masks one.
        result = subprocess.run(
            ["journalctl", "-k", "--since", since_iso],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return detect_hang_signature(result.stdout.splitlines())


def make_live_hang_check_fn() -> Callable[[], str | None]:
    """The real per-cell hang check for the CLI path. `run_queue`'s `hang_check_fn` default is a
    no-op (correct for tests); `cmd_run` wires this in for live runs. Each call polls
    `journalctl -k` since the previous call (first call: since this factory was built), so
    repeated per-cell polling never misses a line."""
    state = {"since": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")}

    def _check() -> str | None:
        since = state["since"]
        state["since"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        return poll_journal_for_hang(since)

    return _check
