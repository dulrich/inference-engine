"""Deterministic haystack assembly + needle/passage insertion (plan WP-3: `haystack_seed`
drives ALL haystack randomness; seeded contiguous-passage selection; sentence-boundary-snapped
injection, single and multi-point). Also covers the longcontext-pow2-overnight-campaign WP-3
512k-regime extension: real-corpus-v2 assertions on the actual randomized-offset and
bounded-overlap properties (audit M1), and the v1-reproducibility guard."""

import hashlib
import itertools
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


class _TrackedText:
    """Test-only wrapper recording every char-range slice `_select_contiguous_passages` takes
    from a book's text, so the 512k-regime tests below can measure the REAL selected-range/
    content overlap directly (audit M1: assert the actual property, not sha256 inequality)
    without depending on internal rng-call bookkeeping, and without splitting the assembled
    haystack back into passages on `"\\n\\n"` -- unsafe, since real Gutenberg prose already
    contains blank-line paragraph breaks that would corrupt a naive split. Duck-types just
    enough (`__len__`, slice `__getitem__`) for `_select_contiguous_passages` to use it exactly
    like a plain `str`."""

    def __init__(self, text):
        self._text = text
        self.slices = []  # [(start, stop), ...] in selection order

    def __len__(self):
        return len(self._text)

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            stop = key.stop if key.stop is not None else len(self._text)
            self.slices.append((start, stop))
        return self._text[key]


_TARGET_TOKENS_512K = 524288  # 512 * 1024 -- the plan's tested 512k-token-equivalent regime


def _select_512k_tracked(manifest, seed):
    """Runs `_select_contiguous_passages` against the REAL on-disk corpus (no network, plain
    file reads via `load_book_text`) at the 512k-token-equivalent char target, wrapped in
    `_TrackedText` so every char range drawn from each book is recorded. Returns
    `(tracked_texts, book_ids, haystack_char_count)`."""
    real_texts = {b["id"]: longcontext.load_book_text(b["id"], manifest) for b in manifest["books"]}
    tracked = {bid: _TrackedText(t) for bid, t in real_texts.items()}
    rng = random.Random(seed)
    order = list(tracked.keys())
    rng.shuffle(order)
    target_chars = int(_TARGET_TOKENS_512K * longcontext.run_harness.CHARS_PER_TOKEN_ESTIMATE)
    text, book_ids = longcontext._select_contiguous_passages(tracked, order, target_chars, rng)
    return tracked, book_ids, len(text)


