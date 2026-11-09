"""`server.registry.ModelRegistry.from_yaml` — the config boundary.

`models.yaml` is trusted-ish input, but `str()`/`bool()` coercion turns a typo into a mystery:
`path: true` used to become `Path("True")` and surface much later as "missing directory", and a
quoted `preload: "false"` used to preload anyway because `bool("false")` is True. Both must be
config errors, raised at load, naming the alias and the key.

Note this module imports `server.registry`, which pulls in `server.runtime` (numpy,
onnxruntime, tokenizers) — unlike `test_schema.py`, it needs the gate venv.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.registry import ModelRegistry, RegistryError

VALID = """
models:
  gliner2-base:
    name: GLiNER2 base
    source_model: fastino/gliner2-base-v1
    path: /models/fastino/gliner2-base-v1-onnx
    preload: true
    notes: launch model
"""


class RegistryYamlTestCase(unittest.TestCase):
    def load(self, text: str) -> ModelRegistry:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "models.yaml"
            config.write_text(text, encoding="utf-8")
            return ModelRegistry.from_yaml(config)

    def reject(self, text: str, *, mentions: str) -> None:
        with self.assertRaises(RegistryError) as ctx:
            self.load(text)
        self.assertIn(mentions, str(ctx.exception))


class TestValidRegistry(RegistryYamlTestCase):
    def test_round_trip(self):
        registry = self.load(VALID)
        self.assertEqual(registry.aliases, ["gliner2-base"])
        entry = registry.get("gliner2-base")
        self.assertEqual(entry.name, "GLiNER2 base")
        self.assertEqual(entry.source_model, "fastino/gliner2-base-v1")
        self.assertEqual(entry.path, Path("/models/fastino/gliner2-base-v1-onnx"))
        self.assertIs(entry.preload, True)
        self.assertEqual(entry.notes, "launch model")

    def test_optional_keys_default(self):
        registry = self.load("models:\n  a:\n    path: /tmp/a\n")
        entry = registry.get("a")
        self.assertEqual(entry.name, "a")
        self.assertEqual(entry.source_model, "")
        self.assertEqual(entry.notes, "")
        self.assertIs(entry.preload, True)

    def test_preload_false_is_honoured(self):
        registry = self.load("models:\n  a:\n    path: /tmp/a\n    preload: false\n")
        self.assertIs(registry.get("a").preload, False)


class TestMalformedRegistry(RegistryYamlTestCase):
    def test_quoted_bool_preload(self):
        """YAML `"false"` is a string; `bool("false")` is True. That must not preload."""
        self.reject("models:\n  a:\n    path: /tmp/a\n    preload: \"false\"\n", mentions="preload")

    def test_preload_int(self):
        self.reject("models:\n  a:\n    path: /tmp/a\n    preload: 1\n", mentions="preload")

    def test_preload_null(self):
        self.reject("models:\n  a:\n    path: /tmp/a\n    preload: null\n", mentions="preload")

    def test_path_true(self):
        """`Path(str(True))` is `Path("True")` — a config error dressed up as a missing dir."""
        self.reject("models:\n  a:\n    path: true\n", mentions="path")

    def test_path_missing(self):
        self.reject("models:\n  a:\n    name: a\n", mentions="path")

    def test_path_empty(self):
        self.reject("models:\n  a:\n    path: \"\"\n", mentions="path")

    def test_path_list(self):
        self.reject("models:\n  a:\n    path: [/tmp/a]\n", mentions="path")

    def test_name_not_a_string(self):
        self.reject("models:\n  a:\n    path: /tmp/a\n    name: 7\n", mentions="name")

    def test_source_model_not_a_string(self):
        self.reject(
            "models:\n  a:\n    path: /tmp/a\n    source_model: null\n", mentions="source_model"
        )

    def test_notes_not_a_string(self):
        self.reject("models:\n  a:\n    path: /tmp/a\n    notes: false\n", mentions="notes")

    def test_alias_not_a_string(self):
        self.reject("models:\n  7:\n    path: /tmp/a\n", mentions="alias")

    def test_entry_not_a_mapping(self):
        self.reject("models:\n  a: /tmp/a\n", mentions="mapping")

    def test_no_models_mapping(self):
        self.reject("models: []\n", mentions="no 'models' mapping")

    def test_missing_file(self):
        with self.assertRaises(RegistryError):
            ModelRegistry.from_yaml("/nonexistent/models.yaml")


if __name__ == "__main__":
    unittest.main()
