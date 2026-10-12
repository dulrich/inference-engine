"""Tests owned by `queue_file.py`: queue validation, cell resolution, deadline policy, the
WP-6 chars-per-token knob, WP-2 speculative-decode validation, WP-1 KV-indexer-geometry
validation, and WP-1 `request_timeout_s` validation. Split from the former `test_batch.py`
omnibus module (WP-R3 of plans/queue-contract-and-test-decomposition-remediation.md); no test
logic changed in the move.

No live Docker/GPU execution anywhere in this suite -- fixtures imported from `batch_fixtures`
are always fakes."""

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from batch_fixtures import (  # noqa: E402
    _fake_battery_fn_factory,
    _handle,
    _longcontext_cell,
    _minimal_queue,
    _sample,
    _write_queue,
)

import batch  # noqa: E402
import longcontext  # noqa: E402
import sweep  # noqa: E402


class TestQueueValidation(unittest.TestCase):
    def test_valid_minimal_queue_passes(self):
        batch.validate_queue(_minimal_queue())  # must not raise

    def test_missing_queue_format_raises(self):
        q = _minimal_queue()
        del q["queue_format"]
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_wrong_queue_format_version_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(queue_format=2))

    def test_empty_models_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(models={}))

    def test_empty_cells_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(cells=[]))

    def test_cell_referencing_unknown_model_raises(self):
        q = _minimal_queue()
        q["cells"][0]["model"] = "model-nonexistent"
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_missing_deadline_raises(self):
        q = _minimal_queue()
        del q["deadline"]
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_malformed_deadline_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(deadline="not-a-time"))

    def test_unknown_vram_profile_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(vram_profile="gaming-rig"))

    def test_unknown_stop_policy_raises(self):
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(_minimal_queue(stop_policy="hard-stop"))

    def test_cell_missing_required_key_raises(self):
        q = _minimal_queue()
        del q["cells"][0]["trials"]
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(q)

    def test_load_queue_from_disk_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_queue(tmp, _minimal_queue())
            loaded = batch.load_queue(path)
            self.assertEqual(loaded["queue_format"], 1)


