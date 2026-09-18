"""Host control-plane dispatcher CLI and wrapper writers (`plans/bakeoff-queue-control-plane.md`
WP-3). Covers `control_dispatcher.py`'s own retained code after the WP-R4 production split
(`plans/repository-tooling-structural-remediation.md`) -- "the CLI (`dispatch` + the wrapper
helper subcommands) and the `results/`/`live.json` writers `run-window.sh` shells out to" per
that module's own module-map docstring. The six phases, crash recovery, and disposition/status
projection coverage lives in `tests/test_control_phases.py` (WP-R6b of
`plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding F6 -- this
file had grown to 1,007 lines carrying both). Shared fakes for both files live in
`control_dispatcher_fixtures.py`.

No live Docker/GPU execution anywhere in this suite -- `launch_fn`/`find_pid_fn`/`teardown_fn`/
`now_fn` are always fakes; `dispatch_pass` never falls through to its real (process/Docker-
touching) defaults in any test here. Every test runs entirely inside a
`tempfile.TemporaryDirectory()` -- never the real `~/.local/state/inference-bakeoff/`."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from control_dispatcher_fixtures import BOOT_ID, REQ_A, _make_paths, _run_dispatch  # noqa: E402

import control_dispatcher as cd  # noqa: E402


class TestWrapperHelperFunctions(unittest.TestCase):
    def test_write_result_merges_identity_fields_across_stage_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            cd.write_result(paths, REQ_A, "wrapper-started", pid=1, pgid=1, pid_starttime="10", boot_id=BOOT_ID)
            cd.write_result(paths, REQ_A, "running")
            body = cd.write_result(paths, REQ_A, "exited", run_exit=0, verify_exit=0, ended_at="2026-08-24T00:02:00Z")
            self.assertEqual(body["pid"], 1)
            self.assertEqual(body["boot_id"], BOOT_ID)
            self.assertEqual(body["stage"], "exited")
            self.assertEqual(body["run_exit"], 0)
            on_disk = json.loads((paths.results_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(on_disk, body)

    def test_write_live_then_set_phase_then_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            cd.write_live(paths, "inv-1", "inv-1", "ladder-v1", 1, 1, "10", BOOT_ID, started_at="2026-08-24T00:00:00Z")
            self.assertEqual(json.loads(paths.live_path.read_text())["phase"], "running")
            cd.set_live_phase(paths, "verifying")
            self.assertEqual(json.loads(paths.live_path.read_text())["phase"], "verifying")
            cd.clear_live(paths)
            self.assertFalse(paths.live_path.exists())
            self.assertIsNone(cd.clear_live(paths))  # idempotent, no error on a second clear

    def test_orphan_recover_no_pid_found_does_not_call_teardown(self):
        calls = []
        result = cd.orphan_recover(
            8899, "bakeoff-scratch",
            find_pid_fn=lambda port, container=None: None,
            teardown_fn=lambda port, container=None: calls.append((port, container)),
        )
        self.assertEqual(result, {"found_pid": None, "torn_down": False})
        self.assertEqual(calls, [])

    def test_orphan_recover_pid_found_calls_teardown(self):
        result = cd.orphan_recover(
            8899, "bakeoff-scratch",
            find_pid_fn=lambda port, container=None: 4242,
            teardown_fn=lambda port, container=None: {"signalled": True},
        )
        self.assertEqual(result, {"found_pid": 4242, "torn_down": True, "detail": {"signalled": True}})

    def test_clear_stop_marker_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            marker = paths.stop_marker("inv-1")
            marker.touch()
            self.assertTrue(cd.clear_stop_marker(paths, "inv-1"))
            self.assertFalse(cd.clear_stop_marker(paths, "inv-1"))


class TestCliArgParsing(unittest.TestCase):
    def test_state_dir_and_queues_dir_resolve_from_env_when_flag_absent(self):
        parser = cd.build_arg_parser()
        args = parser.parse_args(["dispatch", "--state-dir", "/tmp/x", "--queues-dir", "/tmp/y"])
        self.assertEqual(str(cd.resolve_state_dir(args)), "/tmp/x")
        self.assertEqual(str(cd.resolve_queues_dir(args)), "/tmp/y")

    def test_result_write_requires_invocation_id_and_stage(self):
        parser = cd.build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["result-write", "--stage", "busy"])

    def test_result_write_rejects_unknown_stage(self):
        parser = cd.build_arg_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["result-write", "--invocation-id", "x", "--stage", "not-a-stage"])


class TestRestartFailureSurfacing(unittest.TestCase):
    """Plan restart-failure-surfacing WP-1: a recorded router-restart failure must survive
    results -> last-run -> status. The `map_last_run_disposition` unit-level pin for this same
    finding lives beside its siblings in `test_control_phases.py`'s
    `TestLastRunDispositionMapping.test_restart_failure_forces_needs_attention_even_over_clean_exit`."""

    def test_restart_failure_written_by_the_wrapper_reaches_status_json_with_needs_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            now = datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)
            cd.write_result(paths, "inv-1", "exited", run_exit=0, verify_exit=0,
                            restart_failed="CalledProcessError: docker start", now_fn=lambda: now)
            _run_dispatch(paths, Path(tmp) / "queues")
            last_run = json.loads(paths.last_run_path.read_text())
            self.assertEqual(last_run["disposition"], "needs-attention")
            self.assertEqual(last_run["restart_failed"], "CalledProcessError: docker start")
            status = json.loads(paths.status_path.read_text())
            self.assertEqual(status["last_run"]["restart_failed"], "CalledProcessError: docker start")
            self.assertEqual(status["last_run"]["disposition"], "needs-attention")

    def test_a_clean_result_without_the_field_reports_restart_failed_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            now = datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)
            cd.write_result(paths, "inv-1", "exited", run_exit=0, verify_exit=0, now_fn=lambda: now)
            _run_dispatch(paths, Path(tmp) / "queues")
            status = json.loads(paths.status_path.read_text())
            self.assertIsNone(status["last_run"]["restart_failed"])
            self.assertEqual(status["last_run"]["disposition"], "clean")


if __name__ == "__main__":
    unittest.main()
