// Serving-visibility chip logic (WP-4, `plans/bench-dashboard-serving-control.md`) — every
// derivation behind the Serving tab's two per-row chips (`cc`, `openwebui`), as plain,
// DOM-free functions so `Serving.svelte` stays markup-thin (the `lib/router/*.ts` division).
//
// THE CHIP STATE HAS EXACTLY ONE SOURCE: `GET /api/serving/status`'s `visibility` map, which
// is the host dispatcher's republication of the model catalog's visibility (`router/catalog/catalog.json`). It is never derived
// from the `GET /api/serving` YAML parse (that file carries no visibility at all) and never
// from the catalog file directly (not mounted into this container). When the status
// is unavailable — disabled, unreadable, empty or STALE — the chips render disabled with the
// reason in words rather than guessing a state, because a click over a silently frozen map
// would record a write nobody will execute.
//
// Two further disable rules, both invariants of the declaration rather than UI taste:
//   - `cc` on a row whose `gateway_id` is `null` ("not a gateway model"). `cc => gateway_id
//     non-null` is the declaration's own rule; ten of the seventeen keys are null today, and
//     an enabled chip there would let a click write a declaration the picker gate rejects.
//   - a model key present in `GET /api/serving` but absent from the status map (drift). The
//     two WP-3 gates make that state an alarm, not a normal one.
//
// There is no restart banner and no `restart_required` anywhere on this path: nothing about
// applying visibility requires a container restart.

import type {
  ServingConsumer,
  ServingStatusResponse,
  ServingStatusUnavailableReason,
  ServingVisibilityEntry,
} from "@bench-dashboard/shared";
import { SERVING_CONSUMER_ORDER } from "@bench-dashboard/shared";

/** The two chips, in the canonical order the declaration itself uses. */
export const SERVING_CONSUMERS: readonly ServingConsumer[] = SERVING_CONSUMER_ORDER;

/** Product-surface names. `cc` is the Claude Code model picker; `openwebui` is the Open
 * WebUI row's `is_active`. */
const CONSUMER_LABEL: Record<ServingConsumer, string> = {
  cc: "Claude Code",
  openwebui: "Open WebUI",
};

export function consumerLabel(consumer: ServingConsumer): string {
  return CONSUMER_LABEL[consumer];
}

/** Canonical order + dedupe, the same normalization the dispatcher applies before writing
 * the declaration — done here too so the POSTed array is already the array that will land,
 * and a round-trip never looks like a change. */
export function canonicalConsumers(consumers: readonly ServingConsumer[]): ServingConsumer[] {
  return SERVING_CONSUMER_ORDER.filter((consumer) => consumers.includes(consumer));
}

/** The FULL desired array for one model after toggling one chip — the POST body's
 * `consumers` is never a delta. */
export function nextConsumers(
  current: readonly ServingConsumer[],
  consumer: ServingConsumer,
  on: boolean,
): ServingConsumer[] {
  const kept = current.filter((value) => value !== consumer);
  return canonicalConsumers(on ? [...kept, consumer] : kept);
}

/** The four `available: false` reasons in words. `stale` names the cause an operator can
 * act on: the minutely host dispatcher is what keeps the map fresh. */
export function unavailableReasonText(reason: ServingStatusUnavailableReason): string {
  switch (reason) {
    case "disabled":
      return "serving control is not configured";
    case "unreadable":
      return "the status file is missing or unreadable";
    case "empty":
      return "no status has been published yet";
    case "stale":
      return "the status file is stale — the host dispatcher is not running";
  }
}

/** One model's declared entry, or `null` when the status is unavailable/not yet loaded or
 * the key is absent from the map (drift). */
export function visibilityEntry(
  status: ServingStatusResponse | null,
  modelKey: string,
): ServingVisibilityEntry | null {
  if (status === null || !status.available) return null;
  return status.visibility[modelKey] ?? null;
}

// ---- the optimistic "requested" state -------------------------------------------------
//
// Keyed by `modelKey` (one in-flight request per row) and carrying the `requestId` the POST
// returned; cleared when the status file's `recent[]` shows that id. Every state in
// `recent[]` is terminal — the dispatcher records a disposition and THEN unlinks the request
// file — so an id appearing there is the settle signal regardless of which disposition it
// carries.

export interface PendingVisibilityRequest {
  requestId: string;
  consumers: ServingConsumer[];
}

export type PendingVisibilityMap = Record<string, PendingVisibilityRequest>;

export function isRequestSettled(status: ServingStatusResponse | null, requestId: string): boolean {
  if (status === null || !status.available) return false;
  return status.recent.some((record) => record.request_id === requestId);
}

/** Drop every pending entry the status file has now dispositioned. An unavailable status
 * settles nothing — the pending marks stay until a readable status proves the outcome. */
export function settlePending(
  pending: PendingVisibilityMap,
  status: ServingStatusResponse | null,
): PendingVisibilityMap {
  if (status === null || !status.available) return pending;
  const next: PendingVisibilityMap = {};
  let changed = false;
  for (const [modelKey, request] of Object.entries(pending)) {
    if (isRequestSettled(status, request.requestId)) {
      changed = true;
      continue;
    }
    next[modelKey] = request;
  }
  return changed ? next : pending;
}

/** The disposition the status file recorded for a settled request, for the row's result
 * line: `null` when the id is unknown to `recent[]`. */