class TestResolveQueueCells(unittest.TestCase):
    def test_resolves_every_cell_with_merged_flags(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        self.assertEqual(len(resolved), 4)
        first = resolved[0]
        self.assertEqual(first.server_flags["-c"], 32768)
        self.assertEqual(first.server_flags["-ctk"], "f16")
        self.assertEqual(first.server_flags["-ngl"], 99)  # inherited from model-a base flags
        self.assertEqual(first.queue.model_id, "model-a")
        self.assertEqual(first.queue.sampling_arm, "capability")
        self.assertEqual(first.queue.trials, 3)
        # Execution settings are DERIVED from the normalized flags, not separate state.
        self.assertEqual(first.execution.context, 32768)
        self.assertEqual(first.execution.kv_key_type, "f16")

    def test_gpu_weights_bytes_and_kv_attention_layers_propagate_from_model_registry(self):
        q = _minimal_queue(cells=[{
            "model": "model-b", "alias": "model-b-c8k", "c": 8192, "ctk": "f16", "ctv": "f16",
            "fa": "on", "battery": ["vram"], "sampling_arm": "capability", "trials": 1,
        }])
        resolved = batch.resolve_queue_cells(q)
        self.assertEqual(resolved[0].fit.gpu_weights_bytes, 6 * sweep.GIB)
        self.assertEqual(resolved[0].fit.kv_attention_layers, 10)
        self.assertEqual(resolved[0].server_flags["-ncmoe"], 32)
        # ...and they reach sweep.py through the one dict adapter, unchanged.
        self.assertEqual(resolved[0].as_sweep_cell()["gpu_weights_bytes"], 6 * sweep.GIB)

    def test_model_family_propagates_from_model_registry_when_present(self):
        q = _minimal_queue()
        q["models"]["model-a"]["model_family"] = "gemma4"
        resolved = batch.resolve_queue_cells(q)
        self.assertEqual(resolved[0].model_family, "gemma4")

    def test_model_family_absent_when_registry_omits_it(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())  # model-a carries no model_family
        self.assertIsNone(resolved[0].model_family)
        self.assertNotIn("model_family", resolved[0].as_sweep_cell())

    def test_cell_ids_are_distinct(self):
        resolved = batch.resolve_queue_cells(_minimal_queue())
        ids = [c.cell_id for c in resolved]
        self.assertEqual(len(ids), len(set(ids)))

    def test_colliding_cell_ids_raise(self):
        q = _minimal_queue()
        q["cells"] = [dict(q["cells"][0]), dict(q["cells"][0])]  # byte-identical duplicate
        with self.assertRaises(batch.QueueValidationError):
            batch.resolve_queue_cells(q)


class TestDeadline(unittest.TestCase):
    def test_deadline_later_today_stays_today(self):
        start = datetime(2026, 8, 23, 2, 0, tzinfo=UTC)
        deadline_dt = batch.compute_deadline_dt("07:00", start)
        self.assertEqual(deadline_dt, datetime(2026, 8, 23, 7, 0, tzinfo=UTC))

    def test_deadline_already_passed_today_rolls_to_tomorrow(self):
        start = datetime(2026, 8, 23, 23, 0, tzinfo=UTC)
        deadline_dt = batch.compute_deadline_dt("07:00", start)
        self.assertEqual(deadline_dt, datetime(2026, 8, 24, 7, 0, tzinfo=UTC))

    def test_deadline_exactly_at_start_rolls_to_tomorrow(self):
        start = datetime(2026, 8, 23, 7, 0, tzinfo=UTC)
        deadline_dt = batch.compute_deadline_dt("07:00", start)
        self.assertEqual(deadline_dt, datetime(2026, 8, 24, 7, 0, tzinfo=UTC))


class TestWp6CharsPerToken(unittest.TestCase):
    """WP-6: per-model measured `chars_per_token` (WP-5 haystack-calibration finding)."""

    def test_validate_queue_rejects_non_positive_or_non_numeric(self):
        for bad in (0, -1.5, "4.2", True):
            queue = _minimal_queue()
            queue["models"]["model-a"]["chars_per_token"] = bad
            with self.assertRaises(batch.QueueValidationError):
                batch.validate_queue(queue)

    def test_validate_queue_accepts_positive_float_and_absence(self):
        queue = _minimal_queue()
        batch.validate_queue(queue)  # absent everywhere: fine
        queue["models"]["model-a"]["chars_per_token"] = 4.24
        batch.validate_queue(queue)  # present and positive: fine

    def test_resolved_cell_carries_ratio_without_changing_cell_id(self):
        plain = batch.resolve_queue_cells(_minimal_queue())
        queue = _minimal_queue()
        queue["models"]["model-a"]["chars_per_token"] = 4.24
        with_ratio = batch.resolve_queue_cells(queue)
        for before, after in zip(plain, with_ratio):
            # The knob is sizing metadata, never cell identity: same cell_id either way -- and
            # structurally so now, since it does not live on `CellIdentity` at all.
            self.assertEqual(before.cell_id, after.cell_id)
        for cell in with_ratio:
            self.assertEqual(cell.queue.chars_per_token, 4.24)  # every cell is model-a's
        for cell in plain:
            self.assertIsNone(cell.queue.chars_per_token)  # absent in the registry -> None

    def test_runner_forwards_ratio_and_task_caps_to_battery_and_detail(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "correct", True)],
        })
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
        )
        cell = _longcontext_cell(chars_per_token=4.06)
        outcome = runner(_handle(), cell)
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(battery_fn.calls[0]["chars_per_token"], 4.06)
        self.assertEqual(
            battery_fn.calls[0]["max_tokens_by_task"], batch.LONGCONTEXT_MAX_TOKENS_BY_TASK
        )
        self.assertEqual(outcome.detail["chars_per_token"], 4.06)

    def test_runner_forwards_none_when_queue_carries_no_ratio(self):
        battery_fn = _fake_battery_fn_factory({
            "single_needle": [_sample("single_needle", "correct", True)],
        })
        runner = batch.make_longcontext_cell_runner(
            battery_fn=battery_fn, vram_used_bytes_fn=lambda: 5000,
        )
        outcome = runner(_handle(), _longcontext_cell())  # helper carries no chars_per_token
        self.assertEqual(outcome.status, "completed")
        self.assertIsNone(battery_fn.calls[0]["chars_per_token"])
        self.assertIsNone(outcome.detail["chars_per_token"])


