"""Host control-plane dispatcher (plans/bakeoff-queue-control-plane.md WP-3). No live Docker/GPU
execution anywhere in this suite -- `launch_fn`/`probe_cron_lock_fn`/`find_pid_fn`/`teardown_fn`/
`router_start_fn`/`boot_id_fn`/`pgid_alive_fn`/`pid_alive_fn`/`now_fn` are always fakes;
`dispatch_pass` never falls through to its real (process/Docker-touching) defaults in any test
here. Every test runs entirely inside a `tempfile.TemporaryDirectory()` -- never the real
`~/.local/state/inference-bakeoff/`."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import control_dispatcher as cd  # noqa: E402

BOOT_ID = "boot-aaaa-1111"


def _fixed_now(base):
    return lambda: base


def _always_alive(pgid):
    return True


def _never_alive(pgid):
    return False


def _make_paths(tmp):
    paths = cd.Paths(Path(tmp) / "state")
    paths.ensure_dirs()
    return paths


def _write_queue_stem(tmp, stem="ladder-v1"):
    queues_dir = Path(tmp) / "queues"
    queues_dir.mkdir(exist_ok=True)
    (queues_dir / f"{stem}.json").write_text("{}", encoding="utf-8")
    return queues_dir


def _write_request(paths, request_id, action, requested_at="2026-08-24T00:00:00Z", **extra):
    body = {"schema": "control-request-v1", "request_id": request_id, "action": action, "requested_at": requested_at}
    body.update(extra)
    (paths.requests_dir / f"{request_id}.json").write_text(json.dumps(body), encoding="utf-8")
    return body


def _run_dispatch(paths, queues_dir, **overrides):
    kwargs = dict(
        now_fn=_fixed_now(datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)),
        boot_id_fn=lambda: BOOT_ID,
        pgid_alive_fn=_never_alive,
        pid_alive_fn=lambda pid, st: False,
        probe_cron_lock_fn=lambda: True,
        launch_fn=lambda req: None,
        find_pid_fn=lambda port, container=None: None,
        teardown_fn=lambda port, container=None: {},
        router_start_fn=lambda name: None,
    )
    kwargs.update(overrides)
    return cd.dispatch_pass(paths, queues_dir, **kwargs)


REQ_A = "req-20260824T000000Z-0011223344556677"
REQ_B = "req-20260824T000100Z-1122334455667788"


class TestIngestHardening(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = _make_paths(self.tmp.name)
        self.queues_dir = _write_queue_stem(self.tmp.name)

    def _disposition(self, request_id):
        return json.loads((self.paths.dispositions_dir / f"{request_id}.json").read_text())

    def test_non_matching_basename_is_ignored_and_counted_not_dispositioned(self):
        (self.paths.requests_dir / "not-a-request.json").write_text("{}")
        (self.paths.requests_dir / f".{REQ_A}.deadbeef.tmp").write_text("{}")  # WP-4's transient tmp
        pending, ignored = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(ignored, 2)
        self.assertFalse((self.paths.dispositions_dir / "not-a-request.json").exists())

    def test_symlink_rejected_malformed(self):
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"request_id": REQ_A, "action": "arm", "requested_at": "2026-08-24T00:00:00Z"}))
        (self.paths.requests_dir / f"{REQ_A}.json").symlink_to(target)
        pending, ignored = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(self._disposition(REQ_A)["state"], "rejected")

    def test_oversized_rejected_malformed(self):
        body = {"request_id": REQ_A, "action": "arm", "requested_at": "2026-08-24T00:00:00Z", "pad": "x" * 9000}
        (self.paths.requests_dir / f"{REQ_A}.json").write_text(json.dumps(body))
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(self._disposition(REQ_A)["state"], "rejected")

    def test_request_id_body_mismatch_rejected_malformed(self):
        _write_request(self.paths, REQ_A, "arm")
        # Overwrite with a body whose request_id disagrees with the basename.
        (self.paths.requests_dir / f"{REQ_A}.json").write_text(
            json.dumps({"request_id": REQ_B, "action": "arm", "requested_at": "2026-08-24T00:00:00Z"})
        )
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(self._disposition(REQ_A)["reason"], "malformed: request_id does not match filename")

    def test_invalid_action_rejected_malformed(self):
        _write_request(self.paths, REQ_A, "explode")
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(self._disposition(REQ_A)["state"], "rejected")

    def test_bad_queue_stem_rejected_malformed(self):
        _write_request(self.paths, REQ_A, "start", queue_stem="Not_Valid!", minutes=60)
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertIn("queue_stem", self._disposition(REQ_A)["reason"])

    def test_unknown_queue_stem_rejected_malformed(self):
        _write_request(self.paths, REQ_A, "start", queue_stem="nope-does-not-exist", minutes=60)
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(self._disposition(REQ_A)["state"], "rejected")

    def test_minutes_out_of_bounds_rejected_malformed(self):
        for bad_minutes in (14, 481, "60", True):
            with self.subTest(bad_minutes=bad_minutes):
                req_id = f"req-20260824T0002{abs(hash(bad_minutes)) % 100:02d}Z-0011223344556677"
                _write_request(self.paths, req_id, "start", queue_stem="ladder-v1", minutes=bad_minutes)
                pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
                self.assertNotIn(req_id, [p.request_id for p in pending])
                self.assertEqual(self._disposition(req_id)["state"], "rejected")

    def test_bad_target_request_id_rejected_malformed(self):
        _write_request(self.paths, REQ_A, "cancel", target_request_id="not-a-valid-id")
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertIn("target_request_id", self._disposition(REQ_A)["reason"])

    def test_valid_target_request_id_grammar_accepts_nightly_form(self):
        _write_request(self.paths, REQ_A, "cancel", target_request_id="nightly-20260824")
        pending, _ = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(len(pending), 1)

    def test_already_dispositioned_id_is_never_reprocessed(self):
        _write_request(self.paths, REQ_A, "arm")
        cd.write_disposition(self.paths, REQ_A, "done", None, lambda: datetime.now(timezone.utc))
        # Corrupt the request body now -- idempotence must come from the disposition file
        # existing, never from re-deriving/re-validating body content.
        (self.paths.requests_dir / f"{REQ_A}.json").write_text("not json at all")
        pending, ignored = cd.ingest_pass(self.paths, self.queues_dir)
        self.assertEqual(pending, [])
        self.assertEqual(ignored, 0)


class TestDeterministicOrdering(unittest.TestCase):
    def test_pending_sorted_by_requested_at_then_request_id_never_directory_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_B, "arm", requested_at="2026-08-24T00:00:00Z")
            _write_request(paths, REQ_A, "arm", requested_at="2026-08-24T00:00:00Z")
            pending, _ = cd.ingest_pass(paths, queues_dir)
            self.assertEqual([p.request_id for p in pending], sorted([REQ_A, REQ_B]))


class TestArmDisarm(unittest.TestCase):
    def test_arm_creates_marker_and_dispositions_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_A, "arm")
            _run_dispatch(paths, queues_dir)
            self.assertTrue(paths.armed_marker.exists())
            self.assertEqual(json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())["state"], "done")

    def test_disarm_removes_marker_and_dispositions_done_even_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_A, "disarm")
            _run_dispatch(paths, queues_dir)
            self.assertFalse(paths.armed_marker.exists())
            self.assertEqual(json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())["state"], "done")


class TestStop(unittest.TestCase):
    def _live(self, paths, **overrides):
        body = {
            "invocation_id": "req-live-inv", "request_id": "req-live-inv", "queue_stem": "ladder-v1",
            "pid": 111, "pgid": 111, "pid_starttime": "1", "boot_id": BOOT_ID,
            "phase": "running", "started_at": "2026-08-24T00:00:00Z",
        }
        body.update(overrides)
        cd.atomic_write_json(paths.live_path, body)
        return body

    def test_stop_against_verified_live_run_writes_action_then_marker_then_disposition(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            self._live(paths)
            _write_request(paths, REQ_A, "stop")
            _run_dispatch(paths, queues_dir, pgid_alive_fn=_always_alive)
            action = json.loads((paths.actions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(action["target_invocation_id"], "req-live-inv")
            self.assertTrue(paths.stop_marker("req-live-inv").exists())
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["state"], "done")
            self.assertEqual(disp["reason"], "stop-delivered")

    def test_stop_with_no_live_run_is_rejected_no_action_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_A, "stop")
            _run_dispatch(paths, queues_dir)
            self.assertFalse((paths.actions_dir / f"{REQ_A}.json").exists())
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["state"], "rejected")
            self.assertEqual(disp["reason"], "no-live-run")

    def test_stop_against_live_but_not_running_phase_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            self._live(paths, phase="verifying")
            _write_request(paths, REQ_A, "stop")
            _run_dispatch(paths, queues_dir, pgid_alive_fn=_always_alive)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["reason"], "no-live-run")

    def test_stop_against_group_dead_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            self._live(paths)
            _write_request(paths, REQ_A, "stop")
            _run_dispatch(paths, queues_dir, pgid_alive_fn=_never_alive)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["reason"], "no-live-run")


class TestCancel(unittest.TestCase):
    def test_missing_target_rejected_bad_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_A, "cancel", target_request_id=REQ_B)
            _run_dispatch(paths, queues_dir)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual((disp["state"], disp["reason"]), ("rejected", "bad-target"))

    def test_non_start_target_rejected_bad_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_B, "arm", requested_at="2026-08-23T00:00:00Z")
            _write_request(paths, REQ_A, "cancel", target_request_id=REQ_B)
            _run_dispatch(paths, queues_dir)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["reason"], "bad-target")

    def test_symlinked_target_rejected_bad_target_never_followed(self):
        # Drives `_act_cancel` DIRECTLY (via `act_phase`, bypassing `ingest_pass`) so this test
        # isolates `_act_cancel`'s OWN hardened target read -- going through the full
        # `_run_dispatch`/`ingest_pass` path would ALSO independently reject the symlinked
        # basename during ingest's own hardening (it enumerates every matching drop-box file,
        # including the cancel's target), which would mask whether `_act_cancel`'s own read is
        # actually hardened.
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            # A symlink planted AT the target's drop-box path -- requests/ is the
            # container-writable drop-box, outside the trust boundary; the target read must be
            # hardened the same way ingest's own read is (lstat regular-file, O_NOFOLLOW), never
            # a plain open() that follows it.
            outside_target = Path(tmp) / "outside-secret.json"
            outside_target.write_text(json.dumps({
                "request_id": REQ_B, "action": "start", "queue_stem": "ladder-v1",
                "minutes": 60, "requested_at": "2026-08-23T00:00:00Z",
            }))
            (paths.requests_dir / f"{REQ_B}.json").symlink_to(outside_target)

            cancel_req = cd.IngestedRequest(
                request_id=REQ_A, action="cancel", queue_stem=None, minutes=None,
                target_request_id=REQ_B, requested_at="2026-08-24T00:00:00Z",
            )
            now_fn = _fixed_now(datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc))
            cd.act_phase(
                [cancel_req], paths, now_fn, lambda: BOOT_ID, _never_alive,
                probe_cron_lock_fn=lambda: True, launch_fn=lambda req: None,
            )
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual((disp["state"], disp["reason"]), ("rejected", "bad-target"))
            # The symlink's destination content (a well-formed "start" body) was never trusted
            # into any output -- REQ_B never got claimed, launched, or terminally dispositioned.
            self.assertFalse((paths.claims_dir / f"{REQ_B}.json").exists())
            self.assertFalse((paths.dispositions_dir / f"{REQ_B}.json").exists())

    def test_already_terminal_target_rejected_bad_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_B, "start", queue_stem="ladder-v1", minutes=60, requested_at="2026-08-23T00:00:00Z")
            cd.write_disposition(paths, REQ_B, "launch-failed", "busy", lambda: datetime.now(timezone.utc))
            _write_request(paths, REQ_A, "cancel", target_request_id=REQ_B)
            _run_dispatch(paths, queues_dir)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["reason"], "bad-target")

    def test_valid_target_no_claim_cancels_target_and_dispositions_cancel_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            # Same requested_at as the cancel below -> (requested_at, request_id) ordering puts
            # REQ_A (cancel) BEFORE REQ_B (start) in this same pass, so the cancel legitimately
            # wins the race against the target's own not-yet-Act-processed start action --
            # exactly the plan's "cancel req wins (no claim)" case.
            _write_request(paths, REQ_B, "start", queue_stem="ladder-v1", minutes=60)
            _write_request(paths, REQ_A, "cancel", target_request_id=REQ_B)
            _run_dispatch(paths, queues_dir)
            target_disp = json.loads((paths.dispositions_dir / f"{REQ_B}.json").read_text())
            cancel_disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(target_disp["state"], "canceled")
            self.assertEqual(cancel_disp["state"], "done")
            action = json.loads((paths.actions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(action["target_request_id"], REQ_B)

    def test_valid_target_already_claimed_rejects_cancel_target_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_B, "start", queue_stem="ladder-v1", minutes=60)
            # claimed_at recent (within the terminalization grace window used by `_run_dispatch`'s
            # fixed now_fn) -- this test isolates the CANCEL rejection path, not phase 3's
            # separate no-launch-evidence terminalization of an abandoned claim.
            cd.atomic_write_json(paths.claims_dir / f"{REQ_B}.json", {
                "request_id": REQ_B, "claimed_at": "2026-08-24T00:04:30Z", "queue_stem": "ladder-v1", "minutes": 60,
            })
            _write_request(paths, REQ_A, "cancel", target_request_id=REQ_B)
            _run_dispatch(paths, queues_dir, pgid_alive_fn=_always_alive)
            cancel_disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual((cancel_disp["state"], cancel_disp["reason"]), ("rejected", "already-claimed"))
            self.assertFalse((paths.dispositions_dir / f"{REQ_B}.json").exists())


class TestReplayFromAction(unittest.TestCase):
    """Matrix row 16: a pending request with an existing action file resumes from the RECORDED
    target -- a dispatcher crash mid-act on a prior pass must never retarget a later run."""

    def test_stop_replay_recreates_marker_when_recorded_target_still_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            cd.atomic_write_json(paths.live_path, {
                "invocation_id": "req-live-inv", "request_id": "req-live-inv", "queue_stem": "ladder-v1",
                "pid": 1, "pgid": 1, "pid_starttime": "1", "boot_id": BOOT_ID,
                "phase": "running", "started_at": "2026-08-24T00:00:00Z",
            })
            _write_request(paths, REQ_A, "stop")
            cd.write_action(paths, REQ_A, "stop", "req-live-inv", "req-live-inv", lambda: datetime.now(timezone.utc))
            # Marker was NOT yet created (dispatcher crashed between action-write and marker-create).
            self.assertFalse(paths.stop_marker("req-live-inv").exists())
            _run_dispatch(paths, queues_dir, pgid_alive_fn=_always_alive)
            self.assertTrue(paths.stop_marker("req-live-inv").exists())
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["state"], "done")

    def test_stop_replay_does_not_recreate_marker_when_recorded_target_is_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            # live.json now names a DIFFERENT (later) invocation -- the recorded target must never
            # be retargeted onto it.
            cd.atomic_write_json(paths.live_path, {
                "invocation_id": "req-later-inv", "request_id": "req-later-inv", "queue_stem": "ladder-v1",
                "pid": 2, "pgid": 2, "pid_starttime": "2", "boot_id": BOOT_ID,
                "phase": "running", "started_at": "2026-08-24T01:00:00Z",
            })
            _write_request(paths, REQ_A, "stop")
            cd.write_action(paths, REQ_A, "stop", "req-dead-inv", "req-dead-inv", lambda: datetime.now(timezone.utc))
            _run_dispatch(paths, queues_dir, pgid_alive_fn=_always_alive)
            self.assertFalse(paths.stop_marker("req-dead-inv").exists())
            self.assertFalse(paths.stop_marker("req-later-inv").exists())  # never retargeted
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["state"], "done")

    def test_cancel_replay_resumes_from_recorded_target_without_reclaiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            queues_dir = _write_queue_stem(tmp)
            _write_request(paths, REQ_A, "cancel", target_request_id=REQ_B)
            cd.write_action(paths, REQ_A, "cancel", None, REQ_B, lambda: datetime.now(timezone.utc))
            _run_dispatch(paths, queues_dir)
            target_disp = json.loads((paths.dispositions_dir / f"{REQ_B}.json").read_text())
            cancel_disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(target_disp["state"], "canceled")
            self.assertEqual(cancel_disp["state"], "done")


class TestTerminalizationGrace(unittest.TestCase):
    def _claim(self, paths, request_id, claimed_at):
        cd.atomic_write_json(paths.claims_dir / f"{request_id}.json", {
            "request_id": request_id, "claimed_at": claimed_at, "queue_stem": "ladder-v1", "minutes": 60,
        })

    def test_no_result_grace_not_elapsed_stays_undecided(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:01:00Z")
            now = datetime(2026, 8, 24, 0, 3, 0, tzinfo=timezone.utc)  # 2 min old, grace is 5
            anomalies, late = cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _never_alive)
            self.assertFalse((paths.dispositions_dir / f"{REQ_A}.json").exists())
            self.assertEqual(anomalies, [])

    def test_no_result_grace_elapsed_and_group_dead_launch_failed_no_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:00:00Z")
            now = datetime(2026, 8, 24, 0, 6, 0, tzinfo=timezone.utc)  # 6 min old
            cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _never_alive)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual((disp["state"], disp["reason"]), ("launch-failed", "no-launch-evidence"))

    def test_busy_result_launch_failed_busy_immediately_no_grace_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:04:59Z")  # 1s old -- would never clear grace
            cd.atomic_write_json(paths.results_dir / f"{REQ_A}.json", {
                "invocation_id": REQ_A, "stage": "busy",
            })
            now = datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)
            cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _never_alive)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual((disp["state"], disp["reason"]), ("launch-failed", "busy"))

    def test_wrapper_started_group_dead_after_grace_pre_run_crash_from_result_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:00:00Z")
            cd.atomic_write_json(paths.results_dir / f"{REQ_A}.json", {
                "invocation_id": REQ_A, "stage": "wrapper-started",
                "pid": 999, "pgid": 999, "pid_starttime": "1", "boot_id": BOOT_ID,
            })
            now = datetime(2026, 8, 24, 0, 6, 0, tzinfo=timezone.utc)
            cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _never_alive)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual((disp["state"], disp["reason"]), ("launch-failed", "pre-run-crash"))

    def test_wrapper_started_group_alive_stays_in_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:00:00Z")
            cd.atomic_write_json(paths.results_dir / f"{REQ_A}.json", {
                "invocation_id": REQ_A, "stage": "wrapper-started",
                "pid": 999, "pgid": 999, "pid_starttime": "1", "boot_id": BOOT_ID,
            })
            now = datetime(2026, 8, 24, 0, 6, 0, tzinfo=timezone.utc)
            cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _always_alive)
            self.assertFalse((paths.dispositions_dir / f"{REQ_A}.json").exists())

    def test_exited_result_dispositions_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:00:00Z")
            cd.atomic_write_json(paths.results_dir / f"{REQ_A}.json", {
                "invocation_id": REQ_A, "stage": "exited",
                "run_exit": 0, "verify_exit": 0, "ended_at": "2026-08-24T00:02:00Z",
            })
            now = datetime(2026, 8, 24, 0, 2, 30, tzinfo=timezone.utc)
            cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _never_alive)
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["state"], "done")

    def test_late_evidence_after_launch_failed_terminal_is_an_anomaly_never_reconciled(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._claim(paths, REQ_A, "2026-08-24T00:00:00Z")
            cd.write_disposition(paths, REQ_A, "launch-failed", "no-launch-evidence", lambda: datetime.now(timezone.utc))
            cd.atomic_write_json(paths.results_dir / f"{REQ_A}.json", {
                "invocation_id": REQ_A, "stage": "wrapper-started",
                "pid": 1, "pgid": 1, "pid_starttime": "1", "boot_id": BOOT_ID,
            })
            now = datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc)
            anomalies, late_ids = cd.terminalize_phase(paths, lambda: now, lambda: BOOT_ID, _never_alive)
            self.assertIn(REQ_A, late_ids)
            self.assertTrue(any("late-launch-after-terminal" in a for a in anomalies))
            # The terminal disposition is never overwritten.
            disp = json.loads((paths.dispositions_dir / f"{REQ_A}.json").read_text())
            self.assertEqual(disp["reason"], "no-launch-evidence")


class TestJanitor(unittest.TestCase):
    def _live(self, paths, **overrides):
        body = {
            "invocation_id": "req-inv", "request_id": "req-inv", "queue_stem": "ladder-v1",
            "pid": 42, "pgid": 42, "pid_starttime": "10", "boot_id": BOOT_ID,
            "phase": "running", "started_at": "2026-08-24T00:00:00Z",
        }
        body.update(overrides)
        cd.atomic_write_json(paths.live_path, body)
        return body

    def test_group_dead_runs_full_recovery_and_composes_crashed_last_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths)
            calls = {"find": [], "teardown": [], "router_start": []}
            cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _never_alive, lambda pid, st: False,
                find_pid_fn=lambda port, container=None: (calls["find"].append((port, container)), 555)[1],
                teardown_fn=lambda port, container=None: (calls["teardown"].append((port, container)), {"ok": True})[1],
                router_start_fn=lambda name: calls["router_start"].append(name),
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(calls["find"], [(8899, "bakeoff-scratch")])
            self.assertEqual(calls["teardown"], [(8899, "bakeoff-scratch")])
            self.assertEqual(calls["router_start"], ["inference-engine-llama"])
            self.assertFalse(paths.live_path.exists())
            last_run = json.loads(paths.last_run_path.read_text())
            self.assertEqual(last_run["disposition"], "crashed")
            self.assertEqual(last_run["invocation_id"], "req-inv")
            self.assertIsNone(last_run["run_exit"])

    def test_group_dead_no_orphan_found_skips_teardown_but_still_restarts_router(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths)
            calls = {"teardown": [], "router_start": []}
            cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _never_alive, lambda pid, st: False,
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: calls["teardown"].append((port, container)),
                router_start_fn=lambda name: calls["router_start"].append(name),
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(calls["teardown"], [])
            self.assertEqual(calls["router_start"], ["inference-engine-llama"])

    def test_wrapper_dead_group_alive_is_anomaly_only_no_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths)
            anomalies = cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: False,  # group alive, specific wrapper pid dead
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(anomalies, ["running (wrapper lost)"])
            self.assertTrue(paths.live_path.exists())
            self.assertFalse(paths.last_run_path.exists())

    def test_boot_id_mismatch_is_dead_by_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths, boot_id="a-different-boot")
            cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: True,  # group WOULD look alive by pgid alone
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertFalse(paths.live_path.exists())
            last_run = json.loads(paths.last_run_path.read_text())
            self.assertEqual(last_run["disposition"], "crashed")

    def test_no_live_file_is_a_pure_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            anomalies = cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: True,
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(anomalies, [])
            self.assertFalse(paths.last_run_path.exists())

    def test_row13_unparseable_live_json_treated_as_absent_but_counted_as_anomaly(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            paths.live_path.parent.mkdir(parents=True, exist_ok=True)
            paths.live_path.write_text("not valid json {{{")
            anomalies = cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: True,
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(anomalies, ["unreadable-control-file: live.json"])
            # "treated as absent" -- no crash recovery attempted, nothing composed/removed.
            self.assertFalse(paths.last_run_path.exists())
            self.assertTrue(paths.live_path.exists())

    def test_row12_claim_live_id_mismatch_anomaly_when_live_names_a_request_with_no_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths, invocation_id=REQ_A, request_id=REQ_A)
            anomalies = cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: True,
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertIn(f"claim-live-id-mismatch: none vs {REQ_A}", anomalies)
            # Liveness still wins for display -- no disposition change, live.json untouched.
            self.assertTrue(paths.live_path.exists())
            self.assertFalse(paths.last_run_path.exists())
            self.assertFalse((paths.dispositions_dir / f"{REQ_A}.json").exists())

    def test_row12_no_anomaly_when_the_live_named_claim_actually_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths, invocation_id=REQ_A, request_id=REQ_A)
            cd.atomic_write_json(paths.claims_dir / f"{REQ_A}.json", {
                "request_id": REQ_A, "claimed_at": "2026-08-24T00:00:00Z", "queue_stem": "ladder-v1", "minutes": 60,
            })
            anomalies = cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: True,
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(anomalies, [])

    def test_row12_nightly_invocation_never_flagged_it_has_no_claim_by_design(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            self._live(paths, invocation_id="nightly-20260824", request_id="nightly-20260824")
            anomalies = cd.janitor_phase(
                paths, lambda: datetime(2026, 8, 24, 0, 10, 0, tzinfo=timezone.utc),
                lambda: BOOT_ID, _always_alive, lambda pid, st: True,
                find_pid_fn=lambda port, container=None: None,
                teardown_fn=lambda port, container=None: None,
                router_start_fn=lambda name: None,
                port=8899, scratch_container="bakeoff-scratch", router_container="inference-engine-llama",
                late_launch_ids=set(),
            )
            self.assertEqual(anomalies, [])


class TestReadJsonSafeAnomalies(unittest.TestCase):
    """Direct unit coverage of the `_read_json_safe(..., anomalies=)` primitive both row 12/13
    anomalies are built on."""

    def test_genuinely_absent_file_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            anomalies: list = []
            result = cd.read_json_safe(Path(tmp) / "does-not-exist.json", anomalies=anomalies)
            self.assertIsNone(result)
            self.assertEqual(anomalies, [])

    def test_oversized_existing_file_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.json"
            path.write_text(json.dumps({"pad": "x" * 100}))
            anomalies: list = []
            result = cd.read_json_safe(path, max_bytes=10, anomalies=anomalies)
            self.assertIsNone(result)
            self.assertEqual(anomalies, ["unreadable-control-file: big.json"])

    def test_unparseable_existing_file_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "garbage.json"
            path.write_text("{not json")
            anomalies: list = []
            result = cd.read_json_safe(path, anomalies=anomalies)
            self.assertIsNone(result)
            self.assertEqual(anomalies, ["unreadable-control-file: garbage.json"])

    def test_valid_file_never_anomaly_and_default_anomalies_none_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "good.json"
            path.write_text(json.dumps({"a": 1}))
            self.assertEqual(cd.read_json_safe(path), {"a": 1})  # no anomalies= at all -- unchanged default
            anomalies: list = []
            self.assertEqual(cd.read_json_safe(path, anomalies=anomalies), {"a": 1})
            self.assertEqual(anomalies, [])


class TestWireStateProjection(unittest.TestCase):
    def test_precedence_disposition_over_live_over_claim_over_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            # pending -- nothing at all.
            self.assertEqual(cd.project_request_state(paths, REQ_A), ("pending", None))
            # claimed.
            cd.atomic_write_json(paths.claims_dir / f"{REQ_A}.json", {"request_id": REQ_A})
            self.assertEqual(cd.project_request_state(paths, REQ_A), ("claimed", None))
            # running -- live.json names it.
            cd.atomic_write_json(paths.live_path, {"request_id": REQ_A, "invocation_id": REQ_A})
            self.assertEqual(cd.project_request_state(paths, REQ_A), ("running", None))
            # disposition wins over everything else.
            cd.write_disposition(paths, REQ_A, "done", None, lambda: datetime.now(timezone.utc))
            self.assertEqual(cd.project_request_state(paths, REQ_A), ("done", None))


class TestLastRunDispositionMapping(unittest.TestCase):
    def test_clean_iff_both_exit_codes_zero(self):
        self.assertEqual(cd.map_last_run_disposition(0, 0, False), "clean")

    def test_deadline_or_stop_pause_is_still_clean_exit_zero(self):
        # batch.py exits 0 for BOTH completion and a deadline/stop pause -- run_exit alone
        # cannot distinguish them, and per the pin it doesn't need to (queue state is surfaced
        # separately).
        self.assertEqual(cd.map_last_run_disposition(0, 0, False), "clean")

    def test_nonzero_run_exit_needs_attention(self):
        for code in (1, 130):
            self.assertEqual(cd.map_last_run_disposition(code, 0, False), "needs-attention")

    def test_nonzero_verify_exit_needs_attention(self):
        for code in (1, 2):
            self.assertEqual(cd.map_last_run_disposition(0, code, False), "needs-attention")

    def test_late_launch_anomaly_forces_needs_attention_even_over_clean_exit(self):
        self.assertEqual(cd.map_last_run_disposition(0, 0, True), "needs-attention")

    def test_crashed_is_never_returned_by_this_function(self):
        for run_exit in (None, 0, 1):
            for verify_exit in (None, 0, 1):
                for late in (False, True):
                    self.assertNotEqual(cd.map_last_run_disposition(run_exit, verify_exit, late), "crashed")


class TestPrune(unittest.TestCase):
    def test_old_files_pruned_recent_files_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).timestamp()
            recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
            old_req = paths.requests_dir / "req-20260101T000000Z-0011223344556677.json"
            recent_req = paths.requests_dir / "req-20260101T000001Z-0011223344556678.json"
            old_req.write_text("{}")
            recent_req.write_text("{}")
            os.utime(old_req, (old_ts, old_ts))
            os.utime(recent_req, (recent_ts, recent_ts))
            cd.prune_phase(paths, lambda: datetime.now(timezone.utc))
            self.assertFalse(old_req.exists())
            self.assertTrue(recent_req.exists())

    def test_claims_and_dispositions_never_pruned_regardless_of_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).timestamp()
            claim = paths.claims_dir / f"{REQ_A}.json"
            disp = paths.dispositions_dir / f"{REQ_A}.json"
            claim.write_text("{}")
            disp.write_text("{}")
            os.utime(claim, (old_ts, old_ts))
            os.utime(disp, (old_ts, old_ts))
            cd.prune_phase(paths, lambda: datetime.now(timezone.utc))
            self.assertTrue(claim.exists())
            self.assertTrue(disp.exists())

    def test_stop_marker_naming_dead_invocation_is_pruned_live_one_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            dead_marker = paths.stop_marker("req-dead-inv")
            live_marker = paths.stop_marker("req-live-inv")
            dead_marker.touch()
            live_marker.touch()
            cd.atomic_write_json(paths.live_path, {"invocation_id": "req-live-inv"})
            cd.prune_phase(paths, lambda: datetime.now(timezone.utc))
            self.assertFalse(dead_marker.exists())
            self.assertTrue(live_marker.exists())


class TestStatusShape(unittest.TestCase):
    def test_pinned_keys_present_with_explicit_nulls(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            now = datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)
            status = cd.compose_status(paths, lambda: now, 0, [])
            self.assertEqual(
                set(status.keys()),
                {"generated_at", "armed", "live", "last_run", "anomalies", "requests", "ignored_invalid_names"},
            )
            self.assertIsNone(status["live"])
            self.assertIsNone(status["last_run"])
            self.assertEqual(status["anomalies"], [])
            self.assertEqual(status["requests"], [])
            self.assertFalse(status["armed"])
            self.assertTrue(status["generated_at"].endswith("Z"))
            written = json.loads(paths.status_path.read_text())
            self.assertEqual(written, status)

    def test_live_and_last_run_projected_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            cd.atomic_write_json(paths.live_path, {
                "invocation_id": "req-inv", "request_id": "req-inv", "queue_stem": "ladder-v1",
                "pid": 1, "pgid": 1, "pid_starttime": "1", "boot_id": BOOT_ID,
                "phase": "running", "started_at": "2026-08-24T00:00:00Z",
            })
            cd.atomic_write_json(paths.last_run_path, {
                "invocation_id": "req-inv", "request_id": "req-inv", "ended_at": "2026-08-23T23:00:00Z",
                "run_exit": 0, "verify_exit": 0, "disposition": "clean",
            })
            now = datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)
            status = cd.compose_status(paths, lambda: now, 3, ["some anomaly"])
            self.assertEqual(status["live"], {
                "request_id": "req-inv", "queue_stem": "ladder-v1", "phase": "running", "started_at": "2026-08-24T00:00:00Z",
            })
            self.assertEqual(status["last_run"], {
                "request_id": "req-inv", "ended_at": "2026-08-23T23:00:00Z",
                "run_exit": 0, "verify_exit": 0, "disposition": "clean",
            })
            self.assertEqual(status["anomalies"], ["some anomaly"])
            self.assertEqual(status["ignored_invalid_names"], 3)

    def test_requests_digest_reflects_projected_state_and_is_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _make_paths(tmp)
            _write_request(paths, REQ_B, "arm", requested_at="2026-08-24T00:00:01Z")
            _write_request(paths, REQ_A, "arm", requested_at="2026-08-24T00:00:00Z")
            cd.write_disposition(paths, REQ_A, "done", None, lambda: datetime.now(timezone.utc))
            now = datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)
            status = cd.compose_status(paths, lambda: now, 0, [])
            self.assertEqual([r["request_id"] for r in status["requests"]], [REQ_A, REQ_B])
            self.assertEqual(status["requests"][0]["state"], "done")
            self.assertEqual(status["requests"][1]["state"], "pending")
            self.assertIsNone(status["requests"][1]["reason"])


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


if __name__ == "__main__":
    unittest.main()
