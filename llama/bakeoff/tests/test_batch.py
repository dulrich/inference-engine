"""Queue-driven batch runner (plan longcontext-pow2-overnight-campaign.md WP-1). No live
Docker/GPU execution anywhere in this suite -- `scratch_server_fn`/`container_controller`/
`hang_check_fn`/`vram_*_bytes_fn`/`now_fn` are always fakes; `run_queue` never falls through to
its real (Docker/sysfs-touching) defaults in any test here.

WP-R1: `resolve_queue_cells` now returns typed `cell_model.ResolvedCell` values instead of plain
dicts, so cell reads here are attribute reads (`cell.cell_id`, `cell.queue.sampling_arm`) rather
than the former dict/underscore-key indexing. The scratch-server seam still speaks plain dicts --
`fake_scratch_server_factory` below receives `cell.as_sweep_cell()`, exactly as `sweep.py` does.
The identity contract itself is locked separately, by `tests/test_cell_identity.py`."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import longcontext  # noqa: E402
import run_harness  # noqa: E402
import sampling_table  # noqa: E402
import sweep  # noqa: E402

# -------------------------------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------------------------------


def _minimal_queue(**overrides):
    queue = {
        "queue_format": 1,
        "vram_profile": "desktop-shared",
        "deadline": "23:59",
        "stop_policy": "pause-at-deadline",
        "models": {
            "model-a": {
                "model_container_path": "/models/a/model-a.gguf",
                "server_flags": {"-ngl": 99},
            },
            "model-b": {
                "model_container_path": "/models/b/model-b.gguf",
                "server_flags": {"-ngl": 99, "-ncmoe": 32},
                "gpu_weights_bytes": 6 * sweep.GIB,
                "kv_attention_layers": 10,
            },
        },
        "cells": [
            {
                "model": "model-a", "alias": "model-a-c32k-f16", "c": 32768,
                "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
                "sampling_arm": "capability", "trials": 3,
            },
            {
                "model": "model-a", "alias": "model-a-c65k-f16", "c": 65536,
                "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
                "sampling_arm": "capability", "trials": 3,
            },
            {
                "model": "model-a", "alias": "model-a-c32k-q4", "c": 32768,
                "ctk": "q4_0", "ctv": "q4_0", "fa": "on", "battery": ["vram", "needle"],
                "sampling_arm": "capability", "trials": 3,
            },
            {
                "model": "model-a", "alias": "model-a-c65k-q4", "c": 65536,
                "ctk": "q4_0", "ctv": "q4_0", "fa": "on", "battery": ["vram", "needle"],
                "sampling_arm": "capability", "trials": 3,
            },
        ],
    }
    queue.update(overrides)
    return queue


def _write_queue(tmpdir, queue):
    path = Path(tmpdir) / "queue.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queue, f)
    return path


class _FakeScratchServerCtx:
    def __init__(self, cell, port, container, healthy, verified):
        self.cell = cell
        self.port = port
        self.container = container
        self.healthy = healthy
        self.verified = verified
        self.handle = None

    def __enter__(self):
        self.handle = sweep.ScratchServerHandle(
            cell_id=self.cell["cell_id"], base_url="http://fake:1", healthy=self.healthy,
            verified=self.verified, details={}, log_text="", vram_before_bytes=0,
            port=self.port, container=self.container,
        )
        return self.handle

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            self.handle.teardown_result = {"signalled": False}
        return False


def fake_scratch_server_factory(healthy=True, verified=True, spawned=None):
    def _fn(cell, port, container):
        if spawned is not None:
            spawned.append(cell["cell_id"])
        return _FakeScratchServerCtx(cell, port, container, healthy, verified)
    return _fn


def scripted_cell_runner(outcomes_by_cell_id, default=None):
    default = default or batch.CellOutcome("completed")

    def _runner(handle, cell):
        return outcomes_by_cell_id.get(cell.cell_id, default)
    return _runner


class FakeContainerController:
    def __init__(self, fail_start=False):
        self.calls = []
        self.fail_start = fail_start

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("docker start failed")


def fixed_now_fn(dt):
    return lambda: dt


def sequenced_now_fn(first, rest):
    """First call returns `first` (used by `run_queue` as `start_dt`, the anchor
    `compute_deadline_dt` computes a strictly-future deadline from); every call after that
    returns `rest`. Needed to test "the deadline has already passed" -- `compute_deadline_dt`
    always returns a `deadline_dt` strictly later than whatever `now_fn()` returned when it was
    computed, so a plain fixed clock can never trip the deadline check inside the same
    invocation; a clock that visibly advances between the anchor read and the per-cell check
    can."""
    state = {"used_first": False}

    def _fn():
        if not state["used_first"]:
            state["used_first"] = True
            return first
        return rest

    return _fn


# -------------------------------------------------------------------------------------------
# Queue validation + resolution
# -------------------------------------------------------------------------------------------


class TestQueueValidation(unittest.TestCase):
    def test_valid_minimal_queue_passes(self):
        batch.validate_queue(_minimal_queue())  # must not raise

    def test_missing_queue_format_raises(self):
        q = _minimal_queue()
        del q["queue_format"]
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_wrong_queue_format_version_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(queue_format=2))

    def test_empty_models_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(models={}))

    def test_empty_cells_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(cells=[]))

    def test_cell_referencing_unknown_model_raises(self):
        q = _minimal_queue()
        q["cells"][0]["model"] = "model-nonexistent"
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_missing_deadline_raises(self):
        q = _minimal_queue()
        del q["deadline"]
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_malformed_deadline_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(deadline="not-a-time"))

    def test_unknown_vram_profile_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(vram_profile="gaming-rig"))

    def test_unknown_stop_policy_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(stop_policy="hard-stop"))

    def test_cell_missing_required_key_raises(self):
        q = _minimal_queue()
        del q["cells"][0]["trials"]
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_load_queue_from_disk_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_queue(tmp, _minimal_queue())
            loaded = batch.load_queue(path)
            self.assertEqual(loaded["queue_format"], 1)


class TestResolveQueueCells(unittest.TestCase):
    def test_resolves_every_cell_with_merged_flags(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        self.assertEqual(len(resolved), 4)
        first = resolved[0]
        self.assertEqual(first.server_flags["-c"], 32768)
        self.assertEqual(first.server_flags["-ctk"], "f16")
        self.assertEqual(first.server_flags["-ngl"], 99)  # inherited from model-a base flags
        self.assertEqual(first.queue.model_id, "model-a")
        self.assertEqual(first.queue.sampling_arm, "capability")
        self.assertEqual(first.queue.trials, 3)
        # Execution settings are DERIVED from the normalized flags, not separate state.
        self.assertEqual(first.execution.context, 32768)
        self.assertEqual(first.execution.kv_key_type, "f16")

    def test_gpu_weights_bytes_and_kv_attention_layers_propagate_from_model_registry(self):
        q = _minimal_queue(cells=[{
            "model": "model-b", "alias": "model-b-c8k", "c": 8192, "ctk": "f16", "ctv": "f16",
            "fa": "on", "battery": ["vram"], "sampling_arm": "capability", "trials": 1,
        }])
        resolved = batch.resolve_queue_cells(q)
        self.assertEqual(resolved[0].sweep_passthrough["gpu_weights_bytes"], 6 * sweep.GIB)
        self.assertEqual(resolved[0].sweep_passthrough["kv_attention_layers"], 10)
        self.assertEqual(resolved[0].server_flags["-ncmoe"], 32)
        # ...and they reach sweep.py through the one dict adapter, unchanged.
        self.assertEqual(resolved[0].as_sweep_cell()["gpu_weights_bytes"], 6 * sweep.GIB)

    def test_model_family_propagates_from_model_registry_when_present(self):
        q = _minimal_queue()
        q["models"]["model-a"]["model_family"] = "gemma4"
        resolved = batch.resolve_queue_cells(q)
        self.assertEqual(resolved[0].model_family, "gemma4")

    def test_model_family_absent_when_registry_omits_it(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())  # model-a carries no model_family
        self.assertIsNone(resolved[0].model_family)
        self.assertNotIn("model_family", resolved[0].as_sweep_cell())

    def test_cell_ids_are_distinct(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        ids = [c.cell_id for c in resolved]
        self.assertEqual(len(ids), len(set(ids)))

    def test_colliding_cell_ids_raise(self):
        q = _minimal_queue()
        q["cells"] = [dict(q["cells"][0]), dict(q["cells"][0])]  # byte-identical duplicate
        with self.assertRaises(batch.QueueValidationError):
            batch.resolve_queue_cells(q)


class TestDeadline(unittest.TestCase):
    def test_deadline_later_today_stays_today(self):
        start = datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)
        deadline_dt = batch.compute_deadline_dt("07:00", start)
        self.assertEqual(deadline_dt, datetime(2026, 8, 23, 7, 0, tzinfo=timezone.utc))

    def test_deadline_already_passed_today_rolls_to_tomorrow(self):
        start = datetime(2026, 8, 23, 23, 0, tzinfo=timezone.utc)
        deadline_dt = batch.compute_deadline_dt("07:00", start)
        self.assertEqual(deadline_dt, datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc))

    def test_deadline_exactly_at_start_rolls_to_tomorrow(self):
        start = datetime(2026, 8, 23, 7, 0, tzinfo=timezone.utc)
        deadline_dt = batch.compute_deadline_dt("07:00", start)
        self.assertEqual(deadline_dt, datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc))


# -------------------------------------------------------------------------------------------
# State machine primitives
# -------------------------------------------------------------------------------------------


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


# -------------------------------------------------------------------------------------------
# Skip propagation (audit E2)
# -------------------------------------------------------------------------------------------


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


# -------------------------------------------------------------------------------------------
# Pre-enqueue size projection (audit E4)
# -------------------------------------------------------------------------------------------


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


# -------------------------------------------------------------------------------------------
# Hang-signature detection (unit-tested against captured lines quoted verbatim from
# fleet-tools/references/rdna4-gpu-hang.md's "The signature" section)
# -------------------------------------------------------------------------------------------

_CAPTURED_HANG_CASCADE = [
    "amdgpu: failed to remove hardware queue from MES, doorbell=0x1000",
    "amdgpu: MES might be in unrecoverable state, issue a GPU reset",
    "amdgpu: Failed to evict queue 0",
    "amdgpu: Failed to evict process queues",
    "amdgpu: GPU reset begin!",
    "amdgpu: MODE1 reset",
    "amdgpu: GPU reset succeeded, trying to resume",
    "amdgpu: [drm] AMDGPU device coredump file has been created",
    "amdgpu: VRAM is lost due to GPU reset!",
    "amdgpu: GPU reset(1) succeeded!",
    "[drm] device wedged, but recovered through reset",
]

_BENIGN_LINES = [
    "amdgpu: ring gfx_0.0.0 timeout, but soft recovered",
    "systemd[1]: Started Daily apt download activities.",
    "kernel: usb 1-2: new high-speed USB device number 5 using xhci_hcd",
]


class TestHangSignatureDetection(unittest.TestCase):
    def test_detects_mes_unrecoverable_line(self):
        match = batch.detect_hang_signature(_CAPTURED_HANG_CASCADE)
        self.assertIsNotNone(match)
        self.assertIn("MES might be in unrecoverable state", match)

    def test_detects_failed_to_evict_process_queues_alone(self):
        match = batch.detect_hang_signature(["amdgpu: Failed to evict process queues"])
        self.assertIsNotNone(match)

    def test_full_cascade_line_by_line_still_matches(self):
        # journald delivers one line at a time -- feed the cascade as a generator, same as a real
        # line-by-line source, and confirm detection still fires.
        match = batch.detect_hang_signature(iter(_CAPTURED_HANG_CASCADE))
        self.assertIsNotNone(match)

    def test_benign_lines_never_match(self):
        self.assertIsNone(batch.detect_hang_signature(_BENIGN_LINES))

    def test_empty_source_returns_none(self):
        self.assertIsNone(batch.detect_hang_signature([]))


# -------------------------------------------------------------------------------------------
# default_cell_runner
# -------------------------------------------------------------------------------------------


class TestDefaultCellRunner(unittest.TestCase):
    def _handle(self, healthy, verified):
        return sweep.ScratchServerHandle(
            cell_id="x", base_url="http://fake", healthy=healthy, verified=verified,
            details={"checks": {}}, log_text="", vram_before_bytes=0, port=1, container="fake",
        )

    def test_healthy_and_verified_completes(self):
        outcome = batch.default_cell_runner(self._handle(True, True))
        self.assertEqual(outcome.status, "completed")
        self.assertFalse(outcome.correctness_failure)

    def test_unhealthy_fails_health_timeout(self):
        outcome = batch.default_cell_runner(self._handle(False, True))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "health_timeout")
        self.assertFalse(outcome.correctness_failure)

    def test_healthy_but_unverified_fails_invalid_config(self):
        outcome = batch.default_cell_runner(self._handle(True, False))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "invalid_config")


# -------------------------------------------------------------------------------------------
# make_longcontext_cell_runner (plan longcontext-pow2-overnight-campaign.md WP-2's battery
# hookup) -- `battery_fn`/`production_sampling_fn`/`vram_used_bytes_fn` are always fakes here;
# this suite never calls `longcontext.run_live_battery` (a live network call) or
# `sweep.read_vram_used_bytes` (real host sysfs).
# -------------------------------------------------------------------------------------------


def _handle(healthy=True, verified=True, base_url="http://fake-bridge:9001", vram_before=1000):
    return sweep.ScratchServerHandle(
        cell_id="lc-cell", base_url=base_url, healthy=healthy, verified=verified,
        details={"checks": {}}, log_text="", vram_before_bytes=vram_before, port=1,
        container="fake",
    )


def _longcontext_cell(battery=("vram", "needle"), sampling_arm="capability", trials=3,
                      model_family="gemma4", context=32768, chars_per_token=None,
                      model_id=None, spec_decode=None, timing_rounds=None,
                      model_host_path=None):
    """A hand-built `ResolvedCell` for the runner/record tests.

    Every knob that used to be poked onto the cell dict after construction (`_chars_per_token`,
    `model`, `spec_decode`, `model_host_path`, ...) is a builder keyword now: `ResolvedCell` is
    frozen, and that is the point -- a test can no longer attach a field to a cell in an order
    that production code could not reproduce. `cell_id` is DERIVED from the identity fields
    rather than asserted as a literal, so these fixtures exercise the same identity path
    production does.
    """
    flags = {"-c": context}
    return batch.ResolvedCell(
        identity=batch.CellIdentity(
            alias="lc-alias",
            model_container_path="/models/lc/lc-model.gguf",
            server_flags=flags,
            model_host_path=model_host_path,
        ),
        execution=batch.ExecutionSettings.from_flags(flags, tuple(battery)),
        queue=batch.QueueCellMetadata(
            model_id=model_id, sampling_arm=sampling_arm, trials=trials,
            chars_per_token=chars_per_token, timing_rounds=timing_rounds,
            spec_decode=spec_decode,
        ),
        sweep_passthrough={} if model_family is None else {"model_family": model_family},
    )


def _sample(task, verdict, correct, success=True):
    return {
        "task": task, "classifier_verdict": verdict, "correct": correct, "success": success,
    }


def _fake_battery_fn_factory(samples_by_task):
    """Returns a `battery_fn` stub matching `longcontext.run_live_battery`'s return shape
    (`{task_name: {position_fraction: CellResult}}`) -- one CellResult per task, all samples at a
    single synthetic position (0.45), and records every call's arguments for assertion."""
    calls = []

    def _fn(base_url, alias, cell_id_prefix, target_tokens, sampling_arm, sampling_payload,
            n_trials=10, max_tokens=256, timeout=300, chars_per_token=None,
            max_tokens_by_task=None, tokenize_fn=None, token_budget=None):
        # WP-2 (transport-taxonomy-and-prompt-fit): `tokenize_fn`/`token_budget` are the new
        # prompt-fit collaborators `_runner` now passes through this seam unconditionally
        # (same as `chars_per_token`) -- accepted and recorded here so pre-WP-2 tests that never
        # asserted on them keep passing unmodified, and `TestBudgetArithmeticThroughBatteryFn`
        # below can assert the exact values `_runner` computed.
        calls.append({
            "base_url": base_url, "alias": alias, "cell_id_prefix": cell_id_prefix,
            "target_tokens": target_tokens, "sampling_arm": sampling_arm,
            "sampling_payload": dict(sampling_payload), "n_trials": n_trials,
            "chars_per_token": chars_per_token,
            "max_tokens_by_task": dict(max_tokens_by_task) if max_tokens_by_task else None,
            "tokenize_fn": tokenize_fn, "token_budget": token_budget,
        })
        out = {}
        for task_name, samples in samples_by_task.items():
            cell_result = longcontext.CellResult(samples=samples, completions=[], summary=None)
            out[task_name] = {0.45: cell_result}
        return out

    _fn.calls = calls
    return _fn


