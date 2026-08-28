"""Fit-model fixes (plan longcontext-pow2-overnight-campaign.md WP-1, audit B2/B4): SWA-aware
and placement-aware `classify_fit`, and the `desktop-shared`/`headless` VRAM profiles. No live
Docker/GPU execution -- every VRAM reader is injected; the real-header tests read GGUF metadata
straight off disk (in scope as data, per this WP's brief), never load a model."""

import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import scratch_lifecycle  # noqa: E402
import sweep  # noqa: E402

GEMMA_QAT_HOST_PATH = Path(
    "/srv/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"
)
QWEN36_A3B_HOST_PATH = Path(
    "/srv/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
)

GIB = sweep.GIB

# qwen36-a3b Config B GPU-resident weight figure (llama/config.yaml offload-placement comments
# ~721-745, host-verified 2026-08-21): 2.494 GiB non-expert weights (always GPU-resident
# regardless of `-ncmoe`) + 8 of 40 layers' routed-expert weights GPU-resident, out of an 18.320
# GiB routed-expert total (~491.8 MiB/layer average) -- computed data-driven from those two cited
# GiB figures, never a hand-typed byte count.
QWEN36_A3B_NON_EXPERT_GIB = 2.494
QWEN36_A3B_ROUTED_EXPERT_TOTAL_GIB = 18.320
QWEN36_A3B_BLOCK_COUNT = 40
QWEN36_A3B_GPU_RESIDENT_LAYERS = 8
QWEN36_A3B_GPU_WEIGHTS_BYTES = int(
    (
        QWEN36_A3B_NON_EXPERT_GIB
        + QWEN36_A3B_ROUTED_EXPERT_TOTAL_GIB * QWEN36_A3B_GPU_RESIDENT_LAYERS / QWEN36_A3B_BLOCK_COUNT
    )
    * GIB
)
# Only 10 of 40 qwen35moe blocks carry real attention KV (full_attention_interval=4) --
# llama/config.yaml:730-733.
QWEN36_A3B_KV_ATTENTION_LAYERS = 10


def _gemma_like_synthetic_geometry(weights_bytes=6_716_356_800, with_swa_pattern=True):
    """A synthetic geometry shaped like gemma4's real header (48 layers, 40 SWA @
    head_count_kv=8 capped at window 1024, 8 global @ head_count_kv=1) -- used for the pure
    synthetic-geometry fit-math tests so they don't depend on any file being present on disk."""
    head_count_kv = [1] * 8 + [8] * 40
    pattern = [False] * 8 + [True] * 40
    synthetic = {
        "weights_bytes": weights_bytes,
        "architecture": "gemma4-synthetic",
        "block_count": 48,
        "head_count_kv": head_count_kv,
        "key_length": 512,
        "value_length": 512,
        "sliding_window": 1024,
    }
    if with_swa_pattern:
        synthetic["sliding_window_pattern"] = pattern
    cell = {
        "alias": "synthetic-gemma",
        "model_container_path": "/models/synthetic/gemma.gguf",
        "synthetic_geometry": synthetic,
        "server_flags": {"-c": 32768, "-ctk": "f16", "-ctv": "f16"},
        "battery": ["vram"],
    }
    if with_swa_pattern:
        cell["swa_layer_spec"] = pattern
    return sweep.normalize_cell(cell)


