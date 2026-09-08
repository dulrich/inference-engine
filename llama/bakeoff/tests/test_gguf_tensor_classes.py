#!/usr/bin/env python3
"""Tensor-class-derived placement in `gguf_fit.py` (WP-4,
`plans/moe-offload-load-mode-and-qwen4exp-fit-math.md`).

Four groups, matching the WP-4 brief's file scope:

(a) `GGML_TYPE_SIZES` spot checks -- the pinned bytes-per-block table, hand-verified against
    hand-computed byte counts (the table itself is cited to this host's own llama.cpp checkout,
    `<code-root>/apps/llama.cpp/ggml/{include/ggml.h,src/ggml-common.h}`, in `gguf_fit.py`'s
    own comment).
(b) `classify_tensor` name-classification table.
(c) Live-GGUF placement pins -- `unittest.skipUnless` when the host model path is absent (these
    read real, multi-GiB-to-tens-of-GiB GGUF files already on this fleet's `/srv/models`
    tree; never faked, matching this repo's "read directly, never assumed from weight size"
    convention). Confirms `derive_placement_bytes` reconciles to this session's live/hand-carried
    figures (`docs/qwen4exp-ple-ondisk-spike.md`), and that the pinned type table reproduces the
    SPIKE's class totals, not the plan's own rough "session-script" figures.
(d) Activation-gate tests -- a dense cell with no placement flag keeps the whole-file weights
    figure and gains no `derived_*` keys; a cell that carries BOTH a placement flag and a
    `gpu_weights_bytes` override keeps the override, and also gains no `derived_*` keys (the
    override always wins, Decisions locked 8). Both drawn from the real committed
    `matrix-v1-dedicated` queue through the exact resolution path `batch.py` uses live.

Entirely offline except (c)'s real GGUF header reads (no Docker, no network, no live sysfs) --
same posture as `test_fit_lock.py`.
"""

import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import gguf_fit  # noqa: E402

GIB = 1024**3

# Real, on-disk fleet models this session's WP-4 pins are grounded against -- see
# `docs/qwen4exp-ple-ondisk-spike.md` (Flash-Next) and `matrix-v1-dedicated.json`'s
# `qwen36-a3b-mtp` block / this repo's own live-measured `Vulkan0 model buffer size` (qwen36-a3b).
QWEN36_A3B_MTP_PATH = Path(
    "/srv/models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
)
FLASH_NEXT_SHARD1_PATH = Path(
    "/srv/models/unsloth/Qwen3.8-Flash-Next-GGUF/"
    "Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf"
)


# -------------------------------------------------------------------------------------------
# (a) GGML_TYPE_SIZES spot checks.
# -------------------------------------------------------------------------------------------


