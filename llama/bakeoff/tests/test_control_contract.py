"""Cross-language control-plane contract tests (WP-R4 of
`plans/repository-tooling-structural-remediation.md`; review finding F4 -- boundary type
contracts).

This suite and `dashboard/apps/api/src/routes/controlContract.test.ts` read **the same committed
bytes** under `dashboard/apps/api/fixtures/control/`. Python is the WRITER of `status.json` and
the VALIDATOR of `requests/*.json`; TypeScript is the READER of `status.json` and the PRODUCER of
`requests/*.json`. Pinning both ends against one fixture tree is what makes the disk wire an
actual contract rather than two hand-synchronized guesses.

Fixture layout (`fixtures/control/`):

- `contract-v1/control/` -- a whole `<state-dir>/control` subtree: eight request files (one per
  action, plus absent / unknown / wrong-typed `schema`), a live `live.json`, and one exited
  `results/*.json`. `contract-v1/control/host/status.json` is the EXPECTED output of one
  `dispatch_pass` over that subtree, byte for byte.
- `contract-v1/queues/contract-queue{.json,.state.json}` -- the queue + queue-state pair.
- `crash-stop-after-action-before-effect/control/` -- crashed between the action evidence and
  the stop marker.
- `crash-cancel-after-effect-before-disposition/control/` -- crashed between the effect (the
  target's `canceled` disposition) and the cancel's own disposition.

Every test copies the fixture tree into a `tempfile.TemporaryDirectory()` before running
anything: the fixtures are frozen inputs, and the real `~/.local/state/inference-bakeoff/` is
never read or written by any test here. No Docker, no `/proc`, no process spawning -- every
live-capable seam is injected.
"""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import control_dispatcher as cd  # noqa: E402
import queue_file  # noqa: E402
import queue_state  # noqa: E402

REPO_ROOT = BAKEOFF_DIR.parent.parent
FIXTURES = REPO_ROOT / "dashboard/apps/api/fixtures/control"
CONTRACT = FIXTURES / "contract-v1"

BOOT_ID = "boot-contract-v1"
PASS_NOW = datetime(2026, 8, 27, 12, 10, 0, tzinfo=timezone.utc)

REQ_DISARM = "req-20260827T120000Z-00000000000000a1"
REQ_ARM = "req-20260827T120100Z-00000000000000a2"
REQ_START = "req-20260827T120200Z-00000000000000a3"
REQ_STOP = "req-20260827T120300Z-00000000000000a4"
REQ_CANCEL = "req-20260827T120400Z-00000000000000a5"
REQ_SCHEMA_ABSENT = "req-20260827T120500Z-00000000000000b1"
REQ_SCHEMA_UNKNOWN = "req-20260827T120600Z-00000000000000b2"
REQ_SCHEMA_WRONG_TYPE = "req-20260827T120700Z-00000000000000b3"

CRASH_STOP_ID = "req-20260827T130000Z-00000000000000c1"
CRASH_START_ID = "req-20260827T140000Z-00000000000000d1"
CRASH_CANCEL_ID = "req-20260827T140100Z-00000000000000d2"


def _stage(testcase, fixture_control_dir):
    """Copies a committed `control/` subtree into a fresh tempdir and returns its `Paths`. The
    committed tree is never opened for writing -- `shutil.copytree` is the only thing that ever
    touches it, and the tempdir is cleaned up by `addCleanup`."""
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    state_dir = Path(tmp.name) / "state"
    shutil.copytree(fixture_control_dir, state_dir / "control")
    return cd.Paths(state_dir)


def _run_pass(paths, queues_dir=None, now=PASS_NOW, group_alive=True):
    return cd.dispatch_pass(
        paths, queues_dir if queues_dir is not None else CONTRACT / "queues",
        now_fn=lambda: now,
        boot_id_fn=lambda: BOOT_ID,
        pgid_alive_fn=lambda pgid: group_alive,
        pid_alive_fn=lambda pid, st: group_alive,
        probe_cron_lock_fn=lambda: True,
        launch_fn=lambda req: None,
        find_pid_fn=lambda port, container=None: None,
        teardown_fn=lambda port, container=None: {},
        router_start_fn=lambda name: None,
    )


def _read_request_bytes(request_id):
    return (CONTRACT / "control/requests" / f"{request_id}.json").read_bytes()


