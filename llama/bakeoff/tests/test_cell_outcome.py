"""The pure outcome-precedence table (`cell_outcome.py`, WP-R1).

Before WP-R1 the rules deciding what a cell's run MEANS were spread across four call sites and
tested only indirectly, through a live-ish runner with an injected battery double. They are now
one pure function over the measurement results, so they can be tested as what they are: a table.

Two things are asserted here that were previously enforced only by hand at each construction
site:

1. **The precedence order** -- transport beats degenerate beats needle_failure beats completed.
2. **The `correctness_failure` invariant** -- it is True for exactly `degenerate` and
   `needle_failure`, and for nothing else. It arms skip propagation, which suppresses later
   cells that were never measured; a transport, startup, size, or hang fact must never do that.

Entirely offline and I/O-free: every input is a literal.
"""

import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import cell_outcome  # noqa: E402


def _s(verdict, success=True):
    return {"classifier_verdict": verdict, "success": success}


CORRECT = _s("correct")
DEGENERATE = _s("degenerate")
WRONG = _s("wrong")
TRUNCATED = _s("truncated(benign)")
TRANSPORT_FAILED = _s("wrong", success=False)


class TestBatterySamplePrecedence(unittest.TestCase):
    """`classify_battery_samples`: one row per rule, plus every shadowing pair that matters."""

    CASES = [
        # (name, samples_by_task, expected reason, expected correctness_failure)
        ("empty battery completes", {}, None, False),
        ("all correct completes", {"single_needle": [CORRECT]}, None, False),
        ("truncated(benign) is not degeneracy evidence",
         {"single_needle": [TRUNCATED]}, None, False),
        ("passage_qa wrong is not a needle failure",
         {"passage_qa": [WRONG]}, None, False),
        ("single_needle wrong is a needle failure",
         {"single_needle": [WRONG]}, "needle_failure", True),
        ("multi_needle wrong is a needle failure",
         {"multi_needle": [WRONG]}, "needle_failure", True),
        ("degenerate anywhere outranks a needle failure",
         {"single_needle": [WRONG], "passage_qa": [DEGENERATE]}, "degenerate", True),
        ("a transport failure outranks degenerate",
         {"passage_qa": [DEGENERATE], "single_needle": [TRANSPORT_FAILED]},
         "transport_error", False),
        ("a transport failure outranks a needle failure",
         {"single_needle": [TRANSPORT_FAILED]}, "transport_error", False),
        ("a transport failure on an unrelated task still wins",
         {"passage_qa": [_s("transport", success=False)], "single_needle": [CORRECT]},
         "transport_error", False),
        ("a genuine wrong from a successful trial still fires",
         {"single_needle": [CORRECT, WRONG]}, "needle_failure", True),
    ]

    def test_precedence_table(self):
        for name, samples_by_task, expected_reason, expected_cf in self.CASES:
            with self.subTest(case=name):
                disposition = cell_outcome.classify_battery_samples(samples_by_task)
                self.assertEqual(disposition.reason, expected_reason)
                self.assertEqual(disposition.correctness_failure, expected_cf)
                self.assertEqual(
                    disposition.status,
                    "completed" if expected_reason is None else "failed",
                )

    def test_a_success_false_row_is_never_read_for_correctness(self):
        # Defense in depth: even an old-schema row that is BOTH transport-failed AND tagged
        # `degenerate` reports the transport fact, and arms nothing.
        disposition = cell_outcome.classify_battery_samples(
            {"single_needle": [_s("degenerate", success=False)]},
        )
        self.assertEqual(disposition.reason, "transport_error")
        self.assertFalse(disposition.correctness_failure)

    def test_classification_is_pure(self):
        # No mutation of the caller's input, and the same input always gives the same answer.
        samples = {"single_needle": [dict(WRONG)]}
        before = repr(samples)
        first = cell_outcome.classify_battery_samples(samples)
        second = cell_outcome.classify_battery_samples(samples)
        self.assertEqual(first, second)
        self.assertEqual(repr(samples), before)


