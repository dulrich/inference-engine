"""Torch-free sequence-classifier inference over onnxruntime (`hsc-seqcls/1` bundles).

The second runtime family this sidecar hosts. A bundle is a directory holding:

    model.onnx      one graph; int64 inputs ⊆ {input_ids, attention_mask, token_type_ids},
                    one output named `logits` of shape (batch, n_labels)
    tokenizer.json  a `tokenizers` tokenizer whose post-processor adds the special tokens
    manifest.json   {"format": "hsc-seqcls/1", "labels": [...in logit order], "max_len": int,
                     "truncation_side": "left" | "right", "temperature": float, "source": {...}}

The tokenization mirrors exactly the path the exporter's parity gate was proven on
(`eval-harness/hook_state_classifier/src/hook_state_classifier/export_serving.py::_onnx_logits`):
`no_padding()`, `enable_truncation(max_len, direction=truncation_side)`, `encode(text)`, one
text per session run, unpadded. Batching with padding would be a second, unproven path, so
texts are run one at a time on purpose.

Probabilities are `softmax(logits / temperature)` in manifest `labels` order; `label` is the
argmax and `confidence` its probability. An unknown or missing `format` is refused at load —
a bundle this code was not written for must read as a load error on `/health`, never as
plausible-looking output.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

SUPPORTED_FORMATS = ("hsc-seqcls/1",)
_FEEDABLE = ("input_ids", "attention_mask", "token_type_ids")


class BundleError(ValueError):
    """A seqcls bundle this runtime refuses to serve (unknown format, bad manifest)."""


class SeqClsOnnxRuntime:
    """One loaded sequence classifier. Thread-safe for concurrent `classify` calls."""

    def __init__(self, model_dir: str | Path, *, intra_op_threads: int | None = None):
        self.model_dir = Path(model_dir)
        manifest_path = self.model_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"{self.model_dir}: no manifest.json (hsc-seqcls bundle)")
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict):
            raise BundleError(f"{manifest_path}: manifest must be a JSON object")
        fmt = manifest.get("format")
        if fmt not in SUPPORTED_FORMATS:
            raise BundleError(
                f"{manifest_path}: unsupported bundle format {fmt!r} "
                f"(this runtime serves {', '.join(SUPPORTED_FORMATS)})"
            )
        labels = manifest.get("labels")
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(lab, str) and lab for lab in labels)
            or len(set(labels)) != len(labels)
        ):
            raise BundleError(f"{manifest_path}: 'labels' must be a non-empty list of unique strings")
        max_len = manifest.get("max_len")
        if isinstance(max_len, bool) or not isinstance(max_len, int) or max_len <= 0:
            raise BundleError(f"{manifest_path}: 'max_len' must be a positive integer")
        side = manifest.get("truncation_side", "right")
        if side not in ("left", "right"):
            raise BundleError(f"{manifest_path}: 'truncation_side' must be 'left' or 'right'")
        temperature = manifest.get("temperature", 1.0)
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
            or temperature <= 0
        ):
            raise BundleError(f"{manifest_path}: 'temperature' must be a positive finite number")

        self.manifest: dict[str, Any] = manifest
        self.format: str = fmt
        self.labels: tuple[str, ...] = tuple(labels)
        self.max_len: int = max_len
        self.truncation_side: str = side
        self.temperature: float = float(temperature)

        model_path = self.model_dir / "model.onnx"
        if not model_path.exists():
            raise FileNotFoundError(f"missing ONNX graph: {model_path}")
        opts = ort.SessionOptions()
        if intra_op_threads:
            opts.intra_op_num_threads = int(intra_op_threads)
        self.session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._input_names = [i.name for i in self.session.get_inputs()]
        unknown = [n for n in self._input_names if n not in _FEEDABLE]
        if unknown:
            raise BundleError(f"{model_path}: unexpected graph inputs {unknown}")
        if "logits" not in [o.name for o in self.session.get_outputs()]:
            raise BundleError(f"{model_path}: graph has no output named 'logits'")

        self.tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(self.max_len, direction=self.truncation_side)

    def logits(self, text: str) -> np.ndarray:
        enc = self.tokenizer.encode(text)
        columns = {
            "input_ids": enc.ids,
            "attention_mask": enc.attention_mask,
            "token_type_ids": enc.type_ids,
        }
        feed = {name: np.asarray([columns[name]], dtype=np.int64) for name in self._input_names}
        (out,) = self.session.run(["logits"], feed)
        row = np.asarray(out, dtype=np.float64).reshape(-1)
        if row.shape[0] != len(self.labels):
            raise BundleError(
                f"graph produced {row.shape[0]} logits for {len(self.labels)} manifest labels"
            )
        return row

    def classify(self, texts: list[str]) -> list[dict[str, Any]]:
        results = []
        for text in texts:
            scaled = self.logits(text) / self.temperature
            scaled = scaled - scaled.max()
            exp = np.exp(scaled)
            probs = exp / exp.sum()
            best = int(np.argmax(probs))
            results.append({
                "label": self.labels[best],
                "confidence": float(probs[best]),
                "probabilities": {lab: float(p) for lab, p in zip(self.labels, probs)},
            })
        return results
