"""Per-model-family production sampling table (plan `longcontext-pow2-overnight-campaign.md`
WP-2). No network -- this module is a static, researched dict plus a lookup function."""

import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import sampling_table  # noqa: E402


class TestProductionSamplingKnownFamilies(unittest.TestCase):
    def test_gemma4_entry(self):
        values = sampling_table.production_sampling("gemma4")
        self.assertEqual(values, {"temperature": 1.0, "top_p": 0.95, "top_k": 64})

    def test_qwen35_entry(self):
        values = sampling_table.production_sampling("qwen35")
        self.assertEqual(values, {"temperature": 1.0, "top_p": 0.95, "top_k": 20})

    def test_qwen35moe_entry(self):
        values = sampling_table.production_sampling("qwen35moe")
        self.assertEqual(values, {"temperature": 1.0, "top_p": 0.95, "top_k": 20})

    def test_bailingmoe3_entry(self):
        values = sampling_table.production_sampling("bailingmoe3")
        self.assertEqual(values, {"temperature": 1.0, "top_p": 0.95, "top_k": 20})

    def test_returned_dict_is_a_fresh_copy_not_the_module_internal(self):
        values = sampling_table.production_sampling("gemma4")
        values["temperature"] = 0.0
        self.assertEqual(sampling_table.production_sampling("gemma4")["temperature"], 1.0)

    def test_every_known_entry_carries_the_full_required_triple(self):
        for family, values in sampling_table.known_families().items():
            for field in ("temperature", "top_p", "top_k"):
                self.assertIn(field, values, f"{family} missing {field!r}")
                self.assertIsNotNone(values[field], f"{family}[{field!r}] is None")


class TestProductionSamplingSilentCards(unittest.TestCase):
    def test_nemotron_h_has_no_entry_deliberately(self):
        # nemotron_h's card states temperature/top_p per use-case but never top_k -- this
        # module's own documented "do not invent values" call (see sampling_table.py's comment).
        with self.assertRaises(sampling_table.MissingProductionSamplingError):
            sampling_table.production_sampling("nemotron_h")

    def test_unknown_family_raises_the_same_error_family(self):
        with self.assertRaises(sampling_table.MissingProductionSamplingError):
            sampling_table.production_sampling("not-a-real-architecture")

    def test_missing_error_is_a_key_error_subclass(self):
        # callers catching plain KeyError (or this module's specific error) both work.
        self.assertTrue(issubclass(sampling_table.MissingProductionSamplingError, KeyError))
        with self.assertRaises(KeyError):
            sampling_table.production_sampling("nemotron_h")


class TestKnownFamilies(unittest.TestCase):
    def test_known_families_matches_researched_set(self):
        self.assertEqual(
            set(sampling_table.known_families()),
            {"gemma4", "qwen35", "qwen35moe", "bailingmoe3"},
        )
        self.assertNotIn("nemotron_h", sampling_table.known_families())


if __name__ == "__main__":
    unittest.main()