class TestCorrectnessFailureInvariant(unittest.TestCase):
    """`correctness_failure` arms `propagate_skips`. Exactly two reasons may set it."""

    ALL_FAILED_DISPOSITIONS = {
        "health_timeout": cell_outcome.HEALTH_TIMEOUT,
        "invalid_config": cell_outcome.INVALID_CONFIG,
        "invalid_sampling_arm": cell_outcome.INVALID_SAMPLING_ARM,
        "battery_error": cell_outcome.BATTERY_ERROR,
        "transport_error": cell_outcome.TRANSPORT_ERROR,
        "degenerate": cell_outcome.DEGENERATE,
        "needle_failure": cell_outcome.NEEDLE_FAILURE,
        "interrupted": cell_outcome.INTERRUPTED,
        "projected_size_exceeded": cell_outcome.PROJECTED_SIZE_EXCEEDED,
    }

    def test_only_degenerate_and_needle_failure_are_correctness_failures(self):
        armed = {
            reason for reason, d in self.ALL_FAILED_DISPOSITIONS.items()
            if d.correctness_failure
        }
        self.assertEqual(armed, {"degenerate", "needle_failure"})
        self.assertEqual(armed, set(cell_outcome.CORRECTNESS_FAILURE_REASONS))

    def test_every_failed_disposition_reports_its_own_reason(self):
        for reason, disposition in self.ALL_FAILED_DISPOSITIONS.items():
            with self.subTest(reason=reason):
                self.assertEqual(disposition.status, "failed")
                self.assertEqual(disposition.reason, reason)

    def test_completed_carries_no_reason_and_never_arms_propagation(self):
        self.assertEqual(cell_outcome.COMPLETED.status, "completed")
        self.assertIsNone(cell_outcome.COMPLETED.reason)
        self.assertFalse(cell_outcome.COMPLETED.correctness_failure)

    def test_a_hang_never_arms_propagation(self):
        # A wedged GPU says nothing about model quality; propagating skips off it would suppress
        # cells that were never measured.
        self.assertFalse(cell_outcome.aborted_hang().correctness_failure)
        self.assertFalse(cell_outcome.aborted_hang("MES might be in...").correctness_failure)
        self.assertEqual(cell_outcome.aborted_hang().status, "aborted-hang")
        self.assertEqual(cell_outcome.aborted_hang().reason, "hang_signature")
        self.assertEqual(cell_outcome.aborted_hang("custom").reason, "custom")


class TestStartupAndSamplingArmGates(unittest.TestCase):
    def test_startup_gate_table(self):
        cases = [
            ((True, True), None),
            ((False, True), "health_timeout"),
            ((False, False), "health_timeout"),   # health outranks verification
            ((True, False), "invalid_config"),
        ]
        for (healthy, verified), expected in cases:
            with self.subTest(healthy=healthy, verified=verified):
                disposition = cell_outcome.classify_startup(healthy, verified)
                if expected is None:
                    self.assertIsNone(disposition)
                else:
                    self.assertEqual(disposition.reason, expected)
                    self.assertFalse(disposition.correctness_failure)

    def test_sampling_arm_gate(self):
        self.assertIsNone(cell_outcome.classify_sampling_arm("capability"))
        self.assertIsNone(cell_outcome.classify_sampling_arm("production"))
        for bad in ("bogus", None, "", "Production"):
            with self.subTest(arm=bad):
                self.assertEqual(
                    cell_outcome.classify_sampling_arm(bad).reason, "invalid_sampling_arm",
                )


class TestDispositionToCellOutcome(unittest.TestCase):
    def test_with_detail_preserves_the_disposition_verbatim(self):
        outcome = cell_outcome.NEEDLE_FAILURE.with_detail({"x": 1})
        self.assertIsInstance(outcome, cell_outcome.CellOutcome)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "needle_failure")
        self.assertTrue(outcome.correctness_failure)
        self.assertEqual(outcome.detail, {"x": 1})
        self.assertEqual(outcome.disposition, cell_outcome.NEEDLE_FAILURE)

    def test_detail_defaults_to_none(self):
        self.assertIsNone(cell_outcome.COMPLETED.with_detail().detail)

    def test_batch_re_exports_the_same_objects(self):
        # The historical public names stay at their old home after the WP-R1 split.
        self.assertIs(batch.CellOutcome, cell_outcome.CellOutcome)
        self.assertIs(batch.classify_battery_samples, cell_outcome.classify_battery_samples)
        self.assertEqual(
            batch.CORRECTNESS_FAILURE_REASONS, cell_outcome.CORRECTNESS_FAILURE_REASONS,
        )


if __name__ == "__main__":
    unittest.main()
