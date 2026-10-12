"""The REQUIRED deterministic dispatcher-to-wrapper integration fixture (plans/bakeoff-queue-
control-plane.md WP-3 brief, cycle-1 E3: "a completion requirement, not optional"). Drives the
REAL `control_dispatcher.py` dispatch passes AND the REAL `run-window.sh` script end to end, with
`BAKEOFF_BATCH_CMD` stubbed (a bash script that waits for/creates files as needed, per the brief)
and every docker-facing sub-step stubbed to a no-op -- no GPU, no docker, real processes only via
the stub. Everything lives under one `tempfile.TemporaryDirectory()`, never the real
`~/.local/state/inference-bakeoff/`.

Sequence exercised (verbatim from the brief): request ingested -> claim written -> detached
launch -> wrapper takes lock -> live `phase:"running"` -> dispatcher stop delivery creates the
target-bound marker -> stub batch consumes it -> paused -> wrapper exits -> result
`stage:"exited"` -> dispatcher composes status."""

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import control_dispatcher as cd  # noqa: E402

# Waits for `--stop-marker <path>`'s argv value to appear, unlinks it (mirroring batch.py's own
# stop-marker consumption -- unlink + pause), then exits 0. Bounded by a real wall-clock timeout
# (not `sleep infinity`) so a wiring bug fails the test instead of hanging the suite forever.
STOP_AWARE_BATCH_STUB = """#!/usr/bin/env bash
set -uo pipefail
cmd="$1"; shift
case "$cmd" in
    run)
        marker=""
        while [ $# -gt 0 ]; do
            if [ "$1" = "--stop-marker" ]; then marker="$2"; fi
            shift
        done
        echo "stub: waiting for stop marker at ${marker}" >&2
        deadline=$(( $(date +%s) + 30 ))
        while [ "$(date +%s)" -lt "${deadline}" ]; do
            if [ -n "${marker}" ] && [ -e "${marker}" ]; then
                rm -f "${marker}"
                echo "stub: consumed stop marker, pausing" >&2
                exit 0
            fi
            sleep 0.05
        done
        echo "stub: TIMED OUT waiting for stop marker" >&2
        exit 1
        ;;
    verify)
        exit 0
        ;;
    *)
        exit 2
        ;;
esac
"""


def _poll(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


class TestDispatcherWrapperIntegration(unittest.TestCase):
    def test_start_stop_lifecycle_end_to_end_through_real_wrapper_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            queues_dir = tmp_path / "queues"
            runs_dir = tmp_path / "runs"
            queues_dir.mkdir()
            runs_dir.mkdir()
            (queues_dir / "ladder-v1.json").write_text("{}")

            stub = tmp_path / "stub-batch.sh"
            stub.write_text(STOP_AWARE_BATCH_STUB)
            stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            paths = cd.Paths(state_dir)
            paths.ensure_dirs()

            start_id = "req-20260824T020000Z-0011223344556677"
            stop_id = "req-20260824T020100Z-1122334455667788"

            def _write_request(request_id, action, **extra):
                body = {
                    "schema": "control-request-v1", "request_id": request_id, "action": action,
                    "requested_at": cd.iso_z(datetime.now(UTC)),
                }
                body.update(extra)
                (paths.requests_dir / f"{request_id}.json").write_text(json.dumps(body))

            _write_request(start_id, "start", queue_stem="ladder-v1", minutes=60)

            env_overrides = {
                "BAKEOFF_BATCH_CMD": str(stub),
                "BAKEOFF_ENSURE_SCRATCH_CMD": "true",
                "BAKEOFF_ORPHAN_RECOVER_CMD": "true",
                "BAKEOFF_RUNS_DIR": str(runs_dir),
            }

            # Pass 1: ingest the start request, write the claim, detach-launch the REAL
            # run-window.sh (control_phases.py's real `default_launch` -- the only
            # non-default seam here is the env it hands the child, patched onto THIS process's
            # os.environ so the real launch path's own `dict(os.environ)` copy picks it up).
            with mock.patch.dict(os.environ, env_overrides):
                status1 = cd.dispatch_pass(paths, queues_dir)

            self.assertTrue((paths.claims_dir / f"{start_id}.json").exists())
            self.assertEqual(
                next(r for r in status1["requests"] if r["request_id"] == start_id)["state"],
                "claimed",
            )

            # Wait for the real wrapper process to take the lock and publish live phase:"running".
            live = _poll(lambda: (_read_json(paths.live_path) or {}).get("phase") == "running" and _read_json(paths.live_path))
            self.assertIsNotNone(live, "wrapper never published live.json phase:running in time")
            self.assertEqual(live["request_id"], start_id)
            self.assertEqual(live["invocation_id"], start_id)

            # Now request a stop, and run a SECOND real dispatch pass (full real defaults --
            # this is the dispatcher verifying the REAL process group of the REAL wrapper).
            _write_request(stop_id, "stop")
            cd.dispatch_pass(paths, queues_dir)
            stop_disp = _read_json(paths.dispositions_dir / f"{stop_id}.json")
            self.assertIsNotNone(stop_disp)
            self.assertEqual(stop_disp["state"], "done")
            self.assertEqual(stop_disp["reason"], "stop-delivered")

            # Wait for the stub to consume the marker and the wrapper to run verify + exit.
            final_result = _poll(lambda: (_read_json(paths.results_dir / f"{start_id}.json") or {}).get("stage") == "exited" and _read_json(paths.results_dir / f"{start_id}.json"))
            self.assertIsNotNone(final_result, "wrapper never reached stage:exited in time")
            self.assertEqual(final_result["run_exit"], 0)
            self.assertEqual(final_result["verify_exit"], 0)

            # live.json is cleared by the wrapper's own step 8 -- poll briefly for the tail end
            # of the wrapper process to finish tearing it down.
            _poll(lambda: (not paths.live_path.exists()) or None, timeout=5.0)
            self.assertFalse(paths.live_path.exists())

            # Pass 3: the dispatcher terminalizes the original start request from evidence
            # (stage:"exited" -> done) and composes last-run.json/status.json.
            with mock.patch.dict(os.environ, env_overrides):
                status3 = cd.dispatch_pass(paths, queues_dir)

            start_disp = _read_json(paths.dispositions_dir / f"{start_id}.json")
            self.assertIsNotNone(start_disp)
            self.assertEqual(start_disp["state"], "done")

            last_run = _read_json(paths.last_run_path)
            self.assertIsNotNone(last_run)
            self.assertEqual(last_run["invocation_id"], start_id)
            self.assertEqual(last_run["disposition"], "clean")

            wire_states = {r["request_id"]: r["state"] for r in status3["requests"]}
            self.assertEqual(wire_states[start_id], "done")
            self.assertEqual(wire_states[stop_id], "done")


if __name__ == "__main__":
    unittest.main()