class TestSWAAwareKVBytes(unittest.TestCase):
    def test_window_capped_layers_stop_growing_past_the_window(self):
        geometry = {
            "block_count": 48,
            "head_count_kv": [1] * 8 + [8] * 40,
            "key_length": 512,
            "value_length": 512,
            "sliding_window": 1024,
            "sliding_window_pattern": [False] * 8 + [True] * 40,
        }
        rate_at_window = sweep.kv_bytes_per_token(geometry, "f16", "f16", context=1024)
        rate_far_past_window = sweep.kv_bytes_per_token(geometry, "f16", "f16", context=262144)
        # Global layers' contribution keeps scaling with context; SWA layers' contribution is
        # capped at the window -- so total bytes at 262144 must be far less than what naive
        # uniform scaling (262144/1024 = 256x) would predict, but strictly more than the bytes at
        # the window size itself (the 8 global layers alone still grow).
        total_at_window = rate_at_window * 1024
        total_far = rate_far_past_window * 262144
        naive_uniform_total = rate_at_window * 1024 * (262144 / 1024)
        self.assertGreater(total_far, total_at_window)
        self.assertLess(total_far, naive_uniform_total)

    def test_explicit_swa_layer_spec_override_matches_header_pattern(self):
        geometry = {
            "block_count": 48,
            "head_count_kv": [1] * 8 + [8] * 40,
            "key_length": 512,
            "value_length": 512,
            "sliding_window": 1024,
            "sliding_window_pattern": None,  # header doesn't carry it in this synthetic case
        }
        override_spec = [False] * 8 + [True] * 40
        rate_no_signal = sweep.kv_bytes_per_token(geometry, "f16", "f16", context=131072)
        rate_with_override = sweep.kv_bytes_per_token(
            geometry, "f16", "f16", context=131072, swa_layer_spec=override_spec
        )
        # No signal at all -> conservative fallback (every entry scales with full context) is
        # strictly larger than the override-driven SWA-aware total.
        self.assertGreater(rate_no_signal, rate_with_override)

    def test_no_swa_signal_falls_back_to_conservative_uniform_scaling(self):
        geometry = {
            "block_count": 24,
            "head_count_kv": [0, 0, 4, 0, 4, 0] * 4,
            "key_length": 128,
            "value_length": 128,
        }
        # No sliding_window / sliding_window_pattern / swa_layer_spec at all -- must match the
        # pre-existing per-layer-array behavior exactly (sum over nonzero entries), regardless of
        # whether `context` is passed.
        rate_without_context = sweep.kv_bytes_per_token(geometry, "f16", "f16")
        rate_with_context = sweep.kv_bytes_per_token(geometry, "f16", "f16", context=65536)
        self.assertEqual(rate_without_context, rate_with_context)

    def test_mismatched_spec_length_falls_back_conservatively(self):
        geometry = {
            "block_count": 4,
            "head_count_kv": [1, 8, 8, 8],
            "key_length": 512,
            "value_length": 512,
            "sliding_window": 1024,
            "sliding_window_pattern": [False, True, True],  # wrong length -- must be ignored
        }
        rate = sweep.kv_bytes_per_token(geometry, "f16", "f16", context=262144)
        fallback = sweep.kv_bytes_per_token(geometry, "f16", "f16")
        self.assertEqual(rate, fallback)


class TestSyntheticGeometrySWAFit(unittest.TestCase):
    def test_swa_aware_synthetic_gemma_fits_at_32k_and_128k(self):
        for c in (32768, 131072):
            cell = _gemma_like_synthetic_geometry()
            cell["server_flags"]["-c"] = c
            fit = sweep.classify_fit(cell, vram_profile="desktop-shared")
            self.assertEqual(fit["classification"], "fits", f"c={c}: {fit}")

    def test_without_swa_spec_the_same_cell_is_excluded(self):
        # Same weights/geometry, but no swa_layer_spec and no sliding_window in the geometry at
        # all -- the pre-existing conservative (uniform-scaling) fallback -- must be markedly
        # worse (never better) than the SWA-aware classification above.
        cell = _gemma_like_synthetic_geometry(with_swa_pattern=False)
        cell["server_flags"]["-c"] = 131072
        fit_no_swa = sweep.classify_fit(cell, vram_profile="desktop-shared")
        cell_with_swa = _gemma_like_synthetic_geometry(with_swa_pattern=True)
        cell_with_swa["server_flags"]["-c"] = 131072
        fit_with_swa = sweep.classify_fit(cell_with_swa, vram_profile="desktop-shared")
        self.assertLess(fit_no_swa["predicted_margin_gib"], fit_with_swa["predicted_margin_gib"])


class TestPlacementAwareness(unittest.TestCase):
    def test_gpu_weights_bytes_override_replaces_whole_file_size(self):
        synthetic = {
            "weights_bytes": 40 * GIB,  # a huge whole-file figure, e.g. an -ncmoe'd MoE GGUF
            "block_count": 10,
            "head_count_kv": 2,
            "key_length": 256,
            "value_length": 256,
        }
        cell_no_override = sweep.normalize_cell({
            "alias": "moe-no-override",
            "model_container_path": "/models/synthetic/moe.gguf",
            "synthetic_geometry": synthetic,
            "server_flags": {"-c": 32768, "-ctk": "f16", "-ctv": "f16"},
            "battery": ["vram"],
        })
        cell_with_override = sweep.normalize_cell({
            "alias": "moe-with-override",
            "model_container_path": "/models/synthetic/moe.gguf",
            "synthetic_geometry": synthetic,
            "gpu_weights_bytes": 6 * GIB,
            "server_flags": {"-c": 32768, "-ctk": "f16", "-ctv": "f16"},
            "battery": ["vram"],
        })
        fit_no_override = sweep.classify_fit(cell_no_override)
        fit_with_override = sweep.classify_fit(cell_with_override)
        self.assertEqual(fit_no_override["weights_bytes"], 40 * GIB)
        self.assertEqual(fit_with_override["weights_bytes"], 6 * GIB)
        self.assertEqual(fit_no_override["classification"], "excluded")
        self.assertEqual(fit_with_override["classification"], "fits")


