import { z } from "zod";

// `GET /api/export/scoreboard-capture` — raw record pass-through, now the FROZEN v1 capture
// contract consumed by `model_scoreboard`'s `local-capture` source adapter
// (`apps/api/src/sources/local-capture/client.ts` in that repo). `captureVersion: 1` and
// `stability: "v1"` are both on the wire, not only in this doc comment, so the consumer's own
// zod read can assert the version it expects rather than trusting a doc claim. `records[]`
// stays a raw pass-through of the bakeoff DB record rows (`config`/`prompts`/`completions`/
// `samples` are never reshaped here) — the envelope is the only versioned surface; record-shape
// evolution is governed by the bakeoff record's own `schema_version` (currently 2). Any future
// envelope field removal/rename, or a record-shape change that breaks the consumer's zod read,
// bumps `captureVersion` together with the consumer's literal.

export const CaptureRecordSchema = z.object({
  experimentId: z.string(),
  config: z.unknown(),
  prompts: z.array(z.unknown()),
  completions: z.array(z.unknown()),
  samples: z.array(z.unknown()),
});
export type CaptureRecord = z.infer<typeof CaptureRecordSchema>;

export const CaptureEnvelopeSchema = z.object({
  captureVersion: z.literal(1),
  stability: z.literal("v1"),
  generatedAt: z.string(),
  recordCount: z.number().int().nonnegative(),
  records: z.array(CaptureRecordSchema),
});
export type CaptureEnvelope = z.infer<typeof CaptureEnvelopeSchema>;
