import { z } from "zod";

// `GET /api/serving` — the served-alias list, read server-side from `llama/config.yaml`
// (path via `LLAMA_CONFIG_PATH`, cycle-1 blocking finding B1: the browser gets no
// executable path to the host file, only this parsed response). Never a write path.

// One ordered `{flag, value}` pair pulled from the macro-expanded `cmd` (WP-1, `plans/
// bench-dashboard-polish.md`) -- `value` is `null` for a boolean-style flag (nothing
// follows it, or the following token is itself a flag by the same test).
export const ServingFlagEntrySchema = z.object({
  flag: z.string(),
  value: z.string().nullable(),
});
export type ServingFlagEntry = z.infer<typeof ServingFlagEntrySchema>;

export const ServingFlagsSchema = z.object({
  contextLength: z.number().int().nullable(),
  kvCacheTypeK: z.string().nullable(),
  kvCacheTypeV: z.string().nullable(),
  flashAttention: z.string().nullable(),
  gpuLayers: z.number().int().nullable(),
  // Additive (WP-1): the full ordered flag list, alongside the untouched typed five-flag
  // subset above. `.default([])` only widens what `.parse()` accepts on the input side (a
  // pre-WP-1 fixture/payload missing the field still round-trips); every real producer in
  // this repo populates it, so the inferred output type keeps it non-optional.
  allFlags: z.array(ServingFlagEntrySchema).default([]),
});
export type ServingFlags = z.infer<typeof ServingFlagsSchema>;

// Capability metadata (WP-2, `plans/capability-surfacing-and-local-agent-endpoints.md`;
// normative contract: `docs/model-capabilities.md`) -- the per-model `metadata:` block in
// `llama/config.yaml`, parsed by the SAME server-side reader as `flags` above (read-only
// file parse, never a live call to the router -- see `llamaConfig.ts`'s header comment for
// the data-path decision and the network-reachability finding behind it). `capability` is
// additive alongside `flags`, never a replacement for it (plan's "augment, never replace"
// pin) -- the pre-existing full-flag pipeline this schema already carried is untouched
// above.
//
// Every leaf is nullable, matching `ServingFlagsSchema`'s own posture just above: a value
// resolves to `null` when it can't be determined, never a guessed default. In practice
// every one of the 12 real served models carries a complete block (WP-1's exit bar), but a
// model with no `metadata:` key at all (a config predating WP-1, or a fixture that doesn't
// need one) still round-trips as `capability: null` on `ServingAlias` below, same "absence
// is a valid state" posture `matchServingToMeasured`'s "no measurement yet" already uses.
//
// `reasoning` and the modality lists are bare strings, not a `z.enum` -- the contract doc
// pins both vocabularies "append-only": a future value this not-yet-updated dashboard
// doesn't recognize must still round-trip and render as its own label (CC-04: the label
// text IS the information), never fail closed on an unknown-but-valid wire value.
export const ServingCapabilitySchema = z.object({
  contextLength: z.number().int().nullable(),
  inputModalities: z.array(z.string()).default([]),
  outputModalities: z.array(z.string()).default([]),
  toolCalling: z.boolean().nullable(),
  reasoning: z.string().nullable(),
  serving: z.object({
    quant: z.string().nullable(),
    kvCacheType: z.string().nullable(),
    flashAttention: z.boolean().nullable(),
    mtp: z.boolean().nullable(),
    // Optional serving keys (contract doc: "absent means macro default, never unknown").
    moeCpuOffloadLayers: z.number().int().nullable().default(null),
    ubatch: z.number().int().nullable().default(null),
  }),
});
export type ServingCapability = z.infer<typeof ServingCapabilitySchema>;

export const ServingAliasSchema = z.object({
  modelKey: z.string(),
  name: z.string().nullable(),
  aliases: z.array(z.string()),
  ttl: z.number().int().nullable(),
  checkEndpoint: z.string().nullable(),
  flags: ServingFlagsSchema,
  // `.default(null)` widens what `.parse()` accepts the same way `allFlags` above does --
  // a payload predating this field still round-trips as `capability: null`.
  capability: ServingCapabilitySchema.nullable().default(null),
});
export type ServingAlias = z.infer<typeof ServingAliasSchema>;

export const ServingResponseSchema = z.object({
  configPath: z.string(),
  models: z.array(ServingAliasSchema),
});
export type ServingResponse = z.infer<typeof ServingResponseSchema>;

// `GET /api/serving/timing` (WP-3, `plans/bakeoff-timing-records-and-estimates.md`) -- a
// SEPARATE DB-backed aggregate, never folded into `ServingResponseSchema`/`GET /api/serving`
// above (Decisions locked: that route stays a pure `llama/config.yaml` file-parse, unchanged
// by this addition). One row per alias carrying at least one `timing_round` metric with a
// numeric `observed_decode_tok_s` (`db/experiments.ts listServingTimingRows`); an alias with
// zero such samples (the `bge-*` embed/rerank aliases are the known case today) simply does
// not appear here -- the web client's join treats absence as the literal "—", never a
// guessed zero. p50 + sample count only, deliberately no p90 (n≈10/record per experiment
// makes a p90 dishonest at this depth -- plan Decisions locked). `p50DecodeTokS` stays
// nullable on the wire type for the same "null, not guessed" posture every other measured
// field in this schema already carries, even though the route's own selection query (only
// rows with at least one sample) means it is never actually null in practice.
export const ServingTimingRowSchema = z.object({
  alias: z.string(),
  p50DecodeTokS: z.number().nullable(),
  sampleCount: z.number().int(),
});
export type ServingTimingRow = z.infer<typeof ServingTimingRowSchema>;
