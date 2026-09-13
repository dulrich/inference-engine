import { join } from "node:path";
import type { RouterSlot, RouterSlotModel, RouterSlotsRecord, RouterSlotsResponse } from "@bench-dashboard/shared";
import { RouterSlotsRecordSchema } from "@bench-dashboard/shared";
import { parseRouterRecipe } from "./cmdRecipe.ts";
import { listDirOrNull, selectSlotsFile } from "./currentFile.ts";
import { SLOTS_TAIL_WINDOW_BYTES, parseJsonlLines, readTailLines } from "./tailReader.ts";

// `GET /api/router/slots`'s reader. The dashboard API does NOT call llama-swap — the
// locked no-outbound-HTTP decision (`apps/api/src/serving/llamaConfig.ts:18-33`) stands
// unchanged. The host unit `dashboard/scripts/router-slots-poller.mjs` is the only process
// that talks to `:4811`, and it writes `slots-YYYY-MM-DD.jsonl` into the SAME directory the
// router sink writes to (`ROUTER_TELEMETRY_DIR`), which this container mounts read-only.

/** Older than this and the slot panel is showing a snapshot, not a state: the poller's
 * default tick is 5 s, so 30 s is six missed ticks. `stale` is a reader verdict computed
 * from `sampledAt`, never a field the poller writes — the same discipline
 * `buildControlSection` applies to the host dispatcher's `status.json`. */
export const DEFAULT_SLOTS_STALE_MS = 30_000;

export interface SlotsFeedOptions {
  windowBytes?: number;
  staleMs?: number;
  /** Injected clock (tests). */
  now?: Date;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function booleanOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** One raw llama-server `/slots` entry -> the camelCase wire slot. The current build
 * returns only `{id, n_ctx, speculative, is_processing}` for an IDLE slot (measured
 * 2026-09-13), so the prompt/cache counters are nullable and `cacheHitRatio` is null
 * unless both are present with a positive denominator. */
export function projectSlot(raw: Record<string, unknown>): RouterSlot {
  const promptTokens = numberOrNull(raw.n_prompt_tokens);
  const promptTokensCache = numberOrNull(raw.n_prompt_tokens_cache);
  const cacheHitRatio =
    promptTokens !== null && promptTokensCache !== null && promptTokens > 0 ? promptTokensCache / promptTokens : null;
  return {
    id: numberOrNull(raw.id),
    nCtx: numberOrNull(raw.n_ctx),
    isProcessing: booleanOrNull(raw.is_processing),
    promptTokens,
    promptTokensCache,
    cacheHitRatio,
  };
}

/** One poller record -> the wire model list. `slots: null` on the record (a model the
 * poller deliberately did not proxy because `/running` did not report it `ready`, or one
 * whose fetch failed) becomes an empty `slots` array; `state`/`error` carry the why. */
export function projectSlotsRecord(record: RouterSlotsRecord): RouterSlotModel[] {
  return record.models.map((model) => {
    const slots = (model.slots ?? []).map(projectSlot);
    const nCtx = slots.find((slot) => slot.nCtx !== null)?.nCtx ?? null;
    return {
      model: model.model,
      state: model.state,
      ttl: model.ttl,
      nCtx,
      recipe: parseRouterRecipe(model.cmd),
      slots,
      error: model.error,
    };
  });
}

/** `GET /api/router/slots`'s body, built from the NEWEST `kind: "slots"` record in the
 * tail. Never throws. An `undefined` dir is `disabled`; an unreadable dir is `unreadable`;
 * a readable dir with no `slots-*.jsonl` (the host unit is not installed or not running)
 * or a file holding no parseable record is `empty`. */
export async function buildRouterSlotsResponse(
  dir: string | undefined,
  options: SlotsFeedOptions = {},
): Promise<RouterSlotsResponse> {
  if (!dir) return { available: false, reason: "disabled" };

  const now = options.now ?? new Date();
  const windowBytes = options.windowBytes ?? SLOTS_TAIL_WINDOW_BYTES;
  const staleMs = options.staleMs ?? DEFAULT_SLOTS_STALE_MS;

  const names = await listDirOrNull(dir);
  if (names === null) return { available: false, reason: "unreadable" };

  const file = selectSlotsFile(names, now);
  if (file === null) return { available: false, reason: "empty" };

  let lines: string[];
  try {
    ({ lines } = await readTailLines(join(dir, file), windowBytes));
  } catch {
    return { available: false, reason: "unreadable" };
  }

  const records = parseJsonlLines(lines, RouterSlotsRecordSchema).records;
  const newest = records[records.length - 1];
  if (newest === undefined) return { available: false, reason: "empty" };

  const sampledAtMs = Date.parse(newest.sampledAt);
  // An unparseable `sampledAt` is maximally stale rather than a NaN age on the wire.
  const ageMs = Number.isFinite(sampledAtMs) ? now.getTime() - sampledAtMs : Number.MAX_SAFE_INTEGER;

  return {
    available: true,
    sampledAt: newest.sampledAt,
    ageMs,
    stale: ageMs > staleMs,
    models: projectSlotsRecord(newest),
  };
}
