import { z } from "zod";

// `GET /health` — probed BY BODY, never by status code (root AGENTS.md Quirks: "a healthy
// service has told you nothing"; "Probe by response body, never status code").
export const HealthResponseSchema = z.object({
  status: z.literal("ok"),
  experimentCount: z.number().int().nonnegative(),
});
export type HealthResponse = z.infer<typeof HealthResponseSchema>;
