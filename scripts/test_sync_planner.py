#!/usr/bin/env python3
"""Unit tests for `openwebui_sync/planner.py`: the create/update/toggle/retire decision matrix
and the managed-field merge semantics. No network, no live Open WebUI or router calls anywhere
in this suite -- fixtures only.

Split from the monolithic `test_sync_openwebui_capabilities.py` (WP-R6a); see
`openwebui_sync/planner.py`'s own docstring for what this layer owns: nothing in that module
performs I/O, reads a file, or knows an endpoint shape -- every function is a pure function of
its arguments, which is what makes it testable against fixtures alone (no HTTP double needed
anywhere in this file).

The module under test has a hyphenated filename (matching this repo's script-invocation
convention: `python3 scripts/sync-openwebui-capabilities.py ...`), so it is not importable via
a normal `import` statement; it is loaded here via `importlib.util` from its file path, exactly
as `sync_test_helpers.py` (this file's shared-fixture module) does.

Run all four split modules: python3 -m unittest discover -s scripts -p 'test_sync_*.py' -v
This module alone: cd scripts && python3 -m unittest test_sync_planner -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path

from sync_test_helpers import _policy, _router_entry, _write_json_once

MODULE_PATH = Path(__file__).resolve().parent / "sync-openwebui-capabilities.py"
_spec = importlib.util.spec_from_file_location("sync_openwebui_capabilities", MODULE_PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync
_spec.loader.exec_module(sync)  # type: ignore[union-attr]


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


class PlanIsActiveTest(unittest.TestCase):
    def test_toggle_only_when_the_observed_value_differs(self):
        self.assertEqual(sync.plan_is_active(True, {"is_active": False}), ("toggle", False))
        self.assertEqual(sync.plan_is_active(False, {"is_active": True}), ("toggle", True))

    def test_no_call_at_all_when_it_already_matches(self):
        self.assertEqual(sync.plan_is_active(True, {"is_active": True}), ("noop", True))
        self.assertEqual(sync.plan_is_active(False, {"is_active": False}), ("noop", False))

    def test_an_absent_row_is_create_with_no_observed_value_to_fabricate(self):
        self.assertEqual(sync.plan_is_active(False, None), ("create", None))
        self.assertEqual(sync.plan_is_active(True, None), ("create", None))


class DerivedRowTest(unittest.TestCase):
    def test_derived_create_body_carries_id_name_base_model_id_params_and_meta(self):
        entry = _router_entry(
            router_id="Qwen3.8-27B-UD-IQ3_S",
            name="Qwen3.8 27B (UD-IQ3_S)",
            sampling={"temperature": 1.0, "top_p": 0.95},
        )
        desired = sync.compute_desired(entry)
        policy = sync.load_policy(_write_json_once(self, _policy()))
        action, diffs, body = sync.plan_change(
            sync.derived_row_id("Qwen3.8-27B-UD-IQ3_S", "low"),
            sync.derived_row_name("Qwen3.8 27B (UD-IQ3_S)", "low"),
            desired,
            None,
            policy=policy,
            arm=True,
            base_model_id="Qwen3.8-27B-UD-IQ3_S",
            reasoning_effort="low",
        )
        self.assertEqual(action, "create")
        self.assertEqual(body["id"], "Qwen3.8-27B-UD-IQ3_S--effort-low")
        self.assertEqual(body["name"], "Qwen3.8 27B (UD-IQ3_S) · low")
        self.assertEqual(body["base_model_id"], "Qwen3.8-27B-UD-IQ3_S")
        self.assertEqual(
            body["params"], {"temperature": 1.0, "top_p": 0.95, "reasoning_effort": "low"}
        )
        self.assertEqual(
            sorted(body["meta"]),
            ["capabilities", "defaultFeatureIds", "description", "llamaswap", "toolIds"],
        )
        self.assertEqual(body["meta"]["capabilities"], {"vision": False})
        self.assertEqual(body["meta"]["description"], desired["description"])
        self.assertEqual(body["meta"]["llamaswap"], {"context_length": desired["context_length"]})
        self.assertIn(("params.reasoning_effort", None, "low"), diffs)

    def test_reasoning_effort_is_never_written_to_a_base_row(self):
        entry = _router_entry(sampling={"temperature": 1.0})
        desired = sync.compute_desired(entry)
        # Create: no `reasoning_effort` key at all.
        _, _, created = sync.plan_change("model-a", "Model A", desired, None)
        self.assertNotIn("reasoning_effort", created["params"])
        # Update: a human-set `reasoning_effort` on a BASE row round-trips untouched and is
        # never a diff, because the sync does not own that key here.
        existing = {
            "id": "model-a",
            "name": "Model A",
            "meta": {
                "description": "stale",
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": {"temperature": 1.0, "reasoning_effort": "xhigh"},
        }
        action, diffs, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "update")
        self.assertEqual(body["params"]["reasoning_effort"], "xhigh")
        self.assertNotIn("params.reasoning_effort", {f for f, _, _ in diffs})


class ArmingTest(unittest.TestCase):
    def _existing(self, desired, **extra_meta):
        meta = {
            "description": desired["description"],
            "capabilities": {"vision": False},
            "llamaswap": {"context_length": desired["context_length"]},
        }
        meta.update(extra_meta)
        return {"id": "model-a", "name": "Model A", "meta": meta, "params": {}}

    def test_arming_merges_policy_ids_and_keeps_human_added_ones(self):
        desired = sync.compute_desired(_router_entry())
        policy = sync.load_policy(_write_json_once(self, _policy()))
        existing = self._existing(desired, toolIds=["server:kagi_search"])
        action, diffs, body = sync.plan_change(
            "model-a", "Model A", desired, existing, policy=policy, arm=True
        )
        # Arming alone is enough to make the row an UPDATE.
        self.assertEqual(action, "update")
        self.assertEqual(
            body["meta"]["toolIds"],
            ["server:kagi_search", "server:web_fetch", "server:web_search"],
        )
        self.assertEqual(body["meta"]["defaultFeatureIds"], ["code_interpreter"])
        self.assertIn("meta.toolIds", {f for f, _, _ in diffs})
        self.assertIn("meta.defaultFeatureIds", {f for f, _, _ in diffs})

        # Second pass over the row the first pass would have written: no diff at all.
        settled = self._existing(
            desired,
            toolIds=list(body["meta"]["toolIds"]),
            defaultFeatureIds=list(body["meta"]["defaultFeatureIds"]),
        )
        action, diffs, _ = sync.plan_change(
            "model-a", "Model A", desired, settled, policy=policy, arm=True
        )
        self.assertEqual((action, diffs), ("noop", []))

    def test_retire_ids_are_filtered_out_of_both_arming_lists(self):
        desired = sync.compute_desired(_router_entry())
        policy = sync.load_policy(
            _write_json_once(
                self,
                _policy(
                    retire_tool_ids=["server:web_search", "server:dead_tool"],
                    retire_feature_ids=["image_generation"],
                ),
            )
        )
        existing = self._existing(
            desired,
            toolIds=["server:dead_tool", "server:human_pick"],
            defaultFeatureIds=["image_generation"],
        )
        _, _, body = sync.plan_change(
            "model-a", "Model A", desired, existing, policy=policy, arm=True
        )
        self.assertEqual(body["meta"]["toolIds"], ["server:human_pick", "server:web_fetch"])
        self.assertEqual(body["meta"]["defaultFeatureIds"], ["code_interpreter"])

    def test_a_row_driven_inactive_is_never_armed(self):
        desired = sync.compute_desired(_router_entry())
        policy = sync.load_policy(_write_json_once(self, _policy()))
        existing = self._existing(desired, toolIds=["server:human_pick"])
        action, diffs, body = sync.plan_change(
            "model-a", "Model A", desired, existing, policy=policy, arm=False
        )
        self.assertEqual((action, diffs, body), ("noop", [], {}))
        # And when something else forces an update, the lists round-trip verbatim.
        stale = self._existing(desired, toolIds=["server:human_pick"])
        stale["meta"]["description"] = "stale"
        _, _, body = sync.plan_change(
            "model-a", "Model A", desired, stale, policy=policy, arm=False
        )
        self.assertEqual(body["meta"]["toolIds"], ["server:human_pick"])
        self.assertNotIn("defaultFeatureIds", body["meta"])


class PlanDecisionMatrixTest(unittest.TestCase):
    """Table-driven: (existing row, desired visibility) -> (plan_change action, is_active action)."""

    def _base_existing(self, desired, **overrides):
        row = {
            "id": "model-a",
            "name": "Model A",
            "is_active": True,
            "meta": {
                "description": desired["description"],
                "capabilities": {"vision": False},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": {"temperature": 1.0},
        }
        row.update(overrides)
        return row

    def test_the_create_update_toggle_noop_matrix(self):
        desired = sync.compute_desired(_router_entry(sampling={"temperature": 1.0}))
        in_sync = self._base_existing(desired)
        stale_desc = self._base_existing(desired)
        stale_desc["meta"] = dict(stale_desc["meta"], description="stale")
        stale_vision = self._base_existing(desired)
        stale_vision["meta"] = dict(stale_vision["meta"], capabilities={"vision": True})
        stale_ctx = self._base_existing(desired)
        stale_ctx["meta"] = dict(stale_ctx["meta"], llamaswap={"context_length": 4096})
        stale_sampling = self._base_existing(desired, params={"temperature": 0.1})
        hidden = self._base_existing(desired)

        cases = (
            ("absent row is a create", None, True, "create", "create"),
            ("everything matches", in_sync, True, "noop", "noop"),
            ("stale description", stale_desc, True, "update", "noop"),
            ("stale vision", stale_vision, True, "update", "noop"),
            ("stale context length", stale_ctx, True, "update", "noop"),
            ("stale sampling key", stale_sampling, True, "update", "noop"),
            ("metadata in sync, visibility not", hidden, False, "noop", "toggle"),
        )
        for label, existing, wanted_active, want_action, want_active_action in cases:
            with self.subTest(case=label):
                action, _, _ = sync.plan_change("model-a", "Model A", desired, existing)
                self.assertEqual(action, want_action)
                active_action, _ = sync.plan_is_active(wanted_active, existing)
                self.assertEqual(active_action, want_active_action)

    def test_an_unchanged_row_plans_an_empty_body(self):
        desired = sync.compute_desired(_router_entry(sampling={"temperature": 1.0}))
        self.assertEqual(
            sync.plan_change("model-a", "Model A", desired, self._base_existing(desired)),
            ("noop", [], {}),
        )


class ManagedFieldRoundTripTest(unittest.TestCase):
    """Everything this script does not own round-trips byte-for-byte on an update body."""

    def test_unowned_meta_params_and_grants_survive_an_update(self):
        desired = sync.compute_desired(_router_entry(sampling={"temperature": 1.0}))
        existing = {
            "id": "model-a",
            "name": "A Human Renamed This",
            "base_model_id": None,
            "access_grants": [{"group_id": "g1"}],
            "meta": {
                "description": "stale",
                "capabilities": {"vision": False, "citations": True},
                "llamaswap": {"context_length": 4096, "note": "hand-added"},
                "profile_image_url": "/static/x.png",
                "tags": [{"name": "local"}],
            },
            "params": {"temperature": 0.1, "seed": 42, "stop": ["</s>"]},
        }
        action, _, body = sync.plan_change("model-a", "Model A", desired, existing)
        self.assertEqual(action, "update")
        self.assertEqual(body["name"], "A Human Renamed This")
        self.assertEqual(body["access_grants"], [{"group_id": "g1"}])
        self.assertEqual(body["meta"]["capabilities"]["citations"], True)
        self.assertEqual(body["meta"]["llamaswap"]["note"], "hand-added")
        self.assertEqual(body["meta"]["profile_image_url"], "/static/x.png")
        self.assertEqual(body["meta"]["tags"], [{"name": "local"}])
        self.assertEqual(body["params"]["seed"], 42)
        self.assertEqual(body["params"]["stop"], ["</s>"])
        # ...and the owned fields did move.
        self.assertEqual(body["meta"]["description"], desired["description"])
        self.assertEqual(body["params"]["temperature"], 1.0)

    def test_a_derived_row_is_never_updated_into_a_base_row(self):
        desired = sync.compute_desired(_router_entry())
        existing = {
            "id": "model-a--effort-low",
            "name": "Model A · low",
            "base_model_id": "model-a",
            "meta": {"description": "stale"},
            "params": {},
        }
        _, _, body = sync.plan_change(
            "model-a--effort-low",
            "Model A · low",
            desired,
            existing,
            base_model_id="model-a",
            reasoning_effort="low",
        )
        self.assertEqual(body["base_model_id"], "model-a")


class RetirementPredicateTest(unittest.TestCase):
    def test_ownership_proofs_and_the_unmanaged_default(self):
        cases = (
            ("llamaswap marker", "x", {"meta": {"llamaswap": {}}}, [], True),
            ("derived id shape", "x--effort-low", {"meta": {}}, [], True),
            ("named in retire_legacy_ids", "legacy", {"meta": {}}, ["legacy"], True),
            ("somebody else's row", "x", {"meta": {"description": "hand"}}, [], False),
            ("no meta at all", "x", {}, [], False),
            ("non-dict meta", "x", {"meta": "broken"}, [], False),
        )
        for label, row_id, row, legacy, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(sync.is_retirement_candidate(row_id, row, legacy), expected)


class DescriptionCompositionTest(unittest.TestCase):
    def test_the_description_is_a_pure_function_of_the_serving_facts(self):
        entry = _router_entry(sampling={"temperature": 1.0})
        self.assertEqual(sync.build_description(entry), sync.build_description(entry))
        ls = entry["meta"]["llamaswap"]
        ls["serving"].update({"flash_attention": True, "mtp": True, "moe_cpu_offload_layers": 12, "ubatch": 512})
        ls["input_modalities"] = ["text", "image"]
        ls["output_modalities"] = ["text", "audio"]
        text = sync.build_description(entry)
        for fragment in (
            "flash-attention",
            "MTP speculative decoding",
            "12 MoE layers offloaded to CPU",
            "-ub 512",
            "vision (image input)",
            "output: text/audio",
            "context: 131072 tokens",
        ):
            self.assertIn(fragment, text)

    def test_a_model_with_no_serving_block_still_describes(self):
        entry = {"id": "embed-a", "meta": {"llamaswap": {"context_length": 512}}}
        text = sync.build_description(entry)
        self.assertIn("unknown quant", text)
        self.assertIn("f16 KV cache", text)
        self.assertIn("Tool calling: no", text)


class DerivedIdentifierShapeTest(unittest.TestCase):
    def test_the_stored_id_and_display_name_shapes_are_append_only(self):
        self.assertEqual(sync.derived_row_id("model-a", "low"), "model-a--effort-low")
        self.assertEqual(sync.derived_row_name("Model A", "low"), "Model A · low")
        self.assertEqual(sync.EFFORT_LEVEL_ORDER, ("none", "low", "medium", "high", "xhigh"))


if __name__ == "__main__":
    import sys

    if not {"-h", "--help"} & set(sys.argv[1:]):
        from build_id import handle_build_flags

        _build_rc = handle_build_flags(sys.argv[1:], __file__)
        if _build_rc is not None:
            sys.exit(_build_rc)
    unittest.main()
