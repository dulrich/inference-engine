"""`inference-engine-onnx` — the loopback HTTP surface over the GLiNER2-family ONNX runtime.

Stdlib `ThreadingHTTPServer` on purpose. The dependency list is the security and reproducibility
story of this image (onnxruntime + numpy + tokenizers + PyYAML and nothing else), and a web
framework would add a transitive tree for a service with three routes. `crucible-search`'s
companion made the same call for the same reason.

Routes
------
  GET  /health      per-model readiness: {"ok": bool, "models": {alias: {...}}}
  GET  /v1/models   the registry, as served
  POST /v1/extract  {"model": alias, "texts": [...], "schema": {...}, "threshold": 0.5}
                    -> {"model": alias, "results": [<gliner2-native result per text>]}

Error discipline (this is a contract, not a preference)
-------------------------------------------------------
An unknown alias returns **404 with a JSON body**, never a silent fallback to some default
model and never a 5xx. Consumers map 5xx to "the service is down" and retry/alert; a typo in a
model name is a client bug and must read as one. Same for a malformed schema (400). 5xx is
reserved for "this service is broken".

Everything a caller can get wrong is answered by a body of the shape
`{"error": {"code": "...", "message": "...", ...}}` — probe the body, never the status code
alone (the fleet has been burned by HTTP-200 error payloads; see `llama/AGENTS.md`).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .registry import ModelRegistry, UnknownModelError
from .runtime import PreparedSchema, SchemaError

DEFAULT_CONFIG = "/app/models.yaml"
DEFAULT_HOST = "0.0.0.0"  # container-internal; the loopback bind is the compose publish
DEFAULT_PORT = 4812
MAX_BODY_BYTES = 32 * 1024 * 1024  # a 19k-row batch job posts big bodies; 32 MiB is generous
SCHEMA_CACHE_SIZE = 64


def _log(*parts: object) -> None:
    print(f"[onnx] {' '.join(str(p) for p in parts)}", flush=True)


class SchemaCache:
    """(alias, schema-hash) -> PreparedSchema, bounded LRU. Thread-safe."""

    def __init__(self, maxsize: int = SCHEMA_CACHE_SIZE):
        self._data: OrderedDict[tuple[str, str], PreparedSchema] = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(alias: str, schema: dict[str, Any]) -> tuple[str, str]:
        # sort_keys=False: label ORDER is part of the prompt, so two schemas that differ only in
        # ordering are genuinely different schemas and must not share a cache entry.
        blob = json.dumps(schema, sort_keys=False, separators=(",", ":"), default=str)
        return alias, hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get_or_build(self, alias: str, schema: dict[str, Any], build) -> PreparedSchema:
        cache_key = self.key(alias, schema)
        with self._lock:
            hit = self._data.get(cache_key)
            if hit is not None:
                self._data.move_to_end(cache_key)
                self.hits += 1
                return hit
            self.misses += 1
        prepared = build()  # built outside the lock: validation should not serialize requests
        with self._lock:
            self._data[cache_key] = prepared
            self._data.move_to_end(cache_key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
        return prepared

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._data), "hits": self.hits, "misses": self.misses}


class ExtractHandler(BaseHTTPRequestHandler):
    server_version = "inference-engine-onnx"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    registry: ModelRegistry
    schema_cache: SchemaCache
    started_at: float

    # ── plumbing ────────────────────────────────────────────────────────────────────────────

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib hook name
        _log("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, err_code: str, message: str, **extra: Any) -> None:
        self._send(code, {"error": {"code": err_code, "message": message, **extra}})

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "bad_request", "Content-Length is not an integer")
            return None
        if length <= 0:
            self._error(400, "bad_request", "empty request body")
            return None
        if length > MAX_BODY_BYTES:
            self._error(413, "payload_too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
            return None
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._error(400, "bad_json", f"request body is not valid JSON: {exc}")
            return None
        if not isinstance(parsed, dict):
            self._error(400, "bad_request", "request body must be a JSON object")
            return None
        return parsed

    # ── routes ──────────────────────────────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            status = self.registry.status()
            self._send(200, {
                "ok": self.registry.all_ready(),
                "service": "inference-engine-onnx",
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "schema_cache": self.schema_cache.stats(),
                "models": status,
            })
        elif path == "/v1/models":
            self._send(200, {
                "object": "list",
                "data": [
                    {"id": alias, **info} for alias, info in self.registry.status().items()
                ],
            })
        elif path == "/":
            self._send(200, {
                "service": "inference-engine-onnx",
                "models": self.registry.aliases,
                "routes": ["GET /health", "GET /v1/models", "POST /v1/extract"],
            })
        else:
            self._error(404, "not_found", f"no such route: GET {path}")

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/v1/extract":
            self._error(404, "not_found", f"no such route: POST {path}")
            return

        payload = self._read_json()
        if payload is None:
            return

        alias = payload.get("model")
        if not isinstance(alias, str) or not alias:
            self._error(400, "missing_model",
                        "request must name a model alias in 'model'",
                        available=self.registry.aliases)
            return
        if alias not in self.registry:
            # 404, deliberately: not a 5xx (that means "service broken") and never a fallback
            # to a default model (that would silently store features from the wrong model).
            self._error(404, "unknown_model",
                        f"unknown model alias '{alias}'",
                        available=self.registry.aliases)
            return

        texts = payload.get("texts")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts:
            self._error(400, "bad_texts", "'texts' must be a non-empty array of strings")
            return
        if not all(isinstance(t, str) for t in texts):
            self._error(400, "bad_texts", "every element of 'texts' must be a string")
            return

        schema = payload.get("schema")
        if not isinstance(schema, dict):
            self._error(400, "bad_schema", "'schema' must be an object with 'entities' "
                                           "and/or 'classifications'")
            return

        threshold = payload.get("threshold", 0.5)
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            self._error(400, "bad_threshold", "'threshold' must be a number")
            return

        started = time.time()
        try:
            runtime = self.registry.runtime_for(alias)
        except UnknownModelError:  # pragma: no cover - guarded above
            self._error(404, "unknown_model", f"unknown model alias '{alias}'")
            return
        except Exception as exc:
            self._error(503, "model_unavailable",
                        f"model '{alias}' could not be loaded: {type(exc).__name__}: {exc}")
            return

        try:
            prepared = self.schema_cache.get_or_build(
                alias, schema, lambda: runtime.prepare(schema)
            )
        except SchemaError as exc:
            self._error(400, "bad_schema", str(exc))
            return

        try:
            results = runtime.extract(texts, prepared, float(threshold))
        except Exception as exc:  # a genuine server fault — 5xx is correct here
            _log("extract failed:", traceback.format_exc())
            self._error(500, "extract_failed", f"{type(exc).__name__}: {exc}")
            return

        elapsed = time.time() - started
        _log(f"extract model={alias} texts={len(texts)} in {elapsed:.2f}s "
             f"({elapsed / len(texts):.3f}s/text)")
        self._send(200, {"model": alias, "results": results})


def build_server(config_path: str, host: str, port: int, threads: int | None) -> ThreadingHTTPServer:
    registry = ModelRegistry.from_yaml(config_path, intra_op_threads=threads)
    _log(f"registry: {config_path} -> {registry.aliases}")
    registry.preload_all(log=_log)

    handler = type("BoundExtractHandler", (ExtractHandler,), {
        "registry": registry,
        "schema_cache": SchemaCache(),
        "started_at": time.time(),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> int:
    config_path = os.environ.get("ONNX_MODELS_CONFIG", DEFAULT_CONFIG)
    host = os.environ.get("ONNX_HOST", DEFAULT_HOST)
    port = int(os.environ.get("ONNX_PORT", str(DEFAULT_PORT)))
    threads_env = os.environ.get("ONNX_INTRA_OP_THREADS")
    threads = int(threads_env) if threads_env else None

    server = build_server(config_path, host, port, threads)
    _log(f"listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