class TestVRAMProfiles(unittest.TestCase):
    def test_desktop_shared_never_touches_injected_readers(self):
        calls = {"total": 0, "used": 0}

        def total_fn():
            calls["total"] += 1
            return 0

        def used_fn():
            calls["used"] += 1
            return 0

        budget = sweep.compute_vram_budget_bytes("desktop-shared", total_fn, used_fn)
        self.assertEqual(budget, sweep.REALISTIC_FREE_VRAM_GIB * GIB)
        self.assertEqual(calls, {"total": 0, "used": 0})

    def test_headless_computes_live_admissible_budget(self):
        total_fn = lambda: int(15.92 * GIB)
        used_fn = lambda: int(0.3 * GIB)  # well under the refusal ceiling
        budget = sweep.compute_vram_budget_bytes("headless", total_fn, used_fn)
        expected = int(15.92 * GIB) - int(0.3 * GIB) - sweep.HEADLESS_PINNED_MARGIN_GIB * GIB
        self.assertAlmostEqual(budget, expected)

    def test_headless_refuses_on_a_live_desktop_baseline(self):
        # llama/config.yaml's gemma fit-math comment: mem_info_vram_used = 3,327,463,424 B
        # (~3.1 GiB) on this exact desktop-shared host -- must trip the refusal.
        total_fn = lambda: int(15.92 * GIB)
        used_fn = lambda: 3_327_463_424
        with self.assertRaises(sweep.VRAMProfileRefused):
            sweep.compute_vram_budget_bytes("headless", total_fn, used_fn)

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            sweep.compute_vram_budget_bytes("gaming-rig", lambda: 0, lambda: 0)

    def test_classify_fit_threads_vram_profile_and_readers_through(self):
        cell = _gemma_like_synthetic_geometry()
        cell["server_flags"]["-c"] = 32768
        total_fn = lambda: int(15.92 * GIB)
        used_fn = lambda: int(0.3 * GIB)
        fit = sweep.classify_fit(cell, vram_profile="headless", vram_total_bytes_fn=total_fn, vram_used_bytes_fn=used_fn)
        self.assertEqual(fit["vram_profile"], "headless")
        self.assertEqual(
            fit["vram_budget_bytes"],
            int(15.92 * GIB) - int(0.3 * GIB) - sweep.HEADLESS_PINNED_MARGIN_GIB * GIB,
        )


@unittest.skipUnless(GEMMA_QAT_HOST_PATH.exists(), f"model file absent: {GEMMA_QAT_HOST_PATH}")
class TestRealGemmaQATHeaderRegression(unittest.TestCase):
    """Real-header regression (audit B2): the actual gemma-4-12B-it-qat-UD-Q4_K_XL.gguf header on
    disk, no synthetic geometry -- gemma's own `attention.sliding_window_pattern` array drives the
    SWA split, with no cell-supplied `swa_layer_spec` override needed."""

    def _cell(self, c):
        return sweep.normalize_cell({
            "alias": f"gemma-qat-real-c{c}",
            "model_container_path": (
                "/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf"
            ),
            "model_host_path": str(GEMMA_QAT_HOST_PATH),
            "server_flags": {"-c": c, "-ctk": "f16", "-ctv": "f16"},
            "battery": ["vram", "timing", "needle"],
        })

    def test_32768_65536_131072_fit_on_desktop_shared(self):
        for c in (32768, 65536, 131072):
            fit = sweep.classify_fit(self._cell(c), vram_profile="desktop-shared")
            self.assertEqual(fit["classification"], "fits", f"c={c}: {fit}")

    def test_old_behaviors_false_exclusion_is_gone(self):
        # Pre-fix, EVERY array entry scaled with the full context -- at 32768 that overestimates
        # KV bytes/token by roughly the SWA-layer-count factor and would misclassify this cell.
        # Confirm the SWA-aware rate is markedly smaller than the old uniform-scaling rate for
        # the exact real header.
        import sweep as sweep_module

        metadata = sweep_module.read_gguf_metadata(GEMMA_QAT_HOST_PATH)
        geometry = sweep_module.gguf_attention_geometry(metadata)
        old_uniform_rate = sweep_module.kv_bytes_per_token(geometry, "f16", "f16")
        new_swa_rate = sweep_module.kv_bytes_per_token(geometry, "f16", "f16", context=32768)
        self.assertGreater(old_uniform_rate, new_swa_rate * 5)


