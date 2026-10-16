// Read-side phases: routes (every route registered in apps/api/src/app.ts + routes/*.ts, under the
// full compose env and under the degraded env), spa, and db-compat (a --db-image-built DB, and the
// pre-ALTER `experiments` column set).
import { cp, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { dumpOf, ISO_8601 } from "./dump.ts";
import { buildPreAlter, composeSpec, degradedSpec, RECONCILED, seedPair, type Fx } from "./fixtures.ts";
import { both, check, checkEqual, contentType, diff, execIngest, expectStatus, pin, request, routeIncluded, run, skip, startPair, stopPair, work, type Json, type Pair } from "./lib.ts";

/** Every GET read route against a seeded pair (the shared battery of routes and db-compat). */
export async function readBattery(pair: Pair, label: string) {
  const ids: string[] = ((await request(pair[0], "GET", "/api/experiments")).json ?? []).map((e: Json) => e.id);
  const health = await both(pair, `${label} health`, "GET", "/health");
  pin(health, `${label} health: status ok, experimentCount = list length`, (j) => j?.status === "ok" && j?.experimentCount === ids.length);
  const list = await both(pair, `${label} experiments list`, "GET", "/api/experiments");
  pin(list, `${label} experiments list non-empty`, (j) => Array.isArray(j) && j.length > 0);
  for (const id of ids) {
    const detail = await both(pair, `${label} experiment ${id}`, "GET", `/api/experiments/${encodeURIComponent(id)}`);
    pin(detail, `${label} experiment ${id}: detail with trials+metrics`, (j) => j?.id === id && Array.isArray(j?.trials) && Array.isArray(j?.metrics));
    await both(pair, `${label} curves ${id}`, "GET", `/api/curves/${encodeURIComponent(id)}`);
  }
  expectStatus(await both(pair, `${label} unknown experiment`, "GET", "/api/experiments/no-such-experiment"), `${label} unknown experiment`, 404);
  await both(pair, `${label} unknown curves`, "GET", "/api/curves/no-such-experiment");
  const repos = [...new Set(((list?.json ?? (await request(pair[0], "GET", "/api/experiments")).json) as Json[]).map((e) => e.lineage?.hfRepo).filter((r) => typeof r === "string"))] as string[];
  for (const repo of repos) {
    // routes/ladders.ts: wildcard route, the repo id keeps its `/` (and is decodeURIComponent'd).
    const ladder = await both(pair, `${label} ladder ${repo}`, "GET", `/api/ladders/${repo}`);
    pin(ladder, `${label} ladder ${repo}: rows`, (j) => Array.isArray(j) && j.length > 0);
    await both(pair, `${label} ladder ${repo} (encoded slash)`, "GET", `/api/ladders/${encodeURIComponent(repo)}`);
  }
  await both(pair, `${label} ladder unknown repo`, "GET", "/api/ladders/nobody/nothing-GGUF");
  await both(pair, `${label} ladder empty repo`, "GET", "/api/ladders/");
  const serving = await both(pair, `${label} serving`, "GET", "/api/serving");
  const timing = await both(pair, `${label} serving timing`, "GET", "/api/serving/timing");
  await both(pair, `${label} serving status`, "GET", "/api/serving/status");
  const capture = await both(pair, `${label} capture export`, "GET", "/api/export/scoreboard-capture");
  pin(capture, `${label} capture: v1 envelope, one record per experiment`, (j) => capture?.status !== 200 || j?.captureVersion === 1 && j?.stability === "v1" && j?.recordCount === ids.length && ISO_8601.test(j?.generatedAt ?? ""));
  if (ids.length > 1) {
    const two = await both(pair, `${label} capture ?id x2`, "GET", `/api/export/scoreboard-capture?id=${encodeURIComponent(ids[1]!)}&id=${encodeURIComponent(ids[0]!)}`);
    pin(two, `${label} capture ?id order follows the query`, (j) => two?.status !== 200 || j?.recordCount === 2 && j?.records?.[0]?.experimentId === ids[1]);
  }
  await both(pair, `${label} capture ?id unknown`, "GET", "/api/export/scoreboard-capture?id=no-such-experiment");
  const queue = await both(pair, `${label} queue`, "GET", "/api/queue");
  for (const q of ["telemetry", "slots"]) {
    for (const query of ["", "?staleMs=999", "?staleMs=abc", "?staleMs=600000", "?staleMs=600001"]) {
      await both(pair, `${label} router ${q}${query}`, "GET", `/api/router/${q}${query}`);
    }
  }
  await both(pair, `${label} router sidecar`, "GET", "/api/router/sidecar");
  return { serving, timing, queue, capture };
}

/** The JSON 404 for unknown /api/* (app.ts `app.all("/api/*")`) under every method. */
async function notFoundBattery(pair: Pair) {
  for (const [method, path] of [["GET", "/api/nope"], ["GET", "/api"], ["POST", "/api/experiments"], ["GET", "/api/control/arm"], ["DELETE", "/api/queue/x"], ["PUT", "/api/serving/visibility"]] as const) {
    const reply = await both(pair, "unknown api route", method, path);
    pin(reply, `${method} ${path}: JSON 404 {error:"not found"}`, (j) => reply?.status === 404 && contentType(reply) === "application/json" && j?.error === "not found");
  }
}

export async function routesPhase(main: Pair, fx: Fx) {
  const r = await readBattery(main, "full env");
  pin(r.serving, "serving: 200 with models", (j) => r.serving?.status === 200 && Array.isArray(j?.models) && j.models.length > 0);
  pin(r.timing, "serving timing: 200 array", (j) => r.timing?.status === 200 && Array.isArray(j));
  pin(r.queue, "queue: queues incl. malformed entries, control fresh", (j) => Array.isArray(j?.queues) && j.queues.some((q: Json) => typeof q.error === "string") && j?.control?.stale === false && ISO_8601.test(j?.generatedAt ?? ""));
  pin(r.queue, "queue: synth-joined ETA on the model-table basis (queue.test.ts pass-through)", (j) => j?.queues?.find((q: Json) => q.name === "synth-joined")?.eta?.basis === "model-table");
  if (routeIncluded("/api/router/telemetry")) {
    const telemetry = await request(main[0], "GET", "/api/router/telemetry");
    check("committed telemetry: newest day file, every in-flight aged out", telemetry.json?.available === true && telemetry.json?.file === "router-2026-09-13.10.jsonl" && telemetry.json?.inFlight?.length === 0, telemetry.text.slice(0, 300));
  }
  if (routeIncluded("/api/router/slots")) {
    const slots = await request(main[0], "GET", "/api/router/slots");
    check("committed slots: stale", slots.json?.available === true && slots.json?.stale === true, slots.text.slice(0, 300));
  }
  await notFoundBattery(main);

  const degraded = await startPair("degraded", () => degradedSpec(fx));
  try {
    await seedPair(degraded);
    const d = await readBattery(degraded, "degraded env");
    for (const [name, reply] of [["serving", d.serving], ["serving timing", d.timing], ["capture", d.capture]] as const) {
      pin(reply, `degraded ${name}: 502 {error} on an absent LLAMA_CONFIG_PATH`, (j) => reply?.status === 502 && typeof j?.error === "string");
    }
    pin(d.queue, "degraded queue: no queues, control null", (j) => Array.isArray(j?.queues) && j.queues.length === 0 && j?.control === null);
    for (const path of ["/api/router/telemetry", "/api/router/slots", "/api/router/sidecar", "/api/serving/status"]) {
      if (!routeIncluded(path)) continue;
      const reply = await request(degraded[0], "GET", path);
      checkEqual(`degraded ${path}: 200 disabled`, { status: reply.status, body: reply.json }, { status: 200, body: { available: false, reason: "disabled" } });
    }
    for (const path of ["/api/control/arm", "/api/control/disarm", "/api/control/stop", "/api/control/start", "/api/control/cancel", "/api/serving/visibility"]) {
      const reply = await both(degraded, "unset CONTROL_REQUESTS_DIR", "POST", path, {});
      pin(reply, `degraded POST ${path}: 503`, (j) => reply?.status === 503 && j?.error === "control plane not configured: CONTROL_REQUESTS_DIR is unset");
    }
  } finally {
    await stopPair(degraded);
  }
}

export async function spaPhase(pair: Pair) {
  const sha = (b: Uint8Array) => new Bun.CryptoHasher("sha256").update(b).digest("hex");
  const view = async (path: string, refPath = path) => {
    if (!routeIncluded(path)) {
      skip(`spa ${path}`, "outside --routes");
      return null;
    }
    const [s, r] = [await request(pair[0], "GET", path), await request(pair[1], "GET", refPath)];
    // Status + headers are compared to the reference twin; body bytes are NOT. Plan WP-6's
    // required apps/web/src/api.ts edit (and the generated shared module) changed the web bundle
    // bytes and hashed asset names, while the reference (--db-image inference-dashboard:bun-dev,
    // kept through WP-7) was built pre-edit. SPA content is instead checked against the subject
    // image's own /web tree below.
    const facts = (x: typeof s) => ({ status: x.status, type: contentType(x), cache: x.headers.get("cache-control") });
    const d = diff(facts(s), facts(r));
    check(`spa GET ${path} matches reference${refPath === path ? "" : ` (${refPath})`}`, d === null, d ?? "");
    return s;
  };
  const webDir = join(work, "subject-web");
  // Web root inside the subject image: /web (Rust image) or /app/apps/web/dist (Bun image).
  await run(["docker", "cp", `${pair[0].name}:/web`, webDir], true);
  if (!(await Bun.file(join(webDir, "index.html")).exists())) {
    await run(["docker", "cp", `${pair[0].name}:/app/apps/web/dist`, webDir]);
  }
  const onDisk = async (rel: string) => new Uint8Array(await Bun.file(join(webDir, rel)).arrayBuffer());
  const index = await view("/");
  if (!index) return;
  check("spa / body == subject /web/index.html", sha(index.bytes) === sha(await onDisk("index.html")), `${index.bytes.length} bytes`);
  check("spa /: html, no-cache", index.status === 200 && contentType(index) === "text/html" && index.headers.get("cache-control") === "no-cache" && index.text.includes("<html"), `${index.status} ${index.headers.get("cache-control")}`);
  const assets = [...index.text.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]!);
  check("spa index references hashed assets", assets.length > 0, index.text.slice(0, 300));
  // Hashed names differ between the bundles, so each subject asset is paired with the reference
  // index's asset at the same position (same build layout, different content hash).
  const refIndex = await request(pair[1], "GET", "/");
  const refAssets = [...refIndex.text.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]!);
  check("spa index asset count matches reference", refAssets.length === assets.length, `${assets.length} vs ${refAssets.length}`);
  for (const [i, asset] of assets.entries()) {
    const reply = await view(asset, refAssets[i] ?? asset);
    if (reply) check(`spa ${asset}: 200 immutable`, reply.status === 200 && reply.headers.get("cache-control") === "public, max-age=31536000, immutable" && reply.bytes.length > 0, `${reply.status} ${reply.headers.get("cache-control")}`);
    if (reply) check(`spa ${asset} body == subject /web${asset}`, (await Bun.file(join(webDir, asset)).exists()) && sha(reply.bytes) === sha(await onDisk(asset.slice(1))), `${reply.bytes.length} bytes`);
  }
  // app.ts spaResponse: anything not on disk inside the web root falls back to index.html, no-cache.
  for (const path of ["/runs", "/serving/some-model", "/assets/missing-0000.js", "/..%2f..%2f..%2fetc%2fpasswd", "/%2e%2e%2fpackage.json", "/assets/..%2f..%2f..%2fpackage.json"]) {
    const reply = await view(path);
    if (reply) check(`spa ${path}: index fallback, no-cache (no root escape)`, reply.status === 200 && reply.headers.get("cache-control") === "no-cache" && sha(reply.bytes) === sha(index.bytes), `${reply.status} ${reply.headers.get("cache-control")} ${reply.text.slice(0, 80)}`);
  }
}