class TestSchemaVersionEnforcement(unittest.TestCase):
    """WP-R4's one deliberate behavior change: `schema` is now REQUIRED to equal
    `SCHEMA_REQUEST`. Absent, unknown, and wrong-typed all reject with the SAME fixed reason --
    the offending value is never echoed into a host-written durable file, because the request
    body is container-written and sits outside the trust boundary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queues_dir = CONTRACT / "queues"

    def _validate(self, request_id):
        return cd.validate_request_body(request_id, _read_request_bytes(request_id), self.queues_dir)

    def test_every_valid_action_fixture_is_accepted(self):
        for request_id, action in (
            (REQ_DISARM, "disarm"), (REQ_ARM, "arm"), (REQ_START, "start"),
            (REQ_STOP, "stop"), (REQ_CANCEL, "cancel"),
        ):
            with self.subTest(action=action):
                result = self._validate(request_id)
                self.assertTrue(result.accepted)
                self.assertIsNone(result.reason)
                self.assertEqual(result.request.action, action)
                self.assertEqual(result.request.request_id, request_id)

    def test_absent_schema_is_rejected(self):
        result = self._validate(REQ_SCHEMA_ABSENT)
        self.assertFalse(result.accepted)
        self.assertIsNone(result.request)
        self.assertEqual(result.reason, cd.REQUEST_SCHEMA_REJECTION)

    def test_unknown_schema_version_is_rejected(self):
        body = json.loads(_read_request_bytes(REQ_SCHEMA_UNKNOWN))
        self.assertEqual(body["schema"], "control-request-v2")  # the fixture really is a v2
        result = self._validate(REQ_SCHEMA_UNKNOWN)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, cd.REQUEST_SCHEMA_REJECTION)

    def test_wrong_typed_schema_is_rejected(self):
        body = json.loads(_read_request_bytes(REQ_SCHEMA_WRONG_TYPE))
        self.assertNotIsInstance(body["schema"], str)
        result = self._validate(REQ_SCHEMA_WRONG_TYPE)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, cd.REQUEST_SCHEMA_REJECTION)

    def test_rejection_reason_never_echoes_the_offending_value(self):
        for request_id in (REQ_SCHEMA_ABSENT, REQ_SCHEMA_UNKNOWN, REQ_SCHEMA_WRONG_TYPE):
            with self.subTest(request_id=request_id):
                self.assertNotIn("control-request-v2", self._validate(request_id).reason)

    def test_request_id_mismatch_still_outranks_the_schema_check(self):
        # Ordering pin: every host output path is derived from the trusted BASENAME, so the
        # identity check stays first -- a body that is wrong about BOTH its id and its schema is
        # rejected for the id, never for the schema.
        payload = json.dumps({"schema": "control-request-v9", "request_id": "someone-elses-id",
                              "action": "arm", "requested_at": "2026-08-27T12:00:00Z"}).encode()
        result = cd.validate_request_body(REQ_ARM, payload, self.queues_dir)
        self.assertEqual(result.reason, "malformed: request_id does not match filename")

    def test_schema_check_outranks_every_field_level_check(self):
        # A v2 body whose `action` is also nonsense rejects for the SCHEMA, not the action: the
        # version is what says the other fields mean what this validator thinks they mean.
        payload = json.dumps({"schema": "control-request-v2", "request_id": REQ_ARM,
                              "action": "detonate", "requested_at": ""}).encode()
        result = cd.validate_request_body(REQ_ARM, payload, self.queues_dir)
        self.assertEqual(result.reason, cd.REQUEST_SCHEMA_REJECTION)

    def test_rejection_uses_the_ordinary_disposition_path_never_a_crash(self):
        # Mechanism pin: a schema rejection is a normal fail-closed disposition, written through
        # the same `write_disposition` call as every other `malformed: ...` -- same file, same
        # keys, same write-once semantics.
        paths = _stage(self, CONTRACT / "control")
        _run_pass(paths)
        for request_id in (REQ_SCHEMA_ABSENT, REQ_SCHEMA_UNKNOWN, REQ_SCHEMA_WRONG_TYPE):
            with self.subTest(request_id=request_id):
                disp = json.loads((paths.dispositions_dir / f"{request_id}.json").read_text())
                self.assertEqual(disp["state"], "rejected")
                self.assertEqual(disp["reason"], cd.REQUEST_SCHEMA_REJECTION)
                self.assertEqual(sorted(disp), ["decided_at", "reason", "request_id", "state"])
                # Rejected, not claimed and not acted on.
                self.assertFalse((paths.claims_dir / f"{request_id}.json").exists())
                self.assertFalse((paths.actions_dir / f"{request_id}.json").exists())

    def test_python_and_typescript_name_the_same_schema_string(self):
        # The TypeScript producer's copy of this constant lives in
        # `dashboard/packages/shared/src/control.ts` as `CONTROL_REQUEST_SCHEMA_V1`; this asserts
        # the two spellings have not drifted without reimplementing a TS parser here.
        shared = (REPO_ROOT / "dashboard/packages/shared/src/control.ts").read_text(encoding="utf-8")
        self.assertIn(f'export const CONTROL_REQUEST_SCHEMA_V1 = "{cd.SCHEMA_REQUEST}"', shared)


class TestStatusFileIsByteIdenticalToTheFixture(unittest.TestCase):
    """`status.json` is the one file Python WRITES and TypeScript READS. The committed fixture is
    the exact output of one pass over the committed request/live/result tree, so any change to
    `compose_status`'s key set, ordering, indentation, or null-honesty breaks here first."""

    def test_dispatch_pass_reproduces_the_committed_status_json_byte_for_byte(self):
        paths = _stage(self, CONTRACT / "control")
        _run_pass(paths)
        self.assertEqual(
            paths.status_path.read_bytes(),
            (CONTRACT / "control/host/status.json").read_bytes(),
        )

    def test_the_pass_is_idempotent_a_second_pass_changes_nothing(self):
        paths = _stage(self, CONTRACT / "control")
        _run_pass(paths)
        first = {p.name: p.read_bytes() for p in sorted(paths.dispositions_dir.glob("*.json"))}
        _run_pass(paths)
        second = {p.name: p.read_bytes() for p in sorted(paths.dispositions_dir.glob("*.json"))}
        self.assertEqual(first, second)
        self.assertEqual(
            paths.status_path.read_bytes(),
            (CONTRACT / "control/host/status.json").read_bytes(),
        )

    def test_the_projected_lifecycle_states_are_the_pinned_ones(self):
        paths = _stage(self, CONTRACT / "control")
        status = _run_pass(paths)
        self.assertEqual(
            [(r["request_id"], r["state"], r["reason"]) for r in status["requests"]],
            [
                (REQ_DISARM, "done", None),
                (REQ_ARM, "done", None),
                (REQ_START, "claimed", None),
                (REQ_STOP, "done", "stop-delivered"),
                (REQ_CANCEL, "rejected", "already-claimed"),
                (REQ_SCHEMA_ABSENT, "rejected", cd.REQUEST_SCHEMA_REJECTION),
                (REQ_SCHEMA_UNKNOWN, "rejected", cd.REQUEST_SCHEMA_REJECTION),
                (REQ_SCHEMA_WRONG_TYPE, "rejected", cd.REQUEST_SCHEMA_REJECTION),
            ],
        )
        for request in status["requests"]:
            self.assertIn(request["state"], cd.WIRE_STATES)