class TestLongcontextCellRunner(unittest.TestCase):
    def test_unhealthy_fails_before_battery_runs(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(healthy=False), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "health_timeout")
        self.assertFalse(outcome.correctness_failure)
        self.assertEqual(battery_fn.calls, [])  # never even attempted

    def test_unverified_fails_before_battery_runs(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(verified=False), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "invalid_config")
        self.assertEqual(battery_fn.calls, [])

    def test_non_needle_battery_defers_to_default_cell_runner(self):
        battery_fn = _fake_battery_fn_factory({})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell(battery=["vram"]))
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(battery_fn.calls, [])  # default_cell_runner never touches the battery

    def test_clean_battery_completes(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "correct", True)],
            "passage_qa": [_sample("passage_qa", "correct", True)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(outcome.reason)
        self.assertFalse(outcome.correctness_failure)

    def test_degenerate_sample_fails_with_correctness_failure(self):
        battery_fn = _fake_battery_fn_factory({
            "passage_qa": [_sample("passage_qa", "degenerate", False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "degenerate")
        self.assertTrue(outcome.correctness_failure)
        self.assertIn(outcome.reason, batch.CORRECTNESS_FAILURE_REASONS)

    def test_needle_recall_wrong_fails_with_correctness_failure(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "wrong", False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "needle_failure")
        self.assertTrue(outcome.correctness_failure)
        self.assertIn(outcome.reason, batch.CORRECTNESS_FAILURE_REASONS)

    def test_multi_needle_wrong_also_counts_as_needle_failure(self):
        battery_fn = _fake_battery_fn_factory({
            "multi_needle": [_sample("multi_needle", "wrong", False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.reason, "needle_failure")

    def test_passage_qa_wrong_alone_does_not_trigger_needle_failure(self):
        # passage_qa is NoLiMa-class non-literal recall, deliberately excluded from the
        # needle_failure predicate (this module's own interpretive call -- see the runner's
        # docstring).
        battery_fn = _fake_battery_fn_factory({
            "passage_qa": [_sample("passage_qa", "wrong", False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "completed")

    def test_truncated_benign_alone_does_not_trigger_needle_failure(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "truncated(benign)", False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "completed")

    def test_degenerate_takes_priority_over_needle_failure(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "wrong", False)],
            "passage_qa": [_sample("passage_qa", "degenerate", False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.reason, "degenerate")

    def test_transport_failure_without_correctness_issue(self):
        # passage_qa (never a needle_failure source) with success=False isolates a pure
        # transport/HTTP failure from the correctness-classification checks above.
        battery_fn = _fake_battery_fn_factory({
            "passage_qa": [_sample("passage_qa", "wrong", False, success=False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "transport_error")
        self.assertFalse(outcome.correctness_failure)

    def test_transport_failure_on_needle_task_never_becomes_needle_failure(self):
        # Regression (transport-failure-taxonomy WP, plan Context): the observed production
        # miscount -- an HTTP-400-failed single_needle/multi_needle trial reads (on an
        # old-schema or otherwise stale record) as classifier_verdict="wrong", which pre-fix
        # fell through to needle_failure and armed skip-propagation on a transport fact.
        # `longcontext.run_cell` no longer emits this shape at all (Change 1), but this
        # predicate-filtering (Change 2, defense in depth) means even a record shaped like this
        # can never trip needle_failure.
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "wrong", False, success=False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "transport_error")
        self.assertFalse(outcome.correctness_failure)

    def test_transport_failure_takes_priority_over_degenerate(self):
        # Reorder regression: a genuine degenerate sample (success=True) coexisting with a
        # transport-failed sample (success=False) in the same battery must report the transport
        # fact FIRST -- never mask it behind (or be masked by) a correctness verdict.
        battery_fn = _fake_battery_fn_factory({
            "passage_qa": [_sample("passage_qa", "degenerate", False)],
            "single_needle": [_sample("single_needle", "wrong", False, success=False)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "transport_error")
        self.assertFalse(outcome.correctness_failure)

    def test_needle_failure_still_fires_on_a_genuine_wrong_from_a_successful_trial(self):
        # Sanity check that Change 2's success-filtering doesn't over-suppress: a real,
        # successfully-transported wrong needle answer with NO transport failures anywhere in
        # the battery still reports needle_failure exactly as before this WP.
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "wrong", False, success=True)],
        })
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "needle_failure")
        self.assertTrue(outcome.correctness_failure)

    def test_capability_arm_uses_capability_preset(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        runner(_handle(), _longcontext_cell(sampling_arm="capability"))
        self.assertEqual(battery_fn.calls[0]["sampling_arm"], "capability")
        payload = battery_fn.calls[0]["sampling_payload"]
        self.assertEqual(payload["temperature"], run_harness.SAMPLING_ARM_CAPABILITY["temperature"])
        self.assertEqual(payload["top_k"], run_harness.SAMPLING_ARM_CAPABILITY["top_k"])

    def test_production_arm_with_known_family_fills_table_values(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell(sampling_arm="production", model_family="gemma4"))
        self.assertEqual(battery_fn.calls[0]["sampling_arm"], "production")
        payload = battery_fn.calls[0]["sampling_payload"]
        expected = sampling_table.production_sampling("gemma4")
        self.assertEqual(payload["temperature"], expected["temperature"])
        self.assertEqual(payload["top_p"], expected["top_p"])
        self.assertEqual(payload["top_k"], expected["top_k"])
        self.assertEqual(outcome.detail["sampling_arm_effective"], "production")
        self.assertIsNone(outcome.detail["production_sampling_fallback_reason"])

    def test_production_arm_with_silent_family_falls_back_to_capability(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(
            _handle(), _longcontext_cell(sampling_arm="production", model_family="nemotron_h"),
        )
        self.assertEqual(battery_fn.calls[0]["sampling_arm"], "capability")
        payload = battery_fn.calls[0]["sampling_payload"]
        self.assertEqual(payload["temperature"], run_harness.SAMPLING_ARM_CAPABILITY["temperature"])
        self.assertEqual(outcome.detail["sampling_arm_effective"], "capability")
        self.assertIsNotNone(outcome.detail["production_sampling_fallback_reason"])
        self.assertEqual(outcome.status, "completed")  # the fallback itself is not a failure

    def test_production_arm_with_unset_model_family_falls_back_to_capability(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        cell = _longcontext_cell(sampling_arm="production", model_family=None)
        outcome = runner(_handle(), cell)
        self.assertEqual(outcome.detail["sampling_arm_effective"], "capability")

    def test_invalid_sampling_arm_fails_cleanly(self):
        battery_fn = _fake_battery_fn_factory({})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        outcome = runner(_handle(), _longcontext_cell(sampling_arm="bogus"))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "invalid_sampling_arm")
        self.assertEqual(battery_fn.calls, [])

    def test_battery_exception_is_caught_and_recorded_not_raised(self):
        def raising_battery_fn(*args, **kwargs):
            raise RuntimeError("boom")
        runner = batch.make_longcontext_cell_runner(battery_fn=raising_battery_fn)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "battery_error")
        self.assertFalse(outcome.correctness_failure)

    def test_target_tokens_leaves_headroom_under_context(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        runner(_handle(), _longcontext_cell(context=32768))
        target_tokens = battery_fn.calls[0]["target_tokens"]
        self.assertLess(target_tokens, 32768)
        self.assertEqual(target_tokens, int(32768 * run_harness.NEEDLE_NEAR_CEILING_FRACTION))

    def test_vram_read_back_and_base_url_alias_forwarded(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5555,
        )
        handle = _handle(base_url="http://fake-bridge:9001", vram_before=1234)
        outcome = runner(handle, _longcontext_cell())
        self.assertEqual(battery_fn.calls[0]["base_url"], "http://fake-bridge:9001")
        self.assertEqual(battery_fn.calls[0]["alias"], "lc-alias")
        self.assertEqual(outcome.detail["vram_before_bytes"], 1234)
        self.assertEqual(outcome.detail["vram_after_bytes"], 5555)

    def test_real_defaults_never_touched_when_battery_fn_injected(self):
        # The factory's real defaults (longcontext.run_live_battery, sampling_table.
        # production_sampling, sweep.read_vram_used_bytes) are never exercised by this test --
        # only injected fakes -- proving this suite itself never opens a socket or reads sysfs.
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn, vram_used_bytes_fn=lambda: 0)
        outcome = runner(_handle(), _longcontext_cell())
        self.assertEqual(outcome.status, "completed")


# -------------------------------------------------------------------------------------------
# Prompt-fit budget + tokenize_fn wiring (plan `bakeoff-transport-taxonomy-and-prompt-fit.md`
# WP-2, Change 3) -- `prompt_fit_budget`'s pure arithmetic, plus proof that `_runner` threads
# both `token_budget` and a `tokenize_fn` bound to `handle.base_url` through the same
# `battery_fn` seam that already carries `chars_per_token`. No live network anywhere here:
# `run_harness.tokenize_count` itself is monkeypatched in the one test that calls through the
# closure `_runner` builds.
# -------------------------------------------------------------------------------------------


class TestPromptFitBudget(unittest.TestCase):
    def test_budget_is_context_minus_largest_response_cap_minus_margin(self):
        # max_tokens (256) < LONGCONTEXT_MAX_TOKENS_BY_TASK["multi_needle"] (768) -- the larger
        # of the two wins (one conservative scalar budget, brief-pinned).
        budget = batch.prompt_fit_budget(context=32768, max_tokens=256)
        self.assertEqual(budget, 32768 - 768 - batch.TEMPLATE_MARGIN)

    def test_budget_uses_flat_max_tokens_when_it_exceeds_every_task_override(self):
        budget = batch.prompt_fit_budget(context=8192, max_tokens=2000)
        self.assertEqual(budget, 8192 - 2000 - batch.TEMPLATE_MARGIN)

    def test_budget_scales_with_context(self):
        small = batch.prompt_fit_budget(context=8192, max_tokens=256)
        large = batch.prompt_fit_budget(context=32768, max_tokens=256)
        self.assertLess(small, large)
        self.assertEqual(large - small, 32768 - 8192)


class TestLongcontextCellRunnerPromptFitWiring(unittest.TestCase):
    def test_token_budget_and_tokenize_fn_reach_battery_fn(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn, max_tokens=256)
        runner(_handle(), _longcontext_cell(context=32768))
        call = battery_fn.calls[0]
        self.assertEqual(call["token_budget"], 32768 - 768 - batch.TEMPLATE_MARGIN)
        self.assertTrue(callable(call["tokenize_fn"]))

    def test_tokenize_fn_closure_calls_run_harness_tokenize_count_against_handle_base_url(self):
        battery_fn = _fake_battery_fn_factory({"single_needle": [_sample("single_needle", "correct", True)]})
        runner = batch.make_longcontext_cell_runner(battery_fn=battery_fn)
        handle = _handle(base_url="http://fake-bridge:9001")
        runner(handle, _longcontext_cell())
        tokenize_fn = battery_fn.calls[0]["tokenize_fn"]

        captured = {}
        orig = run_harness.tokenize_count

        def fake_tokenize_count(base_url, text):
            captured["base_url"] = base_url
            captured["text"] = text
            return 42

        run_harness.tokenize_count = fake_tokenize_count
        try:
            result = tokenize_fn("some haystack text")
        finally:
            run_harness.tokenize_count = orig
        self.assertEqual(result, 42)
        self.assertEqual(captured["base_url"], "http://fake-bridge:9001")
        self.assertEqual(captured["text"], "some haystack text")


# -------------------------------------------------------------------------------------------
# run_queue end-to-end (fakes throughout -- no Docker, no sysfs, no network)
# -------------------------------------------------------------------------------------------


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


# -------------------------------------------------------------------------------------------
# Budget arithmetic (audit B4) -- threaded through run_queue.
# -------------------------------------------------------------------------------------------


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


# -------------------------------------------------------------------------------------------
# Control-plane WP-2: --stop-marker, --deadline-minutes, paused_reason
# -------------------------------------------------------------------------------------------


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
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue())
            marker_path = Path(tmp) / "stop.marker"
            captured = {}

            def _fake_run_queue(*args, **kwargs):
                captured.update(kwargs)
                return {"noop": True, "queue_state": batch.QUEUE_COMPLETED}

            buf = io.StringIO()
            with mock.patch("batch.run_queue", side_effect=_fake_run_queue):
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


# -------------------------------------------------------------------------------------------
# CLI seams
# -------------------------------------------------------------------------------------------


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


class TestWriteCellRecord(unittest.TestCase):
    """WP-5 glue: `write_cell_record` persists one live cell's battery output as the
    `verify_run.py`-consumable run-directory shape (`records_dir/<cell_id>/`, four-file
    `write_experiment_record` layout, experiment_id == cell_id)."""

    def _battery_results(self, n=2, summary=None):
        samples = [_sample("single_needle", "correct", True) for _ in range(n)]
        completions = [{"type": "longcontext_probe", "trial_index": i} for i in range(n)]
        return {"single_needle": {0.45: longcontext.CellResult(
            samples=samples, completions=completions, summary=summary,
        )}}

    def test_writes_verify_run_consumable_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_gguf = Path(tmp) / "fake-model.gguf"
            fake_gguf.write_bytes(b"x" * 128)
            cell = _longcontext_cell(model_host_path=str(fake_gguf))
            records_dir = Path(tmp) / "run-wp5"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            self.assertEqual(out, records_dir / cell.cell_id)
            config = json.loads((out / "config.json").read_text())
            for key in run_harness.REQUIRED_LINEAGE_FIELDS:
                self.assertIn(key, config["lineage"])
            self.assertEqual(config["lineage"]["filename"], "fake-model.gguf")
            self.assertEqual(config["lineage"]["file_size"], 128)
            self.assertEqual(config["sweep_cell"]["cell_id"], cell.cell_id)
            self.assertEqual(config["vram"]["baseline_bytes"], 1000)
            self.assertEqual(config["vram"]["loaded_bytes"], 9000)
            self.assertIsNone(config["wilson_summary"])
            samples = (out / "samples.jsonl").read_text().strip().splitlines()
            self.assertEqual(len(samples), 2)

    def test_runner_writes_record_when_records_dir_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-x",
            )
            outcome = runner(_handle(), _longcontext_cell())
            self.assertEqual(outcome.status, "completed")
            self.assertIsNotNone(outcome.detail["record_path"])
            self.assertIsNone(outcome.detail["record_error"])
            self.assertTrue((Path(outcome.detail["record_path"]) / "config.json").exists())

    def test_config_carries_vram_profile_and_response_caps_when_supplied(self):
        # WP-B item 2: record `config.json` gains top-level `vram_profile` + `response_caps`.
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell()
            records_dir = Path(tmp) / "run-wp-b"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
                vram_profile="desktop-shared",
                response_caps={"default": 256, "by_task": {"multi_needle": 768}},
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["vram_profile"], "desktop-shared")
            self.assertEqual(
                config["response_caps"], {"default": 256, "by_task": {"multi_needle": 768}},
            )

    def test_config_vram_profile_and_response_caps_default_to_none(self):
        # Additive/backward-compatible: a caller (this WP's own earlier tests above) that omits
        # the two new kwargs still gets a valid record, with the keys present but null.
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell()
            records_dir = Path(tmp) / "run-wp-b-default"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIsNone(config["vram_profile"])
            self.assertIsNone(config["response_caps"])

    def test_needle_runner_threads_vram_profile_and_response_caps_into_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-wp-b-runner",
                vram_profile="headless",
            )
            outcome = runner(_handle(), _longcontext_cell())
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["vram_profile"], "headless")
            self.assertEqual(
                config["response_caps"],
                {"default": 256, "by_task": batch.LONGCONTEXT_MAX_TOKENS_BY_TASK},
            )

    def test_deferred_vram_cell_record_also_carries_vram_profile_and_response_caps(self):
        # The non-needle deferral path (default_cell_runner + augmented VRAM read-backs) writes
        # through the same `write_cell_record` seam -- it must gain the two fields too.
        with tempfile.TemporaryDirectory() as tmp:
            runner = batch.make_longcontext_cell_runner(
                battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 4321,
                records_dir=Path(tmp) / "run-wp-b-vram-only",
                vram_profile="desktop-shared",
            )
            cell = _longcontext_cell(battery=["vram"])
            outcome = runner(_handle(vram_before=1000), cell)
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["vram_profile"], "desktop-shared")
            self.assertEqual(
                config["response_caps"],
                {"default": 256, "by_task": batch.LONGCONTEXT_MAX_TOKENS_BY_TASK},
            )

    def test_runner_record_write_failure_never_aborts_the_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            unwritable = Path(tmp) / "not-a-dir.txt"
            unwritable.write_text("occupied")
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=unwritable,  # a file where a directory is needed -> write fails
            )
            outcome = runner(_handle(), _longcontext_cell())
            self.assertEqual(outcome.status, "completed")  # persistence failure is not a cell failure
            self.assertIsNone(outcome.detail["record_path"])
            self.assertIsNotNone(outcome.detail["record_error"])

    # -- lineage fill (local-capture WP-1): hf_repo derived from model_host_path, ---------------
    # -- llama_cpp_build resolved live, past-record None values never rewritten -----------------

    def test_record_lineage_derives_hf_repo_and_llama_cpp_build(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_host_path=(
                "/srv/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf"
            ))
            records_dir = Path(tmp) / "run-lineage-fill"
            with mock.patch.object(
                run_harness, "resolve_llama_cpp_build", return_value="b1234-fixed",
            ):
                out = batch.write_cell_record(
                    records_dir, cell, self._battery_results(),
                    vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                    sampling_arm_effective="capability",
                )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["lineage"]["hf_repo"], "unsloth/Qwen3.8-27B-GGUF")
            self.assertEqual(config["lineage"]["llama_cpp_build"], "b1234-fixed")
            self.assertIsNone(config["lineage"]["hf_revision"])
            self.assertIsNone(config["lineage"]["sha256"])

    def test_record_lineage_hf_repo_none_when_host_path_outside_shared_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(
                model_host_path="/opt/models/unsloth/Qwen3.8-27B-GGUF/model.gguf",
            )
            records_dir = Path(tmp) / "run-lineage-outside"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIsNone(config["lineage"]["hf_repo"])
            # llama_cpp_build is resolved regardless of host_path shape -- it is not path-derived.
            self.assertIsNotNone(config["lineage"]["llama_cpp_build"])

    def test_record_lineage_hf_repo_none_when_host_path_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_host_path=None)
            records_dir = Path(tmp) / "run-lineage-absent"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIsNone(config["lineage"]["hf_repo"])


class TestHfRepoFromHostPath(unittest.TestCase):
    """Unit coverage for `batch.hf_repo_from_host_path` in isolation from `write_cell_record` --
    the local-capture WP-1 lineage-fill helper (`"<org>/<repo>"` from a `model_host_path` under
    `/srv/models`, else `None`)."""

    def test_matches_org_repo_under_shared_models_prefix(self):
        host_path = "/srv/models/unsloth/Qwen3.8-27B-GGUF/x.gguf"
        self.assertEqual(
            batch.hf_repo_from_host_path(host_path), "unsloth/Qwen3.8-27B-GGUF",
        )

    def test_path_outside_shared_models_prefix_returns_none(self):
        self.assertIsNone(
            batch.hf_repo_from_host_path("/home/other_models/unsloth/repo/x.gguf"),
        )

    def test_none_input_returns_none(self):
        self.assertIsNone(batch.hf_repo_from_host_path(None))

    def test_path_with_only_two_segments_after_prefix_returns_none(self):
        # No repo directory -- just an org segment then a file, one level short of the
        # required org/repo/file shape.
        self.assertIsNone(
            batch.hf_repo_from_host_path("/srv/models/unsloth/x.gguf"),
        )


class TestWp5CliArgs(unittest.TestCase):
    def test_scratch_container_and_records_dir_default_to_none(self):
        parser = batch.build_arg_parser()
        args = parser.parse_args(["run", "--queue", "q.json"])
        self.assertIsNone(args.scratch_container)
        self.assertIsNone(args.records_dir)


class TestWp6CharsPerToken(unittest.TestCase):
    """WP-6: per-model measured `chars_per_token` (WP-5 haystack-calibration finding)."""

    def test_validate_queue_rejects_non_positive_or_non_numeric(self):
        for bad in (0, -1.5, "4.2", True):
            queue = _minimal_queue()
            queue["models"]["model-a"]["chars_per_token"] = bad
            with self.assertRaises(batch.QueueValidationError):
                batch.validate_queue(queue)

    def test_validate_queue_accepts_positive_float_and_absence(self):
        queue = _minimal_queue()
        batch.validate_queue(queue)  # absent everywhere: fine
        queue["models"]["model-a"]["chars_per_token"] = 4.24
        batch.validate_queue(queue)  # present and positive: fine

    def test_resolved_cell_carries_ratio_without_changing_cell_id(self):
        plain = batch.resolve_queue_cells(_minimal_queue())
        queue = _minimal_queue()
        queue["models"]["model-a"]["chars_per_token"] = 4.24
        with_ratio = batch.resolve_queue_cells(queue)
        for before, after in zip(plain, with_ratio):
            # The knob is sizing metadata, never cell identity: same cell_id either way -- and
            # structurally so now, since it does not live on `CellIdentity` at all.
            self.assertEqual(before.cell_id, after.cell_id)
        for cell in with_ratio:
            self.assertEqual(cell.queue.chars_per_token, 4.24)  # every cell is model-a's
        for cell in plain:
            self.assertIsNone(cell.queue.chars_per_token)  # absent in the registry -> None

    def test_runner_forwards_ratio_and_task_caps_to_battery_and_detail(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "correct", True)],
        })
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
        )
        cell = _longcontext_cell(chars_per_token=4.06)
        outcome = runner(_handle(), cell)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(battery_fn.calls[0]["chars_per_token"], 4.06)
        self.assertEqual(
            battery_fn.calls[0]["max_tokens_by_task"], batch.LONGCONTEXT_MAX_TOKENS_BY_TASK
        )
        self.assertEqual(outcome.detail["chars_per_token"], 4.06)

    def test_runner_forwards_none_when_queue_carries_no_ratio(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "correct", True)],
        })
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
        )
        outcome = runner(_handle(), _longcontext_cell())  # helper carries no chars_per_token
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(battery_fn.calls[0]["chars_per_token"])
        self.assertIsNone(outcome.detail["chars_per_token"])


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
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
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
                    now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
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
                now_fn=fixed_now_fn(datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)),
                vram_total_bytes_fn=lambda: int(15.92 * sweep.GIB),
                vram_used_bytes_fn=lambda: int(3.1 * sweep.GIB),
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_COMPLETED)
            for cs in result["state"]["cells"].values():
                self.assertEqual(cs["state"], batch.COMPLETED)


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