/**
 * (a) a pair booted on copies of the --db-image-built DB serves the battery identically and boot
 * leaves the DB content unchanged; (b) the pre-ALTER `experiments` column set is reconciled at boot
 * (schema.ts RECONCILED_EXPERIMENTS_COLUMNS) and re-ingest restores the fresh rows.
 */
export async function dbCompatPhase(fx: Fx, seededDir: string) {
  const seeded = await dumpOf(seededDir);
  const dirs: [string, string] = [join(work, "data", "compat-s"), join(work, "data", "compat-r")];
  for (const d of dirs) {
    await mkdir(d, { recursive: true });
    await cp(seededDir, d, { recursive: true });
  }
  const pair = await startPair("compat", composeSpec(fx), dirs);
  try {
    await readBattery(pair, "db-image DB");
    checkEqual("db-image DB: subject boot leaves the DB content unchanged", (await dumpOf(dirs[0])).tables, seeded.tables);
  } finally {
    await stopPair(pair);
  }

  const pre: [string, string] = [join(work, "data", "prealter-s"), join(work, "data", "prealter-r")];
  for (const d of pre) await buildPreAlter(seededDir, d);
  const old = await startPair("prealter", composeSpec(fx), pre);
  try {
    const after = await dumpOf(pre[0]);
    checkEqual("pre-ALTER: subject schema equals the reference's", after.schema, (await dumpOf(pre[1])).schema);
    checkEqual("pre-ALTER: experiments columns reconciled to the fresh set", after.schema.tables.experiments, seeded.schema.tables.experiments);
    check("pre-ALTER: reconciled rows read schema_origin 'v2' (DDL default), other added columns NULL",
      (after.tables.experiments ?? []).every((row) => row.schema_origin === "v2" && RECONCILED.filter((c) => c !== "schema_origin").every((c) => row[c] === null)), JSON.stringify(after.tables.experiments?.[0]).slice(0, 300));
    await readBattery(old, "pre-ALTER DB");
    for (const target of old) {
      const result = await execIngest(target);
      check(`pre-ALTER ${target.name}: ingest exit 0`, result.code === 0, result.err.slice(0, 300));
    }
    const [s, r] = [await dumpOf(pre[0]), await dumpOf(pre[1])];
    checkEqual("pre-ALTER after ingest: subject dump equals reference", s, r);
    checkEqual("pre-ALTER after ingest: experiments rows equal the fresh ingest", s.tables.experiments, seeded.tables.experiments);
  } finally {
    await stopPair(old);
  }
}
