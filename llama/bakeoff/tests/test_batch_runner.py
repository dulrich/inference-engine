"""Tests owned by the cell-runner/hang-rail seam (`cell_runner.py`, `hang_rail.py`): hang-
signature detection, the default and longcontext cell runners, prompt-fit budget wiring, the
BTRE timing-battery item, and the WP-1 `request_timeout_s` runner-threading contract. Split from
the former `test_batch.py` omnibus module (WP-R3 of
plans/queue-contract-and-test-decomposition-remediation.md); no test logic changed in the move.

No live Docker/GPU execution anywhere in this suite -- fixtures imported from `batch_fixtures`
are always fakes; `_fake_timing_round_fn` below is local because both its consumers live in this
module."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from batch_fixtures import (  # noqa: E402
    _fake_battery_fn_factory,
    _handle,
    _longcontext_cell,
    _minimal_queue,
    _sample,
    _write_queue,
)

import batch  # noqa: E402
import run_harness  # noqa: E402
import sampling_table  # noqa: E402
import sweep  # noqa: E402

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


class TestWp1RequestTimeoutSRunnerThreading(unittest.TestCase):
    """The transport timeout is a per-recv socket timeout (`harness_transport.py:58-64`) with no
    override anywhere except `make_longcontext_cell_runner`'s own `timeout` parameter, which
    reaches both the needle battery (`cell_runner.py:376-388`) and the timing rounds
    (`cell_runner.py:439`)."""

    def test_needle_battery_and_timing_rounds_both_receive_the_configured_timeout(self):
        battery_fn = _fake_battery_fn_factory(
            {"single_needle": [_sample("single_needle", "correct", True)]},
        )
        round_fn = _fake_timing_round_fn()
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, timing_round_fn=round_fn, vram_used_bytes_fn=lambda: 5000,
            timeout=1800,
        )
        outcome = runner(_handle(), _longcontext_cell(battery=["timing", "needle"]))
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(battery_fn.calls[0]["timeout"], 1800)
        self.assertTrue(round_fn.calls)
        self.assertTrue(all(c["timeout"] == 1800 for c in round_fn.calls))

    def test_cli_dry_run_threads_the_queues_own_effective_timeout(self):
        # Worker discretion (brief): additionally patch batch.make_longcontext_cell_runner and
        # drive a --dry-run over a temp queue carrying request_timeout_s to assert the CLI
        # threads it through the SAME load_queue effective_vram_profile already uses.
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = _write_queue(tmp, _minimal_queue(request_timeout_s=1800))
            captured = {}
            real_factory = batch.make_longcontext_cell_runner

            def spy_factory(*args, **kwargs):
                captured["timeout"] = kwargs.get("timeout")
                return real_factory(*args, **kwargs)

            with mock.patch.object(batch, "make_longcontext_cell_runner", spy_factory):
                code = batch.main(["run", "--queue", str(queue_path), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertEqual(captured["timeout"], 1800)
