#!/usr/bin/env python3
"""Per-model-family recommended "production" sampling table
(`plans/longcontext-pow2-overnight-campaign.md`, WP-2).

`run_harness.py`'s `SAMPLING_ARM_PRODUCTION` is deliberately a `None`-placeholder SHAPE (that
module's own docstring, ~lines 702-709): "the model's own recommended sampling" is a per-model
fact that module has no table for. This module IS that table -- `production_sampling(family)`
returns the researched `{temperature, top_p, top_k}` triple for a GGUF `general.architecture`
family string, sourced from each family's own HF model card (quoted verbatim below, one citation
comment per entry). The values here are then threaded through `run_harness.sampling_arm_payload`
by a live caller (`longcontext.run_live_battery` / `batch.make_longcontext_cell_runner`) exactly
as that function's existing contract expects -- this module never mutates
`run_harness.SAMPLING_ARM_PRODUCTION` itself, and `run_harness.py` is not touched by this WP.

**Do not invent values.** A family whose card is silent on any one of the three required fields
gets NO entry here -- `production_sampling` raises `MissingProductionSamplingError` (a `KeyError`
subclass) rather than guess, and the caller falls back to the `capability` arm for that family,
recording the gap (plan Assumptions: "where a card is silent, the family falls back to
capability-arm-only"; audit M3 disposition: "both sampling arms only for families with a complete
`sampling_table.py` production entry, capability-only otherwise"). `nemotron_h` is exactly this
case below -- its card states temperature/top_p per use-case but never states top_k, so it is
deliberately omitted rather than filled with a guessed/server-default top_k.

Zero new runtime dependencies: stdlib-only (a plain `Dict[str, Dict[str, float]]` table), matching
every other module in this package.

Consumer-facing sibling: `metadata.sampling` in `llama/config.yaml` (`plans/overnight-test-matrix-
redesign.md` WP-1) is the same research surfaced to `/v1/models` consumers (Open WebUI,
obsidian-crucible), spec'd in `docs/model-capabilities.md`. It is per-key optional rather than this
module's all-or-nothing triple (nemotron-4b there carries `temperature`/`top_p` with no `top_k`,
matching this module's own do-not-invent omission), and it also carries `min_p`/`presence_penalty`/
`repetition_penalty`/`source` keys this module's interface does not model. Same-edit drift rule:
for the keys both homes carry (`temperature`, `top_p`, `top_k`), an edit to either home's value
updates the other in the same change; a key only one home carries is not drift.
"""

from __future__ import annotations


class MissingProductionSamplingError(KeyError):
    """Raised by `production_sampling()` when `model_family` has no researched, complete
    (temperature + top_p + top_k) HF-model-card recommendation -- either because the family's
    card was never researched, or because it was researched and found genuinely silent on one of
    the three required fields (see `nemotron_h` below). Callers must fall back to the
    `capability` sampling arm for this family and record the gap; this module never invents a
    value to avoid the fallback."""


# ---------------------------------------------------------------------------------------------
# Researched production-sampling table, keyed by GGUF `general.architecture` (the same literal
# string `sweep.classify_fit(...)["architecture"]` reports, and the same family names the plan's
# Key Changes section names: "gemma4, qwen35/qwen35moe (Qwen3.x), bailingmoe3 (Ling), nemotron_h").
# Every entry below carries its own source citation (URL + quoted card text) directly above it --
# researched live 2026-08-23 (WebSearch/WebFetch, doc research, no live-service operation).
# ---------------------------------------------------------------------------------------------

