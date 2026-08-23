import {
  CurveSeriesSchema,
  ExperimentDetailSchema,
  ExperimentSummarySchema,
  HealthResponseSchema,
  LadderRowSchema,
  MetricRowSchema,
  ServingResponseSchema,
  TrialRowSchema,
  type CurveSeries,
  type ExperimentSummary,
  type HealthResponse,
  type LadderRow,
  type ServingResponse,
} from "@bench-dashboard/shared";
import { z } from "zod";

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
// (`apps/api/src/routes/experiments.ts`) -- a shape `ExperimentDetailSchema` alone doesn't
// cover, so this extends it the same way the route itself builds the response object.
const ExperimentFullDetailSchema = ExperimentDetailSchema.extend({
  trials: TrialRowSchema.array(),
  metrics: MetricRowSchema.array(),
});
export type ExperimentFullDetail = z.infer<typeof ExperimentFullDetailSchema>;

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
