"""WP-1 (`plans/small-dense-decode-roofline-spike.md`): the timing arm's two additive,
non-identity depth/length knobs (`timing_prompt_tokens`/`timing_max_tokens`) and the optional
`config.json` `timing_geometry` record field they feed.

Covers, in order: queue-file validation (rejects a malformed override, accepts a positive int or
absence, phrased like `timing_rounds`); `resolve_cell` threading the two knobs onto
`QueueCellMetadata` without perturbing `cell_id`; `cell_runner.resolve_timing_prompt`/
`resolve_timing_max_tokens` (defaults preserved byte-for-byte, fixture filler at the requested
target with the pinned seed and the pinned continuation instruction appended); `run_timing_rounds`
sending the resolved prompt/cap to every round and stamping the four `timing_geometry` fields on
`TimingArm.detail`; and `write_cell_record` -- `timing_geometry` omitted when neither knob is set
(byte-identical to a pre-WP-1 record), present and correctly shaped when at least one is, and
`timing_summary` never written under any of those conditions.

No live Docker/GPU execution anywhere in this suite -- `batch_fixtures`' fakes throughout, same
as every other module in this split."""

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from batch_fixtures import _handle, _longcontext_cell, _minimal_queue  # noqa: E402

import batch  # noqa: E402
import cell_record  # noqa: E402
import cell_runner  # noqa: E402
import queue_file  # noqa: E402
import run_harness  # noqa: E402


def _cell_with_knobs(timing_prompt_tokens=None, timing_max_tokens=None, **kw):
    """A `_longcontext_cell` (the shared runner/record fixture) with the two new knobs set on
    its `queue` value -- `QueueCellMetadata` is frozen, so `dataclasses.replace` is the only way
    to attach them, exactly the pattern `test_cell_identity.py`'s own variant table uses."""
    cell = _longcontext_cell(**kw)
    return dataclasses.replace(
        cell,
        queue=dataclasses.replace(
            cell.queue,
            timing_prompt_tokens=timing_prompt_tokens,
            timing_max_tokens=timing_max_tokens,
        ),
    )


def _capturing_round_fn():
    """Stand-in for `run_harness.run_round`, recording every call's `(prompt, max_tokens)` and
    returning the minimal `(sample, completion)` shape `run_timing_rounds` needs."""
    calls = []

    def _fn(base_url, alias, run_state, round_index, prompt, max_tokens, timeout=180):
        calls.append({
            "base_url": base_url, "alias": alias, "run_state": run_state,
            "round_index": round_index, "prompt": prompt, "max_tokens": max_tokens,
            "timeout": timeout,
        })
        sample = {
            "type": "timing_round", "round_index": round_index, "run_state": run_state,
            "success": True,
        }
        completion = {
            "type": "timing_round", "round_index": round_index, "run_state": run_state,
            "prompt_char_len": len(prompt), "content_text": "text",
        }
        return sample, completion

    _fn.calls = calls
    return _fn


class TestQueueFileValidation(unittest.TestCase):
    """`validate_queue` on the two new abbreviated-cell keys -- same optional-and-validated-
    when-present shape as `timing_rounds` (`queue_file.py:151`)."""

    def test_rejects_malformed_timing_prompt_tokens(self):
        for bad in (0, -1, 2.5, "8000", True):
            with self.subTest(bad=bad):
                queue = _minimal_queue()
                queue["cells"][0]["timing_prompt_tokens"] = bad
                with self.assertRaises(queue_file.QueueValidationError):
                    queue_file.validate_queue(queue)

    def test_rejects_malformed_timing_max_tokens(self):
        for bad in (0, -1, 2.5, "256", True):
            with self.subTest(bad=bad):
                queue = _minimal_queue()
                queue["cells"][0]["timing_max_tokens"] = bad
                with self.assertRaises(queue_file.QueueValidationError):
                    queue_file.validate_queue(queue)

    def test_accepts_a_positive_int_or_absence_for_both_keys(self):
        queue = _minimal_queue()
        queue_file.validate_queue(queue)  # absent -- must not raise
        queue["cells"][0]["timing_prompt_tokens"] = 8000
        queue["cells"][0]["timing_max_tokens"] = 256
        queue_file.validate_queue(queue)  # present, positive -- must not raise


