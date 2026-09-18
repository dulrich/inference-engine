"""Host serving-control dispatcher (plans/bench-dashboard-serving-control.md WP-5).

No generator ever runs in this suite and no container/GPU/HTTP is touched: `run_picker`/`run_sync`
are always recording fakes, so `execute_pass` never falls through to its real `subprocess`
defaults. Every test runs inside a `tempfile.TemporaryDirectory()` with its own COPY of
`router/visibility.json` -- the real declaration is only ever READ (by the contract tests, which
must agree with the committed dashboard fixtures), never written, and the real
`~/.local/state/inference-bakeoff/`, the real crontab and the real `~/.config/inference-serving/`
are never touched at all (the wrapper tests point `HOME` and `INFERENCE_SERVING_ENV` at a tempdir).

The contract cases read the SAME BYTES the TypeScript side parses
(`dashboard/apps/api/fixtures/serving/contract-v1/`, validated there by
`dashboard/packages/shared/src/servingRequest.ts`'s Zod schemas) rather than a copy, so the two
validators cannot drift apart silently.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BAKEOFF_DIR.parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import serving_dispatcher as sd  # noqa: E402
import serving_protocol as sp  # noqa: E402
from control_store import flock_nonblocking_acquire, flock_release  # noqa: E402

FIXTURES = REPO_ROOT / "dashboard" / "apps" / "api" / "fixtures" / "serving" / "contract-v1"
DECLARATION_PATH = REPO_ROOT / "router" / "visibility.json"
# A FROZEN copy of the declaration, committed beside the dashboard fixtures. The status fixture is
# pinned to THIS, never to the live `router/visibility.json`, so a live visibility toggle is a
# one-file commit instead of a fixture refresh. It is a sample, never re-synced by a toggle.
SAMPLE_DECLARATION_PATH = FIXTURES / "router" / "visibility.sample.json"
WRAPPER = BAKEOFF_DIR / "serving-dispatch.sh"

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-09-13T12:00:00Z"

# Two keys of the real declaration, chosen for their `gateway_id`: one non-null (so `cc` is
# legal), one null (so `cc` is the plan's enforcing-gate rejection).
GATEWAY_KEY = "Qwen3.8-27B-UD-IQ3_S"
NULL_GATEWAY_KEY = "Ling-3.0-tiny-Q8_0"
OTHER_GATEWAY_KEY = "Qwen3.6-35B-A3B-UD-Q4_K_XL"

RID_A = "srv-20260913T115900Z-00000000000000a1"
RID_B = "srv-20260913T115930Z-00000000000000a2"
RID_C = "srv-20260913T115945Z-00000000000000a3"


class _Recorder:
    """An injected generator seam: records its argv, returns a fixed exit code."""

    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list = []

    def __call__(self, argv) -> int:
        self.calls.append(list(argv))
        return self.code


class _WriteCounter:
    def __init__(self) -> None:
        self.count = 0


@contextlib.contextmanager
def _count_writes(path):
    """Counts atomic writes of ONE path -- the declaration -- while delegating to the real writer,
    so "one transaction" is asserted as one `os.replace` of `visibility.json` rather than inferred
    from the file's final contents (N sequential writes leave the same bytes as one)."""
    counter = _WriteCounter()
    real = sd.atomic_write_json

    def _counting(target, payload):
        if Path(target) == Path(path):
            counter.count += 1
        return real(target, payload)

    sd.atomic_write_json = _counting
    try:
        yield counter
    finally:
        sd.atomic_write_json = real


def _declaration() -> dict:
    return json.loads(DECLARATION_PATH.read_text(encoding="utf-8"))


def _sample_declaration() -> dict:
    return json.loads(SAMPLE_DECLARATION_PATH.read_text(encoding="utf-8"))


def _fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


