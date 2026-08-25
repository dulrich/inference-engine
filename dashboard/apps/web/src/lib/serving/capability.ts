// Serving view's capability-block rendering helpers (WP-2, `plans/
// capability-surfacing-and-local-agent-endpoints.md`) -- plain, DOM-free functions so the
// pill/label mapping is unit-testable, same division `lib/serving/expand.ts` already
// established for the full-flag expand row. `ServingAlias.capability` is additive
// (`@bench-dashboard/shared`'s own header comment) and every leaf is nullable ("resolves to
// null, never a guessed default") -- every helper below renders the null-safe em-dash /
// `pill-muted` fallback the rest of this app already uses for "nothing to show", never a
// guessed value.

import type { ServingCapability } from "@bench-dashboard/shared";
import { formatCount } from "../format.ts";

// ---- Tool calling ----------------------------------------------------------------------
// CC-04: the pill's own label text carries the polarity, never color alone.

export function capabilityToolsLabel(toolCalling: boolean | null): string {
  if (toolCalling === null) return "—";
  return toolCalling ? "tools" : "no tools";
}

export function capabilityToolsPillClass(toolCalling: boolean | null): string {
  return toolCalling === true ? "pill-ok" : "pill-muted";
}

// ---- Reasoning level --------------------------------------------------------------------
// `docs/model-capabilities.md`'s enumeration is append-only -- an unrecognized-but-valid
// future level still renders (its own label IS the information) via the `?? "pill-muted"`
// fallback, same "never hide an unrecognized value" posture `lib/profile.ts`'s
// `vramProfilePillClass` and `lib/runs/verdict.ts`'s `verdictPillClass` already use.

const REASONING_PILL_CLASS: Record<string, string> = {
  none: "pill-muted",
  "request-controllable": "pill-info",
  "always-on": "pill-warn",
};

export function capabilityReasoningLabel(reasoning: string | null): string {
  return reasoning ?? "—";
}

export function capabilityReasoningPillClass(reasoning: string | null): string {
  if (reasoning === null) return "pill-muted";
  return REASONING_PILL_CLASS[reasoning] ?? "pill-muted";
}

// ---- Serving-facts summary ---------------------------------------------------------------
// One compact, order-stable string joining `capability.contextLength` and every
// `capability.serving.*` fact (quant/KV/FA/MTP plus the two optional keys) -- the brief's
// "quant/KV/FA/MTP and optional serving keys" rendering pin, kept as a single tested
// function rather than inline `Serving.svelte` template logic. A boolean fact (FA/MTP) is
// only listed when true (matching `serving.*`'s own "absent means macro default" contract:
// there is nothing informative about naming an off-by-default flag); a `null` leaf is
// skipped rather than printed as "null" or a guessed value. An all-null capability (every
// field unresolvable) renders the same "—" every other empty cell in this app uses.
export function formatCapabilityServingSummary(capability: ServingCapability): string {
  const parts: string[] = [];
  if (capability.contextLength !== null) parts.push(`${formatCount(capability.contextLength)} ctx`);
  if (capability.serving.quant !== null) parts.push(capability.serving.quant);
  if (capability.serving.kvCacheType !== null) parts.push(`${capability.serving.kvCacheType} kv`);
  if (capability.serving.flashAttention === true) parts.push("fa");
  if (capability.serving.mtp === true) parts.push("mtp");
  if (capability.serving.moeCpuOffloadLayers !== null) parts.push(`ncmoe ${capability.serving.moeCpuOffloadLayers}`);
  if (capability.serving.ubatch !== null) parts.push(`ub ${capability.serving.ubatch}`);
  return parts.length > 0 ? parts.join(" · ") : "—";
}
