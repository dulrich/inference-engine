"""**The fit-and-command lock.** Every committed queue must resolve to the same fit
classification and the same `llama-server` argv, byte for byte, forever.

Companion to `test_cell_identity.py` (which locks the ordered `cell_id` list). Identity says
*which* cell a record belongs to; this file says *what that cell was judged to be and what it
would actually run*. Both are consumed downstream: a fit classification gates whether a cell is
attempted at all, and the built command is the exact process a bake-off measures. A refactor
that silently shifts either does not error -- it quietly changes what the fleet runs and what
its recorded numbers mean.

The current lock: 13 committed queues, 138 cells --

    fit_classifications  e9c8494cf5ab38ca  n=138
    build_commands       2571ce75b9bd783c  n=138
    vram_budgets         2f795a1ad011a72b
    geometry_shape       89c2f041b8feb1bf
    kv_bytes_table       5cc5a673e0dffe7b

History: captured at commit `27e1153` (pre-WP-R3) across 5 committed queues, 94 cells
(`d8b09bba7a47d2b0`/`56bfd847eaf30d3a`); extended at WP-1 of
`plans/qwen4exp-bakeoff-campaign.md` to 6 committed queues, 98 cells (`qwen4exp-viability-v1.json`
joined; `c049cba53b8962b5`/`ec2214347c80c9e2`), with the 94-cell digests re-derived unchanged by
that WP-1's additivity proof; further extended by WP-1 of `plans/qwen4exp-ram-ab-spike.md` (the
three `qwen4exp-ram-*` queues) to a 9-queue/113-cell state (`88a4b6faca3905fd`/
`35ead6585c1b30df`), with the 98-cell digests re-derived unchanged by that WP's additivity proof;
extended by WP-2 of `plans/minicpm5-2b-context-ladder.md` (`minicpm5-2b-ladder-v1.json` joined)
to a 10-queue/122-cell state (`e8573b2630a00cc4`/`72c9756e86b8466c`), with the 113-cell digests
re-derived unchanged before that WP's append; extended by WP-2 of
`plans/moe-offload-load-mode-and-qwen4exp-fit-math.md` (`lm-ab-v1.json` joined) to an
11-queue/126-cell state (`19b0f305383da67f`/`d0c807d4af2e8f57`), with the 122-cell digests
re-derived unchanged before that append; extended by WP-2 of
`plans/gemma4-26b-a4b-qat-mtp-ladder.md` (`gemma4-26b-a4b-ladder-v1.json` joined) to a
12-queue/132-cell state (`51845f5f73509e9d`/`0a984493131e9630`), with the 126-cell digests
re-derived unchanged -- and re-derived unchanged again AFTER that WP's
`gguf_fit._ROUTED_EXPERT_RE` fused-expert (`ffn_gate_up_exps`) fix, which no pre-existing
committed queue's model has a tensor for -- before that append; extended by WP-2b of the same
plan (`gemma4-26b-a4b-ladder-v2.json` joined, the no-think `--reasoning off` twin of that
ladder) to the current 13-queue/138-cell state above, with the 132-cell digests re-derived
unchanged before this append.

The per-cell `GOLDEN` table (in `fit_lock_fixtures.py`, WP-R6b of
`plans/post-audit-architecture-and-reconciliation-remediation.md` -- review finding F6, this
file had grown to 1,126 lines carrying that data table inline) is the human-auditable form of
the first two; the digests are a second, cheaper assertion over the *full* fit dicts (every byte
count and margin, not just the classification string) and the full argv lists.

**Fixed VRAM injections.** `classify_fit` reads host sysfs through injectable readers, and real
readings vary per run, so every assertion here injects 12.8 GiB total / 1 GiB used. These are
never to be replaced with live reads to make a pin match -- a pin that moves with the machine is
not a pin.

`TestTheLockIsNotVacuous` proves the table can fail: perturbing a cell's `-c` changes its
classification, and perturbing its KV type changes its command. Without those, a lock that
accidentally asserted nothing would look identical to one that works.

Entirely offline: reads committed queue JSON and (for cells carrying a `model_host_path`) GGUF
headers already on disk. No Docker, no network, no live sysfs.
"""

import contextlib
import functools
import hashlib
import json
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

