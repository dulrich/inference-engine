#!/usr/bin/env python3
"""Unit tests for sync-openwebui-capabilities.py's pure functions (`compute_desired` /
`plan_change`). No network, no live Open WebUI or router calls anywhere in this suite --
fixtures only.

The module under test has a hyphenated filename (matching this repo's script-invocation
convention: `python3 scripts/sync-openwebui-capabilities.py ...`), so it is not importable via
a normal `import` statement; it is loaded here via `importlib.util` from its file path.

Run: python3 -m unittest discover -s scripts -p 'test_sync_openwebui_capabilities.py' -v
(equivalently: cd scripts && python3 -m unittest test_sync_openwebui_capabilities -v)
"""

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / "sync-openwebui-capabilities.py"
_spec = importlib.util.spec_from_file_location("sync_openwebui_capabilities", MODULE_PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync
_spec.loader.exec_module(sync)  # type: ignore[union-attr]


def _router_entry(router_id="model-a", name="Model A", sampling=None, context_length=131072):
    ls = {
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tool_calling": True,
        "reasoning": "none",
        "context_length": context_length,
        "serving": {"quant": "Q4_K_XL", "kv_cache_type": "f16", "flash_attention": False, "mtp": False},
    }
    if sampling is not None:
        ls["sampling"] = sampling
    return {"id": router_id, "name": name, "meta": {"llamaswap": ls}}


class ComputeDesiredSamplingTest(unittest.TestCase):
    def test_allowlisted_keys_extracted(self):
        entry = _router_entry(
            sampling={
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 20,
                "source": "https://huggingface.co/example (researched 2026-08-23)",
            }
        )
        desired = sync.compute_desired(entry)
        self.assertEqual(
            desired["sampling_params"], {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
        )

    def test_source_never_in_sampling_params(self):
        entry = _router_entry(sampling={"source": "https://huggingface.co/example"})
        desired = sync.compute_desired(entry)
        self.assertNotIn("source", desired["sampling_params"])
        self.assertEqual(desired["sampling_params"], {})

    def test_no_sampling_block_yields_empty(self):
        entry = _router_entry(sampling=None)
        desired = sync.compute_desired(entry)
        self.assertEqual(desired["sampling_params"], {})

    def test_all_allowlisted_keys_pass_through(self):
        full = {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "presence_penalty": 0.1,
            "repetition_penalty": 1.05,
            "source": "https://huggingface.co/example",
        }
        entry = _router_entry(sampling=full)
        desired = sync.compute_desired(entry)
        expected = {k: v for k, v in full.items() if k != "source"}
        self.assertEqual(desired["sampling_params"], expected)
        self.assertNotIn("source", desired["sampling_params"])


class PlanChangeSamplingTest(unittest.TestCase):
    def test_create_row_seeds_params_with_sampling(self):
        entry = _router_entry(sampling={"temperature": 1.0, "top_p": 0.95})
        desired = sync.compute_desired(entry)
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, None)
        self.assertEqual(action, "create")
        self.assertEqual(body["params"], {"temperature": 1.0, "top_p": 0.95})
        self.assertNotIn("source", body["params"])
        diff_fields = {f for f, _, _ in diffs}
        self.assertIn("params.temperature", diff_fields)
        self.assertIn("params.top_p", diff_fields)

    def test_create_row_no_sampling_yields_empty_params(self):
        entry = _router_entry(sampling=None)
        desired = sync.compute_desired(entry)
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, None)
        self.assertEqual(action, "create")
        self.assertEqual(body["params"], {})

    def test_existing_row_merges_sampling_and_preserves_human_key(self):
        entry = _router_entry(sampling={"temperature": 1.0, "top_p": 0.95})
        desired = sync.compute_desired(entry)
        existing = {
            "id": "model-a",
            "name": "Model A",
            "meta": {
                "description": desired["description"],
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": {"max_tokens": 4096},
        }
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "update")
        self.assertEqual(
            body["params"], {"max_tokens": 4096, "temperature": 1.0, "top_p": 0.95}
        )
        diff_fields = {f for f, _, _ in diffs}
        self.assertIn("params.temperature", diff_fields)
        self.assertIn("params.top_p", diff_fields)
        self.assertNotIn("params.source", diff_fields)

    def test_source_never_written_to_existing_params(self):
        entry = _router_entry(
            sampling={"temperature": 1.0, "source": "https://huggingface.co/example"}
        )
        desired = sync.compute_desired(entry)
        existing = {
            "id": "model-a",
            "name": "Model A",
            "meta": {
                "description": desired["description"],
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": {},
        }
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "update")
        self.assertNotIn("source", body["params"])
        self.assertEqual(body["params"], {"temperature": 1.0})

    def test_idempotent_second_pass_is_noop(self):
        entry = _router_entry(sampling={"temperature": 1.0, "top_p": 0.95, "top_k": 20})
        desired = sync.compute_desired(entry)
        existing = {
            "id": "model-a",
            "name": "Model A",
            "meta": {
                "description": desired["description"],
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "max_tokens": 4096},
        }
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "noop")
        self.assertEqual(diffs, [])
        self.assertEqual(body, {})

    def test_model_without_sampling_leaves_existing_params_untouched(self):
        entry = _router_entry(sampling=None, context_length=999999)
        desired = sync.compute_desired(entry)
        existing = {
            "id": "model-a",
            "name": "Model A",
            "meta": {
                "description": "stale description",
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": 131072},
            },
            "params": {"max_tokens": 4096, "temperature": 0.3},
        }
        # Force a non-sampling diff (context_length changed) so the update path runs, and
        # confirm params round-trip byte-identical since the router model carries no sampling.
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "update")
        self.assertEqual(body["params"], {"max_tokens": 4096, "temperature": 0.3})
        diff_fields = {f for f, _, _ in diffs}
        self.assertFalse(any(f.startswith("params.") for f in diff_fields))

    def test_noop_when_model_has_no_sampling_and_nothing_else_changed(self):
        entry = _router_entry(sampling=None)
        desired = sync.compute_desired(entry)
        existing = {
            "id": "model-a",
            "name": "Model A",
            "meta": {
                "description": desired["description"],
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": {"max_tokens": 4096},
        }
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "noop")
        self.assertEqual(diffs, [])
        self.assertEqual(body, {})


if __name__ == "__main__":
    unittest.main()
