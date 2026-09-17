"""The HTTP layer: endpoint shapes, authentication, timeouts, read-after-write verification.

Every route this sync touches is spelled exactly once, here. Two repo-wide rules are enforced
in this module rather than left to callers: `_http_json` never raises on a non-2xx (callers
probe by response BODY, never by status code), and every mutating call that matters is read
back before it is believed -- a 200 from a route that did not move the flag is the canonical
"a healthy service has told you nothing" failure.

Test seam: the fixtures-only suite replaces `_http_json` ON THIS MODULE
(`sync.client._http_json = fake`); every function below resolves it through this module's
globals, so one patch covers the whole HTTP surface and nothing can slip past to a socket.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .errors import SyncError
from .types import OpenWebUIRow, OpenWebUIRows, RequestBody, RouterEntry

DEFAULT_ROUTER_URL = "http://127.0.0.1:4811"


REQUEST_TIMEOUT_S = 15


def _http_json(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = REQUEST_TIMEOUT_S,
) -> Tuple[int, Any]:
    """Issue an HTTP request and parse the response body as JSON.

    Returns (status_code, parsed_body). Never raises on non-2xx — the caller decides what a
    given status+body combination means (this fleet's standing rule: probe by body, not status
    code, so callers must be able to inspect error responses too).
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise SyncError(f"{method} {url} failed: {exc.reason}") from exc
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = text
    return status, parsed


def fetch_router_models(router_url: str) -> List[RouterEntry]:
    """GET <router>/v1/models, return the `data` list. Raises SyncError on any shape surprise."""
    status, body = _http_json("GET", router_url.rstrip("/") + "/v1/models")
    if status != 200 or not isinstance(body, dict) or "data" not in body:
        raise SyncError(
            f"router capability discovery failed: GET {router_url}/v1/models "
            f"returned status={status} body={body!r}"
        )
    return body["data"]


