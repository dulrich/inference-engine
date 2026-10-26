// POST-response handling for the serving drop-box writes (`POST /api/serving/visibility`,
// `POST /api/catalog/*`): parses `ServingAcceptedResponseSchema` on a 2xx status, else the
// route's `{error}` body via the shared `errorMessage` extractor `api.ts`'s `getJson` uses too.
// Kept out of `api.ts` so it is testable without mocking `fetch`.

import { ServingAcceptedResponseSchema, type ServingAcceptedResponse } from "@bench-dashboard/shared";

export type ControlActionOutcome = { ok: true; accepted: ServingAcceptedResponse } | { ok: false; error: string };

/** A route's `{error: string}` body as a message, or `null` when the body carries none. */
export function errorMessage(body: unknown): string | null {
  return body && typeof body === "object" && "error" in body ? String((body as { error: unknown }).error) : null;
}

export function parseControlActionResponse(status: number, body: unknown): ControlActionOutcome {
  if (status >= 200 && status < 300) {
    const parsed = ServingAcceptedResponseSchema.safeParse(body);
    if (parsed.success) {
      return { ok: true, accepted: parsed.data };
    }
    return { ok: false, error: "Malformed response from the control API." };
  }
  return { ok: false, error: errorMessage(body) ?? `Control action failed: HTTP ${status}` };
}
