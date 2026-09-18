"""Shared, explicit fixtures for the `sync-openwebui-capabilities.py` test suite (WP-R6a).

This module carries no `TestCase`s and no `unittest.main()` sentinel -- it is not itself an
executable test module, just the small set of builder functions and fixture-only HTTP doubles
that more than one of `test_sync_declarations.py` / `test_sync_planner.py` /
`test_sync_client.py` / `test_sync_cli.py` needs. Deliberately NOT named `test_*.py`, so
`unittest discover`'s default pattern never mistakes it for a test module.

Two of the builders below (`_policy`, `_vis_doc`) need the coordinator's re-exported schema
constants, so this module loads `sync-openwebui-capabilities.py` itself exactly the way every
test module does (see each test module's own docstring for why: the hyphenated filename is not
importable via a normal `import` statement). Loading it again here is safe and cheap: the real
`openwebui_sync` package underneath is import-cached process-wide (a true singleton), so every
copy of `sync.client` -- whichever module loaded it -- is the same object, which is what makes
patching `sync.client._http_json` from any test module affect every other one.
"""

import importlib.util
import json
import sys
import tempfile
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


class _RecordingHttp:
    """Stands in for `sync.client._http_json`; records every call and replays scripted responses.

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