class TestCrashPointReplay(unittest.TestCase):
    """The action -> effect -> disposition ordering exists so a crash between any two steps is
    recoverable. These two fixture trees are frozen snapshots of exactly those two gaps."""

    def test_crashed_after_the_action_before_the_stop_marker(self):
        paths = _stage(self, FIXTURES / "crash-stop-after-action-before-effect/control")
        marker = paths.stop_marker("inv-crash-0001")
        self.assertFalse(marker.exists())  # the effect never landed pre-crash
        self.assertFalse((paths.dispositions_dir / f"{CRASH_STOP_ID}.json").exists())
        recorded_before = json.loads((paths.actions_dir / f"{CRASH_STOP_ID}.json").read_text())

        _run_pass(paths)

        # Replay resumes from the RECORDED target, never from a fresh read of live state.
        self.assertTrue(marker.exists())
        disp = json.loads((paths.dispositions_dir / f"{CRASH_STOP_ID}.json").read_text())
        self.assertEqual((disp["state"], disp["reason"]), ("done", "stop-delivered"))
        # The action file is write-once -- a replay never re-derives or restamps it.
        self.assertEqual(json.loads((paths.actions_dir / f"{CRASH_STOP_ID}.json").read_text()),
                         recorded_before)

    def test_a_dead_target_group_is_not_re_marked_on_replay(self):
        # Same tree, but the recorded target's group is gone: replay must NOT recreate a marker
        # that could retarget a later run, and must still terminalize the stop request.
        paths = _stage(self, FIXTURES / "crash-stop-after-action-before-effect/control")
        _run_pass(paths, group_alive=False)
        self.assertFalse(paths.stop_marker("inv-crash-0001").exists())
        disp = json.loads((paths.dispositions_dir / f"{CRASH_STOP_ID}.json").read_text())
        self.assertEqual((disp["state"], disp["reason"]), ("done", "stop-delivered"))

    def test_crashed_after_the_effect_before_the_disposition(self):
        paths = _stage(self, FIXTURES / "crash-cancel-after-effect-before-disposition/control")
        target_disp_path = paths.dispositions_dir / f"{CRASH_START_ID}.json"
        target_before = json.loads(target_disp_path.read_text())
        self.assertEqual(target_before["state"], "canceled")   # the effect landed pre-crash
        self.assertFalse((paths.dispositions_dir / f"{CRASH_CANCEL_ID}.json").exists())

        _run_pass(paths)

        # The cancel finally gets its own disposition...
        disp = json.loads((paths.dispositions_dir / f"{CRASH_CANCEL_ID}.json").read_text())
        self.assertEqual((disp["state"], disp["reason"]), ("done", None))
        # ...and the already-landed effect is NOT rewritten (write-once: same bytes, same
        # `decided_at` from before the crash).
        self.assertEqual(json.loads(target_disp_path.read_text()), target_before)
        # The canceled start was never claimed or launched by the replaying pass.
        self.assertFalse((paths.claims_dir / f"{CRASH_START_ID}.json").exists())


