#!/usr/bin/env python3
"""Unit tests for `openwebui_sync/declarations.py`: fail-closed loading of the model catalog
(visibility + effort ladder) and the policy declaration. No network, no live Open WebUI or router calls
anywhere in this suite -- fixtures only.

Split from the monolithic `test_sync_openwebui_capabilities.py` (WP-R6a); see
`openwebui_sync/declarations.py`'s own docstring for what this layer owns: every loader here is
FAIL-CLOSED by contract -- a missing, unparseable or unknown-schema document raises `SyncError`
rather than yielding an empty default, because an empty-by-accident declaration would silently
deactivate every model.

The module under test has a hyphenated filename (matching this repo's script-invocation
convention: `python3 scripts/sync-openwebui-capabilities.py ...`), so it is not importable via
a normal `import` statement; it is loaded here via `importlib.util` from its file path, exactly
as `sync_test_helpers.py` (this file's shared-fixture module) does. The script puts its own
directory on `sys.path` before importing `openwebui_sync`, so that loader works unchanged.

Run all four split modules: python3 -m unittest discover -s scripts -p 'test_sync_*.py' -v
This module alone: cd scripts && python3 -m unittest test_sync_declarations -v
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from sync_test_helpers import _catalog_doc, _gateway, _policy, _vis_doc, _write_json_once

MODULE_PATH = Path(__file__).resolve().parent / "sync-openwebui-capabilities.py"
_spec = importlib.util.spec_from_file_location("sync_openwebui_capabilities", MODULE_PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync
_spec.loader.exec_module(sync)  # type: ignore[union-attr]


def _visibility(**models):
    """A catalog document over the given `key=consumers` pairs (all `exposures` records)."""
    return _catalog_doc(
        {"models": {k: {"gateway_id": None, "consumers": v} for k, v in models.items()}}
    )


def _write_visibility(doc):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, handle)
    handle.close()
    return Path(handle.name)


class VisibilityDeclarationTest(unittest.TestCase):
    """`is_active` is owned by the sync and driven by the model catalog's visibility."""

    def _doc(self, **models):
        return _visibility(**models)

    def _models(self, **models):
        path = _write_visibility(self._doc(**models))
        self.addCleanup(path.unlink)
        return sync.load_visibility(path)

    def test_default_path_is_resolved_from_the_script_not_the_cwd(self):
        path = sync.default_catalog_path()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parts[-3:], ("router", "catalog", "catalog.json"))
        self.assertEqual(path.parent.parent.parent, Path(sync.__file__).resolve().parent.parent)

    def test_desired_is_active_follows_the_declared_consumers(self):
        models = _vis_doc(
            {
                "visible": (["cc", "openwebui"], "gw-v"),
                "owui_only": (["openwebui"], None),
                "cc_only": (["cc"], "gw-c"),
                "router_only": ([], None),
            }
        )["models"]
        self.assertTrue(sync.desired_is_active(models, "visible"))
        self.assertTrue(sync.desired_is_active(models, "owui_only"))
        self.assertFalse(sync.desired_is_active(models, "cc_only"))
        self.assertFalse(sync.desired_is_active(models, "router_only"))

    def test_an_undeclared_router_id_is_an_error_not_a_default(self):
        models = self._models(declared=["openwebui"])
        with self.assertRaises(sync.SyncError) as ctx:
            sync.desired_is_active(models, "some-new-key")
        self.assertIn("no entry in the visibility declaration", str(ctx.exception))

    def test_unknown_consumer_token_is_rejected(self):
        doc = self._doc(model_a=["openwebui", "slack"])
        path = _write_visibility(doc)
        self.addCleanup(path.unlink)
        with self.assertRaises(sync.SyncError) as ctx:
            sync.load_visibility(path)
        self.assertIn("unknown consumer token", str(ctx.exception))

    def test_missing_unparseable_and_wrong_schema_are_all_fail_closed(self):
        missing = Path(tempfile.gettempdir()) / "definitely-not-a-catalog-file-9e3f.json"
        with self.assertRaises(sync.SyncError):
            sync.load_visibility(missing)

        broken = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        broken.write("{not json")
        broken.close()
        self.addCleanup(lambda: Path(broken.name).unlink())
        with self.assertRaises(sync.SyncError) as ctx:
            sync.load_visibility(Path(broken.name))
        self.assertIn("not valid JSON", str(ctx.exception))

        doc = self._doc(model_a=["openwebui"])
        doc["schema"] = "model-catalog-v2"
        path = _write_visibility(doc)
        self.addCleanup(path.unlink)
        with self.assertRaises(sync.SyncError) as ctx:
            sync.load_visibility(path)
        self.assertIn("expected", str(ctx.exception))

    def test_a_conforming_declaration_loads(self):
        path = _write_visibility(self._doc(model_a=["cc", "openwebui"], model_b=[]))
        self.addCleanup(path.unlink)
        models = sync.load_visibility(path)
        self.assertEqual(sorted(models), ["model_a", "model_b"])
        self.assertTrue(sync.desired_is_active(models, "model_a"))
        self.assertFalse(sync.desired_is_active(models, "model_b"))

    def test_claude_local_model_keys_and_exposures_project_into_one_map(self):
        doc = _catalog_doc(
            _vis_doc({"Key-A": (["cc", "openwebui"], "gw-a"), "Key-B": ([], None)})
        )
        doc["providers"]["claude-local"]["models"].append({"id": "no-key"})
        path = _write_visibility(doc)
        self.addCleanup(path.unlink)
        self.assertEqual(
            sync.load_visibility(path),
            {
                "Key-A": {"gateway_id": "gw-a", "consumers": ["cc", "openwebui"]},
                "Key-B": {"gateway_id": None, "consumers": []},
            },
        )
        doc["exposures"]["Key-A"] = {"consumers": []}
        path = _write_visibility(doc)
        self.addCleanup(path.unlink)
        with self.assertRaises(sync.SyncError) as ctx:
            sync.load_visibility(path)
        self.assertIn("declared twice", str(ctx.exception))

    def test_the_shipped_catalog_projects_well_formed_records(self):
        # Structural only: the live catalog's contents (model count, who consumes what) are
        # operator data that changes without a code change, so they are never pinned here.
        models = sync.load_visibility(sync.default_catalog_path())
        self.assertTrue(models)
        for key, record in models.items():
            with self.subTest(model=key):
                self.assertEqual(sorted(record), ["consumers", "gateway_id"])
                self.assertIsInstance(record["gateway_id"], (str, type(None)))
                self.assertIsInstance(record["consumers"], list)


