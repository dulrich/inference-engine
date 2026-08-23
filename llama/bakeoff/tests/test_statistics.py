"""95% Wilson intervals on failure rates (plan Statistics pins, cycle-2 audit correction:
"state the convention explicitly at every use site ... never mix one-sided/two-sided in one
comparison"). Cross-checked against the plan's own cited worked numbers (Split provenance:
"0/30 Wilson upper = 8.27%", "a 0-vs-0 Newcombe difference bound at n=30 is ~11.7pp")."""

import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402


class TestWilsonInterval(unittest.TestCase):
    def test_sided_is_required_no_silent_default(self):
        with self.assertRaises(TypeError):
            longcontext.wilson_interval(0, 30)  # missing required `sided` positional arg

    def test_invalid_sided_raises(self):
        with self.assertRaises(ValueError):
            longcontext.wilson_interval(0, 30, sided="both")

    def test_unsupported_confidence_raises(self):
        with self.assertRaises(ValueError):
            longcontext.wilson_interval(0, 30, sided="two", confidence=0.99)

    def test_zero_n_raises(self):
        with self.assertRaises(ValueError):
            longcontext.wilson_interval(0, 0, sided="two")

    def test_k_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            longcontext.wilson_interval(31, 30, sided="two")

    def test_one_sided_zero_of_thirty_matches_plan_cited_value(self):
        # plan Split provenance: "one-sided 95%: 0/30 Wilson upper = 8.27%"
        result = longcontext.wilson_interval(0, 30, sided="one")
        self.assertIsNone(result["lower"])
        self.assertAlmostEqual(result["upper"], 0.0827, places=3)

    def test_two_sided_populates_both_bounds(self):
        result = longcontext.wilson_interval(15, 30, sided="two")
        self.assertIsNotNone(result["lower"])
        self.assertIsNotNone(result["upper"])
        self.assertLess(result["lower"], 0.5)
        self.assertGreater(result["upper"], 0.5)

    def test_result_always_states_its_own_convention(self):
        for sided in ("one", "two"):
            result = longcontext.wilson_interval(3, 20, sided=sided)
            self.assertEqual(result["sided"], sided)
            self.assertEqual(result["confidence"], 0.95)

    def test_bounds_are_within_zero_one(self):
        result = longcontext.wilson_interval(0, 30, sided="two")
        self.assertGreaterEqual(result["lower"], 0.0)
        self.assertLessEqual(result["upper"], 1.0)

    def test_full_failure_rate_upper_bound_near_one(self):
        result = longcontext.wilson_interval(30, 30, sided="two")
        self.assertGreater(result["upper"], 0.85)
        self.assertLessEqual(result["upper"], 1.0)


class TestDeriveTrialSeed(unittest.TestCase):
    def test_deterministic(self):
        a = longcontext.derive_trial_seed("cell-1", 0, "haystack")
        b = longcontext.derive_trial_seed("cell-1", 0, "haystack")
        self.assertEqual(a, b)

    def test_haystack_and_sampler_seeds_are_distinct(self):
        h = longcontext.derive_trial_seed("cell-1", 0, "haystack")
        s = longcontext.derive_trial_seed("cell-1", 0, "sampler")
        self.assertNotEqual(h, s)

    def test_different_trial_index_differs(self):
        a = longcontext.derive_trial_seed("cell-1", 0, "haystack")
        b = longcontext.derive_trial_seed("cell-1", 1, "haystack")
        self.assertNotEqual(a, b)

    def test_different_cell_id_differs(self):
        a = longcontext.derive_trial_seed("cell-1", 0, "haystack")
        b = longcontext.derive_trial_seed("cell-2", 0, "haystack")
        self.assertNotEqual(a, b)

    def test_seed_is_nonnegative_31_bit(self):
        seed = longcontext.derive_trial_seed("cell-x", 5, "sampler")
        self.assertGreaterEqual(seed, 0)
        self.assertLess(seed, 2 ** 31)


if __name__ == "__main__":
    unittest.main()