class TestQueueAndQueueStateContractFixtures(unittest.TestCase):
    """The queue/queue-state pair the dashboard's `buildQueueListResponse` joins by alias. Python
    is the writer of both files in production (`queue_file`/`queue_state`), so it validates them
    here with the real loaders -- the TypeScript reader parses the same two files."""

    def setUp(self):
        self.queue_path = CONTRACT / "queues/contract-queue.json"
        self.state_path = CONTRACT / "queues/contract-queue.state.json"

    def test_the_queue_fixture_passes_the_real_queue_validator(self):
        queue = queue_file.load_queue(self.queue_path)  # raises QueueValidationError on drift
        self.assertEqual([c["alias"] for c in queue["cells"]],
                         ["contract-a", "contract-b", "contract-c"])

    def test_the_state_fixture_is_keyed_by_this_queue_s_own_resolved_cell_ids(self):
        resolved = queue_file.resolve_queue_cells(queue_file.load_queue(self.queue_path))
        state = queue_state.load_state(self.state_path)
        self.assertEqual(sorted(state["cells"]), sorted(c.cell_id for c in resolved))
        for cell in resolved:
            self.assertEqual(state["cells"][cell.cell_id]["alias"], cell.identity.alias)

    def test_the_state_fixture_only_uses_vocabulary_the_dashboard_can_render(self):
        # The TypeScript reader drops any cell/queue state outside its Zod enums, so a fixture
        # carrying an unrenderable value would silently join as `null` on the wire.
        state = queue_state.load_state(self.state_path)
        self.assertEqual(state["queue_state"], "paused-resumable")
        self.assertEqual(
            [state["cells"][k]["state"] for k in sorted(state["cells"])],
            ["completed", "failed", "pending"],
        )


class TestFixturesAreNeverTheLiveStateDirectory(unittest.TestCase):
    def test_every_fixture_path_used_here_is_inside_the_repo(self):
        # Guard rail, not ceremony: a minutely cron dispatcher runs against
        # `~/.local/state/inference-bakeoff/`, and nothing in this suite may ever address it.
        live = Path.home() / ".local/state/inference-bakeoff"
        for path in (FIXTURES, CONTRACT, CONTRACT / "queues"):
            self.assertTrue(path.is_relative_to(REPO_ROOT))
            self.assertFalse(path.is_relative_to(live))


if __name__ == "__main__":
    unittest.main()
