// Consumes `GET /api/serving/facts` (one `serving-facts-v1` generation per response) and
// `GET /api/serving`; produces the Serving view's state. Facts being unavailable is an expected
// state: the columns say so in words and the served-alias table still renders.

import type {
  ServingFactsResponse,
  ServingFactsUnavailableReason,
  ServingResponse,
  ServingTimingRow,
} from "@bench-dashboard/shared";
import { matchServingToMeasured, type ServingMatchRow } from "./matchMeasured.ts";

/** A run's page on the research dashboard, on the page's own host (LAN and WG both work) at
 * the port the server reports: the research SPA's `/runs/:id` route, id URI-encoded. */
export function researchRunUrl(
  location: { protocol: string; hostname: string },
  researchPort: number,
  experimentId: string,
): string {
  return `${location.protocol}//${location.hostname}:${researchPort}/runs/${encodeURIComponent(experimentId)}`;
}

const REASON_TEXT: Record<ServingFactsUnavailableReason, string> = {
  disabled: "the facts file is not configured (SERVING_FACTS_PATH is unset)",
  absent: "the research dashboard has not exported its facts file yet",
  unreadable: "the facts file could not be read",
  invalid: "the facts file is not a valid serving-facts-v1 file",
  "stale-config": "the facts were computed against a different serving config",
};

export function factsUnavailableText(reason: ServingFactsUnavailableReason): string {
  return REASON_TEXT[reason];
}

function message(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export interface ServingLoad {
  /** `null` when the serving config itself failed (the only thing that blanks the table). */
  configPath: string | null;
  rows: ServingMatchRow[] | null;
  servingError: string;
  timingRows: ServingTimingRow[];
  /** Non-empty when timing/measured are unavailable; shown in words, never blanks the table. */
  factsNote: string;
  /** The facts file's `generatedAt` when available, else `null`. */
  factsGeneratedAt: string | null;
  /** The research dashboard's port for run links; `null` until a facts response arrives. */
  researchPort: number | null;
}

/** Folds the serving and facts fetches into the view state. Only `GET /api/serving` failing
 * withholds the rows; the facts failing or being typed unavailable leaves the rows rendered
 * with empty timing/measured and a note. */
export function resolveServingLoad(
  serving: PromiseSettledResult<ServingResponse>,
  facts: PromiseSettledResult<ServingFactsResponse>,
): ServingLoad {
  const available = facts.status === "fulfilled" && facts.value.available ? facts.value : null;
  const timingRows = available?.timing ?? [];
  const measuredList = available?.measured ?? [];
  let factsNote = "";
  if (facts.status === "rejected") {
    factsNote = `Timing and measured cells are unavailable: ${message(facts.reason, "unable to load the facts")}.`;
  } else if (!facts.value.available) {
    factsNote = `Timing and measured cells are unavailable: ${factsUnavailableText(facts.value.reason)}.`;
  }
  const shared = {
    timingRows,
    factsNote,
    factsGeneratedAt: available?.generatedAt ?? null,
    researchPort: facts.status === "fulfilled" ? facts.value.researchPort : null,
  };
  if (serving.status === "rejected") {
    return {
      configPath: null,
      rows: null,
      servingError: message(serving.reason, "Unable to load served aliases"),
      ...shared,
    };
  }
  return {
    configPath: serving.value.configPath,
    rows: matchServingToMeasured(serving.value.models, measuredList),
    servingError: "",
    ...shared,
  };
}
