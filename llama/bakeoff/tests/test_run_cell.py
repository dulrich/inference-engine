"""Cell driver: position sweep + n-trial cell driver emitting schema-v2 samples (plan WP-3).
No live Docker/GPU execution -- `request_fn` is a deterministic offline stub throughout; this
suite never touches the network."""

import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402
import run_harness  # noqa: E402


def _make_correct_request_fn(task):
    """Returns a request_fn that always answers correctly for the given task, by inspecting the
    prompt it's handed (a pure offline stub -- no network)."""

    def request_fn(prompt, max_tokens, sampling):
        if task.name == "single_needle":
            # pull the code back out of the prompt itself (test-only shortcut; the real model
            # never sees this) -- the code is on the "Reference code for this section: X. " line
            marker = "Reference code for this section: "
            start = prompt.index(marker) + len(marker)
            code = prompt[start:prompt.index(".", start)]
            content = code
        elif task.name == "multi_needle":
            import re
            codes = re.findall(r"Checkpoint \d+ marker: ([A-Z0-9-]+)\.", prompt)
            content = ", ".join(codes)
        elif task.name == "passage_qa":
            found = None
            for item in longcontext.PASSAGE_QA_ITEMS:
                if item["passage"] in prompt:
                    found = item["answer_keyword"]
                    break
            content = found or ""
        else:
            content = ""
        return {
            "success": True, "error": None, "content_text": content, "reasoning_text": "ok",
            "finish_reason": "stop", "usage": {"prompt_tokens": 1234}, "wall_seconds": 0.1,
            "t0_iso": "2026-08-23T00:00:00+00:00",
        }

    return request_fn


def _always_wrong_request_fn(prompt, max_tokens, sampling):
    return {
        "success": True, "error": None, "content_text": "nope", "reasoning_text": "",
        "finish_reason": "stop", "usage": {"prompt_tokens": 1234}, "wall_seconds": 0.1,
        "t0_iso": "2026-08-23T00:00:00+00:00",
    }


class TestRunCellPinnedSampling(unittest.TestCase):
    def test_missing_sampling_arm_raises(self):
        task = longcontext.SingleNeedleTask()
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=1)
        with self.assertRaises(ValueError):
            longcontext.run_cell(
                _always_wrong_request_fn, task, "cell-x", 0.45, 2000,
                sampling_arm=None, sampling_payload=payload, n_trials=1,
            )

    def test_unfilled_production_arm_raises(self):
        task = longcontext.SingleNeedleTask()
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_PRODUCTION, seed=1)
        with self.assertRaises(ValueError):
            longcontext.run_cell(
                _always_wrong_request_fn, task, "cell-x", 0.45, 2000,
                sampling_arm="production", sampling_payload=payload, n_trials=1,
            )

    def test_missing_sampling_payload_raises(self):
        task = longcontext.SingleNeedleTask()
        with self.assertRaises(ValueError):
            longcontext.run_cell(
                _always_wrong_request_fn, task, "cell-x", 0.45, 2000,
                sampling_arm="capability", sampling_payload={}, n_trials=1,
            )

    def test_filled_capability_arm_does_not_raise(self):
        task = longcontext.SingleNeedleTask()
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=1)
        result = longcontext.run_cell(
            _always_wrong_request_fn, task, "cell-ok", 0.45, 2000,
            sampling_arm="capability", sampling_payload=payload, n_trials=1,
        )
        self.assertEqual(len(result.samples), 1)


