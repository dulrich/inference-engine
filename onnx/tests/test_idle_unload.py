"""Lazy load + idle unload for `preload: false` entries (fleet-memory-crush WP-2).

Stub runtimes and an injected clock: no model weights, no real sleeps. The concurrency test pins
audit item E1 — an in-flight request must never have its runtime dropped under it.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from server import registry as reg
from server.registry import ModelEntry, ModelRegistry, RegistryError


class StubRuntime:
    instances = 0

    def __init__(self, path, intra_op_threads=None):
        StubRuntime.instances += 1
        self.manifest = {}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class IdleUnloadTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(reg.RUNTIME_TYPES, {"gliner2": StubRuntime})
        patcher.start()
        self.addCleanup(patcher.stop)
        StubRuntime.instances = 0
        self.clock = Clock()

    def entry(self, *, preload=False, idle=600):
        return ModelEntry(alias="a", name="a", source_model="", path=Path("/tmp/a"),
                          preload=preload, idle_unload_seconds=idle, clock=self.clock)

    def test_lazy_load_on_first_lease(self):
        e = self.entry()
        self.assertFalse(e.status()["loaded"])
        with e.lease() as rt:
            self.assertIsInstance(rt, StubRuntime)
        self.assertTrue(e.status()["loaded"])
        self.assertEqual(StubRuntime.instances, 1)

    def test_idle_unload_after_window_then_reload(self):
        e = self.entry()
        with e.lease():
            pass
        self.clock.now += 599
        self.assertFalse(e.unload_if_idle())
        self.clock.now += 1
        with mock.patch.object(reg, "_malloc_trim") as trim:
            self.assertTrue(e.unload_if_idle())
            trim.assert_called_once()
        status = e.status()
        self.assertFalse(status["loaded"])
        self.assertEqual(status["unloads"], 1)
        with e.lease():
            pass
        self.assertTrue(e.loaded)
        self.assertEqual(StubRuntime.instances, 2)

    def test_no_unload_while_request_holds_entry(self):
        e = self.entry()
        inside, release = threading.Event(), threading.Event()
        seen = []

        def request():
            with e.lease() as rt:
                inside.set()
                release.wait(5)
                seen.append(rt is e.runtime)

        t = threading.Thread(target=request)
        t.start()
        self.assertTrue(inside.wait(5))
        self.clock.now += 10_000
        self.assertFalse(e.unload_if_idle())
        self.assertEqual(e.status()["in_use"], 1)
        release.set()
        t.join(5)
        self.assertEqual(seen, [True])
        # The idle clock restarted when the request finished, not when it started.
        self.assertFalse(e.unload_if_idle())
        self.clock.now += 600
        self.assertTrue(e.unload_if_idle())

    def test_preload_entry_never_unloaded(self):
        e = self.entry(preload=True)
        e.ensure_loaded()
        self.clock.now += 10**9
        self.assertFalse(e.unload_if_idle())
        self.assertTrue(e.loaded)

    def test_entry_without_window_never_unloaded(self):
        e = self.entry(idle=None)
        with e.lease():
            pass
        self.clock.now += 10**9
        self.assertFalse(e.unload_if_idle())

    def test_registry_sweep_and_readiness(self):
        lazy = self.entry()
        hot = ModelEntry(alias="h", name="h", source_model="", path=Path("/tmp/h"),
                         preload=True, clock=self.clock)
        registry = ModelRegistry({"a": lazy, "h": hot})
        registry.preload_all(log=lambda *_: None)
        self.assertTrue(registry.all_ready())  # a cold lazy alias does not fail readiness
        with registry.lease("a"):
            pass
        self.clock.now += 600
        self.assertEqual(registry.unload_idle(log=lambda *_: None), ["a"])
        self.assertTrue(hot.loaded)
        self.assertTrue(registry.all_ready())


class IdleConfigTest(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(text)
        self.addCleanup(Path(fh.name).unlink)
        return ModelRegistry.from_yaml(fh.name)

    def test_valid_and_default(self):
        r = self.load("models:\n  a:\n    path: /tmp/a\n    preload: false\n"
                      "    idle_unload_seconds: 900\n  b:\n    path: /tmp/b\n")
        self.assertEqual(r.get("a").idle_unload_seconds, 900)
        self.assertIsNone(r.get("b").idle_unload_seconds)

    def test_rejects_non_positive_int(self):
        with self.assertRaises(RegistryError):  # a window on a preloaded model is dead config
            self.load("models:\n  a:\n    path: /tmp/a\n    idle_unload_seconds: 900\n")
        for bad in ('"900"', "true", "0", "-5", "1.5", "null"):
            with self.subTest(value=bad), self.assertRaises(RegistryError) as ctx:
                self.load(f"models:\n  a:\n    path: /tmp/a\n    preload: false\n"
                          f"    idle_unload_seconds: {bad}\n")
            self.assertIn("idle_unload_seconds", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