@unittest.skipUnless(QWEN36_A3B_HOST_PATH.exists(), f"model file absent: {QWEN36_A3B_HOST_PATH}")
class TestRealQwen36A3BHeaderRegression(unittest.TestCase):
    """Real-header regression (audit B2): the actual Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf header,
    with the `-ncmoe 32` placement override (`gpu_weights_bytes`) and the hybrid-architecture
    `kv_attention_layers` override (only 10 of 40 blocks carry real attention KV) -- both required
    together for this cell to classify correctly; the whole-file weight size alone (~20.8 GiB)
    would misclassify it `excluded` regardless of KV math."""

    def _cell(self, c):
        return sweep.normalize_cell({
            "alias": f"qwen36-a3b-real-c{c}",
            "model_container_path": (
                "/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
            ),
            "model_host_path": str(QWEN36_A3B_HOST_PATH),
            "gpu_weights_bytes": QWEN36_A3B_GPU_WEIGHTS_BYTES,
            "kv_attention_layers": QWEN36_A3B_KV_ATTENTION_LAYERS,
            "server_flags": {"-c": c, "-ctk": "f16", "-ctv": "f16", "-ncmoe": 32},
            "battery": ["vram", "timing", "needle"],
        })

    def test_fits_at_measured_safe_rung_on_desktop_shared(self):
        fit = sweep.classify_fit(self._cell(32768), vram_profile="desktop-shared")
        self.assertEqual(fit["classification"], "fits", fit)

    def test_whole_file_weight_size_alone_would_have_excluded_it(self):
        whole_file_bytes = QWEN36_A3B_HOST_PATH.stat().st_size
        self.assertGreater(whole_file_bytes, QWEN36_A3B_GPU_WEIGHTS_BYTES)
        cell_no_override = sweep.normalize_cell({
            "alias": "qwen36-a3b-no-override",
            "model_container_path": (
                "/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"
            ),
            "model_host_path": str(QWEN36_A3B_HOST_PATH),
            "kv_attention_layers": QWEN36_A3B_KV_ATTENTION_LAYERS,
            "server_flags": {"-c": 32768, "-ctk": "f16", "-ctv": "f16"},
            "battery": ["vram"],
        })
        fit_no_override = sweep.classify_fit(cell_no_override, vram_profile="desktop-shared")
        self.assertEqual(fit_no_override["classification"], "excluded")


class TestBuildCommandBooleanFlags(unittest.TestCase):
    """WP-5 attended-validation finding #3: JSON `true` in server_flags must emit the BARE flag,
    never a literal `True` argument -- llama-server rejects `--jinja True` at parse time
    (`error: invalid argument: True`), the server dies instantly, and the health poll times out
    against a corpse while the queue exits 0 'completed'."""

    @staticmethod
    def _cell(server_flags):
        flags = {"-c": 32768}
        flags.update(server_flags)
        return sweep.normalize_cell({
            "alias": "boolflags",
            "model_container_path": "/models/fake.gguf",
            "server_flags": flags,
            "battery": ["needle"],
        })

    def test_true_emits_bare_flag_without_argument(self):
        argv = sweep.build_command(self._cell({"--jinja": True}), port=18080)
        self.assertIn("--jinja", argv)
        self.assertNotIn("True", argv)
        # The bare flag must not swallow the next token: whatever follows --jinja is another flag.
        nxt = argv[argv.index("--jinja") + 1] if argv.index("--jinja") + 1 < len(argv) else None
        if nxt is not None:
            self.assertTrue(nxt.startswith("-"), f"--jinja swallowed {nxt!r}")

    def test_false_and_none_omit_the_flag_entirely(self):
        argv = sweep.build_command(self._cell({"--jinja": False, "--no-warmup": None}), port=18080)
        self.assertNotIn("--jinja", argv)
        self.assertNotIn("--no-warmup", argv)
        self.assertNotIn("False", argv)
        self.assertNotIn("None", argv)

    def test_non_boolean_values_still_serialize_as_key_value_pairs(self):
        argv = sweep.build_command(self._cell({"--jinja": True}), port=18080)
        i = argv.index("-c")
        self.assertEqual(argv[i + 1], "32768")
        self.assertEqual(argv[argv.index("--port") + 1], "18080")