class TestHaystack512kRegime(unittest.TestCase):
    """512k-token-equivalent (524288-token) regime against the real on-disk corpus v2
    (`gutenberg-longcontext-haystack-v2`) -- asserts the actual randomized-offset and
    bounded-overlap PROPERTIES the passage-selection fix guarantees, not sha256 inequality alone
    (audit M1: seeded book-order shuffling alone already made sha256 differ across seeds on the
    PRE-FIX code, so a digest-only test passed vacuously even when every book but one collapsed
    to the identical whole-book, offset-zero content on every seed)."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = longcontext.load_corpus_manifest()
        if cls.manifest["corpus_id"] != "gutenberg-longcontext-haystack-v2":
            raise AssertionError(
                "expected the default corpus manifest to be gutenberg-longcontext-haystack-v2, "
                f"got {cls.manifest['corpus_id']!r}"
            )

    def test_start_offsets_randomized_across_books(self):
        """Across several seeds, the large majority of per-book passage draws where the book is
        LONGER than its slice (the case the fix targets -- see `_select_contiguous_passages`'
        docstring) land at a nonzero start offset. Checked as a distribution over many draws
        across seeds/books (audit M1), not a single lucky draw."""
        eligible = []  # (seed, book_id, start) where stop-start < len(book) -- offset was free
        nonzero = 0
        for seed in (11, 22, 33, 44, 55):
            tracked, _book_ids, _char_count = _select_512k_tracked(self.manifest, seed)
            for bid, tt in tracked.items():
                for (start, stop) in tt.slices:
                    if stop - start < len(tt):
                        eligible.append((seed, bid, start))
                        if start != 0:
                            nonzero += 1
        # The fix must actually engage across MULTIPLE books per haystack (not degrade every
        # book but one to a forced whole-book take, which is what the pre-fix code did).
        self.assertGreaterEqual(
            len(eligible), 20,
            f"too few book>slice draws to assert a distribution (got {len(eligible)}): {eligible}",
        )
        nonzero_fraction = nonzero / len(eligible)
        self.assertGreater(
            nonzero_fraction, 0.9,
            f"expected >90% nonzero offsets among book>slice draws, got {nonzero}/{len(eligible)}",
        )

    def test_cross_seed_passage_content_overlap_bounded(self):
        """Selected char-range overlap between different seeds' 512k haystacks stays bounded --
        measured on the actual selected source ranges, not digests (audit M1).

        Bound: overlap chars / min(haystack char count) < 0.75. Chosen with margin above the
        fixed algorithm's measured range against the v2 corpus (~0.11-0.67 over 100 sampled seed
        pairs during this WP's own validation) and comfortably below the PRE-FIX algorithm's
        measured overlap against the old v1 corpus at the same 512k target (~0.85-0.97, since
        the pre-fix code reused the same whole book, verbatim, for all but one book on every
        seed) -- so this bound is both realistic for the fixed algorithm and would have caught
        the original bug."""
        seeds = (101, 202, 303, 404)
        for seed_a, seed_b in itertools.combinations(seeds, 2):
            tracked_a, _, len_a = _select_512k_tracked(self.manifest, seed_a)
            tracked_b, _, len_b = _select_512k_tracked(self.manifest, seed_b)
            overlap = 0
            for bid in tracked_a:
                for (sa, ea) in tracked_a[bid].slices:
                    for (sb, eb) in tracked_b[bid].slices:
                        overlap += max(0, min(ea, eb) - max(sa, sb))
            frac = overlap / min(len_a, len_b)
            self.assertLess(
                frac, 0.75,
                f"seeds {seed_a},{seed_b}: cross-seed overlap fraction {frac:.4f} exceeded bound",
            )

    def test_distinct_sha256_across_at_least_three_seeds(self):
        """Weakest of the three 512k-regime assertions (audit M1) -- kept for regression
        coverage, not load-bearing on its own: seeded book-order shuffling alone already makes
        this pass on the pre-fix code, which is exactly why the two assertions above exist."""
        digests = set()
        for seed in (1, 2, 3):
            rng = random.Random(seed)
            h = longcontext.assemble_haystack(rng, target_tokens=_TARGET_TOKENS_512K, manifest=self.manifest)
            digests.add(hashlib.sha256(h.text.encode("utf-8")).hexdigest())
        self.assertEqual(len(digests), 3)


class TestV1CorpusReproducibility(unittest.TestCase):
    """v1 corpus (`gutenberg-longcontext-haystack-v1`) must stay loadable byte-for-byte, and its
    haystack assembly must stay deterministic, so records produced under it remain reproducible
    even though the module's DEFAULT manifest is now v2 (`load_corpus_manifest`'s own module-
    level cache is keyed by the `path` argument, so requesting v1 explicitly never touches the
    v2 default)."""

    def test_v1_manifest_loadable_and_distinct_from_default(self):
        v1_path = str(longcontext.CORPUS_DIR / "manifest-v1.json")
        v1 = longcontext.load_corpus_manifest(v1_path)
        self.assertEqual(v1["corpus_id"], "gutenberg-longcontext-haystack-v1")
        self.assertEqual(len(v1["books"]), 5)
        default = longcontext.load_corpus_manifest()
        self.assertEqual(default["corpus_id"], "gutenberg-longcontext-haystack-v2")
        self.assertGreaterEqual(len(default["books"]), 12)

    def test_v1_haystack_assembly_still_deterministic(self):
        v1 = longcontext.load_corpus_manifest(str(longcontext.CORPUS_DIR / "manifest-v1.json"))
        rng1 = random.Random(20260823)
        rng2 = random.Random(20260823)
        h1 = longcontext.assemble_haystack(rng1, target_tokens=2000, manifest=v1)
        h2 = longcontext.assemble_haystack(rng2, target_tokens=2000, manifest=v1)
        self.assertEqual(h1.text, h2.text)
        self.assertEqual(h1.book_ids, h2.book_ids)
        valid_v1_ids = {b["id"] for b in v1["books"]}
        self.assertTrue(set(h1.book_ids) <= valid_v1_ids)


if __name__ == "__main__":
    unittest.main()