class TestResolveCellThreadsKnobsNonIdentity(unittest.TestCase):
    """`resolve_cell`/`resolve_queue_cells`: the two knobs land on `QueueCellMetadata` and never
    perturb `cell_id` -- the same proof `test_cell_identity.py` runs for `timing_rounds`."""

    def test_resolved_cell_carries_the_knobs_without_changing_cell_id(self):
        plain = queue_file.resolve_queue_cells(_minimal_queue())
        queue = _minimal_queue()
        queue["cells"][0]["timing_prompt_tokens"] = 8000
        queue["cells"][0]["timing_max_tokens"] = 256
        with_knobs = queue_file.resolve_queue_cells(queue)
        for before, after in zip(plain, with_knobs):
            self.assertEqual(before.cell_id, after.cell_id)
        self.assertEqual(with_knobs[0].queue.timing_prompt_tokens, 8000)
        self.assertEqual(with_knobs[0].queue.timing_max_tokens, 256)
        self.assertIsNone(plain[0].queue.timing_prompt_tokens)
        self.assertIsNone(plain[0].queue.timing_max_tokens)

    def test_absent_knobs_resolve_to_none_on_every_cell(self):
        for cell in queue_file.resolve_queue_cells(_minimal_queue()):
            self.assertIsNone(cell.queue.timing_prompt_tokens)
            self.assertIsNone(cell.queue.timing_max_tokens)


class TestResolveTimingPromptAndMaxTokens(unittest.TestCase):
    """`cell_runner.resolve_timing_prompt`/`resolve_timing_max_tokens` -- the pure resolution
    functions `run_timing_rounds` calls once per timing arm."""

    def test_absent_or_malformed_target_keeps_the_fixed_short_prompt(self):
        for bad in (None, 0, -1, 2.5, "8000", True):
            with self.subTest(bad=bad):
                prompt, source = cell_runner.resolve_timing_prompt(bad)
                self.assertEqual(prompt, cell_runner.TIMING_PROMPT)
                self.assertEqual(source, "fixed-short")

    def test_absent_or_malformed_override_keeps_64_max_tokens(self):
        for bad in (None, 0, -1, 2.5, "256", True):
            with self.subTest(bad=bad):
                self.assertEqual(cell_runner.resolve_timing_max_tokens(bad), cell_runner.TIMING_MAX_TOKENS)
        self.assertEqual(cell_runner.TIMING_MAX_TOKENS, 64)

    def test_positive_override_returns_it_verbatim(self):
        self.assertEqual(cell_runner.resolve_timing_max_tokens(256), 256)

    def test_positive_target_builds_fixture_filler_at_the_pinned_seed_plus_instruction(self):
        prompt, source = cell_runner.resolve_timing_prompt(8000)
        self.assertEqual(source, "fixture-filler")
        expected = (
            run_harness._build_filler_text(8000, seed=run_harness.FIXTURE_SEED)
            + cell_runner.TIMING_DEPTH_INSTRUCTION
        )
        self.assertEqual(prompt, expected)
        self.assertTrue(prompt.endswith(
            "\n\nContinue the text above in the same style for as long as you can, without "
            "stopping or summarizing."
        ))

    def test_fixture_filler_is_deterministic_across_calls(self):
        first, _ = cell_runner.resolve_timing_prompt(500)
        second, _ = cell_runner.resolve_timing_prompt(500)
        self.assertEqual(first, second)

    def test_a_different_target_changes_the_filler_text(self):
        short, _ = cell_runner.resolve_timing_prompt(500)
        long_, _ = cell_runner.resolve_timing_prompt(8000)
        self.assertNotEqual(short, long_)


