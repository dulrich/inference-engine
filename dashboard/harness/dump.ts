// Clock normalization of responses (plan WP-1 invariants). The DB dump left with the research
// half (eval-harness `inference_research`).
export const CLOCK = "<clock>";
export const ISO_8601 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/;

/**
 * Response keys carrying the process clock: `generatedAt` on router telemetry (router/telemetryFeed.ts:159) and sidecar
 * (router/sidecarFeed.ts:112) and `generated_at` on GET /api/serving/live (serving_live.rs, the
 * reader's clock) — compared by ISO-8601 shape; `ageMs` (no route emits it since the slots feed
 * retired, kept for safety) — compared as a number. `statusGeneratedAt` and every other `*_at`
 * echoed from a fixture file is data and stays exact.
 */
/** The GET /api/serving/live payload (its `generated_at` is the reader clock, not fixture data). */
const isLiveReply = (value: object) => "gpu" in value && "onnx" in value && "recent" in value;

export function normalizeResponse(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalizeResponse);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, v] of Object.entries(value)) {
      if (key === "generatedAt" && typeof v === "string") out[key] = ISO_8601.test(v) ? CLOCK : `not-iso:${v}`;
      else if (key === "generated_at" && typeof v === "string" && isLiveReply(value)) out[key] = ISO_8601.test(v) ? CLOCK : `not-iso:${v}`;
      else if (key === "ageMs" && typeof v === "number") out[key] = CLOCK;
      else out[key] = normalizeResponse(v);
    }
    return out;
  }
  return value;
}
