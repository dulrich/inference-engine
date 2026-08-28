#!/usr/bin/env python3
"""Serving-harness bake-off tooling (plans/serving-harness-round-2.md, WP-1).

Reusable measurement tooling for any already-configured router alias served by
inference-engine-llama (llama-swap + llama.cpp, OpenAI-compatible `/v1/chat/completions`).
WP-2 through WP-5 each point this at a different alias -- there is no alias-specific code path
anywhere in this module.

Zero new runtime dependencies: Python 3 stdlib only (urllib, json, time, statistics, hashlib,
threading, argparse). No `requests`, no venv, no `requirements.txt`.

**Module layout (WP-R2 of `plans/repository-tooling-structural-remediation.md`, review finding
F7).** This file was 1454 lines; it is now the harness's probe/corpus/lineage layer plus the
public surface every caller outside this directory has always reached for as `run_harness.<name>`.
The four extracted modules are, in dependency order:

  - `harness_fixture.py` -- paths, shared constants, and the fixed performance fixture
  - `harness_transport.py` -- streaming HTTP, `ResidencyPoller`, timing rounds, tokenizer probes
  - `harness_record.py` -- the frozen schema-v2 experiment-record writer
  - `harness_cli.py` -- the argparse surface (imports this module; imported back only from the
    `__main__` guard at the bottom, so the dependency graph stays acyclic)

Everything they own is re-exported below at its historical `run_harness.` name. Nothing about
the wire shape, the record bytes, the CLI, or any deterministic result changed.

Honest-timing-semantics contract (hard invariant, see README.md; implemented in
`harness_transport.py`):
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

Three run states (cold/warm/hot) share one fixed performance fixture (constants
`FIXTURE_PROMPT_TEXT` / `FIXTURE_OUTPUT_TOKENS`, built once at import time in
`harness_fixture.py` from a fixed-seed deterministic filler generator -- not re-derived per
call). See README.md for the warm/hot cache-priming design (why hot fires an explicit,
unrecorded priming call rather than depending on execution order).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# -- re-exports: the public surface `batch.py`, `cell_record.py`, `cell_runner.py`,
# -- `longcontext*.py`, `verify_run.py`, `sampling_table.py`, and every `tests/test_*.py` have
# -- always reached for as `run_harness.<name>`. Kept at their historical home so the WP-R2 split
# -- is invisible to every caller outside this directory. Two of them are load-bearing beyond
# -- convenience: the test suite rebinds `run_harness._execute_streaming_request` and
# -- `run_harness.tokenize_count` as injected fakes, and every live caller resolves those through
# -- this module object at call time.
from harness_fixture import (  # noqa: F401
    CHARS_PER_TOKEN_ESTIMATE, DEFAULT_BASE_URL, DEFAULT_ROUTER_BASE_URL, DEFAULT_RUNS_DIR,
    DOCKERFILE_PATH, FALLBACK_LLAMA_CPP_BUILD, FIXTURE_INPUT_TOKENS_TARGET, FIXTURE_INSTRUCTION,
    FIXTURE_OUTPUT_TOKENS, FIXTURE_PROMPT_BODY, FIXTURE_PROMPT_TEXT, FIXTURE_SEED, LLAMA_DIR,
    MODULE_DIR, QUALITY_CORPUS_PATH, REPO_ROOT, REQUIRED_LINEAGE_FIELDS, SIZE_LIMIT_BYTES,
    _build_filler_text, _single_user_message, _utc_now_iso, _WORD_BANK, summarize,
)
from harness_record import (  # noqa: F401
    SizeBudgetExceeded, _jsonl, write_experiment_record,
)
from harness_transport import (  # noqa: F401
    CALIBRATION_MAX_TOKENS, CALIBRATION_PROBE_TOKENS, CALIBRATION_SEED, ResidencyPoller,
    _execute_streaming_request, _novel_variant, _post_stream, _summarize_sweep,
    calibrate_chars_per_token, run_round, run_sweep, tokenize_count,
)


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


if __name__ == "__main__":
    # Running this file directly makes it `__main__`. `harness_cli` imports `run_harness` by
    # name, which would otherwise execute this module a SECOND time (rebuilding
    # FIXTURE_PROMPT_TEXT and handing the CLI a different module object than the one the user is
    # running). Registering this module under its own name first makes the two the same object.
    sys.modules.setdefault("run_harness", sys.modules[__name__])
    from harness_cli import main

    sys.exit(main())
