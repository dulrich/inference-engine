"""Shared test fakes for the `test_control_dispatcher.py` / `test_control_phases.py` split
(WP-R6b of `plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding
F6 -- file size). Plain module-level helpers only -- no base classes, no decorators, no
fixture-plugin framework. Every helper here is consumed by both `test_control_dispatcher.py`
(the CLI + `results/`/`live.json` writers `run-window.sh` shells out to -- this module's own
retained code, per its docstring's module map) and `test_control_phases.py` (the six phases,
crash recovery, and the action -> effect -> disposition ordering, extracted verbatim from
`control_dispatcher.py` at WP-R4 of `plans/repository-tooling-structural-remediation.md` into
`control_phases.py`); a helper used by only one of those files stays defined beside its sole
consumer instead of living here.

No live Docker/GPU execution anywhere in this suite -- `launch_fn`/`probe_cron_lock_fn`/
`find_pid_fn`/`teardown_fn`/`router_start_fn`/`boot_id_fn`/`pgid_alive_fn`/`pid_alive_fn`/
`now_fn` are always fakes; `dispatch_pass` never falls through to its real (process/Docker-
touching) defaults in any test that imports from this module. The one real seam is
`acquire_cron_lock_fn`: a genuine non-blocking flock, but always on the tempdir's own
`cron.lock`, never the real state dir."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import control_dispatcher as cd  # noqa: E402
from control_store import flock_nonblocking_acquire  # noqa: E402

BOOT_ID = "boot-aaaa-1111"


def _fixed_now(base):
    return lambda: base


def _always_alive(pgid):
    return True


def _never_alive(pgid):
    return False


def _make_paths(tmp):
    paths = cd.Paths(Path(tmp) / "state")
    paths.ensure_dirs()
    return paths


def _write_queue_stem(tmp, stem="ladder-v1"):
    queues_dir = Path(tmp) / "queues"
    queues_dir.mkdir(exist_ok=True)
    (queues_dir / f"{stem}.json").write_text("{}", encoding="utf-8")
    return queues_dir


def _write_request(paths, request_id, action, requested_at="2026-08-24T00:00:00Z", **extra):
    body = {"schema": "control-request-v1", "request_id": request_id, "action": action, "requested_at": requested_at}
    body.update(extra)
    (paths.requests_dir / f"{request_id}.json").write_text(json.dumps(body), encoding="utf-8")
    return body


def _run_dispatch(paths, queues_dir, **overrides):
    kwargs = dict(
        now_fn=_fixed_now(datetime(2026, 8, 24, 0, 5, 0, tzinfo=timezone.utc)),
        boot_id_fn=lambda: BOOT_ID,
        pgid_alive_fn=_never_alive,
        pid_alive_fn=lambda pid, st: False,
        probe_cron_lock_fn=lambda: True,
        launch_fn=lambda req: None,
        find_pid_fn=lambda port, container=None: None,
        teardown_fn=lambda port, container=None: {},
        router_start_fn=lambda name: None,
        acquire_cron_lock_fn=lambda: flock_nonblocking_acquire(paths.cron_lock),
    )
    kwargs.update(overrides)
    return cd.dispatch_pass(paths, queues_dir, **kwargs)


REQ_A = "req-20260824T000000Z-0011223344556677"
REQ_B = "req-20260824T000100Z-1122334455667788"
