"""Task battery: single-needle literal recall, multi-needle ordered readback, passage-anchored
QA (plan WP-3; doc Req 1's literal-vs-non-literal separation)."""

import random
import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402

HAYSTACK = ("Once upon a time in a quiet valley. There were many houses. Each house had a garden. "
            "The gardens grew all manner of flowers. ") * 60


class TestSingleNeedleTask(unittest.TestCase):
    def setUp(self):
        self.task = longcontext.SingleNeedleTask()

    def test_build_trial_contains_code_and_question(self):
        rng = random.Random(1)
        trial = self.task.build_trial(HAYSTACK, 0.45, rng)
        self.assertIn(trial.expected, trial.prompt)
        self.assertIn("Question:", trial.prompt)
        self.assertEqual(trial.task, "single_needle")

    def test_score_correct(self):
        rng = random.Random(1)
        trial = self.task.build_trial(HAYSTACK, 0.45, rng)
        self.assertTrue(self.task.score(f"The code is {trial.expected}.", trial))

    def test_score_wrong(self):
        rng = random.Random(1)
        trial = self.task.build_trial(HAYSTACK, 0.45, rng)
        self.assertFalse(self.task.score("XX9-NOPE-0000", trial))

    def test_score_empty_content(self):
        rng = random.Random(1)
        trial = self.task.build_trial(HAYSTACK, 0.45, rng)
        self.assertFalse(self.task.score("", trial))


class TestMultiNeedleTask(unittest.TestCase):
    def setUp(self):
        self.task = longcontext.MultiNeedleTask()

    def test_build_trial_has_three_distinct_codes_in_ascending_order_in_prompt(self):
        rng = random.Random(2)
        trial = self.task.build_trial(HAYSTACK, 0.10, rng)
        self.assertEqual(len(trial.expected), 3)
        self.assertEqual(len(set(trial.expected)), 3)
        positions = [trial.prompt.find(c) for c in trial.expected]
        self.assertTrue(all(p != -1 for p in positions))
        self.assertEqual(positions, sorted(positions))

    def test_score_correct_order(self):
        rng = random.Random(2)
        trial = self.task.build_trial(HAYSTACK, 0.10, rng)
        readback = ", ".join(trial.expected)
        self.assertTrue(self.task.score(readback, trial))

    def test_score_wrong_order(self):
        rng = random.Random(2)
        trial = self.task.build_trial(HAYSTACK, 0.10, rng)
        readback = ", ".join(reversed(trial.expected))
        self.assertFalse(self.task.score(readback, trial))

    def test_score_missing_one_code(self):
        rng = random.Random(2)
        trial = self.task.build_trial(HAYSTACK, 0.10, rng)
        readback = ", ".join(trial.expected[:2])
        self.assertFalse(self.task.score(readback, trial))


class TestPassageQATask(unittest.TestCase):
    def setUp(self):
        self.task = longcontext.PassageQATask()

    def test_build_trial_contains_passage_and_question(self):
        rng = random.Random(3)
        trial = self.task.build_trial(HAYSTACK, 0.85, rng)
        self.assertIn(trial.expected, trial.prompt)
        self.assertIn("Question:", trial.prompt)

    def test_score_correct_case_insensitive(self):
        rng = random.Random(3)
        trial = self.task.build_trial(HAYSTACK, 0.85, rng)
        self.assertTrue(self.task.score(f"It was called the {trial.expected.lower()}.", trial))

    def test_score_wrong(self):
        rng = random.Random(3)
        trial = self.task.build_trial(HAYSTACK, 0.85, rng)
        self.assertFalse(self.task.score("I don't know.", trial))

    def test_answer_keyword_never_appears_verbatim_in_its_own_question(self):
        """doc Req 1 (NoLiMa-class): the question must not let a model win by literal string
        match against the answer-bearing term. Verified programmatically, not just by eye."""
        for item in longcontext.PASSAGE_QA_ITEMS:
            self.assertNotIn(
                item["answer_keyword"].lower(), item["question"].lower(),
                f"{item['id']}: answer_keyword leaks verbatim into its own question text",
            )

    def test_all_qa_items_have_required_fields(self):
        for item in longcontext.PASSAGE_QA_ITEMS:
            for field in ("id", "passage", "question", "answer_keyword"):
                self.assertIn(field, item)


class TestTaskRegistry(unittest.TestCase):
    def test_tasks_dict_has_all_three_battery_tasks(self):
        self.assertEqual(set(longcontext.TASKS), {"single_needle", "multi_needle", "passage_qa"})

    def test_position_fractions_are_the_plan_pinned_set(self):
        self.assertEqual(longcontext.POSITION_FRACTIONS, (0.10, 0.45, 0.85))


if __name__ == "__main__":
    unittest.main()
