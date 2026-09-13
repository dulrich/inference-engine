// Cache & slots panel logic (WP-4) over `RouterSlotsResponse`. Every derivation here is a
// projection of the frozen contract -- notably `recipe`'s all-nullable members, where an
// absent flag is `null` and is rendered as ABSENT rather than as a guessed default (the
// `serving/llamaConfig.ts` posture the API side already holds).

import type { RouterSlot, RouterSlotRecipe, RouterUnavailableReason } from "@bench-dashboard/shared";
import { formatAgeMs, formatRatioPct } from "./format.ts";

export interface RecipeFlag {
  label: string;
  value: string;
}

/** Only the non-null flags, in the recipe's declared order. `mtp` is a boolean on the wire
 * (`--spec-type` OR `-md` present) and reads as "on"/"off"; the rest render their value. */
export function recipeFlags(recipe: RouterSlotRecipe): RecipeFlag[] {
  const flags: RecipeFlag[] = [];
  if (recipe.ctk !== null) flags.push({ label: "ctk", value: recipe.ctk });
  if (recipe.ctv !== null) flags.push({ label: "ctv", value: recipe.ctv });
  if (recipe.fa !== null) flags.push({ label: "fa", value: recipe.fa });
  if (recipe.reasoningBudget !== null) flags.push({ label: "reasoning budget", value: String(recipe.reasoningBudget) });
  if (recipe.mtp !== null) flags.push({ label: "mtp", value: recipe.mtp ? "on" : "off" });
  if (recipe.moeCpuLayers !== null) flags.push({ label: "moe cpu layers", value: String(recipe.moeCpuLayers) });
  return flags;
}

/** The prefix-cache readout. Null is the contract's "idle slot, no prompt counters yet"
 * (measured 2026-09-13), not a zero hit rate -- so it renders the em-dash, never "0 %". */
export function cacheRatioLabel(slot: RouterSlot): string {
  return formatRatioPct(slot.cacheHitRatio);
}

export interface StatusPill {
  cls: string;
  label: string;
}

/** llama-swap's model state is an OPEN string (`ready`, `stopping`, `starting`, ...) -- the
 * known values get a hue, anything else stays informational rather than being mislabelled.
 * CC-04: the label text is always rendered beside the class, hue is never the sole carrier. */
export function modelStatePill(state: string | null): StatusPill {
  if (state === null) return { cls: "pill-muted", label: "unknown" };
  if (state === "ready") return { cls: "pill-ok", label: state };
  if (state === "starting" || state === "stopping") return { cls: "pill-warn", label: state };
  return { cls: "pill-info", label: state };
}

/** `isProcessing: null` means the poller did not project the flag for this slot -- distinct
 * from a known-idle slot, so it gets its own word. */
export function processingPill(isProcessing: boolean | null): StatusPill {
  if (isProcessing === null) return { cls: "pill-muted", label: "unknown" };
  return isProcessing ? { cls: "pill-info", label: "processing" } : { cls: "pill-muted", label: "idle" };
}

/** The three `available: false` reasons in words. `empty` names the operator action that
 * fixes it (the host poller unit is installed by the user, never by the dashboard). */
export function unavailableMessage(reason: RouterUnavailableReason): string {
  switch (reason) {
    case "disabled":
      return "not configured";
    case "unreadable":
      return "log directory unreadable";
    default:
      return "no slots samples yet — enable the host poller unit";
  }
}

/** Header line for the panel: the sample's age from the READER-supplied `ageMs` (the API
 * computes it against its own clock, so the browser's skew never shows up here). */
export function sampleAgeLabel(ageMs: number): string {
  return `sampled ${formatAgeMs(ageMs)}`;
}