class TestRunTimingRoundsSendsResolvedPromptAndStampsDetail(unittest.TestCase):
    """`run_timing_rounds`: every round of one cell's timing pass uses the SAME resolved
    prompt/cap, and `TimingArm.detail` carries the four `timing_geometry` fields unconditionally
    (whether they end up on the record is `cell_runner._timing_geometry_for_record`'s decision,
    covered below)."""

    def test_default_cell_sends_the_pre_wp1_fixed_prompt_and_cap(self):
        round_fn = _capturing_round_fn()
        cell = _cell_with_knobs(battery=["timing"])
        result = cell_runner.run_timing_rounds(_handle(), cell, round_fn=round_fn)
        self.assertEqual({c["prompt"] for c in round_fn.calls}, {cell_runner.TIMING_PROMPT})
        self.assertEqual({c["max_tokens"] for c in round_fn.calls}, {cell_runner.TIMING_MAX_TOKENS})
        self.assertIsNone(result.detail["prompt_tokens_target"])
        self.assertEqual(result.detail["max_tokens"], cell_runner.TIMING_MAX_TOKENS)
        self.assertEqual(result.detail["prompt_source"], "fixed-short")
        self.assertFalse(result.detail["instruction_pinned"])

    def test_depth_cell_sends_fixture_filler_and_the_requested_cap_every_round(self):
        round_fn = _capturing_round_fn()
        cell = _cell_with_knobs(
            timing_prompt_tokens=8000, timing_max_tokens=256, battery=["timing"],
        )
        result = cell_runner.run_timing_rounds(_handle(), cell, round_fn=round_fn)
        expected_prompt, _ = cell_runner.resolve_timing_prompt(8000)
        self.assertEqual({c["prompt"] for c in round_fn.calls}, {expected_prompt})
        self.assertEqual({c["max_tokens"] for c in round_fn.calls}, {256})
        self.assertEqual(len(round_fn.calls), 3)  # TIMING_DEFAULT_ROUNDS, unaffected by the knobs
        self.assertEqual(result.detail["prompt_tokens_target"], 8000)
        self.assertEqual(result.detail["max_tokens"], 256)
        self.assertEqual(result.detail["prompt_source"], "fixture-filler")
        self.assertTrue(result.detail["instruction_pinned"])

    def test_only_one_knob_set_still_resolves_the_other_to_its_default(self):
        round_fn = _capturing_round_fn()
        cell = _cell_with_knobs(timing_prompt_tokens=500, battery=["timing"])
        result = cell_runner.run_timing_rounds(_handle(), cell, round_fn=round_fn)
        self.assertEqual({c["max_tokens"] for c in round_fn.calls}, {cell_runner.TIMING_MAX_TOKENS})
        self.assertEqual(result.detail["prompt_source"], "fixture-filler")
        self.assertEqual(result.detail["max_tokens"], cell_runner.TIMING_MAX_TOKENS)


class TestTimingGeometryForRecord(unittest.TestCase):
    """`cell_runner._timing_geometry_for_record`: the omit-vs-emit decision, isolated from
    `write_cell_record` itself."""

    def test_no_timing_arm_omits_the_block(self):
        cell = _cell_with_knobs(battery=["vram"])
        self.assertIsNone(cell_runner._timing_geometry_for_record(cell, cell_runner.NO_TIMING))

    def test_timing_arm_with_neither_knob_omits_the_block(self):
        cell = _cell_with_knobs(battery=["timing"])
        timing = cell_runner.run_timing_rounds(_handle(), cell, round_fn=_capturing_round_fn())
        self.assertIsNone(cell_runner._timing_geometry_for_record(cell, timing))

    def test_timing_arm_with_a_knob_set_emits_the_four_fields(self):
        cell = _cell_with_knobs(timing_prompt_tokens=8000, battery=["timing"])
        timing = cell_runner.run_timing_rounds(_handle(), cell, round_fn=_capturing_round_fn())
        geometry = cell_runner._timing_geometry_for_record(cell, timing)
        self.assertEqual(
            sorted(geometry),
            ["instruction_pinned", "max_tokens", "prompt_source", "prompt_tokens_target"],
        )
        self.assertEqual(geometry["prompt_tokens_target"], 8000)
        self.assertEqual(geometry["max_tokens"], cell_runner.TIMING_MAX_TOKENS)
        self.assertEqual(geometry["prompt_source"], "fixture-filler")
        self.assertTrue(geometry["instruction_pinned"])


