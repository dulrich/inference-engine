// The Serving view's research-derived data (plan `research-to-eval-harness` WP-4): timing and
// the measured link come from the `serving-facts-v1` file the research dashboard writes, read
// server-side (`GET /api/serving/{timing,measured,facts-status}`). Facts being unavailable is
// an expected state: the columns say so in words and the served-alias table still renders.

import type {
  ServingFactsStatusResponse,
  ServingFactsUnavailableReason,
  ServingMeasuredEntry,
  ServingResponse,
  ServingTimingRow,
} from "@bench-dashboard/shared";
import { matchServingToMeasured, type ServingMatchRow } from "./matchMeasured.ts";

/** The research dashboard's port (`inference-research`, eval-harness). */
export const RESEARCH_PORT = 4816;

/** A run's page on the research dashboard, on the page's own host: the research SPA's own
 * `/runs/:id` drill-down route (`routes.ts runDetailPath` there), id URI-encoded. */
export function researchRunUrl(location: { protocol: string; hostname: string }, experimentId: string): string {
  return `${location.protocol}//${location.hostname}:${RESEARCH_PORT}/runs/${encodeURIComponent(experimentId)}`;
}

const REASON_TEXT: Record<ServingFactsUnavailableReason, string> = {
  disabled: "the facts file is not configured (SERVING_FACTS_PATH is unset)",
  absent: "the research dashboard has not exported its facts file yet",
  unreadable: "the facts file could not be read",
  invalid: "the facts file is not a valid serving-facts-v1 file",
  "stale-config": "the facts were computed against a different serving config",
};

export function factsUnavailableText(reason: ServingFactsUnavailableReason): string {
  return REASON_TEXT[reason] ?? reason;
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
}

/** Folds the four settled fetches into the view state. Only `GET /api/serving` failing
 * withholds the rows; timing, measured or facts-status failing (or the facts being typed
 * unavailable) leaves the rows rendered with empty timing/measured and a note. */
export function resolveServingLoad(
  serving: PromiseSettledResult<ServingResponse>,
  timing: PromiseSettledResult<ServingTimingRow[]>,
  measured: PromiseSettledResult<ServingMeasuredEntry[]>,
  factsStatus: PromiseSettledResult<ServingFactsStatusResponse>,
): ServingLoad {
  const timingRows = timing.status === "fulfilled" ? timing.value : [];
  const measuredList = measured.status === "fulfilled" ? measured.value : [];
  let factsNote = "";
  if (factsStatus.status === "rejected") {
    factsNote = `Timing and measured cells are unavailable: ${message(factsStatus.reason, "unable to load the facts status")}.`;
  } else if (!factsStatus.value.available) {
    factsNote = `Timing and measured cells are unavailable: ${factsUnavailableText(factsStatus.value.reason)}.`;
  } else if (timing.status === "rejected" || measured.status === "rejected") {
    const failed = timing.status === "rejected" ? timing.reason : (measured as PromiseRejectedResult).reason;
    factsNote = `Timing and measured cells are unavailable: ${message(failed, "unable to load the facts")}.`;
  }
  if (serving.status === "rejected") {
    return {
      configPath: null,
      rows: null,
      servingError: message(serving.reason, "Unable to load served aliases"),
      timingRows,
      factsNote,
    };
  }
  return {
    configPath: serving.value.configPath,
    rows: matchServingToMeasured(serving.value.models, measuredList),
    servingError: "",
    timingRows,
    factsNote,
  };
}