class PolicyLoaderTest(unittest.TestCase):
    """Audit E2: every malformed policy shape stops the run, none of them degrades to a default."""

    def test_every_malformed_policy_shape_is_fail_closed(self):
        missing = Path(tempfile.gettempdir()) / "definitely-not-a-policy-file-7c21.json"
        with self.assertRaises(sync.SyncError):
            sync.load_policy(missing)

        broken = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        broken.write("{not json")
        broken.close()
        self.addCleanup(lambda: Path(broken.name).unlink())
        with self.assertRaises(sync.SyncError) as ctx:
            sync.load_policy(Path(broken.name))
        self.assertIn("not valid JSON", str(ctx.exception))

        malformed = {
            "a bare list is not a policy": ["server:web_fetch"],
            "wrong schema": _policy(schema="openwebui-policy-v2"),
            "missing schema": {k: v for k, v in _policy().items() if k != "schema"},
            "a list field is a string": _policy(arm_tool_ids="server:web_fetch"),
            "a list field holds a non-string": _policy(arm_feature_ids=["ok", 7]),
            "a list field is absent": {
                k: v for k, v in _policy().items() if k != "retire_legacy_ids"
            },
            "an unknown top-level key": dict(_policy(), arm_everything=True),
        }
        for label, doc in malformed.items():
            path = _write_json_once(self, doc)
            with self.subTest(shape=label), self.assertRaises(sync.SyncError):
                sync.load_policy(path)

    def test_the_shipped_policy_loads_with_five_string_lists(self):
        path = sync.default_policy_path()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parts[-2:], ("scripts", "openwebui-policy.json"))
        policy = sync.load_policy(path)
        self.assertEqual(sorted(policy), sorted(sync.POLICY_LIST_FIELDS))
        self.assertEqual(
            policy["arm_tool_ids"],
            ["server:web_fetch", "server:web_search", "server:kagi_search"],
        )
        self.assertEqual(policy["arm_feature_ids"], ["code_interpreter"])
        self.assertIn("gemma-4-12B-it-Q4_K_M", policy["retire_legacy_ids"])


