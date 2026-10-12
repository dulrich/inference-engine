#!/usr/bin/env python3
"""The control plane's on-disk PROTOCOL: schema names, id/stem grammars, bounds, timestamp
spelling, and the one function that decides whether an untrusted drop-box request body is a
`control-request-v1` at all (WP-R4 of `plans/repository-tooling-structural-remediation.md`;
review finding F4 -- boundary type contracts).

Extracted verbatim from `control_dispatcher.py`'s "Schema names"/"Grammars and bounds" banners
plus its `_validate_request_body`, with ONE deliberate behavior change (WP-R4's only one): the
declared `schema` field is now REQUIRED to equal `SCHEMA_REQUEST`. Before this module existed
the constant was defined and never read -- the dispatcher inspected `request_id`/`action`/
`requested_at`/`queue_stem`/`minutes`/`target_request_id` and treated `schema` as a tolerated
extra key, so a future incompatible `control-request-v2` would have been ACCEPTED for as long as
the fields this validator happens to look at kept their v1 spelling. See
`REQUEST_SCHEMA_REJECTION` below for the fail-closed disposition that replaces that.

This module is deliberately the pure end of the split: no locking, no atomic writes, no `/proc`,
no process spawning. Its single filesystem touch is the queue-stem existence check inside
`validate_request_body`, and that is over a caller-supplied `queues_dir` (every test passes a
tempdir), never a module-level default.

Typed values follow `cell_model.py`'s WP-R1 pattern: frozen dataclasses whose fields are exactly
the contract, so a non-contract key cannot be bolted on after construction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "GRACE_SECONDS",
    "HOST_ID_RE",
    "LAST_RUN_DISPOSITIONS",
    "MAX_REQUEST_BYTES",
    "MINUTES_MAX",
    "MINUTES_MIN",
    "PRUNE_AGE_SECONDS",
    "QUEUE_STEM_RE",
    "REQUEST_BASENAME_RE",
    "REQUEST_SCHEMA_REJECTION",
    "SCHEMA_ACTION",
    "SCHEMA_CLAIM",
    "SCHEMA_DISPOSITION",
    "SCHEMA_REQUEST",
    "SCHEMA_RESULT",
    "VALID_ACTIONS",
    "WIRE_STATES",
    "IngestedRequest",
    "RequestValidation",
    "iso_z",
    "parse_iso",
    "utc_now_iso",
    "validate_request_body",
]

# -------------------------------------------------------------------------------------------
# Schema names -- these name the on-disk shapes documented in `plans/bakeoff-queue-control-plane
# .md`'s "Request schema and lifecycle" section. Only `SCHEMA_REQUEST` is a WIRE requirement: the
# CONTAINER-written request file carries `"schema": "control-request-v1"` (WP-4's
# `ControlRequestFileV1`, and now `ControlRequestFileSchema` in
# `dashboard/packages/shared/src/control.ts`), and `validate_request_body` REQUIRES it. The other
# four name host-written shapes that this module's writers stamp implicitly; none of those files
# embed a literal `schema` field, matching the plan's own JSON examples exactly.
# -------------------------------------------------------------------------------------------

SCHEMA_REQUEST = "control-request-v1"
SCHEMA_CLAIM = "control-claim-v1"
SCHEMA_RESULT = "control-result-v1"
SCHEMA_ACTION = "control-action-v1"
SCHEMA_DISPOSITION = "control-disposition-v1"

# -------------------------------------------------------------------------------------------
# Grammars and bounds (plan Dispatcher phase 1 "Ingest -- hardened").
# -------------------------------------------------------------------------------------------

REQUEST_BASENAME_RE = re.compile(r"^req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.json$")
HOST_ID_RE = re.compile(r"^(req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}|nightly-[0-9]{8})$")
QUEUE_STEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

VALID_ACTIONS = ("arm", "disarm", "start", "stop", "cancel")
MAX_REQUEST_BYTES = 8192
MINUTES_MIN, MINUTES_MAX = 15, 480

# Terminalization grace: "measured from the claim's claimed_at timestamp being >=5 minutes old at
# pass time (minutely cadence proxy) -- the dispatcher is stateless across passes; no in-memory
# pass counters" (WP-3 brief, pinning cycle-1 B3/cycle-2 B1's "5 passes" as wall-clock minutes).
GRACE_SECONDS = 5 * 60

WIRE_STATES = ("pending", "claimed", "running", "done", "rejected", "canceled", "launch-failed")
LAST_RUN_DISPOSITIONS = ("clean", "needs-attention", "crashed")

PRUNE_AGE_SECONDS = 30 * 24 * 3600

# The single fail-closed reason for every schema-version failure -- ABSENT, wrong-typed, and
# unknown all collapse to this one fixed string on purpose. Two reasons for the collapse: the
# disposition `reason` is rendered verbatim in the dashboard's request digest, and the request
# body is CONTAINER-written (outside the trust boundary), so echoing the offending value back
# into a host-written durable file would put untrusted content on a trusted surface; and a
# producer that got the version wrong is a producer to fix, not a case to triage three ways. It
# is spelled in the existing `"malformed: ..."` vocabulary because it IS one of those: a normal
# ingest rejection, written through the same `write_disposition` call as every other one, never a
# crash and never a silent skip.
REQUEST_SCHEMA_REJECTION = f"malformed: schema is not {SCHEMA_REQUEST}"


def iso_z(dt: datetime) -> str:
    """`...Z`-suffixed UTC ISO-8601, matching the plan's own JSON examples and the
    `req-<UTCcompact>-...` id grammar's own timestamp convention (never `+00:00`)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    return iso_z(datetime.now(UTC))


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@dataclass(frozen=True)
class IngestedRequest:
    """One validated `control-request-v1`, carrying EXACTLY the fields the act phase is allowed
    to read. Frozen (`cell_model.CellIdentity`'s pattern): a later phase cannot attach a derived
    key to it, so "what came off the wire" and "what we computed" can never blur.

    `request_id` is always the basename-derived id -- never the body's own copy of it, even
    though `validate_request_body` has just proven the two are equal."""

    request_id: str
    action: str
    queue_stem: str | None
    minutes: int | None
    target_request_id: str | None
    requested_at: str


