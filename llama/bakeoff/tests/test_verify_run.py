"""Post-queue mechanical verifier (plan longcontext-pow2-overnight-campaign.md WP-4). Every run
directory in this suite is a synthetic fixture built directly on disk (either via
`run_harness.write_experiment_record` for a "real record shape" cell, or hand-assembled JSON for
a deliberately broken one) -- no live Docker/GPU/network anywhere in this file, matching
`test_batch.py`'s own no-live-operation stance."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import run_harness  # noqa: E402
import sweep  # noqa: E402
import verify_run  # noqa: E402


# -------------------------------------------------------------------------------------------
# Fixture builders
# -------------------------------------------------------------------------------------------


def _lineage(**overrides):
    lineage = {
        "hf_repo": "unsloth/gemma-4-12B-it-qat-GGUF",
        "hf_revision": None,
        "filename": "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
        "file_size": 6 * sweep.GIB,
        "sha256": None,
        "llama_cpp_build": "b10534",
    }
    lineage.update(overrides)
    return lineage


def _lc_sample(cell_id, trial_index, target_tokens=8192, prompt_tokens=None, classifier_verdict="correct", correct=True):
    if prompt_tokens is None:
        prompt_tokens = target_tokens
    return {
        "type": "longcontext_probe", "schema_version": 2, "task": "single_needle",
        "cell_id": cell_id, "trial_index": trial_index, "position_fraction": 0.45,
        "target_tokens": target_tokens, "haystack_structure": "coherent",
        "haystack_book_ids": ["book-1"], "corpus_manifest_id": "gutenberg-longcontext-haystack-v2",
        "haystack_seed": 1000 + trial_index, "sampler_seed": 2000 + trial_index,
        "sampling_arm": "capability", "temperature": 0.0, "top_p": 1.0, "top_k": 1,
        "haystack_sha256": "0" * 64, "expected": "CODE-1234", "trial_meta": {},
        "correct": correct, "classifier_verdict": classifier_verdict, "classifier_shape": None,
        "classifier_flags": {}, "finish_reason": "stop", "success": True, "error": None,
        "wall_seconds": 1.23, "prompt_tokens": prompt_tokens,
        "timestamp": "2026-08-23T00:00:00+00:00",
    }


def _lc_completion(cell_id, trial_index):
    return {
        "type": "longcontext_probe", "task": "single_needle", "cell_id": cell_id,
        "trial_index": trial_index, "expected": "CODE-1234", "content_text": "CODE-1234",
        "reasoning_text": "", "finish_reason": "stop",
    }


_SYNTHETIC_GEOMETRY = {
    "weights_bytes": 6 * sweep.GIB, "block_count": 8, "head_count_kv": 8,
    "key_length": 128, "value_length": 128,
}


def _estimated_delta_bytes(ctk="f16", ctv="f16", context=8192, weights_bytes=6 * sweep.GIB):
    bytes_per_token = sweep.kv_bytes_per_token(_SYNTHETIC_GEOMETRY, ctk, ctv, context=context)
    return weights_bytes + bytes_per_token * context


def _write_good_cell(run_dir, cell_id, n_trials=10, wilson_n=10, prompt_tokens=None, vram_ratio=1.0):
    """A fully schema-conformant, arithmetic-consistent `completed` cell -- the baseline every
    negative-test fixture below is a single deliberate deviation from."""
    estimated_delta = _estimated_delta_bytes()
    baseline = 1 * sweep.GIB
    loaded = baseline + int(estimated_delta * vram_ratio)
    config = {
        "lineage": _lineage(),
        "sweep_cell": {
            "cell_id": cell_id, "alias": cell_id,
            "server_flags": {"-c": 8192, "-ctk": "f16", "-ctv": "f16", "-fa": "on"},
            "synthetic_geometry": _SYNTHETIC_GEOMETRY,
        },
        "vram": {"baseline_bytes": baseline, "loaded_bytes": loaded, "after_teardown_bytes": baseline},
        "wilson_summary": None if wilson_n is None else {
            "k": 1, "n": wilson_n, "rate": 0.1, "sided": "two", "confidence": 0.95,
            "lower": 0.001, "upper": 0.4, "cell_id": cell_id, "task": "single_needle",
            "position_fraction": 0.45,
        },
    }
    samples = [_lc_sample(cell_id, i, prompt_tokens=prompt_tokens) for i in range(n_trials)]
    completions = [_lc_completion(cell_id, i) for i in range(n_trials)]
    run_harness.write_experiment_record(cell_id, config, [], completions, samples, base_dir=run_dir)


def _write_state(run_dir, cells, queue_state="completed", sibling=True):
    state = {"queue_state": queue_state, "created_at": "2026-08-23T00:00:00+00:00", "cells": cells}
    if sibling:
        path = run_dir.parent / (run_dir.name + ".state.json")
    else:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "queue.state.json"
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return path


def _cell_state(state, reason=None):
    return {"state": state, "reason": reason}


# -------------------------------------------------------------------------------------------
# discover_state_path / discover_cell_dirs
# -------------------------------------------------------------------------------------------


class TestStateAndCellDiscovery(unittest.TestCase):
    def test_sibling_state_file_discovered_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            _write_state(run_dir, {}, sibling=True)
            resolved = verify_run.discover_state_path(run_dir)
            self.assertEqual(resolved, run_dir.parent / "nightly-run.state.json")

    def test_run_dir_child_state_file_preferred_over_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            _write_state(run_dir, {"child": True}, sibling=False)
            _write_state(run_dir, {"sibling": True}, sibling=True)
            resolved = verify_run.discover_state_path(run_dir)
            self.assertEqual(resolved, run_dir / "queue.state.json")

    def test_no_state_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            self.assertIsNone(verify_run.discover_state_path(run_dir))

    def test_explicit_state_path_overrides_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            explicit = Path(tmp) / "elsewhere.json"
            explicit.write_text("{}", encoding="utf-8")
            self.assertEqual(verify_run.discover_state_path(run_dir, explicit), explicit)

    def test_explicit_state_path_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            missing = Path(tmp) / "nope.json"
            self.assertIsNone(verify_run.discover_state_path(run_dir, missing))

    def test_discover_cell_dirs_only_dirs_with_config_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            (run_dir / "not-a-cell").mkdir()
            (run_dir / "loose-file.txt").write_text("x", encoding="utf-8")
            cell_dirs = verify_run.discover_cell_dirs(run_dir)
            self.assertEqual(set(cell_dirs), {"cell-a"})

    def test_discover_cell_dirs_nonexistent_run_dir(self):
        self.assertEqual(verify_run.discover_cell_dirs(Path("/nonexistent/run/dir")), {})


# -------------------------------------------------------------------------------------------
# schema_conformance_errors
# -------------------------------------------------------------------------------------------


class TestSchemaConformance(unittest.TestCase):
    def _good_config(self):
        return {
            "schema_version": 2, "experiment_id": "cell-a", "timestamp": "2026-08-23T00:00:00+00:00",
            "lineage": _lineage(), "sweep_cell": {}, "wilson_summary": None, "size_check": {},
        }

    def test_conformant_record_has_no_errors(self):
        config = self._good_config()
        samples = [_lc_sample("cell-a", 0)]
        self.assertEqual(verify_run.schema_conformance_errors(config, samples), [])

    def test_none_config_is_an_error(self):
        self.assertTrue(verify_run.schema_conformance_errors(None, [])[0].startswith("config.json missing"))

    def test_wrong_schema_version_flagged(self):
        config = self._good_config()
        config["schema_version"] = 1
        errors = verify_run.schema_conformance_errors(config, [_lc_sample("cell-a", 0)])
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_missing_top_level_key_flagged(self):
        config = self._good_config()
        del config["wilson_summary"]
        errors = verify_run.schema_conformance_errors(config, [_lc_sample("cell-a", 0)])
        self.assertTrue(any("wilson_summary" in e for e in errors))

    def test_missing_lineage_field_flagged(self):
        config = self._good_config()
        del config["lineage"]["sha256"]
        errors = verify_run.schema_conformance_errors(config, [_lc_sample("cell-a", 0)])
        self.assertTrue(any("sha256" in e for e in errors))

    def test_empty_samples_flagged(self):
        errors = verify_run.schema_conformance_errors(self._good_config(), [])
        self.assertTrue(any("no parsed sample rows" in e for e in errors))

    def test_longcontext_probe_missing_field_flagged(self):
        sample = _lc_sample("cell-a", 0)
        del sample["classifier_verdict"]
        errors = verify_run.schema_conformance_errors(self._good_config(), [sample])
        self.assertTrue(any("classifier_verdict" in e for e in errors))

    def test_sample_missing_type_flagged(self):
        sample = _lc_sample("cell-a", 0)
        del sample["type"]
        errors = verify_run.schema_conformance_errors(self._good_config(), [sample])
        self.assertTrue(any("missing 'type'" in e for e in errors))

    def test_needle_probe_missing_v2_field_flagged(self):
        sample = {"type": "needle_probe", "schema_version": 2, "temperature": 0.0, "top_p": 1.0, "top_k": 1, "sampler_seed": 1}
        errors = verify_run.schema_conformance_errors(self._good_config(), [sample])
        self.assertTrue(any("sampling_arm" in e for e in errors))

    def test_timing_round_only_needs_type(self):
        sample = {"type": "timing_round", "prompt_tokens": 100}
        errors = verify_run.schema_conformance_errors(self._good_config(), [sample])
        self.assertEqual(errors, [])


# -------------------------------------------------------------------------------------------
# check_vram_arithmetic
# -------------------------------------------------------------------------------------------


class TestVramArithmetic(unittest.TestCase):
    def _config(self, ctk="f16", ctv="f16", context=8192, vram_ratio=1.0, geometry=_SYNTHETIC_GEOMETRY, weights_bytes=6 * sweep.GIB):
        estimated = weights_bytes + sweep.kv_bytes_per_token(geometry, ctk, ctv, context=context) * context
        baseline = 1 * sweep.GIB
        return {
            "sweep_cell": {
                "server_flags": {"-c": context, "-ctk": ctk, "-ctv": ctv},
                "synthetic_geometry": geometry,
            },
            "vram": {"baseline_bytes": baseline, "loaded_bytes": baseline + int(estimated * vram_ratio)},
            "lineage": _lineage(),
        }

    def test_ratio_one_passes(self):
        item = verify_run.check_vram_arithmetic("c", self._config(vram_ratio=1.0))
        self.assertEqual(item["verdict"], "pass")

    def test_ratio_within_band_passes(self):
        item = verify_run.check_vram_arithmetic("c", self._config(vram_ratio=2.5))
        self.assertEqual(item["verdict"], "pass")

    def test_ratio_far_out_of_band_fails(self):
        item = verify_run.check_vram_arithmetic("c", self._config(vram_ratio=10.0))
        self.assertEqual(item["verdict"], "fail")

    def test_no_geometry_is_unknown(self):
        config = {
            "sweep_cell": {"server_flags": {"-c": 8192, "-ctk": "f16", "-ctv": "f16"}},
            "vram": {"baseline_bytes": 1, "loaded_bytes": 2},
            "lineage": _lineage(),
        }
        item = verify_run.check_vram_arithmetic("c", config)
        self.assertEqual(item["verdict"], "unknown")

    def test_missing_vram_fields_is_unknown(self):
        config = {
            "sweep_cell": {"server_flags": {"-c": 8192, "-ctk": "f16", "-ctv": "f16"}, "synthetic_geometry": _SYNTHETIC_GEOMETRY},
            "vram": {},
            "lineage": _lineage(),
        }
        item = verify_run.check_vram_arithmetic("c", config)
        self.assertEqual(item["verdict"], "unknown")

    def test_none_config_is_unknown(self):
        item = verify_run.check_vram_arithmetic("c", None)
        self.assertEqual(item["verdict"], "unknown")

    def test_stale_model_host_path_is_unknown_not_fail(self):
        config = {
            "sweep_cell": {
                "server_flags": {"-c": 8192, "-ctk": "f16", "-ctv": "f16"},
                "model_host_path": "/nonexistent/model.gguf",
            },
            "vram": {"baseline_bytes": 1, "loaded_bytes": 2},
            "lineage": _lineage(),
        }
        item = verify_run.check_vram_arithmetic("c", config)
        self.assertEqual(item["verdict"], "unknown")


# -------------------------------------------------------------------------------------------
# check_prompt_tokens_tolerance / check_wilson_min_trials / check_classifier_distribution
# -------------------------------------------------------------------------------------------


class TestPromptTokensTolerance(unittest.TestCase):
    def test_within_tolerance_passes(self):
        samples = [_lc_sample("c", i, target_tokens=8000, prompt_tokens=7900) for i in range(3)]
        item = verify_run.check_prompt_tokens_tolerance("c", samples)
        self.assertEqual(item["verdict"], "pass")

    def test_outside_tolerance_fails(self):
        samples = [_lc_sample("c", i, target_tokens=8000, prompt_tokens=6000) for i in range(3)]
        item = verify_run.check_prompt_tokens_tolerance("c", samples)
        self.assertEqual(item["verdict"], "fail")
        self.assertEqual(len(item["observed"]["violations"]), 3)

    def test_no_relevant_samples_is_unknown(self):
        samples = [{"type": "timing_round"}]
        item = verify_run.check_prompt_tokens_tolerance("c", samples)
        self.assertEqual(item["verdict"], "unknown")

    def test_exactly_at_boundary_passes(self):
        # 10% of 8000 = 800; 7200 is exactly at the boundary (<=), must pass.
        samples = [_lc_sample("c", 0, target_tokens=8000, prompt_tokens=7200)]
        item = verify_run.check_prompt_tokens_tolerance("c", samples)
        self.assertEqual(item["verdict"], "pass")


class TestWilsonMinTrials(unittest.TestCase):
    def test_no_wilson_summary_claimed_passes(self):
        item = verify_run.check_wilson_min_trials("c", {"wilson_summary": None})
        self.assertEqual(item["verdict"], "pass")

    def test_n_at_minimum_passes(self):
        item = verify_run.check_wilson_min_trials("c", {"wilson_summary": {"n": 10}})
        self.assertEqual(item["verdict"], "pass")

    def test_n_below_minimum_fails(self):
        item = verify_run.check_wilson_min_trials("c", {"wilson_summary": {"n": 5}})
        self.assertEqual(item["verdict"], "fail")

    def test_none_config_treated_as_no_claim(self):
        item = verify_run.check_wilson_min_trials("c", None)
        self.assertEqual(item["verdict"], "pass")


class TestClassifierDistribution(unittest.TestCase):
    def test_counts_by_verdict(self):
        samples = [
            _lc_sample("c", 0, classifier_verdict="correct"),
            _lc_sample("c", 1, classifier_verdict="correct"),
            _lc_sample("c", 2, classifier_verdict="degenerate"),
        ]
        item = verify_run.check_classifier_distribution("c", samples)
        self.assertEqual(item["verdict"], "informational")
        self.assertEqual(item["observed"], {"correct": 2, "degenerate": 1})

    def test_null_verdicts_excluded(self):
        samples = [_lc_sample("c", 0, classifier_verdict=None)]
        item = verify_run.check_classifier_distribution("c", samples)
        self.assertEqual(item["observed"], {})


# -------------------------------------------------------------------------------------------
# check_abort_state
# -------------------------------------------------------------------------------------------


class TestAbortState(unittest.TestCase):
    def test_no_state_file_is_unknown(self):
        item = verify_run.check_abort_state(None)
        self.assertEqual(item["verdict"], "unknown")

    def test_running_queue_passes(self):
        item = verify_run.check_abort_state({"queue_state": "running", "cells": {}})
        self.assertEqual(item["verdict"], "pass")

    def test_aborted_hang_queue_fails_and_lists_cells(self):
        state = {
            "queue_state": batch.QUEUE_ABORTED_HANG,
            "cells": {"cell-a": {"state": batch.ABORTED_HANG}, "cell-b": {"state": batch.COMPLETED}},
        }
        item = verify_run.check_abort_state(state)
        self.assertEqual(item["verdict"], "fail")
        self.assertEqual(item["observed"]["aborted_hang_cells"], ["cell-a"])


# -------------------------------------------------------------------------------------------
# verify_run() end-to-end over synthetic fixture run directories
# -------------------------------------------------------------------------------------------


class TestVerifyRunEndToEnd(unittest.TestCase):
    def test_nonexistent_run_dir_is_exit_2(self):
        result = verify_run.verify_run(Path("/nonexistent/run/dir"))
        self.assertEqual(result["exit_code"], 2)
        self.assertFalse(result["run_dir_exists"])

    def test_empty_run_dir_no_state_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 2)

    def test_all_good_queue_is_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["summary"]["fail"], 0)
            self.assertGreater(result["summary"]["pass"], 0)

    def test_completed_cell_with_no_records_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 1)
            records_items = [c for c in result["checks"] if c["check"] == verify_run.CHECK_RECORDS_PRESENT]
            self.assertEqual(records_items[0]["verdict"], "fail")

    def test_failed_cell_with_no_records_is_not_penalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            _write_state(run_dir, {"cell-a": _cell_state(batch.FAILED, reason="health_timeout")})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 0)
            records_items = [c for c in result["checks"] if c["check"] == verify_run.CHECK_RECORDS_PRESENT]
            self.assertEqual(records_items[0]["verdict"], "unknown")

    def test_aborted_hang_queue_is_exit_1_and_surfaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            run_dir.mkdir(exist_ok=True)
            _write_state(
                run_dir,
                {"cell-a": _cell_state(batch.COMPLETED), "cell-b": _cell_state(batch.ABORTED_HANG, reason="MES might be in unrecoverable state")},
                queue_state=batch.QUEUE_ABORTED_HANG,
            )
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 1)
            self.assertEqual(result["queue_state"], batch.QUEUE_ABORTED_HANG)
            abort_items = [c for c in result["checks"] if c["check"] == verify_run.CHECK_ABORT_STATE]
            self.assertEqual(abort_items[0]["verdict"], "fail")
            self.assertIn("cell-b", abort_items[0]["observed"]["aborted_hang_cells"])

    def test_bad_prompt_tokens_cell_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a", prompt_tokens=4000)  # target 8192, way off
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 1)

    def test_bad_wilson_n_cell_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a", wilson_n=5)
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 1)

    def test_bad_vram_arithmetic_cell_is_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a", vram_ratio=20.0)
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 1)

    def test_corrupt_samples_jsonl_fails_schema_conformance(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            (run_dir / "cell-a" / "samples.jsonl").write_text("{not valid json\n", encoding="utf-8")
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            self.assertEqual(result["exit_code"], 1)
            schema_items = [c for c in result["checks"] if c["check"] == verify_run.CHECK_SCHEMA_CONFORMANT]
            self.assertEqual(schema_items[0]["verdict"], "fail")

    def test_orphan_dir_not_in_state_still_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-orphan")
            _write_state(run_dir, {})  # empty cells map -- cell-orphan is not in queue state at all
            result = verify_run.verify_run(run_dir)
            self.assertIn("cell-orphan", result["cells"])
            self.assertIsNone(result["cells"]["cell-orphan"]["state"])
            self.assertEqual(result["exit_code"], 0)

    def test_explicit_state_path_used_over_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            other_state = Path(tmp) / "other.state.json"
            with open(other_state, "w", encoding="utf-8") as f:
                json.dump({"queue_state": "completed", "cells": {"cell-a": _cell_state(batch.COMPLETED)}}, f)
            result = verify_run.verify_run(run_dir, state_path=other_state)
            self.assertEqual(result["state_path"], str(other_state))
            self.assertEqual(result["exit_code"], 0)


# -------------------------------------------------------------------------------------------
# Report writers + CLI main()
# -------------------------------------------------------------------------------------------


class TestReportsAndMain(unittest.TestCase):
    def test_write_reports_produces_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            result = verify_run.verify_run(run_dir)
            json_path = verify_run.write_json_report(run_dir, result)
            md_path = verify_run.write_markdown_report(run_dir, result)
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            reloaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded["exit_code"], 0)
            self.assertIn("cell-a", md_path.read_text(encoding="utf-8"))

    def test_markdown_report_carries_abort_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            _write_state(run_dir, {"cell-a": _cell_state(batch.ABORTED_HANG, reason="hang")}, queue_state=batch.QUEUE_ABORTED_HANG)
            result = verify_run.verify_run(run_dir)
            md = verify_run._render_markdown(result)
            self.assertIn("ABORTED-HANG", md)

    def test_main_returns_0_for_clean_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            rc = verify_run.main(["--run", str(run_dir)])
            self.assertEqual(rc, 0)
            self.assertTrue((run_dir / verify_run.JSON_REPORT_NAME).exists())
            self.assertTrue((run_dir / verify_run.MARKDOWN_REPORT_NAME).exists())

    def test_main_returns_2_for_nonexistent_run_dir(self):
        rc = verify_run.main(["--run", "/nonexistent/run/dir"])
        self.assertEqual(rc, 2)

    def test_main_returns_1_for_aborted_hang(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            run_dir.mkdir()
            _write_state(run_dir, {"cell-a": _cell_state(batch.ABORTED_HANG)}, queue_state=batch.QUEUE_ABORTED_HANG)
            rc = verify_run.main(["--run", str(run_dir)])
            self.assertEqual(rc, 1)

    def test_batch_cmd_verify_dispatches_to_verify_run_main(self):
        """The WP-1 seam this whole module exists to satisfy: `batch.py cmd_verify` imports
        `verify_run` and calls `verify_run.main(["--run", args.run])`, returning its int exit
        code untouched. Exercised here via `batch.main(["verify", "--run", ...])` end-to-end,
        never editing `batch.py` itself."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "nightly-run"
            _write_good_cell(run_dir, "cell-a")
            _write_state(run_dir, {"cell-a": _cell_state(batch.COMPLETED)})
            rc = batch.main(["verify", "--run", str(run_dir)])
            self.assertEqual(rc, 0)
            self.assertTrue((run_dir / verify_run.JSON_REPORT_NAME).exists())


if __name__ == "__main__":
    unittest.main()