class TestWp2SpecDecode(unittest.TestCase):
    """WP-2 (plans/overnight-test-matrix-redesign.md): the base-vs-MTP axis, the additive
    optional abbreviated-cell key `spec_decode: "base" | "mtp"`."""

    def _mtp_queue(self, **cell_overrides):
        queue = _minimal_queue()
        queue["models"]["model-mtp"] = {
            "model_container_path": "/models/mtp/model-mtp.gguf",
            "server_flags": {
                "-ngl": 99,
                "-md": "/models/mtp/model-mtp-draft.gguf",
                "--spec-type": "draft-mtp",
            },
        }
        cell = {
            "model": "model-mtp", "alias": "model-mtp-c32k-f16", "c": 32768,
            "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
            "sampling_arm": "capability", "trials": 3,
        }
        cell.update(cell_overrides)
        queue["cells"] = [cell]
        return queue

    # -- resolve_cell: base strips, mtp asserts/passes, absent is a no-op ------------------

    def test_base_strips_md_and_spec_type_from_a_registry_entry_carrying_both(self):
        resolved = batch.resolve_queue_cells(self._mtp_queue(spec_decode="base"))[0]
        self.assertNotIn("-md", resolved.server_flags)
        self.assertNotIn("--spec-type", resolved.server_flags)
        self.assertEqual(resolved.queue.spec_decode, "base")

    def test_mtp_passes_through_untouched_when_registry_carries_spec_type(self):
        resolved = batch.resolve_queue_cells(self._mtp_queue(spec_decode="mtp"))[0]
        self.assertEqual(resolved.server_flags["--spec-type"], "draft-mtp")
        self.assertEqual(resolved.server_flags["-md"], "/models/mtp/model-mtp-draft.gguf")
        self.assertEqual(resolved.queue.spec_decode, "mtp")

    def test_mtp_raises_when_registry_entry_carries_no_spec_type(self):
        queue = self._mtp_queue(spec_decode="mtp")
        queue["models"]["model-mtp"]["server_flags"] = {"-ngl": 99}  # no MTP flags at all
        with self.assertRaises(batch.QueueValidationError):
            batch.resolve_queue_cells(queue)

    def test_absent_key_is_a_no_op_on_flags_but_stamps_mtp_byte_identical_cell_id(self):
        resolved = batch.resolve_queue_cells(self._mtp_queue())[0]  # no spec_decode key
        # served-recipe-p50-prefill-and-decode WP-2: an undeclared cell whose resolved flags
        # carry `--spec-type`/`-md` is stamped "mtp" -- never left `None` -- so a record is
        # self-describing about MTP without requiring every queue author to declare it.
        self.assertEqual(resolved.queue.spec_decode, "mtp")
        # The registry's MTP flags survive untouched -- absence means "as-is", never a strip.
        self.assertEqual(resolved.server_flags["-md"], "/models/mtp/model-mtp-draft.gguf")
        self.assertEqual(resolved.server_flags["--spec-type"], "draft-mtp")
        # The stamp is non-identity (`QueueCellMetadata`, not `CellIdentity`): same
        # recompute-and-compare shape as the `model`/`chars_per_token` precedents -- identity is
        # exactly what `sweep.compute_cell_id` derives from the four identity fields alone.
        self.assertEqual(sweep.compute_cell_id(resolved.as_sweep_cell()), resolved.cell_id)
        self.assertEqual(
            sweep.compute_cell_id(resolved.identity.identity_payload()), resolved.cell_id,
        )

    def test_base_and_mtp_arms_of_the_same_cell_produce_distinct_cell_ids(self):
        base_cell = batch.resolve_queue_cells(self._mtp_queue(spec_decode="base"))[0]
        mtp_cell = batch.resolve_queue_cells(self._mtp_queue(spec_decode="mtp"))[0]
        self.assertNotEqual(base_cell.cell_id, mtp_cell.cell_id)

    # -- validate_queue: malformed values only ----------------------------------------------

    def test_validate_queue_rejects_malformed_values(self):
        for bad in ("draft", 1, None):
            queue = self._mtp_queue()
            queue["cells"][0]["spec_decode"] = bad  # present (even as explicit null) but bad
            with self.assertRaises(batch.QueueValidationError):
                batch.validate_queue(queue)

    def test_validate_queue_accepts_base_mtp_and_absence(self):
        batch.validate_queue(self._mtp_queue())  # absent entirely: fine
        batch.validate_queue(self._mtp_queue(spec_decode="base"))
        batch.validate_queue(self._mtp_queue(spec_decode="mtp"))

    # -- write_cell_record: sweep_cell.spec_decode round-trips, nullable --------------------

    def _one_task_battery_results(self):
        return {"single_needle": {0.45: longcontext.CellResult(
            samples=[_sample("single_needle", "correct", True)], completions=[], summary=None,
        )}}

    def test_record_round_trip_persists_spec_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("base", "mtp", None):
                cell = _longcontext_cell(spec_decode=value)
                out = batch.write_cell_record(
                    Path(tmp) / f"run-{value}", cell, self._one_task_battery_results(),
                    vram_baseline_bytes=1, vram_loaded_bytes=2,
                    sampling_arm_effective="capability",
                )
                config = json.loads((out / "config.json").read_text())
                self.assertEqual(config["sweep_cell"]["spec_decode"], value)

    def test_record_defaults_to_none_when_cell_never_declared_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = batch.write_cell_record(
                Path(tmp) / "run", _longcontext_cell(), self._one_task_battery_results(),
                vram_baseline_bytes=1, vram_loaded_bytes=2,
                sampling_arm_effective="capability",
            )
            config = json.loads((out / "config.json").read_text())
            self.assertIn("spec_decode", config["sweep_cell"])  # key present, not just absent
            self.assertIsNone(config["sweep_cell"]["spec_decode"])


