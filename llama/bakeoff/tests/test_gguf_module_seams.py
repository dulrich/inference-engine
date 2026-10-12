#!/usr/bin/env python3
"""The `gguf_*` module seams created by WP-R3 of
`plans/post-audit-architecture-and-reconciliation-remediation.md` (review finding F5).

`gguf_fit.py` crossed 1,000 lines by carrying four separable domains; WP-R3 split the binary
format (`gguf_reader.py`), placement (`gguf_placement.py`), and attention/KV geometry
(`gguf_cache.py`) into focused siblings and left the VRAM-budget/fit-policy owner behind. That
split is only safe while three properties hold, and nothing else in the suite asserts them
directly:

(a) **The facade is re-export, never a wrapper.** Every name moved out of `gguf_fit` must still
    resolve THROUGH `gguf_fit` as the *same object* (`is`, not merely equal) -- five in-package
    consumers (`queue_file`, `scratch_lifecycle`, `sweep`, `cell_spec`, `cell_model`) and three
    test modules spell these names off `gguf_fit`/`sweep`, and
    `mock.patch.object(gguf_fit, "read_gguf_metadata", ...)` in `test_record_contract.py` and
    `test_fit_lock.py` only works while `gguf_fit` owns a module-global binding for it.
(b) **The dependency edges run one way.** `gguf_reader` imports no sibling (it is the base, which
    is why `GIB` lives there); `gguf_placement` and `gguf_cache` import only `gguf_reader`;
    `gguf_fit` composes all three. A back-edge would close an import cycle on a flat,
    `__init__.py`-free layout where every module is a top-level name.
(c) **The finding stays fixed.** Every production module of the family stays under 1,000 lines.

Plus the behaviour anchors the WP-R3 brief names as having to survive byte-identical across the
move -- the `GGUFParseError` failure DIRECTION for an unknown ggml type, split-shard discovery,
array/scalar metadata head semantics, placement precedence, and the injected sysfs seams. Those
are asserted here against the new module boundaries rather than only through `gguf_fit`, so a
future edit to one sibling cannot quietly change them.

Entirely offline: the only file I/O is a handful of synthetic GGUF byte blobs written to a
tempdir. No Docker, no network, no live sysfs, no GPU.
"""

import inspect
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import gguf_cache  # noqa: E402
import gguf_fit  # noqa: E402
import gguf_placement  # noqa: E402
import gguf_reader  # noqa: E402
import sweep  # noqa: E402

# The split, as a data table: which sibling owns which public name. Every entry is asserted to
# re-export identically through `gguf_fit` (and, for the names `sweep.py`'s facade already
# carried, onward through `sweep`).
MOVED_NAMES = {
    "gguf_reader": (
        "GGML_TYPE_SIZES",
        "GIB",
        "GGUFParseError",
        "TensorInfo",
        "read_gguf_metadata",
        "read_gguf_tensor_infos",
    ),
    "gguf_placement": (
        "PlacementBytes",
        "classify_tensor",
        "derive_placement_bytes",
    ),
    "gguf_cache": (
        "INDEXER_GEOMETRY_KEYS",
        "KV_TYPE_BYTES_PER_ELEMENT",
        "IndexerGeometry",
        "gguf_attention_geometry",
        "kv_bytes_per_token",
    ),
}

# The names `sweep.py`'s own public facade re-exports onward out of the moved set. The rest of
# `MOVED_NAMES` was never on `sweep` (the WP-4 tensor-placement surface is spelled off
# `gguf_fit` directly by `tests/test_gguf_tensor_classes.py`), and this WP does not widen it.
SWEEP_FACADE_NAMES = (
    "GIB",
    "GGUFParseError",
    "IndexerGeometry",
    "KV_TYPE_BYTES_PER_ELEMENT",
    "gguf_attention_geometry",
    "kv_bytes_per_token",
    "read_gguf_metadata",
)

SIBLING_MODULES = {
    "gguf_reader": gguf_reader,
    "gguf_placement": gguf_placement,
    "gguf_cache": gguf_cache,
}

_GGUF_TYPE_UINT32 = 4
_GGUF_TYPE_STRING = 8
_GGUF_TYPE_ARRAY = 9


