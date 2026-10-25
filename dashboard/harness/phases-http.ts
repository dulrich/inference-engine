// Read-side phases: routes (every route the serving dashboard registers, under the full compose
// env and under the degraded env, plus the JSON 404 for the research routes that moved to
// eval-harness `inference_research`) and spa.
import { join } from "node:path";
import { degradedSpec, FACTS_MEASURED, type Fx } from "./fixtures.ts";
import { both, check, checkEqual, contentType, diff, pin, request, routeIncluded, run, skip, startPair, stopPair, work, type Pair } from "./lib.ts";

/** Every GET read route against a pair (the routes phase battery). */
export async function readBattery(pair: Pair, label: string) {
  const health = await both(pair, `${label} health`, "GET", "/health");
  pin(health, `${label} health: status ok`, (j) => j?.status === "ok");
  const serving = await both(pair, `${label} serving`, "GET", "/api/serving");
  const timing = await both(pair, `${label} serving timing`, "GET", "/api/serving/timing");
  const measured = await both(pair, `${label} serving measured`, "GET", "/api/serving/measured");
  const facts = await both(pair, `${label} serving facts status`, "GET", "/api/serving/facts-status");
  await both(pair, `${label} serving status`, "GET", "/api/serving/status");
  const live = await both(pair, `${label} serving live`, "GET", "/api/serving/live");
  for (const query of ["", "?staleMs=999", "?staleMs=abc", "?staleMs=600000", "?staleMs=600001"]) {
    await both(pair, `${label} router telemetry${query}`, "GET", `/api/router/telemetry${query}`);
  }
  await both(pair, `${label} router sidecar`, "GET", "/api/router/sidecar");
  return { serving, timing, measured, facts, live };
}

/** The JSON 404 for unknown /api/* under every method, and for every retired research route. */
async function notFoundBattery(pair: Pair) {
  const research = [
    ["GET", "/api/experiments"], ["GET", "/api/experiments/fixture-v2-full"], ["GET", "/api/ladders"], ["GET", "/api/ladders/example-org/Example-Tiny-GGUF"],
    ["GET", "/api/curves/fixture-v2-full"], ["GET", "/api/queue"], ["GET", "/api/export/scoreboard-capture"],
    ["POST", "/api/control/arm"], ["POST", "/api/control/disarm"], ["POST", "/api/control/stop"], ["POST", "/api/control/start"], ["POST", "/api/control/cancel"],
  ] as const;
  for (const [method, path] of [["GET", "/api/router/slots"], ["GET", "/api/nope"], ["GET", "/api"], ["DELETE", "/api/queue/x"], ["PUT", "/api/serving/visibility"], ...research] as const) {
    const reply = await both(pair, "unknown api route", method, path);
    pin(reply, `${method} ${path}: JSON 404 {error:"not found"}`, (j) => reply?.status === 404 && contentType(reply) === "application/json" && j?.error === "not found");
  }
}

export async function routesPhase(main: Pair, _fx: Fx) {
  const r = await readBattery(main, "full env");
  pin(r.serving, "serving: 200 with models", (j) => r.serving?.status === 200 && Array.isArray(j?.models) && j.models.length > 0);
  pin(r.timing, "facts present: timing is the facts file's rows", (j) => r.timing?.status === 200 && Array.isArray(j) && j.length === 1 && j[0]?.modelKey === "harness-model");
  pin(r.measured, "facts present: measured is the facts file's list", (j) => r.measured?.status === 200 && diff(j, FACTS_MEASURED) === null);
  pin(r.facts, "facts present: status available with the generatedAt + revision", (j) => j?.available === true && j?.generatedAt === "2026-10-25T12:00:00.000Z" && /^[0-9a-f]{64}$/.test(j?.servingConfigRevision ?? ""));
  if (routeIncluded("/api/router/telemetry")) {
    const telemetry = await request(main[0], "GET", "/api/router/telemetry");
    check("committed telemetry: newest day file, every in-flight aged out", telemetry.json?.available === true && telemetry.json?.file === "router-2026-09-13.10.jsonl" && telemetry.json?.inFlight?.length === 0, telemetry.text.slice(0, 300));
  }
  pin(r.live, "committed serving feed (2026-10-19): typed stale", (j) => r.live?.status !== 200 || j?.available === false && j?.reason === "stale");
  await notFoundBattery(main);

  const degraded = await startPair("degraded", () => degradedSpec());
  try {
    const d = await readBattery(degraded, "degraded env");
    pin(d.serving, "degraded serving: 502 {error} on an absent LLAMA_CONFIG_PATH", (j) => d.serving?.status === 502 && typeof j?.error === "string");
    for (const [name, reply] of [["timing", d.timing], ["measured", d.measured]] as const) {
      pin(reply, `facts absent: ${name} 200 []`, (j) => reply?.status === 200 && Array.isArray(j) && j.length === 0);
    }
    pin(d.facts, "facts absent: typed {available:false, reason:absent}", (j) => d.facts?.status === 200 && diff(j, { available: false, reason: "absent" }) === null);
    for (const path of ["/api/router/telemetry", "/api/router/sidecar", "/api/serving/status", "/api/serving/live"]) {
      if (!routeIncluded(path)) continue;
      const reply = await request(degraded[0], "GET", path);
      checkEqual(`degraded ${path}: 200 disabled`, { status: reply.status, body: reply.json }, { status: 200, body: { available: false, reason: "disabled" } });
    }
    for (const path of ["/api/serving/visibility", "/api/catalog/add", "/api/catalog/remove", "/api/catalog/consumers"]) {
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
    // bytes and hashed asset names, so the reference twin (the same --image) is not a byte oracle for them. SPA content is instead checked against the subject
    // image's own /web tree below.
    const facts = (x: typeof s) => ({ status: x.status, type: contentType(x), cache: x.headers.get("cache-control") });
    const d = diff(facts(s), facts(r));
    check(`spa GET ${path} matches reference${refPath === path ? "" : ` (${refPath})`}`, d === null, d ?? "");
    return s;
  };
  const webDir = join(work, "subject-web");
  // Web root inside the subject image: /web (Rust image).
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
  for (const path of ["/runs", "/runs/fixture-v2-full", "/serving/some-model", "/assets/missing-0000.js", "/..%2f..%2f..%2fetc%2fpasswd", "/%2e%2e%2fpackage.json", "/assets/..%2f..%2f..%2fpackage.json"]) {
    const reply = await view(path);
    if (reply) check(`spa ${path}: index fallback, no-cache (no root escape)`, reply.status === 200 && reply.headers.get("cache-control") === "no-cache" && sha(reply.bytes) === sha(index.bytes), `${reply.status} ${reply.headers.get("cache-control")} ${reply.text.slice(0, 80)}`);
  }
}