export function requestOutcome(
  status: ServingStatusResponse | null,
  requestId: string,
): { state: string; reason: string | null } | null {
  if (status === null || !status.available) return null;
  const record = status.recent.find((entry) => entry.request_id === requestId);
  return record ? { state: record.state, reason: record.reason } : null;
}

// ---- chip state ------------------------------------------------------------------------

export type ServingChipKind = "enabled" | "disabled" | "requested";

export interface ServingChipState {
  consumer: ServingConsumer;
  kind: ServingChipKind;
  /** The visibility this chip currently reflects — the declared value, or the requested one
   * while a request is in flight. */
  on: boolean;
  /** True unless `kind === "enabled"` — a `requested` chip is locked until it settles. */
  disabled: boolean;
  /** Why the chip is disabled, in words; `null` unless `kind === "disabled"`. */
  reason: string | null;
  /** The in-flight request id; `null` unless `kind === "requested"`. */
  requestId: string | null;
  label: string;
  stateLabel: string;
  pillClass: string;
  title: string;
}

/** CC-04: the pill's own text carries the state, hue is never the sole signal. */
function pillClassFor(kind: ServingChipKind, on: boolean): string {
  if (kind === "requested") return "pill-info";
  if (kind === "disabled") return "pill-muted";
  return on ? "pill-ok" : "pill-muted";
}

function chip(state: Omit<ServingChipState, "label" | "pillClass" | "disabled">): ServingChipState {
  return {
    ...state,
    // Only an `enabled` chip is clickable. A `requested` one is locked until `recent[]`
    // settles it: a second click would otherwise build its desired array from the DECLARED
    // consumers (the requested array is not the declaration yet) and silently supersede the
    // in-flight request with a body computed from a stale base.
    disabled: state.kind !== "enabled",
    label: consumerLabel(state.consumer),
    pillClass: pillClassFor(state.kind, state.on),
  };
}

/**
 * One chip's whole render state. Precedence, highest first:
 *   1. no status at all (not loaded, or the fetch failed) — disabled, `statusError` in words;
 *   2. `available: false` — disabled, the reason in words (this is where `stale` lands);
 *   3. the key is absent from the status map — disabled, drift;
 *   4. `cc` on a `gateway_id: null` row — disabled, "not a gateway model";
 *   5. a pending request for this row — `requested`, reflecting the REQUESTED array;
 *   6. otherwise enabled, reflecting the declared array.
 */
export function chipState(
  consumer: ServingConsumer,
  modelKey: string,
  status: ServingStatusResponse | null,
  pending: PendingVisibilityMap = {},
  statusError = "",
): ServingChipState {
  const base = { consumer, on: false, reason: null as string | null, requestId: null as string | null };

  if (status === null) {
    const reason = statusError === "" ? "visibility status is not loaded yet" : statusError;
    return chip({ ...base, kind: "disabled", reason, stateLabel: "unknown", title: reason });
  }
  if (!status.available) {
    const reason = unavailableReasonText(status.reason);
    return chip({ ...base, kind: "disabled", reason, stateLabel: "unknown", title: reason });
  }

  const entry = status.visibility[modelKey];
  if (entry === undefined) {
    const reason = `${modelKey} is served but absent from the visibility declaration (drift)`;
    return chip({ ...base, kind: "disabled", reason, stateLabel: "unknown", title: reason });
  }

  const declaredOn = entry.consumers.includes(consumer);

  if (consumer === "cc" && entry.gateway_id === null) {
    const reason = "not a gateway model";
    return chip({
      ...base,
      kind: "disabled",
      on: declaredOn,
      reason,
      stateLabel: declaredOn ? "on" : "off",
      title: `${reason} — no gateway id is declared for ${modelKey}, so it cannot be offered to Claude Code`,
    });
  }

  const request = pending[modelKey];
  if (request !== undefined) {
    const requestedOn = request.consumers.includes(consumer);
    return chip({
      ...base,
      kind: "requested",
      on: requestedOn,
      requestId: request.requestId,
      stateLabel: "requested",
      title: `Requested ${requestedOn ? "on" : "off"} (${request.requestId}) — the effect lands on the next dispatcher pass`,
    });
  }

  return chip({
    ...base,
    kind: "enabled",
    on: declaredOn,
    stateLabel: declaredOn ? "on" : "off",
    title: declaredOn
      ? `Visible to ${consumerLabel(consumer)} — click to withdraw it`
      : `Not visible to ${consumerLabel(consumer)} — click to publish it`,
  });
}

/** Both chips for one row, in canonical order — the single call `Serving.svelte` makes. */
export function rowChips(
  modelKey: string,
  status: ServingStatusResponse | null,
  pending: PendingVisibilityMap = {},
  statusError = "",
): ServingChipState[] {
  return SERVING_CONSUMERS.map((consumer) => chipState(consumer, modelKey, status, pending, statusError));
}

/** The POST body for toggling one chip: the full desired array for that model, built from
 * the DECLARED array (never from the chip's own rendering), canonically ordered. */
export function visibilityRequestBody(
  modelKey: string,
  entry: ServingVisibilityEntry,
  consumer: ServingConsumer,
  on: boolean,
): { modelKey: string; consumers: ServingConsumer[] } {
  return { modelKey, consumers: nextConsumers(entry.consumers, consumer, on) };
}
