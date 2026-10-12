#!/usr/bin/env python3
"""`run_harness.py`'s argparse surface and command composition (WP-R2 of
`plans/repository-tooling-structural-remediation.md`; review finding F7).

Pure composition: every subcommand (`sweep` / `needle` / `quality` / `full`) wires already-built
measurement functions together and prints their JSON. No measurement logic lives here.

**The CLI contract is unchanged.** Same `prog`, same four subcommands, same flags, same
defaults, same stdout JSON shape, same exit codes (`needle` still exits 1 on a wrong recall;
everything else exits 0).

This module sits at the TOP of the harness dependency DAG -- it imports `run_harness`, and
`run_harness` imports it only from inside its `if __name__ == "__main__":` guard, so there is no
import cycle. Running `python3 llama/bakeoff/run_harness.py ...` still works exactly as
documented in `llama/bakeoff/README.md`.

Zero new runtime dependencies: Python 3 stdlib only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from harness_fixture import (
    DEFAULT_BASE_URL,
    DEFAULT_RUNS_DIR,
    FIXTURE_INPUT_TOKENS_TARGET,
    FIXTURE_OUTPUT_TOKENS,
    QUALITY_CORPUS_PATH,
)
from harness_record import write_experiment_record
from harness_transport import run_sweep

# The probe/corpus/lineage half of the harness still lives at its historical home; importing the
# module (not the symbols) here would work equally well, but the direct form keeps the moved CLI
# bodies byte-identical to what they were before the split.
from run_harness import (
    compute_file_lineage,
    load_quality_corpus,
    needle_probe,
    resolve_llama_cpp_build,
    run_quality_corpus,
)

# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--alias", required=True, help="already-configured router alias to test")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"default: {DEFAULT_BASE_URL}")
    parser.add_argument("--timeout", type=float, default=180, help="per-request socket timeout (s)")


def _lineage_from_args(args: argparse.Namespace) -> dict[str, Any]:
    lineage: dict[str, Any] = {
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
    samples, _completions, summary = run_sweep(
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
    samples, _completions = run_quality_corpus(
        args.base_url, args.alias, corpus=corpus, max_tokens=args.max_tokens, timeout=args.timeout
    )
    print(json.dumps({"samples": samples}, indent=2, default=str))
    return 0


def cmd_full(args: argparse.Namespace) -> int:
    base_url, alias = args.base_url, args.alias
    all_samples: list[dict[str, Any]] = []
    all_completions: list[dict[str, Any]] = []
    notes: list[str] = list(args.notes or [])

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

    prompts: list[dict[str, Any]] = []
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


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return args.func(args)
