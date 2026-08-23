"""Deterministic haystack assembly + needle/passage insertion (plan WP-3: `haystack_seed`
drives ALL haystack randomness; seeded contiguous-passage selection; sentence-boundary-snapped
injection, single and multi-point)."""

import random
import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402


class TestSelectContiguousPassages(unittest.TestCase):
    """Exercises the disk-free chunking core directly against a tiny synthetic corpus -- this
    is what forces the multi-book concatenation path, which the real corpus (each book far
    larger than any realistic sweep target) never needs to exercise on its own."""

    def setUp(self):
        self.books = {
            "alpha": "AAAA " * 20,   # 100 chars
            "bravo": "BBBB " * 20,   # 100 chars
            "charlie": "CCCC " * 20,  # 100 chars
        }
        self.order = ["alpha", "bravo", "charlie"]

    def test_single_book_covers_small_target(self):
        rng = random.Random(1)
        text, book_ids = longcontext._select_contiguous_passages(self.books, self.order, 50, rng)
        self.assertEqual(len(text), 50)
        self.assertEqual(book_ids, ("alpha",))

    def test_multi_book_concatenation_when_target_exceeds_one_book(self):
        rng = random.Random(1)
        target = 250  # exceeds any single 100-char synthetic book -- forces >=3 passages
        text, book_ids = longcontext._select_contiguous_passages(self.books, self.order, target, rng)
        self.assertEqual(len(book_ids), 3)
        self.assertEqual(set(book_ids), set(self.order))
        # 3 passages joined by "\n\n" (2 chars each) -- total length is passage chars + separators
        self.assertEqual(len(text) - 2 * 2, sum(len(p) for p in text.split("\n\n")))

    def test_zero_target_returns_empty(self):
        rng = random.Random(1)
        text, book_ids = longcontext._select_contiguous_passages(self.books, self.order, 0, rng)
        self.assertEqual(text, "")
        self.assertEqual(book_ids, ())

    def test_empty_book_order_returns_empty(self):
        rng = random.Random(1)
        text, book_ids = longcontext._select_contiguous_passages(self.books, [], 50, rng)
        self.assertEqual(text, "")
        self.assertEqual(book_ids, ())


class TestAssembleHaystack(unittest.TestCase):
    def test_deterministic_same_seed_same_output(self):
        rng1 = random.Random(20260823)
        rng2 = random.Random(20260823)
        h1 = longcontext.assemble_haystack(rng1, target_tokens=2000)
        h2 = longcontext.assemble_haystack(rng2, target_tokens=2000)
        self.assertEqual(h1.text, h2.text)
        self.assertEqual(h1.book_ids, h2.book_ids)

    def test_different_seed_different_output(self):
        rng1 = random.Random(1)
        rng2 = random.Random(2)
        h1 = longcontext.assemble_haystack(rng1, target_tokens=2000)
        h2 = longcontext.assemble_haystack(rng2, target_tokens=2000)
        self.assertNotEqual(h1.text, h2.text)

    def test_char_count_approximately_matches_target(self):
        rng = random.Random(42)
        target_tokens = 5000
        h = longcontext.assemble_haystack(rng, target_tokens=target_tokens)
        expected_chars = int(target_tokens * longcontext.run_harness.CHARS_PER_TOKEN_ESTIMATE)
        self.assertEqual(h.char_count, expected_chars)

    def test_uses_only_manifest_books(self):
        manifest = longcontext.load_corpus_manifest()
        valid_ids = {b["id"] for b in manifest["books"]}
        rng = random.Random(7)
        h = longcontext.assemble_haystack(rng, target_tokens=3000, manifest=manifest)
        self.assertTrue(set(h.book_ids) <= valid_ids)
        self.assertTrue(len(h.book_ids) >= 1)


class TestInsertion(unittest.TestCase):
    def test_insert_at_fraction_contains_injected_text(self):
        haystack = "One. Two. Three. Four. Five. " * 20
        assembled, offset = longcontext.insert_at_fraction(haystack, 0.5, "NEEDLE. ")
        self.assertIn("NEEDLE. ", assembled)
        self.assertEqual(assembled[offset:offset + len("NEEDLE. ")], "NEEDLE. ")

    def test_insert_at_fraction_near_zero_and_one(self):
        haystack = "One. Two. Three. Four. Five. " * 20
        assembled_start, _ = longcontext.insert_at_fraction(haystack, 0.0, "NEEDLE. ")
        assembled_end, _ = longcontext.insert_at_fraction(haystack, 0.98, "NEEDLE. ")
        self.assertIn("NEEDLE. ", assembled_start)
        self.assertIn("NEEDLE. ", assembled_end)

    def test_insert_multiple_preserves_ascending_order(self):
        haystack = "One. Two. Three. Four. Five. " * 40
        fractions = [0.10, 0.13, 0.16]
        injections = ["FIRST. ", "SECOND. ", "THIRD. "]
        assembled, boundaries = longcontext.insert_multiple(haystack, fractions, injections)
        first_idx = assembled.find("FIRST. ")
        second_idx = assembled.find("SECOND. ")
        third_idx = assembled.find("THIRD. ")
        self.assertTrue(first_idx < second_idx < third_idx)
        self.assertEqual(len(boundaries), 3)

    def test_insert_multiple_mismatched_lengths_raises(self):
        with self.assertRaises(ValueError):
            longcontext.insert_multiple("some text. ", [0.1, 0.2], ["only one injection"])

    def test_insert_multiple_descending_fractions_raises(self):
        haystack = "One. Two. Three. Four. Five. " * 20
        with self.assertRaises(ValueError):
            longcontext.insert_multiple(haystack, [0.5, 0.2], ["A. ", "B. "])

    def test_insert_multiple_tied_boundaries_preserve_ascending_order(self):
        """Regression test: two fractions that snap to the EXACT SAME sentence boundary (a real
        tie, not just close) must still come out in ascending-index left-to-right order in the
        final text -- verified live against this module's real corpus at a 0.03 fraction spread,
        where two of three multi-needle insertion points landed on the same boundary. A naive
        stable sort-by-boundary-value approach silently inverted this tie; the fix processes
        insertions in strict descending-index order instead (see `insert_multiple`'s docstring).
        """
        # A single long sentence with no ". " inside it -- both interior fractions snap to the
        # SAME boundary (the one ". " after the long run).
        long_sentence = "word " * 200
        haystack = long_sentence + ". " + ("Tail sentence here. " * 10)
        fractions = [0.05, 0.30, 0.35]  # 0.30 and 0.35 both fall inside the long sentence
        injections = ["FIRST. ", "SECOND. ", "THIRD. "]
        assembled, boundaries = longcontext.insert_multiple(haystack, fractions, injections)
        self.assertEqual(boundaries[1], boundaries[2], "test setup expected a real tie")
        first_idx = assembled.find("FIRST. ")
        second_idx = assembled.find("SECOND. ")
        third_idx = assembled.find("THIRD. ")
        self.assertTrue(
            first_idx < second_idx < third_idx,
            f"expected ascending order, got FIRST={first_idx} SECOND={second_idx} THIRD={third_idx}",
        )


if __name__ == "__main__":
    unittest.main()
