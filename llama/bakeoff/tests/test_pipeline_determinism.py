"""**The pipeline determinism lock.** Eight golden digests over the pure, deterministic core of
the bake-off measurement pipeline, plus a byte-shape lock on the frozen schema-v2 record writer.

Why this exists (WP-R2 of `plans/repository-tooling-structural-remediation.md`, review finding
F7): WP-R2 split `run_harness.py` and `longcontext.py` into eight modules. A green behavioral
test suite does **not** prove a deterministic pipeline is unchanged -- a refactor can preserve
every unit assertion while silently reordering an `rng` draw, changing a filler-text boundary, or
re-keying a dict, and every downstream record and stored `cell_id` would fork exactly the way an
alias rename forks consumer state (root `CLAUDE.md`, "The alias contract"). So the pipeline's
observable outputs are frozen by an explicit golden table, not by hoping.

The eight digests below were captured at commit `27e1153` (pre-WP-R2, the last commit before the
split) and are the contract. Each probe is a pure function of committed inputs -- no wall clock,
no `os.urandom`, no network -- so a digest that moves means the pipeline moved.

Every table entry has a paired NEGATIVE case proving the lock is not vacuous: perturbing the
probe's own input must change the digest. A lock that can never fail is not a lock.

This suite is entirely offline: it reads the committed corpus, computes hashes, and writes one
record into a `TemporaryDirectory`. No Docker, no network, no host sysfs, no live service.
"""

import hashlib
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import longcontext  # noqa: E402
import run_harness  # noqa: E402


