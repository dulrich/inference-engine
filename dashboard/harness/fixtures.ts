// Fixtures: the committed fixtures/ tree copied into per-run temp dirs (never mounted
// directly), plus clock-relative files written at run start, the compose-shaped subject env, the
// ingest seed. No live data.
import { chmod, cp, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { check, execIngest, work, type AppSpec, type Instance, type Pair } from "./lib.ts";

/** The one fixture-root constant (WP-6 `git mv`s the tree to dashboard/fixtures/). */
export const FIXTURE_ROOT = join(import.meta.dir, "../fixtures");

/**
 * Run-start clock. Fresh/stale verdicts are placed an hour either side of it so they sit far from
 * every threshold: 180 s control/serving (routes/queue.ts CONTROL_STALE_THRESHOLD_MS,
 * shared SERVING_STATUS_STALE_MS), 15 s telemetry in-flight (router/telemetryFeed.ts:21), 60 s serving
 * live residency (SERVING_LIVE_STALE_MS), and the 600 000 ms `?staleMs=` ceiling (routes/routerStatus.ts:20).
 * "Fresh" is a future timestamp: every reader computes `now - ts > threshold`, so a future ts is
 * fresh for any run length. queue/eta.ts reads no clock (only record timestamps and the fixture
 * `live.started_at`), so its inputs are fixed.
 */
export const NOW = Date.now();
export const FRESH = new Date(NOW + 3_600_000).toISOString();
export const STALE = new Date(NOW - 3_600_000).toISOString();
export const TODAY = new Date(NOW).toISOString().slice(0, 10);

export interface Fx {
  root: string;
  records: string;
  records2: string;
  queues: string;
  llamaConfig: string;
  host: string;
  router: string;
  sidecar: string;
  servingFeed: string;
}

async function copyChildren(from: string, to: string) {
  await mkdir(to, { recursive: true });
  for (const name of await readdir(from)) await cp(join(from, name), join(to, name), { recursive: true });
}
async function writeJson(path: string, body: unknown) {
  await mkdir(join(path, ".."), { recursive: true });
  await writeFile(path, JSON.stringify(body, null, 2));
}
const readJson = async (path: string) => JSON.parse(await readFile(path, "utf8"));

/** Control host files with `generated_at` set to `at` (status.json + serving-status.json). */
export async function writeHostFiles(dir: string, at: string) {
  await mkdir(dir, { recursive: true });
  const status = await readJson(join(FIXTURE_ROOT, "control/contract-v1/control/host/status.json"));
  const serving = await readJson(join(FIXTURE_ROOT, "serving/contract-v1/control/host/serving-status.json"));
  await writeJson(join(dir, "status.json"), { ...status, generated_at: at });
  await writeJson(join(dir, "serving-status.json"), { ...serving, generated_at: at });
}

export async function writeFixtures(): Promise<Fx> {
  const root = join(work, "fx");
  const fx: Fx = {
    root,
    records: join(root, "records"),
    records2: join(root, "records2"),
    queues: join(root, "queues"),
    llamaConfig: join(root, "llama-config.yaml"),
    host: join(root, "host"),
    router: join(root, "router"),
    sidecar: join(root, "sidecar"),
    servingFeed: join(root, "serving-feed"),
  };
  // One /records holding the v2/v1 set, the legacy-timing side door and the projection record.
  for (const set of ["records", "records-legacy-timing", "records-projections"]) await copyChildren(join(FIXTURE_ROOT, set), fx.records);
  // Invalid records (ingest/record.ts:250 parse failure, :252 non-object).
  await mkdir(join(fx.records, "invalid-json"), { recursive: true });
  await writeFile(join(fx.records, "invalid-json", "config.json"), "{");
  await writeJson(join(fx.records, "invalid-array", "config.json"), [1, 2]);
  // ETA inputs (queue/eta.ts): a cost-table record for synth-joined's running c=8192 cell
  // (queue.test.ts "descriptor pass-through"), and timestamped cells for the live contract-queue.
  await writeJson(join(fx.records, "synth-joined", "hist-gemma-c8192", "config.json"), {
    cell_wall_seconds: 300,
    sweep_cell: { model: "gemma-4-12b-qat", server_flags: { "-c": 8192 } },
  });
  for (const [i, minute] of [5, 17, 31, 52].entries()) {
    await writeJson(join(fx.records, "contract-queue", `cell-${i}`, "config.json"), { timestamp: `2026-08-27T11:${String(minute).padStart(2, "0")}:00Z` });
  }
  await copyChildren(join(FIXTURE_ROOT, "records-two-level"), fx.records2);
  await copyChildren(join(FIXTURE_ROOT, "queues"), fx.queues);
  await copyChildren(join(FIXTURE_ROOT, "control/contract-v1/queues"), fx.queues);
  await cp(join(FIXTURE_ROOT, "llama-config.yaml"), fx.llamaConfig);
  await copyChildren(join(FIXTURE_ROOT, "control/contract-v1/control/host"), fx.host);
  await writeHostFiles(fx.host, FRESH);
  // The committed router/sidecar jsonl (2026-09-13: never today, so the newest-day pick applies
  // and every progress line is stale against the run clock). The committed serving feed
  // (2026-10-19) is likewise never fresh: /api/serving/live answers `stale`.
  await copyChildren(join(FIXTURE_ROOT, "router/logs"), fx.router);
  await copyChildren(join(FIXTURE_ROOT, "router/sidecar"), fx.sidecar);
  await copyChildren(join(FIXTURE_ROOT, "serving-feed"), fx.servingFeed);
  await run755(root);
  return fx;
}
/** uid 1001 (the image user) reads everything; drop-boxes are made 0777 by `requestsBox`. */
export async function run755(dir: string) {
  await chmod(dir, 0o755);
  for (const entry of await readdir(dir, { withFileTypes: true, recursive: true })) {
    await chmod(join(entry.parentPath, entry.name), entry.isDirectory() ? 0o755 : 0o644);
  }
}
let boxes = 0;
export async function requestsBox(): Promise<string> {
  boxes += 1;
  const dir = join(work, "requests", String(boxes));
  await mkdir(dir, { recursive: true });
  await chmod(dir, 0o777);
  return dir;
}

/** The compose `inference-dashboard` block's env (fleet-tools/fleet/compose.fleet.yml). */
export const COMPOSE_ENV = {
  PORT: "4810",
  APP_DB_PATH: "/data/bench.db",
  RECORDS_DIR: "/records",
  LLAMA_CONFIG_PATH: "/llama-config.yaml",
  QUEUES_DIR: "/queues",
  CONTROL_REQUESTS_DIR: "/control/requests",
  CONTROL_HOST_DIR: "/control/host",
  ROUTER_TELEMETRY_DIR: "/var/log/inference-router",
  CODEX_SIDECAR_LOG_DIR: "/var/log/codex-sidecar",
  SERVING_FEED_DIR: "/var/log/inference-serving",
};
/** The compose mounts (all ro except the per-twin drop-box). */
export const composeMounts = (fx: Fx) => [
  `${fx.records}:/records:ro`,
  `${fx.records2}:/records2:ro`,
  `${fx.llamaConfig}:/llama-config.yaml:ro`,
  `${fx.queues}:/queues:ro`,
  `${fx.host}:/control/host:ro`,
  `${fx.router}:/var/log/inference-router:ro`,
  `${fx.sidecar}:/var/log/codex-sidecar:ro`,
  `${fx.servingFeed}:/var/log/inference-serving:ro`,
];
/** Full compose env + mounts, each twin with its own drop-box. */
export const composeSpec = (fx: Fx) => async (): Promise<AppSpec> => ({ env: COMPOSE_ENV, mounts: composeMounts(fx), requestsDir: await requestsBox() });
/** Every optional var unset (config.ts: CONTROL_*, ROUTER_TELEMETRY_DIR, CODEX_SIDECAR_LOG_DIR, SERVING_FEED_DIR stay
 * undefined), LLAMA_CONFIG_PATH and QUEUES_DIR pointing at absent paths (502 / empty-queue states). */
export const degradedSpec = (fx: Fx): AppSpec => ({
  env: { PORT: "4810", APP_DB_PATH: "/data/bench.db", RECORDS_DIR: "/records", LLAMA_CONFIG_PATH: "/absent/config.yaml", QUEUES_DIR: "/absent/queues" },
  mounts: [`${fx.records}:/records:ro`],
});

/** Seeds one instance through its own ingest CLI (the operator command, Decision 3/6). */
export async function seedOne(target: Instance) {
  const result = await execIngest(target);
  check(`seed ${target.name}: ingest exit 0`, result.code === 0, `${result.code}: ${result.err.slice(0, 300)}`);
}
export const seedPair = async (pair: Pair) => {
  for (const target of pair) await seedOne(target);
};
