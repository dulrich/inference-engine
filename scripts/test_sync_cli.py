#!/usr/bin/env python3
"""Unit tests for `sync-openwebui-capabilities.py`'s `main()`: the CLI coordinator -- credential
guards, dry-run vs `--apply` selection, `--only` filtering, fail-closed load paths, and full
create/derive/update/toggle/retire runs against an in-memory Open WebUI. These exercise
`openwebui_sync/runner.py`'s row-application path too, since `main()` is the only caller that
drives it: no test here calls `runner.process_row` / `sync_model` / `retire_rows` directly, so
these end-to-end runs are also this suite's only coverage of that layer. No network, no live
Open WebUI or router calls anywhere in this suite -- fixtures only, via `_FakeOpenWebUI`.

Split from the monolithic `test_sync_openwebui_capabilities.py` (WP-R6a); see
`sync-openwebui-capabilities.py`'s own docstring and `openwebui_sync/runner.py`'s docstring for
what these two layers own together: the planner's decisions carried out through the client, with
`main()` as the stable invocation path whose argument surface this WP does not change.

The module under test has a hyphenated filename (matching this repo's script-invocation
convention: `python3 scripts/sync-openwebui-capabilities.py ...`), so it is not importable via
a normal `import` statement; it is loaded here via `importlib.util` from its file path, exactly
as `sync_test_helpers.py` (this file's shared-fixture module) does.

Run all four split modules: python3 -m unittest discover -s scripts -p 'test_sync_*.py' -v
This module alone: cd scripts && python3 -m unittest test_sync_cli -v
"""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

from sync_test_helpers import (
    _FakeOpenWebUI,
    _RecordingHttp,
    _catalog_doc,
    _gateway,
    _policy,
    _router_entry,
    _vis_doc,
    _write_json_once,
)

