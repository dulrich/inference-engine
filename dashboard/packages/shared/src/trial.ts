import { z } from "zod";

// One row per `samples.jsonl` line — every v2 sample `type` lands here (`timing_round`,
// `needle_probe`, `quality_corpus_result`, `longcontext_probe`, and any type this schema
// hasn't seen yet: `type` is data, not an enum ingest validates against, per the brief's
// input-contract pin — a future `longcontext_probe`-adjacent type must not require a
// dashboard change). Every field below is optional/nullable because no single sample
// `type` populates all of them; `raw` always carries the full original JSON line so
// nothing is lost to this schema's own gaps.

export const TrialRowSchema = z.object({
  id: z.number().int().positive(),
  experimentId: z.string(),
  lineIndex: z.number().int().nonnegative(),
  type: z.string(),
  trial: z.number().int().nonnegative().nullable(),
  samplingArm: z.string().nullable(),
  temperature: z.number().nullable(),
  topP: z.number().nullable(),
  topK: z.number().int().nullable(),
  samplerSeed: z.number().nullable(),
  haystackSeed: z.number().nullable(),
  corpusManifestId: z.string().nullable(),
  haystackSha256: z.string().nullable(),
  classifierVerdict: z.string().nullable(),
  classifierShape: z.string().nullable(),
  promptTokens: z.number().int().nullable(),
  completionTokens: z.number().int().nullable(),
  finishReason: z.string().nullable(),
  positionFraction: z.number().nullable(),
  task: z.string().nullable(),
  cellId: z.string().nullable(),
  correct: z.boolean().nullable(),
  raw: z.unknown(),
});
export type TrialRow = z.infer<typeof TrialRowSchema>;
