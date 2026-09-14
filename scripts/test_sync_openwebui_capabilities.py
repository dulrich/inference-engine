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
import json
import sys
import tempfile
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


def _visibility(**models):
    """A `serving-visibility-v1` document over the given `key=consumers` pairs."""
    return {
        "schema": "sync.VISIBILITY_SCHEMA",
        "models": {k: {"gateway_id": None, "consumers": v} for k, v in models.items()},
    }


def _write_visibility(doc):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, handle)
    handle.close()
    return Path(handle.name)


class VisibilityDeclarationTest(unittest.TestCase):
    """WP-3: `is_active` is owned by the sync and driven by `router/visibility.json`."""

    def _doc(self, **models):
        doc = _visibility(**models)
        doc["schema"] = sync.VISIBILITY_SCHEMA
        return doc

    def test_default_path_is_resolved_from_the_script_not_the_cwd(self):
        path = sync.default_visibility_path()
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parts[-2:], ("router", "visibility.json"))
        self.assertEqual(path.parent.parent, Path(sync.__file__).resolve().parent.parent)

    def test_desired_is_active_follows_the_declared_consumers(self):
        models = self._doc(
            visible=["cc", "openwebui"],
            owui_only=["openwebui"],
            cc_only=["cc"],
            router_only=[],
        )["models"]
        self.assertTrue(sync.desired_is_active(models, "visible"))
        self.assertTrue(sync.desired_is_active(models, "owui_only"))
        self.assertFalse(sync.desired_is_active(models, "cc_only"))
        self.assertFalse(sync.desired_is_active(models, "router_only"))

    def test_an_undeclared_router_id_is_an_error_not_a_default(self):
        models = self._doc(declared=["openwebui"])["models"]
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
        missing = Path(tempfile.gettempdir()) / "definitely-not-a-visibility-file-9e3f.json"
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
        doc["schema"] = "serving-visibility-v2"
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


class _RecordingHttp:
    """Stands in for `sync._http_json`; records every call and replays scripted responses."""

    def __init__(self, rows_by_call):
        self.calls = []
        self.rows_by_call = list(rows_by_call)

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append((method, url, body))
        if url.endswith("/api/v1/models/base"):
            return 200, self.rows_by_call.pop(0)
        return 200, {"ok": True}


class ApplyIsActiveTest(unittest.TestCase):
    """The apply path: create, RE-READ, toggle only if the created row's actual value differs."""

    def _patch(self, http):
        original = sync._http_json
        sync._http_json = http
        self.addCleanup(lambda: setattr(sync, "_http_json", original))

    def test_created_row_is_re_read_and_toggled_only_when_its_default_differs(self):
        # Open WebUI creates the row active; the declaration wants it inactive.
        http = _RecordingHttp([[{"id": "model-a", "is_active": True}], [{"id": "model-a", "is_active": False}]])
        self._patch(http)
        failures = sync.apply_model(
            "http://owui", "key", "model-a", "create", {"id": "model-a"}, "create", False
        )
        self.assertEqual(failures, 0)
        urls = [url for _, url, _ in http.calls]
        self.assertEqual(urls[0], "http://owui/api/v1/models/create")
        self.assertEqual(urls[1], "http://owui/api/v1/models/base")  # the re-read
        self.assertIn("/api/v1/models/model/toggle?id=model-a", urls[2])
        self.assertEqual(urls[3], "http://owui/api/v1/models/base")  # the read-back

    def test_created_row_that_already_matches_is_never_toggled(self):
        http = _RecordingHttp([[{"id": "model-a", "is_active": False}]])
        self._patch(http)
        failures = sync.apply_model(
            "http://owui", "key", "model-a", "create", {"id": "model-a"}, "create", False
        )
        self.assertEqual(failures, 0)
        self.assertFalse(any("toggle" in url for _, url, _ in http.calls))

    def test_an_in_sync_row_issues_no_call_of_any_kind(self):
        http = _RecordingHttp([])
        self._patch(http)
        failures = sync.apply_model("http://owui", "key", "model-a", "noop", {}, "noop", True)
        self.assertEqual(failures, 0)
        self.assertEqual(http.calls, [])

    def test_a_toggle_that_does_not_move_the_flag_is_a_failure_not_a_green_run(self):
        # Probe by body, never status code: a 200 whose read-back still disagrees fails.
        http = _RecordingHttp([[{"id": "model-a", "is_active": True}]])
        self._patch(http)
        failures = sync.apply_model("http://owui", "key", "model-a", "noop", {}, "toggle", False)
        self.assertEqual(failures, 1)


class MainFailClosedTest(unittest.TestCase):
    def test_a_broken_declaration_stops_the_run_before_any_http_call(self):
        http = _RecordingHttp([])
        original = sync._http_json
        sync._http_json = http
        self.addCleanup(lambda: setattr(sync, "_http_json", original))

        missing = Path(tempfile.gettempdir()) / "definitely-not-a-visibility-file-4a71.json"
        code = sync.main(
            [
                "--openwebui-url",
                "http://owui",
                "--openwebui-key",
                "k",
                "--visibility",
                str(missing),
                "--apply",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(http.calls, [], "a fail-closed declaration must make no call at all")


if __name__ == "__main__":
    unittest.main()
