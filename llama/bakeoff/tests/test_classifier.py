"""Degeneracy classifier (plan WP-3 classifier spec; doc Req 6: shape, not just pass/fail).

Binding invariant (plan): "both real degenerate fragments classify degenerate and all clean
gemma-qat rows classify non-degenerate." This suite tests that invariant against the actual
captured fixtures, plus each degenerate SHAPE via clearly-labeled synthetic fixtures for the
shapes no real capture exists for (n-gram loop repetition, special-token spam, wrong-but-fluent,
truncated(benign)) -- see `docs/long-context-collapse.md`'s fixture-source interpretation notes
and this WP's report for the threshold rationale.
"""

import json
import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BAKEOFF_DIR.parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402

CLEAN_COMPLETIONS_PATH = REPO_ROOT / "runs" / "bakeoff" / "gemma-4-12b-qat-full" / "completions.jsonl"


def _load_degenerate_fragments():
    with (FIXTURES_DIR / "degenerate_fragments.json").open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data["fragments"]


def _load_clean_completions():
    rows = []
    with CLEAN_COMPLETIONS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class TestDegenerateFragmentsAreGroundTruthPositives(unittest.TestCase):
    """Real captured fragments -- SYNTHETIC-CLEAR (they are the opposite: verbatim real
    captures, never invented text) -- from `runs/dispatch/qshiv2-wp-3-fixture-source.md`."""

    @classmethod
    def setUpClass(cls):
        cls.fragments = _load_degenerate_fragments()

    def test_fixture_file_has_exactly_two_fragments(self):
        self.assertEqual(len(self.fragments), 2)

    def test_fixture_metadata_signals_present(self):
        # "their content_text is empty and finish_reason is length -- both metadata signals are
        # part of the fixture" (brief).
        for frag in self.fragments:
            self.assertEqual(frag["content_text"], "")
            self.assertEqual(frag["finish_reason"], "length")

    def test_both_fragments_classify_degenerate(self):
        for frag in self.fragments:
            result = longcontext.classify_trial(
                reasoning_text=frag["reasoning_text_tail"],
                content_text=frag["content_text"],
                finish_reason=frag["finish_reason"],
                correct=False,
            )
            self.assertEqual(
                result.verdict, "degenerate",
                f"{frag['id']}: expected degenerate, got {result.verdict} (flags={result.flags})",
            )

    def test_both_fragments_shape_is_incoherent_not_repetition(self):
        # Fixture-source interpretation notes: "the incoherent-high-entropy shape ... NOT the
        # repetition/entropy-collapse shape."
        for frag in self.fragments:
            result = longcontext.classify_trial(
                reasoning_text=frag["reasoning_text_tail"],
                content_text=frag["content_text"],
                finish_reason=frag["finish_reason"],
                correct=False,
            )
            self.assertEqual(result.shape, "incoherent")

    def test_fragments_have_zero_core_function_words(self):
        for frag in self.fragments:
            flags = longcontext.compute_flags(
                frag["reasoning_text_tail"], frag["content_text"], frag["finish_reason"]
            )
            self.assertEqual(flags["core_function_word_count"], 0)
            self.assertGreaterEqual(flags["token_count"], longcontext.MIN_TOKENS_FOR_COHERENCE_CHECK)