def _gguf_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _build_gguf(
    metadata: "list[tuple[str, int, object]]",
    tensors: "list[tuple[str, tuple[int, ...], int]] | None" = None,
) -> bytes:
    """Synthesizes a minimal, spec-shaped GGUF header + KV block + tensor-info directory.

    `metadata` entries are `(key, gguf_value_type, value)` with only the three value types these
    tests need (u32 scalar, string, and an array of u32) -- enough to exercise the array/scalar
    head semantics without pulling in a GGUF writer this repo deliberately does not have.
    """
    tensors = tensors or []
    out = bytearray(b"GGUF")
    out += struct.pack("<I", 3)  # version
    out += struct.pack("<Q", len(tensors))
    out += struct.pack("<Q", len(metadata))
    for key, value_type, value in metadata:
        out += _gguf_string(key)
        out += struct.pack("<I", value_type)
        if value_type == _GGUF_TYPE_STRING:
            out += _gguf_string(str(value))
        elif value_type == _GGUF_TYPE_ARRAY:
            assert isinstance(value, (list, tuple))
            out += struct.pack("<I", _GGUF_TYPE_UINT32)
            out += struct.pack("<Q", len(value))
            for elem in value:
                out += struct.pack("<I", int(elem))
        else:
            out += struct.pack("<I", int(value))  # type: ignore[arg-type]
    for offset, (name, dims, ggml_type) in enumerate(tensors):
        out += _gguf_string(name)
        out += struct.pack("<I", len(dims))
        out += struct.pack("<" + "Q" * len(dims), *dims)
        out += struct.pack("<I", ggml_type)
        out += struct.pack("<Q", offset)
    return bytes(out)


class ReExportFacadeTests(unittest.TestCase):
    """(a) -- the facade is re-export, never an identity wrapper."""

    def test_every_moved_name_is_the_same_object_on_gguf_fit(self):
        for module_name, names in MOVED_NAMES.items():
            module = SIBLING_MODULES[module_name]
            for name in names:
                with self.subTest(module=module_name, name=name):
                    self.assertTrue(hasattr(gguf_fit, name), f"gguf_fit lost {name}")
                    self.assertIs(getattr(gguf_fit, name), getattr(module, name))

    def test_sweep_facade_still_resolves_the_same_objects(self):
        for name in SWEEP_FACADE_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(sweep, name), getattr(gguf_fit, name))

    def test_no_moved_name_is_an_identity_wrapper(self):
        """A re-exported function must be the sibling's own function object, so its
        `__module__` names the sibling -- a same-signature shim in `gguf_fit` would say
        `gguf_fit` instead. That shim is exactly the anti-pattern WP-R3 was told to avoid."""
        for module_name, names in MOVED_NAMES.items():
            for name in names:
                obj = getattr(gguf_fit, name)
                if inspect.isfunction(obj) or inspect.isclass(obj):
                    with self.subTest(module=module_name, name=name):
                        self.assertEqual(obj.__module__, module_name)

    def test_cell_model_still_imports_indexer_geometry_from_gguf_fit(self):
        import cell_model

        self.assertIs(cell_model.IndexerGeometry, gguf_cache.IndexerGeometry)

    def test_scratch_lifecycle_still_imports_the_sysfs_reader_from_gguf_fit(self):
        import scratch_lifecycle

        self.assertIs(scratch_lifecycle.read_vram_used_bytes, gguf_fit.read_vram_used_bytes)