class EffortLadderTest(unittest.TestCase):
    def test_levels_are_the_distinct_reasoning_efforts_plus_none_in_canonical_order(self):
        path = _write_json_once(
            self,
            _catalog_doc(
                ladders=_gateway(
                **{
                    # Deliberately out of canonical order, with two gateway levels collapsing
                    # onto one `reasoning_effort` value and one entry carrying none at all.
                    "gw-a": {
                        "xhigh": {"reasoning_effort": "xhigh"},
                        "low": {"reasoning_effort": "low"},
                        "high": {"reasoning_effort": "medium"},
                        "medium": {"reasoning_effort": "medium"},
                        "max": {"reasoning_effort": "xhigh"},
                        "disabled": {"enable_thinking": False},
                    }
                }
            )),
        )
        levels = sync.load_effort_levels(path)
        self.assertEqual(levels["gw-a"], ["none", "low", "medium", "xhigh"])

    def test_no_entry_yields_no_levels_and_a_ladderless_config_is_not_an_error(self):
        path = _write_json_once(
            self, _catalog_doc(ladders=_gateway(**{"gw-a": {"low": {"reasoning_effort": "low"}}}))
        )
        levels = sync.load_effort_levels(path)
        self.assertIsNone(levels.get("gw-b"))

        visibility = _vis_doc(
            {"with-ladder": (["openwebui"], "gw-a"), "without": (["openwebui"], "gw-b")}
        )["models"]
        self.assertEqual(
            sync.effort_levels_for(visibility, levels, "with-ladder"), ["none", "low"]
        )
        self.assertEqual(sync.effort_levels_for(visibility, levels, "without"), [])
        # A key with `gateway_id: null` (embed/rerank) has no ladder either.
        self.assertEqual(sync.effort_levels_for({"x": {"gateway_id": None}}, levels, "x"), [])

        ladderless = _write_json_once(
            self, {"schema": sync.CATALOG_SCHEMA, "providers": {"p": {"models": [{"id": "m"}]}}}
        )
        self.assertEqual(sync.load_effort_levels(ladderless), {})

        with self.assertRaises(sync.SyncError):
            sync.load_effort_levels(Path(tempfile.gettempdir()) / "no-catalog-3b91.json")

    def test_the_shipped_catalog_yields_the_qwen_ladder(self):
        path = sync.default_catalog_path()
        levels = sync.load_effort_levels(path)
        self.assertEqual(levels["qwen38-27b-iq3s"], ["none", "low", "medium", "xhigh"])
        visibility = sync.load_visibility(path)
        self.assertEqual(
            sync.effort_levels_for(visibility, levels, "Qwen3.8-27B-UD-IQ3_S"),
            ["none", "low", "medium", "xhigh"],
        )
        # The four `-reasoning-*` keys map to gateway ids with no `effortKwargs` entry.
        self.assertEqual(
            sync.effort_levels_for(visibility, levels, "Qwen3.8-27B-UD-IQ3_S-reasoning-low"), []
        )


if __name__ == "__main__":
    unittest.main()
