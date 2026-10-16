#!/usr/bin/env python3
"""The serving-control plane's on-disk PROTOCOL (`plans/bench-dashboard-serving-control.md` WP-5):
schema literals, the request-id grammar, the closed consumer vocabulary, bounds, and the two pure
functions that decide whether an untrusted drop-box body is a `serving-request-v1` at all and what
`serving-status-v1` a pass should publish.

Deliberately the pure end of the split, exactly like `control_protocol.py` is for the bake-off
plane: no locking, no atomic writes, no subprocesses, no filesystem access of any kind. The
declaration is passed IN as an already-parsed dict; `serving_dispatcher.py` owns every path and
every write.

**This plane is a second, separate protocol -- not a widening of the bake-off one.** The two
share one drop-box directory and each ignores the other's basenames at ingest (`req-` vs `srv-`),
but they have different failure domains, different states, and different durable records, so the
constants are re-declared here rather than imported and overloaded. The one thing that IS reused
is timestamp spelling: `control_protocol.iso_z`/`parse_iso`, so both planes' files carry the same
`...Z` UTC ISO-8601 convention.

The wire shapes are fixed by the generated contract (`dashboard/packages/shared/src/index.ts`,
`gen-ts` output of `dashboard/crates/domain`) and by the committed cross-language fixture bytes under
`dashboard/fixtures/serving/contract-v1/`, which `tests/test_serving_dispatcher.py`
parses with the validators below -- the same bytes the generated validators parse. Neither side is
renegotiable here.

Two rejection reasons are load-bearing prose, not incidental strings:

- every `"malformed: ..."` reason is a FIXED string, never derived from body content -- the
  request file is container-written (outside the host trust boundary) and its `reason` is
  rendered verbatim on the dashboard, so untrusted bytes must never reach that surface; and
- `CC_REQUIRES_GATEWAY_ID` is spelled without the `malformed:` prefix on purpose: it is a
  well-formed request that the DECLARATION refuses, and it is the enforcing gate for the plan's
  `cc => gateway_id non-null` invariant (the UI only disables the chip; the API cannot see the
  declaration at all). Rejection happens before any write, so there is nothing to keep.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from control_protocol import iso_z, parse_iso

__all__ = [
    "ACTIONS",
    "CC_REQUIRES_GATEWAY_ID",
    "CONSUMERS",
    "MAX_REQUEST_BYTES",
    "PRUNE_AGE_SECONDS",
    "RECENT_LIMIT",
    "REQUEST_BASENAME_RE",
    "REQUEST_ID_RE",
    "SCHEMA_REQUEST",
    "SCHEMA_STATUS",
    "SCHEMA_VISIBILITY",
    "STATES",
    "RequestValidation",
    "ServingRequest",
    "canonical_consumers",
    "compose_status",
    "declared_models",
    "make_record",
    "validate_request",
    "validate_status_file",
]

# -------------------------------------------------------------------------------------------
# Schema names. All three are WIRE requirements, unlike the bake-off plane where only the
# request literal is checked: the request file is container-written, the status file is read
# back by this dispatcher (to carry `recent[]` forward) and by the API, and the declaration is
# the repo-owned file both generators read.
# -------------------------------------------------------------------------------------------

SCHEMA_REQUEST = "serving-request-v1"
SCHEMA_STATUS = "serving-status-v1"
SCHEMA_VISIBILITY = "serving-visibility-v1"

# -------------------------------------------------------------------------------------------
# Grammars, vocabulary and bounds.
# -------------------------------------------------------------------------------------------

# `srv-`, never `req-`: the two planes share `control/requests/` and each ignores the other's
# basenames (`control_phases.ingest_pass` matches `REQUEST_BASENAME_RE` for `req-` only).
REQUEST_ID_RE = re.compile(r"^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$")
REQUEST_BASENAME_RE = re.compile(r"^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.json$")

# The closed consumer vocabulary IN CANONICAL ORDER -- this tuple's order IS the canonical
# order (`dashboard/packages/shared/src/index.ts`'s `SERVING_CONSUMER_ORDER`), which is
# why `canonical_consumers` can sort by it directly. Append-only, like the alias contract.
CONSUMERS = ("cc", "openwebui")
ACTIONS = ("set-visibility",)
STATES = ("applied", "rejected", "failed")

MAX_REQUEST_BYTES = 8192
RECENT_LIMIT = 20
PRUNE_AGE_SECONDS = 30 * 24 * 3600

CC_REQUIRES_GATEWAY_ID = "cc requires a gateway id"


def canonical_consumers(values: Sequence[Any]) -> list[str]:
    """Canonical order (`CONSUMERS`) and deduplicated. Anything outside the vocabulary is dropped
    -- callers that must REJECT an unknown consumer check for it first (`validate_request` does);
    this function is the normalizer, not the gate."""
    present = {value for value in values if isinstance(value, str)}
    return [consumer for consumer in CONSUMERS if consumer in present]


def declared_models(declaration: dict[str, Any] | None) -> dict[str, Any] | None:
    """The `models` map of a `serving-visibility-v1` document, or `None` when the document is not
    one. Fail-closed and structural: a wrong schema literal, a missing/ill-typed `models` map, or
    ANY entry that is not `{gateway_id: str|null, consumers: [...]}` yields `None` for the whole
    declaration rather than a partially-trusted map -- a dispatcher that cannot read the
    declaration must publish nothing and apply nothing, never guess."""
    if not isinstance(declaration, dict):
        return None
    if declaration.get("schema") != SCHEMA_VISIBILITY:
        return None
    models = declaration.get("models")
    if not isinstance(models, dict):
        return None
    for key, entry in models.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            return None
        gateway_id = entry.get("gateway_id")
        if gateway_id is not None and not isinstance(gateway_id, str):
            return None
        if not isinstance(entry.get("consumers"), list):
            return None
    return models


@dataclass(frozen=True)
class ServingRequest:
    """One validated `serving-request-v1`, carrying EXACTLY the fields the apply phase may read.
    Frozen for `control_protocol.IngestedRequest`'s reason: a later phase cannot attach a derived
    key, so "what came off the wire" and "what we computed" can never blur. `request_id` is always
    the basename-derived id, never the body's own copy (which `validate_request` has just proven
    equal to it), and `consumers` is already canonical and deduplicated."""

    request_id: str
    action: str
    model_key: str
    consumers: tuple[str, ...]
    requested_at: str


@dataclass(frozen=True)
class RequestValidation:
    """Exactly one of `request`/`reason` is set -- `control_protocol.RequestValidation`'s shape,
    restated here so this plane does not import the bake-off plane's `IngestedRequest` type."""

    request: ServingRequest | None = None
    reason: str | None = None

    @classmethod
    def accept(cls, request: ServingRequest) -> RequestValidation:
        return cls(request=request, reason=None)

    @classmethod
    def reject(cls, reason: str) -> RequestValidation:
        return cls(request=None, reason=reason)

    @property
    def accepted(self) -> bool:
        return self.request is not None


