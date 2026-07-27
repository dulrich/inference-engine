"""Torch-free GLiNER2 inference over onnxruntime.

This is a faithful reimplementation of the *inference* half of the `gliner2` library
(`gliner2/processor.py` prompt assembly + `gliner2/inference/engine.py` decode) using numpy and
five exported ONNX graphs. It exists so `inference-engine-onnx` can serve the gliner2 family
with onnxruntime + numpy + tokenizers and no torch — see `tools/export_gliner2.py` for the
export side and for why the third-party `gliner2-onnx` package could not be used verbatim.

"Faithful" is a checked claim, not an aspiration: `export_gliner2.py --verify` asserts (a) the
input ids this module builds are IDENTICAL to the ones the library's own collator builds for
the same (text, schema), and (b) the decoded output matches the library's `batch_extract(...,
include_confidence=True)` within a float tolerance, on a fixture set that includes the
crisis24-shaped schema and degenerate one-word inputs. Any change to this file must be re-run
through that gate.

The pipeline per text:

    words           = WhitespaceTokenSplitter(text.lower())        # regex, offsets into the ORIGINAL text
    prompt          = "( [P] entities ( [E] person [E] ... ) ) [SEP_STRUCT] ( [P] severity ( [L] ... ) ) [SEP_TEXT] w1 w2 ..."
    hidden          = encoder(input_ids, attention_mask)           # ONNX
    token_embs      = hidden[word_first_subtoken_positions]        # "first" pooling
    schema_embs[j]  = hidden[special_token_positions_of_struct_j]  # [P] first, then each [E]/[L]

    entities:   count = argmax(count_pred(embs[0]));  count <= 0 => no entities at all
                proj  = count_embed(embs[1:])                      # slot 0 only; see the exporter
                score = sigmoid(einsum("lkd,bpd->bplk", span_rep(token_embs), proj))
    classify:   probs = softmax(classifier(embs[1:]))              # sigmoid when multi_label

Output shape is the gliner2 library's native shape, because news-ingestion's `features.py`
reads that shape verbatim:

    {"entities": {"person": [{"text": ..., "confidence": ...}], ...},
     "severity": {"label": "Critical", "confidence": 0.83},
     "event_type": {"label": "political", "confidence": 0.62}}

Deliberately NOT supported: relations and JSON structures. Those decode paths consume count
slots > 0, which the count-pinned export cannot reproduce. They raise `SchemaError` at the API
boundary rather than returning a plausible-looking wrong answer.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

# Verbatim from gliner2/processor.py::WhitespaceTokenSplitter. Do not "tidy" it: the word
# boundaries it produces are the model's span units, and the character offsets it yields index
# the ORIGINAL (non-lowercased) text, which is how extracted spans keep their original casing.
WORD_PATTERN = re.compile(
    r"""(?:https?://[^\s]+|www\.[^\s]+)
    |[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}
    |@[a-z0-9_]+
    |\w+(?:[-_]\w+)*
    |\S""",
    re.VERBOSE | re.IGNORECASE,
)

SEP_STRUCT = "[SEP_STRUCT]"
SEP_TEXT = "[SEP_TEXT]"
P_TOKEN = "[P]"
E_TOKEN = "[E]"
L_TOKEN = "[L]"
DESC_TOKEN = "[DESCRIPTION]"

# The subset of special tokens whose positions become schema embeddings (mirrors
# SchemaTransformer._special_ids: [P] [C] [E] [R] [L] — NOT the separators).
EMBEDDING_SPECIALS = ("[P]", "[C]", "[E]", "[R]", "[L]")


class SchemaError(ValueError):
    """A request schema this runtime cannot serve (bad shape, or relations/structures)."""


@dataclass(frozen=True)
class PreparedSchema:
    """A validated, compiled schema: normalized struct descriptors + their prompt token lists."""

    structs: list[dict[str, Any]]
    tokens: list[list[str]]


class Gliner2OnnxRuntime:
    """One loaded model. Thread-safe for concurrent `extract` calls (ORT sessions are)."""

    def __init__(self, model_dir: str | Path, *, intra_op_threads: int | None = None):
        self.model_dir = Path(model_dir)
        manifest_path = self.model_dir / "gliner2_onnx.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"{self.model_dir}: no gliner2_onnx.json — export with tools/export_gliner2.py"
            )
        self.manifest: dict[str, Any] = json.loads(manifest_path.read_text())
        self.max_width = int(self.manifest["max_width"])
        self.hidden_size = int(self.manifest["hidden_size"])
        self.special_token_ids: dict[str, int] = {
            k: int(v) for k, v in self.manifest["special_token_ids"].items()
        }
        self._embedding_special_ids = frozenset(
            self.special_token_ids[t] for t in EMBEDDING_SPECIALS if t in self.special_token_ids
        )

        files = self.manifest["files"]
        opts = ort.SessionOptions()
        if intra_op_threads:
            opts.intra_op_num_threads = int(intra_op_threads)
        providers = ["CPUExecutionProvider"]
        self.encoder = self._session(files["encoder"], opts, providers)
        self.span_rep = self._session(files["span_rep"], opts, providers)
        self.classifier = self._session(files["classifier"], opts, providers)
        self.count_pred = self._session(files["count_pred"], opts, providers)
        self.count_embed = self._session(files["count_embed"], opts, providers)

        self.tokenizer = Tokenizer.from_file(str(self.model_dir / files["tokenizer"]))
        # Word-level subword ids repeat constantly across a corpus; the library caches them for
        # the same reason (processor.py OPT-6). Tokenization is deterministic, so this is safe.
        self._tok_cache: dict[str, list[int]] = {}

    def _session(self, name: str, opts: ort.SessionOptions, providers: list[str]) -> ort.InferenceSession:
        path = self.model_dir / name
        if not path.exists():
            raise FileNotFoundError(f"missing ONNX graph: {path}")
        return ort.InferenceSession(str(path), sess_options=opts, providers=providers)

    # ── tokenization ────────────────────────────────────────────────────────────────────────

    def _encode(self, piece: str) -> list[int]:
        cached = self._tok_cache.get(piece)
        if cached is None:
            cached = self.tokenizer.encode(piece, add_special_tokens=False).ids
            if len(self._tok_cache) < 200_000:
                self._tok_cache[piece] = cached
        return cached

    @staticmethod
    def normalize_text(text: str) -> str:
        """`processor.py::_collate_batch`'s terminal-punctuation rule — easy to miss, load-bearing.

        The library appends a "." to any text that does not already end in `.`/`!`/`?` (and
        turns an empty text into "."), BEFORE word-splitting. It is not cosmetic: it adds a word
        to the sequence, which shifts every downstream span index. Skipping it silently
        de-synchronises this runtime from the torch model on exactly the inputs that are already
        hardest — short ones. The export-time parity gate carries "n/a" as a fixture because
        that is the case that caught it.
        """
        if not text:
            return "."
        return text if text.endswith((".", "!", "?")) else text + "."

    @staticmethod
    def split_words(text: str) -> tuple[list[str], list[int], list[int]]:
        """Lowercased word tokens plus their (start, end) offsets in the original string."""
        words: list[str] = []
        starts: list[int] = []
        ends: list[int] = []
        for m in WORD_PATTERN.finditer(text.lower()):
            words.append(m.group())
            starts.append(m.start())
            ends.append(m.end())
        return words, starts, ends

    # ── schema -> prompt ────────────────────────────────────────────────────────────────────

    def _structs(self, schema: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize a request schema into ordered struct descriptors.

        Order is the library's: entities first, then classifications in the order supplied
        (`processor.py::_infer_from_json`). Getting this order wrong changes the prompt and
        therefore every logit, silently.
        """
        for unsupported in ("relations", "structures", "json_structures"):
            if schema.get(unsupported):
                raise SchemaError(
                    f"schema key '{unsupported}' is not supported by this service — "
                    "only 'entities' and 'classifications' are served"
                )

        structs: list[dict[str, Any]] = []

        entities = schema.get("entities")
        if entities:
            if isinstance(entities, list):
                fields = [str(e) for e in entities]
                descs: dict[str, str] = {}
            elif isinstance(entities, dict):
                fields = [str(k) for k in entities]
                # A string value is a description — including an empty one, exactly as
                # Schema._parse_entity_input treats it.
                descs = {str(k): str(v) for k, v in entities.items() if isinstance(v, str)}
            else:
                raise SchemaError("schema.entities must be a list of labels or a {label: description} object")
            if not fields:
                raise SchemaError("schema.entities is empty")
            structs.append({"kind": "entities", "name": "entities", "parent": "entities",
                            "fields": fields, "descs": descs, "child": E_TOKEN})

        for raw in schema.get("classifications") or []:
            if not isinstance(raw, dict):
                raise SchemaError("each schema.classifications entry must be an object")
            name = raw.get("name") or raw.get("task")
            labels = raw.get("labels")
            if not name or not isinstance(labels, list) or not labels:
                raise SchemaError("each classification needs a non-empty 'name' and 'labels' list")
            descs = raw.get("descriptions") or raw.get("label_descriptions") or {}
            if not isinstance(descs, dict):
                raise SchemaError(f"classification '{name}': descriptions must be an object")
            structs.append({
                "kind": "classification",
                "name": str(name),
                "parent": str(name),
                "fields": [str(x) for x in labels],
                "descs": {str(k): str(v) for k, v in descs.items()},
                "child": L_TOKEN,
                "multi_label": bool(raw.get("multi_label", False)),
                "cls_threshold": float(raw.get("cls_threshold", raw.get("threshold", 0.5))),
            })

        if not structs:
            raise SchemaError("schema must contain 'entities' and/or 'classifications'")
        return structs

    @staticmethod
    def _struct_tokens(struct: dict[str, Any]) -> list[str]:
        """`processor.py::_transform_schema` for the inference (is_training=False) path."""
        prompt = struct["parent"]
        descs = {k: v for k, v in struct["descs"].items() if k in struct["fields"]}
        if descs:
            for label, desc in descs.items():
                prompt += f" {DESC_TOKEN} {label}: {desc}"
        tokens = ["(", P_TOKEN, prompt, "("]
        for field in struct["fields"]:
            tokens.extend([struct["child"], field])
        tokens.extend([")", ")"])
        return tokens

    def _format_input(
        self, struct_token_lists: list[list[str]], words: list[str]
    ) -> tuple[list[int], list[int], list[list[int]]]:
        """`processor.py::_format_input_with_mapping`, minus the training-only bookkeeping."""
        combined: list[str] = []
        for tokens in struct_token_lists:
            combined.extend(tokens)
            combined.append(SEP_STRUCT)
        if combined:
            combined.pop()
        combined.append(SEP_TEXT)
        combined.extend(words)

        input_ids: list[int] = []
        word_first_positions: list[int] = []
        special_positions: list[list[int]] = [[] for _ in struct_token_lists]

        current_struct = 0
        found_sep = False
        for token in combined:
            pos = len(input_ids)
            ids = self._encode(token)
            if token == SEP_TEXT:
                found_sep = True
            elif not found_sep:
                if ids and ids[0] in self._embedding_special_ids:
                    special_positions[current_struct].append(pos)
                if token == SEP_STRUCT:
                    current_struct += 1
            elif ids:
                word_first_positions.append(pos)
            input_ids.extend(ids)

        return input_ids, word_first_positions, special_positions

    def build_input_ids(self, text: str, schema: dict[str, Any]) -> list[int]:
        """The prompt this runtime would encode. Used by the export-time parity gate."""
        words, _, _ = self.split_words(self.normalize_text(text))
        structs = self._structs(schema)
        ids, _, _ = self._format_input([self._struct_tokens(s) for s in structs], words)
        return ids

    # ── inference ───────────────────────────────────────────────────────────────────────────

    def prepare(self, schema: dict[str, Any]) -> "PreparedSchema":
        """Validate + compile a request schema once so a batch does not redo it per text.

        Compilation is cheap on its own (string assembly); what it buys is that the schema
        prefix's subword tokenization is resolved once and then served out of `_tok_cache`,
        and that a malformed schema fails before any model runs. Callers cache the result
        per (alias, schema-hash) — see `app.py`.
        """
        structs = self._structs(schema)
        return PreparedSchema(structs=structs, tokens=[self._struct_tokens(s) for s in structs])

    def extract(
        self,
        texts: list[str],
        schema: dict[str, Any] | "PreparedSchema",
        threshold: float = 0.5,
    ) -> list[dict[str, Any]]:
        """One result dict per input text, in input order, in the gliner2 native output shape."""
        prepared = schema if isinstance(schema, PreparedSchema) else self.prepare(schema)
        return [
            self._extract_one(t, prepared.structs, prepared.tokens, threshold) for t in texts
        ]

    def _extract_one(
        self,
        text: str,
        structs: list[dict[str, Any]],
        struct_tokens: list[list[str]],
        threshold: float,
    ) -> dict[str, Any]:
        text = self.normalize_text(text)
        words, start_map, end_map = self.split_words(text)
        input_ids, word_positions, special_positions = self._format_input(struct_tokens, words)

        ids = np.asarray([input_ids], dtype=np.int64)
        mask = np.ones_like(ids)
        hidden = self.encoder.run(None, {"input_ids": ids, "attention_mask": mask})[0][0]

        token_embs = (
            hidden[np.asarray(word_positions, dtype=np.int64)]
            if word_positions
            else np.zeros((0, self.hidden_size), dtype=np.float32)
        )

        span_rep = None
        if token_embs.shape[0] > 0 and any(s["kind"] == "entities" for s in structs):
            span_rep = self._span_rep(token_embs)

        results: dict[str, Any] = {}
        for struct, positions in zip(structs, special_positions):
            embs = hidden[np.asarray(positions, dtype=np.int64)]
            if struct["kind"] == "classification":
                results[struct["name"]] = self._classify(struct, embs)
            else:
                results["entities"] = self._entities(
                    struct, embs, span_rep, words, text, start_map, end_map, threshold
                )
        return results

    def _span_rep(self, token_embs: np.ndarray) -> np.ndarray:
        """(T, H) -> (T, W, H). Span index construction mirrors `model.py::compute_span_rep`."""
        text_len = token_embs.shape[0]
        width = self.max_width
        starts = np.repeat(np.arange(text_len, dtype=np.int64), width)
        offsets = np.tile(np.arange(width, dtype=np.int64), text_len)
        ends = starts + offsets
        invalid = ends >= text_len
        # Invalid spans are clamped to (0, 0) exactly as the library does; their scores are
        # never read because the decode only walks (start, width) pairs that stay in range.
        safe_starts = np.where(invalid, 0, starts)
        safe_ends = np.where(invalid, 0, ends)
        spans = np.stack([safe_starts, safe_ends], axis=-1)[np.newaxis, :, :]
        out = self.span_rep.run(
            None,
            {"hidden": token_embs[np.newaxis, :, :].astype(np.float32), "spans_idx": spans},
        )[0]
        return out[0]

    def _classify(self, struct: dict[str, Any], embs: np.ndarray) -> Any:
        label_embs = embs[1:]
        logits = self.classifier.run(None, {"embs": label_embs.astype(np.float32)})[0].reshape(-1)
        labels = struct["fields"]
        if struct["multi_label"]:
            probs = _sigmoid(logits)
            chosen = [
                {"label": labels[i], "confidence": float(probs[i])}
                for i in range(len(labels))
                if float(probs[i]) >= struct["cls_threshold"]
            ]
            if not chosen:
                best = int(np.argmax(probs))
                chosen = [{"label": labels[best], "confidence": float(probs[best])}]
            return chosen
        probs = _softmax(logits)
        best = int(np.argmax(probs))
        return {"label": labels[best], "confidence": float(probs[best])}

    def _entities(
        self,
        struct: dict[str, Any],
        embs: np.ndarray,
        span_rep: np.ndarray | None,
        words: list[str],
        text: str,
        start_map: list[int],
        end_map: list[int],
        threshold: float,
    ) -> dict[str, list[dict[str, Any]]]:
        names = struct["fields"]
        # The count head is a GATE here, not a slot selector: gliner2 returns no entities at all
        # when the predicted count is 0, and the entity decode only ever reads slot 0.
        count_logits = self.count_pred.run(None, {"embs": embs[0:1].astype(np.float32)})[0]
        pred_count = int(np.argmax(count_logits, axis=1)[0])
        if pred_count <= 0 or span_rep is None:
            return {}

        proj = self.count_embed.run(None, {"label_embs": embs[1:].astype(np.float32)})[0]
        # span_rep (T, W, H) x proj (1, M, H) -> (M, T, W) for slot 0.
        scores = _sigmoid(np.einsum("twh,mh->mtw", span_rep, proj[0]))

        text_len = len(words)
        out: dict[str, list[dict[str, Any]]] = {}
        for idx, name in enumerate(names):
            spans = _find_spans(scores[idx], threshold, text_len, text, start_map, end_map)
            out[name] = _format_spans(spans)
        return _dedupe_entity_dict(out)


def _find_spans(
    scores: np.ndarray,
    threshold: float,
    text_len: int,
    text: str,
    start_map: list[int],
    end_map: list[int],
) -> list[tuple[str, float, int, int]]:
    """`engine.py::_find_spans`. `scores` is (text_len, max_width) for one label."""
    found: list[tuple[str, float, int, int]] = []
    hits = np.argwhere(scores >= threshold)
    for start, width in hits:
        start = int(start)
        end = start + int(width) + 1
        if 0 <= start < text_len and end <= text_len:
            char_start = start_map[start]
            char_end = end_map[end - 1]
            span_text = text[char_start:char_end].strip()
            if span_text:
                found.append((span_text, float(scores[start, width]), char_start, char_end))
    return found


def _format_spans(spans: list[tuple[str, float, int, int]]) -> list[dict[str, Any]]:
    """`engine.py::_format_spans` — highest-confidence-wins greedy overlap removal."""
    if not spans:
        return []
    selected: list[tuple[str, float, int, int]] = []
    for span in sorted(spans, key=lambda x: x[1], reverse=True):
        _, _, start, end = span
        if not any(not (end <= s[2] or start >= s[3]) for s in selected):
            selected.append(span)
    return [{"text": s[0], "confidence": s[1]} for s in selected]


def _dedupe_entity_dict(entities: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """`engine.py::_format_entity_dict` — case-insensitive text dedupe per label."""
    formatted: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for name, spans in entities.items():
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for span in spans:
            span_text = span.get("text", "")
            if span_text and span_text.lower() not in seen:
                seen.add(span_text.lower())
                unique.append(span)
        formatted[name] = unique
    return dict(formatted)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    """Numerically stable elementwise logistic."""
    out = np.empty_like(x, dtype=np.float64)
    pos = x >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-x[pos]))
    exp_x = np.exp(x[~pos])
    out[~pos] = exp_x / (1.0 + exp_x)
    return out


def _softmax(x: np.ndarray) -> np.ndarray:
    shifted = np.exp(x.astype(np.float64) - float(np.max(x)))
    return shifted / np.sum(shifted)
