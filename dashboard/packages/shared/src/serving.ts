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

export const ServingAliasSchema = z.object({
  modelKey: z.string(),
  name: z.string().nullable(),
  aliases: z.array(z.string()),
  ttl: z.number().int().nullable(),
  checkEndpoint: z.string().nullable(),
  flags: ServingFlagsSchema,
});
export type ServingAlias = z.infer<typeof ServingAliasSchema>;

export const ServingResponseSchema = z.object({
  configPath: z.string(),
  models: z.array(ServingAliasSchema),
});
export type ServingResponse = z.infer<typeof ServingResponseSchema>;
