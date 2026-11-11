"""Shutdown contract (fleet-graceful-shutdown WP-3): SIGTERM exits 0 within the deadline.

Subprocess level, ephemeral port, no model weights: the registry holds one `preload: false`
entry, and the driver blocks `/health` forever so a request is genuinely in flight at SIGTERM, and wedges
`server.shutdown()` so only the watchdog can end the process.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from server import shutdown

ONNX_DIR = Path(__file__).resolve().parents[1]
TEST_DEADLINE_S = 1.0
SLACK_S = 1.5

DRIVER = """
import sys, threading
from server import app, registry
if sys.argv[1] == "block":
    def blocked(self):
        threading.Event().wait()
    registry.ModelRegistry.status = blocked
    # A wedged graceful stop (as a held lock would cause): only the watchdog can end the process.
    app.ThreadingHTTPServer.shutdown = lambda self: threading.Event().wait()
sys.exit(app.main())
"""


def reserve_port() -> tuple[socket.socket, int]:
    """Held, never-listening SO_REUSEADDR reservation (fleet test-ports contract pattern 3a): no other
    process can draw the number via bind(0); the child's SO_REUSEADDR bind shares it. Close it only
    after the child reports listening."""
    r = socket.socket()
    r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    r.bind(("127.0.0.1", 0))
    return r, r.getsockname()[1]


class DeadlineEnvTest(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(shutdown.deadline_from_env({}), shutdown.DEADLINE_S)
        self.assertEqual(shutdown.deadline_from_env({shutdown.DEADLINE_ENV: "0.5"}), 0.5)

    def test_bad_values_are_rejected_not_coerced(self):
        for bad in ("", "abc", "0", "-1", "nan", "inf"):
            with self.assertRaises(ValueError, msg=bad):
                shutdown.deadline_from_env({shutdown.DEADLINE_ENV: bad})


class TriggerTest(unittest.TestCase):
    def test_trigger_is_idempotent_and_arms_watchdog_once(self):
        stops, exits = [], threading.Event()
        sd = shutdown.Shutdown(lambda: stops.append(1), deadline=0.05, exit_fn=lambda c: exits.set())
        sd.trigger()
        sd.trigger()
        self.assertTrue(exits.wait(2))
        time.sleep(0.1)
        self.assertEqual(stops, [1])


class SigtermSubprocessTest(unittest.TestCase):
    def _spawn(self, mode: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        model_dir = Path(tmp.name) / "m"
        model_dir.mkdir()
        cfg = Path(tmp.name) / "models.yaml"
        cfg.write_text(f"models:\n  stub:\n    path: {model_dir}\n    preload: false\n")
        reservation, port = reserve_port()
        self.addCleanup(reservation.close)
        env = dict(os.environ, ONNX_MODELS_CONFIG=str(cfg), ONNX_HOST="127.0.0.1",
                   ONNX_PORT=str(port), **{shutdown.DEADLINE_ENV: str(TEST_DEADLINE_S)})
        proc = subprocess.Popen([sys.executable, "-c", DRIVER, mode], cwd=ONNX_DIR, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.addCleanup(proc.stdout.close)
        for line in proc.stdout:
            if "listening on" in line:
                break
        else:
            self.fail("server never reported listening")
        reservation.close()  # the child now serves; the held number is no longer needed
        return proc, port

    def test_blocked_request_does_not_hold_the_exit_past_the_deadline(self):
        proc, port = self._spawn("block")
        t = threading.Thread(
            target=lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=30).read(),
            daemon=True)
        t.start()
        time.sleep(0.3)  # the request is now parked inside the handler
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=TEST_DEADLINE_S + SLACK_S), 0)
        self.assertGreaterEqual(time.monotonic() - t0, TEST_DEADLINE_S * 0.8)
        self.assertIn("forcing exit", proc.stdout.read())

    def test_idle_server_stops_gracefully_well_before_the_deadline(self):
        proc, _ = self._spawn("idle")
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=TEST_DEADLINE_S + SLACK_S), 0)
        self.assertLess(time.monotonic() - t0, TEST_DEADLINE_S)
        self.assertNotIn("forcing exit", proc.stdout.read())


if __name__ == "__main__":
    import sys

    if not {"-h", "--help"} & set(sys.argv[1:]):
        from server.build_id import handle_build_flags

        _build_rc = handle_build_flags(sys.argv[1:], __file__)
        if _build_rc is not None:
            sys.exit(_build_rc)
    unittest.main()