def _setup(tmpname: str):
    tmp = Path(tmpname)
    paths = sd.Paths(tmp / "state")
    paths.requests_dir.mkdir(parents=True, exist_ok=True)
    paths.host_dir.mkdir(parents=True, exist_ok=True)
    visibility = tmp / "visibility.json"
    visibility.write_text(DECLARATION_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return paths, visibility


def _write_request(paths, request_id, model_key, consumers, requested_at, **overrides):
    body = {
        "schema": sp.SCHEMA_REQUEST,
        "request_id": request_id,
        "action": "set-visibility",
        "model_key": model_key,
        "consumers": consumers,
        "requested_at": requested_at,
    }
    body.update(overrides)
    (paths.requests_dir / f"{request_id}.json").write_text(json.dumps(body), encoding="utf-8")
    return body


def _run(paths, visibility, picker=None, sync=None, now=NOW, unlink_fn=None, stderr=None):
    """One pass with both generator seams faked. `stderr` captures the pass's own diagnostics
    so a test that expects one asserts on it instead of leaking it into the suite's output."""
    picker = picker if picker is not None else _Recorder()
    sync = sync if sync is not None else _Recorder()
    with contextlib.redirect_stderr(stderr if stderr is not None else io.StringIO()):
        code, counts = sd.execute_pass(
            paths, visibility, lambda: now, picker, sync,
            repo_root=REPO_ROOT, unlink_fn=unlink_fn,
        )
    return code, counts, picker, sync


def _status(paths) -> dict:
    return json.loads(sd.serving_status_path(paths).read_text(encoding="utf-8"))


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths, self.visibility = _setup(self.tmp.name)

    def _declared(self) -> dict:
        return json.loads(self.visibility.read_text(encoding="utf-8"))


class TestCrossLanguageContract(unittest.TestCase):
    """The committed fixture bytes, parsed by this plane's Python validator exactly as the Zod
    schemas parse them on the TypeScript side."""

    def test_both_valid_request_fixtures_validate_against_the_real_declaration(self):
        declaration = _declaration()
        for request_id, model_key, consumers in (
            (RID_A, "Qwen3.8-27B-UD-IQ3_S", ("cc", "openwebui")),
            (RID_B, "Qwen3.8-27B-UD-IQ3_S-reasoning-off", ()),
        ):
            with self.subTest(request_id=request_id):
                data = _fixture_bytes("control", "requests", f"{request_id}.json")
                validation = sp.validate_request(request_id, data, declaration)
                self.assertIsNone(validation.reason)
                self.assertIsNotNone(validation.request)
                self.assertEqual(validation.request.model_key, model_key)
                self.assertEqual(validation.request.consumers, consumers)
                self.assertEqual(validation.request.action, "set-visibility")

    def test_every_invalid_request_fixture_is_rejected_with_its_own_reason(self):
        declaration = _declaration()
        cases = {
            "request-consumer-unknown.json": "malformed: unknown consumer",
            "request-id-grammar.json": "malformed: invalid request id",
            "request-model-key-empty.json": "malformed: invalid model_key",
            "request-schema-unknown.json": f"malformed: schema is not {sp.SCHEMA_REQUEST}",
        }
        for name, expected in cases.items():
            with self.subTest(fixture=name):
                data = _fixture_bytes("invalid", name)
                # The drop-box basename is always `<request_id>.json`, so the body's own id is
                # what the file WOULD have been called -- including the `req-`-prefixed one,
                # which is exactly what the id-grammar case is about.
                basename_id = json.loads(data)["request_id"]
                validation = sp.validate_request(basename_id, data, declaration)
                self.assertIsNone(validation.request)
                self.assertEqual(validation.reason, expected)

    def test_the_status_fixture_validates_and_its_unknown_schema_variant_does_not(self):
        status = json.loads(_fixture_bytes("control", "host", "serving-status.json"))
        self.assertIsNone(sp.validate_status_file(status))
        bad = json.loads(_fixture_bytes("invalid", "status-schema-unknown.json"))
        self.assertEqual(sp.validate_status_file(bad), f"status schema is not {sp.SCHEMA_STATUS}")

    def test_the_status_fixture_carries_the_declarations_own_matrix(self):
        """Drift alarm, pinned to the FROZEN SAMPLE (`router/visibility.sample.json`) rather than
        to the live `router/visibility.json`: the fixture claims to be a declaration's map
        verbatim, and this plane re-publishes that map as the chips' only source of truth. The two
        files move together as one committed pair; a live visibility toggle does not touch
        either."""
        status = json.loads(_fixture_bytes("control", "host", "serving-status.json"))
        models = _sample_declaration()["models"]
        self.assertEqual(list(status["visibility"]), list(models))
        self.assertEqual(status["visibility"], models)

    def test_compose_status_publishes_the_sample_declarations_map_verbatim(self):
        """The publisher still round-trips: the sample is a valid `serving-visibility-v1` document,
        and composing a status from it reproduces the fixture's `visibility` block exactly."""
        sample = _sample_declaration()
        self.assertIsNotNone(sp.declared_models(sample))
        status = json.loads(_fixture_bytes("control", "host", "serving-status.json"))
        self.assertEqual(sp.compose_status(sample, [], NOW)["visibility"], status["visibility"])

    def test_compose_status_reproduces_the_fixtures_shape(self):
        body = sp.compose_status(_declaration(), [], NOW)
        self.assertEqual(body["schema"], sp.SCHEMA_STATUS)
        self.assertEqual(body["generated_at"], NOW_ISO)
        self.assertIsNone(body["last_request"])
        self.assertEqual(body["recent"], [])
        self.assertIsNone(sp.validate_status_file(body))


class TestValidationRules(unittest.TestCase):
    def test_cc_on_a_null_gateway_key_is_rejected_with_the_exact_reason(self):
        declaration = _declaration()
        body = json.dumps({
            "schema": sp.SCHEMA_REQUEST, "request_id": RID_A, "action": "set-visibility",
            "model_key": NULL_GATEWAY_KEY, "consumers": ["cc"],
            "requested_at": "2026-09-13T11:59:00.480Z",
        }).encode("utf-8")
        validation = sp.validate_request(RID_A, body, declaration)
        self.assertIsNone(validation.request)
        self.assertEqual(validation.reason, "cc requires a gateway id")
        self.assertEqual(validation.reason, sp.CC_REQUIRES_GATEWAY_ID)

    def test_an_unparseable_requested_at_is_a_protocol_error(self):
        declaration = _declaration()
        body = json.dumps({
            "schema": sp.SCHEMA_REQUEST, "request_id": RID_A, "action": "set-visibility",
            "model_key": GATEWAY_KEY, "consumers": [], "requested_at": "yesterday",
        }).encode("utf-8")
        self.assertEqual(
            sp.validate_request(RID_A, body, declaration).reason,
            "malformed: invalid requested_at",
        )

    def test_an_undeclared_model_key_is_rejected(self):
        declaration = _declaration()
        body = json.dumps({
            "schema": sp.SCHEMA_REQUEST, "request_id": RID_A, "action": "set-visibility",
            "model_key": "no-such-model", "consumers": [],
            "requested_at": "2026-09-13T11:59:00.480Z",
        }).encode("utf-8")
        self.assertEqual(
            sp.validate_request(RID_A, body, declaration).reason,
            "malformed: undeclared model_key",
        )

    def test_canonical_consumers_orders_and_deduplicates(self):
        self.assertEqual(sp.canonical_consumers(["openwebui", "cc", "cc"]), ["cc", "openwebui"])
        self.assertEqual(sp.canonical_consumers([]), [])

    def test_a_declaration_with_the_wrong_schema_is_not_partially_trusted(self):
        declaration = _declaration()
        declaration["schema"] = "serving-visibility-v2"
        self.assertIsNone(sp.declared_models(declaration))


class TestIngestOrdering(_TmpCase):
    def test_pending_is_sorted_by_requested_at_then_request_id(self):
        """Same-second ids: `request_id` alone does NOT order them, because the id's stamp has
        whole-second resolution and its 16 hex characters are random. The tuple does."""
        early_at_late_id = "srv-20260913T115900Z-ffffffffffffffff"
        late_at_early_id = "srv-20260913T115900Z-0000000000000000"
        _write_request(self.paths, early_at_late_id, GATEWAY_KEY, [], "2026-09-13T11:59:00.100Z")
        _write_request(self.paths, late_at_early_id, GATEWAY_KEY, [], "2026-09-13T11:59:00.900Z")
        pending, rejections, ignored = sd._ingest(self.paths, self._declared())
        self.assertEqual([r.request_id for r in pending], [early_at_late_id, late_at_early_id])
        self.assertEqual((rejections, ignored), ([], 0))

    def test_a_non_matching_basename_is_ignored_and_counted_never_dispositioned(self):
        (self.paths.requests_dir / "req-20260913T115900Z-00000000000000a1.json").write_text("{}")
        (self.paths.requests_dir / f".{RID_A}.deadbeef.tmp").write_text("{}")
        (self.paths.requests_dir / "srv-nonsense.json").write_text("{}")
        _, counts, _, _ = _run(self.paths, self.visibility)
        self.assertEqual(counts["ignored"], 3)
        self.assertEqual((counts["applied"], counts["rejected"], counts["failed"]), (0, 0, 0))
        self.assertEqual(_status(self.paths)["recent"], [])
        # The other plane's file is never read, dispositioned, or removed by this dispatcher.
        self.assertTrue(
            (self.paths.requests_dir / "req-20260913T115900Z-00000000000000a1.json").exists(),
        )


class TestApply(_TmpCase):
    def test_the_happy_path_rewrites_only_the_target_keys_consumers(self):
        before = self._declared()
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["openwebui", "cc", "cc"],
                       "2026-09-13T11:59:00.480Z")
        code, counts, picker, sync = _run(self.paths, self.visibility)
        after = self._declared()
        self.assertEqual(code, 0)
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(after["models"][GATEWAY_KEY]["consumers"], ["cc", "openwebui"])
        # `gateway_id` untouched, key order preserved, every OTHER key structurally identical.
        self.assertEqual(
            after["models"][GATEWAY_KEY]["gateway_id"],
            before["models"][GATEWAY_KEY]["gateway_id"],
        )
        self.assertEqual(list(after["models"]), list(before["models"]))
        self.assertEqual(after["schema"], before["schema"])
        for key in before["models"]:
            if key != GATEWAY_KEY:
                self.assertEqual(after["models"][key], before["models"][key], key)
        self.assertEqual(len(picker.calls), 1)
        self.assertEqual(len(sync.calls), 1)
        record = _status(self.paths)["recent"][0]
        self.assertEqual(record, {
            "request_id": RID_A, "state": "applied", "reason": None, "applied_at": NOW_ISO,
        })

    def test_the_empty_consumers_request_is_the_unpublish_shape(self):
        _write_request(self.paths, RID_A, OTHER_GATEWAY_KEY, [], "2026-09-13T11:59:00.480Z")
        _, counts, _, _ = _run(self.paths, self.visibility)
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(self._declared()["models"][OTHER_GATEWAY_KEY]["consumers"], [])

    def test_cc_on_a_null_gateway_key_leaves_the_declaration_untouched(self):
        before = self.visibility.read_bytes()
        _write_request(self.paths, RID_A, NULL_GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _, counts, picker, sync = _run(self.paths, self.visibility)
        self.assertEqual(counts["rejected"], 1)
        self.assertEqual(self.visibility.read_bytes(), before)
        # A rejection-only pass is still a VALID pass, so it still reconciles exactly once --
        # from a declaration the rejection did not touch.
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))
        self.assertEqual(_status(self.paths)["recent"][0], {
            "request_id": RID_A, "state": "rejected",
            "reason": "cc requires a gateway id", "applied_at": NOW_ISO,
        })
        self.assertFalse((self.paths.requests_dir / f"{RID_A}.json").exists())

    def test_the_latest_request_per_model_wins_and_supersedes_the_earlier_one(self):
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _write_request(self.paths, RID_B, GATEWAY_KEY, ["openwebui"], "2026-09-13T11:59:30.117Z")
        _, counts, picker, sync = _run(self.paths, self.visibility)
        self.assertEqual(counts["applied"], 2)
        self.assertEqual(self._declared()["models"][GATEWAY_KEY]["consumers"], ["openwebui"])
        # Only the winner is applied for real: one picker run, one sync run.
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))
        recent = _status(self.paths)["recent"]
        self.assertEqual([r["request_id"] for r in recent], [RID_B, RID_A])
        self.assertTrue(all(r["state"] == "applied" for r in recent))
        for request_id in (RID_A, RID_B):
            self.assertFalse((self.paths.requests_dir / f"{request_id}.json").exists())

    def test_two_models_in_one_pass_share_one_declaration_write_and_one_projector_pair(self):
        """Two DISTINCT model keys in one pass are one transaction, not two: both changes land in
        a single atomic write of the declaration, and the pair that projects it runs once. Each
        generator already reconciles the whole declaration, so a second run would be redundant
        serial whole-world work rather than extra safety."""
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _write_request(self.paths, RID_B, OTHER_GATEWAY_KEY, [], "2026-09-13T11:59:30.117Z")
        with _count_writes(self.visibility) as writes:
            code, counts, picker, sync = _run(self.paths, self.visibility)
        self.assertEqual((code, counts["applied"]), (0, 2))
        models = self._declared()["models"]
        self.assertEqual(models[GATEWAY_KEY]["consumers"], ["cc"])
        self.assertEqual(models[OTHER_GATEWAY_KEY]["consumers"], [])
        self.assertEqual(writes.count, 1)
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))
        recent = _status(self.paths)["recent"]
        self.assertEqual([r["request_id"] for r in recent], [RID_B, RID_A])
        self.assertTrue(all(r["state"] == "applied" for r in recent))

    def test_a_batch_whose_entries_are_already_current_writes_nothing_and_still_reconciles(self):
        """The declaration write is the cheap half; the projection is the reconciliation loop. An
        already-current request rewrites no bytes and still gets its one picker/sync pair."""
        current = self._declared()["models"][GATEWAY_KEY]["consumers"]
        before = self.visibility.read_bytes()
        _write_request(self.paths, RID_A, GATEWAY_KEY, current, "2026-09-13T11:59:00.480Z")
        with _count_writes(self.visibility) as writes:
            _, counts, picker, sync = _run(self.paths, self.visibility)
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(writes.count, 0)
        self.assertEqual(self.visibility.read_bytes(), before)
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))

    def test_one_projection_failure_dispositions_every_request_the_batch_represents(self):
        """There is ONE projection result per pass, so there is one outcome to hand out: both
        winners AND the request one of them superseded are recorded `failed` with the same reason.
        No per-model side effect is invented to give them distinct ones."""
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _write_request(self.paths, RID_B, OTHER_GATEWAY_KEY, [], "2026-09-13T11:59:30.117Z")
        _write_request(self.paths, RID_C, GATEWAY_KEY, [], "2026-09-13T11:59:45.900Z")
        stderr = io.StringIO()
        code, counts, picker, sync = _run(
            self.paths, self.visibility, sync=_Recorder(2), stderr=stderr,
        )
        self.assertEqual(code, sd.EXIT_RECONCILIATION_FAILED)
        self.assertEqual((counts["failed"], counts["applied"]), (3, 0))
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))
        recent = _status(self.paths)["recent"]
        self.assertEqual([r["request_id"] for r in recent], [RID_C, RID_B, RID_A])
        for record in recent:
            self.assertEqual(record["state"], "failed")
            self.assertEqual(record["reason"], "sync-openwebui-capabilities.py exited 2")
        # Every edit is KEPT -- including the superseded key's winning value -- and every request
        # file is terminal, which is exactly why the self-heal must not need a new request.
        models = self._declared()["models"]
        self.assertEqual(models[GATEWAY_KEY]["consumers"], [])
        self.assertEqual(models[OTHER_GATEWAY_KEY]["consumers"], [])
        for request_id in (RID_A, RID_B, RID_C):
            self.assertFalse((self.paths.requests_dir / f"{request_id}.json").exists())
        self.assertIn("the declaration edit is KEPT", stderr.getvalue())

    def test_a_picker_failure_names_the_step_keeps_the_edit_and_skips_the_sync(self):
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _, counts, picker, sync = _run(self.paths, self.visibility, picker=_Recorder(1))
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(self._declared()["models"][GATEWAY_KEY]["consumers"], ["cc"])
        self.assertEqual(sync.calls, [])
        self.assertEqual(_status(self.paths)["recent"][0], {
            "request_id": RID_A, "state": "failed",
            "reason": "model-picker-lineup.mjs exited 1", "applied_at": NOW_ISO,
        })
        self.assertEqual(picker.calls, [[
            "node", str(REPO_ROOT / "router" / "model-picker-lineup.mjs"), "--write",
        ]])

    def test_a_sync_failure_names_the_step_and_keeps_the_edit(self):
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _, counts, picker, sync = _run(self.paths, self.visibility, sync=_Recorder(2))
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(self._declared()["models"][GATEWAY_KEY]["consumers"], ["cc"])
        self.assertEqual(_status(self.paths)["recent"][0]["reason"],
                         "sync-openwebui-capabilities.py exited 2")
        self.assertEqual(sync.calls, [[
            "python3", str(REPO_ROOT / "scripts" / "sync-openwebui-capabilities.py"), "--apply",
        ]])
        self.assertEqual(len(picker.calls), 1)

    def test_a_failed_pass_self_heals_on_the_next_one_without_a_new_request(self):
        """The generators are the reconciliation loop; the request is only the trigger -- so the
        NEXT pass re-runs them from the kept declaration edit with NO new request at all. This is
        the module contract's own promise, and it is why this plane needs no retry queue: the
        second pass below is EMPTY, and it must still reconcile exactly once."""
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        first_code, counts, _, _ = _run(self.paths, self.visibility, sync=_Recorder(2))
        self.assertEqual(counts["failed"], 1)
        # The edit is KEPT and the request is terminal: nothing durable remains to retry from.
        self.assertEqual(self._declared()["models"][GATEWAY_KEY]["consumers"], ["cc"])
        self.assertFalse((self.paths.requests_dir / f"{RID_A}.json").exists())

        # No second request is written. The empty pass is the self-heal.
        code, counts, picker, sync = _run(
            self.paths, self.visibility, now=NOW + timedelta(minutes=1),
        )
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))
        self.assertEqual(
            counts, {"applied": 0, "failed": 0, "ignored": 0, "pruned": 0, "rejected": 0},
        )
        self.assertEqual(self._declared()["models"][GATEWAY_KEY]["consumers"], ["cc"])
        self.assertEqual(first_code, sd.EXIT_RECONCILIATION_FAILED)
        self.assertEqual(code, 0)

    def test_an_unreadable_declaration_aborts_the_pass_and_publishes_nothing(self):
        self.visibility.write_text("{ not json", encoding="utf-8")
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        stderr = io.StringIO()
        code, counts, picker, sync = _run(self.paths, self.visibility, stderr=stderr)
        self.assertEqual(code, 1)
        self.assertIn("nothing applied, nothing published", stderr.getvalue())
        self.assertFalse(sd.serving_status_path(self.paths).exists())
        self.assertEqual((picker.calls, sync.calls), ([], []))
        self.assertEqual(counts["applied"], 0)
        # The request is left alone -- it is re-read once the declaration is readable again.
        self.assertTrue((self.paths.requests_dir / f"{RID_A}.json").exists())


