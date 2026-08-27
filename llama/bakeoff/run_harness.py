#!/usr/bin/env python3
"""Serving-harness bake-off tooling (plans/serving-harness-round-2.md, WP-1).

Reusable measurement tooling for any already-configured router alias served by
inference-engine-llama (llama-swap + llama.cpp, OpenAI-compatible `/v1/chat/completions`).
WP-2 through WP-5 each point this at a different alias -- there is no alias-specific code path
anywhere in this module.

Zero new runtime dependencies: Python 3 stdlib only (urllib, json, time, statistics, hashlib,
threading, argparse). No `requests`, no venv, no `requirements.txt`.

Honest-timing-semantics contract (hard invariant, see README.md):
  - `model_load_seconds` is populated ONLY if a distinct load-ready transition was actually
    observed by the background `ResidencyPoller` during a cold round. It is `null` whenever that
    transition wasn't caught (including: the alias was already resident when the poller started,
    or the transition happened between polls). It is NEVER fabricated by subtracting an assumed
    constant from another timing value.
  - `first_request_e2e_ttft_ms` is always populated (request issued -> first generated token of
    any kind) and, for a genuinely cold round, includes model load time inside it -- this is the
    number every later WP actually reports if `model_load_seconds` stays null.
  - Warm rounds assert `cached_tokens / prompt_tokens < 0.05`; hot rounds assert
    `cached_tokens / prompt_tokens > 0.90`. A round that fails its own cache-state assertion is
    flagged (`cache_assertion_passed: false`), never silently folded into an aggregate.

Three run states (cold/warm/hot) share one fixed performance fixture (module-level constants
`FIXTURE_PROMPT_TEXT` / `FIXTURE_OUTPUT_TOKENS`, built once at import time from a
fixed-seed deterministic filler generator -- not re-derived per call). See README.md for the
warm/hot cache-priming design (why hot fires an explicit, unrecorded priming call rather than
depending on execution order).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    parts: List[str] = []
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
    return datetime.now(timezone.utc).isoformat()


def _single_user_message(prompt: str) -> List[Dict[str, str]]:
    return [{"role": "user", "content": prompt}]


def summarize(values: List[Optional[float]]) -> Dict[str, Optional[float]]:
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


# ---------------------------------------------------------------------------------------------
# llama.cpp build resolution + artifact lineage
# ---------------------------------------------------------------------------------------------


def resolve_llama_cpp_build() -> str:
    """Read the pinned llama.cpp tag from `llama/Dockerfile`'s `ARG LLAMA_CPP_TAG=` line.

    Falls back to `FALLBACK_LLAMA_CPP_BUILD` (kept in sync by hand) if the file can't be read or
    the line can't be parsed -- reading the Dockerfile is cheap, so this should only trigger if
    the Dockerfile's ARG line itself changes shape.
    """
    try:
        text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_LLAMA_CPP_BUILD
    match = re.search(r"^ARG\s+LLAMA_CPP_TAG=(\S+)", text, re.MULTILINE)
    if not match:
        return FALLBACK_LLAMA_CPP_BUILD
    return match.group(1)


def compute_file_lineage(host_path: Path, chunk_size: int = 1024 * 1024) -> Dict[str, Any]:
    """Reads a local GGUF file to derive `filename`/`file_size`/`sha256` for config.json's
    lineage block. Takes a real host filesystem path -- this module deliberately does not
    hardcode the container-internal `/models` -> host bind-mount mapping (that mapping lives in
    fleet-tools's compose.fleet.yml, out of this WP's scope); callers that know the mapping
    pass the resolved host path in.
    """
    host_path = Path(host_path)
    size = host_path.stat().st_size
    digest = hashlib.sha256()
    with host_path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return {"filename": host_path.name, "file_size": size, "sha256": digest.hexdigest()}


# ---------------------------------------------------------------------------------------------
# Streaming HTTP
# ---------------------------------------------------------------------------------------------


def _post_stream(url: str, payload: Dict[str, Any], timeout: float):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            yield raw_line


def _execute_streaming_request(
    base_url: str,
    alias: str,
    messages: List[Dict[str, str]],
    max_tokens: int,
    timeout: float = 180,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[float] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Fires one streaming `/v1/chat/completions` request and independently timestamps every
    delta as it arrives (verified live against this repo's real router: urllib's chunked-transfer
    decoding delivers SSE lines incrementally, not buffered -- see WP-1 report for the probe).

    Reasoning-model-aware: the first NON-EMPTY generated delta (either `reasoning_content` or
    `content`) is what's timestamped for `first_delta_ts`; `first_content_ts` is only set by a
    non-empty `content` delta specifically. The initial role-announcement chunk
    (`delta: {"role": "assistant", "content": null}`) is never treated as a generated token.

    Requests `stream_options: {"include_usage": true}` so llama.cpp's final chunk carries
    `usage.prompt_tokens_details.cached_tokens` and `timings.*` (server-reported
    prompt/decode counts, times, tok/s) -- both retained verbatim in the returned dict, never
    substituted for the client-side timestamps.

    `temperature`/`top_p`/`top_k`/`seed` (plan `quant-sweep-harness-and-instrument-v2.md` WP-1,
    "Pinned-sampling plumbing"): each is omitted from the request payload entirely when `None`
    (the default) -- calling this with all four omitted is byte-identical to the pre-WP-1 payload,
    preserving current behavior for the timing fixtures (`run_round`/`run_sweep`), which the plan
    explicitly keeps on server defaults ("Timing fixtures may keep server defaults; they are never
    quality-scored" -- Decisions locked). `needle_probe` and `run_quality_corpus` are the two
    callers that thread real values through these parameters, for correctness-bearing requests.
    """
    url = f"{base_url}/chat/completions"
    payload = {
        "model": alias,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k is not None:
        payload["top_k"] = top_k
    if seed is not None:
        payload["seed"] = seed

    t0 = time.monotonic()
    t0_iso = _utc_now_iso()
    first_delta_ts: Optional[float] = None
    first_content_ts: Optional[float] = None
    last_delta_ts: Optional[float] = None
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    timings: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    try:
        for raw_line in _post_stream(url, payload, timeout):
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[len("data:"):].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            now = time.monotonic()
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                fr = choices[0].get("finish_reason")
                if fr:
                    finish_reason = fr
                reasoning = delta.get("reasoning_content")
                content = delta.get("content")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if first_delta_ts is None:
                        first_delta_ts = now
                    last_delta_ts = now
                if content:
                    content_parts.append(content)
                    if first_delta_ts is None:
                        first_delta_ts = now
                    if first_content_ts is None:
                        first_content_ts = now
                    last_delta_ts = now
            else:
                if "usage" in obj:
                    usage = obj.get("usage")
                if "timings" in obj:
                    timings = obj.get("timings")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"

    t_end = time.monotonic()
    return {
        "success": error is None,
        "error": error,
        "t0": t0,
        "t0_iso": t0_iso,
        "wall_seconds": t_end - t0,
        "first_delta_ts": first_delta_ts,
        "first_content_ts": first_content_ts,
        "last_delta_ts": last_delta_ts,
        "content_text": "".join(content_parts),
        "reasoning_text": "".join(reasoning_parts),
        "finish_reason": finish_reason,
        "usage": usage,
        "timings": timings,
    }


# ---------------------------------------------------------------------------------------------
# model_load_seconds -- candidate observability mechanism (see README.md / plan Assumptions)
# ---------------------------------------------------------------------------------------------


class ResidencyPoller:
    """Background poller watching `GET {base_url}/models` for `alias`'s resident status,
    the candidate `model_load_seconds` signal per the plan's Assumptions section.

    Best-effort by explicit plan sanction: if the alias is already resident when the poller
    starts (never observed a non-"loaded" status), or the load-ready transition happens between
    polls, `result()` returns `None` and `model_load_seconds` stays null on the caller's sample --
    this is the honest-timing-semantics fallback, not a bug.
    """

    def __init__(self, base_url: str, alias: str, poll_interval: float = 0.05):
        self._base_url = base_url
        self._alias = alias
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._t_start: Optional[float] = None
        self._saw_unloaded = False
        self._load_ready_ts: Optional[float] = None
        self._lock = threading.Lock()

    def _poll_once(self) -> Optional[str]:
        try:
            req = urllib.request.Request(f"{self._base_url}/models", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            return None
        for entry in obj.get("data", []):
            aliases = (entry.get("meta", {}).get("llamaswap", {}) or {}).get("aliases", [])
            if self._alias in aliases:
                return (entry.get("status") or {}).get("value")
        return None

    def _run(self) -> None:
        while not self._stop.is_set():
            status = self._poll_once()
            now = time.monotonic()
            with self._lock:
                if status is not None and status != "loaded":
                    self._saw_unloaded = True
                elif status == "loaded" and self._saw_unloaded and self._load_ready_ts is None:
                    self._load_ready_ts = now
            time.sleep(self._poll_interval)

    def start(self) -> None:
        self._t_start = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def result(self) -> Optional[float]:
        with self._lock:
            if self._t_start is None or self._load_ready_ts is None:
                return None
            return self._load_ready_ts - self._t_start


# ---------------------------------------------------------------------------------------------
# Timing rounds (cold/warm/hot)
# ---------------------------------------------------------------------------------------------


def run_round(
    base_url: str,
    alias: str,
    run_state: str,
    round_index: int,
    prompt: str,
    max_tokens: int,
    timeout: float = 180,
    poll_for_load: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Executes one cold/warm/hot round of the fixed performance fixture. Never raises on a
    transport failure or a malformed response -- returns `success: false` on the sample instead,
    so a sweep can record a bad round without aborting the rest of the measurement run.

    Returns `(sample, completion)`: `sample` is the numeric record for `samples.jsonl`,
    `completion` carries the verbatim raw text for `completions.jsonl`.
    """
    poller: Optional[ResidencyPoller] = None
    if poll_for_load:
        poller = ResidencyPoller(base_url, alias)
        poller.start()

    result = _execute_streaming_request(
        base_url, alias, _single_user_message(prompt), max_tokens, timeout=timeout
    )

    model_load_seconds: Optional[float] = None
    if poller is not None:
        poller.stop()
        model_load_seconds = poller.result()

    usage = result["usage"] or {}
    timings = result["timings"] or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    total_tokens = usage.get("total_tokens")
    cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    if cached_tokens is None:
        cached_tokens = timings.get("cache_n")  # fallback signal, per plan text
    cache_hit_ratio = (
        cached_tokens / prompt_tokens if (cached_tokens is not None and prompt_tokens) else None
    )

    cache_assertion_passed: Optional[bool] = None
    if run_state == "warm":
        cache_assertion_passed = cache_hit_ratio is not None and cache_hit_ratio < 0.05
    elif run_state == "hot":
        cache_assertion_passed = cache_hit_ratio is not None and cache_hit_ratio > 0.90

    def _ms(a: Optional[float], b: Optional[float]) -> Optional[float]:
        return (a - b) * 1000.0 if (a is not None and b is not None) else None

    first_generation_token_ms = _ms(result["first_delta_ts"], result["t0"])
    first_visible_content_ms = _ms(result["first_content_ts"], result["t0"])
    # Always populated (request issued -> first generated token of any kind); includes model
    # load time whenever the round was genuinely cold. Same basis as first_generation_token_ms --
    # kept as an explicit separate key because the plan pins both names.
    first_request_e2e_ttft_ms = first_generation_token_ms
    warm_ttft_ms = first_generation_token_ms if run_state == "warm" else None
    hot_ttft_ms = first_generation_token_ms if run_state == "hot" else None
    decode_wall_ms = _ms(result["last_delta_ts"], result["first_delta_ts"])
    observed_decode_tok_s = None
    if decode_wall_ms and completion_tokens:
        observed_decode_tok_s = completion_tokens / (decode_wall_ms / 1000.0)

    completion_ref = f"{run_state}-{round_index}"
    sample = {
        "type": "timing_round",
        "completion_ref": completion_ref,
        "round_index": round_index,
        "run_state": run_state,
        "timestamp": result["t0_iso"],
        "success": result["success"],
        "error": result["error"],
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cache_hit_ratio": cache_hit_ratio,
        "cache_assertion_passed": cache_assertion_passed,
        "first_generation_token_ms": first_generation_token_ms,
        "first_visible_content_ms": first_visible_content_ms,
        "first_request_e2e_ttft_ms": first_request_e2e_ttft_ms,
        "warm_ttft_ms": warm_ttft_ms,
        "hot_ttft_ms": hot_ttft_ms,
        "model_load_seconds": model_load_seconds,
        "decode_wall_ms": decode_wall_ms,
        "observed_decode_tok_s": observed_decode_tok_s,
        "server_prompt_eval_count": timings.get("prompt_n"),
        "server_prompt_eval_time_ms": timings.get("prompt_ms"),
        "server_prompt_eval_tok_s": timings.get("prompt_per_second"),
        "server_eval_count": timings.get("predicted_n"),
        "server_eval_time_ms": timings.get("predicted_ms"),
        "server_eval_tok_s": timings.get("predicted_per_second"),
        "finish_reason": result["finish_reason"],
        "wall_seconds": result["wall_seconds"],
    }
    completion = {
        "type": "timing_round",
        "completion_ref": completion_ref,
        "round_index": round_index,
        "run_state": run_state,
        "prompt_char_len": len(prompt),
        "content_text": result["content_text"],
        "reasoning_text": result["reasoning_text"],
        "finish_reason": result["finish_reason"],
    }
    return sample, completion


def _novel_variant(base_text: str, round_index: int) -> str:
    """A same-size-shape but genuinely novel warm-round prompt: a short unique nonce line
    prepended to the fixed fixture body. Changing the prefix from token 0 guarantees a cache
    miss regardless of what's currently cached, without needing to regenerate ~8K tokens of new
    filler text per round.
    """
    nonce = f"[[warm-round={round_index} nonce={uuid.uuid4().hex}]]\n"
    return nonce + base_text


def _summarize_sweep(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for state in ("cold", "warm", "hot"):
        # round_index 0 is the hot phase's unrecorded priming call -- excluded from aggregates.
        state_samples = [
            s for s in samples if s["run_state"] == state and s.get("round_index", 0) > 0
        ]
        ttft_vals = [s["first_generation_token_ms"] for s in state_samples]
        decode_vals = [s["observed_decode_tok_s"] for s in state_samples]
        out[state] = {
            "first_generation_token_ms": summarize(ttft_vals),
            "observed_decode_tok_s": summarize(decode_vals),
            "cache_assertions_passed": sum(
                1 for s in state_samples if s.get("cache_assertion_passed") is True
            ),
            "cache_assertions_total": sum(
                1 for s in state_samples if s.get("cache_assertion_passed") is not None
            ),
        }
    return out


def run_sweep(
    base_url: str,
    alias: str,
    cold_rounds: int = 3,
    warm_rounds: int = 3,
    hot_rounds: int = 3,
    input_tokens_target: int = FIXTURE_INPUT_TOKENS_TARGET,
    output_tokens: int = FIXTURE_OUTPUT_TOKENS,
    timeout: float = 180,
    cold_precondition_hook=None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Runs the fixed performance fixture through cold, then warm, then hot phases.

    `cold_precondition_hook`, if given, is called with no arguments immediately before EACH cold
    round to let the caller force a genuine model-not-resident state (container restart, or
    `exclusive: true` group eviction via a request into a different group -- plan Assumptions;
    this harness deliberately does not choose or perform that mechanism itself, since either
    satisfies the same observable honest-timing contract). Without a hook, cold rounds still run
    and are recorded, but are only genuinely cold the first time nothing else has warmed the
    alias -- callers running real bake-off WPs should supply one.

    Hot-phase cache priming: an explicit, unrecorded priming request (round_index 0) is fired
    with the identical fixture text immediately before the measured hot rounds, so hot-phase
    correctness never depends on what ran before it (specifically: not on warm's execution
    happening before or after).

    Uses `input_tokens_target == FIXTURE_INPUT_TOKENS_TARGET` (the default) to reuse the
    module-level `FIXTURE_PROMPT_TEXT` constant without rebuilding it; a non-default token target
    rebuilds an equivalent fixed prompt for this call only.
    """
    if input_tokens_target == FIXTURE_INPUT_TOKENS_TARGET:
        fixture = FIXTURE_PROMPT_TEXT
    else:
        fixture = _build_filler_text(input_tokens_target, seed=FIXTURE_SEED) + FIXTURE_INSTRUCTION

    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []

    for i in range(cold_rounds):
        if cold_precondition_hook is not None:
            cold_precondition_hook()
        s, c = run_round(
            base_url, alias, "cold", i + 1, fixture, output_tokens,
            timeout=timeout, poll_for_load=(i == 0),
        )
        samples.append(s)
        completions.append(c)

    for i in range(warm_rounds):
        prompt = _novel_variant(fixture, i + 1)
        s, c = run_round(base_url, alias, "warm", i + 1, prompt, output_tokens, timeout=timeout)
        samples.append(s)
        completions.append(c)

    s_prime, c_prime = run_round(base_url, alias, "hot", 0, fixture, output_tokens, timeout=timeout)
    s_prime["note"] = "priming call (round_index 0) -- excluded from the hot aggregate"
    samples.append(s_prime)
    completions.append(c_prime)
    for i in range(hot_rounds):
        s, c = run_round(base_url, alias, "hot", i + 1, fixture, output_tokens, timeout=timeout)
        samples.append(s)
        completions.append(c)

    summary = _summarize_sweep(samples)
    return samples, completions, summary


# ---------------------------------------------------------------------------------------------
# Live tokenizer calibration (plan qwen38-27b-kv-cache-quantization.md, WP-1)
# ---------------------------------------------------------------------------------------------

# A modest probe -- large enough that per-token rounding noise doesn't dominate the ratio, small
# enough that calibration itself stays cheap. Fixed seed for reproducibility, matching this
# module's other filler-text callers (FIXTURE_SEED, needle_probe's own rng).
CALIBRATION_PROBE_TOKENS = 2000
CALIBRATION_SEED = 20260821
CALIBRATION_MAX_TOKENS = 8  # only usage.prompt_tokens is read back -- the completion is discarded


def calibrate_chars_per_token(
    base_url: str,
    alias: str,
    probe_tokens: int = CALIBRATION_PROBE_TOKENS,
    timeout: float = 60,
) -> float:
    """Live-derives `alias`'s real tokens-per-nominal-character-estimate-token ratio.

    Builds a modest filler sample via `_build_filler_text(probe_tokens, seed=CALIBRATION_SEED)`,
    fires one minimal chat-completion request (small `max_tokens` -- only `usage.prompt_tokens`
    is read back, the completion itself is discarded), and returns the empirical ratio
    `real_prompt_tokens / probe_tokens`.

    Live-derived every call -- this deliberately never hardcodes CHARS_PER_TOKEN_ESTIMATE (~4.5,
    the module's own filler-text sizing constant) or any other fixed ratio. The historical ~0.66
    ratio this repo has previously observed (docs/ling-3-tiny-bakeoff-results.md) was measured on
    a ~35K-token sample for a different model/tokenizer; nothing guarantees it holds for a
    different alias or a much larger target, which is exactly why this function exists instead of
    a constant.

    Raises (does not catch-and-default) if the calibration request itself errors or times out --
    a calibration failure is a hard stop for whatever called this, never silently defaulted to
    1.0 or any other guessed value.
    """
    filler = _build_filler_text(probe_tokens, seed=CALIBRATION_SEED)
    result = _execute_streaming_request(
        base_url, alias, _single_user_message(filler), CALIBRATION_MAX_TOKENS, timeout=timeout
    )
    if not result["success"]:
        raise RuntimeError(
            f"calibrate_chars_per_token({alias!r}): calibration request failed: {result['error']}"
        )
    usage = result["usage"] or {}
    real_prompt_tokens = usage.get("prompt_tokens")
    if real_prompt_tokens is None:
        raise RuntimeError(
            f"calibrate_chars_per_token({alias!r}): response carried no usage.prompt_tokens"
        )
    return real_prompt_tokens / probe_tokens


def tokenize_count(base_url: str, text: str, timeout: float = 60) -> int:
    """Live-measures `text`'s real token count against `base_url`'s live llama-server via
    `POST /tokenize`, body `{"content": text}`.

    Verified premise (plan `bakeoff-transport-taxonomy-and-prompt-fit.md` WP-2 brief,
    orchestrator, live, 2026-08-24): this stack's `llama-server` -- both the published router
    and the bakeoff scratch server spawned directly on port 8899, the same binary -- answers
    that request with `{"tokens": [...]}`. Counts the raw text only, never the chat-templated
    prompt (callers absorb template + needle/question overhead with their own fixed margin, not
    by second-guessing this function).

    Body-probed, never status-code-probed (root `AGENTS.md` / `llama/AGENTS.md`: "probe by
    response body, never status code" -- the exact LM-Studio-style false-capable-response shape
    this repo has been burned by before, an HTTP 200 whose body doesn't carry what the caller
    assumed). A response that isn't a dict with a `tokens` list -- including a 200 with the
    wrong shape -- raises, it is never silently treated as "zero tokens" or any other guessed
    value.

    Stdlib urllib only, matching `_execute_streaming_request`'s transport conventions (no new
    deps). Raises (does not catch-and-default) on any transport failure or body-shape mismatch,
    mirroring `calibrate_chars_per_token`'s raise-don't-default posture just above -- a prompt-fit
    measurement that silently defaults to "it fits" is exactly the bug class this WP exists to
    close.
    """
    url = f"{base_url}/tokenize"
    payload = {"content": text}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"tokenize_count: request to {url} failed: {exc}") from exc
    try:
        obj = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tokenize_count: response from {url} was not valid JSON: {exc}") from exc
    tokens = obj.get("tokens") if isinstance(obj, dict) else None
    if not isinstance(tokens, list):
        raise RuntimeError(
            f"tokenize_count: response from {url} carried no `tokens` list (body-shape check, "
            f"probe by body never status code -- see this function's docstring) -- got {obj!r}"
        )
    return len(tokens)


# ---------------------------------------------------------------------------------------------
# Pinned-sampling arm presets (plan `quant-sweep-harness-and-instrument-v2.md`, Decisions locked:
# "Sampling is pinned everywhere: every correctness-bearing request carries explicit
# temperature/top_p/top_k/seed. Two standard arms: capability (temperature 0, seeded) and
# production (the model's own recommended sampling, seeded)."). Defined once, here.
# ---------------------------------------------------------------------------------------------

# Capability arm: deterministic-decoding shape, independent of which model is under test --
# temperature 0 is deterministic by construction, so top_p/top_k are pinned to no-op values
# (top_p=1.0 keeps the full distribution available before temperature-0 argmax selects one
# token; top_k=1 is belt-and-suspenders with the same effect). Numeric values are fixed.
SAMPLING_ARM_CAPABILITY: Dict[str, Optional[float]] = {"temperature": 0.0, "top_p": 1.0, "top_k": 1}

# Production arm: "the model's own recommended sampling" is a per-model fact (e.g. Gemma's
# card recommends ~temperature 1.0; a different model recommends something else) -- this harness
# has no per-model sampling-recommendation table, and building one is out of this WP's scope
# (schema + plumbing, not a research campaign). The preset below is therefore the ARM'S SHAPE
# only: `None` placeholders the caller (a later WP or campaign that already knows the specific
# model's recommended sampling) fills in before use. `seed` is the one field this WP fixes for
# both arms -- seeding is a harness policy, not a per-model one (see `sampling_arm_payload`).
SAMPLING_ARM_PRODUCTION: Dict[str, Optional[float]] = {"temperature": None, "top_p": None, "top_k": None}


def sampling_arm_payload(arm: Dict[str, Optional[float]], seed: int) -> Dict[str, Optional[float]]:
    """Merges a sampling-arm preset (`SAMPLING_ARM_CAPABILITY`/`SAMPLING_ARM_PRODUCTION`, or a
    caller-built dict with the production arm's `None` placeholders filled in) with an explicit
    seed -- the caller-supplied value, never a harness-chosen default (Decisions locked:
    "seeded"). Returns a plain `{"temperature": ..., "top_p": ..., "top_k": ..., "seed": ...}`
    dict whose keys match `_execute_streaming_request`'s sampling parameters exactly, suitable for
    `**sampling_arm_payload(...)`-style unpacking into `needle_probe`/`run_quality_corpus` calls.
    """
    payload = dict(arm)
    payload["seed"] = seed
    return payload


# ---------------------------------------------------------------------------------------------
# Needle-in-haystack correctness probe
# ---------------------------------------------------------------------------------------------

NEEDLE_MODERATE_TOKENS = 31000  # midpoint of the plan's ~29-33K "moderate" band
NEEDLE_CALIBRATION_TOLERANCE = 0.10  # +/-10% band around target_actual_tokens (plan WP-1)
NEEDLE_NEAR_CEILING_FRACTION = 0.85  # leaves headroom for the question + reasoning-model budget

_NEEDLE_WORDS = (
    "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GAMMA", "OMEGA", "SIGMA",
    "ZENITH", "VERTEX", "NOVA", "CIPHER", "QUASAR", "LUMEN",
)


def _generate_reference_code(rng: random.Random) -> str:
    """Matches this repo's own established needle-probe shape (docs/ling-3-tiny-bakeoff-results.md
    used e.g. "XK7-DELTA-9042", "ZQ3-OMEGA-7716")."""
    letters = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(2))
    digit = rng.choice("0123456789")
    word = rng.choice(_NEEDLE_WORDS)
    tail = "".join(rng.choice("0123456789") for _ in range(4))
    return f"{letters}{digit}-{word}-{tail}"


def needle_probe(
    base_url: str,
    alias: str,
    size: str,
    context_ceiling: Optional[int] = None,
    position_fraction: float = 0.45,
    max_tokens: int = 256,
    timeout: float = 300,
    seed: Optional[int] = None,
    target_actual_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[float] = None,
    sampler_seed: Optional[int] = None,
    sampling_arm: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Formalizes the needle-in-haystack correctness probe both prior Ling plans hand-rolled ad
    hoc (runs/dispatch/wp-1-report.md, runs/dispatch/wp-2-report.md from
    ling-3-tiny-context-ceiling; docs/ling-3-tiny-bakeoff-results.md has the worked pattern: a
    unique reference code embedded ~45% through a constructed filler haystack, the model asked
    to recall it at the end).

    `size`: "moderate" (~29-33K tokens, fixed) or "near_ceiling" (a fraction of `context_ceiling`,
    the target model's real context length -- required for this size, never a fixed number).

    A wrong-but-well-formed recall answer is a recorded finding (`correct: false`), never a hard
    failure this function raises -- the caller decides disposition (plan invariant, "Abort
    criteria corrected").

    Haystack generation is fully reproducible from `(seed, target_tokens, position_fraction)` --
    the haystack text itself is not persisted verbatim (it would dominate the 25 MiB experiment
    size budget at near-ceiling scale); the record keeps the generation parameters instead (this
    WP's own field-to-file split call, see README.md).

    `target_actual_tokens` (optional, plan WP-1): when given, bypasses the fixed
    `NEEDLE_NEAR_CEILING_FRACTION`-of-`context_ceiling` (or fixed `NEEDLE_MODERATE_TOKENS`)
    nominal-target computation entirely for this call. Instead, calls
    `calibrate_chars_per_token(base_url, alias)` to get this alias's real, live tokenizer ratio,
    and sets the nominal (character-count) build target to `target_actual_tokens / ratio`. The
    haystack is built and sent at that nominal size; if the real `usage.prompt_tokens` the server
    reports lands within +/-10% (`NEEDLE_CALIBRATION_TOLERANCE`) of `target_actual_tokens`, that
    attempt's result is used as-is. If not, the nominal target is rescaled once more from the
    *observed* ratio of that attempt and the probe is retried exactly once more -- never more than
    two total haystack-building requests for one `target_actual_tokens=...` call. The returned
    sample always carries `calibration_tolerance_met: bool` in this mode, reflecting whichever
    attempt was actually used (the second, if a retry happened) -- `true` only if that attempt's
    real `prompt_tokens` landed within the band, `false` otherwise (never silently claimed `true`
    when it wasn't met). This only changes what gets *requested*; `prompt_tokens` in the sample is
    always the real value read back from the server's own `usage` field for whichever attempt was
    used, exactly as in the non-calibrated path (honest-timing-semantics contract, unchanged).

    When `target_actual_tokens` is not given (the default, `None`), behavior is byte-identical to
    before this parameter existed -- the existing `"moderate"`/`"near_ceiling"` size-handling
    paths are untouched, and `calibration_tolerance_met` is absent from the returned sample.

    `temperature`/`top_p`/`top_k`/`sampler_seed`/`sampling_arm` (plan WP-1, "Pinned-sampling
    plumbing"): threaded straight through to `_execute_streaming_request` (`sampler_seed` maps to
    that function's `seed` parameter -- named differently here on purpose, see the deferred audit
    pin below). All default `None`, so calling this exactly as before is byte-identical --
    sampling stays unpinned unless a caller opts in. `sampling_arm` is a plain label
    (`"capability"`/`"production"`/`None`) recorded in the sample for provenance; it does not
    itself set temperature/top_p/top_k -- pass `**sampling_arm_payload(SAMPLING_ARM_CAPABILITY,
    seed)` (renaming that dict's `seed` key to `sampler_seed` first) plus
    `sampling_arm="capability"` to actually apply a preset.

    **Separate seed fields (deferred audit pin, `quant-sweep-harness-and-instrument-v2.md` WP-1
    execution brief)**: `seed` (this function's existing parameter, haystack generation/code
    selection) and `sampler_seed` (new, the sampler's own `seed` field on the wire) are always
    kept distinct in the returned sample -- `haystack_seed` and `sampler_seed` are two separate
    keys, never one shared value, even when a caller happens to pass the same integer for both.
    """
    calibrated = target_actual_tokens is not None
    if calibrated:
        ratio = calibrate_chars_per_token(base_url, alias)
        target_tokens = int(target_actual_tokens / ratio)
    elif size == "moderate":
        target_tokens = NEEDLE_MODERATE_TOKENS
    elif size == "near_ceiling":
        if not context_ceiling:
            raise ValueError(
                "needle_probe(size='near_ceiling') requires context_ceiling "
                "(the target model's real, verified context length)"
            )
        target_tokens = int(context_ceiling * NEEDLE_NEAR_CEILING_FRACTION)
    else:
        raise ValueError(f"unknown needle probe size: {size!r} (expected 'moderate' or 'near_ceiling')")

    effective_seed = seed if seed is not None else (hash((alias, size, context_ceiling)) & 0xFFFFFFFF)
    rng = random.Random(effective_seed)
    code = _generate_reference_code(rng)

    def _build_and_send(tt: int):
        haystack = _build_filler_text(tt, seed=rng.randint(0, 2**31 - 1))
        insert_at = int(len(haystack) * position_fraction)
        boundary = haystack.find(". ", insert_at)
        boundary = boundary + 2 if boundary != -1 else insert_at
        sentinel_sentence = f"Reference code for this section: {code}. "
        prompt = (
            haystack[:boundary] + sentinel_sentence + haystack[boundary:]
            + "\n\nQuestion: what is the exact reference code given above? "
              "Reply with only the code, no other text."
        )
        # sha256 of the assembled prompt text -- the haystack-by-reference field (plan Decisions
        # locked: "v2 records store haystacks by construction reference ... never the full prompt
        # text"). The full text itself is never persisted (see the module docstring above); this
        # is the one derived value from it that IS persisted, letting a later consumer confirm a
        # regenerated haystack (from seed_used/target_tokens/position_fraction) matches exactly.
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        result = _execute_streaming_request(
            base_url, alias, _single_user_message(prompt), max_tokens, timeout=timeout,
            temperature=temperature, top_p=top_p, top_k=top_k, seed=sampler_seed,
        )
        return prompt, prompt_sha256, result

    prompt, prompt_sha256, result = _build_and_send(target_tokens)

    calibration_tolerance_met: Optional[bool] = None
    if calibrated:
        actual_prompt_tokens = (result["usage"] or {}).get("prompt_tokens")
        tolerance_met = (
            actual_prompt_tokens is not None
            and abs(actual_prompt_tokens - target_actual_tokens)
            <= NEEDLE_CALIBRATION_TOLERANCE * target_actual_tokens
        )
        if not tolerance_met and actual_prompt_tokens:
            target_tokens = int(target_tokens * target_actual_tokens / actual_prompt_tokens)
            prompt, prompt_sha256, result = _build_and_send(target_tokens)
            actual_prompt_tokens = (result["usage"] or {}).get("prompt_tokens")
            tolerance_met = (
                actual_prompt_tokens is not None
                and abs(actual_prompt_tokens - target_actual_tokens)
                <= NEEDLE_CALIBRATION_TOLERANCE * target_actual_tokens
            )
        calibration_tolerance_met = bool(tolerance_met)

    usage = result["usage"] or {}
    completion_text = result["content_text"] or ""
    found = bool(completion_text) and code in completion_text

    sample = {
        "type": "needle_probe",
        "schema_version": 2,
        "size": size,
        "target_tokens": target_tokens,
        "context_ceiling": context_ceiling,
        "position_fraction": position_fraction,
        "seed_used": effective_seed,
        # Separate seed fields (deferred audit pin -- see needle_probe's docstring): haystack_seed
        # is this same effective_seed value under its v2 field name; sampler_seed is the distinct
        # wire-level sampler seed, never folded into one shared field even when a caller passes
        # the same integer for both.
        "haystack_seed": effective_seed,
        "sampler_seed": sampler_seed,
        "sampling_arm": sampling_arm,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        # Haystack-by-reference fields (plan Decisions locked): the full haystack text is never
        # stored (see this function's docstring); `haystack_sha256` plus (seed_used, target_tokens,
        # position_fraction) above are what a later consumer needs to reproduce and verify it.
        # `corpus_manifest_id` is `null` here by design -- this probe still uses the old word-soup
        # timing-fixture-derived haystack (`_build_filler_text`), not WP-3's real coherent corpus,
        # which is the only haystack source that has a manifest id.
        "corpus_manifest_id": None,
        "haystack_sha256": prompt_sha256,
        # Per-trial classifier verdict (nullable now -- plan Deferred audit item: "the summary
        # field is always present, null when no replicated correctness cells exist, never
        # omitted"; the classifier itself lands in WP-3, this WP only reserves the field).
        "classifier_verdict": None,
        "prompt_tokens": usage.get("prompt_tokens"),
        "expected_code": code,
        "correct": found,
        "success": result["success"],
        "error": result["error"],
        "finish_reason": result["finish_reason"],
        "wall_seconds": result["wall_seconds"],
        "timestamp": result["t0_iso"],
    }
    if calibrated:
        sample["target_actual_tokens"] = target_actual_tokens
        sample["calibration_tolerance_met"] = calibration_tolerance_met
    completion = {
        "type": "needle_probe",
        "size": size,
        "expected_code": code,
        "content_text": result["content_text"],
        "reasoning_text": result["reasoning_text"],
        "finish_reason": result["finish_reason"],
    }
    return sample, completion


# ---------------------------------------------------------------------------------------------
# Router log capture (router-managed aliases only -- plan qwen38-27b-kv-cache-quantization.md,
# WP-1)
# ---------------------------------------------------------------------------------------------


def fetch_router_logs(
    router_base_url: str = DEFAULT_ROUTER_BASE_URL,
    tail_bytes: Optional[int] = None,
    timeout: float = 30,
) -> str:
    """Fetches llama-swap's own captured stdout/stderr log ring buffer: a plain
    `GET {router_base_url}/logs` (confirmed live and returning `200` this session; no `/v1`
    prefix -- see DEFAULT_ROUTER_BASE_URL's own comment for why this is a separate constant from
    DEFAULT_BASE_URL, which is `/v1`-suffixed for OpenAI-compatible chat calls and would produce
    the wrong path, `.../v1/logs`, if reused here).

    This captures stdout/stderr **only for children llama-swap itself spawned** -- it is the
    right mechanism for verifying a router-managed alias's real startup output (e.g. confirming a
    landed KV-cache-type / Flash-Attention flag actually took effect on the live process, not just
    in the config that requested it). It captures **nothing** for a bare scratch `llama-server`
    process launched directly, outside llama-swap -- that is a different WP's concern
    (plans/qwen38-27b-kv-cache-quantization.md WP-2, which verifies its scratch server via its own
    redirected process log instead, not this function). Do not wire this up against a scratch
    server expecting it to see anything.

    Returns the raw response text verbatim -- no parsing. `tail_bytes`, if given, trims the
    returned string client-side to at most its last `tail_bytes` bytes (decoded) -- this endpoint
    is a ring buffer of indeterminate/unbounded total length, so this is a client-side safety net
    for a large buffer, not a request for the server to do the trimming.
    """
    url = f"{router_base_url}/logs"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if tail_bytes is not None and len(raw) > tail_bytes:
        raw = raw[-tail_bytes:]
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------------------------
# Quality corpus
# ---------------------------------------------------------------------------------------------


def load_quality_corpus(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = Path(path) if path else QUALITY_CORPUS_PATH
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def run_quality_corpus(
    base_url: str,
    alias: str,
    corpus: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 768,
    timeout: float = 180,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[float] = None,
    sampler_seed: Optional[int] = None,
    sampling_arm: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Runs every fixed quality-corpus prompt once against `alias`. No automated judge --
    per Decisions locked, scoring stays manual/raw-capture this round; the per-answer rubric
    fields are written as `null` placeholders for a human (or a later WP) to fill in.

    `temperature`/`top_p`/`top_k`/`sampler_seed`/`sampling_arm` (plan WP-1, "Pinned-sampling
    plumbing"): threaded straight through to `_execute_streaming_request` for every corpus entry
    (the whole corpus shares one sampling configuration per call -- per-entry overrides are not
    needed here, unlike `needle_probe`'s per-call haystack seed). All default `None`; calling this
    exactly as before is byte-identical. `sampling_arm` is a plain provenance label, same
    semantics as in `needle_probe`.
    """
    if corpus is None:
        corpus = load_quality_corpus()

    samples: List[Dict[str, Any]] = []
    completions: List[Dict[str, Any]] = []
    for entry in corpus:
        messages = entry.get("messages")
        if not messages:
            messages = _single_user_message(entry["prompt"])
        result = _execute_streaming_request(
            base_url, alias, messages, max_tokens, timeout=timeout,
            temperature=temperature, top_p=top_p, top_k=top_k, seed=sampler_seed,
        )
        usage = result["usage"] or {}

        sentinel_correct = None
        if entry.get("sentinel") and entry.get("expected_answer"):
            body = (result["content_text"] or "").strip()
            sentinel_correct = entry["expected_answer"] in body

        sample = {
            "type": "quality_corpus_result",
            "schema_version": 2,
            "id": entry["id"],
            "category": entry["category"],
            "sentinel": entry.get("sentinel", False),
            "sentinel_correct": sentinel_correct,
            "sampler_seed": sampler_seed,
            "sampling_arm": sampling_arm,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "finish_reason": result["finish_reason"],
            "wall_seconds": result["wall_seconds"],
            "success": result["success"],
            "error": result["error"],
            # Manual-scoring placeholders (Decisions locked: no automated judge this round).
            "correctness": None,
            "instruction_following": None,
            "completeness": None,
            "reasoning": None,
            "critical_error": None,
            "pairwise_preference": None,
        }
        completion = {
            "type": "quality_corpus_result",
            "id": entry["id"],
            "category": entry["category"],
            "content_text": result["content_text"],
            "reasoning_text": result["reasoning_text"],
            "finish_reason": result["finish_reason"],
        }
        samples.append(sample)
        completions.append(completion)
    return samples, completions


# ---------------------------------------------------------------------------------------------
# JSON-per-experiment record writer
# ---------------------------------------------------------------------------------------------


class SizeBudgetExceeded(Exception):
    """Raised by `write_experiment_record` when the projected on-disk size of
    `llama/bakeoff/records/<experiment-id>/` exceeds `SIZE_LIMIT_BYTES`, computed BEFORE anything is
    written to disk. Cycle-2 audit finding (material) -- a hold-for-user signal, never a silent
    over-budget commit."""

    def __init__(self, total_bytes: int, limit_bytes: int):
        super().__init__(
            f"projected llama/bakeoff/records/<experiment-id>/ size {total_bytes} bytes exceeds the "
            f"{limit_bytes}-byte (25 MiB) budget -- hold for user before writing anything"
        )
        self.total_bytes = total_bytes
        self.limit_bytes = limit_bytes


def _jsonl(records: List[Dict[str, Any]]) -> str:
    if not records:
        return ""
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


def write_experiment_record(
    experiment_id: str,
    config: Dict[str, Any],
    prompts: List[Dict[str, Any]],
    completions: List[Dict[str, Any]],
    samples: List[Dict[str, Any]],
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Writes `llama/bakeoff/records/<experiment-id>/{config.json,samples.jsonl,prompts.jsonl,
    completions.jsonl}`.

    Field-to-file split (this WP's own call -- the plan pins only the four-file split itself):
      - config.json: run-level metadata + the artifact-lineage field set (REQUIRED_LINEAGE_FIELDS)
      - prompts.jsonl: the quality-corpus prompt text (one line per corpus entry)
      - completions.jsonl: every raw completion verbatim (timing rounds, needle probes, quality
        corpus answers) -- never truncated or summarized
      - samples.jsonl: numeric timing/sample statistics per round, plus needle-probe pass/fail
        and cache-assertion results, plus quality-corpus per-answer records (scores as null
        placeholders this round)

    Requires `config["lineage"]` to contain all of REQUIRED_LINEAGE_FIELDS (values may be `None`
    if genuinely unresolvable -- e.g. `hf_revision` when only a ref string, not a commit SHA, is
    known -- but every key must be present).

    Computes the total projected on-disk size BEFORE writing anything; raises
    `SizeBudgetExceeded` instead of writing an over-budget experiment directory.

    **Schema v2** (plan `quant-sweep-harness-and-instrument-v2.md` WP-1: "Record schema v2 in the
    producer, from the start ... every record this plan's WPs write is v2-conformant from day
    one"; frozen and documented as the versioned DB contract by WP-4, not here). Every call
    unconditionally stamps `config["schema_version"] = 2` -- this producer no longer emits v1
    records (pre-existing v1 records on disk are untouched; there is no version toggle). Two
    record-level fields are guaranteed present via `setdefault` (so a caller that doesn't know
    about them yet, e.g. a non-sweep `cmd_full` invocation, still gets a v2-conformant record):
      - `config["sweep_cell"]`: the sweep-cell identity (cell_id/alias/server_flags snapshot) a
        sweep-driven caller supplies directly in `config` before calling this; `None` for a record
        this WP-1's own CLI (`full`) writes outside a sweep run.
      - `config["wilson_summary"]`: **deferred audit item, pinned nullable-with-explicit-null**
        (this WP's execution brief) -- always present as a key, `None` whenever the record has no
        replicated correctness cells (n>=10 trials) to summarize with a Wilson interval. Never
        omitted, and never silently absent.
    Per-trial classifier-verdict and sampling-arm fields are NOT this function's concern -- they
    live on the individual `samples` entries (`needle_probe`/`run_quality_corpus` already write
    them per-sample; this function writes `samples` through to `samples.jsonl` unchanged).
    """
    base_dir = Path(base_dir) if base_dir else DEFAULT_RUNS_DIR
    lineage = config.get("lineage") or {}
    missing = [k for k in REQUIRED_LINEAGE_FIELDS if k not in lineage]
    if missing:
        raise ValueError(f"config['lineage'] is missing required field(s): {missing}")

    config = dict(config)
    config.setdefault("experiment_id", experiment_id)
    config.setdefault("timestamp", _utc_now_iso())
    config["schema_version"] = 2
    config.setdefault("sweep_cell", None)
    config.setdefault("wilson_summary", None)

    prompts_text = _jsonl(prompts)
    completions_text = _jsonl(completions)
    samples_text = _jsonl(samples)

    # size_check describes its own file's contribution; computed against a placeholder-sized
    # provisional serialization first, then baked into the real config.json (the byte-count
    # of the placeholder vs. the final integer differs by at most a few bytes -- negligible
    # against the 25 MiB budget).
    provisional = dict(config)
    provisional["size_check"] = {
        "total_bytes": 0, "limit_bytes": SIZE_LIMIT_BYTES, "within_budget": True,
    }
    provisional_text = json.dumps(provisional, indent=2, ensure_ascii=False) + "\n"
    total_bytes = sum(
        len(t.encode("utf-8"))
        for t in (provisional_text, prompts_text, completions_text, samples_text)
    )
    within_budget = total_bytes <= SIZE_LIMIT_BYTES

    config["size_check"] = {
        "total_bytes": total_bytes, "limit_bytes": SIZE_LIMIT_BYTES, "within_budget": within_budget,
    }
    config_text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"

    if not within_budget:
        raise SizeBudgetExceeded(total_bytes, SIZE_LIMIT_BYTES)

    exp_dir = base_dir / experiment_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "config.json").write_text(config_text, encoding="utf-8")
    (exp_dir / "prompts.jsonl").write_text(prompts_text, encoding="utf-8")
    (exp_dir / "completions.jsonl").write_text(completions_text, encoding="utf-8")
    (exp_dir / "samples.jsonl").write_text(samples_text, encoding="utf-8")

    actual_bytes = sum(
        (exp_dir / name).stat().st_size
        for name in ("config.json", "prompts.jsonl", "completions.jsonl", "samples.jsonl")
    )
    return {
        "experiment_dir": str(exp_dir),
        "projected_bytes": total_bytes,
        "actual_bytes": actual_bytes,
        "limit_bytes": SIZE_LIMIT_BYTES,
    }


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--alias", required=True, help="already-configured router alias to test")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default: {DEFAULT_BASE_URL}")
    parser.add_argument("--timeout", type=float, default=180, help="per-request socket timeout (s)")


def _lineage_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    lineage: Dict[str, Any] = {
        "hf_repo": getattr(args, "hf_repo", None),
        "hf_revision": getattr(args, "hf_revision", None) or "unknown",
        "llama_cpp_build": resolve_llama_cpp_build(),
        "filename": None,
        "file_size": None,
        "sha256": None,
    }
    gguf_host_path = getattr(args, "gguf_host_path", None)
    if gguf_host_path:
        lineage.update(compute_file_lineage(Path(gguf_host_path)))
    elif getattr(args, "filename", None):
        lineage["filename"] = args.filename
    return lineage


def cmd_sweep(args: argparse.Namespace) -> int:
    samples, completions, summary = run_sweep(
        args.base_url, args.alias,
        cold_rounds=args.cold, warm_rounds=args.warm, hot_rounds=args.hot,
        input_tokens_target=args.input_tokens, output_tokens=args.output_tokens,
        timeout=args.timeout,
    )
    print(json.dumps({"summary": summary, "samples": samples}, indent=2, default=str))
    return 0


def cmd_needle(args: argparse.Namespace) -> int:
    sample, completion = needle_probe(
        args.base_url, args.alias, args.size,
        context_ceiling=args.context_ceiling, position_fraction=args.position_fraction,
        max_tokens=args.max_tokens, timeout=args.timeout, seed=args.seed,
    )
    print(json.dumps({"sample": sample, "completion": completion}, indent=2, default=str))
    return 0 if sample["correct"] else 1


def cmd_quality(args: argparse.Namespace) -> int:
    corpus = load_quality_corpus(Path(args.corpus)) if args.corpus else None
    samples, completions = run_quality_corpus(
        args.base_url, args.alias, corpus=corpus, max_tokens=args.max_tokens, timeout=args.timeout
    )
    print(json.dumps({"samples": samples}, indent=2, default=str))
    return 0


def cmd_full(args: argparse.Namespace) -> int:
    base_url, alias = args.base_url, args.alias
    all_samples: List[Dict[str, Any]] = []
    all_completions: List[Dict[str, Any]] = []
    notes: List[str] = list(args.notes or [])

    sweep_samples, sweep_completions, sweep_summary = run_sweep(
        base_url, alias,
        cold_rounds=args.cold, warm_rounds=args.warm, hot_rounds=args.hot,
        input_tokens_target=args.input_tokens, output_tokens=args.output_tokens,
        timeout=args.timeout,
    )
    all_samples.extend(sweep_samples)
    all_completions.extend(sweep_completions)

    cold_load_observed = any(
        s.get("model_load_seconds") is not None
        for s in sweep_samples if s["run_state"] == "cold"
    )
    if not cold_load_observed:
        notes.append(
            "model_load_seconds: no cold round observed a load-ready transition this run "
            "(poller saw the alias already resident, or the transition wasn't caught) -- field "
            "is null per the honest-timing-semantics contract, not fabricated."
        )

    if not args.skip_needle:
        s, c = needle_probe(base_url, alias, "moderate", timeout=max(args.timeout, 300), seed=args.needle_seed)
        all_samples.append(s)
        all_completions.append(c)
        if args.context_ceiling:
            s, c = needle_probe(
                base_url, alias, "near_ceiling", context_ceiling=args.context_ceiling,
                timeout=max(args.timeout, 600), seed=args.needle_seed,
            )
            all_samples.append(s)
            all_completions.append(c)

    prompts: List[Dict[str, Any]] = []
    if not args.skip_quality:
        corpus = load_quality_corpus(Path(args.corpus)) if args.corpus else load_quality_corpus()
        prompts = corpus
        q_samples, q_completions = run_quality_corpus(
            base_url, alias, corpus=corpus, max_tokens=args.quality_max_tokens, timeout=args.timeout
        )
        all_samples.extend(q_samples)
        all_completions.extend(q_completions)

    lineage = _lineage_from_args(args)
    config = {
        "alias": alias,
        "base_url": base_url,
        "lineage": lineage,
        "configuration": {
            "fixture_input_tokens_target": args.input_tokens,
            "fixture_output_tokens": args.output_tokens,
            "cold_rounds": args.cold, "warm_rounds": args.warm, "hot_rounds": args.hot,
            "needle_context_ceiling": args.context_ceiling,
        },
        "timing_summary": sweep_summary,
        "status": args.status,
        "notes": notes,
    }
    result = write_experiment_record(
        args.experiment_id, config, prompts, all_completions, all_samples,
        base_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(json.dumps(result, indent=2))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_harness.py",
        description="Serving-harness bake-off tooling (plans/serving-harness-round-2.md WP-1). "
        "See llama/bakeoff/README.md for usage examples.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sweep = sub.add_parser("sweep", help="run a cold/warm/hot timing sweep, print JSON to stdout")
    _add_common_args(p_sweep)
    p_sweep.add_argument("--cold", type=int, default=3)
    p_sweep.add_argument("--warm", type=int, default=3)
    p_sweep.add_argument("--hot", type=int, default=3)
    p_sweep.add_argument("--input-tokens", type=int, default=FIXTURE_INPUT_TOKENS_TARGET)
    p_sweep.add_argument("--output-tokens", type=int, default=FIXTURE_OUTPUT_TOKENS)
    p_sweep.set_defaults(func=cmd_sweep)

    p_needle = sub.add_parser("needle", help="run one needle-in-haystack correctness probe")
    _add_common_args(p_needle)
    p_needle.add_argument("--size", choices=("moderate", "near_ceiling"), required=True)
    p_needle.add_argument("--context-ceiling", type=int, default=None)
    p_needle.add_argument("--position-fraction", type=float, default=0.45)
    p_needle.add_argument("--max-tokens", type=int, default=256)
    p_needle.add_argument("--seed", type=int, default=None)
    p_needle.set_defaults(func=cmd_needle)

    p_quality = sub.add_parser("quality", help="run the fixed quality corpus once, print JSON")
    _add_common_args(p_quality)
    p_quality.add_argument("--corpus", default=None, help=f"default: {QUALITY_CORPUS_PATH}")
    p_quality.add_argument("--max-tokens", type=int, default=768)
    p_quality.set_defaults(func=cmd_quality)

    p_full = sub.add_parser(
        "full", help="sweep + needle probe(s) + quality corpus, then write a JSON experiment record"
    )
    _add_common_args(p_full)
    p_full.add_argument("--experiment-id", required=True, help="<model-slug>-<quant>-<config-tag>")
    p_full.add_argument("--cold", type=int, default=3)
    p_full.add_argument("--warm", type=int, default=3)
    p_full.add_argument("--hot", type=int, default=3)
    p_full.add_argument("--input-tokens", type=int, default=FIXTURE_INPUT_TOKENS_TARGET)
    p_full.add_argument("--output-tokens", type=int, default=FIXTURE_OUTPUT_TOKENS)
    p_full.add_argument("--context-ceiling", type=int, default=None)
    p_full.add_argument("--needle-seed", type=int, default=None)
    p_full.add_argument("--skip-needle", action="store_true")
    p_full.add_argument("--corpus", default=None)
    p_full.add_argument("--quality-max-tokens", type=int, default=768)
    p_full.add_argument("--skip-quality", action="store_true")
    p_full.add_argument("--hf-repo", default=None)
    p_full.add_argument("--hf-revision", default=None)
    p_full.add_argument("--gguf-host-path", default=None, help="real host filesystem path to the GGUF, for sha256/file_size lineage")
    p_full.add_argument("--filename", default=None, help="fallback filename if --gguf-host-path is not given")
    p_full.add_argument("--output-dir", default=None, help=f"default: {DEFAULT_RUNS_DIR}")
    p_full.add_argument("--status", default="captured")
    p_full.add_argument("--notes", action="append", default=None)
    p_full.set_defaults(func=cmd_full)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
