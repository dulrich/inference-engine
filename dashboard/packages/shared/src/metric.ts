import { z } from "zod";

// One row per measured metric: either a VRAM snapshot pulled from `config.json`'s `vram`
// object (`metricType` `"vram_baseline"` / `"vram_loaded"` / `"vram_after_teardown"`), or a
// `timing_round` sample from `samples.jsonl` (`metricType` `"timing_round"`, `roundType`
// `"cold"`/`"warm"`/`"hot"`). Denormalized on purpose (Decisions locked — the DB is derived
// and query convenience wins over normal form here); `raw` carries the source object.

export const MetricRowSchema = z.object({
  id: z.number().int().positive(),
  experimentId: z.string(),
  metricType: z.string(),
  roundType: z.string().nullable(),
  roundIndex: z.number().int().nullable(),
  bytes: z.number().int().nullable(),
  firstGenerationTokenMs: z.number().nullable(),
  observedDecodeTokS: z.number().nullable(),
  promptTokens: z.number().int().nullable(),
  cachedTokens: z.number().int().nullable(),
  cacheHitRatio: z.number().nullable(),
  cacheAssertionPassed: z.boolean().nullable(),
  raw: z.unknown(),
});
export type MetricRow = z.infer<typeof MetricRowSchema>;