from fit_lock_fixtures import (  # noqa: E402
    GEOMETRY_PROBE_METADATA,
    GOLDEN,
    GOLDEN_CELL_COUNT,
    GOLDEN_COMMAND_DIGEST,
    GOLDEN_FIT_DIGEST,
    GOLDEN_GEOMETRY,
    GOLDEN_KV_BYTES_PER_TOKEN,
    GOLDEN_VRAM_BUDGETS,
)

import batch  # noqa: E402
import gguf_fit  # noqa: E402
import sweep  # noqa: E402

QUEUES_DIR = BAKEOFF_DIR / "queues"

# The pinned injections (12.8 GiB total / 1 GiB used) -- see this module's docstring.
PINNED_VRAM_TOTAL_BYTES = 13744734208
PINNED_VRAM_USED_BYTES = 1073741824
PINNED_COMMAND_PORT = 8080


def _vram_total() -> int:
    return PINNED_VRAM_TOTAL_BYTES


def _vram_used() -> int:
    return PINNED_VRAM_USED_BYTES


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


@contextlib.contextmanager
def _memoized_gguf_reader():
    """Reads each distinct GGUF header ONCE per run instead of once per cell.

    `read_gguf_metadata` parses a model's entire metadata block, tokenizer vocabulary included --
    ~0.14 s per call against these headers. The 94 cells reference only a handful of distinct
    model files, so a naive table walk spends ~13 s re-parsing the same files over and over,
    against a whole-suite baseline of ~2.5 s.

    This wraps the REAL reader in an `lru_cache` for the duration of the computation: same
    parser, same bytes off disk, same results (a model file cannot change mid-run) -- it is a
    cache, never a fake. Nothing about what is being locked changes.
    """
    cached = functools.lru_cache(maxsize=None)(gguf_fit.read_gguf_metadata)
    with mock.patch.object(gguf_fit, "read_gguf_metadata", cached):
        yield


def _resolved_sweep_cells(queue_name):
    """`(vram_profile, [plain-dict cell, ...])` for one committed queue, through the exact same
    path `batch.py` uses live: `load_queue` -> `resolve_queue_cells` -> `as_sweep_cell()`."""
    queue = batch.load_queue(QUEUES_DIR / f"{queue_name}.json")
    return queue["vram_profile"], [c.as_sweep_cell() for c in batch.resolve_queue_cells(queue)]


def _compute(queue_name):
    vram_profile, cells = _resolved_sweep_cells(queue_name)
    return {
        "vram_profile": vram_profile,
        "cells": cells,
        "fits": [sweep.classify_fit(c, vram_profile, _vram_total, _vram_used) for c in cells],
        "commands": [sweep.build_command(c, PINNED_COMMAND_PORT) for c in cells],
    }


_RESULTS = None


def _results():
    """Every queue's cells, fit classifications, and built commands -- computed once for the
    whole module, in committed-queue order."""
    global _RESULTS
    if _RESULTS is None:
        with _memoized_gguf_reader():
            _RESULTS = {name: _compute(name) for name in GOLDEN}
    return _RESULTS


class TestCommittedQueueFitClassifications(unittest.TestCase):
    """Every committed cell classifies exactly as it did at `27e1153`."""

    def test_every_cell_classifies_identically(self):
        for queue_name, golden in GOLDEN.items():
            computed = _results()[queue_name]
            self.assertEqual(computed["vram_profile"], golden.vram_profile, queue_name)
            self.assertEqual(len(computed["cells"]), len(golden.cells), f"{queue_name}: count")
            for cell, fit, expected in zip(computed["cells"], computed["fits"], golden.cells):
                with self.subTest(queue=queue_name, cell_id=expected.cell_id):
                    self.assertEqual(cell["cell_id"], expected.cell_id)
                    self.assertEqual(fit["classification"], expected.classification)

    def test_full_fit_dicts_match_the_pinned_digest(self):
        """Locks every number behind the classification -- weights, KV bytes, budget, margin --
        not merely the four-way classification string."""
        fits = [f for name in GOLDEN for f in _results()[name]["fits"]]
        self.assertEqual(len(fits), GOLDEN_CELL_COUNT)
        self.assertEqual(_digest(fits), GOLDEN_FIT_DIGEST)