class TestRunCellSchemaV2Fields(unittest.TestCase):
    def setUp(self):
        self.task = longcontext.SingleNeedleTask()
        self.payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=999)

    def test_emitted_samples_carry_required_v2_fields(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-single-needle-p045", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=3,
        )
        self.assertEqual(len(result.samples), 3)
        for sample in result.samples:
            self.assertEqual(sample["schema_version"], 2)
            # v2 since lc-pow2 WP-3 made gutenberg-longcontext-haystack-v2 the default manifest
            # (v1 stays on disk as corpus/manifest-v1.json for reproducing old records).
            self.assertEqual(sample["corpus_manifest_id"], "gutenberg-longcontext-haystack-v2")
            self.assertIn("haystack_sha256", sample)
            self.assertIsNotNone(sample["haystack_sha256"])
            self.assertIn("classifier_verdict", sample)
            self.assertIsNotNone(sample["classifier_verdict"])
            self.assertEqual(sample["haystack_structure"], "coherent")
            self.assertIn("haystack_seed", sample)
            self.assertIn("sampler_seed", sample)
            # separate-seed-fields invariant (WP-1 deferred audit pin, reused here)
            self.assertNotEqual(sample["haystack_seed"], sample["sampler_seed"])

    def test_all_correct_trials_classify_correct(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-all-correct", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=3,
        )
        for sample in result.samples:
            self.assertTrue(sample["correct"])
            self.assertEqual(sample["classifier_verdict"], "correct")

    def test_trial_seeds_distinct_across_trials(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-distinct-seeds", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=5,
        )
        haystack_seeds = [s["haystack_seed"] for s in result.samples]
        self.assertEqual(len(haystack_seeds), len(set(haystack_seeds)))

    def test_summary_null_below_wilson_floor(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-few-trials", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=3,
        )
        self.assertIsNone(result.summary)

    def test_summary_populated_at_wilson_floor_with_explicit_convention(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-ten-trials", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload,
            n_trials=longcontext.MIN_TRIALS_FOR_WILSON, wilson_sided="two",
        )
        self.assertIsNotNone(result.summary)
        self.assertEqual(result.summary["sided"], "two")
        self.assertEqual(result.summary["n"], longcontext.MIN_TRIALS_FOR_WILSON)
        self.assertEqual(result.summary["k"], 0)  # all correct in this stub -> zero failures

    def test_all_wrong_trials_summary_reflects_full_failure(self):
        result = longcontext.run_cell(
            _always_wrong_request_fn, self.task, "cell-all-wrong", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload,
            n_trials=longcontext.MIN_TRIALS_FOR_WILSON,
        )
        self.assertEqual(result.summary["k"], longcontext.MIN_TRIALS_FOR_WILSON)
        for sample in result.samples:
            self.assertNotEqual(sample["classifier_verdict"], "correct")


