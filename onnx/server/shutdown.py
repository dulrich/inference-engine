"""Fleet shutdown contract for a stdlib Python server (PID 1 in its container).

SIGTERM/SIGINT start the graceful stop (`on_stop`, run on a helper thread) AND arm a daemon
watchdog thread that sleeps the deadline and then `os._exit(0)`s, so a hung request, a held lock
or a stuck native call can never hold the process past the deadline. One stderr line is logged at
the signal and one at a forced exit. Exit status is 0 either way.

The signal is noticed through `signal.set_wakeup_fd`: the C-level handler writes to a pipe that a
reader thread blocks on, so the watchdog is armed even while the main thread sits in a native
call (model preload) and cannot run Python-level signal handlers. `install` must run on the main
thread, before anything slow (registry build, preload, bind).
"""

from __future__ import annotations

import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable

DEADLINE_S = 5.0  # under docker's / systemd's 10 s stop budgets
DEADLINE_ENV = "ONNX_SHUTDOWN_DEADLINE_S"  # test seam; unset in production


def deadline_from_env(env: dict[str, str] | os._Environ[str] = os.environ) -> float:
    """The drain deadline: `DEADLINE_S`, or the env seam. A bad value is an error, never coerced."""
    raw = env.get(DEADLINE_ENV)
    if raw is None:
        return DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{DEADLINE_ENV}={raw!r} is not a number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{DEADLINE_ENV}={raw!r} must be a positive finite number of seconds")
    return value


class Shutdown:
    """Idempotent trigger: first call logs, arms the watchdog and starts `on_stop`."""

    def __init__(self, on_stop: Callable[[], None], deadline: float = DEADLINE_S,
                 exit_fn: Callable[[int], None] = os._exit):
        self.deadline = deadline
        self._on_stop = on_stop
        self._exit = exit_fn
        self._lock = threading.Lock()
        self._triggered = threading.Event()

    @property
    def triggered(self) -> bool:
        return self._triggered.is_set()

    def trigger(self, reason: str = "signal") -> None:
        with self._lock:
            if self._triggered.is_set():
                return
            self._triggered.set()
        print(f"[shutdown] {reason}: draining (deadline {self.deadline:g}s)", file=sys.stderr, flush=True)
        threading.Thread(target=self._watchdog, name="shutdown-watchdog", daemon=True).start()
        threading.Thread(target=self._stop, name="shutdown-stop", daemon=True).start()

    def _watchdog(self) -> None:
        time.sleep(self.deadline)
        print(f"[shutdown] drain deadline {self.deadline:g}s elapsed; forcing exit", file=sys.stderr, flush=True)
        self._exit(0)

    def _stop(self) -> None:
        try:
            self._on_stop()
        except Exception as exc:  # the watchdog still bounds the exit
            print(f"[shutdown] graceful stop failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def install(on_stop: Callable[[], None], deadline: float | None = None,
            exit_fn: Callable[[int], None] = os._exit) -> Shutdown:
    """Wire SIGTERM/SIGINT to a `Shutdown`. Main thread only."""
    sd = Shutdown(on_stop, deadline_from_env() if deadline is None else deadline, exit_fn)
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    os.set_blocking(read_fd, True)

    def reader() -> None:
        while True:
            data = os.read(read_fd, 64)
            if not data:
                return
            sd.trigger("signal received")

    threading.Thread(target=reader, name="shutdown-signal", daemon=True).start()
    # A Python-level handler must exist for the C handler to write the wakeup byte; the reader
    # thread does the work, so the handler itself is a no-op.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: None)
    signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
    return sd
