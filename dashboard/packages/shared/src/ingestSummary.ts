import { z } from "zod";

// The `bun run ingest` CLI's own summary object (also printed as its final stdout line) —
// not served over HTTP, shared so a future caller (WP-2's smoke, a future admin route)
// can parse it without re-deriving the shape.
export const IngestSummarySchema = z.object({
  scanned: z.number().int().nonnegative(),
  ingested: z.number().int().nonnegative(),
  skippedV1: z.number().int().nonnegative(),
  skippedInvalid: z.number().int().nonnegative(),
  rebuilt: z.boolean(),
});
export type IngestSummary = z.infer<typeof IngestSummarySchema>;
