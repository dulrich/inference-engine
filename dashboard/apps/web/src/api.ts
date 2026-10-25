import {
  HealthResponseSchema,
  RouterSidecarResponseSchema,
  RouterTelemetryResponseSchema,
  ServingFactsStatusResponseSchema,
  ServingMeasuredEntrySchema,
  ServingResponseSchema,
  ServingLiveResponseSchema,
  ServingStatusResponseSchema,
  ServingTimingRowSchema,
  type ServingAcceptedResponse,
  type HealthResponse,
  type RouterSidecarResponse,
  type RouterTelemetryResponse,
  type ServingCatalogAddRequest,
  type ServingCatalogConsumersRequest,
  type ServingCatalogRemoveRequest,
  type ServingFactsStatusResponse,
  type ServingMeasuredEntry,
  type ServingResponse,
  type ServingLiveResponse,
  type ServingStatusResponse,
  type ServingTimingRow,
  type ServingVisibilityRequest,
} from "@bench-dashboard/shared";
import { parseControlActionResponse } from "./lib/serving/control.ts";

// The fetch seam for every view: the serving, catalog and router reads/writes. The research
// routes (experiments, ladders, curves, queue, bake-off control) moved to the research
// dashboard (eval-harness `inference_research`, :4816) and are not fetched here.

async function getJson<T>(path: string, schema: { parse: (value: unknown) => T }): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    // The API's own error routes (e.g. `/api/serving`'s 502 on a bad LLAMA_CONFIG_PATH)
    // return `{ error: string }` -- surface that message when present rather than a bare
    // status code, so a view's error state reads as actionable.
    const body = await response.json().catch(() => null);
    const message = body && typeof body === "object" && "error" in body ? String((body as { error: unknown }).error) : null;
    throw new Error(message ?? `${path}: HTTP ${response.status}`);
  }
  return schema.parse(await response.json());
}

export function getHealth(): Promise<HealthResponse> {
  return getJson("/health", HealthResponseSchema);
}

export function getServing(): Promise<ServingResponse> {
  return getJson("/api/serving", ServingResponseSchema);
}

// `GET /api/serving/{timing,measured,facts-status}` (`research-to-eval-harness` WP-4): the
// research side's `serving-facts-v1` file, read server-side. Timing and measured are always a
// 200 array (`[]` when the facts are unavailable); the status route says why, as a typed
// `{available:false, reason}`. Joined client-side (`lib/serving/{matchTiming,matchMeasured}.ts`).
export function getServingTiming(): Promise<ServingTimingRow[]> {
  return getJson("/api/serving/timing", ServingTimingRowSchema.array());
}

export function getServingMeasured(): Promise<ServingMeasuredEntry[]> {
  return getJson("/api/serving/measured", ServingMeasuredEntrySchema.array());
}

export function getServingFactsStatus(): Promise<ServingFactsStatusResponse> {
  return getJson("/api/serving/facts-status", ServingFactsStatusResponseSchema);
}

// The drop-box POSTs (serving visibility, catalog intents) -- each route only ever RECORDS a
// request into the host dispatcher's drop-box; `accepted: true` means *recorded*, never
// *executed*. Response parsing/error-extraction is `parseControlActionResponse`
// (`lib/serving/control.ts`) so it's unit-testable without mocking `fetch`; this wrapper is
// the thin fetch-plumbing seam only, same division as `getJson` above.
async function postControlAction(path: string, body?: unknown): Promise<ServingAcceptedResponse> {
  const response = await fetch(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const json = await response.json().catch(() => null);
  const outcome = parseControlActionResponse(response.status, json);
  if (!outcome.ok || outcome.accepted === null) {
    throw new Error(outcome.error ?? `${path}: HTTP ${response.status}`);
  }
  return outcome.accepted;
}

// `GET /api/serving/status` + `POST /api/serving/visibility` (WP-4, `plans/bench-dashboard-
// serving-control.md`) -- the serving-visibility control pair, fetched by `Serving.svelte`
// ALONGSIDE `getServing()`/`getServingTiming()`. The status read is a discriminated union on
// `available` (an unset env var, a missing/unparseable/empty file, or a `generated_at` older
// than 180 s is a typed `{available:false, reason}` at 200, never an error), so only a
// genuine transport/parse failure throws out of it -- the router-status posture. The POST
// only ever RECORDS a request into the host dispatcher's drop-box: `accepted: true` means
// *recorded*, never *executed*, and the chips learn the real outcome from the next status
// poll.
export function getServingStatus(): Promise<ServingStatusResponse> {
  return getJson("/api/serving/status", ServingStatusResponseSchema);
}

// `GET /api/serving/live` (serving-control-redesign WP-6): resident models per tier from the
// host status collector's feed; a typed `{available:false, reason}` at 200 when off/stale.
export function getServingLive(): Promise<ServingLiveResponse> {
  return getJson("/api/serving/live", ServingLiveResponseSchema);
}

export function postServingVisibility(body: ServingVisibilityRequest): Promise<ServingAcceptedResponse> {
  return postControlAction("/api/serving/visibility", body);
}

// `POST /api/catalog/{add,remove,consumers}` (WP-4, `plans/serving-control-redesign.md`) --
// the model-catalog intents (`serving-request-v2`). Same posture as the visibility POST: the
// API writes a drop-box file and returns the request id; the dispatcher validates the
// candidate catalog, applies it, and reports the outcome in the status file's `recent[]`.
export function postCatalogAdd(body: ServingCatalogAddRequest): Promise<ServingAcceptedResponse> {
  return postControlAction("/api/catalog/add", body);
}

export function postCatalogRemove(body: ServingCatalogRemoveRequest): Promise<ServingAcceptedResponse> {
  return postControlAction("/api/catalog/remove", body);
}

export function postCatalogConsumers(body: ServingCatalogConsumersRequest): Promise<ServingAcceptedResponse> {
  return postControlAction("/api/catalog/consumers", body);
}

// `GET /api/router/{telemetry,sidecar}` (WP-3/WP-4, `plans/router-observability-and-
// tool-search-conformance.md`; `/slots` retired at serving-control-redesign WP-6) -- the
// Router tab's independent read-only feeds. Each
// is a discriminated union on `available`: an unset env var or an unreadable/empty source
// directory is a typed `{available:false, reason}` at 200, NOT an error, so only a genuine
// transport/parse failure ever throws out of these. `/telemetry` accepts an
// optional `?staleMs`; the UI deliberately does not surface it -- the API's defaults are the
// contract.
export function getRouterTelemetry(): Promise<RouterTelemetryResponse> {
  return getJson("/api/router/telemetry", RouterTelemetryResponseSchema);
}

export function getRouterSidecar(): Promise<RouterSidecarResponse> {
  return getJson("/api/router/sidecar", RouterSidecarResponseSchema);
}
