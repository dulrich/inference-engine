// In-flight generations panel logic (WP-4) over `RouterInFlight`. The reasoning/response
// split is a SERIES, not a verdict -- CC-17 -- so the widths computed here are painted with
// `--viz-1`/`--viz-2` and never with a status hue.

import type { RouterInFlight } from "@bench-dashboard/shared";
import { formatDurationMs, formatTokPerSec } from "./format.ts";
import type { StatusPill } from "./slots.ts";

export interface TokenSplit {
  total: number;
  /** Percentages of the two-segment bar; they sum to 100 whenever `total > 0`, and are both
   * 0 before the first token so the bar renders empty rather than half-filled. */
  reasoningPct: number;
  responsePct: number;
}

export function tokenSplit(reasoningTokens: number, responseTokens: number): TokenSplit {
  const reasoning = Math.max(0, reasoningTokens);
  const response = Math.max(0, responseTokens);
  const total = reasoning + response;
  if (total === 0) return { total: 0, reasoningPct: 0, responsePct: 0 };
  const reasoningPct = Math.round((reasoning / total) * 100);
  return { total, reasoningPct, responsePct: 100 - reasoningPct };
}

/** Phase is a lifecycle position, not a health verdict: prefill is quiet, decode is the
 * informational "tokens are moving" state. Both always render their label (CC-04). */
export function phasePill(phase: RouterInFlight["phase"]): StatusPill {
  return phase === "decode" ? { cls: "pill-info", label: "decode" } : { cls: "pill-muted", label: "prefill" };
}

export function elapsedLabel(elapsedMs: number): string {
  return formatDurationMs(elapsedMs);
}

/** `tokPerSec` is null until two progress samples exist (contract) -- an honest em-dash, not
 * a fabricated 0.0. */
export function tokPerSecLabel(tokPerSec: number | null): string {
  return formatTokPerSec(tokPerSec);
}

/** The screen-reader / redundancy text for the split bar: a bar alone is not a readout. */
export function splitLabel(split: TokenSplit, reasoningTokens: number, responseTokens: number): string {
  if (split.total === 0) return "no tokens yet";
  return `${reasoningTokens} reasoning / ${responseTokens} response`;
}