_PRODUCTION_SAMPLING: dict[str, dict[str, float]] = {
    # gemma4 -- google/gemma-4-12B-it, "Best Practices" section 1 "Sampling Parameters":
    # "Use the following standardized sampling configuration across all use cases:
    #  temperature=1.0, top_p=0.95, top_k=64"
    # https://huggingface.co/google/gemma-4-12B-it
    # One standardized configuration for every use case (no thinking/non-thinking split) --
    # used as-is, no interpretive choice needed.
    "gemma4": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},

    # qwen35 -- Qwen/Qwen3.8-27B, "Best Practices" -> "Sampling Parameters", Thinking Mode
    # (general tasks): "temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0,
    #  repetition_penalty=1.0"
    # https://huggingface.co/Qwen/Qwen3.8-27B
    # The card also documents a distinct Non-Thinking (Instruct) mode
    # (temperature=0.7, top_p=0.80, top_k=20) and a Thinking-mode "precise coding" variant
    # (temperature=0.6). Thinking-mode/general-task values are used here: `llama/config.yaml`
    # carries no `enable_thinking`/no-think override for either qwen35 or qwen35moe alias, so
    # these models run in their GGUF chat template's DEFAULT mode (thinking, for the Qwen3.x
    # family) against this harness's plain single-turn completion requests -- this is an
    # interpretive choice between two officially-documented options, not an invented value; both
    # quoted values are real card text, and the non-chosen mode is recorded here for a future
    # caller that DOES toggle thinking explicitly.
    "qwen35": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},

    # qwen35moe -- Qwen/Qwen3.6-35B-A3B, "Best Practices" -> "Sampling Parameters", Thinking Mode
    # (general tasks): "temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5,
    #  repetition_penalty=1.0"
    # https://huggingface.co/Qwen/Qwen3.6-35B-A3B
    # Same thinking-mode-default rationale as qwen35 above (its own card, not borrowed from the
    # dense qwen35 sibling) -- the temperature/top_p/top_k triple matches qwen35's exactly, only
    # `presence_penalty` (not part of this table's interface) differs between the two cards.
    "qwen35moe": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},

    # bailingmoe3 -- inclusionAI/Ling-3.0-tiny (the campaign's own on-disk Ling quant is
    # Ling-3.0-tiny Q4_K_M): "The recommended sampling parameters for Ling-3.0-tiny are
    # `temperature=1.0`, `top_p=0.95`, and `top_k=20`."
    # https://huggingface.co/inclusionAI/Ling-3.0-tiny
    "bailingmoe3": {"temperature": 1.0, "top_p": 0.95, "top_k": 20},

    # nemotron_h -- DELIBERATELY OMITTED. nvidia/NVIDIA-Nemotron-3-Nano-4B-BF16's card states:
    # "temperature=1.0 and top_p=0.95 are recommended for reasoning tasks" and "temperature=0.6
    # and top_p=0.95 are recommended for tool calling" (https://huggingface.co/nvidia/
    # NVIDIA-Nemotron-3-Nano-4B-BF16) -- but NEVER states a top_k value for either use case. This
    # module's interface requires the full {temperature, top_p, top_k} triple (plan Public
    # Interfaces); filling top_k with a guessed/server-default value would violate "do not invent
    # values" (plan Assumptions), so this family has NO entry -- `production_sampling("nemotron_h")`
    # raises `MissingProductionSamplingError`, and the caller falls back to `capability` for it
    # (audit M3: "capability-only otherwise").
}


def production_sampling(model_family: str) -> dict[str, float]:
    """Returns a fresh `{"temperature": float, "top_p": float, "top_k": int}` dict for
    `model_family` (a GGUF `general.architecture` string -- `gemma4`, `qwen35`, `qwen35moe`, or
    `bailingmoe3`; see the module-level table above for each entry's source citation).

    Raises `MissingProductionSamplingError` (a `KeyError` subclass) for any family with no
    complete researched entry -- `nemotron_h` today, and any unrecognized family string. Never
    guesses/invents a value; the caller is responsible for falling back to the `capability`
    sampling arm and recording the gap (plan Assumptions / audit M3).
    """
    if model_family not in _PRODUCTION_SAMPLING:
        raise MissingProductionSamplingError(
            f"production_sampling: no complete researched HF-model-card sampling recommendation "
            f"for model_family {model_family!r} -- either this family's card is genuinely silent "
            f"on temperature/top_p/top_k (see this module's nemotron_h comment for the documented "
            f"example), or {model_family!r} is not one of the campaign's known families "
            f"{sorted(_PRODUCTION_SAMPLING)}. The caller must fall back to the 'capability' "
            f"sampling arm for this family, never invent a value."
        )
    return dict(_PRODUCTION_SAMPLING[model_family])


def known_families() -> dict[str, dict[str, float]]:
    """Read-only snapshot of every family this table has a complete entry for -- convenience for
    a caller (or a queue author) deciding up front which families support the `production` arm at
    all, without having to catch `MissingProductionSamplingError` per family (audit M3: a queue
    should only emit `production` cells for families with a complete entry)."""
    return {family: dict(values) for family, values in _PRODUCTION_SAMPLING.items()}
