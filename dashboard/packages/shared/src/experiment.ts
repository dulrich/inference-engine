import { z } from "zod";

// Wire schemas for the `experiments` table (API <-> SPA). Field names mirror the frozen
// v2 record contract (`llama/bakeoff/README.md` "Schema v2 (FROZEN 2026-08-23)") one for
// one — see the ingest module (`apps/api/src/ingest/record.ts`) for the config.json ->
// row mapping this schema validates on the way out.

/** The six lineage keys `config.lineage` frozen v2 requires (a value may be `null` when
 * genuinely unresolvable, but the key itself is always present on a v2 record). */
export const LineageSchema = z.object({
  hfRepo: z.string().nullable(),
  hfRevision: z.string().nullable(),
  filename: z.string().nullable(),
  fileSize: z.number().int().nonnegative().nullable(),
  sha256: z.string().nullable(),
  llamaCppBuild: z.string().nullable(),
});
export type Lineage = z.infer<typeof LineageSchema>;

/** `config.sweep_cell` — the sweep-cell identity a sweep-driven caller supplied; `null`
 * for a non-sweep `full` CLI record (frozen contract). */
export const SweepCellSchema = z
  .object({
    cellId: z.string(),
    alias: z.string(),
    serverFlags: z.record(z.string(), z.unknown()),
    command: z.string().nullable().optional(),
  })
  .nullable();
export type SweepCell = z.infer<typeof SweepCellSchema>;

/** `config.wilson_summary` — always present as a key on a v2 record, `null` whenever the
 * record has no replicated correctness cells (n>=10) to summarize (deferred audit pin,
 * "wilson_summary explicit-null"). Shape follows `longcontext.wilson_interval`'s own
 * return contract: `sided`/`confidence` are always present when the summary itself is
 * non-null (never guessed from context downstream). */
export const WilsonSummarySchema = z
  .object({
    n: z.number().int().nonnegative(),
    k: z.number().int().nonnegative(),
    rate: z.number(),
    sided: z.enum(["one", "two"]),
    confidence: z.number(),
    lower: z.number().nullable(),
    upper: z.number().nullable(),
  })
  .nullable();
export type WilsonSummary = z.infer<typeof WilsonSummarySchema>;

/** `GET /api/experiments` list row — one per `experiments` table row, no trials/metrics
 * joined (denormalized counts only, for a fast browser view). */
export const ExperimentSummarySchema = z.object({
  id: z.string(),
  schemaVersion: z.literal(2),
  description: z.string().nullable(),
  timestamp: z.string().nullable(),
  lineage: LineageSchema,
  sweepCell: SweepCellSchema,
  hasWilsonSummary: z.boolean(),
  trialCount: z.number().int().nonnegative(),
  metricCount: z.number().int().nonnegative(),
});
export type ExperimentSummary = z.infer<typeof ExperimentSummarySchema>;

/** `GET /api/experiments/:id` detail — the summary plus every trial/metric row and the
 * raw wilson summary object (drill-down view). */
export const ExperimentDetailSchema = ExperimentSummarySchema.extend({
  wilsonSummary: WilsonSummarySchema,
});
export type ExperimentDetail = z.infer<typeof ExperimentDetailSchema>;
