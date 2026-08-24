// Queue-tab control-panel helpers (WP-5, `plans/bakeoff-queue-control-plane.md`) --
// label/pillClass mapping for the two closed control-plane enums
// (`ControlRequestState`, `ControlLastRunDisposition`, `packages/shared/src/control.ts`),
// the `armed` boolean's own word+pill pair, start-form validation, and POST-response
// handling -- all as plain, DOM-free functions so `Queue.svelte` never buries testable
// logic where the suite can't reach it (mirrors `lib/queue/status.ts` and
// `lib/runs/verdict.ts`: a `Record<string, string>` lookup plus a `pill-muted` fallback
// for anything the taxonomy hasn't seen, CC-04 -- the pill's own label text carries the
// state, color is never the sole signal).

import { ControlAcceptedResponseSchema, type ControlAcceptedResponse } from "@bench-dashboard/shared";

// ---- armed (control.armed) -------------------------------------------------------------
// `armed` gates the nightly window (plan: fail-closed default `disarmed` until an operator
// arms it). Neither state is an error; `armed` reads as the deliberately-configured
// "ready to run" state (`pill-ok`), `disarmed` as the quiet default (`pill-muted`) --
// same polarity as `vramProfilePillClass`'s `headless` -> `pill-ok` "expected steady state"
// treatment, not a warning about either state.

export function armedLabel(armed: boolean): string {
  return armed ? "armed" : "disarmed";
}

export function armedPillClass(armed: boolean): string {
  return armed ? "pill-ok" : "pill-muted";
}

// ---- control.requests[].state (ControlRequestState, cycle-1 E1) -----------------------
// `pending`/`claimed`/`running` are the in-flight states (not-yet-terminal, `pill-muted`/
// `pill-info` the same way `cellStatePillClass` treats `pending`/`running`); `done` is the
// one success terminal (`pill-ok`); `rejected`/`launch-failed` are hard-failure terminals
// (`pill-error`); `canceled` is a deliberate, non-error terminal (`pill-warn`, the same
// "in-between, not a hard pass/fail" treatment `queueStatePillClass` gives
// `paused-resumable`). Params are loosely typed `string | null` (not the closed
// `ControlRequestState` union) so a future/unrecognized wire value still renders instead
// of being typechecked away -- the fallback path this file's own tests exercise.

const CONTROL_REQUEST_STATE_PILL: Record<string, string> = {
  pending: "pill-muted",
  claimed: "pill-info",
  running: "pill-info",
  done: "pill-ok",
  rejected: "pill-error",
  canceled: "pill-warn",
  "launch-failed": "pill-error",
};

export function controlRequestStatePillClass(state: string | null): string {
  if (state === null) return "pill-muted";
  return CONTROL_REQUEST_STATE_PILL[state] ?? "pill-muted";
}

export function controlRequestStateLabel(state: string | null): string {
  return state ?? "unknown";
}

// ---- control.lastRun.disposition (ControlLastRunDisposition, cycle-2 E2) --------------

const LAST_RUN_DISPOSITION_PILL: Record<string, string> = {
  clean: "pill-ok",
  "needs-attention": "pill-warn",
  crashed: "pill-error",
};

export function lastRunDispositionPillClass(disposition: string | null): string {
  if (disposition === null) return "pill-muted";
  return LAST_RUN_DISPOSITION_PILL[disposition] ?? "pill-muted";
}

export function lastRunDispositionLabel(disposition: string | null): string {
  return disposition ?? "unknown";
}

// ---- Start-form validation (UX guardrail only -- `ControlStartRequestSchema` and the
// host dispatcher both re-validate fail-closed; this exists so the form can reject an
// obviously-bad submission before round-tripping to the API) ----------------------------

export const START_MINUTES_MIN = 15;
export const START_MINUTES_MAX = 480;

export interface StartFormInput {
  queueStem: string;
  minutes: number;
}

export interface StartFormValidation {
  valid: boolean;
  error: string | null;
}

export function validateStartForm(input: StartFormInput): StartFormValidation {
  if (input.queueStem.trim() === "") {
    return { valid: false, error: "Pick a queue to start." };
  }
  if (!Number.isInteger(input.minutes) || input.minutes < START_MINUTES_MIN || input.minutes > START_MINUTES_MAX) {
    return {
      valid: false,
      error: `Minutes must be a whole number between ${START_MINUTES_MIN} and ${START_MINUTES_MAX}.`,
    };
  }
  return { valid: true, error: null };
}

// ---- POST /api/control/* response handling ---------------------------------------------
// Shared by every `api.ts` control POST helper: parses `ControlAcceptedResponseSchema` on
// a 2xx status, else extracts the route's `{error}` body (mirrors `api.ts`'s own `getJson`
// error-message extraction) -- kept here, not inline in `api.ts`, so it's testable without
// mocking `fetch`.

export interface ControlActionOutcome {
  ok: boolean;
  accepted: ControlAcceptedResponse | null;
  error: string | null;
}

export function parseControlActionResponse(status: number, body: unknown): ControlActionOutcome {
  if (status >= 200 && status < 300) {
    const parsed = ControlAcceptedResponseSchema.safeParse(body);
    if (parsed.success) {
      return { ok: true, accepted: parsed.data, error: null };
    }
    return { ok: false, accepted: null, error: "Malformed response from the control API." };
  }
  const message =
    body && typeof body === "object" && "error" in body ? String((body as { error: unknown }).error) : null;
  return { ok: false, accepted: null, error: message ?? `Control action failed: HTTP ${status}` };
}
