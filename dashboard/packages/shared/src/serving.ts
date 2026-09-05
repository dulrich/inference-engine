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

// Served-recipe projection (WP-1, `plans/served-recipe-p50-prefill-and-decode.md`) --
// artifact + MTP + expert placement + KV types, read straight off the already-expanded
// `cmd` (`llamaConfig.ts`'s `extractRecipe`), never a live probe. `-ctk`/`-ctv` absent
// resolve to `"f16"` (the macro/llama.cpp default this fleet always runs with when the
// flag itself is omitted) -- never `null` -- matching `recipeFromServedAlias`'s own
// posture in `apps/api/src/serving/recipe.ts`, the record-side counterpart that compares
// against this same tuple. `draftPath`/`specType`/`moeCpuLayers` are `null` when the
// corresponding flag (`-md`/`--spec-type`/`-ncmoe`) is absent -- "MTP" (the product-surface
// label, `docs/model-capabilities.md:27`) is `specType !== null || draftPath !== null`,
// computed wherever needed rather than duplicated as a redundant boolean on this type.
export const ServingRecipeSchema = z.object({
  modelPath: z.string().nullable(),
  draftPath: z.string().nullable(),
  specType: z.string().nullable(),
  moeCpuLayers: z.number().int().nullable(),
  kvK: z.string(),
  kvV: z.string(),
});
export type ServingRecipe = z.infer<typeof ServingRecipeSchema>;

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
  // Additive (WP-1), same `.default(null)`-style widening as `capability` above -- a
  // payload predating this field still round-trips as `recipe: null`. Every real producer
  // (`parseServedAliases`) populates it for every served model going forward.
  recipe: ServingRecipeSchema.nullable().default(null),
});
export type ServingAlias = z.infer<typeof ServingAliasSchema>;

export const ServingResponseSchema = z.object({
  configPath: z.string(),
  models: z.array(ServingAliasSchema),
});
export type ServingResponse = z.infer<typeof ServingResponseSchema>;

// `GET /api/serving/timing` (WP-1, `plans/served-recipe-p50-prefill-and-decode.md` --
// revises WP-3's `plans/bakeoff-timing-records-and-estimates.md` original shape) -- a
// SEPARATE DB-backed aggregate, never folded into `ServingResponseSchema`/`GET /api/serving`
// above (Decisions locked: that route stays a pure `llama/config.yaml` file-parse, unchanged
// by this addition). One row per SERVED MODEL (`modelKey`, not per raw `sweep_alias` --
// WP-1 rekeyed the join onto the served-recipe resolver, `apps/api/src/serving/recipe.ts`)
// carrying at least one recipe-matched `timing_round` round; a served model with zero such
// rounds (the `bge-*` embed/rerank aliases are the known case today, and any served model
// whose only measured cells are off-recipe) simply does not appear here -- the web client's
// join treats absence as the literal "—", never a guessed zero.
//
// p50 **and p90** for both prefill (`server_prompt_eval_tok_s`, extracted from a
// `timing_round`'s `raw_json` -- reversing WP-3's original "no p90" call, recorded here so
// it is never read as a regression) and decode (`observed_decode_tok_s`) -- four numbers,
// all independently nullable ("null, not guessed" -- prefill samples may be fewer than
// decode samples within the same admissible round set, per the plan's Decisions locked).
// `samplingArm`/`sampleCount` travel with every number (arm-aware, never pooled -- the
// winning arm, by preference production -> capability -> unlabelled-only, supplies both
// metrics together). `mtp` is the RECIPE's mtp (so a reader knows whether the number is an
// MTP number), which agrees by construction with `ServingCapability.serving.mtp` under the
// recipe-match admission rule. `p50DecodeTokS`/`sampleCount` keep their pre-WP-1 name and
// semantics; `alias` (singular) is replaced by `modelKey` + `aliases` (plural) -- the row is
// now keyed to the served model the config actually names, not to whichever raw
// `sweep_alias` string a record happened to carry.
export const ServingTimingRowSchema = z.object({
  modelKey: z.string(),
  aliases: z.array(z.string()),
  p50PrefillTokS: z.number().nullable(),
  p90PrefillTokS: z.number().nullable(),
  p50DecodeTokS: z.number().nullable(),
  p90DecodeTokS: z.number().nullable(),
  samplingArm: z.string().nullable(),
  sampleCount: z.number().int(),
  mtp: z.boolean(),
});
export type ServingTimingRow = z.infer<typeof ServingTimingRowSchema>;
