// Host-side llama-swap slot poller — the ONLY process in this fleet that asks llama-swap
// about slots (`plans/router-observability-and-tool-search-conformance.md` WP-3, user
// decision 2026-09-13: option (b)).
//
// Why it exists on the HOST and not in the dashboard API: the dashboard API makes zero
// outbound HTTP calls, a locked decision recorded in `dashboard/AGENTS.md`
// (the server crate has no HTTP client). Option (b) preserves that decision — this unit runs
// as the user's own systemd `--user` service, writes `slots-YYYY-MM-DD.jsonl` beside the
// router's telemetry sink, and the container reads that file read-only. It is a FILE
// WRITER with no listener, so the plan's "no new port and no new network service"
// assumption survives intact.
//
// THE SPAWN HAZARD, the single most important behaviour in this file:
// `/upstream/<model>/…` is a llama-swap PROXY path. Asking it about a model that is not
// resident SWAPS THAT MODEL INTO VRAM — a status page that enumerated configured models
// would load a 27B model to draw a card. So `/upstream/<model>/slots` is requested ONLY
// for models `GET /running` reports with `state === "ready"`, never for any other state,
// never for a model absent from `/running`, and never from a `/v1/models` listing. A
// non-ready model is still REPORTED (with `slots: null`) so the view can show it.
//
// DISABLED by user decision 2026-10-19, for two measured residency effects of polling `/upstream`:
// (a) `/upstream/<model>/slots` counts as activity for llama-swap's ttl, so a polled model never
// reaches its ttl (the 27B stayed resident ~10 h past ttl 1800 on 2026-10-19);
// (b) an `/upstream` hit on an exclusive-group model evicts the other groups' members, so each tick
// evicted bge-m3/bge-small and the next polls cold-started them (1.25 s each, ~10k spawns over 72 h).
// The installer refuses without `--i-accept-model-residency`.
//
// Bun, ESM, zero dependencies. Importing this module has no side effects; only direct
// execution (`import.meta.main`) starts the loop.

