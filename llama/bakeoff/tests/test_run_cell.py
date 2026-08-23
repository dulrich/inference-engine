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
            self.assertEqual(sample["corpus_manifest_id"], "gutenberg-longcontext-haystack-v1")
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


if __name__ == "__main__":
    unittest.main()
