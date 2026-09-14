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

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
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
    """Stands in for `sync._http_json`; records every call and replays scripted responses.

    Row enumeration is two routes now (`/base` ∪ `/export`) and the single-row re-read is a
    third (`/api/v1/models/model?id=`), so this fixture routes on all three. `rows_by_call` is
    the queue of row-lists served to whichever enumeration/re-read call comes next -- a re-read
    pops one list and answers with the matching row (404 when that list does not carry it),
    exactly as Open WebUI does.
    """

    def __init__(self, rows_by_call, export_rows=None, router_models=None):
        self.calls = []
        self.rows_by_call = list(rows_by_call)
        self.export_rows = list(export_rows) if export_rows is not None else None
        self.router_models = list(router_models) if router_models is not None else []

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append((method, url, body))
        if url.endswith("/v1/models"):
            return 200, {"data": [dict(m) for m in self.router_models]}
        if url.endswith("/api/v1/models/base"):
            return 200, self.rows_by_call.pop(0)
        if url.endswith("/api/v1/models/export"):
            return 200, (self.export_rows.pop(0) if self.export_rows else [])
        if "/api/v1/models/model?id=" in url:
            wanted = urllib.parse.unquote(url.split("id=", 1)[1])
            for row in self.rows_by_call.pop(0):
                if row.get("id") == wanted:
                    return 200, row
            return 404, {"detail": "Model not found"}
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
        self.assertEqual(urls[1], "http://owui/api/v1/models/model?id=model-a")  # re-read
        self.assertIn("/api/v1/models/model/toggle?id=model-a", urls[2])
        self.assertEqual(
            urls[3], "http://owui/api/v1/models/model?id=model-a"
        )  # the read-back

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


# ---------------------------------------------------------------------------
# WP-2 (`plans/owui-effort-rows-and-arming.md`): arming policy, derived effort rows, retirement.
# ---------------------------------------------------------------------------