@dataclass(frozen=True)
class RequestValidation:
    """Exactly one of `request`/`reason` is set: `accept()` yields a request and no reason,
    `reject()` a reason and no request. Replaces the old `Tuple[Optional[IngestedRequest],
    Optional[str]]` return, where nothing but caller discipline said which half to read."""

    request: IngestedRequest | None = None
    reason: str | None = None

    @classmethod
    def accept(cls, request: IngestedRequest) -> RequestValidation:
        return cls(request=request, reason=None)

    @classmethod
    def reject(cls, reason: str) -> RequestValidation:
        return cls(request=None, reason=reason)

    @property
    def accepted(self) -> bool:
        return self.request is not None


def validate_request_body(basename_id: str, data: bytes, queues_dir: Path) -> RequestValidation:
    """Fail-closed validation of one drop-box request body (plan Dispatcher phase 1). Every
    rejection's `reason` is a fixed `"malformed: ..."` string, never derived from body content.

    Check order is load-bearing and unchanged apart from the schema insertion:

    1. parseable UTF-8 JSON, and a JSON object;
    2. `request_id` equals the trusted basename -- FIRST among the content checks because every
       host output path (claims, actions, dispositions) is derived from that basename, so this
       equality is the one place body content is allowed to matter at all;
    3. `schema` equals `SCHEMA_REQUEST` -- immediately after the identity check and before any
       field-by-field interpretation, because the version is what says those fields MEAN what
       this function thinks they mean;
    4. the per-field/per-action checks, exactly as before.
    """
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RequestValidation.reject("malformed: invalid JSON")
    if not isinstance(body, dict):
        return RequestValidation.reject("malformed: not a JSON object")
    if body.get("request_id") != basename_id:
        return RequestValidation.reject("malformed: request_id does not match filename")
    # WP-R4's one deliberate behavior change. `.get` returns None when absent and the `!=`
    # comparison rejects any non-str value, so absent / wrong-typed / unknown are one branch.
    if body.get("schema") != SCHEMA_REQUEST:
        return RequestValidation.reject(REQUEST_SCHEMA_REJECTION)
    action = body.get("action")
    if action not in VALID_ACTIONS:
        return RequestValidation.reject("malformed: invalid action")
    requested_at = body.get("requested_at")
    if not isinstance(requested_at, str) or not requested_at:
        return RequestValidation.reject("malformed: missing requested_at")
    queue_stem = body.get("queue_stem")
    minutes = body.get("minutes")
    target_request_id = body.get("target_request_id")
    if action == "start":
        if not isinstance(queue_stem, str) or not QUEUE_STEM_RE.match(queue_stem):
            return RequestValidation.reject("malformed: invalid queue_stem")
        if not (queues_dir / f"{queue_stem}.json").exists():
            return RequestValidation.reject("malformed: unknown queue_stem")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not (MINUTES_MIN <= minutes <= MINUTES_MAX):
            return RequestValidation.reject("malformed: invalid minutes")
    if action == "cancel":
        if not isinstance(target_request_id, str) or not HOST_ID_RE.match(target_request_id):
            return RequestValidation.reject("malformed: invalid target_request_id")
    return RequestValidation.accept(IngestedRequest(
        request_id=basename_id, action=action,
        queue_stem=queue_stem if isinstance(queue_stem, str) else None,
        minutes=minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else None,
        target_request_id=target_request_id if isinstance(target_request_id, str) else None,
        requested_at=requested_at,
    ))