class TestServedRecipeSpecDecodeStamp(unittest.TestCase):
    """`plans/served-recipe-p50-prefill-and-decode.md` WP-2: `resolve_cell` stamps
    `spec_decode` from the resolved `server_flags` when the queue declared nothing, so a newly
    resolved cell is self-describing about MTP without every queue author declaring it by hand.
    Exercised against the real, committed `matrix-v1-dedicated-prod.json` registry+cells (not a
    synthetic fixture), so the pinned `cell_id`s are the ones a live run actually produces."""

    QUEUE_PATH = BAKEOFF_DIR / "queues" / "matrix-v1-dedicated-prod.json"

    @classmethod
    def setUpClass(cls):
        cls.resolved = {
            cell.identity.alias: cell
            for cell in batch.resolve_queue_cells(batch.load_queue(cls.QUEUE_PATH))
        }

    def test_mtp_cell_with_no_declaration_is_stamped_mtp_cell_id_unmoved(self):
        # qwen36-a3b-mtp's registry entry carries `--spec-type` (embedded MTP heads, no `-md`
        # sidecar); this cell (`mxdp-qwen36-c128k-f16-mtp-prod`) never declares `spec_decode`.
        cell = self.resolved["mxdp-qwen36-c128k-f16-mtp-prod"]
        self.assertEqual(cell.queue.spec_decode, "mtp")
        self.assertEqual(
            cell.cell_id, "mxdp-qwen36-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-f330211a",
        )

    def test_plain_cell_with_no_declaration_is_none_never_base_cell_id_unmoved(self):
        # qwen38-iq3s's registry entry carries no MTP flags at all; this cell
        # (`mxdp-qwen38-iq3s-c128k-q4q4-fa-prod`) never declares `spec_decode` either.
        cell = self.resolved["mxdp-qwen38-iq3s-c128k-q4q4-fa-prod"]
        self.assertIsNone(cell.queue.spec_decode)
        self.assertNotEqual(cell.queue.spec_decode, "base")
        self.assertEqual(
            cell.cell_id,
            "mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-ctkq4_0-ctvq4_0-faon-c131072-0d8108e6",
        )

    def test_explicit_base_declaration_survives_against_an_mtp_serving_registry_entry(self):
        # `mxdp-qwen36-c128k-f16-timing-base-think` explicitly declares "base" against the same
        # qwen36-a3b-mtp registry entry the mtp-arm cell above resolves -- the explicit
        # declaration must win over the resolved-flags inference, not merely agree with it (this
        # cell's OWN resolved flags no longer carry `--spec-type`/`-md`, since
        # `_merge_server_flags` strips them for a "base" declaration before `spec_decode`'s
        # stamp is computed).
        cell = self.resolved["mxdp-qwen36-c128k-f16-timing-base-think"]
        self.assertEqual(cell.queue.spec_decode, "base")
        self.assertNotIn("--spec-type", cell.server_flags)
        self.assertNotIn("-md", cell.server_flags)


