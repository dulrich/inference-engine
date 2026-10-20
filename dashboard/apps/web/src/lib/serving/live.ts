// Live residency panel logic (plans/serving-control-redesign.md WP-6) over
// `GET /api/serving/live`: the host status collector's feed (llama-swap `/running` + ONNX
// `/health`, both passive). Pure functions; Serving.svelte stays markup-thin.

import type {
  ServingLiveEvent,
  ServingLiveModel,
  ServingLiveTierStatus,
  ServingLiveUnavailableReason,
} from "@bench-dashboard/shared";
import type { StatusPill } from "../router/status.ts";

export const TIER_LABELS = { gpu: "GPU (llama-swap)", onnx: "CPU (ONNX)" } as const;

/** The top-level `available: false` reasons in words; each names what fixes it. */
export function liveUnavailableMessage(reason: ServingLiveUnavailableReason): string {
  switch (reason) {
    case "disabled":
      return "not configured (SERVING_FEED_DIR unset)";
    case "unreadable":
      return "feed directory unreadable";
    case "empty":
      return "no collector snapshots yet — is the status-collector unit installed?";
    default:
      return "the collector has not written for over a minute — is the status-collector unit running?";
  }
}

/** llama-swap's model state is an OPEN string; ONNX residency has no state (resident = loaded).
 * CC-04: the label is always rendered, hue is never the sole carrier. */
export function residentPill(model: ServingLiveModel): StatusPill {
  const state = model.state;
  if (state === null || state === "ready") return { cls: "pill-ok", label: state ?? "loaded" };
  if (state === "starting" || state === "stopping") return { cls: "pill-warn", label: state };
  return { cls: "pill-info", label: state };
}

/** The one-line residency policy for a model: GPU ttl, or ONNX preload / idle unload. */
export function residencyDetail(model: ServingLiveModel): string {
  if (model.ttl !== null) return model.ttl > 0 ? `unloads after ${model.ttl} s idle (ttl)` : "no ttl";
  if (model.preload === true) return "preloaded, always resident";
  if (model.idle_unload_seconds !== null) return `lazy, unloads after ${model.idle_unload_seconds} s idle`;
  return "—";
}

export interface TierView {
  available: boolean;
  sampledAt: string | null;
  models: ServingLiveModel[];
  note: string;
}

/** One tier's panel body. An unavailable tier keeps its reason, never a blank. */
export function tierView(tier: ServingLiveTierStatus): TierView {
  if (!tier.available) {
    return { available: false, sampledAt: tier.sampled_at, models: [], note: `unavailable — ${tier.reason}` };
  }
  return {
    available: true,
    sampledAt: tier.sampled_at,
    models: tier.models,
    note: tier.models.length === 0 ? "nothing resident" : "",
  };
}

export function eventPill(event: ServingLiveEvent): StatusPill {
  return event.kind === "load" ? { cls: "pill-info", label: "load" } : { cls: "pill-muted", label: "unload" };
}

/** The newest `limit` events (the API already returns newest first). */
export function recentEvents(events: ServingLiveEvent[], limit = 10): ServingLiveEvent[] {
  return events.slice(0, limit);
}