def _digest(obj) -> str:
    """Stable 16-hex-char digest of any JSON-able structure (sorted keys, `str` fallback)."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------------
# The eight probes. Each is a zero-argument callable returning a digest string; the golden value
# beside it was measured at `27e1153`.
# --------------------------------------------------------------------------------------------

_HAYSTACK_31K_SEED = 20260821
_HAYSTACK_31K_TOKENS = 31000


def _haystack_31k():
    return longcontext.assemble_haystack(
        random.Random(_HAYSTACK_31K_SEED), _HAYSTACK_31K_TOKENS
    )


def probe_haystack_31k_text() -> str:
    return _text_digest(_haystack_31k().text)


def probe_haystack_31k_meta() -> str:
    return _digest({k: v for k, v in _haystack_31k()._asdict().items() if k != "text"})


def probe_trial_seeds() -> str:
    return _digest([
        [longcontext.derive_trial_seed("cell-x", i, p) for p in ("haystack", "sampler")]
        for i in range(8)
    ])


def probe_wilson_table() -> str:
    return _digest([
        longcontext.wilson_interval(k, n, s)
        for n in (10, 20, 50)
        for k in (0, 1, n // 2, n)
        for s in ("one", "two")
    ])


def probe_classifier_flags() -> str:
    return _digest(longcontext.compute_flags(
        "think " * 30, "the cat sat on the mat", "stop", _haystack_31k().text[:2000],
    ))


def probe_fixture_prompt() -> str:
    return _text_digest(run_harness.FIXTURE_PROMPT_TEXT)


def probe_summarize() -> str:
    return _digest(run_harness.summarize([1.0, 2.0, 3.0, None, 5.0]))


def probe_sampling_arm_payload() -> str:
    return _digest(run_harness.sampling_arm_payload(
        {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "min_p": None}, 1234,
    ))


# Golden table, measured at `27e1153`. Ordered as the WP-R2 brief states them.
GOLDEN_DIGESTS = (
    ("haystack_31k_text", probe_haystack_31k_text, "1c542e3333b096c0"),
    ("haystack_31k_meta", probe_haystack_31k_meta, "daa7534647fe84c2"),
    ("trial_seeds", probe_trial_seeds, "32c216a3e5145b39"),
    ("wilson_table", probe_wilson_table, "98f55825f5dabd05"),
    ("classifier_flags", probe_classifier_flags, "6677b7fe120d5307"),
    ("fixture_prompt", probe_fixture_prompt, "e0f4172e8babdf71"),
    ("summarize", probe_summarize, "5a05424cb289361b"),
    ("sampling_arm_payload", probe_sampling_arm_payload, "6be4d359ef40ef94"),
)

# Sizes are part of the same contract -- a digest match with a different length is impossible,
# but a length assertion localizes a failure to "the text changed size" vs "the text changed".
GOLDEN_SIZES = {
    "haystack_31k_text_chars": 139500,
    "fixture_prompt_chars": 36102,
}


class TestGoldenDigests(unittest.TestCase):
    """The table itself: every probe still produces its `27e1153` digest."""

    def test_every_probe_matches_its_golden_digest(self):
        for name, probe, expected in GOLDEN_DIGESTS:
            with self.subTest(probe=name):
                self.assertEqual(probe(), expected)

    def test_golden_sizes(self):
        self.assertEqual(
            len(_haystack_31k().text), GOLDEN_SIZES["haystack_31k_text_chars"]
        )
        self.assertEqual(
            len(run_harness.FIXTURE_PROMPT_TEXT), GOLDEN_SIZES["fixture_prompt_chars"]
        )

    def test_probes_are_reproducible_within_a_process(self):
        # Re-running a probe must return the same digest -- catches hidden per-call state
        # (a consumed module-level rng, a mutated cache) that a single call would miss.
        for name, probe, expected in GOLDEN_DIGESTS:
            with self.subTest(probe=name):
                self.assertEqual(probe(), probe())


class TestGoldenDigestsAreNotVacuous(unittest.TestCase):
    """The converse of every entry above: perturbing a probe's input MUST move its digest.

    Without these, a probe that silently degenerated to a constant (an exception swallowed into
    a default, a cache returning a stale value) would still "pass" the golden table forever.
    """

    def test_a_different_haystack_seed_changes_the_text_digest(self):
        other = longcontext.assemble_haystack(
            random.Random(_HAYSTACK_31K_SEED + 1), _HAYSTACK_31K_TOKENS
        )
        self.assertNotEqual(_text_digest(other.text), probe_haystack_31k_text())

    def test_a_different_target_size_changes_the_haystack_metadata_digest(self):
        other = longcontext.assemble_haystack(
            random.Random(_HAYSTACK_31K_SEED), _HAYSTACK_31K_TOKENS // 2
        )
        meta = {k: v for k, v in other._asdict().items() if k != "text"}
        self.assertNotEqual(_digest(meta), probe_haystack_31k_meta())

    def test_a_different_cell_id_changes_the_trial_seed_schedule(self):
        other = _digest([
            [longcontext.derive_trial_seed("cell-y", i, p) for p in ("haystack", "sampler")]
            for i in range(8)
        ])
        self.assertNotEqual(other, probe_trial_seeds())

    def test_haystack_and_sampler_purposes_never_collide(self):
        # The separate-seed-fields contract (`run_harness.needle_probe`'s docstring) as an
        # assertion, not prose: the two streams are distinct for every trial index.
        for i in range(32):
            with self.subTest(trial_index=i):
                self.assertNotEqual(
                    longcontext.derive_trial_seed("cell-x", i, "haystack"),
                    longcontext.derive_trial_seed("cell-x", i, "sampler"),
                )

    def test_a_different_k_over_n_changes_the_wilson_digest(self):
        other = _digest([
            longcontext.wilson_interval(k, n, s)
            for n in (11, 20, 50)
            for k in (0, 1, n // 2, n)
            for s in ("one", "two")
        ])
        self.assertNotEqual(other, probe_wilson_table())

    def test_degenerate_text_changes_the_classifier_flag_digest(self):
        other = _digest(longcontext.compute_flags(
            "<unused24> " * 30, "<unused24><unused24><unused24>", "length",
            _haystack_31k().text[:2000],
        ))
        self.assertNotEqual(other, probe_classifier_flags())

    def test_a_different_filler_seed_changes_the_fixture_digest(self):
        other = run_harness._build_filler_text(
            run_harness.FIXTURE_INPUT_TOKENS_TARGET, seed=run_harness.FIXTURE_SEED + 1
        ) + run_harness.FIXTURE_INSTRUCTION
        self.assertNotEqual(_text_digest(other), probe_fixture_prompt())

    def test_different_values_change_the_summarize_digest(self):
        other = _digest(run_harness.summarize([1.0, 2.0, 3.0, None, 6.0]))
        self.assertNotEqual(other, probe_summarize())

    def test_a_different_sampler_seed_changes_the_arm_payload_digest(self):
        other = _digest(run_harness.sampling_arm_payload(
            {"temperature": 0.7, "top_p": 0.9, "top_k": 40, "min_p": None}, 1235,
        ))
        self.assertNotEqual(other, probe_sampling_arm_payload())


# --------------------------------------------------------------------------------------------
# Schema-v2 record byte lock
# --------------------------------------------------------------------------------------------

RECORD_EXPERIMENT_ID = "wp-r2-byte-probe"

RECORD_CONFIG = {
    "alias": "probe-alias",
    "base_url": "http://127.0.0.1:4811/v1",
    "lineage": {
        "hf_repo": "org/model",
        "hf_revision": "abc123",
        "filename": "model.gguf",
        "file_size": 1234567,
        "sha256": "de" * 32,
        "llama_cpp_build": "b10534",
    },
    "configuration": {
        "fixture_input_tokens_target": 8000,
        "fixture_output_tokens": 256,
        "cold_rounds": 3,
        "warm_rounds": 3,
        "hot_rounds": 3,
        "needle_context_ceiling": 131072,
    },
    "status": "captured",
    "notes": ["probe record -- WP-R2 byte-identity check"],
}

RECORD_PROMPTS = [
    {"id": "q1", "category": "reasoning", "prompt": "unicode probe: café — ünïcode ✓"},
]

RECORD_COMPLETIONS = [
    {"type": "timing_round", "completion_ref": "cold-1", "round_index": 1, "run_state": "cold",
     "prompt_char_len": 36102, "content_text": "answer", "reasoning_text": "",
     "finish_reason": "stop"},
    {"type": "needle_probe", "size": "moderate", "expected_code": "XK7-DELTA-9042",
     "content_text": "XK7-DELTA-9042", "reasoning_text": "", "finish_reason": "stop"},
]

RECORD_SAMPLES = [
    {"type": "timing_round", "completion_ref": "cold-1", "success": True, "prompt_tokens": 8001,
     "cached_tokens": 0, "model_load_seconds": None, "first_request_e2e_ttft_ms": 120.5},
    {"type": "needle_probe", "schema_version": 2, "size": "moderate", "correct": True,
     "haystack_seed": 4242, "sampler_seed": None, "classifier_verdict": None},
]

# Digests measured at `27e1153` over the four files this fixture produces. `config.json`'s
# `timestamp` is the one field that legitimately varies between two runs, so it is replaced by a
# fixed sentinel before hashing; every other byte, including key insertion order and the
# `size_check` totals, is frozen.
GOLDEN_RECORD_FILE_DIGESTS = {
    "config.json": "57d01caf9decf15aeb724923d5cc06407c87a327be8787d9a1a2c0a9ee86f6a7",
    "prompts.jsonl": "2ba149c187d19da09e5a5a5708ee3497a36f458d9b6d850f67b2bbf6fbb10df7",
    "completions.jsonl": "d7e386b1929af2e5708e2722ae7ef2987d302026ed298934c1e9c461c195664d",
    "samples.jsonl": "c9518104d154667979afa724a480c054fe88c98bbfe562b10ef7d05d98e0edfd",
}

GOLDEN_RECORD_PROJECTED_BYTES = 1647
GOLDEN_RECORD_ACTUAL_BYTES = 1650

# The frozen schema-v2 `config.json` key order. `write_experiment_record` builds this by
# `dict(config)` + `setdefault` + assignment, so the order is a function of the code path, not of
# the caller's dict alone -- which is exactly why it is pinned here.
GOLDEN_CONFIG_KEY_ORDER = [
    "alias", "base_url", "lineage", "configuration", "status", "notes",
    "experiment_id", "timestamp", "schema_version", "sweep_cell", "wilson_summary", "size_check",
]

_TIMESTAMP_SENTINEL = "1970-01-01T00:00:00+00:00"


def _write_probe_record(base_dir: Path):
    return run_harness.write_experiment_record(
        RECORD_EXPERIMENT_ID, dict(RECORD_CONFIG), RECORD_PROMPTS, RECORD_COMPLETIONS,
        RECORD_SAMPLES, base_dir=base_dir,
    )


def _normalized_config_bytes(path: Path) -> bytes:
    """`config.json`'s bytes with only the `timestamp` VALUE replaced by a fixed sentinel --
    line structure, key order, indentation, and every other byte are left untouched."""
    text = path.read_text(encoding="utf-8")
    stamp = json.loads(text)["timestamp"]
    return text.replace(stamp, _TIMESTAMP_SENTINEL).encode("utf-8")


class TestSchemaV2RecordBytes(unittest.TestCase):
    """Record schema v2 is FROZEN: `write_experiment_record`'s output does not change by one
    byte. Everything except `config["timestamp"]` is pinned to its `27e1153` digest."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.result = _write_probe_record(self.base_dir)
        self.exp_dir = Path(self.result["experiment_dir"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_three_jsonl_files_are_byte_identical_to_their_golden_digests(self):
        for name in ("prompts.jsonl", "completions.jsonl", "samples.jsonl"):
            with self.subTest(file=name):
                got = hashlib.sha256((self.exp_dir / name).read_bytes()).hexdigest()
                self.assertEqual(got, GOLDEN_RECORD_FILE_DIGESTS[name])

    def test_config_json_is_byte_identical_once_the_timestamp_is_normalized(self):
        got = hashlib.sha256(_normalized_config_bytes(self.exp_dir / "config.json")).hexdigest()
        self.assertEqual(got, GOLDEN_RECORD_FILE_DIGESTS["config.json"])

    def test_config_json_key_order_is_frozen(self):
        with (self.exp_dir / "config.json").open("r", encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(list(config.keys()), GOLDEN_CONFIG_KEY_ORDER)
        self.assertEqual(config["schema_version"], 2)
        # Both nullable-with-explicit-null keys are always PRESENT, never omitted.
        self.assertIn("sweep_cell", config)
        self.assertIn("wilson_summary", config)
        self.assertIsNone(config["sweep_cell"])
        self.assertIsNone(config["wilson_summary"])

    def test_size_accounting_is_frozen(self):
        self.assertEqual(self.result["projected_bytes"], GOLDEN_RECORD_PROJECTED_BYTES)
        self.assertEqual(self.result["actual_bytes"], GOLDEN_RECORD_ACTUAL_BYTES)
        self.assertEqual(self.result["limit_bytes"], run_harness.SIZE_LIMIT_BYTES)

    def test_two_writes_differ_only_in_the_timestamp(self):
        # The falsifiable form of "the timestamp is the ONLY varying field".
        with tempfile.TemporaryDirectory() as other:
            other_dir = Path(_write_probe_record(Path(other))["experiment_dir"])
            for name in ("prompts.jsonl", "completions.jsonl", "samples.jsonl"):
                with self.subTest(file=name):
                    self.assertEqual(
                        (self.exp_dir / name).read_bytes(), (other_dir / name).read_bytes()
                    )
            self.assertEqual(
                _normalized_config_bytes(self.exp_dir / "config.json"),
                _normalized_config_bytes(other_dir / "config.json"),
            )

    def test_the_record_lock_is_not_vacuous(self):
        # Changing one sample byte must move the samples.jsonl digest.
        with tempfile.TemporaryDirectory() as other:
            mutated = [dict(RECORD_SAMPLES[0]), dict(RECORD_SAMPLES[1])]
            mutated[0]["prompt_tokens"] = 8002
            run_harness.write_experiment_record(
                RECORD_EXPERIMENT_ID, dict(RECORD_CONFIG), RECORD_PROMPTS, RECORD_COMPLETIONS,
                mutated, base_dir=Path(other),
            )
            got = hashlib.sha256(
                (Path(other) / RECORD_EXPERIMENT_ID / "samples.jsonl").read_bytes()
            ).hexdigest()
            self.assertNotEqual(got, GOLDEN_RECORD_FILE_DIGESTS["samples.jsonl"])


class TestPublicSurfaceStillResolvesAtItsHistoricalHome(unittest.TestCase):
    """WP-R2 moved code into eight modules; every name callers outside `llama/bakeoff/` (and
    every sibling module inside it) reach for must still resolve as `run_harness.<name>` /
    `longcontext.<name>`. A missing re-export is an import-time break for `batch.py`,
    `cell_runner.py`, `verify_run.py`, or a test -- caught here as a flat list, not incidentally.

    The two underscore-prefixed entries are deliberate and load-bearing: the suite rebinds
    `run_harness._execute_streaming_request` and `run_harness.tokenize_count` as injected fakes,
    and every live caller resolves them through the module object at call time.
    """

    RUN_HARNESS_SURFACE = (
        "CHARS_PER_TOKEN_ESTIMATE", "DEFAULT_BASE_URL", "DEFAULT_ROUTER_BASE_URL",
        "DEFAULT_RUNS_DIR", "FIXTURE_INPUT_TOKENS_TARGET", "FIXTURE_OUTPUT_TOKENS",
        "FIXTURE_PROMPT_TEXT", "NEEDLE_NEAR_CEILING_FRACTION", "REQUIRED_LINEAGE_FIELDS",
        "SAMPLING_ARM_CAPABILITY", "SAMPLING_ARM_PRODUCTION", "SIZE_LIMIT_BYTES",
        "SizeBudgetExceeded", "_build_filler_text", "_execute_streaming_request",
        "_generate_reference_code", "calibrate_chars_per_token", "compute_file_lineage",
        "fetch_router_logs", "load_quality_corpus", "needle_probe", "resolve_llama_cpp_build",
        "run_quality_corpus", "run_round", "run_sweep", "sampling_arm_payload", "summarize",
        "tokenize_count", "urllib", "write_experiment_record",
    )

    LONGCONTEXT_SURFACE = (
        "CellResult", "DEGENERATE_SHAPES", "HaystackOverBudgetError",
        "MIN_TOKENS_FOR_COHERENCE_CHECK", "MIN_TRIALS_FOR_WILSON", "MultiNeedleTask",
        "PASSAGE_QA_ITEMS", "POSITION_FRACTIONS", "PassageQATask", "SingleNeedleTask", "TASKS",
        "VERDICTS", "_assemble_haystack_fit", "_select_contiguous_passages", "assemble_haystack",
        "build_live_request_fn", "classify_trial", "compute_flags", "derive_trial_seed",
        "insert_at_fraction", "insert_multiple", "load_book_text", "load_corpus_manifest",
        "run_cell", "run_harness", "run_live_battery", "run_position_sweep", "wilson_interval",
    )

    def test_run_harness_surface(self):
        for name in self.RUN_HARNESS_SURFACE:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(run_harness, name), f"run_harness.{name} is missing")

    def test_longcontext_surface(self):
        for name in self.LONGCONTEXT_SURFACE:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(longcontext, name), f"longcontext.{name} is missing")

    def test_rebinding_the_transport_seam_is_still_observed_by_live_callers(self):
        # `tests/test_run_cell.py` relies on this; asserted here as the split's own invariant.
        original = run_harness._execute_streaming_request
        try:
            run_harness._execute_streaming_request = lambda *a, **k: {"sentinel": True}
            request_fn = longcontext.build_live_request_fn("http://192.0.2.1:1", "alias")
            self.assertEqual(request_fn("prompt", 8, {}), {"sentinel": True})
        finally:
            run_harness._execute_streaming_request = original


class TestCliExitCodeContract(unittest.TestCase):
    """`run_harness.py`'s exit codes are part of its contract: `needle` exits 1 on a wrong
    recall, every other subcommand exits 0. WP-R2 moved `main()` into `harness_cli.py`, and a
    `main()` that parsed correctly but dropped its dispatch return value would still produce
    identical `--help` output and pass every other test in this suite while silently reporting
    success for a failed probe. Locked here, offline: the probe functions are replaced by
    injected fakes, so no request is ever made.
    """

    def setUp(self):
        import harness_cli

        self.cli = harness_cli
        self._saved = {
            name: getattr(harness_cli, name)
            for name in ("needle_probe", "load_quality_corpus", "run_quality_corpus")
        }

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.cli, name, value)

    def test_needle_exits_1_when_the_recall_is_wrong(self):
        self.cli.needle_probe = lambda *a, **k: ({"correct": False}, {})
        self.assertEqual(
            self.cli.main(["needle", "--alias", "a", "--size", "moderate"]), 1
        )

    def test_needle_exits_0_when_the_recall_is_correct(self):
        self.cli.needle_probe = lambda *a, **k: ({"correct": True}, {})
        self.assertEqual(
            self.cli.main(["needle", "--alias", "a", "--size", "moderate"]), 0
        )

    def test_quality_exits_0(self):
        self.cli.load_quality_corpus = lambda *a, **k: []
        self.cli.run_quality_corpus = lambda *a, **k: ([], [])
        self.assertEqual(self.cli.main(["quality", "--alias", "a"]), 0)

    def test_run_harness_still_exposes_the_cli_entry_points_it_documents(self):
        # `llama/bakeoff/README.md` documents `python3 llama/bakeoff/run_harness.py <cmd>`;
        # the module-level `main`/`build_arg_parser` live in `harness_cli` now.
        self.assertTrue(callable(self.cli.main))
        self.assertTrue(callable(self.cli.build_arg_parser))
        parser = self.cli.build_arg_parser()
        self.assertEqual(parser.prog, "run_harness.py")


if __name__ == "__main__":
    unittest.main()
