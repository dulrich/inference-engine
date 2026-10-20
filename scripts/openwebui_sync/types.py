"""Shapes that cross a seam between the loader, planner, client and runner layers.

`TypedDict` is used rather than dataclasses wherever the value is also a wire/JSON document
(a router entry, an Open WebUI row, the policy): those stay plain dicts at runtime, so the
JSON they came from and the JSON sent back are the same object shape, with no lossy
round-trip through a constructor. Genuinely internal aggregates ARE dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:  # pragma: no cover - typing-only convenience
    from typing import TypedDict
except ImportError:  # pragma: no cover - Python < 3.8
    TypedDict = dict  # type: ignore[assignment, misc]


class VisibilityEntry(TypedDict, total=False):
    """One llama model key's visibility, projected from the model catalog
    (`declarations.load_visibility`): `gateway_id` is the `claude-local` entry's id, or None for an
    `exposures` record."""

    consumers: List[str]
    gateway_id: Optional[str]


# The catalog's visibility projection, keyed by `llama/config.yaml` model key.
VisibilityModels = Dict[str, VisibilityEntry]

# `scripts/openwebui-policy.json` after validation: exactly POLICY_LIST_FIELDS -> list of str.
Policy = Dict[str, List[str]]

# {gateway id: [effort level, ...]} from the catalog entries' `effortKwargs`.
EffortLevels = Dict[str, List[str]]

# One `GET <router>/v1/models` `data` entry, and one Open WebUI `/api/v1/models/*` row.
RouterEntry = Dict[str, Any]
OpenWebUIRow = Dict[str, Any]

# Rows keyed by Open WebUI id (`/base` union `/export`).
OpenWebUIRows = Dict[str, OpenWebUIRow]

# One printed managed-field change: (dotted field, old value, new value).
FieldDiff = Tuple[str, Any, Any]

# The request body handed to the create/update route (`{}` for a no-op).
RequestBody = Dict[str, Any]


class DesiredRow(TypedDict):
    """What the router says a row must carry: the three managed `meta` fields plus sampling."""

    vision: bool
    description: str
    context_length: Optional[int]
    sampling_params: Dict[str, Any]


@dataclass
class RetirementPlan:
    """The partition of every Open WebUI row this sync does not currently manage.

    `active` is the only list that may be mutated, and only outside `--only`; the other two
    exist so the operator sees what was considered and why it was left alone.
    """

    active: List[str] = field(default_factory=list)
    already_inactive: List[str] = field(default_factory=list)
    unmanaged: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.active or self.already_inactive or self.unmanaged)


@dataclass
class RowOutcome:
    """What one planned/executed row contributed: its printed action and its failure count."""

    action: str
    failures: int = 0
