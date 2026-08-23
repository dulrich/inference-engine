import { z } from "zod";

// `GET /api/export/scoreboard-capture` — raw record pass-through for the deferred
// `model_scoreboard` connector successor plan. Explicitly NOT a stable public interface of
// this plan (cycle-1 material finding M4): the envelope's `stability` field says so on the
// wire, not only in a doc comment, so a consumer parsing this response sees the same claim
// this plan makes about it.

export const CaptureRecordSchema = z.object({
  experimentId: z.string(),
  config: z.unknown(),
  prompts: z.array(z.unknown()),
  completions: z.array(z.unknown()),
  samples: z.array(z.unknown()),
});
export type CaptureRecord = z.infer<typeof CaptureRecordSchema>;

export const CaptureEnvelopeSchema = z.object({
  stability: z.literal("experimental"),
  generatedAt: z.string(),
  recordCount: z.number().int().nonnegative(),
  records: z.array(CaptureRecordSchema),
});
export type CaptureEnvelope = z.infer<typeof CaptureEnvelopeSchema>;
