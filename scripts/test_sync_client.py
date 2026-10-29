#!/usr/bin/env python3
"""Unit tests for `openwebui_sync/client.py`: endpoint shapes, the apply path (create, RE-READ,
toggle only if the created row's actual value differs), and row enumeration. No network, no
live Open WebUI or router calls anywhere in this suite -- fixtures only.

Split from the monolithic `test_sync_openwebui_capabilities.py` (WP-R6a); see
`openwebui_sync/client.py`'s own docstring for what this layer owns: `_http_json` never raises
on a non-2xx (callers probe by response BODY, never by status code), and every mutating call
that matters is read back before it is believed. The fixtures-only suite replaces `_http_json`
ON `openwebui_sync.client` (`sync.client._http_json = fake`); every function under test resolves
it through that module's globals, so one patch covers the whole HTTP surface.

The module under test has a hyphenated filename (matching this repo's script-invocation
convention: `python3 scripts/sync-openwebui-capabilities.py ...`), so it is not importable via
a normal `import` statement; it is loaded here via `importlib.util` from its file path, exactly
as `sync_test_helpers.py` (this file's shared-fixture module) does.

Run all four split modules: python3 -m unittest discover -s scripts -p 'test_sync_*.py' -v
This module alone: cd scripts && python3 -m unittest test_sync_client -v
"""

import importlib.util
import sys
import unittest
from pathlib import Path

from sync_test_helpers import _FakeOpenWebUI, _RecordingHttp

MODULE_PATH = Path(__file__).resolve().parent / "sync-openwebui-capabilities.py"
_spec = importlib.util.spec_from_file_location("sync_openwebui_capabilities", MODULE_PATH)
sync = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sync
_spec.loader.exec_module(sync)  # type: ignore[union-attr]


class ApplyIsActiveTest(unittest.TestCase):
    """The apply path: create, RE-READ, toggle only if the created row's actual value differs."""

    def _patch(self, http):
        original = sync.client._http_json
        sync.client._http_json = http
        self.addCleanup(lambda: setattr(sync.client, "_http_json", original))

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


class RowEnumerationTest(unittest.TestCase):
    def test_fetch_openwebui_rows_unions_base_and_export(self):
        fake = _FakeOpenWebUI(
            rows=[
                {"id": "model-a", "base_model_id": None, "is_active": True},
                {"id": "model-a--effort-low", "base_model_id": "model-a", "is_active": True},
            ]
        )
        original = sync.client._http_json
        sync.client._http_json = fake
        self.addCleanup(lambda: setattr(sync.client, "_http_json", original))
        rows = sync.fetch_openwebui_rows("http://owui", "k")
        self.assertEqual(sorted(rows), ["model-a", "model-a--effort-low"])
        self.assertEqual(
            [url for _, url, _ in fake.calls],
            ["http://owui/api/v1/models/base", "http://owui/api/v1/models/export"],
        )


if __name__ == "__main__":
    import sys

    if not {"-h", "--help"} & set(sys.argv[1:]):
        from build_id import handle_build_flags

        _build_rc = handle_build_flags(sys.argv[1:], __file__)
        if _build_rc is not None:
            sys.exit(_build_rc)
    unittest.main()
