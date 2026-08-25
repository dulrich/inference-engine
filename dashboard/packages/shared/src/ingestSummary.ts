import { z } from "zod";

// The `bun run ingest` CLI's own summary object (also printed as its final stdout line) —
// not served over HTTP, shared so a future caller (WP-2's smoke, a future admin route)
// can parse it without re-deriving the shape.
export const IngestSummarySchema = z.object({
  scanned: z.number().int().nonnegative(),
  ingested: z.number().int().nonnegative(),
  skippedV1: z.number().int().nonnegative(),
  skippedInvalid: z.number().int().nonnegative(),
  // WP-4 (`plans/bakeoff-timing-records-and-estimates.md`): pre-v2 dirs whose
  // `samples.jsonl` contained `timing_round` rows, ingested under `'v1-timing'`
  // provenance. Counted separately from `skippedV1` so the summary distinguishes
  // fully-skipped legacy dirs from timing-ingested ones — `skippedV1` keeps its
  // pre-existing meaning (never renamed, per the brief's counter-honesty pin).
  legacyTimingIngested: z.number().int().nonnegative(),
  rebuilt: z.boolean(),
});
export type IngestSummary = z.infer<typeof IngestSummarySchema>;