class TestPublish(_TmpCase):
    def test_a_pass_with_no_requests_still_publishes_with_a_fresh_generated_at(self):
        code, _counts, picker, sync = _run(self.paths, self.visibility)
        self.assertEqual(code, 0)
        first = _status(self.paths)
        self.assertEqual(first["generated_at"], NOW_ISO)
        self.assertEqual(first["visibility"], _declaration()["models"])
        self.assertIsNone(first["last_request"])
        # And it reconciles: one picker run, one sync run, with no request at all.
        self.assertEqual((len(picker.calls), len(sync.calls)), (1, 1))
        _run(self.paths, self.visibility, now=NOW + timedelta(minutes=1))
        self.assertEqual(_status(self.paths)["generated_at"], "2026-09-13T12:01:00Z")

    def test_recent_is_bounded_at_twenty_newest_first_with_last_request_at_its_head(self):
        seeded = [
            sp.make_record(f"srv-20260913T1150{i:02d}Z-{i:016x}", "applied", None, NOW)
            for i in range(20)
        ]
        sd.atomic_write_json(
            sd.serving_status_path(self.paths),
            sp.compose_status(self._declared(), seeded, NOW),
        )
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _run(self.paths, self.visibility, now=NOW + timedelta(minutes=1))
        status = _status(self.paths)
        self.assertEqual(len(status["recent"]), sp.RECENT_LIMIT)
        self.assertEqual(status["recent"][0]["request_id"], RID_A)
        self.assertEqual(status["recent"][1]["request_id"], seeded[0]["request_id"])
        self.assertEqual(status["last_request"], status["recent"][0])
        self.assertIsNone(sp.validate_status_file(status))

    def test_an_unparseable_previous_status_is_treated_as_no_history(self):
        sd.serving_status_path(self.paths).write_text("{ torn", encoding="utf-8")
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        _run(self.paths, self.visibility)
        status = _status(self.paths)
        self.assertEqual([r["request_id"] for r in status["recent"]], [RID_A])

    def test_the_status_file_lands_in_the_host_dir_the_dashboard_mounts(self):
        _run(self.paths, self.visibility)
        self.assertEqual(
            sd.serving_status_path(self.paths),
            self.paths.state_dir / "control" / "host" / "serving-status.json",
        )
        self.assertTrue(sd.serving_status_path(self.paths).exists())


