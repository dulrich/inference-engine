"""Queue-driven batch runner (plan longcontext-pow2-overnight-campaign.md WP-1): run-loop
orchestration and CLI composition only. No live Docker/GPU execution anywhere in this suite --
`scratch_server_fn`/`container_controller`/`hang_check_fn`/`vram_*_bytes_fn`/`now_fn` are always
fakes; `run_queue` never falls through to its real (Docker/sysfs-touching) defaults in any test
here.

WP-R1: `resolve_queue_cells` now returns typed `cell_model.ResolvedCell` values instead of plain
dicts, so cell reads here are attribute reads (`cell.cell_id`, `cell.queue.sampling_arm`) rather
than the former dict/underscore-key indexing. The scratch-server seam still speaks plain dicts --
`fake_scratch_server_factory` below receives `cell.as_sweep_cell()`, exactly as `sweep.py` does.
The identity contract itself is locked separately, by `tests/test_cell_identity.py`.

WP-R3: this module used to also carry queue validation/resolution, state-machine, runner, and
record-serialization tests; those moved to `test_queue_file.py`, `test_queue_state.py`,
`test_batch_runner.py`, and `test_record_contract.py` respectively, alongside the production
concepts they exercise. Shared fakes moved to `batch_fixtures.py`. No test logic changed in the
split."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from batch_fixtures import (  # noqa: E402
    FakeContainerController,
    _fake_battery_fn_factory,
    _handle,
    _longcontext_cell,
    _minimal_queue,
    _write_queue,
    fake_scratch_server_factory,
    fixed_now_fn,
    scripted_cell_runner,
    sequenced_now_fn,
)

import batch  # noqa: E402
import queue_file  # noqa: E402
import run_harness  # noqa: E402
import sweep  # noqa: E402


class TestSizeProjection(unittest.TestCase):
    def test_projection_exceeds_a_representative_real_shaped_record(self):
        trials = 3
        projected = batch.project_cell_record_size_bytes(262144, trials)

        realistic_sample = {
            "type": "needle_probe", "schema_version": 2, "size": "near_ceiling",
            "target_tokens": 222222, "context_ceiling": 262144, "position_fraction": 0.45,
            "seed_used": 123456789, "haystack_seed": 123456789, "sampler_seed": 987654321,
            "sampling_arm": "production", "temperature": 0.7, "top_p": 0.9, "top_k": 40,
            "corpus_manifest_id": "gutenberg-longcontext-haystack-v2",
            "haystack_sha256": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2",
            "classifier_verdict": "correct", "prompt_tokens": 221987,
            "expected_code": "FW9-DELTA-3123", "correct": True, "success": True,
            "error": None, "finish_reason": "stop", "wall_seconds": 42.17,
            "timestamp": "2026-08-23T00:00:00+00:00",
        }
        realistic_completion = {
            "type": "needle_probe", "size": "near_ceiling", "expected_code": "FW9-DELTA-3123",
            "content_text": "FW9-DELTA-3123",
            "reasoning_text": "The exact reference code given above is FW9-DELTA-3123.",
            "finish_reason": "stop",
        }
        one_trial_bytes = (
            len(json.dumps(realistic_sample, ensure_ascii=False).encode("utf-8")) + 1
            + len(json.dumps(realistic_completion, ensure_ascii=False).encode("utf-8")) + 1
        )
        actual_bytes = one_trial_bytes * trials
        self.assertGreater(projected, actual_bytes)

    def test_scales_with_trials(self):
        self.assertGreater(
            batch.project_cell_record_size_bytes(524288, 10),
            batch.project_cell_record_size_bytes(524288, 1),
        )

    def test_a_context_below_the_threshold_is_never_size_checked(self):
        # The gate is `c >= LARGE_CONTEXT_SIZE_CHECK_THRESHOLD`; below it, no projection is
        # computed at all and no cell can be refused on size.
        cell = _longcontext_cell(context=batch.LARGE_CONTEXT_SIZE_CHECK_THRESHOLD - 1,
                                 trials=10_000)
        self.assertIsNone(batch._size_check_failure(cell))

    def test_a_large_context_cell_over_budget_is_refused_with_both_numbers(self):
        cell = _longcontext_cell(context=batch.LARGE_CONTEXT_SIZE_CHECK_THRESHOLD,
                                 trials=10_000)
        failure = batch._size_check_failure(cell)
        self.assertIsNotNone(failure)
        self.assertGreater(failure["projected_bytes"], run_harness.SIZE_LIMIT_BYTES)
        self.assertEqual(failure["limit_bytes"], run_harness.SIZE_LIMIT_BYTES)

    def test_a_large_context_cell_within_budget_is_admitted(self):
        cell = _longcontext_cell(context=batch.LARGE_CONTEXT_SIZE_CHECK_THRESHOLD, trials=3)
        self.assertIsNone(batch._size_check_failure(cell))

    def test_an_absent_trial_count_is_treated_as_one_not_a_crash(self):
        # `trials` is a required abbreviated-cell key, so a resolved queue cell always has one;
        # a hand-built cell that doesn't must still be projectable rather than raising inside
        # the run loop.
        cell = _longcontext_cell(context=batch.LARGE_CONTEXT_SIZE_CHECK_THRESHOLD, trials=None)
        self.assertIsNone(batch._size_check_failure(cell))

    def test_run_queue_refuses_an_over_budget_cell_without_spawning_it(self):
        # End-to-end: the refused cell is `failed`/`projected_size_exceeded` with its numbers in
        # `detail`, no scratch server is ever spawned for it, and the queue still completes.
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue(cells=[{
                "model": "model-a", "alias": "model-a-c256k-huge",
                "c": batch.LARGE_CONTEXT_SIZE_CHECK_THRESHOLD,
                "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
                "sampling_arm": "capability", "trials": 10_000,
            }])
            queue_path = _write_queue(tmp, queue)
            spawned = []
            result = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(spawned=spawned),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            self.assertEqual(spawned, [])  # never spawned -- refused before the server starts
            cell_state = next(iter(result["state"]["cells"].values()))
            self.assertEqual(cell_state["state"], batch.FAILED)
            self.assertEqual(cell_state["reason"], "projected_size_exceeded")
            self.assertGreater(
                cell_state["detail"]["projected_bytes"], run_harness.SIZE_LIMIT_BYTES,
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)

    def test_projection_is_comparable_against_run_harness_size_limit(self):
        # A single 262144+ cell's own trials are nowhere near the 25 MiB whole-experiment budget
        # at realistic trial counts -- the projection and the real limit are the same unit
        # (bytes) and the comparison in `run_queue` is exactly `projected > SIZE_LIMIT_BYTES`.
        projected = batch.project_cell_record_size_bytes(262144, trials=3)
        self.assertLess(projected, run_harness.SIZE_LIMIT_BYTES)
        self.assertIsInstance(projected, int)


class TestRunQueueEndToEnd(unittest.TestCase):
    def _run(self, queue, tmp, **kwargs):
        queue_path = _write_queue(tmp, queue)
        defaults = dict(
            scratch_server_fn=fake_scratch_server_factory(),
            now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
            vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
            vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
        )
        defaults.update(kwargs)
        return queue_path, batch.run_queue(queue_path, **defaults)

    def test_all_cells_complete_and_queue_reaches_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FakeContainerController()
            _, result = self._run(_minimal_queue(), tmp, container_controller=controller)
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            self.assertTrue(all(
                cs["state"] == batch.COMPLETED for cs in result["state"]["cells"].values()
            ))
            self.assertEqual(controller.calls, ["stop", "start"])

    def test_dry_run_never_touches_container_or_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FakeContainerController()
            queue_path, result = self._run(
                _minimal_queue(), tmp, dry_run=True, container_controller=controller,
            )
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["cell_count"], 4)
            self.assertEqual(controller.calls, [])
            self.assertFalse(batch.default_state_path(queue_path).exists())

    def test_deadline_already_passed_pauses_before_any_cell_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawned = []
            controller = FakeContainerController()
            _, result = self._run(
                _minimal_queue(deadline="00:30"), tmp,
                scratch_server_fn=fake_scratch_server_factory(spawned=spawned),
                now_fn=sequenced_now_fn(
                    datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc),  # past the rolled-forward deadline
                ),
                container_controller=controller,
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_PAUSED_RESUMABLE)
            self.assertEqual(spawned, [])
            self.assertTrue(all(
                cs["state"] == batch.PENDING for cs in result["state"]["cells"].values()
            ))
            # restart still attempted even though no cell ever ran
            self.assertEqual(controller.calls, ["stop", "start"])

    def test_resume_after_pause_picks_up_remaining_pending_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            # Force a pause on the first pass by using a deadline already past relative to now_fn.
            queue_paused = dict(queue, deadline="00:30")
            queue_path2 = _write_queue(tmp, queue_paused)
            first = batch.run_queue(
                queue_path2,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=sequenced_now_fn(
                    datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc),
                ),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                container_controller=FakeContainerController(),
            )
            self.assertEqual(first["queue_state"], batch.QUEUE_PAUSED_RESUMABLE)

            second = batch.run_queue(
                queue_path2,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                container_controller=FakeContainerController(),
            )
            self.assertEqual(second["queue_state"], batch.QUEUE_COMPLETED)
            self.assertTrue(all(
                cs["state"] == batch.COMPLETED for cs in second["state"]["cells"].values()
            ))

    def test_terminal_queue_no_ops_without_re_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path, first = self._run(_minimal_queue(), tmp)
            self.assertEqual(first["queue_state"], batch.QUEUE_COMPLETED)
            controller = FakeContainerController()
            second = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                container_controller=controller,
            )
            self.assertTrue(second["noop"])
            self.assertEqual(second["queue_state"], batch.QUEUE_COMPLETED)
            self.assertEqual(controller.calls, [])  # no-op never touches the container

    def test_re_arm_reruns_a_terminal_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path, first = self._run(_minimal_queue(), tmp)
            self.assertEqual(first["queue_state"], batch.QUEUE_COMPLETED)
            second = batch.run_queue(
                queue_path,
                re_arm=True,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                container_controller=FakeContainerController(),
            )
            self.assertFalse(second["noop"])
            self.assertEqual(second["queue_state"], batch.QUEUE_COMPLETED)

    def test_interrupted_running_cell_is_marked_failed_and_never_silently_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            queue_path = _write_queue(tmp, queue)
            resolved = batch.resolve_queue_cells(queue)
            crashed_id = resolved[0].cell_id
            state = batch.init_state(resolved)
            state["cells"][crashed_id]["state"] = batch.RUNNING
            batch.save_state(batch.default_state_path(queue_path), state)

            spawned = []
            result = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(spawned=spawned),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                container_controller=FakeContainerController(),
            )
            self.assertEqual(result["state"]["cells"][crashed_id]["state"], batch.FAILED)
            self.assertEqual(result["state"]["cells"][crashed_id]["reason"], "interrupted")
            self.assertNotIn(crashed_id, spawned)  # never silently re-run in the same invocation

    def test_hang_signature_halts_queue_terminally_and_still_restarts_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            controller = FakeContainerController()
            hang_line = "amdgpu: MES might be in unrecoverable state, issue a GPU reset"
            calls = {"n": 0}

            def hang_check_fn():
                calls["n"] += 1
                return hang_line if calls["n"] == 1 else None

            _queue_path, result = self._run(
                queue, tmp, container_controller=controller, hang_check_fn=hang_check_fn,
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_ABORTED_HANG)
            first_cell_id = batch.resolve_queue_cells(queue)[0].cell_id
            self.assertEqual(result["state"]["cells"][first_cell_id]["state"], batch.ABORTED_HANG)
            # every later cell stays untouched (pending) -- the queue halts, it doesn't skip-mark
            remaining = [
                cs["state"] for cid, cs in result["state"]["cells"].items() if cid != first_cell_id
            ]
            self.assertTrue(all(s == batch.PENDING for s in remaining))
            self.assertEqual(controller.calls, ["stop", "start"])  # restart attempted regardless

    def test_correctness_failure_on_quantized_cell_propagates_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            resolved = batch.resolve_queue_cells(queue)
            by_alias = {c.alias: c for c in resolved}
            failing_id = by_alias["model-a-c32k-q4"].cell_id
            later_id = by_alias["model-a-c65k-q4"].cell_id
            runner = scripted_cell_runner({
                failing_id: batch.CellOutcome("failed", "degenerate", True),
            })
            _, result = self._run(queue, tmp, cell_runner=runner)
            self.assertEqual(result["state"]["cells"][failing_id]["state"], batch.FAILED)
            self.assertEqual(result["state"]["cells"][later_id]["state"], batch.SKIPPED_PROPAGATED)
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)

    def test_non_correctness_failure_does_not_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            resolved = batch.resolve_queue_cells(queue)
            by_alias = {c.alias: c for c in resolved}
            failing_id = by_alias["model-a-c32k-q4"].cell_id
            later_id = by_alias["model-a-c65k-q4"].cell_id
            runner = scripted_cell_runner({
                failing_id: batch.CellOutcome("failed", "health_timeout", False),
            })
            _, result = self._run(queue, tmp, cell_runner=runner)
            self.assertEqual(result["state"]["cells"][failing_id]["state"], batch.FAILED)
            # a non-correctness failure never skip-propagates -- the later cell still runs
            # normally (and, per this test's scripted runner, succeeds) rather than being
            # marked skipped-propagated.
            self.assertNotEqual(result["state"]["cells"][later_id]["state"], batch.SKIPPED_PROPAGATED)
            self.assertEqual(result["state"]["cells"][later_id]["state"], batch.COMPLETED)

    def test_container_restarts_even_when_cell_runner_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FakeContainerController()

            def raising_runner(handle, cell):
                raise RuntimeError("boom")

            queue_path = _write_queue(tmp, _minimal_queue())
            with self.assertRaises(RuntimeError):
                batch.run_queue(
                    queue_path,
                    scratch_server_fn=fake_scratch_server_factory(),
                    cell_runner=raising_runner,
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                    container_controller=controller,
                )
            self.assertEqual(controller.calls, ["stop", "start"])

    def test_container_restart_failure_never_masks_original_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FakeContainerController(fail_start=True)

            def raising_runner(handle, cell):
                raise RuntimeError("boom")

            queue_path = _write_queue(tmp, _minimal_queue())
            with self.assertRaises(RuntimeError) as ctx:
                batch.run_queue(
                    queue_path,
                    scratch_server_fn=fake_scratch_server_factory(),
                    cell_runner=raising_runner,
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                    container_controller=controller,
                )
            self.assertEqual(str(ctx.exception), "boom")
            self.assertEqual(controller.calls, ["stop", "start"])  # start was still attempted
            # ...and the failure is a durable record beside the state file, never a silent pass
            # (plan restart-failure-surfacing WP-1).
            record_path = batch.restart_failure_path(batch.default_state_path(queue_path))
            self.assertTrue(record_path.exists(), record_path)
            record = json.loads(record_path.read_text())
            self.assertIn("error", record)
            self.assertIn("at", record)

    def test_container_restart_failure_is_recorded_and_returned_on_a_clean_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = FakeContainerController(fail_start=True)
            queue_path = _write_queue(tmp, _minimal_queue())
            result = batch.run_queue(
                    queue_path,
                    scratch_server_fn=fake_scratch_server_factory(),
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                    container_controller=controller,
                )
            # termination completed (the run itself is fine) but the failure is not swallowed
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            self.assertIsNotNone(result["restart_failure"])
            record_path = batch.restart_failure_path(batch.default_state_path(queue_path))
            self.assertTrue(record_path.exists())
            # a following run clears the stale record before it starts
            ok = FakeContainerController()
            result2 = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                container_controller=ok, re_arm=True,
            )
            self.assertIsNone(result2["restart_failure"])
            self.assertFalse(record_path.exists())


class TestBudgetArithmeticInRunQueue(unittest.TestCase):
    def test_headless_refusal_stops_before_container_is_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue(vram_profile="headless"))
            controller = FakeContainerController()
            with self.assertRaises(sweep.VRAMProfileRefused):
                batch.run_queue(
                    queue_path,
                    scratch_server_fn=fake_scratch_server_factory(),
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.3 * sweep.GIB),  # live-desktop baseline
                    container_controller=controller,
                )
            self.assertEqual(controller.calls, [])

    def test_headless_profile_succeeds_below_the_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue(vram_profile="headless"))
            controller = FakeContainerController()
            result = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(0.2 * sweep.GIB),
                container_controller=controller,
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            self.assertEqual(controller.calls, ["stop", "start"])

    def test_cli_vram_profile_flag_overrides_queue_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue(vram_profile="desktop-shared"))
            with self.assertRaises(sweep.VRAMProfileRefused):
                batch.run_queue(
                    queue_path,
                    vram_profile="headless",
                    scratch_server_fn=fake_scratch_server_factory(),
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.3 * sweep.GIB),
                    container_controller=FakeContainerController(),
                )


class TestControlWp2StopMarkerAndDeadline(unittest.TestCase):
    """plans/bakeoff-queue-control-plane.md WP-2 -- no live Docker/GPU here either; the marker
    is a plain tempdir file the fake `cell_runner` touches as a side effect, exactly mimicking
    what the (untested-here) wrapper would do from outside the process."""

    def _run(self, queue, tmp, **kwargs):
        queue_path = _write_queue(tmp, queue)
        defaults = dict(
            scratch_server_fn=fake_scratch_server_factory(),
            now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
            vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
            vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
        )
        defaults.update(kwargs)
        return queue_path, batch.run_queue(queue_path, **defaults)

    def test_marker_created_after_first_cell_pauses_at_next_boundary(self):
        # Marker appears (from the runner's side effect) once cell 1 of 4 finishes -- the check
        # is at the TOP of the loop, so it fires at the next boundary, before cell 2 starts.
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            resolved = batch.resolve_queue_cells(queue)
            first_id = resolved[0].cell_id
            marker_path = Path(tmp) / "stop.marker"

            def runner(handle, cell):
                if cell.cell_id == first_id:
                    marker_path.write_text("stop")
                return batch.CellOutcome("completed")

            _, result = self._run(queue, tmp, cell_runner=runner, stop_marker_path=marker_path)
            self.assertEqual(result["queue_state"], batch.QUEUE_PAUSED_RESUMABLE)
            self.assertEqual(result["state"]["paused_reason"], "stop-requested")
            self.assertFalse(marker_path.exists())  # consumed (unlinked)
            cells = result["state"]["cells"]
            self.assertEqual(cells[first_id]["state"], batch.COMPLETED)
            for cell in resolved[1:]:
                self.assertEqual(cells[cell.cell_id]["state"], batch.PENDING)

    def test_marker_created_during_last_cell_is_consumed_but_run_still_completes(self):
        # Marker appears during the FINAL cell -- by the time the loop would check it again,
        # there's no next cell; the marker is consumed in the completion `else` branch and the
        # queue still reaches `completed` (req 6: inert when the run finishes first).
        with tempfile.TemporaryDirectory() as tmp:
            queue = _minimal_queue()
            resolved = batch.resolve_queue_cells(queue)
            last_id = resolved[-1].cell_id
            marker_path = Path(tmp) / "stop.marker"

            def runner(handle, cell):
                if cell.cell_id == last_id:
                    marker_path.write_text("stop")
                return batch.CellOutcome("completed")

            _, result = self._run(queue, tmp, cell_runner=runner, stop_marker_path=marker_path)
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            self.assertFalse(marker_path.exists())  # consumed (unlinked)
            self.assertNotIn("paused_reason", result["state"])
            self.assertTrue(all(
                cs["state"] == batch.COMPLETED for cs in result["state"]["cells"].values()
            ))

    def test_marker_absent_at_configured_path_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker_path = Path(tmp) / "never-created.marker"
            _, result = self._run(_minimal_queue(), tmp, stop_marker_path=marker_path)
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            self.assertNotIn("paused_reason", result["state"])

    def test_stop_marker_path_none_behaves_identically_to_omitting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, result = self._run(_minimal_queue(), tmp, stop_marker_path=None)
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            self.assertTrue(all(
                cs["state"] == batch.COMPLETED for cs in result["state"]["cells"].values()
            ))

    def test_deadline_minutes_overrides_queue_deadline_and_wins(self):
        # The queue's own HH:MM deadline ("23:59") is nowhere near tripping; only the
        # --deadline-minutes override (30 minutes past a 01:00 start) can be responsible for the
        # pause the sequenced clock (jumping to 02:00 on the per-cell check) then trips.
        with tempfile.TemporaryDirectory() as tmp:
            spawned = []
            _, result = self._run(
                _minimal_queue(deadline="23:59"), tmp,
                scratch_server_fn=fake_scratch_server_factory(spawned=spawned),
                now_fn=sequenced_now_fn(
                    datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc),
                ),
                deadline_minutes=30,
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_PAUSED_RESUMABLE)
            self.assertEqual(result["state"]["paused_reason"], "deadline")
            self.assertEqual(spawned, [])

    def test_paused_reason_deadline_recorded_and_cleared_by_re_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, result = self._run(
                _minimal_queue(deadline="00:30"), tmp,
                now_fn=sequenced_now_fn(
                    datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 24, 1, 0, tzinfo=timezone.utc),
                ),
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_PAUSED_RESUMABLE)
            self.assertEqual(result["state"]["paused_reason"], "deadline")
            state = result["state"]
            batch.re_arm_state(state)
            self.assertNotIn("paused_reason", state)
            self.assertEqual(state["queue_state"], batch.QUEUE_RUNNING)


class TestControlWp2CliArgs(unittest.TestCase):
    def test_stop_marker_and_deadline_minutes_default_to_none(self):
        parser = batch.build_arg_parser()
        args = parser.parse_args(["run", "--queue", "q.json"])
        self.assertIsNone(args.stop_marker)
        self.assertIsNone(args.deadline_minutes)

    def test_deadline_minutes_parses_a_positive_int(self):
        parser = batch.build_arg_parser()
        args = parser.parse_args(["run", "--queue", "q.json", "--deadline-minutes", "45"])
        self.assertEqual(args.deadline_minutes, 45)

    def test_deadline_minutes_rejects_non_positive(self):
        parser = batch.build_arg_parser()
        for bad in ("0", "-5"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["run", "--queue", "q.json", "--deadline-minutes", bad])

    def test_cmd_run_forwards_stop_marker_and_deadline_minutes_to_run_queue(self):
        # WP-R1: `cmd_run` now calls the internal run core (`_run_resolved_queue`) directly with
        # its own already-loaded `ResolvedQueue`, rather than the public `run_queue` wrapper
        # (which would load/resolve the queue file a second time) -- so this spies on
        # `_run_resolved_queue`, the CLI's actual forwarding target, instead of `run_queue`.
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            marker_path = Path(tmp) / "stop.marker"
            captured = {}

            def _fake_run_resolved_queue(*args, **kwargs):
                captured.update(kwargs)
                return {"noop": True, "queue_state": batch.QUEUE_COMPLETED}

            buf = io.StringIO()
            with mock.patch("batch._run_resolved_queue", side_effect=_fake_run_resolved_queue):
                with redirect_stdout(buf):
                    code = batch.main([
                        "run", "--queue", str(queue_path),
                        "--stop-marker", str(marker_path),
                        "--deadline-minutes", "60",
                        "--no-container-control",
                    ])
            self.assertEqual(code, 0)
            self.assertEqual(captured["stop_marker_path"], marker_path)
            self.assertEqual(captured["deadline_minutes"], 60)


class TestCLI(unittest.TestCase):
    def test_dry_run_via_main_prints_json_and_exits_zero(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = batch.main(["run", "--queue", str(queue_path), "--dry-run"])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["cell_count"], 4)

    def test_cmd_verify_reports_a_clean_error_when_verify_run_is_absent(self):
        # verify_run.py exists since WP-4 landed, so the genuinely-absent-module state this test
        # originally exercised (WP-1's dispatch-seam-only era) is no longer reachable on disk.
        # Masking the module (`sys.modules[name] = None` makes `import verify_run` raise
        # ImportError) keeps cmd_verify's clean-error path -- still real code -- covered without
        # deleting verify_run.py. The landed seam's end-to-end success path is covered by
        # test_verify_run.py's own `batch.main(["verify", ...])` test.
        import io
        from contextlib import redirect_stderr
        from unittest import mock

        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"verify_run": None}), redirect_stderr(buf):
            code = batch.main(["verify", "--run", "/tmp/does-not-matter"])
        self.assertEqual(code, 2)
        self.assertIn("verify_run.py not found", buf.getvalue())


class TestCmdRunLoadsQueueOnce(unittest.TestCase):
    """WP-R1 regression: `cmd_run` must resolve its queue file exactly once (one `load_queue`
    call, one `resolve_queue_cells` call, both inside `queue_file.load_resolved_queue`) and
    thread that SAME resolved value into both the CLI-composed cell runner and the dry-run
    report / state snapshot. Pre-WP-R1, `cmd_run` called `load_queue` itself to build
    `make_longcontext_cell_runner`'s `vram_profile`/`timeout`, and `run_queue` loaded the same
    file a second time internally -- two independent reads/resolutions that could silently
    disagree if the file changed between them (or, as proven here, simply differ because nothing
    enforced they came from the same load).

    `queue_file.load_queue` is patched, not `batch.load_queue`: `batch.load_queue` is a separate
    binding of the same function object (`from queue_file import load_queue`), and
    `queue_file.load_resolved_queue`'s own call to `load_queue` resolves the name against
    `queue_file`'s module globals, not `batch`'s -- patching `batch.load_queue` would silently
    not intercept anything.
    """

    def test_dry_run_uses_a_single_load_and_threads_it_into_runner_and_report(self):
        # Two distinct valid queues (differing vram_profile AND request_timeout_s) so a stray
        # second `load_queue` call would be caught two different ways: the dry-run report's
        # `vram_profile` and the cell-runner factory's stamped `vram_profile`/`timeout` would
        # disagree with each other (one from queue_a, one from queue_b) instead of both reading
        # queue_a.
        queue_a = _minimal_queue(request_timeout_s=1800, vram_profile="desktop-shared")
        queue_b = _minimal_queue(request_timeout_s=900, vram_profile="headless")

        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, queue_a)  # on-disk bytes are irrelevant once mocked
            captured_runner_kwargs = {}
            real_factory = batch.make_longcontext_cell_runner

            def spy_factory(*args, **kwargs):
                captured_runner_kwargs.update(kwargs)
                return real_factory(*args, **kwargs)

            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with mock.patch("queue_file.load_queue", side_effect=[queue_a, queue_b]) as mock_load:
                with mock.patch.object(batch, "make_longcontext_cell_runner", spy_factory):
                    with redirect_stdout(buf):
                        code = batch.main(["run", "--queue", str(queue_path), "--dry-run"])

            self.assertEqual(code, 0)
            self.assertEqual(mock_load.call_count, 1)  # exactly one load per CLI invocation

            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["vram_profile"], "desktop-shared")  # queue_a, never queue_b
            self.assertEqual(captured_runner_kwargs["vram_profile"], "desktop-shared")
            self.assertEqual(captured_runner_kwargs["timeout"], 1800)


class TestRunQueueLoadsQueueOnce(unittest.TestCase):
    """Direct-`run_queue` counterpart to `TestCmdRunLoadsQueueOnce`: the public wrapper
    (`batch.run_queue`) still loads and resolves its queue file itself -- exactly once, via its
    own `load_resolved_queue` call -- and its dry-run behavior is unchanged from before the
    WP-R1 split into `run_queue` + `_run_resolved_queue`."""

    def test_run_queue_dry_run_calls_load_queue_and_resolve_queue_cells_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            real_load_queue = queue_file.load_queue
            real_resolve_queue_cells = queue_file.resolve_queue_cells
            with mock.patch(
                "queue_file.load_queue", side_effect=real_load_queue,
            ) as mock_load, mock.patch(
                "queue_file.resolve_queue_cells", side_effect=real_resolve_queue_cells,
            ) as mock_resolve:
                result = batch.run_queue(queue_path, dry_run=True)

            self.assertEqual(mock_load.call_count, 1)
            self.assertEqual(mock_resolve.call_count, 1)
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["cell_count"], 4)
            self.assertEqual(result["vram_profile"], "desktop-shared")


class TestWp5DetailPersistence(unittest.TestCase):
    """WP-5 glue: `run_queue` persists `CellOutcome.detail` into the state file (WP-2's runner
    populated it but nothing wrote it durably -- the 262144 fit-measurement margin, audit B1's
    WP-6 admission condition, needs an on-disk home)."""

    def test_outcome_detail_persisted_in_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            detail = {"vram_before_bytes": 111, "vram_after_bytes": 222, "sample_count": 3}
            result = batch.run_queue(
                queue_path,
                cell_runner=lambda handle, cell: batch.CellOutcome("completed", None, False, dict(detail)),
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            for cs in result["state"]["cells"].values():
                self.assertEqual(cs["detail"], detail)
            # And durably: the saved state file carries it too, not just the in-memory dict.
            saved = json.loads(batch.default_state_path(queue_path).read_text())
            for cs in saved["cells"].values():
                self.assertEqual(cs["detail"], detail)

    def test_deferred_vram_cell_detail_gains_vram_readbacks(self):
        battery_fn = _fake_battery_fn_factory({})
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 4321,
        )
        outcome = runner(_handle(vram_before=1000), _longcontext_cell(battery=["vram"]))
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(battery_fn.calls, [])  # still deferred -- battery never ran
        self.assertEqual(outcome.detail["vram_before_bytes"], 1000)
        self.assertEqual(outcome.detail["vram_after_bytes"], 4321)
        self.assertIn("startup_details", outcome.detail)

    def test_deferred_vram_cell_writes_record_when_records_dir_set(self):
        # WP-5 verifier finding: a completed non-needle cell with no records subdirectory fails
        # verify_run's records_present check -- the deferral path must write the same four-file
        # record shape (empty sample set; the config's vram numbers are the payload).
        with tempfile.TemporaryDirectory() as td:
            records_dir = Path(td)
            runner = batch.make_longcontext_cell_runner(
                battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 4321,
                records_dir=records_dir,
            )
            cell = _longcontext_cell(battery=["vram"])
            outcome = runner(_handle(vram_before=1000), cell)
            self.assertEqual(outcome.status, "completed")
            self.assertIsNone(outcome.detail["record_error"])
            record_dir = Path(outcome.detail["record_path"])
            self.assertTrue(record_dir.is_dir())
            self.assertEqual(record_dir.parent, records_dir)
            for fname in ("config.json", "samples.jsonl", "completions.jsonl", "prompts.jsonl"):
                self.assertTrue((record_dir / fname).exists(), fname)
            config = json.loads((record_dir / "config.json").read_text())
            self.assertEqual(config["vram"]["baseline_bytes"], 1000)
            self.assertEqual(config["vram"]["loaded_bytes"], 4321)


class TestWp5CliArgs(unittest.TestCase):
    def test_scratch_container_and_records_dir_default_to_none(self):
        parser = batch.build_arg_parser()
        args = parser.parse_args(["run", "--queue", "q.json"])
        self.assertIsNone(args.scratch_container)
        self.assertIsNone(args.records_dir)


class TestCmdReArm(unittest.TestCase):
    """Transport-failure-taxonomy WP (`plans/bakeoff-transport-taxonomy-and-prompt-fit.md`
    WP-1), Change 4: standalone `batch.py re-arm` -- pure state-file mutation, never a window,
    never a container/lock/journal side effect."""

    def _seeded_state(self, tmp):
        queue = _minimal_queue()
        queue_path = _write_queue(tmp, queue)
        cells = batch.resolve_queue_cells(queue)
        state = batch.init_state(cells)
        # cell 0: completed: cell 1: failed; cell 2: skipped-propagated; cell 3: left pending.
        ids = [c.cell_id for c in cells]
        state["cells"][ids[0]]["state"] = batch.COMPLETED
        state["cells"][ids[1]]["state"] = batch.FAILED
        state["cells"][ids[1]]["reason"] = "needle_failure"
        state["cells"][ids[2]]["state"] = batch.SKIPPED_PROPAGATED
        state["cells"][ids[2]]["reason"] = "propagated from failing cell x"
        state["queue_state"] = batch.QUEUE_ABORTED_HANG
        batch.save_state(batch.default_state_path(queue_path), state)
        return queue_path, cells, ids

    def test_no_state_file_errors_cleanly(self):
        import io
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = batch.main(["re-arm", "--queue", str(queue_path)])
            self.assertEqual(code, 2)
            self.assertIn("no state file found", buf.getvalue())

    def test_targeted_re_arm_touches_only_named_cells(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            queue_path, _cells, ids = self._seeded_state(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = batch.main(
                    ["re-arm", "--queue", str(queue_path), "--cell", ids[1], "--cell", ids[2]]
                )
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(set(payload["re_armed"]), {ids[1], ids[2]})
            self.assertEqual(payload["queue_state"], batch.QUEUE_RUNNING)

            reloaded = batch.load_state(batch.default_state_path(queue_path))
            self.assertEqual(reloaded["cells"][ids[1]]["state"], batch.PENDING)
            self.assertIsNone(reloaded["cells"][ids[1]]["reason"])
            self.assertEqual(reloaded["cells"][ids[2]]["state"], batch.PENDING)
            # completed cell (not named) untouched
            self.assertEqual(reloaded["cells"][ids[0]]["state"], batch.COMPLETED)
            self.assertEqual(reloaded["queue_state"], batch.QUEUE_RUNNING)

    def test_all_form_re_arms_every_non_completed_cell(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            queue_path, _cells, ids = self._seeded_state(tmp)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = batch.main(["re-arm", "--queue", str(queue_path)])
            self.assertEqual(code, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(set(payload["re_armed"]), {ids[1], ids[2], ids[3]})

            reloaded = batch.load_state(batch.default_state_path(queue_path))
            self.assertEqual(reloaded["cells"][ids[0]]["state"], batch.COMPLETED)  # untouched
            for cid in (ids[1], ids[2], ids[3]):
                self.assertEqual(reloaded["cells"][cid]["state"], batch.PENDING)
            self.assertEqual(reloaded["queue_state"], batch.QUEUE_RUNNING)

    def test_unknown_cell_name_errors_cleanly_and_writes_nothing(self):
        import io
        from contextlib import redirect_stderr

        with tempfile.TemporaryDirectory() as tmp:
            queue_path, _cells, _ids = self._seeded_state(tmp)
            state_path = batch.default_state_path(queue_path)
            before = state_path.read_text(encoding="utf-8")
            buf = io.StringIO()
            with redirect_stderr(buf):
                code = batch.main(["re-arm", "--queue", str(queue_path), "--cell", "no-such-cell"])
            self.assertEqual(code, 2)
            self.assertIn("no-such-cell", buf.getvalue())
            self.assertEqual(state_path.read_text(encoding="utf-8"), before)  # byte-untouched

    def test_re_arm_never_invokes_subprocess(self):
        # Proves the "never touches containers, locks, or journalctl" invariant at the
        # subprocess level -- ContainerController.stop/start and poll_journal_for_hang both go
        # through batch.subprocess.run; re-arm must never reach either.
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            queue_path, _cells, ids = self._seeded_state(tmp)
            buf = io.StringIO()
            with mock.patch.object(
                batch.subprocess, "run",
                side_effect=AssertionError("re-arm must never call subprocess.run"),
            ), redirect_stdout(buf):
                code = batch.main(["re-arm", "--queue", str(queue_path), "--cell", ids[1]])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
