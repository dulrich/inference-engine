import { z } from "zod";

// `GET /api/ladders/:hfRepo` — one row per quant/KV/context cell measured for a model
// (the bake-off doc's summary table, live). Computed server-side by joining
// `experiments` (sweep-cell identity) against `metrics` (timing medians) and `trials`
// (correctness rate) for every experiment sharing an `hfRepo`. WP-2 consumes this
// directly for the Ladders view; this WP only guarantees the route returns it in shape.

export const LadderRowSchema = z.object({
  experimentId: z.string(),
  alias: z.string().nullable(),
  cellId: z.string().nullable(),
  filename: z.string().nullable(),
  contextLength: z.number().int().nullable(),
  kvCacheTypeK: z.string().nullable(),
  kvCacheTypeV: z.string().nullable(),
  flashAttention: z.string().nullable(),
  medianDecodeTokS: z.number().nullable(),
  medianFirstGenerationTokenMs: z.number().nullable(),
  correctnessRate: z.number().nullable(),
  trialCount: z.number().int().nonnegative(),
});
export type LadderRow = z.infer<typeof LadderRowSchema>;
