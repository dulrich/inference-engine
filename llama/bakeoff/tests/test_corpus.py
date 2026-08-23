"""Corpus manifest integrity: every book's sha256/char_count matches its committed file, the
manifest covers >=4 distinct domains, and the total corpus directory stays inside the 8 MiB
budget (plan `quant-sweep-harness-and-instrument-v2.md` WP-3)."""

import hashlib
import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402

CORPUS_SIZE_LIMIT_BYTES = 8 * 1024 * 1024


class TestCorpusManifest(unittest.TestCase):
    def setUp(self):
        self.manifest = longcontext.load_corpus_manifest()

    def test_manifest_has_at_least_four_books(self):
        self.assertGreaterEqual(len(self.manifest["books"]), 4)

    def test_manifest_covers_distinct_domains(self):
        domains = {b["domain"] for b in self.manifest["books"]}
        self.assertGreaterEqual(len(domains), 4, f"expected >=4 distinct domains, got {domains}")

    def test_every_book_sha256_matches_committed_file(self):
        for book in self.manifest["books"]:
            path = longcontext.CORPUS_DIR / book["file"]
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(
                digest, book["sha256"],
                f"{book['id']}: recomputed sha256 does not match manifest.json",
            )

    def test_every_book_char_count_matches_committed_file(self):
        for book in self.manifest["books"]:
            path = longcontext.CORPUS_DIR / book["file"]
            text = path.read_text(encoding="utf-8")
            self.assertEqual(len(text), book["char_count"])

    def test_every_book_required_fields_present(self):
        required = {"id", "title", "author", "source_url", "sha256", "license_note", "char_count"}
        for book in self.manifest["books"]:
            missing = required - set(book)
            self.assertFalse(missing, f"{book.get('id')}: missing manifest fields {missing}")

    def test_total_corpus_size_within_budget(self):
        total = sum(
            (longcontext.CORPUS_DIR / book["file"]).stat().st_size for book in self.manifest["books"]
        )
        total += (longcontext.MANIFEST_PATH).stat().st_size
        self.assertLessEqual(total, CORPUS_SIZE_LIMIT_BYTES)

    def test_load_book_text_matches_direct_read(self):
        book = self.manifest["books"][0]
        via_loader = longcontext.load_book_text(book["id"], manifest=self.manifest)
        direct = (longcontext.CORPUS_DIR / book["file"]).read_text(encoding="utf-8")
        self.assertEqual(via_loader, direct)

    def test_load_book_text_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            longcontext.load_book_text("not-a-real-book-id", manifest=self.manifest)


if __name__ == "__main__":
    unittest.main()
