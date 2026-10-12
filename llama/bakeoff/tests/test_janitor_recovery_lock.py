"""Crash recovery is serialized with GPU ownership (WP-R1 of
`plans/2026-10-11-tn-code-review-remediation.md`; review finding F1 of
`reviews/2026-10-11-tn-code-review.md`). `cron.lock` is the single GPU-ownership lock: `run-window.sh`
holds it for a bake-off run's lifetime and the TTS window (`tts/tier/window.sh`) holds it while llama
is stopped and TTS serves. `janitor_phase` must therefore hold that same lock across every recovery
effect for a dead invocation -- scratch teardown, router `docker start`, `last-run.json`
publication and `live.json` removal -- and defer (evidence intact) when it is busy.

Every lock here is a REAL non-blocking flock on the tempdir's own `cron.lock`; the "TTS-equivalent
holder" is a second open file description on that path, which `flock(2)` treats as a distinct
owner even inside this one process. Docker/process seams stay fakes, as everywhere in this suite."""

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from control_dispatcher_fixtures import (  # noqa: E402
    BOOT_ID,
    _always_alive,
    _make_paths,
    _never_alive,
    _run_dispatch,
    _write_queue_stem,
)

import control_dispatcher as cd  # noqa: E402
import control_phases  # noqa: E402
from control_store import flock_nonblocking_acquire, flock_release  # noqa: E402

NOW = datetime(2026, 8, 24, 0, 10, 0, tzinfo=UTC)
DEFERRED = "recovery-deferred: cron.lock busy (req-inv)"


def _live(paths, **overrides):
    body = {
        "invocation_id": "req-inv", "request_id": "req-inv", "queue_stem": "ladder-v1",
        "pid": 42, "pgid": 42, "pid_starttime": "10", "boot_id": BOOT_ID,
        "phase": "running", "started_at": "2026-08-24T00:00:00Z",
    }
    body.update(overrides)
    cd.atomic_write_json(paths.live_path, body)
    return body


def _lock_is_free(paths):
    fd = flock_nonblocking_acquire(paths.cron_lock)
    if fd is None:
        return False
    flock_release(fd)
    return True


class _Recorder:
    """Records every recovery effect, and whether `cron.lock` was held (by someone other than a
    fresh opener) at the moment each effect ran."""

    def __init__(self, paths, found_pid=555):
        self.paths = paths
        self.found_pid = found_pid
        self.calls = []
        self.lock_free_during = []

    def _note(self, name):
        self.calls.append(name)
        self.lock_free_during.append(_lock_is_free(self.paths))

    def find_pid(self, port, container=None):
        self._note("find")
        return self.found_pid

    def teardown(self, port, container=None):
        self._note("teardown")
        return {"ok": True}

    def router_start(self, name):
        self._note("router_start")


def _janitor(paths, rec, pgid_alive_fn=_never_alive, **kw):
    return cd.janitor_phase(
        paths, lambda: NOW, lambda: BOOT_ID, pgid_alive_fn, lambda pid, st: False,
        find_pid_fn=rec.find_pid, teardown_fn=rec.teardown, router_start_fn=rec.router_start,
        port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
        late_launch_ids=set(), **kw,
    )