# -------------------------------------------------------------------------------------------
# WP-1 of `plans/bakeoff-timing-records-and-estimates.md`: additive per-cell timing in
# `config.json`, `sweep_cell.model`, and the previously-declared-but-missing `"timing"` battery
# item. No live scratch server anywhere below -- `timing_round_fn` is always a fake.
# -------------------------------------------------------------------------------------------


def _fake_timing_round_fn(fail_at=None, raise_at=None):
    """Stand-in for `run_harness.run_round`, returning its exact `(sample, completion)` shape
    (the pre-v2 `timing_round` field set the dashboard already parses). Records its calls."""

    def _fn(base_url, alias, run_state, round_index, prompt, max_tokens, timeout=180):
        _fn.calls.append({
            "base_url": base_url, "alias": alias, "run_state": run_state,
            "round_index": round_index, "prompt": prompt, "max_tokens": max_tokens,
            "timeout": timeout,
        })
        if raise_at is not None and round_index == raise_at:
            raise RuntimeError("boom")
        success = not (fail_at is not None and round_index == fail_at)
        sample = {
            "type": "timing_round",
            "completion_ref": f"{run_state}-{round_index}",
            "round_index": round_index,
            "run_state": run_state,
            "timestamp": "2026-08-25T00:00:00+00:00",
            "success": success,
            "error": None if success else "URLError: refused",
            "prompt_tokens": 20,
            "cached_tokens": 0 if run_state == "cold" else 19,
            "completion_tokens": 64,
            "total_tokens": 84,
            "cache_hit_ratio": 0.0 if run_state == "cold" else 0.95,
            "cache_assertion_passed": None if run_state == "cold" else True,
            "first_generation_token_ms": 120.0,
            "first_visible_content_ms": 120.0,
            "first_request_e2e_ttft_ms": 120.0,
            "warm_ttft_ms": None,
            "hot_ttft_ms": 120.0 if run_state == "hot" else None,
            "model_load_seconds": None,
            "decode_wall_ms": 1000.0,
            "observed_decode_tok_s": 64.0,
            "server_prompt_eval_count": 20,
            "server_prompt_eval_time_ms": 12.0,
            "server_prompt_eval_tok_s": 1666.0,
            "server_eval_count": 64,
            "server_eval_time_ms": 1000.0,
            "server_eval_tok_s": 64.0,
            "finish_reason": "length",
            "wall_seconds": 1.2,
        }
        completion = {
            "type": "timing_round", "completion_ref": f"{run_state}-{round_index}",
            "round_index": round_index, "run_state": run_state,
            "prompt_char_len": len(prompt), "content_text": "text",
            "reasoning_text": "", "finish_reason": "length",
        }
        return sample, completion

    _fn.calls = []
    return _fn