class TestVerifyStartupState(unittest.TestCase):
    """`verify_startup_state` -- "a healthy endpoint has told you nothing" (llama/AGENTS.md).

    WP-R3 moved this function to `scratch_lifecycle.py`; before that move it had no direct
    assertions anywhere in the suite (it was only ever a `mock.patch` target), so the move is
    landed together with the coverage that pins its verdicts. The contract these lock: a field
    the log never mentions is a MISMATCH, never a silent pass.
    """

    CELL = {
        "alias": "demo",
        "model_container_path": "/models/unsloth/demo/demo-Q4_K_M.gguf",
        "server_flags": {"-c": 32768, "-ctk": "f16", "-ctv": "f16", "-fa": "on", "-ngl": 99},
        "battery": ["vram"],
    }

    GOOD_LOG = (
        "load_model: loading model '/models/unsloth/demo/demo-Q4_K_M.gguf'\n"
        "llama_context: n_ctx                 = 32768\n"
        "llama_context: flash_attn            = enabled\n"
        "load_tensors: offloaded 25/25 layers to GPU\n"
        "llama_kv_cache:  size =  1536.00 MiB ( 32768 cells,  24 layers), "
        "K (f16):  768.00 MiB, V (f16):  768.00 MiB\n"
    )

    def test_a_matching_log_verifies_every_field(self):
        ok, details = sweep.verify_startup_state(self.GOOD_LOG, self.CELL)
        self.assertTrue(ok, details)
        self.assertEqual(
            details["checks"],
            {
                "model_path": True, "n_ctx": True, "type_k": True, "type_v": True,
                "flash_attn": True, "offload_placement": True,
            },
        )

    def test_an_empty_log_fails_every_check_rather_than_passing_silently(self):
        ok, details = sweep.verify_startup_state("", self.CELL)
        self.assertFalse(ok)
        self.assertEqual(set(details["checks"].values()), {False})
        self.assertEqual(set(details["matches"].values()), {None})

    def test_each_mismatched_field_is_reported_individually(self):
        cases = {
            "n_ctx": ("n_ctx                 = 32768", "n_ctx                 = 8192"),
            "type_k": ("K (f16)", "K (q8_0)"),
            "type_v": ("V (f16)", "V (q4_0)"),
            "flash_attn": ("flash_attn            = enabled",
                           "flash_attn            = disabled"),
            "model_path": ("demo-Q4_K_M.gguf'", "other-Q4_K_M.gguf'"),
        }
        for field, (needle, replacement) in cases.items():
            with self.subTest(field=field):
                log = self.GOOD_LOG.replace(needle, replacement)
                self.assertNotEqual(log, self.GOOD_LOG, "fixture edit did not apply")
                ok, details = sweep.verify_startup_state(log, self.CELL)
                self.assertFalse(ok)
                self.assertFalse(details["checks"][field])

    def test_partial_offload_below_the_request_fails_placement(self):
        log = self.GOOD_LOG.replace("offloaded 25/25 layers", "offloaded 12/25 layers")
        ok, details = sweep.verify_startup_state(log, self.CELL)
        self.assertFalse(ok)
        self.assertFalse(details["checks"]["offload_placement"])

    def test_flash_attention_off_is_verified_as_off(self):
        cell = dict(self.CELL)
        cell["server_flags"] = dict(self.CELL["server_flags"], **{"-fa": "off"})
        log = self.GOOD_LOG.replace("flash_attn            = enabled",
                                    "flash_attn            = disabled")
        ok, details = sweep.verify_startup_state(log, cell)
        self.assertTrue(ok, details)
        # ... and the same log against a cell that asked for -fa on is a mismatch.
        self.assertFalse(sweep.verify_startup_state(log, self.CELL)[0])