class TestJanitorRecoveryLock(unittest.TestCase):
    def _hold(self, paths):
        """A TTS-equivalent holder of `cron.lock` (window.sh's `flock -n` on the same path)."""
        fd = flock_nonblocking_acquire(paths.cron_lock)
        self.assertIsNotNone(fd)
        return fd

    def test_held_lock_blocks_every_recovery_effect_and_keeps_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            body = _live(paths)
            holder = self._hold(paths)
            try:
                rec = _Recorder(paths)
                anomalies = _janitor(paths, rec)
            finally:
                flock_release(holder)
            self.assertEqual(anomalies, [DEFERRED])
            self.assertEqual(rec.calls, [])  # no teardown probe, no teardown, no docker start
            self.assertEqual(json.loads(paths.live_path.read_text()), body)  # evidence intact
            self.assertFalse(paths.last_run_path.exists())  # no crashed disposition published

    def test_recovery_runs_on_the_next_pass_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)
            holder = self._hold(paths)
            try:
                self.assertEqual(_janitor(paths, _Recorder(paths)), [DEFERRED])
            finally:
                flock_release(holder)
            rec = _Recorder(paths)
            anomalies = _janitor(paths, rec)
            self.assertEqual(anomalies, [])
            self.assertEqual(rec.calls, ["find", "teardown", "router_start"])
            last_run = json.loads(paths.last_run_path.read_text())
            self.assertEqual(last_run["disposition"], "crashed")
            self.assertEqual(last_run["invocation_id"], "req-inv")
            self.assertFalse(paths.live_path.exists())
            self.assertTrue(_lock_is_free(paths))  # released in `finally`

    def test_lock_is_held_through_every_effect_not_probed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)
            rec = _Recorder(paths)
            seen = {}
            real_compose = control_phases.compose_last_run

            def compose_spy(*args, **kwargs):
                seen["compose_lock_free"] = _lock_is_free(paths)
                seen["live_present_at_compose"] = paths.live_path.exists()
                return real_compose(*args, **kwargs)

            with mock.patch.object(control_phases, "compose_last_run", compose_spy):
                _janitor(paths, rec)
            self.assertEqual(rec.calls, ["find", "teardown", "router_start"])
            self.assertEqual(rec.lock_free_during, [False, False, False])
            self.assertEqual(seen, {"compose_lock_free": False, "live_present_at_compose": True})
            self.assertFalse(paths.live_path.exists())
            self.assertTrue(_lock_is_free(paths))

    def test_lock_released_even_when_recovery_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)
            with mock.patch.object(control_phases, "compose_last_run", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    _janitor(paths, _Recorder(paths))
            self.assertTrue(paths.live_path.exists())  # removal is last, so evidence survives
            self.assertTrue(_lock_is_free(paths))

    def test_invocation_swapped_between_observation_and_acquisition_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)
            fresh = {}

            def acquire_after_swap():
                # A new run took the lock's place and published its own `live.json` between the
                # janitor's lock-free observation and its acquisition.
                fresh.update(_live(paths, invocation_id="req-new", request_id="req-new", pid=77, pgid=77))
                return flock_nonblocking_acquire(paths.cron_lock)

            rec = _Recorder(paths)
            anomalies = _janitor(paths, rec, acquire_cron_lock_fn=acquire_after_swap)
            self.assertEqual(anomalies, [])
            self.assertEqual(rec.calls, [])
            self.assertEqual(json.loads(paths.live_path.read_text()), fresh)  # new run untouched
            self.assertFalse(paths.last_run_path.exists())
            self.assertTrue(_lock_is_free(paths))

    def test_live_removed_between_observation_and_acquisition_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)

            def acquire_after_removal():
                paths.live_path.unlink()
                return flock_nonblocking_acquire(paths.cron_lock)

            rec = _Recorder(paths)
            self.assertEqual(_janitor(paths, rec, acquire_cron_lock_fn=acquire_after_removal), [])
            self.assertEqual(rec.calls, [])
            self.assertFalse(paths.last_run_path.exists())
            self.assertTrue(_lock_is_free(paths))

    def test_phase_only_change_is_still_the_same_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)

            def acquire_after_phase_rewrite():
                _live(paths, phase="verifying")  # `set_live_phase` rewrites only `phase`
                return flock_nonblocking_acquire(paths.cron_lock)

            rec = _Recorder(paths)
            _janitor(paths, rec, acquire_cron_lock_fn=acquire_after_phase_rewrite)
            self.assertEqual(rec.calls, ["find", "teardown", "router_start"])
            self.assertFalse(paths.live_path.exists())

    def test_alive_group_never_contends_for_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _live(paths)
            attempts = []
            rec = _Recorder(paths)
            _janitor(
                paths, rec, pgid_alive_fn=_always_alive,
                acquire_cron_lock_fn=lambda: attempts.append(1),
            )
            self.assertEqual(attempts, [])
            self.assertEqual(rec.calls, [])
            self.assertTrue(paths.live_path.exists())

    def test_dispatch_pass_defers_under_held_lock_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _live(paths)
            starts = []
            holder = self._hold(paths)
            try:
                status = _run_dispatch(paths, queues_dir, router_start_fn=starts.append)
            finally:
                flock_release(holder)
            self.assertIn(DEFERRED, status["anomalies"])
            self.assertEqual(starts, [])
            self.assertIsNotNone(status["live"])
            self.assertTrue(paths.live_path.exists())
            status = _run_dispatch(paths, queues_dir, router_start_fn=starts.append)
            self.assertEqual(starts, ["inference-engine-llama"])
            self.assertIsNone(status["live"])
            self.assertEqual(status["last_run"]["disposition"], "crashed")
            self.assertTrue(_lock_is_free(paths))


if __name__ == "__main__":
    unittest.main()
