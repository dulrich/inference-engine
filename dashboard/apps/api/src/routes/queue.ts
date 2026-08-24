import { readdir } from "node:fs/promises";
import { join } from "node:path";
import type { CellState, ControlSection, QueueListItem, QueueListResponse, QueueState } from "@bench-dashboard/shared";
import { ControlSectionSchema } from "@bench-dashboard/shared";
import type { Hono } from "hono";
import { config } from "../config.ts";

// `GET /api/queue` (WP-A) — enumerates `*.json` queue files in `config.queuesDir`
// (skipping `*.state.json`), joins each against its sibling `<stem>.state.json` by
// alias, and returns a read-only summary. Never a route-wide 500: a missing/empty queues
// dir is `{queues: []}` (200), and a malformed queue/state file reports that ONE queue
// entry with an `error` field rather than failing the whole response (brief's pin).
//
// `buildControlSection` (WP-4, `plans/bakeoff-queue-control-plane.md`) lives in this same
// file beside `buildQueueListResponse` — same seam pattern (explicit dir argument, never
// `config` directly, so tests aren't fighting the process-wide `config.ts` singleton) —
// and is composed into the wire response by `registerQueueRoutes` below, independently of
// `buildQueueListResponse` itself (so every existing `buildQueueListResponse` test/caller
// keeps working unchanged: `control` is additive, never required).

const KNOWN_CELL_STATES: readonly CellState[] = [
  "pending",
  "running",
  "completed",
  "failed",
  "skipped-propagated",
  "aborted-hang",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}
function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}
function asCellState(value: unknown): CellState | null {
  const s = asString(value);
  return s !== null && (KNOWN_CELL_STATES as readonly string[]).includes(s) ? (s as CellState) : null;
}

const KNOWN_QUEUE_STATES: readonly QueueState[] = ["running", "paused-resumable", "completed", "aborted-hang"];
function asQueueState(value: unknown): QueueState | null {
  const s = asString(value);
  return s !== null && (KNOWN_QUEUE_STATES as readonly string[]).includes(s) ? (s as QueueState) : null;
}

async function readJsonFile(path: string): Promise<unknown> {
  const text = await Bun.file(path).text();
  return JSON.parse(text) as unknown;
}

interface ParsedQueueCell {
  alias: string;
  model: string | null;
  c: number | null;
  ctk: string | null;
  ctv: string | null;
  fa: string | null;
  samplingArm: string | null;
  trials: number | null;
}

/** A cell without a string `alias` is dropped — `alias` is the join key this whole route
 * depends on, and a queue-file cell that doesn't have one cannot be represented on the
 * wire (`QueueCellSchema.alias` is required, never nullable). */
function parseQueueCell(raw: unknown): ParsedQueueCell | null {
  if (!isRecord(raw)) return null;
  const alias = asString(raw.alias);
  if (alias === null) return null;
  return {
    alias,
    model: asString(raw.model),
    c: asNumber(raw.c),
    ctk: asString(raw.ctk),
    ctv: asString(raw.ctv),
    fa: asString(raw.fa),
    samplingArm: asString(raw.sampling_arm),
    trials: asNumber(raw.trials),
  };
}

interface ParsedQueueFile {
  deadline: string | null;
  vramProfile: string | null;
  cells: ParsedQueueCell[];
}

function parseQueueFile(raw: unknown): ParsedQueueFile {
  if (!isRecord(raw)) throw new Error("queue file is not a JSON object");
  if (!Array.isArray(raw.cells)) throw new Error("queue file 'cells' is not an array");
  return {
    deadline: asString(raw.deadline),
    vramProfile: asString(raw.vram_profile),
    cells: raw.cells.map(parseQueueCell).filter((c): c is ParsedQueueCell => c !== null),
  };
}

interface StateCellMatch {
  state: CellState | null;
  reason: string | null;
}

interface ParsedStateFile {
  queueState: QueueState | null;
  /** Keyed by alias — `cells[<cellId>].alias` (top-level, written by WP-B at init time),
   * falling back to `cells[<cellId>].detail.alias` when the top-level key is absent. An
   * entry with neither (every state file predating WP-B, e.g. the committed
   * `wp5-validation` pair) contributes no lookup entry at all, so every queue cell falls
   * through to unjoined for that state file — never a fabricated match. */
  cellsByAlias: Map<string, StateCellMatch>;
  /** Per-state counts over ALL of this state file's cell entries, regardless of whether
   * any of them join to a queue cell by alias (brief: "from the state file when joined"
   * means "when a state file exists", not "only for cells that matched"). */
  counts: Record<CellState, number>;
}

function parseStateFile(raw: unknown): ParsedStateFile {
  if (!isRecord(raw)) throw new Error("state file is not a JSON object");
  if (!isRecord(raw.cells)) throw new Error("state file 'cells' is not an object");

  const counts: Record<CellState, number> = {
    pending: 0,
    running: 0,
    completed: 0,
    failed: 0,
    "skipped-propagated": 0,
    "aborted-hang": 0,
  };
  const cellsByAlias = new Map<string, StateCellMatch>();

  for (const entry of Object.values(raw.cells)) {
    if (!isRecord(entry)) continue;
    const state = asCellState(entry.state);
    if (state !== null) counts[state] += 1;
    const reason = asString(entry.reason);
    const detail = isRecord(entry.detail) ? entry.detail : null;
    const alias = asString(entry.alias) ?? (detail ? asString(detail.alias) : null);
    if (alias !== null) cellsByAlias.set(alias, { state, reason });
  }

  return { queueState: asQueueState(raw.queue_state), cellsByAlias, counts };
}

const NULL_CELL_COUNTS: Record<CellState, number | null> = {
  pending: null,
  running: null,
  completed: null,
  failed: null,
  "skipped-propagated": null,
  "aborted-hang": null,
};

