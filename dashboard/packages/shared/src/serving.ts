import { z } from "zod";

// `GET /api/serving` — the served-alias list, read server-side from `llama/config.yaml`
// (path via `LLAMA_CONFIG_PATH`, cycle-1 blocking finding B1: the browser gets no
// executable path to the host file, only this parsed response). Never a write path.

export const ServingFlagsSchema = z.object({
  contextLength: z.number().int().nullable(),
  kvCacheTypeK: z.string().nullable(),
  kvCacheTypeV: z.string().nullable(),
  flashAttention: z.string().nullable(),
  gpuLayers: z.number().int().nullable(),
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