class TestWriteCellRecordTimingGeometry(unittest.TestCase):
    """The record-level proof (brief item 4): `timing_geometry` survives into `config.json`
    when supplied, is omitted (never `null`) when not, and `timing_summary` never appears."""

    def _write(self, base_dir, timing_geometry=None):
        cell = _longcontext_cell(battery=["vram"])
        records_dir = Path(base_dir) / "run-timing-geometry"
        out = cell_record.write_cell_record(
            records_dir, cell, {}, vram_baseline_bytes=1000, vram_loaded_bytes=9000,
            sampling_arm_effective="capability", timing_geometry=timing_geometry,
        )
        return json.loads((out / "config.json").read_text())

    def test_default_omits_the_key_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write(tmp)
            self.assertNotIn("timing_geometry", config)
            self.assertNotIn("timing_summary", config)

    def test_supplied_geometry_round_trips_verbatim(self):
        geometry = {
            "prompt_tokens_target": 8000, "max_tokens": 256,
            "prompt_source": "fixture-filler", "instruction_pinned": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = self._write(tmp, timing_geometry=geometry)
            self.assertEqual(config["timing_geometry"], geometry)
            self.assertNotIn("timing_summary", config)

    def test_omitting_and_supplying_differ_only_by_that_one_key(self):
        # The byte-identical-by-omission proof: a record written with `timing_geometry=None`
        # (every pre-WP-1 caller) and one written with it set differ in EXACTLY one top-level
        # key, never in any pre-existing field's shape or value.
        geometry = {
            "prompt_tokens_target": 500, "max_tokens": 64,
            "prompt_source": "fixture-filler", "instruction_pinned": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            without = self._write(Path(tmp) / "a")
            with_geometry = self._write(Path(tmp) / "b", timing_geometry=geometry)
        # `timestamp`/`size_check.total_bytes` are expected to differ (wall clock; the added
        # key's own serialized byte count) -- neither is part of this proof.
        for config in (without, with_geometry):
            config.pop("timestamp", None)
            config.pop("size_check", None)
        del with_geometry["timing_geometry"]
        self.assertEqual(without, with_geometry)


class TestRunnerEndToEndPersistsTimingGeometry(unittest.TestCase):
    """`make_longcontext_cell_runner` -> `_persist` -> `write_cell_record`, the exact seam a
    live `"timing"`-only cell (this plan's queue) drives -- no `"needle"` battery item, so
    `_startup_only` is the path under test."""

    def test_depth_cell_record_carries_timing_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            round_fn = _capturing_round_fn()
            cell = _cell_with_knobs(
                timing_prompt_tokens=8000, timing_max_tokens=256, battery=["vram", "timing"],
            )
            runner = cell_runner.make_longcontext_cell_runner(
                vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-e2e", timing_round_fn=round_fn,
            )
            outcome = runner(_handle(), cell)
            self.assertEqual(outcome.status, "completed")
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["timing_geometry"], {
                "prompt_tokens_target": 8000, "max_tokens": 256,
                "prompt_source": "fixture-filler", "instruction_pinned": True,
            })
            self.assertNotIn("timing_summary", config)

    def test_default_cell_record_has_no_timing_geometry_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            round_fn = _capturing_round_fn()
            cell = _cell_with_knobs(battery=["vram", "timing"])
            runner = cell_runner.make_longcontext_cell_runner(
                vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-e2e-default", timing_round_fn=round_fn,
            )
            outcome = runner(_handle(), cell)
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertNotIn("timing_geometry", config)
            self.assertNotIn("timing_summary", config)


class TestBatchReExportsTheNewSymbols(unittest.TestCase):
    """Sanity check on `batch.py`'s existing re-export list: nothing in this WP needed to widen
    it (`resolve_timing_rounds`/`run_timing_rounds`/`TIMING_PROMPT`/`TIMING_MAX_TOKENS` were
    already re-exported; the two new module-level names this WP adds --
    `resolve_timing_prompt`/`resolve_timing_max_tokens`/`TIMING_DEPTH_INSTRUCTION` -- are reached
    through `cell_runner` directly elsewhere in this file, matching how `test_cell_outcome.py`
    reaches `cell_outcome` directly rather than via `batch`)."""

    def test_batch_still_resolves_queues_the_same_way(self):
        self.assertIs(batch.resolve_queue_cells, queue_file.resolve_queue_cells)
