#!/usr/bin/env python3
"""Shared paths, constants, and the fixed cold/warm/hot performance fixture (WP-R2 of
`plans/repository-tooling-structural-remediation.md`; review finding F7).

This is the dependency-free base of the `run_harness` family. It owns the three things every
other harness module needs before it can do anything: where the repo's files live, the numeric
constants the record writer and the CLI both quote, and the deterministic filler-text fixture
whose bytes are pinned by `tests/test_pipeline_determinism.py`.

`FIXTURE_PROMPT_TEXT` is built ONCE at import time from a fixed-seed generator and must never be
re-derived per call (plan `serving-harness-round-2.md` WP-1) -- moving it here does not change
that: there is still exactly one construction, in exactly one module, at import time.

Imports nothing from this package. Every other `harness_*` module (and `run_harness.py` itself)
depends on this one, never the other way round -- that acyclic direction is what lets
`harness_cli.py` import `run_harness` at module level without a cycle.

Every public name here is re-exported from `run_harness` at its historical home, so
`run_harness.FIXTURE_PROMPT_TEXT` / `run_harness.CHARS_PER_TOKEN_ESTIMATE` / `run_harness.
summarize` (and the underscore-prefixed `_build_filler_text`, which `longcontext.py`'s docstring
and this repo's tests both name explicitly) keep resolving exactly as before.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import random
import statistics
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------------------------

MODULE_DIR = Path(__file__).resolve().parent
LLAMA_DIR = MODULE_DIR.parent
REPO_ROOT = LLAMA_DIR.parent
DOCKERFILE_PATH = LLAMA_DIR / "Dockerfile"
QUALITY_CORPUS_PATH = MODULE_DIR / "quality_corpus.jsonl"
DEFAULT_RUNS_DIR = REPO_ROOT / "llama" / "bakeoff" / "records"

DEFAULT_BASE_URL = "http://127.0.0.1:4811/v1"

# Deliberately separate from DEFAULT_BASE_URL above: this is llama-swap's own root, used only by
# fetch_router_logs() for its `/logs` endpoint. Reusing DEFAULT_BASE_URL here would silently
# produce the wrong path (".../v1/logs", which does not exist) -- plan WP-1, rev 2 audit finding.
DEFAULT_ROUTER_BASE_URL = "http://127.0.0.1:4811"

# Kept in sync with `llama/Dockerfile`'s `ARG LLAMA_CPP_TAG=` line by `resolve_llama_cpp_build()`
# below; this is only the fallback used if that read/parse ever fails.
FALLBACK_LLAMA_CPP_BUILD = "b10534"

# Decisions locked / cycle-2 audit finding: any single llama/bakeoff/records/<experiment-id>/ directory
# over this size is a hold-for-user signal, not a silent commit.
SIZE_LIMIT_BYTES = 25 * 1024 * 1024  # 25 MiB

# config.json's pinned artifact-lineage field set (plan "Decisions locked").
REQUIRED_LINEAGE_FIELDS = (
    "hf_repo",
    "hf_revision",
    "filename",
    "file_size",
    "sha256",
    "llama_cpp_build",
)

# ---------------------------------------------------------------------------------------------
# Fixed performance fixture -- identical input-token-count/output-budget shape across every
# cold/warm/hot round of a given model (plan WP-1: "Keep this fixture's prompt text and token
# budget as named constants in the module, not re-derived per call").
# ---------------------------------------------------------------------------------------------

FIXTURE_INPUT_TOKENS_TARGET = 8000
FIXTURE_OUTPUT_TOKENS = 256
FIXTURE_SEED = 20260821  # date this WP was drafted; arbitrary but fixed, for reproducibility

# This repo's own measured average across two real needle-probe captures
# (docs/ling-3-tiny-bakeoff-results.md): 135,066 chars / 29,463 tokens ~= 4.585 chars/token;
# 500,032 chars / 111,272 tokens ~= 4.495 chars/token. Used only to *target* a character count
# when building filler text -- the real `prompt_tokens` is always read back from the server's own
# `usage` field, never assumed from character count (plan invariant).
CHARS_PER_TOKEN_ESTIMATE = 4.5

_WORD_BANK = (
    "system", "network", "signal", "process", "cluster", "buffer", "protocol", "vector",
    "sequence", "channel", "context", "module", "pipeline", "gateway", "interval", "region",
    "matrix", "surface", "boundary", "cycle", "pattern", "response", "session", "record",
    "sample", "index", "cluster", "queue", "state", "thread", "layer", "kernel", "device",
    "stream", "packet", "frame", "cache", "token", "graph", "node", "edge", "weight", "value",
    "table", "field", "entry", "block", "chunk", "batch", "epoch", "trial", "metric", "trace",
    "engine", "driver", "handler", "listener", "monitor", "sensor", "controller", "adapter",
    "manager", "worker", "client", "server", "router", "proxy", "socket", "port", "endpoint",
    "resource", "capacity", "latency", "throughput", "baseline", "variance", "threshold",
    "gradient", "profile", "schema", "policy", "config", "runtime", "instance", "container",
    "volume", "mount", "archive", "backup", "snapshot", "ledger", "journal", "log", "audit",
    "report", "summary", "outcome", "result", "finding", "signal", "anomaly", "artifact",
)


def _build_filler_text(target_tokens: int, seed: int) -> str:
    """Deterministic pseudo-prose filler sized to approximately `target_tokens` tokens.

    Content is meaningless by design -- this is a timing fixture, never quality-scored. The
    actual on-wire token count is always read back from the server's own `usage.prompt_tokens`,
    never assumed from this function's character-count target.
    """
    rng = random.Random(seed)
    target_chars = int(target_tokens * CHARS_PER_TOKEN_ESTIMATE)
    parts: list[str] = []
    length = 0
    words_in_sentence = 0
    sentence_target = rng.randint(8, 16)
    while length < target_chars:
        word = rng.choice(_WORD_BANK)
        words_in_sentence += 1
        if words_in_sentence >= sentence_target:
            word += "."
            words_in_sentence = 0
            sentence_target = rng.randint(8, 16)
        parts.append(word)
        length += len(word) + 1
    text = " ".join(parts)
    # Tidy up "word. Word" capitalization is not worth the complexity for a pure timing fixture.
    return text


FIXTURE_PROMPT_BODY = _build_filler_text(FIXTURE_INPUT_TOKENS_TARGET, seed=FIXTURE_SEED)
FIXTURE_INSTRUCTION = (
    "\n\nBased only on the text above, name three distinct topics it touches on, one short "
    "sentence each."
)
FIXTURE_PROMPT_TEXT = FIXTURE_PROMPT_BODY + FIXTURE_INSTRUCTION


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _single_user_message(prompt: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": prompt}]


def summarize(values: list[float | None]) -> dict[str, float | None]:
    """n/median/mean/min/max/stdev over the non-null values (stdlib `statistics`)."""
    clean = [v for v in values if v is not None]
    if not clean:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None, "stdev": None}
    n = len(clean)
    return {
        "n": n,
        "median": statistics.median(clean),
        "mean": statistics.mean(clean),
        "min": min(clean),
        "max": max(clean),
        "stdev": statistics.stdev(clean) if n >= 2 else 0.0,
    }