class TestReasoningChannelFallbackScoring(unittest.TestCase):
    """WP-5 attended-validation finding #7: a reasoning-model chat template (nemotron_h,
    live-measured) can put the ENTIRE answer in the reasoning channel and finish `stop` with an
    empty content channel. run_cell must grade the reasoning channel in exactly that case --
    empty content + non-empty reasoning + finish_reason 'stop' -- and flag the sample
    (`scored_on_reasoning_channel`), never touching ordinary content-channel scoring."""

    def setUp(self):
        self.task = longcontext.SingleNeedleTask()
        self.payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=999)

    @staticmethod
    def _reasoning_only_request_fn(finish_reason="stop"):
        marker = "Reference code for this section: "

        def request_fn(prompt, max_tokens, sampling):
            start = prompt.index(marker) + len(marker)
            code = prompt[start:prompt.index(".", start)]
            return {
                "success": True, "error": None, "content_text": "",
                "reasoning_text": f"The reference code is {code}.",
                "finish_reason": finish_reason, "usage": {"prompt_tokens": 1234},
                "wall_seconds": 0.1, "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        return request_fn

    def test_empty_content_with_answer_in_reasoning_scores_correct_and_flags(self):
        result = longcontext.run_cell(
            self._reasoning_only_request_fn(), self.task, "cell-reasoning-fallback", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=2,
        )
        for sample in result.samples:
            self.assertTrue(sample["correct"])
            self.assertEqual(sample["classifier_verdict"], "correct")
            self.assertTrue(sample["classifier_flags"]["scored_on_reasoning_channel"])

    def test_truncated_reasoning_never_triggers_fallback(self):
        # finish_reason 'length' with empty content stays the truncated(benign) path -- the
        # fallback applies only to a CLEAN finish whose answer landed in the reasoning channel.
        result = longcontext.run_cell(
            self._reasoning_only_request_fn(finish_reason="length"), self.task,
            "cell-reasoning-truncated", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertFalse(sample["correct"])
        self.assertEqual(sample["classifier_verdict"], "truncated(benign)")
        self.assertFalse(sample["classifier_flags"]["scored_on_reasoning_channel"])

    def test_content_channel_scoring_unaffected(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-content-normal", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertTrue(sample["correct"])
        self.assertFalse(sample["classifier_flags"]["scored_on_reasoning_channel"])

    def test_wrong_on_both_channels_stays_wrong(self):
        def request_fn(prompt, max_tokens, sampling):
            return {
                "success": True, "error": None, "content_text": "",
                "reasoning_text": "I could not find any reference code in the text provided.",
                "finish_reason": "stop", "usage": {"prompt_tokens": 1234},
                "wall_seconds": 0.1, "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        result = longcontext.run_cell(
            request_fn, self.task, "cell-reasoning-wrong", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertFalse(sample["correct"])
        self.assertEqual(sample["classifier_verdict"], "wrong")
        self.assertTrue(sample["classifier_flags"]["scored_on_reasoning_channel"])


class TestTransportFailureTaxonomy(unittest.TestCase):
    """Transport-failure-taxonomy WP (`plans/bakeoff-transport-taxonomy-and-prompt-fit.md`
    WP-1, Change 1): a `success=False` trial (an HTTP/transport failure -- empty channels, null
    `finish_reason`) must never be run through `classify_trial` and must never carry a
    correctness-taxonomy verdict -- it gets `classifier_verdict: "transport"` directly, always,
    regardless of what its (irrelevant) channel content happens to look like."""

    def setUp(self):
        self.task = longcontext.SingleNeedleTask()
        self.payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=999)

    @staticmethod
    def _transport_failure_request_fn(reasoning_text="", finish_reason=None):
        def request_fn(prompt, max_tokens, sampling):
            return {
                "success": False, "error": "HTTPError: HTTP Error 400: Bad Request",
                "content_text": "", "reasoning_text": reasoning_text,
                "finish_reason": finish_reason, "usage": {}, "wall_seconds": 0.7,
                "t0_iso": "2026-08-24T20:03:13+00:00",
            }
        return request_fn

    def test_failed_trial_carries_transport_verdict_never_wrong(self):
        result = longcontext.run_cell(
            self._transport_failure_request_fn(), self.task, "cell-transport", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertEqual(sample["classifier_verdict"], "transport")
        self.assertNotEqual(sample["classifier_verdict"], "wrong")
        self.assertIsNone(sample["classifier_shape"])
        self.assertEqual(sample["classifier_flags"], {})
        self.assertFalse(sample["correct"])
        self.assertEqual(sample["success"], False)
        self.assertEqual(sample["error"], "HTTPError: HTTP Error 400: Bad Request")

    def test_reasoning_channel_fallback_never_fires_for_a_failed_trial(self):
        # Same shape that triggers the fallback for a SUCCESSFUL trial (empty content, non-empty
        # reasoning carrying the right answer, finish_reason 'stop') -- but success=False must
        # short-circuit before that logic ever runs, so this stays 'transport', never 'correct'.
        code = "FL3-VERTEX-6907"
        result = longcontext.run_cell(
            self._transport_failure_request_fn(
                reasoning_text=f"The reference code is {code}.", finish_reason="stop",
            ),
            self.task, "cell-transport-reasoning", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertEqual(sample["classifier_verdict"], "transport")
        self.assertFalse(sample["correct"])

    def test_mixed_battery_classifies_each_trial_independently(self):
        calls = {"n": 0}

        def request_fn(prompt, max_tokens, sampling):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "success": False, "error": "HTTPError: HTTP Error 400: Bad Request",
                    "content_text": "", "reasoning_text": "", "finish_reason": None,
                    "usage": {}, "wall_seconds": 0.7, "t0_iso": "2026-08-24T20:03:13+00:00",
                }
            return _make_correct_request_fn(self.task)(prompt, max_tokens, sampling)

        result = longcontext.run_cell(
            request_fn, self.task, "cell-mixed", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=2,
        )
        self.assertEqual(result.samples[0]["classifier_verdict"], "transport")
        self.assertEqual(result.samples[1]["classifier_verdict"], "correct")


class TestLiveEntryPoint(unittest.TestCase):
    """WP-2's live entry point (`build_live_request_fn`/`run_live_battery`). Never opens a
    socket: `build_live_request_fn`'s own tests monkeypatch `run_harness._execute_streaming_
    request` (the same 'injected fake' idiom `sweep.py`/`batch.py` use for
    `scratch_server_fn`/`container_controller`); `run_live_battery`'s own tests use its
    `request_fn=` override, never the live default."""

    def setUp(self):
        self._orig_execute = run_harness._execute_streaming_request

    def tearDown(self):
        run_harness._execute_streaming_request = self._orig_execute

    def test_build_live_request_fn_forwards_base_url_alias_and_sampling(self):
        captured = {}

        def fake_execute(base_url, alias, messages, max_tokens, timeout=180,
                          temperature=None, top_p=None, top_k=None, seed=None):
            captured["base_url"] = base_url
            captured["alias"] = alias
            captured["messages"] = messages
            captured["max_tokens"] = max_tokens
            captured["timeout"] = timeout
            captured["temperature"] = temperature
            captured["top_p"] = top_p
            captured["top_k"] = top_k
            captured["seed"] = seed
            return {
                "success": True, "error": None, "content_text": "ok", "reasoning_text": "",
                "finish_reason": "stop", "usage": {"prompt_tokens": 42}, "wall_seconds": 0.01,
                "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        run_harness._execute_streaming_request = fake_execute
        request_fn = longcontext.build_live_request_fn("http://fake-bridge-ip:9001", "alias-x", timeout=123)
        result = request_fn("hello world", 128, {"temperature": 1.0, "top_p": 0.9, "top_k": 40, "seed": 7})

        self.assertEqual(captured["base_url"], "http://fake-bridge-ip:9001")
        self.assertEqual(captured["alias"], "alias-x")
        self.assertEqual(captured["messages"], [{"role": "user", "content": "hello world"}])
        self.assertEqual(captured["max_tokens"], 128)
        self.assertEqual(captured["timeout"], 123)
        self.assertEqual(captured["temperature"], 1.0)
        self.assertEqual(captured["top_p"], 0.9)
        self.assertEqual(captured["top_k"], 40)
        self.assertEqual(captured["seed"], 7)
        self.assertEqual(result["usage"]["prompt_tokens"], 42)

    def test_build_live_request_fn_never_invoked_by_construction_alone(self):
        # Constructing the closure must not itself perform any I/O -- only calling the RETURNED
        # function does. If construction touched the network this would hang/fail this test
        # suite outright (no real bridge IP exists in the test environment).
        request_fn = longcontext.build_live_request_fn("http://192.0.2.1:1", "unreachable-alias")
        self.assertTrue(callable(request_fn))


class TestRunLiveBattery(unittest.TestCase):
    """`run_live_battery`'s task/position loop structure, exercised entirely via its `request_fn`
    override (never the live default) -- proves the live entry point drives every task x
    position combination and returns the un-merged shape `make_longcontext_cell_runner`
    (batch.py) depends on."""

    def test_covers_every_task_and_position_with_correct_stub(self):
        def request_fn(prompt, max_tokens, sampling):
            content = ""
            marker = "Reference code for this section: "
            if marker in prompt:
                start = prompt.index(marker) + len(marker)
                content = prompt[start:prompt.index(".", start)]
            else:
                import re
                codes = re.findall(r"Checkpoint \d+ marker: ([A-Z0-9-]+)\.", prompt)
                if codes:
                    content = ", ".join(codes)
                else:
                    for item in longcontext.PASSAGE_QA_ITEMS:
                        if item["passage"] in prompt:
                            content = item["answer_keyword"]
                            break
            return {
                "success": True, "error": None, "content_text": content, "reasoning_text": "ok",
                "finish_reason": "stop", "usage": {"prompt_tokens": 999}, "wall_seconds": 0.01,
                "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=1)
        results = longcontext.run_live_battery(
            "http://unused:1", "unused-alias", "live-cell", 2000,
            "capability", payload, n_trials=2, request_fn=request_fn,
        )
        self.assertEqual(set(results.keys()), set(longcontext.TASKS.keys()))
        for task_name, per_position in results.items():
            self.assertEqual(set(per_position.keys()), set(longcontext.POSITION_FRACTIONS))
            for position_fraction, cell_result in per_position.items():
                self.assertEqual(len(cell_result.samples), 2)
                for sample in cell_result.samples:
                    self.assertEqual(sample["task"], task_name)
                    self.assertEqual(sample["position_fraction"], position_fraction)
                    self.assertTrue(sample["correct"])
                    self.assertEqual(sample["prompt_tokens"], 999)

    def test_never_calls_the_live_default_when_request_fn_is_overridden(self):
        # If this somehow fell through to build_live_request_fn -> _execute_streaming_request, a
        # real socket connection to "192.0.2.1" (TEST-NET-1, guaranteed unreachable) would hang
        # or raise instead of returning cleanly -- overriding request_fn must fully bypass that.
        calls = []

        def request_fn(prompt, max_tokens, sampling):
            calls.append(1)
            return {
                "success": True, "error": None, "content_text": "", "reasoning_text": "",
                "finish_reason": "stop", "usage": {"prompt_tokens": 10}, "wall_seconds": 0.01,
                "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=1)
        longcontext.run_live_battery(
            "http://192.0.2.1:1", "unreachable", "live-cell-2", 2000,
            "capability", payload, n_trials=1, request_fn=request_fn,
            tasks={"single_needle": longcontext.SingleNeedleTask()},
            positions=(0.5,),
        )
        self.assertEqual(len(calls), 1)

    def test_production_arm_pinned_values_pass_through_end_to_end(self):
        # Brief: "add a test proving the production arm passes pinned values end-to-end".
        seen_sampling = []

        def request_fn(prompt, max_tokens, sampling):
            seen_sampling.append(dict(sampling))
            return {
                "success": True, "error": None, "content_text": "whatever", "reasoning_text": "",
                "finish_reason": "stop", "usage": {"prompt_tokens": 5}, "wall_seconds": 0.01,
                "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        production_preset = {"temperature": 1.0, "top_p": 0.95, "top_k": 64}  # gemma4's table entry
        payload = run_harness.sampling_arm_payload(production_preset, seed=0)
        longcontext.run_live_battery(
            "http://unused:1", "unused-alias", "prod-cell", 2000,
            "production", payload, n_trials=3, request_fn=request_fn,
            tasks={"single_needle": longcontext.SingleNeedleTask()},
            positions=(0.45,),
        )
        self.assertEqual(len(seen_sampling), 3)
        for sampling in seen_sampling:
            self.assertEqual(sampling["temperature"], 1.0)
            self.assertEqual(sampling["top_p"], 0.95)
            self.assertEqual(sampling["top_k"], 64)
            self.assertIsNotNone(sampling["seed"])  # per-trial derived seed, never None/omitted

    def test_unfilled_production_arm_still_raises_through_the_live_entry(self):
        # _require_pinned_sampling must keep raising even when reached via the live entry, not
        # just via run_cell called directly (test_run_cell.py's existing coverage).
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_PRODUCTION, seed=1)
        with self.assertRaises(ValueError):
            longcontext.run_live_battery(
                "http://unused:1", "unused-alias", "bad-prod-cell", 2000,
                "production", payload, n_trials=1, request_fn=lambda p, m, s: {},
                tasks={"single_needle": longcontext.SingleNeedleTask()},
                positions=(0.45,),
            )


class TestWp6HaystackCalibrationAndTaskCaps(unittest.TestCase):
    """WP-6 (WP-5 findings): per-model `chars_per_token` haystack sizing and the per-task
    response-token override, threaded through the live entry point."""

    @staticmethod
    def _capturing_request_fn(captured):
        def request_fn(prompt, max_tokens, sampling):
            captured.append({"prompt_chars": len(prompt), "max_tokens": max_tokens})
            return {
                "success": True, "error": None, "content_text": "x", "reasoning_text": "",
                "finish_reason": "stop", "usage": {"prompt_tokens": 10}, "wall_seconds": 0.01,
                "t0_iso": "2026-08-23T00:00:00+00:00",
            }
        return request_fn

    def test_chars_per_token_scales_the_assembled_haystack(self):
        task = longcontext.SingleNeedleTask()
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=1)
        sizes = {}
        for cpt in (None, 2.0):
            captured = []
            longcontext.run_cell(
                self._capturing_request_fn(captured), task, "cpt-cell", 0.45, 2000,
                "capability", payload, n_trials=1, chars_per_token=cpt,
            )
            sizes[cpt] = captured[0]["prompt_chars"]
        # Default sizing targets 2000 * 4.5 = 9000 chars; cpt=2.0 targets 4000. The needle and
        # question add a fixed overhead to both, so assert the ratio band rather than exact chars.
        self.assertLess(sizes[2.0], sizes[None])
        ratio = sizes[2.0] / sizes[None]
        self.assertLess(abs(ratio - (2.0 / run_harness.CHARS_PER_TOKEN_ESTIMATE)), 0.15)

    def test_max_tokens_by_task_overrides_only_the_named_task(self):
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=1)
        seen = []

        def request_fn(prompt, max_tokens, sampling):
            seen.append(max_tokens)
            return {
                "success": True, "error": None, "content_text": "x", "reasoning_text": "",
                "finish_reason": "stop", "usage": {"prompt_tokens": 10}, "wall_seconds": 0.01,
                "t0_iso": "2026-08-23T00:00:00+00:00",
            }

        results = longcontext.run_live_battery(
            "http://unused:1", "unused-alias", "cap-cell", 2000,
            "capability", payload, n_trials=1, request_fn=request_fn,
            tasks={
                "single_needle": longcontext.SingleNeedleTask(),
                "multi_needle": longcontext.MultiNeedleTask(),
            },
            positions=(0.45,),
            max_tokens=256, max_tokens_by_task={"multi_needle": 768},
        )
        self.assertEqual(set(results.keys()), {"single_needle", "multi_needle"})
        # Task iteration order is the tasks-dict order: single_needle first (256), then
        # multi_needle (768), one request each (1 trial x 1 position).
        self.assertEqual(seen, [256, 768])


class TestRunPositionSweep(unittest.TestCase):
    def test_never_averages_across_positions(self):
        task = longcontext.SingleNeedleTask()
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=42)
        results = longcontext.run_position_sweep(
            _make_correct_request_fn(task), task, "sweep-cell", 2000,
            sampling_arm="capability", sampling_payload=payload, n_trials=2,
        )
        self.assertEqual(set(results.keys()), set(longcontext.POSITION_FRACTIONS))
        for position_fraction, cell_result in results.items():
            for sample in cell_result.samples:
                self.assertEqual(sample["position_fraction"], position_fraction)
        # each position is an independently seeded cell -- distinct haystack_seeds across cells
        first_seed_by_position = {
            pos: cell_result.samples[0]["haystack_seed"] for pos, cell_result in results.items()
        }
        self.assertEqual(len(set(first_seed_by_position.values())), len(first_seed_by_position))

    def test_all_three_tasks_runnable_end_to_end_offline(self):
        payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=7)
        for task_name, task in longcontext.TASKS.items():
            result = longcontext.run_cell(
                _make_correct_request_fn(task), task, f"cell-{task_name}", 0.45, 2000,
                sampling_arm="capability", sampling_payload=payload, n_trials=2,
            )
            self.assertEqual(len(result.samples), 2)
            for sample in result.samples:
                self.assertTrue(sample["correct"], f"{task_name}: stub should always answer correctly")


class TestWp2PromptFitShrinkLoop(unittest.TestCase):
    """Plan `bakeoff-transport-taxonomy-and-prompt-fit.md` WP-2: `run_cell`'s optional
    `tokenize_fn`/`token_budget` collaborators (`_assemble_haystack_fit`) -- live-tokenize the
    assembled haystack and deterministically shrink it before `task.build_trial` ever runs.
    Every `tokenize_fn` here is a pure stubbed hook (ignores its `text` argument, returns
    canned values) -- no network anywhere in this suite."""

    def setUp(self):
        self.task = longcontext.SingleNeedleTask()
        self.payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=999)

    @staticmethod
    def _queue_tokenize_fn(values):
        """A `tokenize_fn` stub returning successive entries of `values` on each call (clamped
        to the last entry once exhausted) -- lets a test pin exactly how many assemblies the
        shrink loop performs without needing to reason about the real corpus's char/token
        ratio."""
        values = list(values)
        state = {"n": 0}

        def fn(text):
            idx = min(state["n"], len(values) - 1)
            state["n"] += 1
            return values[idx]

        return fn

    def test_fit_first_try_records_zero_shrink_attempts(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-fit-first-try", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=3,
            tokenize_fn=self._queue_tokenize_fn([500]), token_budget=1000,
        )
        self.assertEqual(len(result.samples), 3)
        for sample in result.samples:
            self.assertEqual(sample["trial_meta"]["haystack_shrink_attempts"], 0)
            self.assertEqual(sample["trial_meta"]["haystack_measured_tokens"], 500)

    def test_shrink_converges_deterministically_across_reruns(self):
        def _run():
            return longcontext.run_cell(
                _make_correct_request_fn(self.task), self.task, "cell-shrink-converges", 0.45, 2000,
                sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
                tokenize_fn=self._queue_tokenize_fn([5000, 800]), token_budget=1000,
            )

        sample_a = _run().samples[0]
        sample_b = _run().samples[0]
        self.assertEqual(sample_a["trial_meta"]["haystack_shrink_attempts"], 1)
        self.assertEqual(sample_a["trial_meta"]["haystack_measured_tokens"], 800)
        # Determinism: an identical stubbed tokenize sequence against the same haystack_seed
        # replays the identical attempt sequence and lands on the identical final haystack text
        # -- `haystack_sha256` (the emitted prompt's hash, needle+question included) is the
        # existing proof point for that; `trial_meta` matching too proves the shrink bookkeeping
        # itself is a pure function of the inputs, not incidentally order- or state-dependent.
        self.assertEqual(sample_a["haystack_sha256"], sample_b["haystack_sha256"])
        self.assertEqual(sample_a["trial_meta"], sample_b["trial_meta"])

    def test_shrink_exhausted_raises_loudly_never_sends_over_budget_prompt(self):
        calls = []

        def failing_request_fn(prompt, max_tokens, sampling):
            calls.append(prompt)  # should never be reached
            return _make_correct_request_fn(self.task)(prompt, max_tokens, sampling)

        always_over = self._queue_tokenize_fn([99999])  # never fits, regardless of assembly
        with self.assertRaises(longcontext.HaystackOverBudgetError):
            longcontext.run_cell(
                failing_request_fn, self.task, "cell-shrink-exhausted", 0.45, 2000,
                sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
                tokenize_fn=always_over, token_budget=1000,
            )
        self.assertEqual(calls, [])  # the over-budget prompt is never sent to request_fn

    def test_tokenize_fn_none_preserves_todays_behavior_byte_for_byte(self):
        result = longcontext.run_cell(
            _make_correct_request_fn(self.task), self.task, "cell-no-fit-check", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertNotIn("haystack_measured_tokens", sample["trial_meta"])
        self.assertNotIn("haystack_shrink_attempts", sample["trial_meta"])
        self.assertEqual(sample["trial_meta"], {"expected_code": sample["expected"]})

    def test_tokenize_fn_transport_failure_propagates_never_silently_treated_as_fitting(self):
        def failing_tokenize_fn(text):
            raise RuntimeError("tokenize_count: request to http://fake/tokenize failed: refused")

        with self.assertRaises(RuntimeError):
            longcontext.run_cell(
                _make_correct_request_fn(self.task), self.task, "cell-tokenize-transport-fail",
                0.45, 2000, sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
                tokenize_fn=failing_tokenize_fn, token_budget=1000,
            )


class TestRunCellTimingEnrichment(unittest.TestCase):
    """WP-1 of `plans/bakeoff-timing-records-and-estimates.md`: three additive, nullable
    per-probe fields on the `longcontext_probe` row -- `completion_tokens` (projected from the
    already-parsed `usage`) and `server_prompt_eval_tok_s`/`server_eval_tok_s` (the server's own
    `timings` block, NEVER derived from `wall_seconds`)."""

    def setUp(self):
        self.task = longcontext.SingleNeedleTask()
        self.payload = run_harness.sampling_arm_payload(run_harness.SAMPLING_ARM_CAPABILITY, seed=7)

    def _request_fn_with(self, usage, timings):
        def request_fn(prompt, max_tokens, sampling):
            result = {
                "success": True, "error": None, "content_text": "x", "reasoning_text": "",
                "finish_reason": "stop", "usage": usage, "wall_seconds": 12.5,
                "t0_iso": "2026-08-25T00:00:00+00:00",
            }
            if timings is not None:
                result["timings"] = timings
            return result

        return request_fn

    def test_projects_completion_tokens_and_server_rates(self):
        request_fn = self._request_fn_with(
            {"prompt_tokens": 1234, "completion_tokens": 56},
            {"prompt_per_second": 1679.5, "predicted_per_second": 61.1, "prompt_n": 1234},
        )
        result = longcontext.run_cell(
            request_fn, self.task, "cell-timing-enriched", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertEqual(sample["completion_tokens"], 56)
        self.assertEqual(sample["server_prompt_eval_tok_s"], 1679.5)
        self.assertEqual(sample["server_eval_tok_s"], 61.1)
        # The existing fields are untouched, and decode rate is NEVER wall-derived: 56/12.5
        # would be 4.48 tok/s, a number this row must never report.
        self.assertEqual(sample["prompt_tokens"], 1234)
        self.assertEqual(sample["wall_seconds"], 12.5)
        self.assertNotAlmostEqual(sample["server_eval_tok_s"], 56 / 12.5)

    def test_missing_timings_block_records_explicit_nulls(self):
        # Back-compat: a response (or an injected stub) with no `timings` and no
        # `completion_tokens` still produces a valid row -- explicit nulls, never a guess.
        request_fn = self._request_fn_with({"prompt_tokens": 1234}, None)
        result = longcontext.run_cell(
            request_fn, self.task, "cell-timing-absent", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        for key in ("completion_tokens", "server_prompt_eval_tok_s", "server_eval_tok_s"):
            self.assertIn(key, sample)
            self.assertIsNone(sample[key])

    def test_transport_failed_trial_still_carries_the_keys(self):
        def failing_request_fn(prompt, max_tokens, sampling):
            return {
                "success": False, "error": "URLError: refused", "content_text": "",
                "reasoning_text": "", "finish_reason": None, "usage": None,
                "wall_seconds": 0.01, "t0_iso": "2026-08-25T00:00:00+00:00",
            }

        result = longcontext.run_cell(
            failing_request_fn, self.task, "cell-timing-transport", 0.45, 2000,
            sampling_arm="capability", sampling_payload=self.payload, n_trials=1,
        )
        sample = result.samples[0]
        self.assertFalse(sample["success"])
        self.assertIsNone(sample["completion_tokens"])
        self.assertIsNone(sample["server_eval_tok_s"])


if __name__ == "__main__":
    unittest.main()