class TestCommittedQueueBuiltCommands(unittest.TestCase):
    """Every committed cell renders the same `llama-server` argv it did at `27e1153`."""

    def test_every_cell_builds_the_same_command(self):
        for queue_name, golden in GOLDEN.items():
            computed = _results()[queue_name]
            for argv, expected in zip(computed["commands"], golden.cells):
                with self.subTest(queue=queue_name, cell_id=expected.cell_id):
                    self.assertEqual(" ".join(shlex.quote(a) for a in argv), expected.command)

    def test_full_argv_lists_match_the_pinned_digest(self):
        cmds = [c for name in GOLDEN for c in _results()[name]["commands"]]
        self.assertEqual(len(cmds), GOLDEN_CELL_COUNT)
        self.assertEqual(_digest(cmds), GOLDEN_COMMAND_DIGEST)


class TestVRAMBudgetsAndKVArithmetic(unittest.TestCase):
    """The pure inputs every classification is built out of."""

    def test_vram_budgets_for_both_profiles(self):
        budgets = {
            p: sweep.compute_vram_budget_bytes(p, _vram_total, _vram_used)
            for p in sweep.VRAM_PROFILES
        }
        self.assertEqual(budgets, GOLDEN_VRAM_BUDGETS)

    def test_attention_geometry_shape(self):
        self.assertEqual(sweep.gguf_attention_geometry(GEOMETRY_PROBE_METADATA), GOLDEN_GEOMETRY)

    def test_kv_bytes_per_token_table(self):
        geometry = sweep.gguf_attention_geometry(GEOMETRY_PROBE_METADATA)
        for (ctk, ctv), expected in GOLDEN_KV_BYTES_PER_TOKEN.items():
            with self.subTest(ctk=ctk, ctv=ctv):
                self.assertEqual(sweep.kv_bytes_per_token(geometry, ctk, ctv), expected)


class TestTheLockIsNotVacuous(unittest.TestCase):
    """Negative controls. A golden table that cannot fail is not a lock, and these three prove
    the assertions above are actually sensitive to the things they claim to pin."""

    def _perturbed(self, flag, value):
        computed = _results()["ladder-v1"]
        cell = dict(computed["cells"][0])
        cell["server_flags"] = dict(cell["server_flags"])
        cell["server_flags"][flag] = value
        return computed["vram_profile"], cell

    def test_a_larger_context_ceiling_changes_the_classification(self):
        computed = _results()["ladder-v1"]
        baseline = computed["fits"][0]
        self.assertEqual(baseline["classification"], GOLDEN["ladder-v1"].cells[0].classification)

        vram_profile, cell = self._perturbed(
            "-c", computed["cells"][0]["server_flags"]["-c"] * 8
        )
        with _memoized_gguf_reader():
            moved = sweep.classify_fit(cell, vram_profile, _vram_total, _vram_used)
        self.assertNotEqual(moved["classification"], baseline["classification"])
        self.assertEqual(moved["classification"], "excluded")

    def test_a_different_kv_type_changes_the_built_command(self):
        baseline = _results()["ladder-v1"]["commands"][0]
        self.assertEqual(
            " ".join(shlex.quote(a) for a in baseline), GOLDEN["ladder-v1"].cells[0].command
        )
        _, cell = self._perturbed("-ctk", "q4_0")
        self.assertNotEqual(sweep.build_command(cell, PINNED_COMMAND_PORT), baseline)

    def test_perturbing_one_cell_breaks_the_cross_queue_digests(self):
        computed = _results()["ladder-v1"]
        vram_profile, cell = self._perturbed(
            "-c", computed["cells"][0]["server_flags"]["-c"] * 8
        )
        with _memoized_gguf_reader():
            fits = [sweep.classify_fit(cell, vram_profile, _vram_total, _vram_used)]
            fits += computed["fits"][1:]
        cmds = [sweep.build_command(cell, PINNED_COMMAND_PORT), *computed["commands"][1:]]
        self.assertNotEqual(_digest(fits), GOLDEN_FIT_DIGEST)
        self.assertNotEqual(_digest(cmds), GOLDEN_COMMAND_DIGEST)


if __name__ == "__main__":
    unittest.main()