class TestWp1KvIndexerGeometryValidation(unittest.TestCase):
    """C4-B1: the optional per-model `kv_indexer_geometry` -- a dict with EXACTLY the keys
    `layers`/`key_length`/`value_length`, each a positive int."""

    def test_validate_queue_accepts_a_well_formed_geometry(self):
        queue = _minimal_queue()
        queue["models"]["model-a"]["kv_indexer_geometry"] = {
            "layers": 12, "key_length": 128, "value_length": 256,
        }
        batch.validate_queue(queue)  # must not raise

    def test_validate_queue_accepts_absence(self):
        batch.validate_queue(_minimal_queue())  # must not raise

    def test_validate_queue_rejects_a_missing_key(self):
        queue = _minimal_queue()
        queue["models"]["model-a"]["kv_indexer_geometry"] = {"layers": 12, "key_length": 128}
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(queue)

    def test_validate_queue_rejects_an_extra_key(self):
        queue = _minimal_queue()
        queue["models"]["model-a"]["kv_indexer_geometry"] = {
            "layers": 12, "key_length": 128, "value_length": 256, "extra": 1,
        }
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(queue)

    def test_validate_queue_rejects_a_non_positive_member(self):
        for bad in (0, -1):
            with self.subTest(bad=bad):
                queue = _minimal_queue()
                queue["models"]["model-a"]["kv_indexer_geometry"] = {
                    "layers": bad, "key_length": 128, "value_length": 256,
                }
                with self.assertRaises(batch.QueueValidationError):
                    batch.validate_queue(queue)

    def test_validate_queue_rejects_a_bool_member(self):
        queue = _minimal_queue()
        queue["models"]["model-a"]["kv_indexer_geometry"] = {
            "layers": True, "key_length": 128, "value_length": 256,
        }
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(queue)

    def test_validate_queue_rejects_a_non_dict(self):
        queue = _minimal_queue()
        queue["models"]["model-a"]["kv_indexer_geometry"] = "not-a-dict"
        with self.assertRaises(batch.QueueValidationError):
            batch.validate_queue(queue)

    def test_resolved_cell_carries_the_geometry_to_the_sweep_adapter(self):
        queue = _minimal_queue()
        geometry = {"layers": 12, "key_length": 128, "value_length": 256}
        queue["models"]["model-a"]["kv_indexer_geometry"] = geometry
        resolved = batch.resolve_queue_cells(queue)
        self.assertEqual(
            resolved[0].fit.indexer_geometry,
            batch.IndexerGeometry(layers=12, key_length=128, value_length=256),
        )
        # ...and the adapter still emits the plain, ordered mapping the fit math and the record
        # writer both consume -- byte-identical to the queue's own block.
        self.assertEqual(resolved[0].as_sweep_cell()["kv_indexer_geometry"], geometry)
        self.assertEqual(
            list(resolved[0].as_sweep_cell()["kv_indexer_geometry"]),
            ["layers", "key_length", "value_length"],
        )

    def test_resolved_cell_carries_the_geometry_without_changing_cell_id(self):
        plain = batch.resolve_queue_cells(_minimal_queue())
        queue = _minimal_queue()
        queue["models"]["model-a"]["kv_indexer_geometry"] = {
            "layers": 12, "key_length": 128, "value_length": 256,
        }
        with_geometry = batch.resolve_queue_cells(queue)
        for before, after in zip(plain, with_geometry):
            self.assertEqual(before.cell_id, after.cell_id)


class TestWp1RequestTimeoutSValidation(unittest.TestCase):
    def test_validate_queue_accepts_a_positive_int(self):
        batch.validate_queue(_minimal_queue(request_timeout_s=1800))  # must not raise

    def test_validate_queue_rejects_non_positive_or_non_integer(self):
        for bad in (0, -1, 1.5, "1800", True):
            with self.subTest(bad=bad):
                with self.assertRaises(batch.QueueValidationError):
                    batch.validate_queue(_minimal_queue(request_timeout_s=bad))

    def test_absent_reads_as_300_for_every_committed_queue_that_omits_it(self):
        # The "five committed queues unchanged" proof (plan Test Plan (a)): every committed
        # queue file that does not itself declare request_timeout_s must still resolve its
        # EFFECTIVE value to today's factory default. Iterates every queues/*.json on disk
        # (excluding .state.json siblings) rather than a hand-picked subset, so this proof stays
        # accurate however many committed queues exist by the time this test runs.
        queues_dir = BAKEOFF_DIR / "queues"
        committed = [p for p in queues_dir.glob("*.json") if not p.name.endswith(".state.json")]
        self.assertTrue(committed)  # sanity: the glob actually found something
        checked_absent = 0
        for path in committed:
            with self.subTest(queue=path.name):
                raw = json.loads(path.read_text())
                if "request_timeout_s" in raw:
                    continue  # a queue that declares its own value is checked elsewhere
                queue = batch.load_queue(path)
                self.assertEqual(batch.effective_request_timeout_s(queue), 300)
                checked_absent += 1
        self.assertGreaterEqual(checked_absent, 5)  # the five pre-WP-1 queues actually ran