def fetch_openwebui_rows(openwebui_url: str, api_key: str) -> OpenWebUIRows:
    """Every Open WebUI workspace row, keyed by id: `/api/v1/models/base` ∪ `/api/v1/models/export`.

    Both admin routes are needed and neither is a superset of the other (live-verified
    2026-09-14): `/base` returns ONLY rows with `base_model_id: null`, so the derived effort rows
    are invisible there, while `/export` returns only the rows that HAVE a `base_model_id` (the
    derived ones), including inactive ones. Using `/base` alone would make the sync re-CREATE
    every derived row on every pass -- a silently non-idempotent run against a route that does
    not error on a duplicate id.

    A row only exists here once an admin (or this script) has created/customized that model at
    least once; an id absent from this map needs a `create`, not an `update`.
    """
    rows: OpenWebUIRows = {}
    for route in ("/api/v1/models/base", "/api/v1/models/export"):
        status, body = _http_json(
            "GET",
            openwebui_url.rstrip("/") + route,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if status != 200 or not isinstance(body, list):
            raise SyncError(
                f"Open WebUI model discovery failed: GET {openwebui_url}{route} "
                f"returned status={status} body={body!r}"
            )
        for row in body:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                rows[row["id"]] = row
    return rows


def apply_change(
    openwebui_url: str, api_key: str, action: str, body: RequestBody
) -> Tuple[int, Any]:
    if action == "create":
        url = openwebui_url.rstrip("/") + "/api/v1/models/create"
    elif action == "update":
        model_id = urllib.parse.quote(body["id"], safe="")
        url = openwebui_url.rstrip("/") + f"/api/v1/models/model/update?id={model_id}"
    else:
        raise SyncError(f"apply_change called with non-mutating action {action!r}")
    return _http_json(
        "POST",
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        body=body,
    )


def fetch_openwebui_row(
    openwebui_url: str, api_key: str, model_id: str
) -> Optional[OpenWebUIRow]:
    """Re-read ONE row via `GET /api/v1/models/model?id=<id>` (works for base and derived rows).

    Used after a `create` and after a `toggle`: a created row's `is_active` default is not
    assumed, and a toggle is verified by reading the value back rather than by trusting a 200
    (repo-wide rule: probe by response body, never status code). Returns None when the route
    answers anything other than 200-with-an-object (404 for an id that does not exist, 401 for a
    key that cannot see it) -- the caller reads None as "not readable back", which is a failure
    after a create, never a silent success.
    """
    quoted = urllib.parse.quote(model_id, safe="")
    status, body = _http_json(
        "GET",
        openwebui_url.rstrip("/") + f"/api/v1/models/model?id={quoted}",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if status != 200 or not isinstance(body, dict):
        return None
    return body


def toggle_is_active(openwebui_url: str, api_key: str, model_id: str) -> Tuple[int, Any]:
    """POST `/api/v1/models/model/toggle?id=...` -- a FLIP, never a setter.

    The caller must already have established that the observed value differs from the desired
    one; issuing this at a row that already matches would move it the wrong way. An empty JSON
    object is sent as the body so the request carries a Content-Length; the route takes its
    argument in the query string (the shape `docs/model-capabilities.md` names, confirmed by the
    user-present reversible probe in WP-3's hold).
    """
    quoted = urllib.parse.quote(model_id, safe="")
    return _http_json(
        "POST",
        openwebui_url.rstrip("/") + f"/api/v1/models/model/toggle?id={quoted}",
        headers={"Authorization": f"Bearer {api_key}"},
        body={},
    )


def apply_model(
    openwebui_url: str,
    api_key: str,
    router_id: str,
    action: str,
    body: RequestBody,
    active_action: str,
    wanted_active: bool,
) -> int:
    """Perform one model's create/update and, if needed, its `is_active` toggle.

    Returns the number of failures (0 or 1) and prints what it did. Order is load-bearing: the
    row must exist before it can be toggled, and a row this pass created is RE-READ rather than
    assumed to have defaulted either way. After a toggle the value is read back and compared --
    a 200 from a route that did not move the flag is exactly the "healthy service has told you
    nothing" failure this repo gates against.
    """
    if action != "noop":
        status, resp_body = apply_change(openwebui_url, api_key, action, body)
        if status not in (200, 201):
            print(f"    APPLY FAILED status={status} body={resp_body!r}")
            return 1
        print(f"    applied (status={status})")

    if action == "create":
        created = fetch_openwebui_row(openwebui_url, api_key, router_id)
        if created is None:
            print("    APPLY FAILED: created row not readable back from /api/v1/models/model")
            return 1
        observed = bool(created.get("is_active"))
        print(f"    is_active on the created row: {observed!r} (re-read, not assumed)")
        needs_toggle = observed != wanted_active
    else:
        needs_toggle = active_action == "toggle"

    if not needs_toggle:
        return 0

    status, resp_body = toggle_is_active(openwebui_url, api_key, router_id)
    if status not in (200, 201):
        print(f"    APPLY FAILED (toggle) status={status} body={resp_body!r}")
        return 1
    print(f"    toggled is_active (status={status}) body={resp_body!r}")

    after = fetch_openwebui_row(openwebui_url, api_key, router_id)
    observed_after = None if after is None else bool(after.get("is_active"))
    if observed_after != wanted_active:
        print(
            f"    APPLY FAILED (toggle): is_active reads back {observed_after!r}, "
            f"wanted {wanted_active!r}"
        )
        return 1
    print(f"    is_active verified {observed_after!r} on re-read")
    return 0


def retire_row(openwebui_url: str, api_key: str, row_id: str) -> int:
    """Drive one orphaned row to `is_active: false` via the toggle route. Returns failures.

    Only ever called for a row observed ACTIVE (the route is a flip, so calling it on an already
    inactive row would REVIVE the orphan), and the result is read back rather than trusted from
    the status code. This never deletes: hiding a model is deactivation.
    """
    status, resp_body = toggle_is_active(openwebui_url, api_key, row_id)
    if status not in (200, 201):
        print(f"    APPLY FAILED (retire) status={status} body={resp_body!r}")
        return 1
    after = fetch_openwebui_row(openwebui_url, api_key, row_id)
    observed_after = None if after is None else bool(after.get("is_active"))
    if observed_after is not False:
        print(
            f"    APPLY FAILED (retire): is_active reads back {observed_after!r}, wanted False"
        )
        return 1
    print("    retired: is_active verified False on re-read")
    return 0
