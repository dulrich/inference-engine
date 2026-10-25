// POST-response handling for the serving drop-box writes (`POST /api/serving/visibility`,
// `POST /api/catalog/*`): parses `ServingAcceptedResponseSchema` on a 2xx status, else
// extracts the route's `{error}` body (mirrors `api.ts`'s own `getJson` error-message
// extraction) -- kept here, not inline in `api.ts`, so it's testable without mocking `fetch`.
// Moved from the retired `lib/queue/control.ts` when the research half left (WP-4).

import { ServingAcceptedResponseSchema, type ServingAcceptedResponse } from "@bench-dashboard/shared";

export interface ControlActionOutcome {
  ok: boolean;
  accepted: ServingAcceptedResponse | null;
  error: string | null;
}

export function parseControlActionResponse(status: number, body: unknown): ControlActionOutcome {
  if (status >= 200 && status < 300) {
    const parsed = ServingAcceptedResponseSchema.safeParse(body);
    if (parsed.success) {
      return { ok: true, accepted: parsed.data, error: null };
    }
    return { ok: false, accepted: null, error: "Malformed response from the control API." };
  }
  const message =
    body && typeof body === "object" && "error" in body ? String((body as { error: unknown }).error) : null;
  return { ok: false, accepted: null, error: message ?? `Control action failed: HTTP ${status}` };
}