MODULE_PATH = Path(__file__).resolve().parent / "sync-openwebui-capabilities.py"
_spec = importlib.util.spec_from_file_location("sync_openwebui_capabilities", MODULE_PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync
_spec.loader.exec_module(sync)  # type: ignore[union-attr]


class _MainHarness(unittest.TestCase):
    """Shared plumbing for the `main()`-level WP-2 tests. No socket is ever opened."""

    def _patch(self, http):
        original = sync.client._http_json
        sync.client._http_json = http
        self.addCleanup(lambda: setattr(sync.client, "_http_json", original))

    def _run(self, fake, visibility, *, policy=None, gateway=None, extra=()):
        self._patch(fake)
        catalog = _catalog_doc(visibility, gateway if gateway is not None else _gateway())
        paths = [_write_json_once(self, doc) for doc in (catalog, policy or _policy())]
        argv = [
            "--openwebui-url",
            "http://owui",
            "--openwebui-key",
            "k",
            "--router-url",
            "http://router",
            "--catalog",
            str(paths[0]),
            "--policy",
            str(paths[1]),
            *extra,
        ]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = sync.main(argv)
        return code, buffer.getvalue()


class MainFailClosedTest(unittest.TestCase):
    def test_a_broken_declaration_stops_the_run_before_any_http_call(self):
        http = _RecordingHttp([])
        original = sync.client._http_json
        sync.client._http_json = http
        self.addCleanup(lambda: setattr(sync.client, "_http_json", original))

        missing = Path(tempfile.gettempdir()) / "definitely-not-a-catalog-file-4a71.json"
        code = sync.main(
            [
                "--openwebui-url",
                "http://owui",
                "--openwebui-key",
                "k",
                "--catalog",
                str(missing),
                "--apply",
            ]
        )
        self.assertEqual(code, 1)
        self.assertEqual(http.calls, [], "a fail-closed declaration must make no call at all")

    def test_the_retired_input_flags_are_an_error_before_any_http_call(self):
        http = _RecordingHttp([])
        original = sync.client._http_json
        sync.client._http_json = http
        self.addCleanup(lambda: setattr(sync.client, "_http_json", original))
        for flag in ("--visibility", "--gateway-config"):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = sync.main(
                    ["--openwebui-url", "http://owui", "--openwebui-key", "k", flag, "x.json"]
                )
            self.assertEqual(code, 2)
            self.assertIn(f"{flag} is retired", errors.getvalue())
        self.assertEqual(http.calls, [])


# ---------------------------------------------------------------------------
# WP-2 (`plans/owui-effort-rows-and-arming.md`): arming policy, derived effort rows, retirement.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# WP-R2 characterization: the decision matrix, managed-field round-trip, `--only`,
# dry-run vs apply selection and the fail-closed load paths, pinned BEFORE the
# plan/apply layers were split out of the script. Fixtures only, no socket.
# ---------------------------------------------------------------------------


class CliSelectionTest(_MainHarness):
    """`main()`'s coordination: credential guards, dry-run selection, `--only`, skips, drift."""

    ROUTER = [
        _router_entry(router_id="model-a", name="Model A", sampling={"temperature": 1.0}),
        _router_entry(router_id="model-b", name="Model B"),
    ]
    VISIBILITY = _vis_doc({"model-a": (["openwebui"], None), "model-b": (["openwebui"], None)})

    def test_missing_credentials_exit_2_before_any_call(self):
        fake = _FakeOpenWebUI()
        self._patch(fake)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            self.assertEqual(sync.main(["--openwebui-key", "k"]), 2)
            self.assertEqual(sync.main(["--openwebui-url", "http://owui"]), 2)
        self.assertEqual(fake.calls, [])
        self.assertIn("base URL not set", buffer.getvalue())
        self.assertIn("API key not set", buffer.getvalue())

    def test_the_default_run_is_a_dry_run_that_mutates_nothing(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER)
        code, out = self._run(fake, self.VISIBILITY)
        self.assertEqual(code, 0, out)
        self.assertIn("=== sync-openwebui-capabilities: DRY-RUN ===", out)
        self.assertIn("CREATE", out)
        self.assertIn("is_active: (no row yet) -> True", out)
        self.assertEqual(fake.rows, {})
        self.assertEqual(
            [method for method, _, _ in fake.calls], ["GET", "GET", "GET"]
        )

    def test_quiet_prints_nothing_on_a_no_op_run(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER)
        code, _ = self._run(fake, self.VISIBILITY, extra=["--apply"])
        self.assertEqual(code, 0)
        code, out = self._run(fake, self.VISIBILITY, extra=["--apply", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_quiet_still_prints_a_mutation_and_the_summary(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER)
        code, out = self._run(fake, self.VISIBILITY, extra=["--apply", "--quiet"])
        self.assertEqual(code, 0, out)
        self.assertIn("[model-a] CREATE", out)
        self.assertIn("applied (status=", out)
        self.assertIn("summary: 2 create", out)
        self.assertNotIn("=== sync-openwebui-capabilities", out)

    def test_without_quiet_a_no_op_run_still_prints_everything(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER)
        self._run(fake, self.VISIBILITY, extra=["--apply"])
        code, out = self._run(fake, self.VISIBILITY, extra=["--apply"])
        self.assertEqual(code, 0, out)
        self.assertIn("=== sync-openwebui-capabilities: APPLY ===", out)
        self.assertIn("(already in sync)", out)
        self.assertIn("summary: 0 create", out)

    def test_only_narrows_the_walk_and_warns_about_an_unknown_id(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code, out = self._run(fake, self.VISIBILITY, extra=["--only", "model-a", "--only", "nope"])
        self.assertEqual(code, 0, out)
        self.assertIn("models:     1", out)
        self.assertIn("[model-a]", out)
        self.assertNotIn("[model-b]", out)
        self.assertIn("--only nope not found in router /v1/models", errors.getvalue())

    def test_a_non_model_llamaswap_type_is_skipped_entirely(self):
        entry = _router_entry(router_id="draft-a", name="Draft A")
        entry["meta"]["llamaswap"]["type"] = "draft"
        fake = _FakeOpenWebUI(router_models=[entry])
        code, out = self._run(
            fake, _vis_doc({"draft-a": (["openwebui"], None)}), extra=["--apply"]
        )
        self.assertEqual(code, 0, out)
        self.assertIn("skip: meta.llamaswap.type='draft', not a model", out)
        self.assertEqual(fake.rows, {})

    def test_an_undeclared_router_id_fails_the_run_without_touching_that_row(self):
        fake = _FakeOpenWebUI(router_models=self.ROUTER)
        code, out = self._run(
            fake, _vis_doc({"model-a": (["openwebui"], None)}), extra=["--apply"]
        )
        self.assertEqual(code, 1, out)
        self.assertIn("[model-b] FAIL:", out)
        self.assertNotIn("model-b", str(fake.rows.keys()))
        self.assertFalse(any("model-b" in url for _, url, _ in fake.calls))


class FailClosedLoadPathTest(_MainHarness):
    """A malformed policy or catalog stops the run before the first HTTP call."""

    def _run_with(self, **docs):
        fake = _FakeOpenWebUI()
        self._patch(fake)
        catalog = docs.get("catalog")
        if catalog is None:
            catalog = _catalog_doc(docs["visibility"], docs["gateway"])
        paths = {
            "catalog": _write_json_once(self, catalog),
            "policy": _write_json_once(self, docs["policy"]),
        }
        errors = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code = sync.main(
                [
                    "--openwebui-url",
                    "http://owui",
                    "--openwebui-key",
                    "k",
                    "--catalog",
                    str(paths["catalog"]),
                    "--policy",
                    str(paths["policy"]),
                    "--apply",
                ]
            )
        return code, errors.getvalue(), fake

    def test_a_malformed_policy_stops_the_run_before_any_http_call(self):
        code, errors, fake = self._run_with(
            visibility=_vis_doc({"model-a": (["openwebui"], None)}),
            policy=_policy(arm_tool_ids="not-a-list"),
            gateway=_gateway(),
        )
        self.assertEqual(code, 1)
        self.assertIn("must be a list of strings", errors)
        self.assertEqual(fake.calls, [])

    def test_a_malformed_catalog_stops_the_run_before_any_http_call(self):
        code, errors, fake = self._run_with(
            catalog=["not", "an", "object"],
            policy=_policy(),
        )
        self.assertEqual(code, 1)
        self.assertIn("not a JSON object", errors)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
