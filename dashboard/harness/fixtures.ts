// Fixtures: the committed fixtures/ tree copied into per-run temp dirs (never mounted
// directly), plus clock-relative files and the synthetic serving-facts-v1 file written at run
// start, and the compose-shaped subject env. No live data.
import { chmod, cp, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { work, type AppSpec } from "./lib.ts";

/** The one fixture-root constant (WP-6 `git mv`s the tree to dashboard/fixtures/). */
export const FIXTURE_ROOT = join(import.meta.dir, "../fixtures");

/**
 * Run-start clock. Fresh/stale verdicts are placed an hour either side of it so they sit far from
 * every threshold: 180 s serving status (shared SERVING_STATUS_STALE_MS), 15 s telemetry in-flight (router/telemetryFeed.ts:21), 60 s serving
 * live residency (SERVING_LIVE_STALE_MS), and the 600 000 ms `?staleMs=` ceiling (routes/routerStatus.ts:20).
 * "Fresh" is a future timestamp: every reader computes `now - ts > threshold`, so a future ts is
 * fresh for any run length.
 */
export const NOW = Date.now();
export const FRESH = new Date(NOW + 3_600_000).toISOString();
export const STALE = new Date(NOW - 3_600_000).toISOString();
export const TODAY = new Date(NOW).toISOString().slice(0, 10);

export interface Fx {
  root: string;
  llamaConfig: string;
  /** Dir mounted at /facts holding a valid, revision-current `serving-facts.json`. */
  facts: string;
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

/** Control host file with `generated_at` set to `at` (serving-status.json). */
export async function writeHostFiles(dir: string, at: string) {
  await mkdir(dir, { recursive: true });
  const serving = await readJson(join(FIXTURE_ROOT, "serving/contract-v1/control/host/serving-status.json"));
  await writeJson(join(dir, "serving-status.json"), { ...serving, generated_at: at });
}

/** The synthetic `serving-facts-v1` the facts-present state reads (revision = this config's SHA-256). */
export const FACTS_MEASURED = [{ alias: "harness-alias", experimentId: "harness-exp-1" }];
export async function writeFactsFile(path: string, llamaConfig: string) {
  const revision = new Bun.CryptoHasher("sha256").update(await readFile(llamaConfig)).digest("hex");
  await writeJson(path, {
    schema: "serving-facts-v1",
    generatedAt: "2026-10-25T12:00:00.000Z",
    servingConfigRevision: revision,
    timing: [{ modelKey: "harness-model", aliases: ["harness-alias"], p50PrefillTokS: 512.5, p90PrefillTokS: 480, p50DecodeTokS: 40, p90DecodeTokS: 38.5, samplingArm: "greedy", sampleCount: 3, mtp: false }],
    measured: FACTS_MEASURED,
  });
}

export async function writeFixtures(): Promise<Fx> {
  const root = join(work, "fx");
  const fx: Fx = {
    root,
    llamaConfig: join(root, "llama-config.yaml"),
    facts: join(root, "facts"),
    host: join(root, "host"),
    router: join(root, "router"),
    sidecar: join(root, "sidecar"),
    servingFeed: join(root, "serving-feed"),
  };
  await mkdir(root, { recursive: true });
  await cp(join(FIXTURE_ROOT, "llama-config.yaml"), fx.llamaConfig);
  await writeFactsFile(join(fx.facts, "serving-facts.json"), fx.llamaConfig);
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
  LLAMA_CONFIG_PATH: "/llama-config.yaml",
  SERVING_FACTS_PATH: "/facts/serving-facts.json",
  CONTROL_REQUESTS_DIR: "/control/requests",
  CONTROL_HOST_DIR: "/control/host",
  ROUTER_TELEMETRY_DIR: "/var/log/inference-router",
  CODEX_SIDECAR_LOG_DIR: "/var/log/codex-sidecar",
  SERVING_FEED_DIR: "/var/log/inference-serving",
};
/** The compose mounts (all ro except the per-twin drop-box). */
export const composeMounts = (fx: Fx) => [
  `${fx.llamaConfig}:/llama-config.yaml:ro`,
  `${fx.facts}:/facts:ro`,
  `${fx.host}:/control/host:ro`,
  `${fx.router}:/var/log/inference-router:ro`,
  `${fx.sidecar}:/var/log/codex-sidecar:ro`,
  `${fx.servingFeed}:/var/log/inference-serving:ro`,
];
/** Full compose env + mounts, each twin with its own drop-box. */
export const composeSpec = (fx: Fx) => async (): Promise<AppSpec> => ({ env: COMPOSE_ENV, mounts: composeMounts(fx), requestsDir: await requestsBox() });
/** Every optional var unset (config.ts: CONTROL_*, ROUTER_TELEMETRY_DIR, CODEX_SIDECAR_LOG_DIR, SERVING_FEED_DIR stay
 * undefined), LLAMA_CONFIG_PATH and SERVING_FACTS_PATH pointing at absent paths (the 502 and facts-`absent` states). */
export const degradedSpec = (): AppSpec => ({
  env: { PORT: "4810", LLAMA_CONFIG_PATH: "/absent/config.yaml", SERVING_FACTS_PATH: "/absent/serving-facts.json" },
  mounts: [],
});
