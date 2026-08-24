"""`run_harness.py`'s own tests: `tokenize_count` (plan `bakeoff-transport-taxonomy-and-
prompt-fit.md` WP-2, Change 1) -- stdlib-urllib transport, body-validated per the fleet's
"probe by response body, never status code" rule (root `AGENTS.md` / `llama/AGENTS.md`). No
live network anywhere in this suite: `urllib.request.urlopen` is monkeypatched to an injected
fake for every test, restored in `tearDown`, matching this repo's established
`run_harness._execute_streaming_request = fake_execute` idiom (see `test_run_cell.py`'s
`TestLiveEntryPoint`) at the transport layer one level down."""

import json
import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import run_harness  # noqa: E402


class _FakeResponse:
    """Minimal stand-in for `http.client.HTTPResponse` as used via the `with urlopen(...) as
    resp:` context-manager protocol -- only `.read()` is exercised by `tokenize_count`."""

    def __init__(self, body_bytes: bytes):
        self._body = body_bytes

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class TestTokenizeCount(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = run_harness.urllib.request.urlopen

    def tearDown(self):
        run_harness.urllib.request.urlopen = self._orig_urlopen

    def _stub_urlopen(self, body_obj=None, raw_body=None, raise_exc=None):
        """Installs a fake `urllib.request.urlopen` and returns a dict this test can inspect
        for the exact request `tokenize_count` sent (url/body/timeout)."""
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["content_type"] = req.get_header("Content-type")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            if raise_exc is not None:
                raise raise_exc
            body = raw_body if raw_body is not None else json.dumps(body_obj).encode("utf-8")
            return _FakeResponse(body)

        run_harness.urllib.request.urlopen = fake_urlopen
        return captured

    def test_valid_tokens_body_returns_length(self):
        captured = self._stub_urlopen(body_obj={"tokens": [1, 2, 3, 4, 5]})
        count = run_harness.tokenize_count("http://fake-bridge:8899", "hello world")
        self.assertEqual(count, 5)
        self.assertEqual(captured["url"], "http://fake-bridge:8899/tokenize")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], {"content": "hello world"})

    def test_timeout_forwarded_to_urlopen(self):
        captured = self._stub_urlopen(body_obj={"tokens": [1]})
        run_harness.tokenize_count("http://fake-bridge:8899", "x", timeout=12.5)
        self.assertEqual(captured["timeout"], 12.5)

    def test_empty_tokens_list_returns_zero(self):
        self._stub_urlopen(body_obj={"tokens": []})
        count = run_harness.tokenize_count("http://fake-bridge:8899", "")
        self.assertEqual(count, 0)

    def test_wrong_shape_200_raises(self):
        # The exact "guard passes and the thing is wrong" shape this repo has been burned by
        # before (root AGENTS.md Quirks) -- a 200 whose body doesn't carry a `tokens` list must
        # never be read back as any guessed token count.
        self._stub_urlopen(body_obj={"unexpected": "shape"})
        with self.assertRaises(RuntimeError):
            run_harness.tokenize_count("http://fake-bridge:8899", "hello world")

    def test_non_list_tokens_field_raises(self):
        self._stub_urlopen(body_obj={"tokens": "not-a-list"})
        with self.assertRaises(RuntimeError):
            run_harness.tokenize_count("http://fake-bridge:8899", "hello world")

    def test_non_dict_body_raises(self):
        self._stub_urlopen(raw_body=b"[1, 2, 3]")
        with self.assertRaises(RuntimeError):
            run_harness.tokenize_count("http://fake-bridge:8899", "hello world")

    def test_invalid_json_raises(self):
        self._stub_urlopen(raw_body=b"not json at all")
        with self.assertRaises(RuntimeError):
            run_harness.tokenize_count("http://fake-bridge:8899", "hello world")

    def test_transport_error_raises_runtime_error_not_the_raw_urllib_exception(self):
        self._stub_urlopen(raise_exc=run_harness.urllib.error.URLError("connection refused"))
        with self.assertRaises(RuntimeError):
            run_harness.tokenize_count("http://fake-bridge:8899", "hello world")


if __name__ == "__main__":
    unittest.main()
