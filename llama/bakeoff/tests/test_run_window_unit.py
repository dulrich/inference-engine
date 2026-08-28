"""`run-window.sh` unit-level wrapper paths (plans/bakeoff-queue-control-plane.md WP-3): busy
exit, revocation exit, stale-marker clear at step 4, result-before-lock ordering. Drives the REAL
`run-window.sh` script as a real (but never GPU/docker-touching) subprocess -- every docker-facing
sub-step is stubbed via env override (`BAKEOFF_ENSURE_SCRATCH_CMD`/`BAKEOFF_ORPHAN_RECOVER_CMD`
set to a no-op `true`), and `BAKEOFF_BATCH_CMD` is stubbed to a trivial always-exit-0 script so
`batch.py` itself is never imported/invoked. State dir is always a tempdir -- never the real
`~/.local/state/inference-bakeoff/`."""

import fcntl
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
RUN_WINDOW = BAKEOFF_DIR / "run-window.sh"
QUEUE_PATH = BAKEOFF_DIR / "queues" / "ladder-v1.json"

TRIVIAL_BATCH_STUB = """#!/usr/bin/env bash
set -uo pipefail
case "$1" in
    run) exit 0 ;;
    verify) exit 0 ;;
    *) exit 2 ;;
esac
"""


class RunWindowFixture:
    """Sets up one tempdir-scoped state dir + a trivial batch stub, with docker-facing sub-steps
    stubbed to no-ops -- shared by every unit test below."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.state_dir = tmp / "state"
        self.runs_dir = tmp / "runs"
        self.state_dir.mkdir()
        self.runs_dir.mkdir()
        stub = tmp / "stub-batch.sh"
        stub.write_text(TRIVIAL_BATCH_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.env = dict(os.environ)
        self.env.update({
            "BAKEOFF_STATE_DIR": str(self.state_dir),
            "BAKEOFF_BATCH_CMD": str(stub),
            "BAKEOFF_ENSURE_SCRATCH_CMD": "true",
            "BAKEOFF_ORPHAN_RECOVER_CMD": "true",
            "BAKEOFF_RUNS_DIR": str(self.runs_dir),
        })
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()

    def result_path(self, invocation_id: str) -> Path:
        return self.state_dir / "control" / "host" / "results" / f"{invocation_id}.json"

    def disposition_path(self, request_id: str) -> Path:
        return self.state_dir / "control" / "host" / "dispositions" / f"{request_id}.json"

    def stop_marker_path(self, invocation_id: str) -> Path:
        return self.state_dir / "control" / f"stop-requested.{invocation_id}"

    def cron_lock_path(self) -> Path:
        return self.state_dir / "cron.lock"

    def run(self, invocation_id: str, extra_env=None, timeout=30):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(RUN_WINDOW), str(QUEUE_PATH), "--request-id", invocation_id],
            env=env, capture_output=True, text=True, timeout=timeout,
        )


class TestBusyExit(unittest.TestCase):
    def test_lock_held_elsewhere_writes_busy_result_and_exits_zero(self):
        with RunWindowFixture() as fx:
            lock_path = fx.cron_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # held for this whole test
                proc = fx.run("req-20260824T010000Z-0011223344556677")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(fx.result_path("req-20260824T010000Z-0011223344556677").read_text())
                self.assertEqual(result["stage"], "busy")
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


class TestRevocationExit(unittest.TestCase):
    def test_existing_disposition_at_step2_exits_without_running(self):
        with RunWindowFixture() as fx:
            invocation_id = "req-20260824T010000Z-0011223344556677"
            fx.disposition_path(invocation_id).parent.mkdir(parents=True, exist_ok=True)
            fx.disposition_path(invocation_id).write_text(json.dumps({
                "request_id": invocation_id, "state": "launch-failed", "reason": "no-launch-evidence",
                "decided_at": "2026-08-24T00:59:00Z",
            }))
            proc = fx.run(invocation_id)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("revoked", proc.stdout)
            # Step 1 (result write) still ran -- but never advanced past wrapper-started, and
            # live.json was never published (steps 3-5 never reached).
            result = json.loads(fx.result_path(invocation_id).read_text())
            self.assertEqual(result["stage"], "wrapper-started")
            self.assertFalse((fx.state_dir / "control" / "host" / "live.json").exists())


class TestStaleMarkerClearAtStep4(unittest.TestCase):
    def test_marker_bound_to_own_invocation_id_is_cleared_before_liveness_publish(self):
        with RunWindowFixture() as fx:
            invocation_id = "req-20260824T010000Z-0011223344556677"
            marker = fx.stop_marker_path(invocation_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
            self.assertTrue(marker.exists())
            proc = fx.run(invocation_id)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # By the time the whole window finished (batch stub exits immediately), the stale
            # marker present at window start must be long gone.
            self.assertFalse(marker.exists())
            result = json.loads(fx.result_path(invocation_id).read_text())
            self.assertEqual(result["stage"], "exited")


class TestResultBeforeLockOrdering(unittest.TestCase):
    def test_result_identity_fields_exist_even_when_lock_is_busy(self):
        """Step 1 (full process identity) is written BEFORE step 3 even attempts the lock --
        cycle-2 B1: a wrapper that dies before ever publishing live.json must still be testable
        for death. Proven here by holding the lock and confirming pid/pgid/boot_id landed on
        disk despite the run never getting past the busy check."""
        with RunWindowFixture() as fx:
            lock_path = fx.cron_lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                invocation_id = "req-20260824T010000Z-0011223344556677"
                proc = fx.run(invocation_id)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                result = json.loads(fx.result_path(invocation_id).read_text())
                for key in ("pid", "pgid", "pid_starttime", "boot_id"):
                    self.assertIn(key, result)
                self.assertIsNotNone(result["pid"])
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