class TestBtreRecordTimingFields(unittest.TestCase):
    """Additive `config.json` keys: `cell_started_at`/`cell_ended_at`/`cell_wall_seconds`/
    `scratch_startup_seconds`, plus `sweep_cell.model`."""

    def _battery_results(self):
        return {"single_needle": {0.45: longcontext.CellResult(
            samples=[_sample("single_needle", "correct", True)],
            completions=[{"type": "longcontext_probe", "trial_index": 0}],
            summary=None,
        )}}

    def test_direct_call_without_the_new_kwargs_still_parses_with_explicit_nulls(self):
        # Back-compat: the pre-WP call shape (positional + the WP-B kwargs) keeps working, and
        # every new key is present-and-null rather than absent-and-ambiguous.
        with tempfile.TemporaryDirectory() as tmp:
            out = batch.write_cell_record(
                Path(tmp) / "run", _longcontext_cell(), self._battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["schema_version"], 2)  # additive only -- never a bump
            for key in ("cell_started_at", "cell_ended_at", "cell_wall_seconds",
                        "scratch_startup_seconds"):
                self.assertIn(key, config)
                self.assertIsNone(config[key])
            self.assertIn("model", config["sweep_cell"])
            self.assertIsNone(config["sweep_cell"]["model"])

    def test_new_kwargs_round_trip_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_id="gemma-4-12b-qat")
            out = batch.write_cell_record(
                Path(tmp) / "run", cell, self._battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
                cell_started_at="2026-08-25T12:00:00+00:00",
                cell_ended_at="2026-08-25T12:34:56+00:00",
                cell_wall_seconds=2096.0,
                scratch_startup_seconds=31.5,
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["cell_started_at"], "2026-08-25T12:00:00+00:00")
            self.assertEqual(config["cell_ended_at"], "2026-08-25T12:34:56+00:00")
            self.assertEqual(config["cell_wall_seconds"], 2096.0)
            self.assertEqual(config["scratch_startup_seconds"], 31.5)
            self.assertEqual(config["sweep_cell"]["model"], "gemma-4-12b-qat")
            # The reserved name stays reserved: the startup measurement must never be recorded
            # under `model_load_seconds` (README honest-timing-semantics contract).
            self.assertNotIn("model_load_seconds", config)

    def test_queue_model_id_is_the_single_source_of_sweep_cell_model(self):
        # WP-R1: pre-refactor a cell dict carried the queue model key under BOTH `model` and
        # `_queue_model_id`, and `write_cell_record` read one with the other as a fallback.
        # There is now exactly one spelling -- `QueueCellMetadata.model_id` -- so the dual-key
        # fallback has no way to disagree with itself. The recorded value is unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_id="model-a")
            out = batch.write_cell_record(
                Path(tmp) / "run", cell, self._battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["sweep_cell"]["model"], "model-a")

    def test_resolved_cells_carry_the_queue_model_key_without_changing_cell_id(self):
        plain = batch.resolve_queue_cells(_minimal_queue())
        for cell in plain:
            self.assertEqual(cell.queue.model_id, "model-a")
            self.assertEqual(cell.record_metadata().model_id, "model-a")
        # Identity is untouched by the queue model key -- structurally, since `model_id` lives
        # on `QueueCellMetadata` and `compute_cell_id` only ever sees `CellIdentity`.
        recomputed = [sweep.compute_cell_id(c.as_sweep_cell()) for c in plain]
        self.assertEqual(recomputed, [c.cell_id for c in plain])
        self.assertNotIn("model", plain[0].as_sweep_cell())

    def test_runner_stamps_the_handles_clock_into_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-clock",
            )
            handle = _handle()
            handle.started_at = "2026-08-25T09:00:00+00:00"
            handle.scratch_startup_seconds = 12.75
            cell = _longcontext_cell(model_id="model-a")
            outcome = runner(handle, cell)
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["cell_started_at"], "2026-08-25T09:00:00+00:00")
            self.assertEqual(config["scratch_startup_seconds"], 12.75)
            self.assertEqual(config["sweep_cell"]["model"], "model-a")
            self.assertIsInstance(config["cell_wall_seconds"], float)
            self.assertGreaterEqual(config["cell_wall_seconds"], 0.0)
            self.assertIsInstance(config["cell_ended_at"], str)

    def test_startup_only_cell_record_also_carries_the_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = batch.make_longcontext_cell_runner(
                battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 4321,
                records_dir=Path(tmp) / "run-vram-clock",
            )
            handle = _handle()
            handle.scratch_startup_seconds = 8.5
            outcome = runner(handle, _longcontext_cell(battery=["vram"]))
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["scratch_startup_seconds"], 8.5)
            self.assertIsInstance(config["cell_wall_seconds"], float)


