"""Tests owned by `queue_state.py`: state-machine primitives, skip propagation/resume
invariants, WP-6 re-arm-cell and queue/state mismatch guards, the WP-B old-state-without-alias
compatibility read, and the WP-1 `request_timeout_s` state-snapshot round trip. Split from the
former `test_batch.py` omnibus module (WP-R3 of
plans/queue-contract-and-test-decomposition-remediation.md); no test logic changed in the move.

No live Docker/GPU execution anywhere in this suite -- fixtures imported from `batch_fixtures`
are always fakes."""

import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from batch_fixtures import (  # noqa: E402
    _minimal_queue,
    _write_queue,
    fake_scratch_server_factory,
    fixed_now_fn,
)

import batch  # noqa: E402
import sweep  # noqa: E402


class TestStateMachinePrimitives(unittest.TestCase):
    def test_init_state_all_pending(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        self.assertEqual(state["queue_state"], batch.QUEUE_RUNNING)
        self.assertTrue(all(cs["state"] == batch.PENDING for cs in state["cells"].values()))

    def test_init_state_writes_alias_at_cell_entry_top_level(self):
        # WP-B: every cell (including a still-`pending` one, before any run) is alias-joinable
        # from the state file alone -- `alias` sits beside `state`/`reason`, not nested under
        # `detail` (which doesn't exist yet for a pending cell).
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        for cell in resolved:
            self.assertEqual(state["cells"][cell.cell_id]["alias"], cell.alias)

    def test_apply_interrupted_rule_marks_running_as_failed_interrupted(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        crashed_id = resolved[0].cell_id
        state["cells"][crashed_id]["state"] = batch.RUNNING
        changed = batch.apply_interrupted_rule(state)
        self.assertEqual(changed, [crashed_id])
        self.assertEqual(state["cells"][crashed_id]["state"], batch.FAILED)
        self.assertEqual(state["cells"][crashed_id]["reason"], "interrupted")

    def test_apply_interrupted_rule_preserves_alias(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        crashed_id = resolved[0].cell_id
        state["cells"][crashed_id]["state"] = batch.RUNNING
        batch.apply_interrupted_rule(state)
        self.assertEqual(state["cells"][crashed_id]["alias"], resolved[0].alias)

    def test_apply_interrupted_rule_is_a_noop_when_nothing_is_running(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        self.assertEqual(batch.apply_interrupted_rule(state), [])

    def test_re_arm_resets_everything_but_completed(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        ids = [c.cell_id for c in resolved]
        state["cells"][ids[0]]["state"] = batch.COMPLETED
        state["cells"][ids[1]]["state"] = batch.FAILED
        state["cells"][ids[2]]["state"] = batch.SKIPPED_PROPAGATED
        state["cells"][ids[3]]["state"] = batch.ABORTED_HANG
        state["queue_state"] = batch.QUEUE_ABORTED_HANG
        batch.re_arm_state(state)
        self.assertEqual(state["cells"][ids[0]]["state"], batch.COMPLETED)
        self.assertEqual(state["cells"][ids[1]]["state"], batch.PENDING)
        self.assertEqual(state["cells"][ids[2]]["state"], batch.PENDING)
        self.assertEqual(state["cells"][ids[3]]["state"], batch.PENDING)
        self.assertEqual(state["queue_state"], batch.QUEUE_RUNNING)

    def test_re_arm_state_clears_paused_reason(self):
        # control-plane WP-2: a `paused_reason` left over from a prior deadline/stop pause no
        # longer describes anything once the queue is re-armed -- must be popped, not left stale.
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        state["paused_reason"] = "stop-requested"
        batch.re_arm_state(state)
        self.assertNotIn("paused_reason", state)

    def test_re_arm_state_preserves_alias(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        for cs in state["cells"].values():
            cs["state"] = batch.FAILED
        batch.re_arm_state(state)
        for cell in resolved:
            self.assertEqual(state["cells"][cell.cell_id]["alias"], cell.alias)

    def test_save_and_load_state_round_trips(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "queue.state.json"
            batch.save_state(state_path, state)
            loaded = batch.load_state(state_path)
            self.assertEqual(loaded["queue_state"], batch.QUEUE_RUNNING)
            self.assertIn("updated_at", loaded)

    def test_load_state_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(batch.load_state(Path(tmp) / "nope.state.json"))

    def test_default_state_path_naming(self):
        queue_path = Path("/x/y/foo.json")
        self.assertEqual(batch.default_state_path(queue_path), Path("/x/y/foo.state.json"))


class TestSkipPropagation(unittest.TestCase):
    def setUp(self):
        self.queue = _minimal_queue()
        self.resolved = batch.resolve_queue_cells(self.queue)
        self.state = batch.init_state(self.resolved)
        self.by_alias = {c.alias: c for c in self.resolved}

    def test_quantized_failure_propagates_to_later_larger_c_same_model_same_kv_type(self):
        failing = self.by_alias["model-a-c32k-q4"]
        changed = batch.propagate_skips(self.resolved, self.state, failing.cell_id)
        larger_same_type = self.by_alias["model-a-c65k-q4"].cell_id
        self.assertIn(larger_same_type, changed)
        self.assertEqual(self.state["cells"][larger_same_type]["state"], batch.SKIPPED_PROPAGATED)
        self.assertIn(failing.cell_id, self.state["cells"][larger_same_type]["reason"])

    def test_does_not_propagate_to_f16_cells(self):
        failing = self.by_alias["model-a-c32k-q4"]
        changed = batch.propagate_skips(self.resolved, self.state, failing.cell_id)
        f16_cell = self.by_alias["model-a-c65k-f16"].cell_id
        self.assertNotIn(f16_cell, changed)
        self.assertEqual(self.state["cells"][f16_cell]["state"], batch.PENDING)

    def test_f16_failure_never_propagates_at_all(self):
        failing = self.by_alias["model-a-c32k-f16"]
        changed = batch.propagate_skips(self.resolved, self.state, failing.cell_id)
        self.assertEqual(changed, [])

    def test_does_not_propagate_to_smaller_or_equal_context(self):
        failing = self.by_alias["model-a-c65k-q4"]  # the LARGER-c quantized cell fails
        changed = batch.propagate_skips(self.resolved, self.state, failing.cell_id)
        smaller = self.by_alias["model-a-c32k-q4"].cell_id
        self.assertNotIn(smaller, changed)

    def test_only_touches_pending_cells(self):
        failing = self.by_alias["model-a-c32k-q4"]
        larger_id = self.by_alias["model-a-c65k-q4"].cell_id
        self.state["cells"][larger_id]["state"] = batch.COMPLETED
        changed = batch.propagate_skips(self.resolved, self.state, failing.cell_id)
        self.assertEqual(changed, [])
        self.assertEqual(self.state["cells"][larger_id]["state"], batch.COMPLETED)


class TestWp6ReArmCell(unittest.TestCase):
    """WP-6: targeted per-cell re-arm (`--re-arm-cell`, the WP-5 hand-edit made first-class)."""

    def _state_and_cells(self):
        cells = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(cells)
        for cs in state["cells"].values():
            cs["state"] = batch.COMPLETED
        state["queue_state"] = batch.QUEUE_COMPLETED
        return state, cells

    def test_re_arms_named_cell_by_cell_id_and_resets_queue_state(self):
        state, cells = self._state_and_cells()
        target = cells[1].cell_id
        changed = batch.re_arm_cells(state, [target], cells)
        self.assertEqual(changed, [target])
        self.assertEqual(state["cells"][target]["state"], batch.PENDING)
        self.assertEqual(state["queue_state"], batch.QUEUE_RUNNING)
        # every other cell untouched
        for cell_id, cs in state["cells"].items():
            if cell_id != target:
                self.assertEqual(cs["state"], batch.COMPLETED)

    def test_re_arms_by_queue_alias(self):
        state, cells = self._state_and_cells()
        changed = batch.re_arm_cells(state, ["model-a-c65k-f16"], cells)
        self.assertEqual(len(changed), 1)
        self.assertEqual(state["cells"][changed[0]]["state"], batch.PENDING)

    def test_re_arm_cells_preserves_alias(self):
        # WP-B: `re_arm_cells` mutates `state`/`reason` in place -- the top-level `alias` set by
        # `init_state` must survive a targeted re-arm untouched.
        state, cells = self._state_and_cells()
        target = cells[1].cell_id
        batch.re_arm_cells(state, [target], cells)
        self.assertEqual(state["cells"][target]["alias"], cells[1].alias)

    def test_unknown_name_raises_instead_of_silently_re_arming_nothing(self):
        state, cells = self._state_and_cells()
        with self.assertRaises(batch.QueueValidationError):
            batch.re_arm_cells(state, ["no-such-cell"], cells)

    def test_cli_re_arm_cell_is_repeatable(self):
        parser = batch.build_arg_parser()
        args = parser.parse_args(
            ["run", "--queue", "q.json", "--re-arm-cell", "a", "--re-arm-cell", "b"]
        )
        self.assertEqual(args.re_arm_cell, ["a", "b"])


class TestWp6QueueStateMismatch(unittest.TestCase):
    """WP-6 (WP-2 punt): a queue file edited after its state file exists fails loudly with a
    QueueValidationError, never a bare KeyError from the cell loop."""

    def test_edited_queue_with_existing_state_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=UTC)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            edited = _minimal_queue()
            edited["cells"][0]["c"] = 131072  # changes that cell's cell_id
            edited_path = _write_queue(tmp, edited)  # same path: same sibling state file
            with self.assertRaises(batch.QueueValidationError) as ctx:
                batch.run_queue(
                    edited_path,
                    re_arm=True,  # even an explicit re-arm never bridges a mismatched queue
                    scratch_server_fn=fake_scratch_server_factory(),
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=UTC)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                )
            self.assertIn("immutable", str(ctx.exception))


class TestWpBOldStateWithoutAlias(unittest.TestCase):
    """WP-B: `alias` at the cell-entry top level is new, additive state-file surface -- a state
    file written before this WP (no `alias` key on any cell entry) must still load and run to
    completion, never require the field on read."""

    def test_old_state_without_alias_still_loads_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            resolved = batch.resolve_queue_cells(_minimal_queue())
            # Hand-built pre-WP-B state shape: `state`/`reason` only, no `alias` anywhere.
            old_state = {
                "queue_state": batch.QUEUE_RUNNING,
                "created_at": "2026-01-01T00:00:00+00:00",
                "cells": {c.cell_id: {"state": batch.PENDING, "reason": None} for c in resolved},
            }
            batch.save_state(batch.default_state_path(queue_path), old_state)
            result = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=UTC)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            for cs in result["state"]["cells"].values():
                self.assertEqual(cs["state"], batch.COMPLETED)


class TestWp1RequestTimeoutSStateSnapshot(unittest.TestCase):
    """`request_timeout_s` is excluded from `cell_id` (cell_spec.py:86-94), so it is snapshotted
    into the queue state at creation (`queue_state.init_state`) and compared on resume
    (`batch._load_or_init_state`) -- otherwise a queue edited after its state exists could change
    the effective timeout across a pause/resume undetected."""

    def test_init_state_snapshots_the_effective_value(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved, request_timeout_s=1800)
        self.assertEqual(state["request_timeout_s"], 1800)

    def test_init_state_defaults_to_300(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        self.assertEqual(state["request_timeout_s"], batch.DEFAULT_REQUEST_TIMEOUT_S)
        self.assertEqual(state["request_timeout_s"], 300)

    def test_resume_with_a_changed_effective_timeout_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=UTC)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            edited = _minimal_queue(request_timeout_s=1800)  # same cells -- only the timeout moved
            edited_path = _write_queue(tmp, edited)  # same path: same sibling state file
            with self.assertRaises(batch.QueueValidationError) as ctx:
                batch.run_queue(
                    edited_path,
                    re_arm=True,  # even an explicit re-arm never bridges a mismatched timeout
                    scratch_server_fn=fake_scratch_server_factory(),
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=UTC)),
                    vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                    vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
                )
            self.assertIn("immutable", str(ctx.exception))

    def test_resume_with_an_old_state_missing_the_key_loads_cleanly(self):
        # Absent-key compatibility: a state file written before this WP has no
        # request_timeout_s key at all; against a queue whose effective value is also the
        # default (300), resume must load cleanly, never require the field on read.
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())  # no override -> effective 300
            resolved = batch.resolve_queue_cells(_minimal_queue())
            old_state = {
                "queue_state": batch.QUEUE_RUNNING,
                "created_at": "2026-01-01T00:00:00+00:00",
                "cells": {c.cell_id: {"state": batch.PENDING, "reason": None} for c in resolved},
            }
            batch.save_state(batch.default_state_path(queue_path), old_state)
            result = batch.run_queue(
                queue_path,
                scratch_server_fn=fake_scratch_server_factory(),
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=UTC)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