class TestTerminalization(_TmpCase):
    def test_the_disposition_is_recorded_before_the_unlink_and_replay_is_idempotent(self):
        def _refuses(path):
            raise OSError("unlink refused")

        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        stderr = io.StringIO()
        code, counts, _, _ = _run(self.paths, self.visibility, unlink_fn=_refuses, stderr=stderr)
        self.assertEqual((code, counts["applied"]), (0, 1))
        self.assertIn("its disposition is already recorded", stderr.getvalue())
        # Recorded, and still present: a crash between the two re-applies rather than vanishing.
        self.assertEqual(_status(self.paths)["recent"][0]["request_id"], RID_A)
        self.assertTrue((self.paths.requests_dir / f"{RID_A}.json").exists())

        _, counts, picker, _ = _run(self.paths, self.visibility, now=NOW + timedelta(minutes=1))
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(len(picker.calls), 1)
        self.assertFalse((self.paths.requests_dir / f"{RID_A}.json").exists())
        recent = _status(self.paths)["recent"]
        self.assertEqual([r["request_id"] for r in recent], [RID_A, RID_A])
        self.assertEqual(self._declared()["models"][GATEWAY_KEY]["consumers"], ["cc"])


class TestPrune(_TmpCase):
    def test_only_surviving_srv_files_older_than_thirty_days_are_pruned(self):
        old = self.paths.requests_dir / "srv-too-malformed-to-disposition.json"
        fresh = self.paths.requests_dir / "srv-also-malformed.json"
        other_plane = self.paths.requests_dir / "req-20260101T000000Z-00000000000000a1.json"
        for path in (old, fresh, other_plane):
            path.write_text("{}", encoding="utf-8")
        stale = (NOW - timedelta(days=40)).timestamp()
        os.utime(old, (stale, stale))
        os.utime(other_plane, (stale, stale))
        _, counts, _, _ = _run(self.paths, self.visibility)
        self.assertEqual(counts["pruned"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        # The bake-off plane owns `req-*` -- its own prune phase removes those, never this one.
        self.assertTrue(other_plane.exists())

    def test_the_prune_age_matches_the_bake_off_planes_shared_drop_box_rule(self):
        self.assertEqual(sp.PRUNE_AGE_SECONDS, 30 * 24 * 3600)


class TestCliAndLock(_TmpCase):
    def _argv(self):
        return [
            "dispatch", "--state-dir", str(self.paths.state_dir),
            "--visibility", str(self.visibility), "--repo-root", str(self.tmp.name),
        ]

    def test_a_held_lock_exits_zero_and_writes_nothing(self):
        fd = flock_nonblocking_acquire(sd.serving_lock_path(self.paths))
        self.assertIsNotNone(fd)
        self.addCleanup(flock_release, fd)
        _write_request(self.paths, RID_A, GATEWAY_KEY, ["cc"], "2026-09-13T11:59:00.480Z")
        before = self.visibility.read_bytes()
        self.assertEqual(sd.main(self._argv()), 0)
        self.assertFalse(sd.serving_status_path(self.paths).exists())
        self.assertEqual(self.visibility.read_bytes(), before)
        self.assertTrue((self.paths.requests_dir / f"{RID_A}.json").exists())

    def test_the_lock_lives_beside_the_bake_off_planes_own_locks(self):
        self.assertEqual(
            sd.serving_lock_path(self.paths), self.paths.state_dir / "serving-dispatch.lock",
        )
        self.assertNotEqual(sd.serving_lock_path(self.paths), self.paths.dispatch_lock)


class TestRepoRootResolution(unittest.TestCase):
    def test_the_repo_root_and_both_generator_argvs_come_from_file_not_cwd(self):
        self.assertEqual(sd.REPO_ROOT, REPO_ROOT)
        self.assertTrue((sd.REPO_ROOT / "router" / "visibility.json").exists())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(tmp.name)
        self.assertEqual(sd.default_visibility_path(), REPO_ROOT / "router" / "visibility.json")
        self.assertEqual(sd.picker_argv(sd.REPO_ROOT), [
            "node", str(REPO_ROOT / "router" / "model-picker-lineup.mjs"), "--write",
        ])
        self.assertEqual(sd.sync_argv(sd.REPO_ROOT), [
            "python3", str(REPO_ROOT / "scripts" / "sync-openwebui-capabilities.py"), "--apply",
        ])
        for argv in (sd.picker_argv(sd.REPO_ROOT), sd.sync_argv(sd.REPO_ROOT)):
            self.assertTrue(Path(argv[1]).is_absolute())
            self.assertTrue(Path(argv[1]).exists())

    def test_the_picker_argv_carries_no_launch_profile_flag(self):
        """Bare `--write` leaves an existing launch-profile env block in `settings.json` in place
        (it re-serialises the parsed object and only replaces `modelPicker.options`), and
        `--launch-profile` would need fleet-tools's gateway key, which this plane never has."""
        self.assertNotIn("--launch-profile", sd.picker_argv(sd.REPO_ROOT))


class TestWrapper(unittest.TestCase):
    """The wrapper's credential handling, exercised in a tempdir HOME -- the real
    `~/.config/inference-serving/env` is never read and the real state dir is never written.

    These are the only cases that reach the REAL `run_subprocess`, because `--repo-root` points at
    the tempdir: a pass now reconciles unconditionally, and the tempdir has no generators to run.
    That resolves to `EXIT_RECONCILIATION_FAILED` either way -- `node` reports a missing module,
    or `run_subprocess` catches the `OSError` of a `node` that is not on PATH at all and returns
    127 -- so these assert exit 3 deterministically, without depending on the host toolchain, and
    never the wrapper's own exit 2 (which would mean the credential file was refused). Nothing
    real is projected: the picker and the sync script do not exist under the tempdir root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths, self.visibility = _setup(self.tmp.name)
        self.env_file = Path(self.tmp.name) / "serving-env"

    def _run_wrapper(self, env_file_mode=None, body="OPENWEBUI_URL=http://127.0.0.1:4813\n"):
        env = dict(os.environ)
        env["HOME"] = self.tmp.name
        env.pop("BAKEOFF_STATE_DIR", None)
        env["INFERENCE_SERVING_ENV"] = str(self.env_file)
        if env_file_mode is not None:
            self.env_file.write_text(body, encoding="utf-8")
            self.env_file.chmod(env_file_mode)
        return subprocess.run(
            [
                str(WRAPPER), "--state-dir", str(self.paths.state_dir),
                "--visibility", str(self.visibility), "--repo-root", self.tmp.name,
            ],
            capture_output=True, text=True, env=env, check=False,
        )

    def test_a_group_readable_env_file_is_refused_before_anything_runs(self):
        result = self._run_wrapper(env_file_mode=0o644)
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be 0600", result.stderr)
        self.assertFalse(sd.serving_status_path(self.paths).exists())

    def test_a_0600_env_file_is_sourced_and_the_pass_runs_without_echoing_the_key(self):
        secret = "sk-never-print-me-0123456789"
        result = self._run_wrapper(
            env_file_mode=0o600,
            body=f"OPENWEBUI_URL=http://127.0.0.1:4813\nOPENWEBUI_API_KEY={secret}\n",
        )
        self.assertNotEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.returncode, sd.EXIT_RECONCILIATION_FAILED, result.stderr)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        # The pass ran to completion despite the absent generators: it published and summarised.
        self.assertEqual(json.loads(result.stdout.strip()), {
            "applied": 0, "failed": 0, "ignored": 0, "pruned": 0, "rejected": 0,
        })
        self.assertTrue(sd.serving_status_path(self.paths).exists())

    def test_an_absent_env_file_is_not_itself_an_error(self):
        """No env file is a legal install state (the plane is simply unconfigured), so the wrapper
        must NOT refuse with its own exit 2 -- it execs the dispatcher, which runs a full pass."""
        result = self._run_wrapper(env_file_mode=None)
        self.assertNotEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.returncode, sd.EXIT_RECONCILIATION_FAILED, result.stderr)
        self.assertTrue(sd.serving_status_path(self.paths).exists())


if __name__ == "__main__":
    unittest.main()