class TestBtreTimingBatteryItem(unittest.TestCase):
    """The `"timing"` battery item: 3 rounds by default, `timing_rounds` override, timing-first
    ordering, `timing_round` rows appended to the SAME `samples.jsonl`."""

    def test_validate_queue_rejects_a_malformed_timing_rounds_override(self):
        for bad in (0, -2, 2.5, "3", True):
            queue = _minimal_queue()
            queue["cells"][0]["timing_rounds"] = bad
            with self.assertRaises(batch.QueueValidationError):
                batch.validate_queue(queue)

    def test_validate_queue_accepts_a_positive_int_or_absence(self):
        queue = _minimal_queue()
        batch.validate_queue(queue)
        queue["cells"][0]["timing_rounds"] = 5
        batch.validate_queue(queue)

    def test_resolved_cell_carries_the_override_without_changing_cell_id(self):
        plain = batch.resolve_queue_cells(_minimal_queue())
        queue = _minimal_queue()
        queue["cells"][0]["timing_rounds"] = 5
        with_override = batch.resolve_queue_cells(queue)
        for before, after in zip(plain, with_override):
            self.assertEqual(before.cell_id, after.cell_id)
        self.assertEqual(with_override[0].queue.timing_rounds, 5)
        self.assertIsNone(plain[0].queue.timing_rounds)

    def test_rounds_default_to_three_and_honor_the_override(self):
        self.assertEqual(batch.TIMING_DEFAULT_ROUNDS, 3)
        # WP-R1: the override arrives as a typed scalar (`QueueCellMetadata.timing_rounds`)
        # rather than being re-indexed out of a cell dict, so the fallback rule is a pure
        # function of that one value.
        self.assertEqual(batch.resolve_timing_rounds(None), 3)
        self.assertEqual(batch.resolve_timing_rounds(7), 7)
        # A malformed value never raises out of the timing arm -- it falls back.
        for bad in (0, -2, "5", True, 2.5):
            self.assertEqual(batch.resolve_timing_rounds(bad), 3)
        # ...and it reaches the arm off the resolved cell.
        self.assertEqual(
            batch.resolve_timing_rounds(
                _longcontext_cell(timing_rounds=5).queue.timing_rounds,
            ), 5,
        )

    def test_round_schedule_is_cold_then_hot_with_indices_from_zero(self):
        round_fn = _fake_timing_round_fn()
        result = batch.run_timing_rounds(
            _handle(), _longcontext_cell(battery=["timing"]), round_fn=round_fn,
        )
        self.assertEqual([c["round_index"] for c in round_fn.calls], [0, 1, 2])
        self.assertEqual([c["run_state"] for c in round_fn.calls], ["cold", "hot", "hot"])
        self.assertEqual({c["alias"] for c in round_fn.calls}, {"lc-alias"})
        self.assertEqual({c["max_tokens"] for c in round_fn.calls}, {batch.TIMING_MAX_TOKENS})
        self.assertEqual(result.detail["rounds_recorded"], 3)
        self.assertEqual(result.detail["rounds_succeeded"], 3)
        self.assertIsNone(result.detail["error"])

    def test_a_raising_round_keeps_partial_rows_and_never_propagates(self):
        round_fn = _fake_timing_round_fn(raise_at=1)
        result = batch.run_timing_rounds(
            _handle(), _longcontext_cell(battery=["timing"]), round_fn=round_fn,
        )
        self.assertEqual(len(result.samples), 1)
        self.assertIn("RuntimeError", result.detail["error"])
        self.assertEqual(result.detail["rounds_recorded"], 1)

    def test_needle_plus_timing_appends_timing_rows_to_the_same_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            round_fn = _fake_timing_round_fn()
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-timing", timing_round_fn=round_fn,
            )
            outcome = runner(_handle(), _longcontext_cell(battery=["timing", "needle"]))
            self.assertEqual(outcome.status, "completed")
            self.assertEqual(outcome.detail["timing"]["rounds_recorded"], 3)
            rows = [
                json.loads(line)
                for line in (Path(outcome.detail["record_path"]) / "samples.jsonl")
                .read_text().strip().splitlines()
            ]
            # This suite's `_sample()` fixture is a minimal needle-probe stand-in with no `type`
            # key, so the battery row is identified by its absence -- what matters here is that
            # the timing rows land in the same file, after the battery's own.
            kinds = [r.get("type") for r in rows]
            self.assertEqual(kinds.count("timing_round"), 3)
            self.assertEqual(len(rows), 4)
            # Battery rows first, timing rows appended after -- a stable, documented order.
            self.assertNotEqual(kinds[0], "timing_round")
            timing_rows = [r for r in rows if r.get("type") == "timing_round"]
            # Exactly the pre-v2 field set `packages/shared/src/metric.ts` parses.
            for key in ("run_state", "round_index", "first_generation_token_ms",
                        "observed_decode_tok_s", "prompt_tokens", "cached_tokens",
                        "cache_hit_ratio", "cache_assertion_passed", "model_load_seconds",
                        "decode_wall_ms", "server_prompt_eval_tok_s", "server_eval_tok_s",
                        "completion_tokens"):
                self.assertIn(key, timing_rows[0])
            # Honest-timing contract: no observed residency transition here, so null -- never a
            # constant subtracted off another field.
            self.assertTrue(all(r["model_load_seconds"] is None for r in timing_rows))
            completions = [
                json.loads(line)
                for line in (Path(outcome.detail["record_path"]) / "completions.jsonl")
                .read_text().strip().splitlines()
            ]
            self.assertEqual(
                sum(1 for c in completions if c.get("type") == "timing_round"), 3
            )

    def test_timing_runs_before_the_needle_battery(self):
        order = []
        round_fn = _fake_timing_round_fn()

        def recording_round_fn(*args, **kwargs):
            order.append("timing")
            return round_fn(*args, **kwargs)

        inner_battery_fn = _fake_battery_fn_factory(
            {"single_needle": [_sample("single_needle", "correct", True)]},
        )

        def recording_battery_fn(*args, **kwargs):
            order.append("needle")
            return inner_battery_fn(*args, **kwargs)

        runner = batch.make_longcontext_cell_runner(
            battery_fn=recording_battery_fn, vram_used_bytes_fn=lambda: 5000,
            timing_round_fn=recording_round_fn,
        )
        runner(_handle(), _longcontext_cell(battery=["timing", "needle"]))
        self.assertEqual(order, ["timing", "timing", "timing", "needle"])

    def test_timing_only_cell_records_rows_via_the_startup_only_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            round_fn = _fake_timing_round_fn()
            runner = batch.make_longcontext_cell_runner(
                battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 4321,
                records_dir=Path(tmp) / "run-timing-only", timing_round_fn=round_fn,
            )
            outcome = runner(_handle(), _longcontext_cell(battery=["timing"]))
            self.assertEqual(outcome.status, "completed")
            rows = [
                json.loads(line)
                for line in (Path(outcome.detail["record_path"]) / "samples.jsonl")
                .read_text().strip().splitlines()
            ]
            self.assertEqual([r["type"] for r in rows], ["timing_round"] * 3)

    def test_needle_only_cell_is_untouched_by_the_timing_arm(self):
        round_fn = _fake_timing_round_fn()
        battery_fn = _fake_battery_fn_factory(
            {"single_needle": [_sample("single_needle", "correct", True)]},
        )
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
            timing_round_fn=round_fn,
        )
        outcome = runner(_handle(), _longcontext_cell(battery=["vram", "needle"]))
        self.assertEqual(round_fn.calls, [])  # never invoked
        self.assertNotIn("timing", outcome.detail)  # detail shape byte-identical to pre-WP
        self.assertEqual(outcome.status, "completed")

    def test_a_failed_timing_round_never_changes_the_cell_disposition(self):
        round_fn = _fake_timing_round_fn(fail_at=0)
        battery_fn = _fake_battery_fn_factory(
            {"single_needle": [_sample("single_needle", "correct", True)]},
        )
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
            timing_round_fn=round_fn,
        )
        outcome = runner(_handle(), _longcontext_cell(battery=["timing", "needle"]))
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(outcome.reason)
        self.assertFalse(outcome.correctness_failure)
        self.assertEqual(outcome.detail["timing"]["rounds_succeeded"], 2)

    def test_unhealthy_handle_never_reaches_the_timing_arm(self):
        round_fn = _fake_timing_round_fn()
        runner = batch.make_longcontext_cell_runner(
            battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 1,
            timing_round_fn=round_fn,
        )
        outcome = runner(_handle(healthy=False), _longcontext_cell(battery=["timing"]))
        self.assertEqual(outcome.reason, "health_timeout")
        self.assertEqual(round_fn.calls, [])


