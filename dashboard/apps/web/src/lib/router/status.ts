// Shared status vocabulary for the Router view's file-derived feeds (moved out of the retired
// `slots.ts` at plans/serving-control-redesign.md WP-6).

import type { RouterUnavailableReason } from "@bench-dashboard/shared";

export interface StatusPill {
  cls: string;
  label: string;
}

/** The three `available: false` reasons in words. */
export function unavailableMessage(reason: RouterUnavailableReason): string {
  switch (reason) {
    case "disabled":
      return "not configured";
    case "unreadable":
      return "log directory unreadable";
    default:
      return "no records yet";
  }
}
