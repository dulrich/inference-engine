#!/usr/bin/env python3
"""Sync llama-router capability metadata into Open WebUI's per-model admin config.

Source of truth: `GET <router>/v1/models` (default `http://127.0.0.1:4811`) — the capability
contract pinned in `docs/model-capabilities.md`, merged flat under `meta.llamaswap` on each
entry. `id` on that endpoint is the config model KEY (not the alias); that is also the id Open
WebUI stores its per-model rows under (see the consumer-registry row in `llama/config.yaml`'s
header).

Target: Open WebUI's admin "Models" API (`/api/v1/models/*`, live-verified 2026-08-24 against
v0.11.0). This script owns exactly three sub-fields of a model's stored `meta`, plus the
allowlisted numeric sampling keys inside `params` (added by
`plans/overnight-test-matrix-redesign.md` WP-5) — and only those. Everything else already
present on a row (name, the rest of `params`, access_grants, other capability toggles a human
set, tool ids, default features, ...) is read back and round-tripped unchanged, because Open
WebUI's update route replaces `name`/`meta`/`params` wholesale rather than patching:

  * `meta.capabilities.vision`      <- bool, `"image" in input_modalities`
  * `meta.description`              <- human summary (name + tool calling + reasoning level +
                                        quant/KV/flash-attention/MTP + context length)
  * `meta.llamaswap.context_length` <- int, `meta.llamaswap.context_length` on the router
  * `params.<SAMPLING_PARAM_KEYS>`  <- per-key merge from `meta.llamaswap.sampling` on the
                                        router (only the numeric allowlist below; `sampling.source`
                                        is router metadata only and is never written to `params`;
                                        every other `params` key on the row is human-owned and
                                        round-trips untouched)

Rows Open WebUI has never seen (no admin has opened/customized that model yet) don't exist in
its DB at all — this script creates them via `POST .../create` with a minimal body (id, the
router's display name, the three managed `meta` fields, and `params` seeded with whatever
allowlisted sampling keys the router model carries — `{}` for a model with no sampling block).
Existing rows are merge-updated via `POST .../model/update`. A router id with no live
counterpart (a retired/renamed model key — see the alias-contract discussion in
`llama/config.yaml`'s header) is reported and left untouched; this script never deletes.

Credentials: `OPENWEBUI_URL` and `OPENWEBUI_API_KEY` come from the environment (or the
`--openwebui-url`/`--openwebui-key` flags) — there is no default and nothing is ever written to
a file or logged. The router URL is not a credential and defaults to the loopback publish.

Dry-run (the default) prints the intended per-model diff and makes no mutating call. `--apply`
performs the sync; running `--apply` twice in a row must show an empty diff on the second run
(the desired state is a pure function of the router's live capability block).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_ROUTER_URL = "http://127.0.0.1:4811"
REQUEST_TIMEOUT_S = 15

# The only `meta.llamaswap.sampling` keys this script ever writes into an Open WebUI model
# row's `params`. Explicit allowlist (plans/overnight-test-matrix-redesign.md WP-5, audit E-4
# pin): `sampling.source` is a router-metadata provenance citation, never a serving parameter,
# and must never appear here -- a literal "map every sampling key" implementation would write
# an invalid `params.source`, which is exactly the defect this allowlist exists to prevent.
SAMPLING_PARAM_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
)


class SyncError(RuntimeError):
    """Raised for any non-recoverable discovery/apply failure; message is safe to print."""


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


def fetch_router_models(router_url: str) -> List[Dict[str, Any]]:
    """GET <router>/v1/models, return the `data` list. Raises SyncError on any shape surprise."""
    status, body = _http_json("GET", router_url.rstrip("/") + "/v1/models")
    if status != 200 or not isinstance(body, dict) or "data" not in body:
        raise SyncError(
            f"router capability discovery failed: GET {router_url}/v1/models "
            f"returned status={status} body={body!r}"
        )
    return body["data"]


def fetch_openwebui_rows(openwebui_url: str, api_key: str) -> Dict[str, Dict[str, Any]]:
    """GET <openwebui>/api/v1/models/base (admin-only), keyed by id.

    A row only exists here once an admin has created/customized it in Open WebUI at least once;
    a router id absent from this map has never been touched and needs a `create`, not an
    `update`.
    """
    status, body = _http_json(
        "GET",
        openwebui_url.rstrip("/") + "/api/v1/models/base",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if status != 200 or not isinstance(body, list):
        raise SyncError(
            f"Open WebUI base-model discovery failed: GET {openwebui_url}/api/v1/models/base "
            f"returned status={status} body={body!r}"
        )
    return {row["id"]: row for row in body}


def build_description(entry: Dict[str, Any]) -> str:
    """Compose the human summary description from name + capability facts. Pure function of
    the router entry — no timestamps, no external state, so re-running produces an identical
    string until the underlying serving facts change."""
    name = entry.get("name") or entry["id"]
    ls = entry.get("meta", {}).get("llamaswap", {})
    serving = ls.get("serving", {})

    bits = [f"{serving.get('quant', 'unknown quant')} quant", f"{serving.get('kv_cache_type', 'f16')} KV cache"]
    if serving.get("flash_attention"):
        bits.append("flash-attention")
    if serving.get("mtp"):
        bits.append("MTP speculative decoding")
    if "moe_cpu_offload_layers" in serving:
        bits.append(f"{serving['moe_cpu_offload_layers']} MoE layers offloaded to CPU")
    if "ubatch" in serving:
        bits.append(f"-ub {serving['ubatch']}")
    serving_str = ", ".join(bits)

    tool_str = "yes" if ls.get("tool_calling") else "no"
    reasoning = ls.get("reasoning", "none")
    ctx = ls.get("context_length")

    modality_bits = []
    if "image" in ls.get("input_modalities", []):
        modality_bits.append("vision (image input)")
    output_modalities = ls.get("output_modalities", [])
    if output_modalities and output_modalities != ["text"]:
        modality_bits.append("output: " + "/".join(output_modalities))
    modality_str = f"; {', '.join(modality_bits)}" if modality_bits else ""

    return (
        f"{name} — {serving_str}. Tool calling: {tool_str}; reasoning: {reasoning}; "
        f"context: {ctx} tokens{modality_str}. Synced from inference-engine capability "
        "metadata (llama router /v1/models)."
    )


def compute_desired(entry: Dict[str, Any]) -> Dict[str, Any]:
    ls = entry.get("meta", {}).get("llamaswap", {})
    sampling = ls.get("sampling") or {}
    return {
        "vision": "image" in ls.get("input_modalities", []),
        "description": build_description(entry),
        "context_length": ls.get("context_length"),
        # Allowlist filter: only the numeric sampling keys, never `source` (audit E-4 pin). A
        # model with no `sampling` block (embed/rerank) yields {} here, so it is a no-op on the
        # params side below.
        "sampling_params": {
            key: sampling[key] for key in SAMPLING_PARAM_KEYS if key in sampling
        },
    }


def plan_change(
    router_id: str,
    router_name: str,
    desired: Dict[str, Any],
    existing: Optional[Dict[str, Any]],
) -> Tuple[str, List[Tuple[str, Any, Any]], Dict[str, Any]]:
    """Return (action, field_diffs, request_body) for one model.

    action is "create", "update", or "noop". field_diffs is a list of (field, old, new) for
    only the managed sub-fields that differ: the three `meta` fields, plus one entry per
    allowlisted sampling key (`params.<key>`) whose value changed. request_body is the full
    JSON body to send to Open WebUI's create/update route if applying (unmodified fields
    round-tripped verbatim from `existing`, including every non-allowlisted `params` key).
    """
    sampling_params: Dict[str, Any] = desired["sampling_params"]

    if existing is None:
        body = {
            "id": router_id,
            "name": router_name,
            "meta": {
                "description": desired["description"],
                "capabilities": {"vision": desired["vision"]},
                "llamaswap": {"context_length": desired["context_length"]},
            },
            "params": dict(sampling_params),
        }
        diffs = [
            ("meta.capabilities.vision", None, desired["vision"]),
            ("meta.description", None, desired["description"]),
            ("meta.llamaswap.context_length", None, desired["context_length"]),
        ]
        for key, value in sampling_params.items():
            diffs.append((f"params.{key}", None, value))
        return "create", diffs, body

    # Round-trip every existing field; only touch the three `meta` fields and the allowlisted
    # sampling keys in `params` that we own.
    meta = dict(existing.get("meta") or {})
    capabilities = dict(meta.get("capabilities") or {})
    llamaswap = dict(meta.get("llamaswap") or {})
    params = dict(existing.get("params") or {})

    diffs: List[Tuple[str, Any, Any]] = []
    old_vision = capabilities.get("vision")
    if old_vision != desired["vision"]:
        diffs.append(("meta.capabilities.vision", old_vision, desired["vision"]))
    old_description = meta.get("description")
    if old_description != desired["description"]:
        diffs.append(("meta.description", old_description, desired["description"]))
    old_context_length = llamaswap.get("context_length")
    if old_context_length != desired["context_length"]:
        diffs.append(
            ("meta.llamaswap.context_length", old_context_length, desired["context_length"])
        )
    # Per-key merge: only keys present in the router's sampling block are compared/written;
    # every other existing `params` key (human-owned) is never inspected or touched.
    for key, value in sampling_params.items():
        old_value = params.get(key)
        if old_value != value:
            diffs.append((f"params.{key}", old_value, value))

    if not diffs:
        return "noop", diffs, {}

    capabilities["vision"] = desired["vision"]
    meta["capabilities"] = capabilities
    meta["description"] = desired["description"]
    llamaswap["context_length"] = desired["context_length"]
    meta["llamaswap"] = llamaswap
    params.update(sampling_params)

    body = {
        "id": existing["id"],
        "name": existing.get("name"),
        "meta": meta,
        "params": params,
    }
    if existing.get("base_model_id") is not None:
        body["base_model_id"] = existing["base_model_id"]
    if existing.get("access_grants") is not None:
        body["access_grants"] = existing["access_grants"]
    return "update", diffs, body


def apply_change(
    openwebui_url: str, api_key: str, action: str, body: Dict[str, Any]
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


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Sync llama-router /v1/models capability metadata into Open WebUI per-model config."
        ),
        epilog=(
            "OPENWEBUI_URL and OPENWEBUI_API_KEY must come from the environment or the "
            "--openwebui-url/--openwebui-key flags -- there is no default. Prefer the "
            "environment variables: a flag value is visible in shell history and process "
            "listings on a shared host."
        ),
    )
    p.add_argument(
        "--router-url",
        default=os.environ.get("LLAMA_ROUTER_URL", DEFAULT_ROUTER_URL),
        help=f"llama router base URL (default: {DEFAULT_ROUTER_URL}, or $LLAMA_ROUTER_URL)",
    )
    p.add_argument(
        "--openwebui-url",
        default=os.environ.get("OPENWEBUI_URL"),
        help="Open WebUI base URL (default: $OPENWEBUI_URL; required)",
    )
    p.add_argument(
        "--openwebui-key",
        default=os.environ.get("OPENWEBUI_API_KEY"),
        help="Open WebUI admin API key (default: $OPENWEBUI_API_KEY; required, never logged)",
    )
    p.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="MODEL_ID",
        help="restrict the sync to this router model id (repeatable); default is all models",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="perform the mutating sync; default is dry-run (print the diff, change nothing)",
    )
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)

    if not args.openwebui_url:
        print(
            "error: Open WebUI base URL not set -- pass --openwebui-url or set $OPENWEBUI_URL",
            file=sys.stderr,
        )
        return 2
    if not args.openwebui_key:
        print(
            "error: Open WebUI API key not set -- pass --openwebui-key or set "
            "$OPENWEBUI_API_KEY",
            file=sys.stderr,
        )
        return 2

    try:
        router_models = fetch_router_models(args.router_url)
        existing_rows = fetch_openwebui_rows(args.openwebui_url, args.openwebui_key)
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.only:
        wanted = set(args.only)
        router_models = [m for m in router_models if m["id"] in wanted]
        missing = wanted - {m["id"] for m in router_models}
        for mid in sorted(missing):
            print(f"warning: --only {mid} not found in router /v1/models", file=sys.stderr)

    router_models = sorted(router_models, key=lambda m: m["id"])
    router_ids = {m["id"] for m in router_models}

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== sync-openwebui-capabilities: {mode} ===")
    print(f"router:     {args.router_url}")
    print(f"open webui: {args.openwebui_url}")
    print(f"models:     {len(router_models)}")
    print()

    counts = {"create": 0, "update": 0, "noop": 0}
    failures = 0

    for entry in router_models:
        router_id = entry["id"]
        router_name = entry.get("name") or router_id
        ls = entry.get("meta", {}).get("llamaswap", {})
        if ls.get("type") not in (None, "model"):
            print(f"[{router_id}] skip: meta.llamaswap.type={ls.get('type')!r}, not a model")
            continue

        desired = compute_desired(entry)
        existing = existing_rows.get(router_id)
        action, diffs, body = plan_change(router_id, router_name, desired, existing)
        counts[action] += 1

        label = {"create": "CREATE", "update": "UPDATE", "noop": "no-op"}[action]
        print(f"[{router_id}] {label}")
        for field, old, new in diffs:
            print(f"    {field}: {old!r} -> {new!r}")
        if action == "noop":
            print("    (already in sync)")

        if args.apply and action != "noop":
            status, resp_body = apply_change(args.openwebui_url, args.openwebui_key, action, body)
            if status not in (200, 201):
                failures += 1
                print(f"    APPLY FAILED status={status} body={resp_body!r}")
            else:
                print(f"    applied (status={status})")
        print()

    stale = sorted(set(existing_rows) - router_ids)
    if stale:
        print("Open WebUI rows with no current router match (left untouched, never deleted):")
        for row_id in stale:
            print(f"    {row_id}")
        print()

    print(
        f"summary: {counts['create']} create, {counts['update']} update, "
        f"{counts['noop']} unchanged, {len(stale)} orphaned row(s) left untouched"
    )
    if args.apply and failures:
        print(f"error: {failures} apply call(s) failed -- see APPLY FAILED lines above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
