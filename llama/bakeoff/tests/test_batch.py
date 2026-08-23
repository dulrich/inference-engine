"""Queue-driven batch runner (plan longcontext-pow2-overnight-campaign.md WP-1). No live
Docker/GPU execution anywhere in this suite -- `scratch_server_fn`/`container_controller`/
`hang_check_fn`/`vram_*_bytes_fn`/`now_fn` are always fakes; `run_queue` never falls through to
its real (Docker/sysfs-touching) defaults in any test here."""

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
        return outcomes_by_cell_id.get(cell["cell_id"], default)
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
        self.assertEqual(first["server_flags"]["-c"], 32768)
        self.assertEqual(first["server_flags"]["-ctk"], "f16")
        self.assertEqual(first["server_flags"]["-ngl"], 99)  # inherited from model-a base flags
        self.assertEqual(first["_queue_model_id"], "model-a")
        self.assertEqual(first["_sampling_arm"], "capability")
        self.assertEqual(first["_trials"], 3)

    def test_gpu_weights_bytes_and_kv_attention_layers_propagate_from_model_registry(self):
        q = _minimal_queue(cells=[{
            "model": "model-b", "alias": "model-b-c8k", "c": 8192, "ctk": "f16", "ctv": "f16",
            "fa": "on", "battery": ["vram"], "sampling_arm": "capability", "trials": 1,
        }])
        resolved = batch.resolve_queue_cells(q)
        self.assertEqual(resolved[0]["gpu_weights_bytes"], 6 * sweep.GIB)
        self.assertEqual(resolved[0]["kv_attention_layers"], 10)
        self.assertEqual(resolved[0]["server_flags"]["-ncmoe"], 32)

    def test_model_family_propagates_from_model_registry_when_present(self):
        q = _minimal_queue()
        q["models"]["model-a"]["model_family"] = "gemma4"
        resolved = batch.resolve_queue_cells(q)
        self.assertEqual(resolved[0]["model_family"], "gemma4")

    def test_model_family_absent_when_registry_omits_it(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())  # model-a carries no model_family
        self.assertNotIn("model_family", resolved[0])

    def test_cell_ids_are_distinct(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        ids = [c["cell_id"] for c in resolved]
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

    def test_apply_interrupted_rule_marks_running_as_failed_interrupted(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        crashed_id = resolved[0]["cell_id"]
        state["cells"][crashed_id]["state"] = batch.RUNNING
        changed = batch.apply_interrupted_rule(state)
        self.assertEqual(changed, [crashed_id])
        self.assertEqual(state["cells"][crashed_id]["state"], batch.FAILED)
        self.assertEqual(state["cells"][crashed_id]["reason"], "interrupted")

    def test_apply_interrupted_rule_is_a_noop_when_nothing_is_running(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        self.assertEqual(batch.apply_interrupted_rule(state), [])

    def test_re_arm_resets_everything_but_completed(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        state = batch.init_state(resolved)
        ids = [c["cell_id"] for c in resolved]
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
        self.by_alias = {c["alias"]: c for c in self.resolved}

    def test_quantized_failure_propagates_to_later_larger_c_same_model_same_kv_type(self):
        failing = self.by_alias["model-a-c32k-q4"]
        changed = batch.propagate_skips(self.resolved, self.state, failing["cell_id"])
        larger_same_type = self.by_alias["model-a-c65k-q4"]["cell_id"]
        self.assertIn(larger_same_type, changed)
        self.assertEqual(self.state["cells"][larger_same_type]["state"], batch.SKIPPED_PROPAGATED)
        self.assertIn(failing["cell_id"], self.state["cells"][larger_same_type]["reason"])

    def test_does_not_propagate_to_f16_cells(self):
        failing = self.by_alias["model-a-c32k-q4"]
        changed = batch.propagate_skips(self.resolved, self.state, failing["cell_id"])
        f16_cell = self.by_alias["model-a-c65k-f16"]["cell_id"]
        self.assertNotIn(f16_cell, changed)
        self.assertEqual(self.state["cells"][f16_cell]["state"], batch.PENDING)

    def test_f16_failure_never_propagates_at_all(self):
        failing = self.by_alias["model-a-c32k-f16"]
        changed = batch.propagate_skips(self.resolved, self.state, failing["cell_id"])
        self.assertEqual(changed, [])

    def test_does_not_propagate_to_smaller_or_equal_context(self):
        failing = self.by_alias["model-a-c65k-q4"]  # the LARGER-c quantized cell fails
        changed = batch.propagate_skips(self.resolved, self.state, failing["cell_id"])
        smaller = self.by_alias["model-a-c32k-q4"]["cell_id"]
        self.assertNotIn(smaller, changed)

    def test_only_touches_pending_cells(self):
        failing = self.by_alias["model-a-c32k-q4"]
        larger_id = self.by_alias["model-a-c65k-q4"]["cell_id"]
        self.state["cells"][larger_id]["state"] = batch.COMPLETED
        changed = batch.propagate_skips(self.resolved, self.state, failing["cell_id"])
        self.assertEqual(changed, [])
        self.assertEqual(self.state["cells"][larger_id]["state"], batch.COMPLETED)


# -------------------------------------------------------------------------------------------
# Pre-enqueue size projection (audit E4)
# -------------------------------------------------------------------------------------------


class TestSizeProjection(unittest.TestCase):
    def test_projection_exceeds_a_representative_real_shaped_record(self):
        cell = {"server_flags": {"-c": 262144}}
        trials = 3
        projected = batch.project_cell_record_size_bytes(cell, trials)

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
        cell = {"server_flags": {"-c": 524288}}
        self.assertGreater(
            batch.project_cell_record_size_bytes(cell, 10),
            batch.project_cell_record_size_bytes(cell, 1),
        )

    def test_projection_is_comparable_against_run_harness_size_limit(self):
        # A single 262144+ cell's own trials are nowhere near the 25 MiB whole-experiment budget
        # at realistic trial counts -- the projection and the real limit are the same unit
        # (bytes) and the comparison in `run_queue` is exactly `projected > SIZE_LIMIT_BYTES`.
        cell = {"server_flags": {"-c": 262144}}
        projected = batch.project_cell_record_size_bytes(cell, trials=3)
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
        outcome = batch.default_cell_runner(self._handle(True, True), {})
        self.assertEqual(outcome.status, "completed")
        self.assertFalse(outcome.correctness_failure)

    def test_unhealthy_fails_health_timeout(self):
        outcome = batch.default_cell_runner(self._handle(False, True), {})
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "health_timeout")
        self.assertFalse(outcome.correctness_failure)

    def test_healthy_but_unverified_fails_invalid_config(self):
        outcome = batch.default_cell_runner(self._handle(True, False), {})
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
                       model_family="gemma4", context=32768):
    return {
        "cell_id": "lc-cell", "alias": "lc-alias", "battery": list(battery),
        "server_flags": {"-c": context}, "_sampling_arm": sampling_arm, "_trials": trials,
        "model_family": model_family,
    }


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
             n_trials=10, max_tokens=256, timeout=300):
        calls.append({
            "base_url": base_url, "alias": alias, "cell_id_prefix": cell_id_prefix,
            "target_tokens": target_tokens, "sampling_arm": sampling_arm,
            "sampling_payload": dict(sampling_payload), "n_trials": n_trials,
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
        cell = _longcontext_cell(sampling_arm="production")
        del cell["model_family"]
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
            crashed_id = resolved[0]["cell_id"]
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

            queue_path, result = self._run(
                queue, tmp, container_controller=controller, hang_check_fn=hang_check_fn,
            )
            self.assertEqual(result["queue_state"], batch.QUEUE_ABORTED_HANG)
            first_cell_id = batch.resolve_queue_cells(queue)[0]["cell_id"]
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
            by_alias = {c["alias"]: c for c in resolved}
            failing_id = by_alias["model-a-c32k-q4"]["cell_id"]
            later_id = by_alias["model-a-c65k-q4"]["cell_id"]
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
            by_alias = {c["alias"]: c for c in resolved}
            failing_id = by_alias["model-a-c32k-q4"]["cell_id"]
            later_id = by_alias["model-a-c65k-q4"]["cell_id"]
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
        with mock.patch.dict(sys.modules, {"verify_run": None}):
            with redirect_stderr(buf):
                code = batch.main(["verify", "--run", "/tmp/does-not-matter"])
        self.assertEqual(code, 2)
        self.assertIn("verify_run.py not found", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
