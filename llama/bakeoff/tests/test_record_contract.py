"""Tests owned by the record seam: `cell_record.py` writing, `cell_model.RecordMetadata` /
`FitMetadata` serialization, the HF-repo-from-host-path helper, BTRE record timing fields, the
WP-R2 typed fit-metadata emission and dual-generation `verify_run` record reads, and the WP-1
KV-geometry `verify_run` round trip. Split from the former `test_batch.py` omnibus module
(WP-R3 of plans/queue-contract-and-test-decomposition-remediation.md); no test logic changed in
the move.

No live Docker/GPU execution anywhere in this suite -- fixtures imported from `batch_fixtures`
are always fakes; `_fit_cell` below is local because its sole consumer lives in this module."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from batch_fixtures import (  # noqa: E402
    _fake_battery_fn_factory,
    _handle,
    _longcontext_cell,
    _minimal_queue,
    _sample,
)

import batch  # noqa: E402
import gguf_fit  # noqa: E402
import longcontext  # noqa: E402
import run_harness  # noqa: E402
import sweep  # noqa: E402
import verify_run  # noqa: E402


class TestWriteCellRecord(unittest.TestCase):
    """WP-5 glue: `write_cell_record` persists one live cell's battery output as the
    `verify_run.py`-consumable run-directory shape (`records_dir/<cell_id>/`, four-file
    `write_experiment_record` layout, experiment_id == cell_id)."""

    def _battery_results(self, n=2, summary=None):
        samples = [_sample("single_needle", "correct", True) for _ in range(n)]
        completions = [{"type": "longcontext_probe", "trial_index": i} for i in range(n)]
        return {"single_needle": {0.45: longcontext.CellResult(
            samples=samples, completions=completions, summary=summary,
        )}}

    def test_writes_verify_run_consumable_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_gguf = Path(tmp) / "fake-model.gguf"
            fake_gguf.write_bytes(b"x" * 128)
            cell = _longcontext_cell(model_host_path=str(fake_gguf))
            records_dir = Path(tmp) / "run-wp5"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            self.assertEqual(out, records_dir / cell.cell_id)
            config = json.loads((out / "config.json").read_text())
            for key in run_harness.REQUIRED_LINEAGE_FIELDS:
                self.assertIn(key, config["lineage"])
            self.assertEqual(config["lineage"]["filename"], "fake-model.gguf")
            self.assertEqual(config["lineage"]["file_size"], 128)
            self.assertEqual(config["sweep_cell"]["cell_id"], cell.cell_id)
            self.assertEqual(config["vram"]["baseline_bytes"], 1000)
            self.assertEqual(config["vram"]["loaded_bytes"], 9000)
            self.assertIsNone(config["wilson_summary"])
            samples = (out / "samples.jsonl").read_text().strip().splitlines()
            self.assertEqual(len(samples), 2)

    def test_runner_writes_record_when_records_dir_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-x",
            )
            outcome = runner(_handle(), _longcontext_cell())
            self.assertEqual(outcome.status, "completed")
            self.assertIsNotNone(outcome.detail["record_path"])
            self.assertIsNone(outcome.detail["record_error"])
            self.assertTrue((Path(outcome.detail["record_path"]) / "config.json").exists())

    def test_config_carries_vram_profile_and_response_caps_when_supplied(self):
        # WP-B item 2: record `config.json` gains top-level `vram_profile` + `response_caps`.
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell()
            records_dir = Path(tmp) / "run-wp-b"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
                vram_profile="desktop-shared",
                response_caps={"default": 256, "by_task": {"multi_needle": 768}},
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["vram_profile"], "desktop-shared")
            self.assertEqual(
                config["response_caps"], {"default": 256, "by_task": {"multi_needle": 768}},
            )

    def test_config_vram_profile_and_response_caps_default_to_none(self):
        # Additive/backward-compatible: a caller (this WP's own earlier tests above) that omits
        # the two new kwargs still gets a valid record, with the keys present but null.
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell()
            records_dir = Path(tmp) / "run-wp-b-default"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIsNone(config["vram_profile"])
            self.assertIsNone(config["response_caps"])

    def test_needle_runner_threads_vram_profile_and_response_caps_into_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-wp-b-runner",
                vram_profile="headless",
            )
            outcome = runner(_handle(), _longcontext_cell())
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["vram_profile"], "headless")
            self.assertEqual(
                config["response_caps"],
                {"default": 256, "by_task": batch.LONGCONTEXT_MAX_TOKENS_BY_TASK},
            )

    def test_deferred_vram_cell_record_also_carries_vram_profile_and_response_caps(self):
        # The non-needle deferral path (default_cell_runner + augmented VRAM read-backs) writes
        # through the same `write_cell_record` seam -- it must gain the two fields too.
        with tempfile.TemporaryDirectory() as tmp:
            runner = batch.make_longcontext_cell_runner(
                battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 4321,
                records_dir=Path(tmp) / "run-wp-b-vram-only",
                vram_profile="desktop-shared",
            )
            cell = _longcontext_cell(battery=["vram"])
            outcome = runner(_handle(vram_before=1000), cell)
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["vram_profile"], "desktop-shared")
            self.assertEqual(
                config["response_caps"],
                {"default": 256, "by_task": batch.LONGCONTEXT_MAX_TOKENS_BY_TASK},
            )

    def test_runner_record_write_failure_never_aborts_the_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            unwritable = Path(tmp) / "not-a-dir.txt"
            unwritable.write_text("occupied")
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=unwritable,  # a file where a directory is needed -> write fails
            )
            outcome = runner(_handle(), _longcontext_cell())
            self.assertEqual(outcome.status, "completed")  # persistence failure is not a cell failure
            self.assertIsNone(outcome.detail["record_path"])
            self.assertIsNotNone(outcome.detail["record_error"])

    # -- lineage fill (local-capture WP-1): hf_repo derived from model_host_path, ---------------
    # -- llama_cpp_build resolved live, past-record None values never rewritten -----------------

    def test_record_lineage_derives_hf_repo_and_llama_cpp_build(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_host_path=(
                "/srv/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf"
            ))
            records_dir = Path(tmp) / "run-lineage-fill"
            with mock.patch.object(
                run_harness, "resolve_llama_cpp_build", return_value="b1234-fixed",
            ):
                out = batch.write_cell_record(
                    records_dir, cell, self._battery_results(),
                    vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                    sampling_arm_effective="capability",
                )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["lineage"]["hf_repo"], "unsloth/Qwen3.8-27B-GGUF")
            self.assertEqual(config["lineage"]["llama_cpp_build"], "b1234-fixed")
            self.assertIsNone(config["lineage"]["hf_revision"])
            self.assertIsNone(config["lineage"]["sha256"])

    def test_record_lineage_hf_repo_none_when_host_path_outside_shared_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(
                model_host_path="/opt/models/unsloth/Qwen3.8-27B-GGUF/model.gguf",
            )
            records_dir = Path(tmp) / "run-lineage-outside"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIsNone(config["lineage"]["hf_repo"])
            # llama_cpp_build is resolved regardless of host_path shape -- it is not path-derived.
            self.assertIsNotNone(config["lineage"]["llama_cpp_build"])

    def test_record_lineage_hf_repo_none_when_host_path_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_host_path=None)
            records_dir = Path(tmp) / "run-lineage-absent"
            out = batch.write_cell_record(
                records_dir, cell, self._battery_results(),
                vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIsNone(config["lineage"]["hf_repo"])


class TestHfRepoFromHostPath(unittest.TestCase):
    """Unit coverage for `batch.hf_repo_from_host_path` in isolation from `write_cell_record` --
    the local-capture WP-1 lineage-fill helper (`"<org>/<repo>"` from a `model_host_path` under
    `/srv/models`, else `None`)."""

    def test_matches_org_repo_under_shared_models_prefix(self):
        host_path = "/srv/models/unsloth/Qwen3.8-27B-GGUF/x.gguf"
        self.assertEqual(
            batch.hf_repo_from_host_path(host_path), "unsloth/Qwen3.8-27B-GGUF",
        )

    def test_path_outside_shared_models_prefix_returns_none(self):
        self.assertIsNone(
            batch.hf_repo_from_host_path("/home/other_models/unsloth/repo/x.gguf"),
        )

    def test_none_input_returns_none(self):
        self.assertIsNone(batch.hf_repo_from_host_path(None))

    def test_path_with_only_two_segments_after_prefix_returns_none(self):
        # No repo directory -- just an org segment then a file, one level short of the
        # required org/repo/file shape.
        self.assertIsNone(
            batch.hf_repo_from_host_path("/srv/models/unsloth/x.gguf"),
        )

    def test_matches_org_repo_under_models_prefix(self):
        # /models is the post-partition-migration prefix (models-partition-migration WP-2);
        # /srv/models above must keep resolving forever as the frozen identity token.
        host_path = "/models/unsloth/Qwen3.8-27B-GGUF/x.gguf"
        self.assertEqual(
            batch.hf_repo_from_host_path(host_path), "unsloth/Qwen3.8-27B-GGUF",
        )

    def test_path_with_only_two_segments_after_models_prefix_returns_none(self):
        # Same too-few-segments shape as the shared_models case above, under the new prefix.
        self.assertIsNone(
            batch.hf_repo_from_host_path("/models/unsloth/x.gguf"),
        )


class TestBtreRecordTimingFields(unittest.TestCase):
    """Additive `config.json` keys: `cell_started_at`/`cell_ended_at`/`cell_wall_seconds`/
    `scratch_startup_seconds`, plus `sweep_cell.model`."""

    def _battery_results(self):
        return {"single_needle": {0.45: longcontext.CellResult(
            samples=[_sample("single_needle", "correct", True)],
            completions=[{"type": "longcontext_probe", "trial_index": 0}],
            summary=None,
        )}}

    def test_direct_call_without_the_new_kwargs_still_parses_with_explicit_nulls(self):
        # Back-compat: the pre-WP call shape (positional + the WP-B kwargs) keeps working, and
        # every new key is present-and-null rather than absent-and-ambiguous.
        with tempfile.TemporaryDirectory() as tmp:
            out = batch.write_cell_record(
                Path(tmp) / "run", _longcontext_cell(), self._battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["schema_version"], 2)  # additive only -- never a bump
            for key in ("cell_started_at", "cell_ended_at", "cell_wall_seconds",
                        "scratch_startup_seconds"):
                self.assertIn(key, config)
                self.assertIsNone(config[key])
            self.assertIn("model", config["sweep_cell"])
            self.assertIsNone(config["sweep_cell"]["model"])

    def test_new_kwargs_round_trip_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_id="gemma-4-12b-qat")
            out = batch.write_cell_record(
                Path(tmp) / "run", cell, self._battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
                cell_started_at="2026-08-25T12:00:00+00:00",
                cell_ended_at="2026-08-25T12:34:56+00:00",
                cell_wall_seconds=2096.0,
                scratch_startup_seconds=31.5,
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["cell_started_at"], "2026-08-25T12:00:00+00:00")
            self.assertEqual(config["cell_ended_at"], "2026-08-25T12:34:56+00:00")
            self.assertEqual(config["cell_wall_seconds"], 2096.0)
            self.assertEqual(config["scratch_startup_seconds"], 31.5)
            self.assertEqual(config["sweep_cell"]["model"], "gemma-4-12b-qat")
            # The reserved name stays reserved: the startup measurement must never be recorded
            # under `model_load_seconds` (README honest-timing-semantics contract).
            self.assertNotIn("model_load_seconds", config)

    def test_queue_model_id_is_the_single_source_of_sweep_cell_model(self):
        # WP-R1: pre-refactor a cell dict carried the queue model key under BOTH `model` and
        # `_queue_model_id`, and `write_cell_record` read one with the other as a fallback.
        # There is now exactly one spelling -- `QueueCellMetadata.model_id` -- so the dual-key
        # fallback has no way to disagree with itself. The recorded value is unchanged.
        with tempfile.TemporaryDirectory() as tmp:
            cell = _longcontext_cell(model_id="model-a")
            out = batch.write_cell_record(
                Path(tmp) / "run", cell, self._battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertEqual(config["sweep_cell"]["model"], "model-a")

    def test_resolved_cells_carry_the_queue_model_key_without_changing_cell_id(self):
        plain = batch.resolve_queue_cells(_minimal_queue())
        for cell in plain:
            self.assertEqual(cell.queue.model_id, "model-a")
            self.assertEqual(cell.record_metadata().model_id, "model-a")
        # Identity is untouched by the queue model key -- structurally, since `model_id` lives
        # on `QueueCellMetadata` and `compute_cell_id` only ever sees `CellIdentity`.
        recomputed = [sweep.compute_cell_id(c.as_sweep_cell()) for c in plain]
        self.assertEqual(recomputed, [c.cell_id for c in plain])
        self.assertNotIn("model", plain[0].as_sweep_cell())

    def test_runner_stamps_the_handles_clock_into_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            battery_fn = _fake_battery_fn_factory(
                {"single_needle": [_sample("single_needle", "correct", True)]},
            )
            runner = batch.make_longcontext_cell_runner(
                battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
                records_dir=Path(tmp) / "run-clock",
            )
            handle = _handle()
            handle.started_at = "2026-08-25T09:00:00+00:00"
            handle.scratch_startup_seconds = 12.75
            cell = _longcontext_cell(model_id="model-a")
            outcome = runner(handle, cell)
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["cell_started_at"], "2026-08-25T09:00:00+00:00")
            self.assertEqual(config["scratch_startup_seconds"], 12.75)
            self.assertEqual(config["sweep_cell"]["model"], "model-a")
            self.assertIsInstance(config["cell_wall_seconds"], float)
            self.assertGreaterEqual(config["cell_wall_seconds"], 0.0)
            self.assertIsInstance(config["cell_ended_at"], str)

    def test_startup_only_cell_record_also_carries_the_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = batch.make_longcontext_cell_runner(
                battery_fn=_fake_battery_fn_factory({}), vram_used_bytes_fn=lambda: 4321,
                records_dir=Path(tmp) / "run-vram-clock",
            )
            handle = _handle()
            handle.scratch_startup_seconds = 8.5
            outcome = runner(handle, _longcontext_cell(battery=["vram"]))
            config = json.loads(
                (Path(outcome.detail["record_path"]) / "config.json").read_text()
            )
            self.assertEqual(config["scratch_startup_seconds"], 8.5)
            self.assertIsInstance(config["cell_wall_seconds"], float)


def _fit_cell(fit=None, model_family=None):
    """A `ResolvedCell` with a fully literal identity, so `sweep_cell_block()` renders to a
    deterministic byte string (`cell_id` included -- it is a pure hash of the four literal
    identity fields below)."""
    flags = {
        "--host": "0.0.0.0", "-lv": 9, "-ctk": "f16", "-ctv": "f16", "-fa": "on", "-ngl": 99,
        "-c": 4096,
    }
    return batch.ResolvedCell(
        identity=batch.CellIdentity(
            alias="wpr2-fit-alias",
            model_container_path="/models/wpr2/fit-model.gguf",
            server_flags=flags,
        ),
        execution=batch.ExecutionSettings.from_flags(flags, ("vram",)),
        queue=batch.QueueCellMetadata(model_id="wpr2-fit-model", model_family=model_family),
        fit=batch.FitMetadata() if fit is None else fit,
    )


class TestWpR2TypedFitMetadataEmission(unittest.TestCase):
    """The two dict boundaries `FitMetadata` feeds, both pinned at the byte level."""

    GEOMETRY: ClassVar[Any] = batch.IndexerGeometry(layers=12, key_length=128, value_length=256)

    def test_sweep_cell_block_bytes_with_indexer_geometry(self):
        cell = _fit_cell(fit=batch.FitMetadata(
            gpu_weights_bytes=3536007987,
            kv_attention_layers=12,
            swa_layer_spec=(True, False),
            indexer_geometry=self.GEOMETRY,
        ))
        block = cell.record_metadata().sweep_cell_block("capability")
        self.assertEqual(
            json.dumps(block, sort_keys=False),
            '{"cell_id": "wpr2-fit-alias-ctkf16-ctvf16-faon-c4096-8cefe4a5", '
            '"alias": "wpr2-fit-alias", '
            '"server_flags": {"--host": "0.0.0.0", "-lv": 9, "-ctk": "f16", "-ctv": "f16", '
            '"-fa": "on", "-ngl": 99, "-c": 4096}, '
            '"model_host_path": null, "battery": ["vram"], "sampling_arm": "capability", '
            '"model": "wpr2-fit-model", "spec_decode": null, '
            '"gpu_weights_bytes": 3536007987, "kv_attention_layers": 12, '
            '"swa_layer_spec": [true, false], '
            '"kv_indexer_geometry": {"layers": 12, "key_length": 128, "value_length": 256}}',
        )

    def test_sweep_cell_block_bytes_without_indexer_geometry(self):
        block = _fit_cell().record_metadata().sweep_cell_block("capability")
        self.assertEqual(
            json.dumps(block, sort_keys=False),
            '{"cell_id": "wpr2-fit-alias-ctkf16-ctvf16-faon-c4096-8cefe4a5", '
            '"alias": "wpr2-fit-alias", '
            '"server_flags": {"--host": "0.0.0.0", "-lv": 9, "-ctk": "f16", "-ctv": "f16", '
            '"-fa": "on", "-ngl": 99, "-c": 4096}, '
            '"model_host_path": null, "battery": ["vram"], "sampling_arm": "capability", '
            '"model": "wpr2-fit-model", "spec_decode": null, '
            '"gpu_weights_bytes": null, "kv_attention_layers": null, '
            '"swa_layer_spec": null, "kv_indexer_geometry": null}',
        )

    def test_swa_layer_spec_serializes_as_a_json_list_never_a_tuple_repr(self):
        # The typed field is a tuple (frozen values only); every emission must be a list, or the
        # record's JSON and every dict-equality assertion downstream would drift.
        cell = _fit_cell(fit=batch.FitMetadata(swa_layer_spec=(True, False, True)))
        block = cell.record_metadata().sweep_cell_block("capability")
        self.assertIsInstance(block["swa_layer_spec"], list)
        self.assertEqual(block["swa_layer_spec"], [True, False, True])
        self.assertIn('"swa_layer_spec": [true, false, true]', json.dumps(block))
        adapter = cell.as_sweep_cell()
        self.assertIsInstance(adapter["swa_layer_spec"], list)
        self.assertEqual(json.dumps(adapter["swa_layer_spec"]), "[true, false, true]")

    def test_unset_facts_are_omitted_by_the_adapter_but_null_in_the_record(self):
        cell = _fit_cell()
        adapter = cell.as_sweep_cell()
        for key in ("gpu_weights_bytes", "kv_attention_layers", "swa_layer_spec",
                    "model_family", "kv_indexer_geometry"):
            with self.subTest(key=key):
                self.assertNotIn(key, adapter)
        block = cell.record_metadata().sweep_cell_block("capability")
        for key in ("gpu_weights_bytes", "kv_attention_layers", "swa_layer_spec",
                    "kv_indexer_geometry"):
            with self.subTest(key=key):
                self.assertIn(key, block)
                self.assertIsNone(block[key])

    def test_the_adapter_emits_set_facts_in_the_pre_wpr2_key_order(self):
        cell = _fit_cell(
            fit=batch.FitMetadata(
                gpu_weights_bytes=1, kv_attention_layers=2, swa_layer_spec=(True,),
                indexer_geometry=self.GEOMETRY,
            ),
            model_family="gemma4",
        )
        self.assertEqual(
            list(cell.as_sweep_cell()),
            ["alias", "model_container_path", "server_flags", "battery", "gpu_weights_bytes",
             "kv_attention_layers", "swa_layer_spec", "model_family", "kv_indexer_geometry",
             "cell_id"],
        )

    def test_an_explicit_registry_null_is_indistinguishable_from_absence(self):
        # The rule inherited verbatim from the pre-WP-R2 `if model_entry.get(key) is not None`
        # filter in `queue_file.resolve_cell`: an explicit JSON `null` never reached the cell.
        queue = _minimal_queue()
        queue["models"]["model-a"].update({
            "gpu_weights_bytes": None, "kv_attention_layers": None, "swa_layer_spec": None,
            "kv_indexer_geometry": None, "model_family": None,
        })
        nulled = batch.resolve_queue_cells(queue)[0]
        absent = batch.resolve_queue_cells(_minimal_queue())[0]
        self.assertEqual(nulled.fit, batch.FitMetadata())
        self.assertEqual(nulled.fit, absent.fit)
        self.assertIsNone(nulled.model_family)
        self.assertEqual(nulled.as_sweep_cell(), absent.as_sweep_cell())
        self.assertEqual(nulled.cell_id, absent.cell_id)


class TestWpR2VerifyRunReadsBothRecordGenerations(unittest.TestCase):
    """`verify_run.check_vram_arithmetic` reads the fit facts off a committed record. Two
    generations exist on disk and neither may be re-judged by this refactor: historical records
    (`records/wp5-validation/*/config.json`) predate the four keys and omit them entirely;
    current ones always carry all four, `null` when unset."""

    SYNTHETIC: ClassVar[dict] = {
        "weights_bytes": 6 * sweep.GIB,
        "block_count": 48,
        "head_count_kv": 2,
        "key_length": 256,
        "value_length": 256,
    }

    def _config(self, **fit_keys):
        sweep_cell = {
            "cell_id": "wpr2-record-cell",
            "alias": "wpr2-record-alias",
            "server_flags": {"-c": 4096, "-ctk": "f16", "-ctv": "f16"},
            "battery": ["vram"],
            "sampling_arm": "capability",
            "synthetic_geometry": dict(self.SYNTHETIC),
        }
        sweep_cell.update(fit_keys)
        return {
            "sweep_cell": sweep_cell,
            "vram": {"baseline_bytes": 1000, "loaded_bytes": 1000 + 6845104128},
        }

    def test_absent_and_explicitly_null_fit_keys_produce_the_same_verdict_item(self):
        historical = verify_run.check_vram_arithmetic("wpr2-record-cell", self._config())
        current = verify_run.check_vram_arithmetic(
            "wpr2-record-cell",
            self._config(
                gpu_weights_bytes=None, kv_attention_layers=None,
                swa_layer_spec=None, kv_indexer_geometry=None,
            ),
        )
        self.assertEqual(historical, current)
        # Not vacuous: the shared verdict is a real computed one, off the synthetic geometry's
        # own weights_bytes (48 layers * 2 heads * (256+256) * 2 B = 98,304 B/token).
        self.assertEqual(historical["verdict"], "pass")
        self.assertEqual(historical["observed"]["weights_bytes"], 6 * sweep.GIB)
        self.assertEqual(historical["observed"]["kv_bytes_per_token"], 98304.0)

    def test_populated_current_shape_overrides_weights_and_adds_the_indexer_term(self):
        item = verify_run.check_vram_arithmetic(
            "wpr2-record-cell",
            self._config(
                gpu_weights_bytes=3 * sweep.GIB,
                kv_attention_layers=12,
                swa_layer_spec=None,
                kv_indexer_geometry={"layers": 12, "key_length": 128, "value_length": 256},
            ),
        )
        # 12 * 2 * (256+256) * 2 = 24,576 main + 12 * (128+256) * 2 = 9,216 indexer.
        self.assertEqual(item["observed"]["kv_bytes_per_token"], 33792.0)
        self.assertEqual(item["observed"]["weights_bytes"], 3 * sweep.GIB)

    def test_a_malformed_recorded_geometry_stays_an_unknown_verdict(self):
        # The typed parse runs inside the same try the fit math does, so a record carrying a
        # half-written geometry block is still a verdict, never an exception out of the verifier.
        item = verify_run.check_vram_arithmetic(
            "wpr2-record-cell", self._config(kv_indexer_geometry={"layers": 12}),
        )
        self.assertEqual(item["verdict"], "unknown")
        self.assertIn("kv_indexer_geometry", item["observed"]["reason"])


class TestWp1KvGeometryVerifyRunRoundTrip(unittest.TestCase):
    """`gguf_fit._resolve_geometry_and_weights_raw` calls `read_gguf_metadata` through gguf_fit's
    OWN module binding; `verify_run._resolve_geometry` calls it through `sweep.read_gguf_
    metadata` -- a SEPARATE binding (`sweep.py` re-exports the name by import, which binds the
    object once, not a live module-attribute lookup). Both must be patched or one side silently
    falls through to the real, disk-dependent reader."""

    SYNTHETIC_HEADER: ClassVar[dict[str, Any]] = {
        "general.architecture": "qwen4exp",
        "qwen4exp.block_count": 48,
        "qwen4exp.attention.head_count": 24,
        "qwen4exp.attention.head_count_kv": 2,
        "qwen4exp.attention.key_length": 256,
        "qwen4exp.attention.value_length": 256,
        "qwen4exp.context_length": 262144,
    }

    def test_persisted_record_and_verifier_agree_on_the_fits_kv_bytes_per_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_gguf = Path(tmp) / "fake-qwen4exp.gguf"
            fake_gguf.write_bytes(b"x" * 128)  # disk-independent: the header read is mocked below
            queue = {
                "queue_format": 1,
                "vram_profile": "desktop-shared",
                "deadline": "23:59",
                "stop_policy": "pause-at-deadline",
                "models": {
                    "qwen4exp-cmoe": {
                        "model_host_path": str(fake_gguf),
                        "model_container_path": "/models/synthetic/qwen4exp.gguf",
                        "server_flags": {"-ngl": 99},
                        "gpu_weights_bytes": 3536007987,
                        "kv_attention_layers": 12,
                        "kv_indexer_geometry": {
                            "layers": 12, "key_length": 128, "value_length": 256,
                        },
                    },
                },
                "cells": [{
                    "model": "qwen4exp-cmoe", "alias": "q4x-roundtrip-c4k-f16", "c": 4096,
                    "ctk": "f16", "ctv": "f16", "fa": "auto", "battery": ["vram"],
                    "sampling_arm": "capability", "trials": 1,
                }],
            }
            batch.validate_queue(queue)
            cell = batch.resolve_queue_cells(queue)[0]

            with mock.patch.object(gguf_fit, "read_gguf_metadata", return_value=self.SYNTHETIC_HEADER), \
                 mock.patch.object(sweep, "read_gguf_metadata", return_value=self.SYNTHETIC_HEADER):
                out = batch.write_cell_record(
                    Path(tmp) / "records", cell, {},
                    vram_baseline_bytes=1000, vram_loaded_bytes=9000,
                    sampling_arm_effective="capability",
                )
                config = json.loads((out / "config.json").read_text())
                sweep_cell = config["sweep_cell"]

                # (i) the persisted sweep_cell carries all four passthroughs, positioned
                # immediately after spec_decode, with the queue's own values.
                keys = list(sweep_cell)
                after_spec_decode = keys[keys.index("spec_decode") + 1:]
                self.assertEqual(
                    after_spec_decode,
                    ["gpu_weights_bytes", "kv_attention_layers", "swa_layer_spec", "kv_indexer_geometry"],
                )
                self.assertEqual(sweep_cell["gpu_weights_bytes"], 3536007987)
                self.assertEqual(sweep_cell["kv_attention_layers"], 12)
                self.assertIsNone(sweep_cell["swa_layer_spec"])
                self.assertEqual(
                    sweep_cell["kv_indexer_geometry"],
                    {"layers": 12, "key_length": 128, "value_length": 256},
                )
                # ...and the cell carried it as the typed value the whole way (WP-R2): the
                # plain mapping above is rendered at the record boundary, not stored.
                self.assertEqual(
                    cell.fit.indexer_geometry,
                    batch.IndexerGeometry(layers=12, key_length=128, value_length=256),
                )
                self.assertEqual(cell.fit.gpu_weights_bytes, 3536007987)

                item = verify_run.check_vram_arithmetic(cell.cell_id, config)
                fit = sweep.classify_fit(cell.as_sweep_cell(), "desktop-shared")

            # (ii) the verifier's observed kv_bytes_per_token equals the fit's, and both equal
            # the hand-derived 33,792 B/token (24,576 main + 9,216 indexer).
            self.assertEqual(fit["kv_bytes_per_token"], 33792.0)
            self.assertEqual(item["observed"]["kv_bytes_per_token"], 33792.0)
            self.assertEqual(item["observed"]["kv_bytes_per_token"], fit["kv_bytes_per_token"])
            # (iii) observed weights_bytes is the queue's gpu_weights_bytes override, never the
            # fake file's on-disk size (128 bytes) -- the multi-shard fit trap this override
            # exists to close.
            self.assertEqual(item["observed"]["weights_bytes"], 3536007987)
            self.assertEqual(fit["weights_bytes"], 3536007987)