/** The route's full logic, over an explicit `queuesDir` — factored out of the Hono
 * handler (which just calls this with `config.queuesDir`) so tests can exercise every
 * scenario (missing dir, empty dir, joined, unjoined, malformed) directly, without
 * fighting `config`'s "computed once at module load from `process.env`" contract (a
 * single process-wide `bun test` run shares that module across every test FILE, not just
 * within one — a per-scenario `QUEUES_DIR` env override only reliably wins if that
 * scenario's file happens to be the first anywhere in the run to import `config.ts`). */
export async function buildQueueListResponse(queuesDir: string): Promise<QueueListResponse> {
  const generatedAt = new Date().toISOString();

  let fileNames: string[];
  try {
    const entries = await readdir(queuesDir, { withFileTypes: true });
    fileNames = entries
      .filter((entry) => entry.isFile() && entry.name.endsWith(".json") && !entry.name.endsWith(".state.json"))
      .map((entry) => entry.name)
      .sort();
  } catch {
    // Missing (or otherwise unreadable) queues dir — not an error, just no queues.
    return { queues: [], generatedAt };
  }

  const queues: QueueListItem[] = [];
  for (const fileName of fileNames) {
    const stem = fileName.slice(0, -".json".length);
    try {
      const queueFile = parseQueueFile(await readJsonFile(join(queuesDir, fileName)));

      const statePath = join(queuesDir, `${stem}.state.json`);
      const stateFile = (await Bun.file(statePath).exists()) ? parseStateFile(await readJsonFile(statePath)) : null;

      const cells = queueFile.cells.map((cell) => {
        const match = stateFile?.cellsByAlias.get(cell.alias) ?? null;
        return {
          alias: cell.alias,
          model: cell.model,
          c: cell.c,
          ctk: cell.ctk,
          ctv: cell.ctv,
          fa: cell.fa,
          samplingArm: cell.samplingArm,
          trials: cell.trials,
          state: match?.state ?? null,
          reason: match?.reason ?? null,
        };
      });

      queues.push({
        name: stem,
        queueState: stateFile?.queueState ?? null,
        deadline: queueFile.deadline,
        vramProfile: queueFile.vramProfile,
        cellCounts: stateFile ? stateFile.counts : NULL_CELL_COUNTS,
        cells,
      });
    } catch (error) {
      queues.push({ name: stem, error: error instanceof Error ? error.message : String(error) });
    }
  }

  return { queues, generatedAt };
}

// ---- `control` section (WP-4): `<CONTROL_HOST_DIR>/status.json` ("control-status-v1",
// snake_case) -> `ControlSection` (camelCase). Field-level renaming only — TYPE
// validation is `ControlSectionSchema.safeParse` itself below, so "schema-invalid" and
// "wrong field types" collapse to the exact same `null` outcome (the brief's pin: missing
// env, missing dir, missing file, unparseable JSON, or schema-invalid all -> `null`,
// honest, never a 500).

/** 3x the minutely dispatcher period (plan "Staleness contract"). */
const CONTROL_STALE_THRESHOLD_MS = 180_000;

function projectControlLive(raw: unknown): unknown {
  if (raw === null || raw === undefined) return null;
  if (!isRecord(raw)) return raw; // let the schema reject the wrong shape
  return { requestId: raw.request_id, queueStem: raw.queue_stem, phase: raw.phase, startedAt: raw.started_at };
}

function projectControlLastRun(raw: unknown): unknown {
  if (raw === null || raw === undefined) return null;
  if (!isRecord(raw)) return raw;
  return {
    requestId: raw.request_id,
    endedAt: raw.ended_at,
    runExit: raw.run_exit,
    verifyExit: raw.verify_exit,
    disposition: raw.disposition,
  };
}

function projectControlRequestDigest(raw: unknown): unknown {
  if (!isRecord(raw)) return raw;
  return { requestId: raw.request_id, action: raw.action, state: raw.state, reason: raw.reason, requestedAt: raw.requested_at };
}

/** Reads+projects `<controlHostDir>/status.json` into the wire `ControlSection`.
 * `undefined` `controlHostDir` (env unset) short-circuits to `null` before any I/O.
 * `stale` is computed here, at read time, from `generated_at` vs "now" — never trusted
 * from the file itself (the dispatcher writes `generated_at`, not `stale`). */
export async function buildControlSection(controlHostDir: string | undefined): Promise<ControlSection | null> {
  if (!controlHostDir) return null;

  let raw: unknown;
  try {
    raw = await readJsonFile(join(controlHostDir, "status.json"));
  } catch {
    // Missing dir, missing file, or unparseable JSON — all honest `null`.
    return null;
  }
  if (!isRecord(raw)) return null;

  const generatedAtMs = typeof raw.generated_at === "string" ? Date.parse(raw.generated_at) : Number.NaN;
  const stale = !Number.isFinite(generatedAtMs) || Date.now() - generatedAtMs > CONTROL_STALE_THRESHOLD_MS;

  const candidate = {
    statusGeneratedAt: raw.generated_at,
    stale,
    armed: raw.armed,
    live: projectControlLive(raw.live),
    lastRun: projectControlLastRun(raw.last_run),
    anomalies: raw.anomalies,
    requests: Array.isArray(raw.requests) ? raw.requests.map(projectControlRequestDigest) : raw.requests,
  };

  const parsed = ControlSectionSchema.safeParse(candidate);
  return parsed.success ? parsed.data : null;
}

export function registerQueueRoutes(app: Hono): void {
  app.get("/api/queue", async (c) => {
    const response = await buildQueueListResponse(config.queuesDir);
    const control = await buildControlSection(config.controlHostDir);
    return c.json({ ...response, control });
  });
}