class TestScratchServerHandleCellClock(unittest.TestCase):
    """WP-1 of `plans/bakeoff-timing-records-and-estimates.md`: the handle's additive cell clock.
    Constructing a handle directly (what every test double in this repo does) must keep working
    unchanged, and must self-populate an honest start rather than leaving a hole."""

    def _handle(self, **kwargs):
        return sweep.ScratchServerHandle(
            cell_id="x", base_url="http://fake", healthy=True, verified=True,
            details={}, log_text="", vram_before_bytes=0, port=1, container="fake", **kwargs
        )

    def test_pre_wp_construction_still_works_and_self_populates_a_start(self):
        handle = self._handle()
        self.assertIsInstance(handle.started_at, str)
        self.assertTrue(handle.started_at.endswith("+00:00"))
        self.assertIsInstance(handle.started_monotonic, float)
        # Never invented: the startup measurement is only ever supplied by the real context
        # manager, so a directly-constructed handle reports null, not a fabricated number.
        self.assertIsNone(handle.scratch_startup_seconds)

    def test_explicit_values_are_carried_verbatim(self):
        handle = self._handle(
            started_at="2026-08-25T12:00:00+00:00", started_monotonic=1000.0,
            scratch_startup_seconds=42.25,
        )
        self.assertEqual(handle.started_at, "2026-08-25T12:00:00+00:00")
        self.assertEqual(handle.started_monotonic, 1000.0)
        self.assertEqual(handle.scratch_startup_seconds, 42.25)


class TestScratchStartupTimer(unittest.TestCase):
    """The NEW monotonic timer around `spawn_scratch_server` -> startup verification
    (`scratch_lifecycle._ScratchServerContext.__enter__`, still reachable as
    `sweep._ScratchServerContext` -- WP-R3 moved the implementation, not the public name).
    Every Docker/sysfs collaborator is patched out; no container, no host sysfs, no network is
    touched by these tests.

    The patch targets name `scratch_lifecycle` rather than `sweep` because that is the module
    whose globals `__enter__` actually reads; `sweep` re-exports the same objects, but patching
    a re-export would rebind only the facade. The assertions below are unchanged."""

    CELL = {
        "cell_id": "timed-cell", "alias": "a",
        "model_container_path": "/models/a.gguf",
        "server_flags": {"-c": 32768, "-ctk": "f16", "-ctv": "f16", "-fa": "on", "-ngl": 99},
    }

    def _run(self, healthy, verified):
        from unittest import mock

        clock = iter([100.0, 110.0, 137.5, 200.0, 300.0, 400.0])
        with mock.patch.object(scratch_lifecycle, "find_host_pid_by_port", return_value=None), \
                mock.patch.object(scratch_lifecycle, "teardown_scratch_server", return_value={}), \
                mock.patch.object(scratch_lifecycle, "read_vram_used_bytes", return_value=0), \
                mock.patch.object(scratch_lifecycle, "build_command", return_value=["llama-server"]), \
                mock.patch.object(scratch_lifecycle, "spawn_scratch_server", return_value=None), \
                mock.patch.object(scratch_lifecycle, "discover_bridge_ip", return_value="10.0.0.2"), \
                mock.patch.object(scratch_lifecycle, "wait_for_health", return_value=healthy), \
                mock.patch.object(scratch_lifecycle, "verify_startup_state", return_value=(verified, {})), \
                mock.patch.object(scratch_lifecycle.subprocess, "run") as run_mock, \
                mock.patch.object(scratch_lifecycle.time, "monotonic", side_effect=lambda: next(clock)):
            run_mock.return_value = mock.Mock(stdout=b"log")
            with sweep.scratch_server(self.CELL, port=1) as handle:
                return handle

    def test_records_spawn_to_verification_elapsed_on_a_good_startup(self):
        handle = self._run(healthy=True, verified=True)
        # clock: 100.0 = cell start (top of __enter__), 110.0 = spawn, 137.5 = verification done.
        self.assertEqual(handle.started_monotonic, 100.0)
        self.assertAlmostEqual(handle.scratch_startup_seconds, 27.5)
        self.assertIsInstance(handle.started_at, str)

    def test_health_timeout_records_null_never_the_timeout_duration(self):
        handle = self._run(healthy=False, verified=True)
        self.assertIsNone(handle.scratch_startup_seconds)
        # The cell clock still started -- only the startup MEASUREMENT is unavailable.
        self.assertEqual(handle.started_monotonic, 100.0)

    def test_failed_startup_verification_records_null(self):
        handle = self._run(healthy=True, verified=False)
        self.assertIsNone(handle.scratch_startup_seconds)


if __name__ == "__main__":
    unittest.main()