def validate_request(
    basename_id: str, data: bytes, declaration: dict[str, Any] | None,
) -> RequestValidation:
    """Fail-closed validation of one drop-box request body against the declaration.

    Check order is load-bearing:

    1. `basename_id` matches the id grammar -- defence in depth. The ingest loop has already
       matched the basename, so this only fires for a direct caller, but it means no code path
       can hand a `req-`-shaped or hand-typed id to the rest of this function;
    2. parseable UTF-8 JSON, and a JSON object;
    3. `request_id` equals the trusted basename -- FIRST among the content checks, because the
       durable record is keyed by the basename-derived id and this equality is the only place
       body content is allowed to matter at all;
    4. `schema` equals `SCHEMA_REQUEST` -- before any field-by-field interpretation, because the
       version is what says those fields MEAN what this function thinks they mean;
    5. `action`, `model_key` (non-empty AND declared), `consumers` (within the vocabulary),
       `requested_at` (parseable -- it is load-bearing for `(requested_at, request_id)` ordering,
       so an unparseable one is a protocol error, not a field to ignore);
    6. LAST, the declaration's own invariant: `cc` requires a non-null `gateway_id`.
    """
    if not REQUEST_ID_RE.match(basename_id):
        return RequestValidation.reject("malformed: invalid request id")
    models = declared_models(declaration)
    if models is None:
        return RequestValidation.reject("malformed: unreadable visibility declaration")
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RequestValidation.reject("malformed: invalid JSON")
    if not isinstance(body, dict):
        return RequestValidation.reject("malformed: not a JSON object")
    if body.get("request_id") != basename_id:
        return RequestValidation.reject("malformed: request_id does not match filename")
    if body.get("schema") != SCHEMA_REQUEST:
        return RequestValidation.reject(f"malformed: schema is not {SCHEMA_REQUEST}")
    action = body.get("action")
    if action not in ACTIONS:
        return RequestValidation.reject("malformed: invalid action")
    model_key = body.get("model_key")
    if not isinstance(model_key, str) or not model_key:
        return RequestValidation.reject("malformed: invalid model_key")
    entry = models.get(model_key)
    if not isinstance(entry, dict):
        return RequestValidation.reject("malformed: undeclared model_key")
    consumers = body.get("consumers")
    if not isinstance(consumers, list):
        return RequestValidation.reject("malformed: invalid consumers")
    for consumer in consumers:
        if not isinstance(consumer, str) or consumer not in CONSUMERS:
            return RequestValidation.reject("malformed: unknown consumer")
    requested_at = body.get("requested_at")
    if parse_iso(requested_at) is None:
        return RequestValidation.reject("malformed: invalid requested_at")
    wanted = canonical_consumers(consumers)
    if "cc" in wanted and entry.get("gateway_id") is None:
        return RequestValidation.reject(CC_REQUIRES_GATEWAY_ID)
    return RequestValidation.accept(ServingRequest(
        request_id=basename_id,
        action=str(action),
        model_key=model_key,
        consumers=tuple(wanted),
        requested_at=str(requested_at),
    ))


