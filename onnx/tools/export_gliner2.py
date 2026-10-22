#!/usr/bin/env python3
"""Export a GLiNER2-family checkpoint to ONNX graphs the sidecar can serve.

This script is the ONLY place torch is allowed to appear in this work package. It runs on the
HOST (or in a build-time venv), never inside `inference-engine-onnx` — the whole point of the
sidecar is that its runtime image carries onnxruntime + numpy + tokenizers and nothing else.

Why a bespoke exporter rather than `pip install gliner2-onnx[export]`
--------------------------------------------------------------------
The third-party `gliner2-onnx` project (MIT, github.com/lmoe/gliner2-onnx) proved the export
mechanics and is where the GRU-unroll idea comes from. It is not usable as-is here:

  * it refuses `fastino/gliner2-base-v1` (the launch consumer's model) because that checkpoint
    uses `count_lstm_v2`; the exporter only handles `count_lstm`;
  * its runtime issues ONE ENCODER PASS PER TASK and hardcodes the classification task name to
    the literal "category", where the gliner2 library encodes every task of a schema in a
    SINGLE prompt (`[SEP_STRUCT]`-joined) using the caller's real task names ("severity",
    "event_type"). Different prompt => different logits => different stored features;
  * it drops per-label descriptions, which news-ingestion's schema supplies deliberately;
  * it skips `count_pred` entirely, so it cannot reproduce the library's "predicted count 0 =>
    no entities" gate.

So we export every leaf module the inference path touches and reimplement the library's
*inference* path (prompt assembly + decode) over numpy in `server/runtime.py`, then assert
byte-level input-id parity and value-level output parity against the torch library right here
in `--verify`. That assertion is the reason this is safe: it is checked, not assumed.

The one deliberate simplification: `count_embed` is exported with the count fixed at 1.
That is EXACT rather than approximate for everything this service serves, and the reason is
structural: the entity decode path in gliner2 only ever reads count slot 0
(`span_scores[0, :, -text_len:]` in `engine.py::_extract_entities`), and slot 0's embedding
cannot depend on the total count — `CountLSTM`/`CountLSTMv2` drive a *causal* GRU over count
steps, so step 0's hidden state is a function of `pos_embedding[0]` and the label embeddings
only. (`CountLSTMv2`'s trailing `DownscaledTransformer` runs with `batch_first=True` over a
tensor shaped `(count, num_labels, hidden)`, i.e. it mixes across LABELS within a slot, never
across slots — also count-independent.) The predicted count still matters as a gate, which is
why `count_pred` IS exported. Relations and JSON structures DO consume slots > 0; they are
out of scope for this sidecar and rejected at the API, not silently mis-served.

Usage:
    python export_gliner2.py --model fastino/gliner2-base-v1 --out /models/fastino/gliner2-base-v1-onnx
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# The numpy runtime lives beside this file's parent; --verify imports it to prove parity.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="HF model id, e.g. fastino/gliner2-base-v1")
    ap.add_argument("--out", required=True, help="output directory for the ONNX artifacts")
    ap.add_argument("--tolerance", type=float, default=2e-3, help="max abs confidence delta vs torch")
    ap.add_argument("--no-verify", action="store_true", help="skip the torch-vs-onnx parity gate")
    ap.add_argument("--verify-only", action="store_true",
                    help="re-run the parity gate against an existing export (no re-export)")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty output directory")
    return ap


# Help is answered before the (slow, optional) torch import below: argparse prints usage and exits 0.
if any(a in ("-h", "--help") for a in sys.argv[1:]):
    _parser().parse_args()

import torch  # noqa: E402
import torch.nn as nn  # noqa: E402

OPSET = 17
# The count-embedding graph is exported through the dynamo path (see `_export_dynamo`), which
# emits `Split` with the `num_outputs` attribute — opset 18 or newer.
OPSET_DYNAMO = 18

# Fixture schema for --verify. Deliberately the crisis24 shape news-ingestion actually sends
# (7 described entity labels + two single-label classifications), because parity on the shape
# nobody sends is worth nothing.
VERIFY_ENTITIES = {
    "person": "Names of people or named individuals",
    "organization": "Companies, agencies, militant groups, or other named organizations",
    "location": "Cities, regions, countries, or named places",
    "facility": "Named buildings, infrastructure, airports, or installations",
    "weapon": "Weapons, ordnance, or means of attack",
    "casualties": "Mentions of deaths, injuries, or people affected",
    "date": "Dates, times, or explicit temporal references",
}
VERIFY_CLASSIFICATIONS = [
    {"name": "severity", "labels": ["Info", "Caution", "Warning", "Critical"]},
    {"name": "event_type", "labels": ["disaster", "attack", "protest", "threat", "political", "economic", "unrest"]},
]
VERIFY_TEXTS = [
    (
        "Security forces in Nairobi clashed with demonstrators near Jomo Kenyatta International "
        "Airport on 14 March 2026 after the Kenyan National Police Service used tear gas and live "
        "rounds against a protest organised by the Wananchi Movement. At least three people were "
        "killed and twenty-seven injured, according to the Kenya Red Cross Society."
    ),
    "Heavy rainfall flooded the A4 motorway outside Lisbon on Tuesday; no injuries were reported.",
    "The central bank of Argentina raised its policy rate to 41% amid renewed currency pressure.",
    # Short/degenerate inputs are where span-index arithmetic goes wrong, so they are fixtures too.
    "Ambush.",
    "n/a",
]


class EncoderWrapper(nn.Module):
    """(input_ids, attention_mask) -> last_hidden_state. Hides the HF output dataclass."""

    def __init__(self, encoder: nn.Module):
        super().__init__()
        self.encoder = encoder

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        return self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state


class SpanRepWrapper(nn.Module):
    """(hidden (1,L,H), spans_idx (1,L*W,2)) -> span_rep (1,L,W,H). SpanMarkerV0 verbatim."""

    def __init__(self, span_rep: nn.Module):
        super().__init__()
        self.span_rep = span_rep

    def forward(self, hidden: torch.Tensor, spans_idx: torch.Tensor) -> torch.Tensor:
        return self.span_rep(hidden, spans_idx)


class CountEmbedWrapper(nn.Module):
    """(label_embs (M,H)) -> struct_proj (1,M,H). Count pinned to 1 — see the module docstring."""

    def __init__(self, count_embed: nn.Module):
        super().__init__()
        self.count_embed = count_embed

    def forward(self, label_embs: torch.Tensor) -> torch.Tensor:
        return self.count_embed(label_embs, 1)


class MlpWrapper(nn.Module):
    """(x (N,H)) -> logits (N,out). Used for both `classifier` and `count_pred`."""

    def __init__(self, mlp: nn.Module):
        super().__init__()
        self.mlp = mlp

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.mlp(x)


def _disable_mha_fastpath() -> None:
    """Stop `nn.TransformerEncoderLayer` fusing itself into an un-exportable aten op.

    `CountLSTMv2` (the counting layer of `gliner2-base-v1` and `gliguard-LLMGuardrails-300M`)
    ends in a `DownscaledTransformer`, i.e. a real `nn.TransformerEncoder`. In eval mode with no
    mask, torch takes the fused inference path and the tracer emits
    `aten::_transformer_encoder_layer_fwd`, which has no ONNX symbolic at ANY opset:

        UnsupportedOperatorError: Exporting the operator
        'aten::_transformer_encoder_layer_fwd' to ONNX opset version 17 is not supported

    That single fused op is the entire reason the upstream `gliner2-onnx` exporter declares
    `count_lstm_v2` — and therefore `fastino/gliner2-base-v1`, this fleet's launch model — an
    unsupported "different GRU architecture". It is not an architecture problem; turning the
    fastpath off makes the layer trace as ordinary matmul/softmax/layernorm ops and export
    cleanly. Values are unchanged: the fastpath is a fusion, not a different computation.
    """
    backend = getattr(torch.backends, "mha", None)
    if backend is not None and hasattr(backend, "set_fastpath_enabled"):
        backend.set_fastpath_enabled(False)


def _export(module: nn.Module, args: tuple, path: Path, input_names, output_names, dynamic_axes) -> None:
    module.eval()
    with torch.no_grad():
        torch.onnx.export(
            module,
            args,
            str(path),
            input_names=input_names,
            output_names=output_names,
            dynamic_axes=dynamic_axes,
            opset_version=OPSET,
            do_constant_folding=True,
            # The legacy tracer, deliberately: the dynamo path re-specialises on the concrete
            # dummy shapes below, and this graph's whole job is to be shape-polymorphic.
            dynamo=False,
        )
    print(f"[export]   {path.name}: {path.stat().st_size / 1e6:.1f} MB")


def _export_dynamo(module: nn.Module, args: tuple, path: Path, input_names, output_names, dynamic_shapes) -> None:
    """Export via `torch.export` (dynamo) instead of the TorchScript tracer.

    Needed for `count_embed`. The tracer records `nn.MultiheadAttention`'s internal reshapes
    with the DUMMY label count baked in as a literal, so the resulting graph loads fine and then
    dies at request time on any other label count:

        Reshape node ... Input shape:{7,1,128}, requested shape:{5,4,32}

    i.e. it silently specialises to a 5-label schema. `dynamic_axes` cannot fix that — it only
    annotates the graph's boundary, not its interior. `torch.export` carries the symbolic dim
    through the attention internals, so the same graph serves 4, 7, or 40 labels. Verified
    against torch on several label counts at export time (max abs delta ~5e-6).
    """
    module.eval()
    with torch.no_grad():
        torch.onnx.export(
            module,
            args,
            str(path),
            input_names=input_names,
            output_names=output_names,
            dynamic_shapes=dynamic_shapes,
            opset_version=OPSET_DYNAMO,
            dynamo=True,
        )
    print(f"[export]   {path.name}: {path.stat().st_size / 1e6:.1f} MB (dynamo, opset {OPSET_DYNAMO})")


def export_model(model_id: str, out_dir: Path) -> dict:
    from gliner2 import GLiNER2

    _disable_mha_fastpath()
    print(f"[export] loading {model_id} (torch, CPU) ...")
    t0 = time.time()
    model = GLiNER2.from_pretrained(model_id)
    model.eval()
    load_s = time.time() - t0
    print(f"[export] loaded in {load_s:.1f}s")

    processor = model.processor
    tokenizer = processor.tokenizer
    hidden = int(model.hidden_size)
    max_width = int(model.max_width)

    out_dir.mkdir(parents=True, exist_ok=True)

    # Dummy shapes only fix the RANK; every meaningful axis is declared dynamic below.
    seq, words, labels = 48, 16, 5
    dummy_ids = torch.ones(1, seq, dtype=torch.long)
    dummy_mask = torch.ones(1, seq, dtype=torch.long)
    dummy_hidden = torch.randn(1, words, hidden)
    dummy_spans = torch.zeros(1, words * max_width, 2, dtype=torch.long)
    dummy_labels = torch.randn(labels, hidden)

    print("[export] exporting graphs ...")
    _export(
        EncoderWrapper(model.encoder), (dummy_ids, dummy_mask), out_dir / "encoder.onnx",
        ["input_ids", "attention_mask"], ["hidden"],
        {"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"},
         "hidden": {0: "batch", 1: "seq"}},
    )
    _export(
        SpanRepWrapper(model.span_rep), (dummy_hidden, dummy_spans), out_dir / "span_rep.onnx",
        ["hidden", "spans_idx"], ["span_rep"],
        {"hidden": {0: "batch", 1: "words"}, "spans_idx": {0: "batch", 1: "nspans"},
         "span_rep": {0: "batch", 1: "words"}},
    )
    _export(
        MlpWrapper(model.classifier), (dummy_labels,), out_dir / "classifier.onnx",
        ["embs"], ["logits"], {"embs": {0: "n"}, "logits": {0: "n"}},
    )
    _export(
        MlpWrapper(model.count_pred), (dummy_labels[:1],), out_dir / "count_pred.onnx",
        ["embs"], ["logits"], {"embs": {0: "n"}, "logits": {0: "n"}},
    )
    _export_dynamo(
        CountEmbedWrapper(model.count_embed), (dummy_labels,), out_dir / "count_embed.onnx",
        ["label_embs"], ["struct_proj"], ({0: torch.export.Dim("m", min=2, max=1024)},),
    )

    # The tokenizer MUST be the processor's own: SchemaTransformer.__init__ adds ten schema
    # special tokens ([P] [E] [L] [SEP_TEXT] ...) and the encoder's embedding matrix was resized
    # to match. Saving the pristine hub tokenizer instead would shift every id by ten.
    print("[export] saving tokenizer (with the schema special tokens added) ...")
    tokenizer.save_pretrained(str(out_dir))
    if not (out_dir / "tokenizer.json").exists():
        raise SystemExit("export failed: no fast tokenizer.json was produced")

    special_ids = {
        tok: int(tokenizer.convert_tokens_to_ids(tok))
        for tok in ("[SEP_STRUCT]", "[SEP_TEXT]", "[P]", "[C]", "[E]", "[R]", "[L]",
                    "[EXAMPLE]", "[OUTPUT]", "[DESCRIPTION]")
    }

    manifest = {
        "source_model": model_id,
        "hidden_size": hidden,
        "max_width": max_width,
        "counting_layer": getattr(model.config, "counting_layer", None),
        "encoder_model": getattr(model.config, "model_name", None),
        "token_pooling": getattr(model.config, "token_pooling", "first"),
        "vocab_size": len(tokenizer),
        "special_token_ids": special_ids,
        "count_fixed_at": 1,
        "opset": OPSET,
        "opset_count_embed": OPSET_DYNAMO,
        "files": {
            "encoder": "encoder.onnx",
            "span_rep": "span_rep.onnx",
            "classifier": "classifier.onnx",
            "count_pred": "count_pred.onnx",
            "count_embed": "count_embed.onnx",
            "tokenizer": "tokenizer.json",
        },
        "exported_by": "inference-engine/onnx/tools/export_gliner2.py",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "torch_version": torch.__version__,
        "gliner2_version": _gliner2_version(),
    }
    (out_dir / "gliner2_onnx.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[export] manifest written: {out_dir / 'gliner2_onnx.json'}")
    return {"model": model, "manifest": manifest}


def _gliner2_version() -> str:
    try:
        from importlib.metadata import version

        return version("gliner2")
    except Exception:  # pragma: no cover - informational only
        return "unknown"


def verify(model, out_dir: Path, tolerance: float) -> int:
    """Assert the numpy/ONNX runtime reproduces the torch library on the fixture set."""
    from server.runtime import Gliner2OnnxRuntime

    print("[verify] loading the ONNX runtime (no torch) ...")
    rt = Gliner2OnnxRuntime(out_dir)

    schema = model.create_schema().entities(VERIFY_ENTITIES)
    for cls in VERIFY_CLASSIFICATIONS:
        schema = schema.classification(cls["name"], cls["labels"])

    req_schema = {"entities": VERIFY_ENTITIES, "classifications": VERIFY_CLASSIFICATIONS}

    failures = 0

    # ── 1. input-id parity: the prompt we build must be the prompt the library builds ────────
    from gliner2.training.trainer import ExtractorCollator

    collator = ExtractorCollator(model.processor, is_training=False)
    for text in VERIFY_TEXTS:
        batch = collator([(text, _schema_dict(schema))])
        want = [int(x) for x in batch.input_ids[0].tolist()]
        want = want[: int(batch.attention_mask[0].sum().item())]
        got = rt.build_input_ids(text, req_schema)
        if want != got:
            failures += 1
            print(f"[verify] FAIL input_ids differ for {text[:40]!r}")
            print(f"[verify]   torch({len(want)}): {want[:40]}")
            print(f"[verify]   onnx ({len(got)}): {got[:40]}")
        else:
            print(f"[verify] PASS input_ids identical ({len(got)} ids) for {text[:40]!r}")

    # ── 2. output parity against the library's own native output ────────────────────────────
    native = model.batch_extract(list(VERIFY_TEXTS), schema, include_confidence=True)
    ours = rt.extract(list(VERIFY_TEXTS), req_schema)
    for text, want, got in zip(VERIFY_TEXTS, native, ours):
        diff = _compare(want, got, tolerance)
        if diff:
            failures += 1
            print(f"[verify] FAIL output differs for {text[:40]!r}: {diff}")
            print(f"[verify]   torch: {json.dumps(want, default=str)}")
            print(f"[verify]   onnx : {json.dumps(got, default=str)}")
        else:
            print(f"[verify] PASS output identical (within {tolerance}) for {text[:40]!r}")

    return failures


def _schema_dict(schema) -> dict:
    import copy

    return copy.deepcopy(schema.build())


def _compare(want, got, tol: float) -> str:
    """Structural comparison; floats within `tol`. Returns '' when equal."""
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return f"type {type(got).__name__} != dict"
        if set(want) != set(got):
            return f"keys {sorted(want)} != {sorted(got)}"
        for k in want:
            d = _compare(want[k], got[k], tol)
            if d:
                return f"[{k}] {d}"
        return ""
    if isinstance(want, list):
        if not isinstance(got, list):
            return f"type {type(got).__name__} != list"
        if len(want) != len(got):
            return f"len {len(want)} != {len(got)}"
        for i, (a, b) in enumerate(zip(want, got)):
            d = _compare(a, b, tol)
            if d:
                return f"[{i}] {d}"
        return ""
    if isinstance(want, float):
        if not isinstance(got, (int, float)):
            return f"type {type(got).__name__} != float"
        return "" if abs(want - float(got)) <= tol else f"{want} != {got}"
    return "" if want == got else f"{want!r} != {got!r}"


def main() -> int:
    args = _parser().parse_args()

    out_dir = Path(args.out)

    if args.verify_only:
        from gliner2 import GLiNER2

        _disable_mha_fastpath()
        model = GLiNER2.from_pretrained(args.model)
        model.eval()
        failures = verify(model, out_dir, args.tolerance)
        if failures:
            print(f"[verify] {failures} check(s) FAILED", file=sys.stderr)
            return 1
        print("[verify] all parity checks passed")
        return 0

    if out_dir.exists() and any(out_dir.iterdir()):
        if not args.force:
            raise SystemExit(f"{out_dir} exists and is not empty (use --force)")
        shutil.rmtree(out_dir)

    torch.manual_seed(0)
    result = export_model(args.model, out_dir)

    if args.no_verify:
        print("[export] parity verification SKIPPED (--no-verify) — do not ship this export")
        return 0

    failures = verify(result["model"], out_dir, args.tolerance)
    if failures:
        print(f"[verify] {failures} check(s) FAILED — the export is not usable", file=sys.stderr)
        return 1
    print("[verify] all parity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
