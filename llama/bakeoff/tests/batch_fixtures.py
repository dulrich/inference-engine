"""Shared test fakes for the `test_batch.py` split (WP-R3 of
plans/queue-contract-and-test-decomposition-remediation.md). Plain module-level helpers only --
no base classes, no decorators, no fixture-plugin framework. Every helper here is consumed by
more than one of `test_queue_file.py` / `test_queue_state.py` / `test_batch_runner.py` /
`test_record_contract.py` / `test_batch.py`; a helper used by exactly one of those modules stays
defined beside its sole consumer instead of living here.

No live Docker/GPU execution anywhere in this suite -- `scratch_server_fn`/`container_controller`/
`hang_check_fn`/`vram_*_bytes_fn`/`now_fn` are always fakes; `run_queue` never falls through to
its real (Docker/sysfs-touching) defaults in any test that imports from this module."""

import json
import sys
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import longcontext  # noqa: E402
import sweep  # noqa: E402


def _minimal_queue(**overrides):
    queue = {
        "queue_format": 1,
        "vram_profile": "desktop-shared",
        "deadline": "23:59",
        "stop_policy": "pause-at-deadline",
        "models": {
            "model-a": {
                "model_container_path": "/models/a/model-a.gguf",
                "server_flags": {"-ngl": 99},
            },
            "model-b": {
                "model_container_path": "/models/b/model-b.gguf",
                "server_flags": {"-ngl": 99, "-ncmoe": 32},
                "gpu_weights_bytes": 6 * sweep.GIB,
                "kv_attention_layers": 10,
            },
        },
        "cells": [
            {
                "model": "model-a", "alias": "model-a-c32k-f16", "c": 32768,
                "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
                "sampling_arm": "capability", "trials": 3,
            },
            {
                "model": "model-a", "alias": "model-a-c65k-f16", "c": 65536,
                "ctk": "f16", "ctv": "f16", "fa": "on", "battery": ["vram"],
                "sampling_arm": "capability", "trials": 3,
            },
            {
                "model": "model-a", "alias": "model-a-c32k-q4", "c": 32768,
                "ctk": "q4_0", "ctv": "q4_0", "fa": "on", "battery": ["vram", "needle"],
                "sampling_arm": "capability", "trials": 3,
            },
            {
                "model": "model-a", "alias": "model-a-c65k-q4", "c": 65536,
                "ctk": "q4_0", "ctv": "q4_0", "fa": "on", "battery": ["vram", "needle"],
                "sampling_arm": "capability", "trials": 3,
            },
        ],
    }
    queue.update(overrides)
    return queue


def _write_queue(tmpdir, queue):
    path = Path(tmpdir) / "queue.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(queue, f)
    return path


class _FakeScratchServerCtx:
    def __init__(self, cell, port, container, healthy, verified):
        self.cell = cell
        self.port = port
        self.container = container
        self.healthy = healthy
        self.verified = verified
        self.handle = None

    def __enter__(self):
        self.handle = sweep.ScratchServerHandle(
            cell_id=self.cell["cell_id"], base_url="http://fake:1", healthy=self.healthy,
            verified=self.verified, details={}, log_text="", vram_before_bytes=0,
            port=self.port, container=self.container,
        )
        return self.handle

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            self.handle.teardown_result = {"signalled": False}
        return False


def fake_scratch_server_factory(healthy=True, verified=True, spawned=None):
    def _fn(cell, port, container):
        if spawned is not None:
            spawned.append(cell["cell_id"])
        return _FakeScratchServerCtx(cell, port, container, healthy, verified)
    return _fn


def scripted_cell_runner(outcomes_by_cell_id, default=None):
    default = default or batch.CellOutcome("completed")

    def _runner(handle, cell):
        return outcomes_by_cell_id.get(cell.cell_id, default)
    return _runner


class FakeContainerController:
    def __init__(self, fail_start=False):
        self.calls = []
        self.fail_start = fail_start

    def stop(self):
        self.calls.append("stop")

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("docker start failed")


def fixed_now_fn(dt):
    return lambda: dt


