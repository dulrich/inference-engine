#!/usr/bin/env python3
"""Streaming HTTP transport, residency polling, timing rounds, and live tokenizer probes
(WP-R2 of `plans/repository-tooling-structural-remediation.md`; review finding F7).

Everything in the harness that speaks HTTP to a llama-server lives here: the SSE streaming
`/v1/chat/completions` call and its independent per-delta timestamps, the background
`ResidencyPoller` behind `model_load_seconds`, the cold/warm/hot timing-round driver built on
top of both, and the two live tokenizer probes (`calibrate_chars_per_token`, `tokenize_count`).

**The honest-timing-semantics contract is unchanged and still lives with the code that
implements it** (see `run_harness.py`'s module docstring for the full statement):
`model_load_seconds` is populated only from an actually-observed load-ready transition, never
fabricated; `first_request_e2e_ttft_ms` is always populated; a round that fails its own
cache-state assertion is flagged, never folded into an aggregate.

`tokenize_count` stays body-probed, never status-code-probed (root `AGENTS.md` / `llama/
AGENTS.md`: "probe by response body, never status code").

Every public name here is re-exported from `run_harness` at its historical home. That matters
beyond tidiness: this repo's tests reach the transport seam by rebinding
`run_harness._execute_streaming_request` (`tests/test_run_cell.py`) and
`run_harness.tokenize_count` (`tests/test_batch.py`), and the live callers in `longcontext_cell`
/ `cell_runner` resolve those names through the `run_harness` module object at CALL time -- so
the injected-fake idiom keeps working across the split, unchanged.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Same "no package `__init__.py` under `llama/`" gotcha every other module here documents: a
# plain top-level import works because the importer has already put this directory on sys.path.
from harness_fixture import (
    FIXTURE_INPUT_TOKENS_TARGET,
    FIXTURE_INSTRUCTION,
    FIXTURE_OUTPUT_TOKENS,
    FIXTURE_PROMPT_TEXT,
    FIXTURE_SEED,
    _build_filler_text,
    _single_user_message,
    _utc_now_iso,
    summarize,
)

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
