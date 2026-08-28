#!/usr/bin/env python3
"""Scratch-server process lifecycle: spawn inside the llama container, discover its bridge IP,
poll health by response body, verify the startup state from the server's own log, and always
tear it down (WP-R3 of `plans/repository-tooling-structural-remediation.md`; review finding F8).

Extracted verbatim from `sweep.py`, which was carrying this process-orchestration layer in the
same file as the pure fit math (`gguf_fit.py`) and the spec/CLI composition (`sweep.py`). Every
symbol keeps its original name, signature, and behavior, and `sweep.py` re-exports all of them,
so `sweep.scratch_server`/`sweep.ScratchServerHandle`/`sweep.find_host_pid_by_port`/
`sweep.teardown_scratch_server`/`sweep.DEFAULT_CONTAINER_NAME` all still resolve for
`batch.py`, `cell_runner.py`, `control_dispatcher.py`, and the test suite.

**This is the only module here that touches Docker, and only when actually called.** Nothing in
`sweep.py --dry-run` reaches it, and the test suite never enters `scratch_server()` against a
live container -- every collaborator (`spawn_scratch_server`, `discover_bridge_ip`,
`wait_for_health`, `find_host_pid_by_port`, `teardown_scratch_server`, `read_vram_used_bytes`,
`subprocess.run`) is patched out at this module's namespace.

Container facts baked in (verified live 2026-08-23, the originating WP's brief):
  - `inference-engine-llama` has no `python3`, `kill`, or `pkill` inside the image -- scratch
    servers are spawned via `docker exec ... sh -c 'nohup ... & echo $!'` (POSIX `sh`/`nohup` are
    both present, ordinary coreutils/dash) and torn down from the HOST side, by signalling the
    host-visible PID `docker top` reports (see `find_host_pid_by_port()` /
    `teardown_scratch_server()`) -- never by asking the container to kill its own child.
  - Only the router's 4806(container)->4811(host) port is published. A scratch server's port is
    container-internal only, reachable from the host via the container's bridge IP
    (`discover_bridge_ip()`), never via `127.0.0.1:<port>`.

Zero new runtime dependencies: Python 3 stdlib only (`subprocess`, `urllib`, `json`, `re`, `os`,
`shlex`, `signal`, `time`, `datetime`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# `compute_cell_id`/`build_command` come from the plain-dict cell layer, and
# `read_vram_used_bytes` from the pure VRAM/fit layer -- imported as module globals (not
# `module.attr` lookups) precisely so a test can patch them on THIS module's namespace, which is
# what `tests/test_sweep.py` does. Neither import is circular: `sweep.py` imports this module,
# never the other way round.
from cell_spec import build_command, compute_cell_id
from gguf_fit import read_vram_used_bytes

# Container facts (brief, verified live 2026-08-23).
DEFAULT_CONTAINER_NAME = "inference-engine-llama"


# -------------------------------------------------------------------------------------------
# Scratch-server lifecycle -- docker exec + internal nohup, bridge-IP discovery, health poll by
# response body, teardown via host-visible PID from `docker top`, VRAM read-back. NONE of these
# are called by `--dry-run`; only the `run` subcommand touches Docker (brief: "No live
# Docker/GPU execution" for this WP -- the orchestrator exercises this live, post-merge).
# -------------------------------------------------------------------------------------------


def discover_bridge_ip(container: str = DEFAULT_CONTAINER_NAME) -> str:
    """Container-internal bridge IP for `container`, via `docker inspect` -- scratch-server ports
    are container-internal only (brief: "only 4806->4811 is published; scratch ports are
    container-internal, reachable from the host via the bridge IP")."""
    fmt = "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"
    result = subprocess.run(
        ["docker", "inspect", "-f", fmt, container],
        capture_output=True, text=True, check=True,
    )
    ip = result.stdout.strip()
    if not ip:
        raise RuntimeError(f"docker inspect returned no bridge IP for container {container!r}")
    return ip


def spawn_scratch_server(
    argv: List[str],
    log_path: str,
    container: str = DEFAULT_CONTAINER_NAME,
) -> None:
    """Spawns `argv` (from `build_command()`) inside `container` via `docker exec` + an internal
    `nohup ... &`, redirecting stdout/stderr to `log_path` (a path inside the container -- the
    per-cell startup-state verification log). Does not itself wait for health or capture a PID:
    correlate the running process afterward via `find_host_pid_by_port()`, which is robust to
    `docker exec`'s own transient shell PID being irrelevant (no `python3`/`kill`/`pkill` in the
    image, so a container-internal `echo $!` PID is not something we could signal from the host
    reliably anyway -- `docker top`'s host-visible PIDs are the mechanism this module commits to).
    """
    inner = " ".join(shlex.quote(a) for a in argv)
    shell_cmd = f"nohup {inner} > {shlex.quote(log_path)} 2>&1 < /dev/null &"
    subprocess.run(
        ["docker", "exec", "-d", container, "sh", "-c", shell_cmd],
        check=True,
    )


def find_host_pid_by_port(port: int, container: str = DEFAULT_CONTAINER_NAME) -> Optional[int]:
    """Finds the HOST-visible PID of the `llama-server --port <port>` process running inside
    `container`, via `docker top` (which reports real host PIDs, not container-namespace ones --
    this is what makes host-side `os.kill()` teardown possible without any kill/pkill binary
    inside the image). Returns `None` if no matching process is found (already exited, or never
    started)."""
    result = subprocess.run(
        ["docker", "top", container, "-eo", "pid,args"],
        capture_output=True, text=True, check=True,
    )
    needle = f"--port {port}"
    for line in result.stdout.splitlines()[1:]:  # skip header row
        line = line.strip()
        if not line or needle not in line:
            continue
        pid_str = line.split(None, 1)[0]
        try:
            return int(pid_str)
        except ValueError:
            continue
    return None


def wait_for_health(base_url: str, timeout: float = 120.0, poll_interval: float = 0.5) -> bool:
    """Polls `GET {base_url}/health` by RESPONSE BODY, never by HTTP status code alone (root
    `AGENTS.md` Quirks: "Probe by response body, never status code" -- a healthy-looking response
    with the wrong body is exactly the failure family this fleet has been burned by repeatedly).
    llama-server's own `/health` returns a JSON body with a `status` field; this treats
    `status == "ok"` as ready and anything else (including a non-JSON body, a connection refused
    while the process is still starting, or a transport error) as not-yet-ready and keeps polling
    until `timeout`. Returns `True`/`False`; never raises on a slow-starting server.
    """
    deadline = time.monotonic() + timeout
    url = f"{base_url}/health"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if body.get("status") == "ok":
                return True
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            pass
        time.sleep(poll_interval)
    return False


def teardown_scratch_server(
    port: int,
    container: str = DEFAULT_CONTAINER_NAME,
    grace_seconds: float = 5.0,
) -> Dict[str, Any]:
    """Finds the host-visible PID for `port` (via `find_host_pid_by_port`) and signals it
    directly from the host (`os.kill` -- SIGTERM, then SIGKILL if it's still alive after
    `grace_seconds`). Never asks the container to kill its own child (no `kill`/`pkill` inside the
    image). Returns a dict describing what happened, including a pre/post VRAM read-back
    (`read_vram_used_bytes()`) so the caller can confirm VRAM actually returned to baseline, not
    merely that the process is gone.
    """
    vram_before = read_vram_used_bytes()
    pid = find_host_pid_by_port(port, container=container)
    if pid is None:
        return {
            "found_pid": None, "signalled": False,
            "vram_before_bytes": vram_before, "vram_after_bytes": vram_before,
        }
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    alive = True
    while time.monotonic() < deadline:
        if find_host_pid_by_port(port, container=container) is None:
            alive = False
            break
        time.sleep(0.25)
    if alive:
        os.kill(pid, signal.SIGKILL)
    vram_after = read_vram_used_bytes()
    return {
        "found_pid": pid, "signalled": True, "force_killed": alive,
        "vram_before_bytes": vram_before, "vram_after_bytes": vram_after,
    }


# -------------------------------------------------------------------------------------------
# Per-cell startup-state verification -- "a healthy endpoint has told you nothing" (llama/
# AGENTS.md). Before a cell's measurements count, assert from its own redirected stdout/stderr
# log that the requested model path, -c, -ctk/-ctv, flash-attention state, and offload placement
# were ACTUALLY activated, not merely requested. A mismatch (or a log too sparse to check, e.g.
# because -v wasn't passed) marks the cell `invalid_config`, never silently measured.
#
# These patterns are written against llama-server build b10534's known console format (per
# docs/qwen38-27b-bakeoff-results.md:174-176: "-v does make llama-server itself emit the
# type_k/type_v/flash_attn lines"). They are NOT live-verified by this WP (no live Docker/GPU
# execution) -- the orchestrator's one live smoke cell (plan Test Plan) is where a pattern
# mismatch would first surface, and a spurious `invalid_config` from a drifted log format is the
# intended, safe failure direction (never a silent pass).
# -------------------------------------------------------------------------------------------

# Patterns verified LIVE against build b10534's actual console output (orchestrator, WP-1 live
# smoke cell, 2026-08-23; scratch log retained in-container as /tmp/sweep-cell-<cell_id>.log).
# The load-bearing lines at `-lv 9` are:
#   load_model: loading model '/models/.../file.gguf'
#   llama_context: n_ctx                 = 8192
#   llama_context: flash_attn            = enabled
#   load_tensors: offloaded 25/25 layers to GPU
#   llama_kv_cache: size = 54.00 MiB (8192 cells, 6 layers, ...), K (f16): ..., V (f16): ...
# NONE of the llama_context/llama_kv_cache lines print at llama-server's default verbosity (3),
# and LOWER -lv values print less, not more -- which is why `-lv 9` lives in
# _DEFAULT_SERVER_FLAGS: without it every cell is unverifiable-by-construction and correctly
# classifies invalid_config. The `type_k =`/`type_v =` alternations cover the older format
# docs/qwen38-27b-bakeoff-results.md:174-176 described for `-v`.
_STARTUP_PATTERNS = {
    "model_path": re.compile(
        r"(?:loading model|model(?:\s+loaded from|\s*(?:path)?\s*[=:]))\s*'?([^\s'\"]+\.gguf)'?",
        re.I,
    ),
    "n_ctx": re.compile(r"n_ctx\s*=\s*(\d+)"),
    "type_k": re.compile(r"(?:type_k\s*=\s*'?|K \()(\w+)"),
    "type_v": re.compile(r"(?:type_v\s*=\s*'?|V \()(\w+)"),
    "flash_attn": re.compile(r"flash_attn\s*=\s*(\d+|enabled|disabled|auto)", re.I),
    "offloaded_layers": re.compile(r"offloaded\s+(\d+)/(\d+)\s+layers\s+to\s+GPU", re.I),
}


def verify_startup_state(log_text: str, cell: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Parses a scratch server's redirected startup log and asserts it matches what `cell`
    requested. Returns `(ok, details)`: `ok` is `True` only if every checkable field was found in
    the log AND matched; any field the log doesn't mention at all (e.g. because `-v` wasn't
    passed, so `type_k`/`type_v`/`flash_attn` never print) counts as a MISMATCH, not a silent
    pass -- an unverifiable cell is not a verified one.
    """
    flags = cell["server_flags"]
    found: Dict[str, Optional[str]] = {}
    for key, pattern in _STARTUP_PATTERNS.items():
        m = pattern.search(log_text)
        found[key] = m.group(0) if m else None

    checks: Dict[str, bool] = {}

    m = _STARTUP_PATTERNS["model_path"].search(log_text)
    expected_model = Path(cell["model_container_path"]).name
    checks["model_path"] = bool(m and expected_model in m.group(1))

    m = _STARTUP_PATTERNS["n_ctx"].search(log_text)
    checks["n_ctx"] = bool(m and int(m.group(1)) == int(flags["-c"]))

    m = _STARTUP_PATTERNS["type_k"].search(log_text)
    checks["type_k"] = bool(m and m.group(1).lower() == str(flags.get("-ctk", "f16")).lower())

    m = _STARTUP_PATTERNS["type_v"].search(log_text)
    checks["type_v"] = bool(m and m.group(1).lower() == str(flags.get("-ctv", "f16")).lower())

    m = _STARTUP_PATTERNS["flash_attn"].search(log_text)
    requested_fa = str(flags.get("-fa", "on")).lower()
    fa_active = bool(m and m.group(1).lower() in ("1", "enabled", "auto") and requested_fa in ("on", "auto"))
    fa_inactive = bool(m and m.group(1).lower() in ("0", "disabled") and requested_fa == "off")
    checks["flash_attn"] = fa_active or fa_inactive

    m = _STARTUP_PATTERNS["offloaded_layers"].search(log_text)
    requested_ngl = int(flags.get("-ngl", 99))
    checks["offload_placement"] = bool(
        m and (int(m.group(1)) == int(m.group(2)) or int(m.group(1)) >= min(requested_ngl, int(m.group(2))))
    )

    ok = all(checks.values())
    return ok, {"checks": checks, "matches": found}


# -------------------------------------------------------------------------------------------
# Scratch-server lifecycle context manager (audit E1, longcontext-pow2-overnight-campaign.md
# WP-1). Single implementation of spawn -> health-check -> verify -> (caller does its work) ->
# teardown, reused by BOTH this module's own `cmd_run` below and `batch.py`'s per-cell execution
# -- never duplicated. Only `scratch_server(...)`'s callers that actually enter the `with` block
# touch Docker; `--dry-run` and every other code path in this module still never do.
# -------------------------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScratchServerHandle:
    """Mutable handle yielded by `scratch_server()`. `teardown_result` is `None` until the `with`
    block exits (set inside the context manager's own `finally`) -- callers that keep a reference
    to the handle can read it once their `with` block has exited, since it's the same object
    mutated in place, not a snapshot.

    **Cell clock (plan `bakeoff-timing-records-and-estimates.md` WP-1, additive)**: three optional
    constructor arguments carry the per-cell timing a record consumer (`batch.write_cell_record`)
    stamps into `config.json`. All three default to `None`, so every pre-existing construction
    site (this module's own context manager before it was updated, and every test that builds a
    stand-in handle positionally or by keyword) keeps working unchanged:

    - `started_at` / `started_monotonic` -- the wall-clock ISO stamp and the monotonic reading
      taken at the TOP of the cell's scratch-server context (before the orphan sweep and the
      spawn), i.e. the start of the cell's real occupancy of the GPU. When omitted they default
      to "now at handle construction", which is the only start a directly-constructed handle can
      honestly claim; `cell_wall_seconds` is always derived from `started_monotonic` (never from
      differencing the ISO strings), so a clock step can never contaminate it.
    - `scratch_startup_seconds` -- see `_ScratchServerContext.__enter__`. Deliberately NOT named
      `model_load_seconds`: that name is reserved by `README.md`'s honest-timing-semantics
      contract for a `ResidencyPoller`-OBSERVED residency transition, and this measurement is a
      different thing (spawn -> startup-verification pass for a bare scratch llama-server).
      `None` whenever the measurement isn't available.
    """

    def __init__(
        self,
        cell_id: str,
        base_url: str,
        healthy: bool,
        verified: bool,
        details: Dict[str, Any],
        log_text: str,
        vram_before_bytes: int,
        port: int,
        container: str,
        started_at: Optional[str] = None,
        started_monotonic: Optional[float] = None,
        scratch_startup_seconds: Optional[float] = None,
    ) -> None:
        self.cell_id = cell_id
        self.base_url = base_url
        self.healthy = healthy
        self.verified = verified
        self.details = details
        self.log_text = log_text
        self.vram_before_bytes = vram_before_bytes
        self.port = port
        self.container = container
        self.teardown_result: Optional[Dict[str, Any]] = None
        self.started_at = started_at if started_at is not None else _utc_now_iso()
        self.started_monotonic = (
            started_monotonic if started_monotonic is not None else time.monotonic()
        )
        self.scratch_startup_seconds = scratch_startup_seconds


class _ScratchServerContext:
    """`contextlib.contextmanager`-style object, written as a class rather than a generator
    function so `teardown_result` can be attached to the yielded handle from inside `__exit__`
    (a plain `@contextmanager` generator's `finally` runs at the same point, but a class makes the
    "teardown always runs, even on an exception from the caller's `with` body" contract explicit
    and testable in isolation)."""

    def __init__(
        self,
        cell: Dict[str, Any],
        port: int,
        container: str,
        health_timeout: float,
    ) -> None:
        self.cell = cell
        self.port = port
        self.container = container
        self.health_timeout = health_timeout
        self.handle: Optional[ScratchServerHandle] = None

    def __enter__(self) -> ScratchServerHandle:
        # WP-1 (bakeoff-timing-records-and-estimates): the cell clock starts HERE, at the top of
        # the context -- before the orphan sweep and the spawn -- so `cell_wall_seconds` on the
        # persisted record covers the cell's whole occupancy of the GPU, not just the battery.
        cell_started_monotonic = time.monotonic()
        cell_started_at = _utc_now_iso()
        cell_id = self.cell.get("cell_id") or compute_cell_id(self.cell)
        # WP-5 attended-validation finding #6: pre-spawn orphan sweep. A crash inside this
        # method after `spawn_scratch_server` but before the handle exists (finding #5's
        # UnicodeDecodeError was exactly that) skips __exit__ teardown and leaves the server
        # running; the next run's spawn on the same port then collides with the orphan -- the
        # health poll answers from the orphan while the new process's log fails startup
        # verification, and the cell fails `invalid_config` for a reason that has nothing to do
        # with its config. Kill anything already on the port before spawning.
        if find_host_pid_by_port(self.port, container=self.container) is not None:
            teardown_scratch_server(self.port, container=self.container)
        vram_before = read_vram_used_bytes()
        argv = build_command(self.cell, port=self.port)
        log_path = f"/tmp/sweep-cell-{cell_id}.log"
        # WP-1 (bakeoff-timing-records-and-estimates): `scratch_startup_seconds`'s clock. Starts
        # at the create call below (`docker exec`-ing the llama-server child into the scratch
        # container) and stops the moment startup verification has a verdict -- the wait that was
        # untimed before this WP. Recorded ONLY when the server both answered health and passed
        # startup-state verification; a health timeout's elapsed time is the timeout, not a
        # startup measurement, so it stays `None` rather than being passed off as one.
        startup_t0 = time.monotonic()
        spawn_scratch_server(argv, log_path, container=self.container)

        bridge_ip = discover_bridge_ip(self.container)
        base_url = f"http://{bridge_ip}:{self.port}"
        healthy = wait_for_health(base_url, timeout=self.health_timeout)

        # WP-5 attended-validation finding #5: NEVER `text=True` here -- llama-server's `-lv 9`
        # startup log is not guaranteed UTF-8 (nemotron's log carried a raw 0xc4 byte), and a
        # strict decode raises UnicodeDecodeError out of __enter__, crashing the whole queue
        # mid-cell (state file left with the cell stuck 'running'). Decode bytes with
        # errors='replace': the startup-state patterns are all ASCII, so lossy replacement can
        # never break `verify_startup_state`.
        log_result = subprocess.run(
            ["docker", "exec", self.container, "cat", log_path],
            capture_output=True,
        )
        log_text = log_result.stdout.decode("utf-8", errors="replace")
        verified, details = verify_startup_state(log_text, self.cell)
        startup_seconds = time.monotonic() - startup_t0 if (healthy and verified) else None

        self.handle = ScratchServerHandle(
            cell_id=cell_id, base_url=base_url, healthy=healthy, verified=verified,
            details=details, log_text=log_text, vram_before_bytes=vram_before,
            port=self.port, container=self.container,
            started_at=cell_started_at, started_monotonic=cell_started_monotonic,
            scratch_startup_seconds=startup_seconds,
        )
        return self.handle

    def __exit__(self, exc_type, exc, tb) -> bool:
        teardown_result = teardown_scratch_server(self.port, container=self.container)
        if self.handle is not None:
            self.handle.teardown_result = teardown_result
        return False  # never swallow an exception from the caller's `with` body


def scratch_server(
    cell: Dict[str, Any],
    port: int,
    container: str = DEFAULT_CONTAINER_NAME,
    health_timeout: float = 120.0,
) -> _ScratchServerContext:
    """Context manager: spawn `cell`'s scratch server inside `container`, health-poll it, verify
    its startup state from its own log, yield a `ScratchServerHandle` -- and ALWAYS tear it down
    (`finally` semantics via `__exit__`, runs even if the caller's `with` body raises). This is
    the one scratch-server lifecycle implementation in this module; `cmd_run` below and
    `batch.py`'s per-cell execution both go through it, never re-implementing spawn/health/verify/
    teardown separately. Only entering this context manager touches Docker -- never called by
    `--dry-run` or by anything in this WP's own test suite (tests exercise `default_cell_runner`/
    `run_queue` in `batch.py` with a fake standing in for this function entirely).
    """
    return _ScratchServerContext(cell, port=port, container=container, health_timeout=health_timeout)