def _write_json(doc):
    """Write a JSON fixture to a temp file and return its path (caller unlinks)."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(doc, handle)
    handle.close()
    return Path(handle.name)


def _write_json_once(case, doc):
    """Write a JSON fixture registered for cleanup on `case`, and return its path."""
    path = _write_json(doc)
    case.addCleanup(path.unlink)
    return path


def _policy(**overrides):
    """A conforming `openwebui-policy-v1` document, overridable field by field."""
    doc = {
        "schema": sync.POLICY_SCHEMA,
        "arm_tool_ids": ["server:web_fetch", "server:web_search"],
        "arm_feature_ids": ["code_interpreter"],
        "retire_tool_ids": [],
        "retire_feature_ids": [],
        "retire_legacy_ids": [],
    }
    doc.update(overrides)
    return doc


def _gateway(**tables):
    """A gateway config whose SECOND provider carries `effortKwargs` (index is never hardcoded)."""
    return {
        "host": "127.0.0.1",
        "port": 4321,
        "providers": [
            {"name": "no-ladder-here", "type": "openai", "models": []},
            {"name": "llama", "type": "openai", "models": [], "effortKwargs": dict(tables)},
        ],
    }


def _vis_doc(models):
    """`models` is {model key: (consumers, gateway_id)}."""
    return {
        "schema": sync.VISIBILITY_SCHEMA,
        "models": {
            key: {"gateway_id": gateway_id, "consumers": list(consumers)}
            for key, (consumers, gateway_id) in models.items()
        },
    }


class _FakeOpenWebUI:
    """In-memory router + Open WebUI: serves both enumeration routes and APPLIES every write.

    Built for the `main()`-level tests, where the point is end-to-end behaviour (including
    idempotence on a second run) rather than one planned body. `/base` answers only rows with
    `base_model_id: null` and `/export` only rows that have one -- the live split this sync's
    union exists for. `toggle` FLIPS, exactly like the real route, so a test that toggles a
    matching row sees it move the wrong way instead of silently passing.
    """

    def __init__(self, router_models=(), rows=(), created_active=True):
        self.calls = []
        self.router_models = [dict(m) for m in router_models]
        self.rows = {row["id"]: dict(row) for row in rows}
        self.created_active = created_active

    def _id_from(self, url):
        return urllib.parse.unquote(url.split("id=", 1)[1])

    def __call__(self, method, url, *, headers=None, body=None, timeout=None):
        self.calls.append((method, url, body))
        if url.endswith("/v1/models") and "/api/" not in url:
            return 200, {"data": [dict(m) for m in self.router_models]}
        if url.endswith("/api/v1/models/base"):
            return 200, [dict(r) for r in self.rows.values() if r.get("base_model_id") is None]
        if url.endswith("/api/v1/models/export"):
            return 200, [
                dict(r) for r in self.rows.values() if r.get("base_model_id") is not None
            ]
        if url.endswith("/api/v1/models/create"):
            row = dict(body)
            row.setdefault("base_model_id", None)
            row["is_active"] = self.created_active
            self.rows[row["id"]] = row
            return 200, dict(row)
        if "/api/v1/models/model/update?id=" in url:
            row = self.rows[self._id_from(url)]
            row.update(dict(body))
            return 200, dict(row)
        if "/api/v1/models/model/toggle?id=" in url:
            row = self.rows[self._id_from(url)]
            row["is_active"] = not bool(row.get("is_active"))
            return 200, dict(row)
        if "/api/v1/models/model?id=" in url:
            row = self.rows.get(self._id_from(url))
            return (200, dict(row)) if row is not None else (404, {"detail": "Model not found"})
        raise AssertionError(f"unexpected route in a fixture-only test: {method} {url}")


class _MainHarness(unittest.TestCase):
    """Shared plumbing for the `main()`-level WP-2 tests. No socket is ever opened."""

    def _patch(self, http):
        original = sync._http_json
        sync._http_json = http
        self.addCleanup(lambda: setattr(sync, "_http_json", original))

    def _run(self, fake, visibility, *, policy=None, gateway=None, extra=()):
        self._patch(fake)
        docs = (visibility, policy or _policy(), gateway if gateway is not None else _gateway())
        paths = [_write_json_once(self, doc) for doc in docs]
        argv = [
            "--openwebui-url",
            "http://owui",
            "--openwebui-key",
            "k",
            "--router-url",
            "http://router",
            "--visibility",
            str(paths[0]),
            "--policy",
            str(paths[1]),
            "--gateway-config",
            str(paths[2]),
            *extra,
        ]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sync.main(argv)
        return code, buffer.getvalue()


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
            _gateway(
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
            ),
        )
        levels = sync.load_effort_levels(path)
        self.assertEqual(levels["gw-a"], ["none", "low", "medium", "xhigh"])

    def test_no_entry_yields_no_levels_and_a_ladderless_config_is_not_an_error(self):
        path = _write_json_once(self, _gateway(**{"gw-a": {"low": {"reasoning_effort": "low"}}}))
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
            self, {"host": "127.0.0.1", "port": 1, "providers": [{"name": "p"}]}
        )
        self.assertEqual(sync.load_effort_levels(ladderless), {})

        with self.assertRaises(sync.SyncError):
            sync.load_effort_levels(Path(tempfile.gettempdir()) / "no-gateway-config-3b91.json")

    def test_the_shipped_gateway_config_yields_the_qwen_ladder(self):
        path = sync.default_gateway_config_path()
        self.assertEqual(path.parts[-2:], ("router", "gateway.config.json"))
        levels = sync.load_effort_levels(path)
        self.assertEqual(levels["qwen38-27b-iq3s"], ["none", "low", "medium", "xhigh"])
        visibility = sync.load_visibility(sync.default_visibility_path())
        self.assertEqual(
            sync.effort_levels_for(visibility, levels, "Qwen3.8-27B-UD-IQ3_S"),
            ["none", "low", "medium", "xhigh"],
        )
        # The four `-reasoning-*` keys map to gateway ids with no `effortKwargs` entry.
        self.assertEqual(
            sync.effort_levels_for(visibility, levels, "Qwen3.8-27B-UD-IQ3_S-reasoning-low"), []
        )


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


class RowEnumerationTest(unittest.TestCase):
    def test_fetch_openwebui_rows_unions_base_and_export(self):
        fake = _FakeOpenWebUI(
            rows=[
                {"id": "model-a", "base_model_id": None, "is_active": True},
                {"id": "model-a--effort-low", "base_model_id": "model-a", "is_active": True},
            ]
        )
        original = sync._http_json
        sync._http_json = fake
        self.addCleanup(lambda: setattr(sync, "_http_json", original))
        rows = sync.fetch_openwebui_rows("http://owui", "k")
        self.assertEqual(sorted(rows), ["model-a", "model-a--effort-low"])
        self.assertEqual(
            [url for _, url, _ in fake.calls],
            ["http://owui/api/v1/models/base", "http://owui/api/v1/models/export"],
        )


class RetirementAndDerivedRunTest(_MainHarness):
    """End-to-end `main()` runs against the in-memory Open WebUI. Fixtures only, no socket."""

    ROUTER = [
        _router_entry(router_id="model-a", name="Model A", sampling={"temperature": 1.0}),
        _router_entry(router_id="model-b", name="Model B"),
    ]
    VISIBILITY = _vis_doc({"model-a": (["openwebui"], "gw-a"), "model-b": (["openwebui"], None)})
    GATEWAY = _gateway(**{"gw-a": {"low": {"reasoning_effort": "low"}}})

    def _orphans(self):
        return [
            # Marker row: this sync wrote it, the router no longer serves it.
            {
                "id": "dead-sync-row",
                "base_model_id": None,
                "is_active": True,
                "meta": {"llamaswap": {"context_length": 1}},
            },
            # No marker, not listed: somebody else's row.
            {
                "id": "somebody-elses-model",
                "base_model_id": None,
                "is_active": True,
                "meta": {"description": "hand-made"},
            },
            # Legacy list: no marker, but named in the policy.
            {
                "id": "gemma-4-12B-it-Q4_K_M",
                "base_model_id": None,
                "is_active": True,
                "meta": {"description": "annotate-created"},
            },
            # Derived shape for a level that no longer exists -- already inactive.
            {
                "id": "model-a--effort-max",
                "base_model_id": "model-a",
                "is_active": False,
                "meta": {"llamaswap": {"context_length": 1}},
            },
        ]

    def test_a_full_apply_retires_owned_orphans_and_never_touches_unmanaged_rows(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER, rows=self._orphans())
        code, out = self._run(
            fake,
            self.VISIBILITY,
            policy=_policy(retire_legacy_ids=["gemma-4-12B-it-Q4_K_M"]),
            gateway=self.GATEWAY,
            extra=["--apply"],
        )
        self.assertEqual(code, 0, out)

        # Derived rows exist, with the base's sampling keys plus their own level.
        for level in ("none", "low"):
            row = fake.rows[f"model-a--effort-{level}"]
            self.assertEqual(row["base_model_id"], "model-a")
            self.assertEqual(row["params"]["reasoning_effort"], level)
            self.assertEqual(row["params"]["temperature"], 1.0)
            self.assertEqual(row["name"], f"Model A · {level}")
            self.assertEqual(row["meta"]["toolIds"], ["server:web_fetch", "server:web_search"])
            self.assertEqual(row["meta"]["defaultFeatureIds"], ["code_interpreter"])
        self.assertIn("DERIVE", out)
        # `model-b` has no ladder, so it gets no derived rows at all.
        self.assertEqual([r for r in fake.rows if r.startswith("model-b--")], [])

        # Retirement: the two owned-and-active orphans are now inactive...
        self.assertFalse(fake.rows["dead-sync-row"]["is_active"])
        self.assertFalse(fake.rows["gemma-4-12B-it-Q4_K_M"]["is_active"])
        # ...the unmanaged row is untouched and never appears in any URL...
        self.assertTrue(fake.rows["somebody-elses-model"]["is_active"])
        self.assertIn("UNMANAGED somebody-elses-model", out)
        self.assertFalse(
            any("somebody-elses-model" in url for _, url, _ in fake.calls),
            "an UNMANAGED row must not be read back, toggled or updated",
        )
        # ...and the already-inactive candidate is reported, never toggled (the route flips).
        self.assertIn("retired (already inactive) model-a--effort-max", out)
        self.assertFalse(fake.rows["model-a--effort-max"]["is_active"])
        self.assertFalse(
            any("toggle" in url and "model-a--effort-max" in url for _, url, _ in fake.calls)
        )
        # Nothing is ever deleted.
        self.assertFalse(any("/delete" in url for _, url, _ in fake.calls))

    def test_a_second_apply_is_a_total_no_op(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER, rows=self._orphans())
        policy = _policy(retire_legacy_ids=["gemma-4-12B-it-Q4_K_M"])
        code, _ = self._run(
            fake, self.VISIBILITY, policy=policy, gateway=self.GATEWAY, extra=["--apply"]
        )
        self.assertEqual(code, 0)
        code, out = self._run(
            fake, self.VISIBILITY, policy=policy, gateway=self.GATEWAY, extra=["--apply"]
        )
        self.assertEqual(code, 0, out)
        self.assertIn(
            "summary: 0 create, 0 derive, 0 update, 0 visibility toggle, "
            "4 unchanged, 0 retire, 1 unmanaged",
            out,
        )

    def test_only_never_retires_and_never_orphans_another_models_rows(self):
        rows = self._orphans() + [
            {
                "id": "model-b",
                "base_model_id": None,
                "is_active": True,
                "meta": {"llamaswap": {"context_length": 131072}},
            }
        ]
        fake = _FakeOpenWebUI(router_models=self.ROUTER, rows=rows)
        code, out = self._run(
            fake,
            self.VISIBILITY,
            policy=_policy(retire_legacy_ids=["gemma-4-12B-it-Q4_K_M"]),
            gateway=self.GATEWAY,
            extra=["--apply", "--only", "model-a"],
        )
        self.assertEqual(code, 0, out)
        # The would-be retirements are printed and explicitly skipped...
        self.assertIn("RETIRE dead-sync-row  (skipped: --only)", out)
        self.assertIn("RETIRE gemma-4-12B-it-Q4_K_M  (skipped: --only)", out)
        self.assertTrue(fake.rows["dead-sync-row"]["is_active"])
        self.assertTrue(fake.rows["gemma-4-12B-it-Q4_K_M"]["is_active"])
        # ...and the model the filter excluded is NOT mistaken for an orphan.
        self.assertNotIn("RETIRE model-b", out)
        self.assertNotIn("UNMANAGED model-b", out)
        self.assertTrue(fake.rows["model-b"]["is_active"])

    def test_a_level_dropped_from_effortkwargs_retires_its_derived_row(self):
        rows = [
            {
                "id": "model-a--effort-xhigh",
                "base_model_id": "model-a",
                "is_active": True,
                "params": {"reasoning_effort": "xhigh"},
                "meta": {"llamaswap": {"context_length": 131072}},
            }
        ]
        fake = _FakeOpenWebUI(router_models=self.ROUTER, rows=rows)
        code, out = self._run(fake, self.VISIBILITY, gateway=self.GATEWAY, extra=["--apply"])
        self.assertEqual(code, 0, out)
        self.assertIn("RETIRE model-a--effort-xhigh", out)
        self.assertFalse(fake.rows["model-a--effort-xhigh"]["is_active"])
        # The levels still in the table were created, not retired.
        self.assertTrue(fake.rows["model-a--effort-low"]["is_active"])

    def test_derived_rows_go_inactive_when_the_base_is_hidden(self):
        rows = [
            {
                "id": "model-a",
                "base_model_id": None,
                "is_active": True,
                "meta": {"llamaswap": {"context_length": 131072}},
                "params": {},
            },
            {
                "id": "model-a--effort-low",
                "base_model_id": "model-a",
                "is_active": True,
                "params": {"reasoning_effort": "low"},
                "meta": {"llamaswap": {"context_length": 131072}},
            },
        ]
        fake = _FakeOpenWebUI(router_models=self.ROUTER, rows=rows)
        hidden = _vis_doc({"model-a": ([], "gw-a"), "model-b": (["openwebui"], None)})
        code, out = self._run(fake, hidden, gateway=self.GATEWAY, extra=["--apply"])
        self.assertEqual(code, 0, out)
        self.assertFalse(fake.rows["model-a"]["is_active"])
        self.assertFalse(fake.rows["model-a--effort-low"]["is_active"])
        # A hidden row is never armed on the way down.
        self.assertNotIn("toolIds", fake.rows["model-a--effort-low"]["meta"])
        # The derived row the base still needs exists, held inactive rather than absent.
        self.assertFalse(fake.rows["model-a--effort-none"]["is_active"])


if __name__ == "__main__":
    unittest.main()
