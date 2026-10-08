"""`SeqClsOnnxRuntime`, registry `type` dispatch, and the /v1/classify request boundary.

Everything runs against a tiny synthetic bundle built in-test: a WordLevel tokenizer over
{a, b, c} and a graph `input_ids -> Gather(embedding) -> ReduceMean -> logits`, where token `a`
votes 10 for label 0, `b` for label 1, `c` for label 2. Needs the gate venv (numpy,
onnxruntime, tokenizers, onnx).
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from server.app import ExtractHandler, SchemaCache
from server.registry import ModelRegistry, RegistryError
from server.runtime_seqcls import BundleError, SeqClsOnnxRuntime
from server.schema import RequestError, check_model_type, compile_classify_texts

LABELS = ["alpha", "beta", "gamma"]


def write_bundle(directory: Path, **manifest_overrides) -> Path:
    vocab = {"[UNK]": 0, "a": 1, "b": 2, "c": 3}
    tok = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = Whitespace()
    tok.save(str(directory / "tokenizer.json"))

    table = np.zeros((4, 3), dtype=np.float32)
    table[1, 0] = table[2, 1] = table[3, 2] = 10.0
    graph = helper.make_graph(
        [
            helper.make_node("Gather", ["table", "input_ids"], ["emb"], axis=0),
            helper.make_node("ReduceMean", ["emb"], ["logits"], axes=[1], keepdims=0),
        ],
        "tiny_seqcls",
        [
            helper.make_tensor_value_info("input_ids", TensorProto.INT64, [1, None]),
            helper.make_tensor_value_info("attention_mask", TensorProto.INT64, [1, None]),
        ],
        [helper.make_tensor_value_info("logits", TensorProto.FLOAT, [1, 3])],
        initializer=[numpy_helper.from_array(table, "table")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    onnx.save(model, str(directory / "model.onnx"))

    manifest = {
        "format": "hsc-seqcls/1",
        "labels": LABELS,
        "max_len": 8,
        "truncation_side": "left",
        "temperature": 2.0,
    }
    manifest.update(manifest_overrides)
    manifest = {k: v for k, v in manifest.items() if v is not None}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


class BundleTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def bundle(self, name="b", **overrides) -> Path:
        d = self.tmp / name
        d.mkdir()
        return write_bundle(d, **overrides)


class TestSeqClsRuntime(BundleTestCase):
    def test_softmax_with_temperature_and_label_order(self):
        rt = SeqClsOnnxRuntime(self.bundle())
        (res,) = rt.classify(["a"])
        self.assertEqual(list(res["probabilities"]), LABELS)
        exp = np.exp(np.array([10.0, 0.0, 0.0]) / 2.0)
        want = exp / exp.sum()
        for lab, p in zip(LABELS, want):
            self.assertAlmostEqual(res["probabilities"][lab], p, places=6)
        self.assertEqual(res["label"], "alpha")
        self.assertAlmostEqual(res["confidence"], want[0], places=6)
        self.assertTrue(math.isclose(sum(res["probabilities"].values()), 1.0, rel_tol=1e-9))

    def test_label_follows_argmax(self):
        rt = SeqClsOnnxRuntime(self.bundle())
        labels = [r["label"] for r in rt.classify(["b", "c c a", "a b b"])]
        self.assertEqual(labels, ["beta", "gamma", "beta"])

    def test_left_truncation_keeps_the_tail(self):
        rt = SeqClsOnnxRuntime(self.bundle(max_len=1))
        self.assertEqual(rt.classify(["a b c"])[0]["label"], "gamma")

    def test_right_truncation_keeps_the_head(self):
        rt = SeqClsOnnxRuntime(self.bundle(max_len=1, truncation_side="right"))
        self.assertEqual(rt.classify(["a b c"])[0]["label"], "alpha")

    def test_unknown_or_missing_format_refused(self):
        with self.assertRaises(BundleError):
            SeqClsOnnxRuntime(self.bundle("x", format="hsc-seqcls/2"))
        with self.assertRaises(BundleError):
            SeqClsOnnxRuntime(self.bundle("y", format=None))


def registry_yaml(tmp: Path, body: str) -> Path:
    config = tmp / "models.yaml"
    config.write_text("models:\n" + body, encoding="utf-8")
    return config


class TestRegistryType(BundleTestCase):
    def test_type_defaults_to_gliner2_and_accepts_seqcls(self):
        config = registry_yaml(self.tmp, (
            "  g:\n    path: /nonexistent\n"
            "  s:\n    type: seqcls\n    path: /nonexistent\n"
        ))
        registry = ModelRegistry.from_yaml(config)
        self.assertEqual(registry.get("g").type, "gliner2")
        self.assertEqual(registry.get("s").type, "seqcls")

    def test_bad_type_is_a_registry_error(self):
        for bad in ("bert", "true", "[seqcls]"):
            config = registry_yaml(self.tmp, f"  m:\n    type: {bad}\n    path: /x\n")
            with self.assertRaises(RegistryError) as ctx:
                ModelRegistry.from_yaml(config)
            self.assertIn("'type'", str(ctx.exception))

    def test_seqcls_status_reports_manifest_fields_and_load_errors(self):
        good = self.bundle()
        bad = self.bundle("bad", format="nope")
        config = registry_yaml(self.tmp, (
            f"  s:\n    type: seqcls\n    path: {good}\n"
            f"  t:\n    type: seqcls\n    path: {bad}\n"
        ))
        registry = ModelRegistry.from_yaml(config)
        registry.preload_all(log=lambda *_: None)
        status = registry.status()
        self.assertEqual(status["s"]["type"], "seqcls")
        self.assertEqual(status["s"]["format"], "hsc-seqcls/1")
        self.assertEqual(status["s"]["labels"], LABELS)
        self.assertEqual(status["s"]["max_len"], 8)
        self.assertNotIn("max_width", status["s"])
        self.assertIn("unsupported bundle format", status["t"]["error"])
        self.assertFalse(registry.all_ready())


class TestClassifyBoundary(unittest.TestCase):
    def test_wrong_type_names_the_type(self):
        with self.assertRaises(RequestError) as ctx:
            check_model_type("/v1/extract", "hsc", "seqcls")
        self.assertEqual(ctx.exception.code, "wrong_model_type")
        self.assertIn("'seqcls'", str(ctx.exception))
        with self.assertRaises(RequestError) as ctx:
            check_model_type("/v1/classify", "g", "gliner2")
        self.assertIn("'gliner2'", str(ctx.exception))
        check_model_type("/v1/classify", "hsc", "seqcls")
        check_model_type("/v1/extract", "g", "gliner2")

    def test_texts_validation(self):
        self.assertEqual(compile_classify_texts(["x", ""]), ["x", ""])
        for bad in ("bare string", [], None, ["ok", 3], ["t"] * 33):
            with self.assertRaises(RequestError) as ctx:
                compile_classify_texts(bad)
            self.assertEqual(ctx.exception.code, "bad_texts")


class TestClassifyHttp(BundleTestCase):
    """End to end through the real handler on an ephemeral loopback port."""

    def test_routes(self):
        config = registry_yaml(self.tmp, (
            f"  s:\n    type: seqcls\n    path: {self.bundle()}\n"
            "  g:\n    path: /nonexistent\n    preload: false\n"
        ))
        registry = ModelRegistry.from_yaml(config)
        registry.preload_all(log=lambda *_: None)
        handler = type("H", (ExtractHandler,), {
            "registry": registry, "schema_cache": SchemaCache(), "started_at": 0.0,
            "log_message": lambda *_: None,
        })
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def post(route, body):
            req = urllib.request.Request(base + route, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        code, body = post("/v1/classify", {"model": "s", "texts": ["a", "c"]})
        self.assertEqual(code, 200)
        self.assertEqual([r["label"] for r in body["results"]], ["alpha", "gamma"])

        code, body = post("/v1/extract", {"model": "s", "texts": ["a"], "schema": {}})
        self.assertEqual((code, body["error"]["code"]), (400, "wrong_model_type"))
        code, body = post("/v1/classify", {"model": "g", "texts": ["a"]})
        self.assertEqual((code, body["error"]["code"]), (400, "wrong_model_type"))
        code, body = post("/v1/classify", {"model": "nope", "texts": ["a"]})
        self.assertEqual((code, body["error"]["code"]), (404, "unknown_model"))
        code, body = post("/v1/classify", {"texts": ["a"]})
        self.assertEqual((code, body["error"]["code"]), (400, "missing_model"))
        code, body = post("/v1/classify", {"model": "s", "texts": "a"})
        self.assertEqual((code, body["error"]["code"]), (400, "bad_texts"))


if __name__ == "__main__":
    unittest.main()