class TestCleanGemmaQatRowsClassifyNonDegenerate(unittest.TestCase):
    """23 rows, real transcripts (full reasoning_text + content_text), committed at
    `runs/bakeoff/gemma-4-12b-qat-full/completions.jsonl` -- includes several timing_round rows
    that share the degenerate fragments' surface shape (finish_reason: length, content_text
    empty) while reasoning in genuinely fluent prose; this is the near-miss case the classifier
    must not flag."""

    @classmethod
    def setUpClass(cls):
        cls.rows = _load_clean_completions()

    def test_loaded_nonzero_rows(self):
        self.assertGreater(len(self.rows), 0)

    def test_all_clean_rows_classify_non_degenerate(self):
        failures = []
        for i, row in enumerate(self.rows):
            result = longcontext.classify_trial(
                reasoning_text=row.get("reasoning_text"),
                content_text=row.get("content_text"),
                finish_reason=row.get("finish_reason"),
                correct=None,
            )
            if result.verdict == "degenerate":
                failures.append((i, row.get("type"), result.shape, result.flags))
        self.assertEqual(
            failures, [],
            f"clean rows misclassified degenerate: {failures}",
        )

    def test_near_miss_content_channel_never_reached_rows_still_non_degenerate(self):
        """The specific near-miss: finish_reason == 'length' AND content_text empty, exactly
        the degenerate fragments' metadata shape -- but coherent reasoning prose."""
        near_miss_rows = [
            r for r in self.rows
            if r.get("finish_reason") == "length" and not (r.get("content_text") or "").strip()
        ]
        self.assertGreater(len(near_miss_rows), 0, "expected at least one near-miss row in the fixture")
        for row in near_miss_rows:
            result = longcontext.classify_trial(
                reasoning_text=row.get("reasoning_text"),
                content_text=row.get("content_text"),
                finish_reason=row.get("finish_reason"),
                correct=None,
            )
            self.assertNotEqual(result.verdict, "degenerate")


class TestSyntheticRepetitionShape(unittest.TestCase):
    """SYNTHETIC fixture -- clearly labeled: n-gram loop repetition (Holtzman-style
    entropy-collapse shape), no real capture exists for this shape."""

    def test_repeated_phrase_loop_classifies_degenerate_repetition(self):
        synthetic_repetition_text = "the answer is clear and the answer is clear and " * 30
        result = longcontext.classify_trial(
            reasoning_text=synthetic_repetition_text,
            content_text="",
            finish_reason="length",
            correct=False,
        )
        self.assertEqual(result.verdict, "degenerate")
        self.assertEqual(result.shape, "repetition")

    def test_low_distinct2_ratio_alone_also_triggers_repetition(self):
        # >= MIN_TOKENS_FOR_REPETITION_CHECK (20) tokens -- a shorter alternating loop would
        # fall through to the (also-degenerate, but wrong-shape) incoherent check instead, since
        # it also has zero core function words but hasn't hit the repetition token-count floor.
        synthetic_low_diversity = "cat dog " * 15  # 30 tokens
        result = longcontext.classify_trial(
            reasoning_text=synthetic_low_diversity,
            content_text="",
            finish_reason="length",
        )
        self.assertEqual(result.verdict, "degenerate")
        self.assertEqual(result.shape, "repetition")


class TestSyntheticSpecialTokenSpamShape(unittest.TestCase):
    """SYNTHETIC fixture -- clearly labeled: `<unused24>`-class special-token leakage
    (`docs/long-context-collapse.md` §6, upstream issue #21321)."""

    def test_unused_token_spam_classifies_degenerate_special_token_spam(self):
        synthetic_spam = "<unused24><unused24><unused31><unused24><unused8><unused24> some text"
        result = longcontext.classify_trial(
            reasoning_text=synthetic_spam,
            content_text="",
            finish_reason="length",
            correct=False,
        )
        self.assertEqual(result.verdict, "degenerate")
        self.assertEqual(result.shape, "special_token_spam")

    def test_single_incidental_special_token_does_not_trigger(self):
        # Below SPECIAL_TOKEN_MIN_COUNT -- a single incidental mention (e.g. discussing
        # tokenizers) should not, by itself, be spam.
        text = ("This is a perfectly normal, fluent answer about tokenizer internals that "
                "happens to mention the literal string <unused24> exactly once in passing, "
                "and then goes on to explain several other unrelated implementation details "
                "at some length so the stream is long enough to exercise every other flag too.")
        self.assertEqual(text.count("<unused24>"), 1)
        result = longcontext.classify_trial(
            reasoning_text=text, content_text=text, finish_reason="stop", correct=True,
        )
        self.assertNotEqual(result.shape, "special_token_spam")


