import {
  ExperimentFullDetailSchema,
  type ExperimentFullDetail,
  CurveSeriesSchema,
  ExperimentSummarySchema,
  HealthResponseSchema,
  LadderRowSchema,
  QueueListResponseSchema,
  RouterSidecarResponseSchema,
  RouterTelemetryResponseSchema,
  ServingResponseSchema,
  ServingLiveResponseSchema,
  ServingStatusResponseSchema,
  ServingTimingRowSchema,
  type ControlAcceptedResponse,
  type ControlCancelRequest,
  type ControlStartRequest,
  type CurveSeries,
  type ExperimentSummary,
  type HealthResponse,
  type LadderRow,
  type QueueListResponse,
  type RouterSidecarResponse,
  type RouterTelemetryResponse,
  type ServingCatalogAddRequest,
  type ServingCatalogConsumersRequest,
  type ServingCatalogRemoveRequest,
  type ServingResponse,
  type ServingLiveResponse,
  type ServingStatusResponse,
  type ServingTimingRow,
  type ServingVisibilityRequest,
} from "@bench-dashboard/shared";
export type { ExperimentFullDetail };
import { parseControlActionResponse } from "./lib/queue/control.ts";

// The fetch seam for every view (WP-2 builds on top of the routes WP-1 already ships:
// `/api/experiments`, `/api/experiments/:id`, `/api/ladders/*`, `/api/curves/:id`,
// `/api/serving`).

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

export function getExperiments(): Promise<ExperimentSummary[]> {
  return getJson("/api/experiments", ExperimentSummarySchema.array());
}

// `GET /api/experiments/:id` returns the summary plus every trial/metric row
// (`crates/server/src/app.rs`) -- the generated `ExperimentFullDetailSchema` carries that shape.
export function getExperimentDetail(id: string): Promise<ExperimentFullDetail> {
  return getJson(`/api/experiments/${encodeURIComponent(id)}`, ExperimentFullDetailSchema);
}

// `hfRepo` (e.g. `unsloth/gemma-4-12B-it-qat-GGUF`) contains a `/` -- encode each path
// segment individually so the slash survives as a path separator (matching the server's
// own wildcard route, `apps/api/src/routes/ladders.ts`, which strips the `/api/ladders/`
// prefix and decodeURIComponents whatever remains as one repo id).
export function getLadders(hfRepo: string): Promise<LadderRow[]> {
  const path = `/api/ladders/${hfRepo.split("/").map(encodeURIComponent).join("/")}`;
  return getJson(path, LadderRowSchema.array());
}

export function getCurves(experimentId: string): Promise<CurveSeries> {
  return getJson(`/api/curves/${encodeURIComponent(experimentId)}`, CurveSeriesSchema);
}

export function getServing(): Promise<ServingResponse> {
  return getJson("/api/serving", ServingResponseSchema);
}

// `GET /api/serving/timing` (WP-3, `plans/bakeoff-timing-records-and-estimates.md`) -- a
// separate DB-backed aggregate, fetched by `Serving.svelte` ALONGSIDE `getServing()` above
// and joined client-side by alias (`lib/serving/matchTiming.ts`). `getServing()` itself
// stays untouched -- this is an additive fetch, not a replacement.
export function getServingTiming(): Promise<ServingTimingRow[]> {
  return getJson("/api/serving/timing", ServingTimingRowSchema.array());
}

// `GET /api/queue` (WP-A) -- read-only queue/state visibility (WP-C Queue tab). Missing
// `QUEUES_DIR` is a valid `{queues: []}` 200, not an error -- see `packages/shared/src/
// queue.ts`.
export function getQueue(): Promise<QueueListResponse> {
  return getJson("/api/queue", QueueListResponseSchema);
}

// `POST /api/control/*` (WP-4/WP-5, `plans/bakeoff-queue-control-plane.md`) -- each route
// only ever RECORDS a request into the host dispatcher's drop-box; `accepted: true` means
// *recorded*, never *executed* (see `packages/shared/src/control.ts`'s own header comment).
// Response parsing/error-extraction is `parseControlActionResponse`
// (`lib/queue/control.ts`) so it's unit-testable without mocking `fetch`; this wrapper is
// the thin fetch-plumbing seam only, same division as `getJson` above.
async function postControlAction(path: string, body?: unknown): Promise<ControlAcceptedResponse> {
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

export function postControlArm(): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/control/arm");
}

export function postControlDisarm(): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/control/disarm");
}

export function postControlStart(body: ControlStartRequest): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/control/start", body);
}

export function postControlStop(): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/control/stop");
}

export function postControlCancel(body: ControlCancelRequest): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/control/cancel", body);
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

export function postServingVisibility(body: ServingVisibilityRequest): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/serving/visibility", body);
}

// `POST /api/catalog/{add,remove,consumers}` (WP-4, `plans/serving-control-redesign.md`) --
// the model-catalog intents (`serving-request-v2`). Same posture as the visibility POST: the
// API writes a drop-box file and returns the request id; the dispatcher validates the
// candidate catalog, applies it, and reports the outcome in the status file's `recent[]`.
export function postCatalogAdd(body: ServingCatalogAddRequest): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/catalog/add", body);
}

export function postCatalogRemove(body: ServingCatalogRemoveRequest): Promise<ControlAcceptedResponse> {
  return postControlAction("/api/catalog/remove", body);
}

export function postCatalogConsumers(body: ServingCatalogConsumersRequest): Promise<ControlAcceptedResponse> {
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
