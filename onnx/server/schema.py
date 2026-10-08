"""The request-schema boundary: validate untrusted JSON into typed specs, and render prompts.

This module is deliberately **pure and stdlib-only** (`dataclasses`, `math`, `typing`). It
imports no numpy, no onnxruntime, no tokenizers, no yaml — which is what makes it unit-testable
without the model stack, and that cheap seam is the whole point of keeping it separate from
`runtime.py`. Do not add a third-party import here.

Two jobs, in order:

1. `compile_schema(raw)` turns whatever JSON a client posted into a tuple of frozen
   `EntitySpec`/`ClassificationSpec` objects, raising **only** `SchemaError` on anything it
   cannot serve. It **validates, never coerces**: no `str()` around a label, no `bool()` around
   a flag, no `float()` around a threshold. Coercion is how a client's `{"cls_threshold": null}`
   used to reach `float(None)` and raise a bare `TypeError` past `except SchemaError`, out of
   `do_POST`, into `socketserver` — which, on a keep-alive connection with no bytes yet
   written, is not a 500 but *no HTTP response at all*. Every rejection here is one
   `400 bad_schema`.

   One asymmetry in there is **deliberate, not an oversight**: `"entities": null` is a
   `SchemaError` while `"entities": []`/`{}` compiles to no entities struct at all. An explicit
   null is a client mistake — a value never filled in — whereas an empty container is a
   coherent "no entity labels requested" that a programmatically-built schema can legitimately
   produce, and that used to return 200. See `_entity_spec`.

2. `struct_tokens(spec)` renders one spec into the prompt token list, mirroring
   `gliner2/processor.py::_transform_schema` on the inference (`is_training=False`) path.

**The ordering rules below are load-bearing** — the export-time parity gate
(`tools/export_gliner2.py --verify-only`) asserts byte-exact input ids against gliner2's own
collator, and a reordering here changes every logit silently rather than erroring:

- Struct order: the entities struct first (when present), then classifications in the order
  supplied.
- Field order: the order supplied (list order, or dict insertion order).
- **Description order is NOT field order.** The prompt emits descriptions in the order the
  *description mapping* supplied them, filtered by membership in the field list. That is why
  `descriptions` is a tuple of `(label, text)` pairs rather than a dict rebuilt from `fields`:
  rebuilding from `fields` would silently reorder the prompt for any client that lists its
  descriptions in a different order than its labels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Prompt vocabulary. Moved here from `runtime.py` (which re-exports them unchanged) because
# `struct_tokens` lives here now; the string values are the model's own special tokens and are
# not adjustable.
P_TOKEN = "[P]"
E_TOKEN = "[E]"
L_TOKEN = "[L]"
DESC_TOKEN = "[DESCRIPTION]"

# Decode paths that consume count slots > 0, which the count-pinned export cannot reproduce.
# Rejected at the boundary rather than served from slot 0 and looking plausible.
UNSUPPORTED_KEYS = ("relations", "structures", "json_structures")

DEFAULT_CLS_THRESHOLD = 0.5


class SchemaError(ValueError):
    """A request schema this runtime cannot serve (bad shape, or relations/structures)."""


@dataclass(frozen=True)
class EntitySpec:
    """The single entities struct. `name` is the literal "entities" the library prompts with."""

    fields: tuple[str, ...]
    descriptions: tuple[tuple[str, str], ...] = ()
    name: str = "entities"

    @property
    def child_token(self) -> str:
        return E_TOKEN


@dataclass(frozen=True)
class ClassificationSpec:
    """One classification task: its labels, its head mode, and its selection threshold."""

    name: str
    fields: tuple[str, ...]
    descriptions: tuple[tuple[str, str], ...] = ()
    multi_label: bool = False
    cls_threshold: float = DEFAULT_CLS_THRESHOLD

    @property
    def child_token(self) -> str:
        return L_TOKEN


Spec = EntitySpec | ClassificationSpec


def _typename(value: object) -> str:
    """JSON-flavoured type name for error messages ("null", not "NoneType")."""
    return "null" if value is None else type(value).__name__


def compile_schema(raw: object) -> tuple[Spec, ...]:
    """Validate a request schema into ordered specs. Raises only `SchemaError`.

    Order is the library's (`processor.py::_infer_from_json`): entities first, then
    classifications in the order supplied. Getting this order wrong changes the prompt and
    therefore every logit, silently.
    """
    if not isinstance(raw, dict):
        raise SchemaError(
            "schema must be an object with 'entities' and/or 'classifications', "
            f"got {_typename(raw)}"
        )

    for unsupported in UNSUPPORTED_KEYS:
        # Truthiness, not presence, exactly as before: an empty `relations: []` requests nothing
        # and stays a no-op rather than becoming a new 400 for existing callers.
        if raw.get(unsupported):
            raise SchemaError(
                f"schema key '{unsupported}' is not supported by this service — "
                "only 'entities' and 'classifications' are served"
            )

    specs: list[Spec] = []
    if "entities" in raw:
        entity_spec = _entity_spec(raw["entities"])
        if entity_spec is not None:
            specs.append(entity_spec)
    specs.extend(_classification_specs(raw.get("classifications", [])))

    if not specs:
        raise SchemaError("schema must contain 'entities' and/or 'classifications'")
    return tuple(specs)


def _entity_spec(entities: object) -> EntitySpec | None:
    """The entities struct, or `None` for a validly-typed but empty container.

    Null and empty are treated differently **on purpose** — do not "fix" the inconsistency:

    - `"entities": null` is a client mistake. A null is not a container, it is a value that was
      never filled in, and quietly reading it as "none requested" is how a bug ships.
    - `"entities": []` / `{}` is a coherent request for no entity labels — a caller building a
      schema programmatically may legitimately end up with an empty list — so it emits no
      struct and the request proceeds on its classifications alone. (This is also the
      pre-existing behaviour, which the strict pass must not change for a request that used to
      return 200.)

    With neither entities nor classifications left, `compile_schema`'s "schema must contain"
    check still fires, so an all-empty schema is still an error.
    """
    if entities is None:
        raise SchemaError(
            "schema.entities is null — omit the key entirely, or pass a list of labels or a "
            "{label: description} object"
        )

    fields: list[str] = []
    descriptions: list[tuple[str, str]] = []

    if isinstance(entities, list):
        for index, label in enumerate(entities):
            if not isinstance(label, str):
                raise SchemaError(
                    f"schema.entities[{index}] must be a string label, got {_typename(label)}"
                )
            fields.append(label)
    elif isinstance(entities, dict):
        for key, value in entities.items():
            if not isinstance(key, str):
                raise SchemaError(
                    f"schema.entities keys must be strings, got {_typename(key)}"
                )
            fields.append(key)
            # A string value is a description — including an empty one, exactly as
            # `Schema._parse_entity_input` treats it. A non-string value is NOT an error: it
            # means "no description for this key", and the key still appears as a field.
            if isinstance(value, str):
                descriptions.append((key, value))
    else:
        raise SchemaError(
            "schema.entities must be a list of labels or a {label: description} object, "
            f"got {_typename(entities)}"
        )

    if not fields:
        return None  # "no entities requested" — see the docstring; NOT an error
    return EntitySpec(fields=tuple(fields), descriptions=tuple(descriptions))


def _classification_specs(classifications: object) -> list[ClassificationSpec]:
    if not isinstance(classifications, list):
        raise SchemaError(
            "schema.classifications must be an array of classification objects, "
            f"got {_typename(classifications)}"
        )
    return [_classification_spec(entry) for entry in classifications]


def _classification_spec(entry: object) -> ClassificationSpec:
    if not isinstance(entry, dict):
        raise SchemaError(
            f"each schema.classifications entry must be an object, got {_typename(entry)}"
        )

    # `or` on the two aliases is the pre-existing selection rule; the type check is the new part.
    name = entry.get("name") or entry.get("task")
    if not isinstance(name, str) or not name:
        raise SchemaError(
            f"each classification needs a non-empty string 'name' (or 'task'), got {_typename(name)}"
        )

    labels = entry.get("labels")
    if not isinstance(labels, list) or not labels:
        raise SchemaError(f"classification '{name}': 'labels' must be a non-empty array")
    fields: list[str] = []
    for index, label in enumerate(labels):
        if not isinstance(label, str):
            raise SchemaError(
                f"classification '{name}': labels[{index}] must be a string, got {_typename(label)}"
            )
        fields.append(label)

    raw_descs = entry.get("descriptions") or entry.get("label_descriptions") or {}
    if not isinstance(raw_descs, dict):
        raise SchemaError(
            f"classification '{name}': descriptions must be an object, got {_typename(raw_descs)}"
        )
    descriptions: list[tuple[str, str]] = []
    for key, value in raw_descs.items():
        if not isinstance(key, str):
            raise SchemaError(
                f"classification '{name}': description keys must be strings, got {_typename(key)}"
            )
        if not isinstance(value, str):
            # Unguarded `str(v)` here used to render a list straight into the prompt as
            # "[1, 2]" — confident nonsense, no error anywhere.
            raise SchemaError(
                f"classification '{name}': description for '{key}' must be a string, "
                f"got {_typename(value)}"
            )
        descriptions.append((key, value))

    multi_label = entry.get("multi_label", False)
    if not isinstance(multi_label, bool):
        # `bool("false")` is True. A quoted boolean must not silently flip the head mode.
        raise SchemaError(
            f"classification '{name}': 'multi_label' must be true or false, "
            f"got {_typename(multi_label)}"
        )

    return ClassificationSpec(
        name=name,
        fields=tuple(fields),
        descriptions=tuple(descriptions),
        multi_label=multi_label,
        cls_threshold=_cls_threshold(entry, name),
    )


def _cls_threshold(entry: dict, name: str) -> float:
    """`cls_threshold` -> `threshold` -> 0.5, where a present-but-null key is an error.

    The fallback chain fires on an *absent* key only. `{"cls_threshold": null}` is a client
    mistake, not a request for the default, and it is the exact input that used to drop the
    connection.
    """
    if "cls_threshold" in entry:
        key, value = "cls_threshold", entry["cls_threshold"]
    elif "threshold" in entry:
        key, value = "threshold", entry["threshold"]
    else:
        return DEFAULT_CLS_THRESHOLD

    # bool is a subclass of int; `True` as a threshold is a type confusion, not 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(
            f"classification '{name}': '{key}' must be a number, got {_typename(value)}"
        )
    if not math.isfinite(value):
        # json.loads accepts NaN/Infinity by default, so these are reachable over the wire.
        raise SchemaError(f"classification '{name}': '{key}' must be finite, got {value!r}")
    return float(value)


def struct_tokens(spec: Spec) -> list[str]:
    """`processor.py::_transform_schema` for the inference (is_training=False) path.

    Descriptions are emitted in **description-mapping order filtered by field membership**, not
    field order — see the module docstring. Do not "tidy" this into a loop over `fields`.
    """
    prompt = spec.name
    for label, desc in spec.descriptions:
        if label in spec.fields:
            prompt += f" {DESC_TOKEN} {label}: {desc}"
    tokens = ["(", P_TOKEN, prompt, "("]
    for field in spec.fields:
        tokens.extend([spec.child_token, field])
    tokens.extend([")", ")"])
    return tokens


# ── /v1/classify and route/type dispatch ───────────────────────────────────────────────────────

MAX_CLASSIFY_TEXTS = 32

# Which runtime family each model-taking route serves. A model of another type is a client
# error (400 naming the model's type), never a 5xx and never a silent cross-family call.
ROUTE_MODEL_TYPE = {"/v1/extract": "gliner2", "/v1/classify": "seqcls"}


class RequestError(ValueError):
    """A request the boundary rejects with a 400. `code` is the wire `error.code`."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_model_type(route: str, alias: str, model_type: str) -> None:
    """Raise `RequestError("wrong_model_type")` when `alias` is not served by `route`."""
    expected = ROUTE_MODEL_TYPE[route]
    if model_type != expected:
        raise RequestError(
            "wrong_model_type",
            f"model '{alias}' is type '{model_type}'; {route} serves only '{expected}' models",
        )


def compile_classify_texts(raw: object) -> list[str]:
    """`texts` for /v1/classify: an array of 1..MAX_CLASSIFY_TEXTS strings.

    Unlike /v1/extract (which wraps a bare string for back-compat), a bare string is a 400
    here: the route is new, so there is no legacy caller to keep, and one response shape per
    request shape keeps `results[i]` unambiguously aligned with `texts[i]`.
    """
    if not isinstance(raw, list) or not raw:
        raise RequestError("bad_texts", "'texts' must be a non-empty array of strings")
    if len(raw) > MAX_CLASSIFY_TEXTS:
        raise RequestError(
            "bad_texts", f"'texts' has {len(raw)} items; at most {MAX_CLASSIFY_TEXTS} per request"
        )
    for index, text in enumerate(raw):
        if not isinstance(text, str):
            raise RequestError(
                "bad_texts", f"texts[{index}] must be a string, got {_typename(text)}"
            )
    return list(raw)