class ModuleGraphTests(unittest.TestCase):
    """(b) and (c) -- one-way edges, and the file-size finding stays fixed."""

    FAMILY = ("gguf_fit.py", "gguf_reader.py", "gguf_placement.py", "gguf_cache.py")

    def test_gguf_reader_imports_no_sibling(self):
        source = (BAKEOFF_DIR / "gguf_reader.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                self.assertNotIn("gguf_fit", line)
                self.assertNotIn("gguf_placement", line)
                self.assertNotIn("gguf_cache", line)

    def test_placement_and_cache_import_only_the_reader(self):
        for name in ("gguf_placement.py", "gguf_cache.py"):
            source = (BAKEOFF_DIR / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                if line.startswith(("import ", "from ")):
                    with self.subTest(module=name, line=line):
                        self.assertNotIn("gguf_fit", line)

    def test_every_family_module_is_under_one_thousand_lines(self):
        for name in self.FAMILY:
            with self.subTest(module=name):
                lines = (BAKEOFF_DIR / name).read_text(encoding="utf-8").count("\n")
                self.assertLess(lines, 1000, f"{name} is {lines} lines -- finding F5 regressed")


class ReaderBehaviorTests(unittest.TestCase):
    """The binary-format behaviours the WP-R3 brief requires to survive byte-identical."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, payload: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(payload)
        return path

    def test_bad_magic_raises_gguf_parse_error(self):
        path = self._write("bad.gguf", b"NOPE" + b"\x00" * 64)
        with self.assertRaises(gguf_reader.GGUFParseError):
            gguf_reader.read_gguf_metadata(path)

    def test_truncated_metadata_raises_gguf_parse_error(self):
        blob = _build_gguf([("general.architecture", _GGUF_TYPE_STRING, "llama")])
        path = self._write("short.gguf", blob[:-4])
        with self.assertRaises(gguf_reader.GGUFParseError):
            gguf_reader.read_gguf_metadata(path)

    def test_scalar_and_array_head_semantics(self):
        """Array-valued metadata comes back as a `list`, scalars as plain ints/strs -- the
        head semantics `gguf_attention_geometry`'s per-layer `head_count_kv` path depends on."""
        blob = _build_gguf([
            ("general.architecture", _GGUF_TYPE_STRING, "llama"),
            ("llama.block_count", _GGUF_TYPE_UINT32, 4),
            ("llama.attention.head_count_kv", _GGUF_TYPE_ARRAY, [8, 8, 1, 1]),
        ])
        metadata = gguf_reader.read_gguf_metadata(self._write("heads.gguf", blob))
        self.assertEqual(metadata["general.architecture"], "llama")
        self.assertEqual(metadata["llama.block_count"], 4)
        self.assertEqual(metadata["llama.attention.head_count_kv"], [8, 8, 1, 1])

    def test_unknown_ggml_type_fails_rather_than_defaulting(self):
        """The failure DIRECTION finding: an unrecognised ggml type id must make the whole
        tensor read fail (`None` out of `read_gguf_tensor_infos`), never silently size the
        tensor from a guessed default."""
        unknown_id = max(gguf_reader.GGML_TYPE_SIZES) + 100
        self.assertNotIn(unknown_id, gguf_reader.GGML_TYPE_SIZES)
        blob = _build_gguf(
            [("general.architecture", _GGUF_TYPE_STRING, "llama")],
            [("blk.0.attn_q.weight", (256, 4), unknown_id)],
        )
        path = self._write("unknown-type.gguf", blob)
        with self.assertRaises(gguf_reader.GGUFParseError):
            gguf_reader._read_gguf_tensor_infos_one_shard(path)
        self.assertIsNone(gguf_reader.read_gguf_tensor_infos(path))

    def test_tensor_nbytes_uses_the_pinned_block_table(self):
        blob = _build_gguf(
            [("general.architecture", _GGUF_TYPE_STRING, "llama")],
            [("blk.0.attn_q.weight", (256, 4), 12)],  # Q4_K: 256 elems / 144 B per block
        )
        infos = gguf_reader.read_gguf_tensor_infos(self._write("q4k.gguf", blob))
        self.assertIsNotNone(infos)
        assert infos is not None
        self.assertEqual(infos[0].nbytes, (256 * 4 // 256) * 144)

    def test_split_shard_discovery_pattern(self):
        path = Path("/models/Foo-UD-Q2_K_XL-00001-of-00003.gguf")
        derived = gguf_reader._sibling_shard_paths(path, 3)
        self.assertEqual(
            derived,
            [Path(f"/models/Foo-UD-Q2_K_XL-{i:05d}-of-00003.gguf") for i in (1, 2, 3)],
        )

    def test_split_shard_discovery_refuses_to_guess(self):
        # A name that does not match the `-NNNNN-of-NNNNN.gguf` convention at all.
        self.assertIsNone(gguf_reader._sibling_shard_paths(Path("/models/Foo.gguf"), 3))
        # A name whose own `-of-` total disagrees with the metadata's `split.count`.
        self.assertIsNone(
            gguf_reader._sibling_shard_paths(Path("/models/Foo-00001-of-00003.gguf"), 4)
        )

    def test_missing_sibling_shard_fails_the_whole_read(self):
        blob = _build_gguf(
            [
                ("general.architecture", _GGUF_TYPE_STRING, "llama"),
                ("split.count", _GGUF_TYPE_UINT32, 2),
            ],
            [("blk.0.attn_q.weight", (256, 4), 12)],
        )
        path = self._write("Split-00001-of-00002.gguf", blob)
        self.assertIsNone(gguf_reader.read_gguf_tensor_infos(path))


class PlacementBehaviorTests(unittest.TestCase):
    """Placement precedence, asserted against `gguf_placement` directly."""

    # `ClassVar` is the accurate annotation for a test-class constant table here, not a
    # workaround -- see `llama/bakeoff/AGENTS.md` "Known-false-positive shapes".
    METADATA: ClassVar[dict[str, Any]] = {"architecture": "qwen35moe", "block_count": 4}

    def _tensors(self):
        return [
            gguf_reader.TensorInfo("token_embd.weight", (1, 1), 12, 1000),
            gguf_reader.TensorInfo("blk.0.ffn_gate_exps.weight", (1, 1), 12, 2000),
            gguf_reader.TensorInfo("blk.3.ffn_gate_exps.weight", (1, 1), 12, 4000),
            gguf_reader.TensorInfo("blk.0.ple_norm.weight", (1, 1), 12, 8),
            gguf_reader.TensorInfo("blk.0.attn_q.weight", (1, 1), 12, 500),
        ]

    def test_fused_gate_up_expert_form_classifies_as_routed_expert(self):
        self.assertEqual(
            gguf_placement.classify_tensor("blk.7.ffn_gate_up_exps.weight"), "routed_expert"
        )
        self.assertEqual(gguf_placement.classify_tensor("blk.7.ffn_down_exps.scale"), "dense")
        self.assertEqual(gguf_placement.classify_tensor("per_layer_token_embd.weight"), "ple")
        self.assertEqual(gguf_placement.classify_tensor("output_norm.weight"), "embd_output")

    def test_ncmoe_moves_only_blocks_below_n_and_token_embd_is_always_cpu(self):
        placement = gguf_placement.derive_placement_bytes(
            self._tensors(), {"-ngl": 99, "-ncmoe": 2}, self.METADATA
        )
        self.assertIsNotNone(placement)
        assert placement is not None
        # blk.0 expert (2000) + token_embd (1000) on CPU; blk.3 expert, ple, attn stay GPU.
        self.assertEqual(placement.cpu_bytes, 3000)
        self.assertEqual(placement.gpu_bytes, 4000 + 8 + 500)
        self.assertEqual(placement.lazy_bytes, 0)

    def test_cmoe_moves_every_routed_expert(self):
        placement = gguf_placement.derive_placement_bytes(
            self._tensors(), {"-ngl": 99, "-cmoe": True}, self.METADATA
        )
        assert placement is not None
        self.assertEqual(placement.cpu_bytes, 1000 + 2000 + 4000)

    def test_partial_ngl_offload_refuses_to_derive(self):
        self.assertIsNone(
            gguf_placement.derive_placement_bytes(
                self._tensors(), {"-ngl": 2, "-cmoe": True}, self.METADATA
            )
        )

    def test_non_cpu_ot_target_refuses_to_derive(self):
        self.assertIsNone(gguf_placement._ot_cpu_patterns("exps=Vulkan0"))
        self.assertIsNone(
            gguf_placement.derive_placement_bytes(
                self._tensors(), {"-ngl": 99, "-ot": "exps=Vulkan0"}, self.METADATA
            )
        )

    def test_no_placement_flag_refuses_to_derive(self):
        self.assertIsNone(
            gguf_placement.derive_placement_bytes(self._tensors(), {"-ngl": 99}, self.METADATA)
        )

    def test_ple_block_tensors_are_never_lazy(self):
        tensors = [
            gguf_reader.TensorInfo("blk.0.ple_norm.weight", (1, 1), 12, 8 * gguf_reader.GIB),
            gguf_reader.TensorInfo(
                "per_layer_token_embd.weight", (1, 1), 12, 8 * gguf_reader.GIB
            ),
        ]
        placement = gguf_placement.derive_placement_bytes(
            tensors, {"-ngl": 99, "-ot": r"ple=CPU,per_layer=CPU"}, self.METADATA
        )
        assert placement is not None
        self.assertEqual(placement.cpu_bytes, 8 * gguf_reader.GIB)  # the ple_ block tensor
        self.assertEqual(placement.lazy_bytes, 8 * gguf_reader.GIB)  # the big PLE table

    def test_lm_none_defeats_mmap_so_nothing_is_lazy(self):
        tensors = [
            gguf_reader.TensorInfo(
                "per_layer_token_embd.weight", (1, 1), 12, 8 * gguf_reader.GIB
            ),
        ]
        placement = gguf_placement.derive_placement_bytes(
            tensors, {"-ngl": 99, "-lm": "none", "-ot": "per_layer=CPU"}, self.METADATA
        )
        assert placement is not None
        self.assertEqual(placement.lazy_bytes, 0)
        self.assertEqual(placement.cpu_bytes, 8 * gguf_reader.GIB)


class CacheBehaviorTests(unittest.TestCase):
    """Attention/indexer geometry and KV byte math, asserted against `gguf_cache` directly."""

    def test_missing_architecture_key_raises_gguf_parse_error(self):
        with self.assertRaises(gguf_reader.GGUFParseError):
            gguf_cache.gguf_attention_geometry({"llama.block_count": 4})

    def test_key_length_falls_back_to_embedding_over_head_count(self):
        geometry = gguf_cache.gguf_attention_geometry({
            "general.architecture": "llama",
            "llama.embedding_length": 4096,
            "llama.block_count": 32,
            "llama.attention.head_count": 32,
        })
        self.assertEqual(geometry["key_length"], 128)
        self.assertEqual(geometry["value_length"], 128)
        self.assertEqual(geometry["head_count_kv"], 32)
        self.assertIsNone(geometry["sliding_window"])

    def test_scalar_path_bytes_per_token(self):
        geometry = {
            "block_count": 2, "head_count_kv": 4, "key_length": 128, "value_length": 128,
        }
        self.assertEqual(
            gguf_cache.kv_bytes_per_token(geometry, "f16", "f16"), 2 * 4 * (128 * 2 + 128 * 2)
        )

    def test_swa_pattern_caps_windowed_layers(self):
        geometry = {
            "block_count": 2,
            "head_count_kv": [8, 1],
            "key_length": 128,
            "value_length": 128,
            "sliding_window": 1024,
            "sliding_window_pattern": [True, False],
        }
        per_token_both = 128 * 2 + 128 * 2
        rate = gguf_cache.kv_bytes_per_token(geometry, "f16", "f16", context=4096)
        expected = (8 * per_token_both * 1024 + 1 * per_token_both * 4096) / 4096
        self.assertAlmostEqual(rate, expected)

    def test_no_swa_signal_reverts_to_the_conservative_full_context_rate(self):
        geometry = {
            "block_count": 2, "head_count_kv": [8, 1], "key_length": 128, "value_length": 128,
        }
        per_token_both = 128 * 2 + 128 * 2
        self.assertEqual(
            gguf_cache.kv_bytes_per_token(geometry, "f16", "f16", context=4096),
            float(9 * per_token_both),
        )

    def test_indexer_term_refuses_a_non_f16_kv_type(self):
        geometry = {
            "block_count": 2, "head_count_kv": 4, "key_length": 128, "value_length": 128,
        }
        with self.assertRaises(ValueError):
            gguf_cache.kv_bytes_per_token(
                geometry, "q8_0", "q8_0",
                kv_indexer_geometry={"layers": 12, "key_length": 128, "value_length": 256},
            )

    def test_indexer_geometry_validation_message_prefix(self):
        with self.assertRaises(ValueError) as ctx:
            gguf_cache.IndexerGeometry.from_mapping({"layers": 1}, prefix="models[x] ")
        self.assertTrue(str(ctx.exception).startswith("models[x] kv_indexer_geometry"))


class InjectedSeamTests(unittest.TestCase):
    """The live-I/O seams that must keep their injection points (no live sysfs is read here)."""

    def test_sysfs_readers_keep_their_injectable_path_parameter(self):
        for reader in (gguf_fit.read_vram_total_bytes, gguf_fit.read_vram_used_bytes):
            with self.subTest(reader=reader.__name__):
                params = inspect.signature(reader).parameters
                self.assertIn("sysfs_path", params)
                self.assertTrue(str(params["sysfs_path"].default).startswith("/sys/class/drm/"))

    def test_desktop_shared_budget_touches_neither_reader(self):
        def boom() -> int:
            raise AssertionError("desktop-shared must not read VRAM")

        budget = gguf_fit.compute_vram_budget_bytes("desktop-shared", boom, boom)
        self.assertEqual(budget, gguf_fit.REALISTIC_FREE_VRAM_GIB * gguf_fit.GIB)

    def test_headless_refuses_on_a_live_desktop_baseline(self):
        with self.assertRaises(gguf_fit.VRAMProfileRefused):
            gguf_fit.compute_vram_budget_bytes(
                "headless",
                lambda: int(15.92 * gguf_fit.GIB),
                lambda: int(3.1 * gguf_fit.GIB),
            )


if __name__ == "__main__":
    unittest.main()