class TestWp2SpecDecode(unittest.TestCase):
    """WP-2 (plans/overnight-test-matrix-redesign.md): the base-vs-MTP axis, the additive
    optional abbreviated-cell key `spec_decode: "base" | "mtp"`."""

    def _mtp_queue(self, **cell_overrides):
        queue = _minimal_queue()
        queue["models"]["model-mtp"] = {
            "model_container_path": "/models/mtp/model-mtp.gguf",
            "server_flags": {
                "-ngl": 99,
                "-md": "/models/mtp/model-mtp-draft.gguf",
                "--spec-type": "draft-mtp",
            },
        }
        cell = {
            "model": "model-mtp", "alias": "model-mtp-c32k-f16", "c": 32768,
            "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
            "sampling_arm": "capability", "trials": 3,
        }
        cell.update(cell_overrides)
        queue["cells"] = [cell]
        return queue

    # -- resolve_cell: base strips, mtp asserts/passes, absent is a no-op ------------------

    def test_base_strips_md_and_spec_type_from_a_registry_entry_carrying_both(self):
        resolved = batch.resolve_queue_cells(self._mtp_queue(spec_decode="base"))[0]
        self.assertNotIn("-md", resolved.server_flags)
        self.assertNotIn("--spec-type", resolved.server_flags)
        self.assertEqual(resolved.queue.spec_decode, "base")

    def test_mtp_passes_through_untouched_when_registry_carries_spec_type(self):
        resolved = batch.resolve_queue_cells(self._mtp_queue(spec_decode="mtp"))[0]
        self.assertEqual(resolved.server_flags["--spec-type"], "draft-mtp")
        self.assertEqual(resolved.server_flags["-md"], "/models/mtp/model-mtp-draft.gguf")
        self.assertEqual(resolved.queue.spec_decode, "mtp")

    def test_mtp_raises_when_registry_entry_carries_no_spec_type(self):
        queue = self._mtp_queue(spec_decode="mtp")
        queue["models"]["model-mtp"]["server_flags"] = {"-ngl": 99}  # no MTP flags at all
        with self.assertRaises(batch.QueueValidationError):
            batch.resolve_queue_cells(queue)

    def test_absent_key_is_a_no_op_byte_identical_to_recomputed_cell_id(self):
        resolved = batch.resolve_queue_cells(self._mtp_queue())[0]  # no spec_decode key
        self.assertIsNone(resolved.queue.spec_decode)
        # The registry's MTP flags survive untouched -- absence means "as-is", never a strip.
        self.assertEqual(resolved.server_flags["-md"], "/models/mtp/model-mtp-draft.gguf")
        self.assertEqual(resolved.server_flags["--spec-type"], "draft-mtp")
        # Same recompute-and-compare shape as the `model`/`chars_per_token` precedents: identity
        # is exactly what `sweep.compute_cell_id` derives from the four identity fields alone.
        self.assertEqual(sweep.compute_cell_id(resolved.as_sweep_cell()), resolved.cell_id)
        self.assertEqual(
            sweep.compute_cell_id(resolved.identity.identity_payload()), resolved.cell_id,
        )

    def test_base_and_mtp_arms_of_the_same_cell_produce_distinct_cell_ids(self):
        base_cell = batch.resolve_queue_cells(self._mtp_queue(spec_decode="base"))[0]
        mtp_cell = batch.resolve_queue_cells(self._mtp_queue(spec_decode="mtp"))[0]
        self.assertNotEqual(base_cell.cell_id, mtp_cell.cell_id)

    # -- validate_queue: malformed values only ----------------------------------------------

    def test_validate_queue_rejects_malformed_values(self):
        for bad in ("draft", 1, None):
            queue = self._mtp_queue()
            queue["cells"][0]["spec_decode"] = bad  # present (even as explicit null) but bad
            with self.assertRaises(batch.QueueValidationError):
                batch.validate_queue(queue)

    def test_validate_queue_accepts_base_mtp_and_absence(self):
        batch.validate_queue(self._mtp_queue())  # absent entirely: fine
        batch.validate_queue(self._mtp_queue(spec_decode="base"))
        batch.validate_queue(self._mtp_queue(spec_decode="mtp"))

    # -- write_cell_record: sweep_cell.spec_decode round-trips, nullable --------------------

    def _one_task_battery_results(self):
        return {"single_needle": {0.45: longcontext.CellResult(
            samples=[_sample("single_needle", "correct", True)], completions=[], summary=None,
        )}}

    def test_record_round_trip_persists_spec_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("base", "mtp", None):
                cell = _longcontext_cell(spec_decode=value)
                out = batch.write_cell_record(
                    Path(tmp) / f"run-{value}", cell, self._one_task_battery_results(),
                    vram_baseline_bytes=1, vram_loaded_bytes=2,
                    sampling_arm_effective="capability",
                )
                config = json.loads((out / "config.json").read_text())
                self.assertEqual(config["sweep_cell"]["spec_decode"], value)

    def test_record_defaults_to_none_when_cell_never_declared_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = batch.write_cell_record(
                Path(tmp) / "run", _longcontext_cell(), self._one_task_battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIn("spec_decode", config["sweep_cell"])  # key present, not just absent
            self.assertIsNone(config["sweep_cell"]["spec_decode"])


if __name__ == "__main__":
    unittest.main()
