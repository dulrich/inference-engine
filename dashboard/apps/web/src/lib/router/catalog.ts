// Model-catalog panel logic (WP-4, `plans/serving-control-redesign.md`) -- every derivation
// behind the Router view's catalog panel, as plain DOM-free functions (the `lib/router/*.ts`
// division), so `Router.svelte` stays markup-thin.
//
// THE PANEL HAS EXACTLY ONE SOURCE: `GET /api/serving/status`'s additive `catalog` block, the
// host dispatcher's projection of `router/catalog/catalog.json` (the dashboard has no read access
// to the catalog file itself). Every edit is a `serving-request-v2` drop-box intent; the
// dispatcher validates the candidate catalog with the gateway's own `check-catalog`, applies
// it, waits for the gateway to report the new revision, and records the outcome in `recent[]`.
// Nothing here decides an outcome -- the checks below only keep a click from recording a
// request the dispatcher is certain to refuse, and the dispatcher re-checks all of them.
//
// Consumers: the UI toggles only `cc` and `openwebui`. The gateway's third tag, `router`
// (router-internal codex suffix ids), is never shown as a toggle and is preserved by the
// dispatcher on every write -- it marks an id as "reachable but not a picker row", which is
// a gateway routing fact rather than a consumer the operator chooses.

import type {
  ServingCatalogModel,
  ServingCatalogProjection,
  ServingCatalogProvider,
  ServingConsumer,
  ServingStatusResponse,
} from "@bench-dashboard/shared";
import { SERVING_CATALOG_ID_PATTERN, SERVING_CONSUMER_ORDER } from "@bench-dashboard/shared";

export const CATALOG_CONSUMERS: readonly ServingConsumer[] = SERVING_CONSUMER_ORDER;

/** The status file's catalog block, or `null` when the status is unavailable or predates it. */
export function catalogBlock(status: ServingStatusResponse | null): ServingCatalogProjection | null {
  if (status === null || !status.available) return null;
  return status.catalog ?? null;
}

/** The UI consumers an entry carries. An entry with NO `consumers` list is "every consumer"
 * to the picker (`router/model-picker-lineup.mjs`), so it reads as both on. */
export function uiConsumers(raw: readonly string[] | null): ServingConsumer[] {
  if (raw === null) return [...CATALOG_CONSUMERS];
  return CATALOG_CONSUMERS.filter((consumer) => raw.includes(consumer));
}

/** The FULL desired `cc`/`openwebui` array after toggling one chip -- never a delta. */
export function nextCatalogConsumers(
  raw: readonly string[] | null,
  consumer: ServingConsumer,
  on: boolean,
): ServingConsumer[] {
  const kept = uiConsumers(raw).filter((value) => value !== consumer);
  const next = on ? [...kept, consumer] : kept;
  return CATALOG_CONSUMERS.filter((value) => next.includes(value));
}

/** True when the entry carries the router-internal tag (shown as a muted marker). */
export function isRouterInternal(raw: readonly string[] | null): boolean {
  return raw !== null && raw.includes("router");
}

export interface Verdict {
  ok: boolean;
  reason: string | null;
}

const OK: Verdict = { ok: true, reason: null };

/** Whether the remove button is live, and why not. The dispatcher's E2 rule: an id a consumer
 * stores (every codex persona id, every alias in the llama consumer registry) is never removed
 * -- hide it with no consumers instead. The last id of a provider is never removed either (an
 * empty list would be a wildcard). */
export function removeVerdict(provider: ServingCatalogProvider, model: ServingCatalogModel): Verdict {
  if (model.protected) {
    return { ok: false, reason: "stored by a consumer — clear its consumers to hide it instead" };
  }
  if (provider.models.length <= 1) {
    return { ok: false, reason: "the provider's last id — an empty list would be a wildcard" };
  }
  return OK;
}

/** Whether a consumer chip is live for this entry. `cc` needs a routed id, so exposure records
 * (served keys the gateway does not route) never get it. */
export function chipVerdict(consumer: ServingConsumer, routed: boolean): Verdict {
  if (consumer === "cc" && !routed) return { ok: false, reason: "not a gateway model" };
  return OK;
}

/** The providers the add form offers: everything except the codex personas (a fixed table). */
export function addableProviders(block: ServingCatalogProjection): ServingCatalogProvider[] {
  return block.providers.filter((provider) => provider.kind !== "codex");
}

/** Client-side pre-check of the add form, mirroring the dispatcher's refusals. */
export function addVerdict(provider: ServingCatalogProvider | null, id: string, block: ServingCatalogProjection): Verdict {
  const trimmed = id.trim();
  if (provider === null) return { ok: false, reason: "pick a provider" };
  if (trimmed === "") return { ok: false, reason: "enter an id" };
  if (trimmed !== id || !SERVING_CATALOG_ID_PATTERN.test(trimmed)) {
    return { ok: false, reason: "not a model slug or @preset/<slug>" };
  }
  if (provider.kind === "codex") return { ok: false, reason: "codex persona ids are a fixed table" };
  if (trimmed.startsWith("@") && !provider.presets) {
    return { ok: false, reason: "@preset ids are accepted only on the OpenRouter legs" };
  }
  if (provider.kind === "local" && !block.addable_local_aliases.includes(trimmed)) {
    return { ok: false, reason: "not an unrouted llama-swap alias" };
  }
  if (provider.models.some((model) => model.id === trimmed)) {
    return { ok: false, reason: "already in the catalog" };
  }
  return OK;
}

export interface CatalogOutcome {
  requestId: string;
  state: string;
  reason: string | null;
  revision: string | null;
}

/** The newest catalog-intent disposition in `recent[]` (v2 records carry `revision`; v1
 * visibility records never do), or `null`. */
export function latestCatalogOutcome(status: ServingStatusResponse | null): CatalogOutcome | null {
  if (status === null || !status.available) return null;
  const record = status.recent.find((entry) => entry.revision !== undefined);
  if (record === undefined) return null;
  return {
    requestId: record.request_id,
    state: record.state,
    reason: record.reason,
    revision: record.revision ?? null,
  };
}

/** The outcome of one known request id, for the panel's "requested" marks: `null` while the
 * dispatcher has not dispositioned it yet. */
export function outcomeFor(status: ServingStatusResponse | null, requestId: string): CatalogOutcome | null {
  if (status === null || !status.available) return null;
  const record = status.recent.find((entry) => entry.request_id === requestId);
  if (record === undefined) return null;
  return { requestId, state: record.state, reason: record.reason, revision: record.revision ?? null };
}

/** `pill` class for a disposition state, in the Signalworks pill vocabulary. */
export function outcomePillClass(state: string): string {
  switch (state) {
    case "applied":
      return "pill-ok";
    case "rejected":
      return "pill-warn";
    default:
      return "pill-error";
  }
}

/** One stable key per entry (`provider/id`; provider names carry no `/`) for keyed `{#each}` and pending maps. */
export function entryKey(provider: string, id: string): string {
  return `${provider}/${id}`;
}