class TestSyntheticWrongButFluentShape(unittest.TestCase):
    """SYNTHETIC fixture -- clearly labeled: a coherent, fluent, well-formed answer that is
    simply incorrect (DeepSeek #25382-style "confident gibberish" -- here, confident WRONG, but
    not degenerate)."""

    def test_fluent_wrong_answer_classifies_wrong_not_degenerate(self):
        fluent_wrong = (
            "Based on the passage above, the reference code mentioned in the text is "
            "XK7-DELTA-9042, which appears clearly in the third paragraph of the provided "
            "context near the middle of the document."
        )
        result = longcontext.classify_trial(
            reasoning_text="", content_text=fluent_wrong, finish_reason="stop", correct=False,
        )
        self.assertEqual(result.verdict, "wrong")
        self.assertIsNone(result.shape)


class TestSyntheticTruncatedBenignShape(unittest.TestCase):
    """SYNTHETIC fixture -- clearly labeled: genuine on-topic reasoning that simply ran out of
    `max_tokens` before reaching content -- benign truncation, not degeneration."""

    def test_fluent_reasoning_cut_off_classifies_truncated_benign(self):
        fluent_reasoning = (
            "Let me work through this carefully. The passage above describes a person and "
            "their profession, and the question asks about a specific detail mentioned only "
            "in that passage. I need to find the exact phrase used to name the object "
            "described, which requires re-reading the relevant paragraph closely before I can "
            "give a confident final answer to the reader who is waiting for this response."
        )
        result = longcontext.classify_trial(
            reasoning_text=fluent_reasoning, content_text="", finish_reason="length", correct=False,
        )
        self.assertEqual(result.verdict, "truncated(benign)")
        self.assertIsNone(result.shape)


class TestHaystackVocabularyOverlapFlag(unittest.TestCase):
    """SYNTHETIC fixture with a KNOWN haystack (the real captured fragments' generating
    haystack is not recoverable -- fixture-source interpretation notes) -- exercises the
    haystack-vocabulary-overlap flag directly, confirming input-mirroring is detectable when the
    haystack IS known."""

    def test_mirroring_completion_has_high_overlap_with_its_haystack(self):
        haystack_text = (
            "quantum flux capacitor manifold resonance harmonic oscillator turbine gearbox "
            "sprocket lever piston valve regulator conduit "
        ) * 10
        mirroring_completion = (
            "flux manifold resonance turbine sprocket valve conduit regulator gearbox piston"
        )
        flags = longcontext.compute_flags(
            reasoning_text=mirroring_completion, content_text="", finish_reason="length",
            haystack_text=haystack_text,
        )
        self.assertIsNotNone(flags["haystack_vocabulary_overlap"])
        self.assertGreater(flags["haystack_vocabulary_overlap"], 0.8)

    def test_unrelated_completion_has_low_overlap_with_haystack(self):
        haystack_text = (
            "quantum flux capacitor manifold resonance harmonic oscillator turbine gearbox "
        ) * 10
        unrelated_completion = "The cat sat quietly on the warm windowsill in the afternoon sun."
        flags = longcontext.compute_flags(
            reasoning_text=unrelated_completion, content_text="", finish_reason="stop",
            haystack_text=haystack_text,
        )
        self.assertIsNotNone(flags["haystack_vocabulary_overlap"])
        self.assertLess(flags["haystack_vocabulary_overlap"], 0.3)

    def test_overlap_is_none_without_haystack_text(self):
        flags = longcontext.compute_flags(
            reasoning_text="some words here that are more than five tokens long",
            content_text="", finish_reason="stop",
        )
        self.assertIsNone(flags["haystack_vocabulary_overlap"])


class TestVerdictAndShapeConstants(unittest.TestCase):
    def test_verdicts_tuple_matches_plan_taxonomy(self):
        self.assertEqual(
            longcontext.VERDICTS, ("correct", "wrong", "degenerate", "truncated(benign)")
        )

    def test_degenerate_shapes_cover_the_three_named_shapes(self):
        self.assertEqual(
            set(longcontext.DEGENERATE_SHAPES), {"incoherent", "repetition", "special_token_spam"}
        )


if __name__ == "__main__":
    unittest.main()