class TestGGMLTypeSizeTable(unittest.TestCase):
    """`GGML_TYPE_SIZES[ggml_type] = (block_elems, block_bytes)`; `nbytes = prod(dims) //
    block_elems * block_bytes` (the brief's own formula, `gguf_fit.py:20-26`'s conservative
    direction unaffected). Every entry here is hand-computed independently of the module's own
    comment/citation -- this is what would catch a transcription error in the pinned table."""

    def test_single_dim_spot_checks(self):
        cases = [
            # (ggml_type, dims, expected_nbytes, name)
            (1, (1024,), 2048, "F16"),  # no block quant: 1024 elem * 2 B/elem
            (8, (256,), 272, "Q8_0"),  # 256/32 = 8 blocks * 34 B/block
            (12, (256,), 144, "Q4_K"),  # 256/256 = 1 block * 144 B/block
            (20, (32,), 18, "IQ4_NL"),  # 32/32 = 1 block * 18 B/block -- SAME as Q4_0, not Q4_1
            (21, (256,), 110, "IQ3_S"),  # 256/256 = 1 block * 110 B/block
        ]
        for ggml_type, dims, expected, name in cases:
            with self.subTest(name=name):
                block_elems, block_bytes = gguf_fit.GGML_TYPE_SIZES[ggml_type]
                nelements = 1
                for d in dims:
                    nelements *= d
                nbytes = (nelements // block_elems) * block_bytes
                self.assertEqual(nbytes, expected)

    def test_multidim_tensor_prod_dims(self):
        """Cross-checks the `prod(dims)` part of the formula (not just a single-dim case) against
        a routed-expert-shaped 3D tensor this session's own live qwen36-a3b header carries
        (`blk.0.ffn_gate_exps.weight`, dims `(2048, 512, 256)`, `ggml_type` 12 = Q4_K):
        `nelements = 2048*512*256 = 268,435,456`; `268,435,456 // 256 = 1,048,576` blocks;
        `1,048,576 * 144 = 150,994,944` bytes -- hand-computed, not derived via the module's own
        formula (that would be circular)."""
        block_elems, block_bytes = gguf_fit.GGML_TYPE_SIZES[12]
        nelements = 2048 * 512 * 256
        nbytes = (nelements // block_elems) * block_bytes
        self.assertEqual(nbytes, 150_994_944)

    def test_unknown_type_id_is_absent(self):
        self.assertNotIn(31, gguf_fit.GGML_TYPE_SIZES)  # removed ids (Q4_0_4_4 family) stay out
        self.assertNotIn(999, gguf_fit.GGML_TYPE_SIZES)


# -------------------------------------------------------------------------------------------
# (b) classify_tensor table.
# -------------------------------------------------------------------------------------------


class TestClassifyTensor(unittest.TestCase):
    def test_classification_table(self):
        cases = [
            ("blk.5.ffn_gate_exps.weight", "routed_expert"),
            ("blk.5.ffn_up_exps.weight", "routed_expert"),
            ("blk.5.ffn_down_exps.weight", "routed_expert"),
            ("blk.40.ffn_gate_exps.weight", "routed_expert"),
            ("blk.5.ffn_gate_shexp.weight", "shared_expert"),
            ("blk.5.ffn_up_shexp.weight", "shared_expert"),
            ("blk.5.ffn_down_shexp.weight", "shared_expert"),
            ("blk.5.ffn_gate_inp_shexp.weight", "shared_expert"),
            ("per_layer_token_embd.weight", "ple"),
            ("blk.1.ple_key.weight", "ple"),
            ("blk.1.ple_value.weight", "ple"),
            ("blk.1.ple_conv1d.weight", "ple"),
            ("token_embd.weight", "embd_output"),
            ("output.weight", "embd_output"),
            ("output_norm.weight", "embd_output"),
            ("blk.5.attn_q.weight", "dense"),
            ("blk.5.attn_qkv.weight", "dense"),
            ("blk.5.ssm_out.weight", "dense"),
            ("blk.5.ffn_gate_inp.weight", "dense"),  # the router gate -- not "_exps", not moved
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(gguf_fit.classify_tensor(name), expected)


# -------------------------------------------------------------------------------------------
# (c) Live-GGUF placement pins.
# -------------------------------------------------------------------------------------------


def _within_pct(actual: int, target: float, pct: float) -> bool:
    return abs(actual - target) / target <= pct / 100.0


@unittest.skipUnless(
    QWEN36_A3B_MTP_PATH.exists(), f"host GGUF not present: {QWEN36_A3B_MTP_PATH}"
)
class TestQwen36A3bLivePlacementPin(unittest.TestCase):
    """`-ncmoe 32` against the real qwen36-a3b-mtp GGUF -- both the hand-carried
    `matrix-v1-dedicated.json` `gpu_weights_bytes` (6,620,000,000 B) and this session's own live
    `Vulkan0 model buffer size` (6421.16 MiB) reading."""

    @classmethod
    def setUpClass(cls):
        cls.metadata = gguf_fit.read_gguf_metadata(QWEN36_A3B_MTP_PATH)
        cls.geometry = gguf_fit.gguf_attention_geometry(cls.metadata)
        cls.tensor_infos = gguf_fit.read_gguf_tensor_infos(QWEN36_A3B_MTP_PATH)

    def test_ncmoe_32_within_3_percent_of_both_targets(self):
        self.assertIsNotNone(self.tensor_infos)
        placement = gguf_fit.derive_placement_bytes(
            self.tensor_infos, {"-ngl": 99, "-ncmoe": 32}, self.geometry
        )
        self.assertIsNotNone(placement)
        hand_carried_bytes = 6_620_000_000
        live_bytes = 6421.16 * 1024 * 1024
        self.assertTrue(
            _within_pct(placement.gpu_bytes, hand_carried_bytes, 3.0),
            f"{placement.gpu_bytes:,} vs hand-carried {hand_carried_bytes:,}",
        )
        self.assertTrue(
            _within_pct(placement.gpu_bytes, live_bytes, 3.0),
            f"{placement.gpu_bytes:,} vs live {live_bytes:,.0f}",
        )


@unittest.skipUnless(
    FLASH_NEXT_SHARD1_PATH.exists(), f"host GGUF not present: {FLASH_NEXT_SHARD1_PATH}"
)
class TestFlashNextLivePlacementPins(unittest.TestCase):
    """qwen4exp (Qwen3.8-Flash-Next) -- both arms measured in
    `docs/qwen4exp-ple-ondisk-spike.md`."""

    @classmethod
    def setUpClass(cls):
        cls.metadata = gguf_fit.read_gguf_metadata(FLASH_NEXT_SHARD1_PATH)
        cls.geometry = gguf_fit.gguf_attention_geometry(cls.metadata)
        cls.tensor_infos = gguf_fit.read_gguf_tensor_infos(FLASH_NEXT_SHARD1_PATH)

    def test_tensor_infos_span_all_three_shards(self):
        self.assertIsNotNone(self.tensor_infos)
        # docs/qwen4exp-ple-ondisk-spike.md: "1224 tensors" across all three shards -- shard 1
        # alone is metadata-only (0 tensors), so this also proves the sibling-shard walk ran.
        self.assertEqual(len(self.tensor_infos), 1224)

    def test_cmoe_ot_within_3_percent(self):
        flags = {
            "-ngl": 99, "-cmoe": True,
            "-ot": "per_layer_token_embd\\.weight=CPU", "-lm": "mmap",
        }
        placement = gguf_fit.derive_placement_bytes(self.tensor_infos, flags, self.geometry)
        self.assertIsNotNone(placement)
        target = 3_536_007_987
        self.assertTrue(
            _within_pct(placement.gpu_bytes, target, 3.0),
            f"{placement.gpu_bytes:,} vs {target:,}",
        )
        # The whole PLE table is lazy (mmap in force, no explicit --tensor-read-lazy, tensor >
        # 4 GiB) and excluded from cpu_bytes -- audit E1's pin.
        self.assertGreater(placement.lazy_bytes, 4 * GIB)

    def test_ncmoe_38_ot_within_3_percent(self):
        flags = {
            "-ngl": 99, "-ncmoe": 38,
            "-ot": "per_layer_token_embd\\.weight=CPU", "-lm": "mmap",
        }
        placement = gguf_fit.derive_placement_bytes(self.tensor_infos, flags, self.geometry)
        self.assertIsNotNone(placement)
        target = 13_104_263_987
        self.assertTrue(
            _within_pct(placement.gpu_bytes, target, 3.0),
            f"{placement.gpu_bytes:,} vs {target:,}",
        )

    def test_class_totals_reconcile_with_the_spike_not_the_session_script(self):
        """`docs/qwen4exp-ple-ondisk-spike.md:18-21` independently measured 144 routed-expert
        tensors totalling 42.92 GiB, and a 26.82 GiB PLE table, from THIS SAME GGUF's tensor
        directory. The plan's own Context section separately carried a rough "session-script"
        table that instead computed 45.34/29.84 GiB for the same two figures, from a bytes-per-
        type table it flagged as unverified. WP-4's job (Decisions locked 8 / Assumption 4) was
        to determine which of the two this WP's OWN pinned `GGML_TYPE_SIZES` table (verified
        directly against `ggml/src/ggml-common.h`'s `static_assert` lines this session) actually
        reproduces -- it's the spike's, not the session script's. Root cause: the session script
        used the Q4_1 block size (20 B/32 elem) for ggml type id 20; the correct value for that id
        (IQ4_NL) is 18 B/32 elem, byte-identical to Q4_0 -- see `gguf_fit.py`'s `GGML_TYPE_SIZES`
        comment."""
        self.assertIsNotNone(self.tensor_infos)
        routed_bytes = sum(
            t.nbytes for t in self.tensor_infos if gguf_fit.classify_tensor(t.name) == "routed_expert"
        )
        routed_count = sum(
            1 for t in self.tensor_infos if gguf_fit.classify_tensor(t.name) == "routed_expert"
        )
        self.assertEqual(routed_count, 144)
        self.assertAlmostEqual(routed_bytes / GIB, 42.92, places=2)  # NOT the session's 45.34

        ple_table_bytes = next(
            t.nbytes for t in self.tensor_infos if t.name == "per_layer_token_embd.weight"
        )
        self.assertAlmostEqual(ple_table_bytes / GIB, 26.82, places=2)  # NOT the session's 29.84


# -------------------------------------------------------------------------------------------
# (d) Activation-gate tests -- real committed queue cells, real resolution path.
# -------------------------------------------------------------------------------------------


class TestActivationGate(unittest.TestCase):
    """Decisions locked 8 / the WP-4 brief's hard constraint: the derivation activates ONLY when
    (a) no `gpu_weights_bytes` override is present AND (b) a recognised CPU-placement flag is
    present. Both cells here are drawn from the real committed `matrix-v1-dedicated` queue,
    through the exact same `load_queue` -> `resolve_queue_cells` -> `as_sweep_cell()` path
    `batch.py`/`test_fit_lock.py` use live -- not hand-built fixtures."""

    @classmethod
    def setUpClass(cls):
        queue = batch.load_queue(BAKEOFF_DIR / "queues" / "matrix-v1-dedicated.json")
        cls.cells = {c.alias: c.as_sweep_cell() for c in batch.resolve_queue_cells(queue)}

    def test_dense_cell_with_no_placement_flag_keeps_whole_file_and_gains_no_derived_keys(self):
        cell = self.cells["mxd-qwen38-iq3s-c128k-q4q4-fa"]
        flags = cell["server_flags"]
        self.assertNotIn("-ncmoe", flags)
        self.assertNotIn("-cmoe", flags)
        self.assertNotIn("-ot", flags)
        self.assertNotIn("gpu_weights_bytes", cell)
        fit = gguf_fit.classify_fit(cell, vram_profile="desktop-shared")
        self.assertEqual(fit["weights_bytes"], Path(cell["model_host_path"]).stat().st_size)
        self.assertNotIn("derived_cpu_weights_bytes", fit)
        self.assertNotIn("derived_lazy_bytes", fit)

    def test_placement_flag_cell_with_an_override_keeps_the_override_and_gains_no_derived_keys(self):
        cell = self.cells["mxd-qwen36-c128k-f16-mtp"]
        self.assertIn("-ncmoe", cell["server_flags"])
        self.assertEqual(cell.get("gpu_weights_bytes"), 6_620_000_000)
        fit = gguf_fit.classify_fit(cell, vram_profile="desktop-shared")
        self.assertEqual(fit["weights_bytes"], 6_620_000_000)
        self.assertNotIn("derived_cpu_weights_bytes", fit)
        self.assertNotIn("derived_lazy_bytes", fit)


if __name__ == "__main__":
    unittest.main()
