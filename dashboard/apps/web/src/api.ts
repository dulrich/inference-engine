import { ExperimentSummarySchema, HealthResponseSchema, type ExperimentSummary, type HealthResponse } from "@bench-dashboard/shared";

// The one fetch seam this WP-1 stub needs: health + experiment count, proving API wiring
// (brief's File scope pin for `apps/web`). WP-2 builds the real views on top of the
// remaining routes (`/api/experiments/:id`, `/api/ladders/*`, `/api/curves/:id`,
// `/api/serving`) this WP already ships.

async function getJson<T>(path: string, schema: { parse: (value: unknown) => T }): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return schema.parse(await response.json());
}

export function getHealth(): Promise<HealthResponse> {
  return getJson("/health", HealthResponseSchema);
}

export function getExperiments(): Promise<ExperimentSummary[]> {
  return getJson("/api/experiments", ExperimentSummarySchema.array());
}