import { appendFileSync, mkdirSync, readdirSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_BASE_URL = "http://127.0.0.1:4811";
export const DEFAULT_OUT_DIR = join(homedir(), ".local", "state", "inference-router", "logs");
export const DEFAULT_INTERVAL_MS = 5000;
export const DEFAULT_TIMEOUT_MS = 2000;
export const DEFAULT_KEEP_DAYS = 7;

const SLOTS_FILE_PATTERN = /^slots-(\d{4}-\d{2}-\d{2})\.jsonl$/;
const MS_PER_DAY = 86_400_000;

/** `YYYY-MM-DD` in UTC — the same stamp the router sink and the sidecar use, so all three
 * feeds roll over together and the reader's file-selection rule is one rule. */
export function utcDayStamp(date) {
  return date.toISOString().slice(0, 10);
}

export function slotsFileName(date) {
  return `slots-${utcDayStamp(date)}.jsonl`;
}

function errorMessage(error) {
  if (error instanceof Error) return `${error.name}: ${error.message}`;
  return String(error);
}

async function fetchJson(fetchImpl, url, timeoutMs) {
  const response = await fetchImpl(url, {
    method: "GET",
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${url}`);
  return await response.json();
}

/** llama-swap's `/running` payload, tolerated in both observed spellings: a bare array, or
 * `{running: [...]}`. Anything else yields no entries rather than a throw. */
export function runningEntries(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload !== null && typeof payload === "object" && Array.isArray(payload.running)) return payload.running;
  return [];
}

/** llama-server's `/slots` payload: a bare array on the current build, `{slots: [...]}`
 * defensively. Anything else is an empty slot list, not an error. */
function slotsArray(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload !== null && typeof payload === "object" && Array.isArray(payload.slots)) return payload.slots;
  return [];
}

/** One tick. Returns the record to append — NEVER throws and never rejects: an absent
 * llama-swap yields `{models: [], error: "<why>"}`, and a single model's `/slots` failure
 * is recorded on that model while the rest of the tick proceeds. `fetchImpl` is injected so
 * the unit test can drive it against a `Bun.serve({port: 0})` fixture instead of `:4811`. */
export async function pollOnce(fetchImpl, baseUrl = DEFAULT_BASE_URL, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const sampledAt = new Date().toISOString();
  const base = baseUrl.replace(/\/+$/, "");

  let payload;
  try {
    payload = await fetchJson(fetchImpl, `${base}/running`, timeoutMs);
  } catch (error) {
    return { kind: "slots", ts: new Date().toISOString(), sampledAt, error: errorMessage(error), models: [] };
  }

  const models = [];
  for (const entry of runningEntries(payload)) {
    if (entry === null || typeof entry !== "object" || typeof entry.model !== "string") continue;
    const model = entry.model;
    const state = typeof entry.state === "string" ? entry.state : null;
    const ttl = typeof entry.ttl === "number" ? entry.ttl : null;
    const cmd = typeof entry.cmd === "string" ? entry.cmd : null;

    // ---- the spawn guard. Do not weaken this without re-reading the header. ----
    if (state !== "ready") {
      models.push({ model, state, ttl, cmd, slots: null, error: null });
      continue;
    }

    try {
      const raw = await fetchJson(fetchImpl, `${base}/upstream/${encodeURIComponent(model)}/slots`, timeoutMs);
      models.push({ model, state, ttl, cmd, slots: slotsArray(raw), error: null });
    } catch (error) {
      models.push({ model, state, ttl, cmd, slots: null, error: errorMessage(error) });
    }
  }

  return { kind: "slots", ts: new Date().toISOString(), sampledAt, error: null, models };
}

/** Appends one record as a single jsonl line. Mode `0644` so the dashboard container (uid
 * 1001) can read it through its `:ro` mount. Returns the file written. */
export function writeSlotsRecord(outDir, record, now = new Date()) {
  mkdirSync(outDir, { recursive: true });
  const file = join(outDir, slotsFileName(now));
  appendFileSync(file, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o644 });
  return file;
}

/** Deletes `slots-*.jsonl` strictly older than `keepDays` UTC days. Best effort: a file
 * that cannot be removed is not a reason to stop polling. Returns the names removed. */
export function pruneOldSlotsFiles(outDir, keepDays = DEFAULT_KEEP_DAYS, now = new Date()) {
  const cutoffDay = utcDayStamp(new Date(now.getTime() - keepDays * MS_PER_DAY));
  const removed = [];
  let names;
  try {
    names = readdirSync(outDir);
  } catch {
    return removed;
  }
  for (const name of names) {
    const match = SLOTS_FILE_PATTERN.exec(name);
    if (match === null || match[1] >= cutoffDay) continue;
    try {
      unlinkSync(join(outDir, name));
      removed.push(name);
    } catch {
      // best effort
    }
  }
  return removed;
}

const USAGE = `usage: bun scripts/router-slots-poller.mjs [options]

  --base-url <url>     llama-swap base URL          (default ${DEFAULT_BASE_URL})
  --out-dir <dir>      jsonl output directory       (default ~/.local/state/inference-router/logs)
  --interval-ms <n>    tick interval                (default ${DEFAULT_INTERVAL_MS})
  --timeout-ms <n>     per-request timeout          (default ${DEFAULT_TIMEOUT_MS})
  --keep-days <n>      retention, UTC days          (default ${DEFAULT_KEEP_DAYS})
  --once               one tick, then exit 0
  -h, --help           this text

Reads only. Queries GET /running, then GET /upstream/<model>/slots for READY models only
(querying a non-resident model through the proxy path would spawn it). Never POSTs, never
touches /v1/*, and writes nothing but slots-YYYY-MM-DD.jsonl in --out-dir.`;

export function parseArgs(argv) {
  const options = {
    baseUrl: DEFAULT_BASE_URL,
    outDir: DEFAULT_OUT_DIR,
    intervalMs: DEFAULT_INTERVAL_MS,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    keepDays: DEFAULT_KEEP_DAYS,
    once: false,
    help: false,
  };
  const numeric = { "--interval-ms": "intervalMs", "--timeout-ms": "timeoutMs", "--keep-days": "keepDays" };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--once") {
      options.once = true;
    } else if (arg === "-h" || arg === "--help") {
      options.help = true;
    } else if (arg === "--base-url") {
      options.baseUrl = argv[++i] ?? DEFAULT_BASE_URL;
    } else if (arg === "--out-dir") {
      options.outDir = argv[++i] ?? DEFAULT_OUT_DIR;
    } else if (arg in numeric) {
      const value = Number(argv[++i]);
      if (!Number.isFinite(value) || value <= 0) throw new Error(`${arg} needs a positive number`);
      options[numeric[arg]] = value;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}

/** The loop. Prunes on the first tick and again at every UTC date rollover. Returns a
 * process exit code; only `--once`/`--help` ever return. */
export async function main(argv, fetchImpl = fetch) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`router-slots-poller: ${errorMessage(error)}\n\n${USAGE}\n`);
    return 2;
  }
  if (options.help) {
    process.stdout.write(`${USAGE}\n`);
    return 0;
  }

  let lastDay = null;
  const tick = async () => {
    const now = new Date();
    const record = await pollOnce(fetchImpl, options.baseUrl, options.timeoutMs);
    writeSlotsRecord(options.outDir, record, now);
    const day = utcDayStamp(now);
    if (lastDay !== day) {
      pruneOldSlotsFiles(options.outDir, options.keepDays, now);
      lastDay = day;
    }
    return record;
  };

  if (options.once) {
    const record = await tick();
    process.stdout.write(`${JSON.stringify({ sampledAt: record.sampledAt, models: record.models.length, error: record.error })}\n`);
    return 0;
  }

  for (;;) {
    await tick();
    await new Promise((resolve) => setTimeout(resolve, options.intervalMs));
  }
}

if (import.meta.main) {
  process.exit(await main(process.argv.slice(2)));
}