def make_record(
    request_id: str, state: str, reason: str | None, now: datetime,
) -> dict[str, Any]:
    """One terminal `recent[]` entry. `reason` is ALWAYS emitted, `null` on the happy path: the
    reader defaults it (`ServingRequestRecordSchema`'s `.default(null)`), but every canonical
    writer and every committed fixture spells it, so this one does too."""
    return {
        "request_id": request_id,
        "state": state,
        "reason": reason,
        "applied_at": iso_z(now),
    }


def compose_status(
    declaration: dict[str, Any] | None,
    recent: Sequence[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """The whole `serving-status-v1` body, pure. `visibility` is the declaration's `models` map
    re-published verbatim (normalized through `canonical_consumers`, never re-derived from
    anything else -- it is the chips' only source of truth); `recent` is newest-first and bounded
    to `RECENT_LIMIT`; `last_request` is `recent[0]` or `null`, copied rather than aliased so a
    caller mutating one cannot silently change the other."""
    models = declared_models(declaration) or {}
    visibility = {
        key: {
            "gateway_id": entry.get("gateway_id"),
            "consumers": canonical_consumers(entry.get("consumers", [])),
        }
        for key, entry in models.items()
    }
    bounded = [dict(record) for record in list(recent)[:RECENT_LIMIT]]
    return {
        "schema": SCHEMA_STATUS,
        "generated_at": iso_z(now),
        "visibility": visibility,
        "last_request": dict(bounded[0]) if bounded else None,
        "recent": bounded,
    }


def _record_problem(record: Any) -> str | None:
    if not isinstance(record, dict):
        return "record is not a JSON object"
    if not isinstance(record.get("request_id"), str):
        return "record has no request_id"
    if record.get("state") not in STATES:
        return "record carries an unknown state"
    reason = record.get("reason", None)
    if reason is not None and not isinstance(reason, str):
        return "record reason is neither a string nor null"
    if not isinstance(record.get("applied_at"), str):
        return "record has no applied_at"
    return None


def validate_status_file(data: dict[str, Any] | None) -> str | None:
    """`None` when `data` is a well-formed `serving-status-v1` body, else a problem string. Used
    for the pass's own read-back of the previous status file (an unparseable/invalid one is
    treated as "no history", never partially trusted) and by the contract tests against the
    committed fixture bytes."""
    if not isinstance(data, dict):
        return "status is not a JSON object"
    if data.get("schema") != SCHEMA_STATUS:
        return f"status schema is not {SCHEMA_STATUS}"
    if parse_iso(data.get("generated_at")) is None:
        return "status generated_at is not a parseable ISO-8601 timestamp"
    visibility = data.get("visibility")
    if not isinstance(visibility, dict):
        return "status has no visibility map"
    for key, entry in visibility.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            return "status visibility entry is not a JSON object"
        gateway_id = entry.get("gateway_id")
        if gateway_id is not None and not isinstance(gateway_id, str):
            return "status visibility gateway_id is neither a string nor null"
        entry_consumers = entry.get("consumers")
        if not isinstance(entry_consumers, list):
            return "status visibility entry has no consumers list"
        for consumer in entry_consumers:
            if consumer not in CONSUMERS:
                return "status visibility entry carries an unknown consumer"
    recent = data.get("recent")
    if not isinstance(recent, list):
        return "status has no recent list"
    for record in recent:
        problem = _record_problem(record)
        if problem is not None:
            return problem
    last_request = data.get("last_request", None)
    if last_request is not None:
        problem = _record_problem(last_request)
        if problem is not None:
            return problem
    return None