def sequenced_now_fn(first, rest):
    """First call returns `first` (used by `run_queue` as `start_dt`, the anchor
    `compute_deadline_dt` computes a strictly-future deadline from); every call after that
    returns `rest`. Needed to test "the deadline has already passed" -- `compute_deadline_dt`
    always returns a `deadline_dt` strictly later than whatever `now_fn()` returned when it was
    computed, so a plain fixed clock can never trip the deadline check inside the same
    invocation; a clock that visibly advances between the anchor read and the per-cell check
    can."""
    state = {"used_first": False}

    def _fn():
        if not state["used_first"]:
            state["used_first"] = True
            return first
        return rest

    return _fn


def _handle(healthy=True, verified=True, base_url="http://fake-bridge:9001", vram_before=1000):
    return sweep.ScratchServerHandle(
        cell_id="lc-cell", base_url=base_url, healthy=healthy, verified=verified,
        details={"checks": {}}, log_text="", vram_before_bytes=vram_before, port=1,
        container="fake",
    )


def _longcontext_cell(battery=("vram", "needle"), sampling_arm="capability", trials=3,
                      model_family="gemma4", context=32768, chars_per_token=None,
                      model_id=None, spec_decode=None, timing_rounds=None,
                      model_host_path=None):
    """A hand-built `ResolvedCell` for the runner/record tests.

    Every knob that used to be poked onto the cell dict after construction (`_chars_per_token`,
    `model`, `spec_decode`, `model_host_path`, ...) is a builder keyword now: `ResolvedCell` is
    frozen, and that is the point -- a test can no longer attach a field to a cell in an order
    that production code could not reproduce. `cell_id` is DERIVED from the identity fields
    rather than asserted as a literal, so these fixtures exercise the same identity path
    production does.
    """
    flags = {"-c": context}
    return batch.ResolvedCell(
        identity=batch.CellIdentity(
            alias="lc-alias",
            model_container_path="/models/lc/lc-model.gguf",
            server_flags=flags,
            model_host_path=model_host_path,
        ),
        execution=batch.ExecutionSettings.from_flags(flags, tuple(battery)),
        queue=batch.QueueCellMetadata(
            model_id=model_id, sampling_arm=sampling_arm, trials=trials,
            chars_per_token=chars_per_token, timing_rounds=timing_rounds,
            spec_decode=spec_decode, model_family=model_family,
        ),
    )


def _sample(task, verdict, correct, success=True):
    return {
        "task": task, "classifier_verdict": verdict, "correct": correct, "success": success,
    }


def _fake_battery_fn_factory(samples_by_task):
    """Returns a `battery_fn` stub matching `longcontext.run_live_battery`'s return shape
    (`{task_name: {position_fraction: CellResult}}`) -- one CellResult per task, all samples at a
    single synthetic position (0.45), and records every call's arguments for assertion."""
    calls = []

    def _fn(base_url, alias, cell_id_prefix, target_tokens, sampling_arm, sampling_payload,
            n_trials=10, max_tokens=256, timeout=300, chars_per_token=None,
            max_tokens_by_task=None, tokenize_fn=None, token_budget=None):
        # WP-2 (transport-taxonomy-and-prompt-fit): `tokenize_fn`/`token_budget` are the new
        # prompt-fit collaborators `_runner` now passes through this seam unconditionally
        # (same as `chars_per_token`) -- accepted and recorded here so pre-WP-2 tests that never
        # asserted on them keep passing unmodified, and `TestBudgetArithmeticThroughBatteryFn`
        # below can assert the exact values `_runner` computed. `timeout` (WP-1, qwen4exp-
        # bakeoff-campaign, cycle-2 blocking 1) is recorded the same additive way.
        calls.append({
            "base_url": base_url, "alias": alias, "cell_id_prefix": cell_id_prefix,
            "target_tokens": target_tokens, "sampling_arm": sampling_arm,
            "sampling_payload": dict(sampling_payload), "n_trials": n_trials,
            "chars_per_token": chars_per_token, "timeout": timeout,
            "max_tokens_by_task": dict(max_tokens_by_task) if max_tokens_by_task else None,
            "tokenize_fn": tokenize_fn, "token_budget": token_budget,
        })
        out = {}
        for task_name, samples in samples_by_task.items():
            cell_result = longcontext.CellResult(samples=samples, completions=[], summary=None)
            out[task_name] = {0.45: cell_result}
        return out

    _fn.calls = calls
    return _fn
