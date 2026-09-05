import { z } from "zod";

// `GET /api/export/scoreboard-capture` — raw record pass-through, now the FROZEN v1 capture
// contract consumed by `model_scoreboard`'s `local-capture` source adapter
// (`apps/api/src/sources/local-capture/client.ts` in that repo). `captureVersion: 1` and
// `stability: "v1"` are both on the wire, not only in this doc comment, so the consumer's own
// zod read can assert the version it expects rather than trusting a doc claim. The envelope
// is the only versioned surface here; record-shape evolution of the verbatim fields
// (`experimentId`/`config`/`prompts`/`completions`/`samples`) is governed by the bakeoff
// record's own `schema_version` (currently 2), never reshaped by this schema. Any future
// envelope field removal/rename, or a verbatim-field change that breaks the consumer's zod
// read, bumps `captureVersion` together with the consumer's literal.
//
// `serving` (WP-3, `plans/served-recipe-p50-prefill-and-decode.md`) is the one exception:
// an additive, dashboard-derived sibling key computed at request time by the served-recipe
// resolver (`apps/api/src/serving/recipe.ts resolveServedModel`) — never part of the
// verbatim bakeoff record, never a source of a `captureVersion` bump. `.nullable().default(
// null)` is the same widening `ServingAliasSchema.capability`/`.recipe` use (`serving.ts`)
// so a payload predating this field, or a record with no served-model match, still parses
// as `serving: null`.

export const CaptureServingSchema = z.object({
  modelKey: z.string(),
  aliases: z.array(z.string()),
  recipeMatch: z.boolean(),
  mtp: z.boolean(),
  moeCpuLayers: z.number().int().nullable(),
  specType: z.string().nullable(),
});
export type CaptureServing = z.infer<typeof CaptureServingSchema>;

export const CaptureRecordSchema = z.object({
  experimentId: z.string(),
  config: z.unknown(),
  prompts: z.array(z.unknown()),
  completions: z.array(z.unknown()),
  samples: z.array(z.unknown()),
  serving: CaptureServingSchema.nullable().default(null),
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
