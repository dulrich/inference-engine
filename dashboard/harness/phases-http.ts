// Read-side phases: routes (every route the serving dashboard registers, under the full compose
// env and under the degraded env, plus the JSON 404 for the research routes that moved to
// eval-harness `inference_research`) and spa.
import { join } from "node:path";
import { degradedSpec, FACTS_MEASURED } from "./fixtures.ts";
import { check, checkEqual, contentType, diff, get, image, pin, request, routeIncluded, run, skip, startApp, stopApp, work, type Instance } from "./lib.ts";

/** Every GET read route against one instance (the routes phase battery). */
export async function readBattery(target: Instance, label: string) {
  const health = await get(target, `${label} health`, "GET", "/health");
  pin(health, `${label} health: status ok`, (j) => j?.status === "ok");
  const serving = await get(target, `${label} serving`, "GET", "/api/serving");
  const facts = await get(target, `${label} serving facts`, "GET", "/api/serving/facts");
  await get(target, `${label} serving status`, "GET", "/api/serving/status");
  const live = await get(target, `${label} serving live`, "GET", "/api/serving/live");
  for (const query of ["", "?staleMs=999", "?staleMs=abc", "?staleMs=600000", "?staleMs=600001"]) {
    await get(target, `${label} router telemetry${query}`, "GET", `/api/router/telemetry${query}`);
  }
  await get(target, `${label} router sidecar`, "GET", "/api/router/sidecar");
  return { serving, facts, live };
}

/** The JSON 404 for unknown /api/* under every method, for every retired research route, and
 * for the three split facts routes `/api/serving/facts` replaced. */
async function notFoundBattery(target: Instance) {
  const research = [
    ["GET", "/api/experiments"], ["GET", "/api/experiments/fixture-v2-full"], ["GET", "/api/ladders"], ["GET", "/api/ladders/example-org/Example-Tiny-GGUF"],
    ["GET", "/api/curves/fixture-v2-full"], ["GET", "/api/queue"], ["GET", "/api/export/scoreboard-capture"],
    ["POST", "/api/control/arm"], ["POST", "/api/control/disarm"], ["POST", "/api/control/stop"], ["POST", "/api/control/start"], ["POST", "/api/control/cancel"],
    ["GET", "/api/serving/timing"], ["GET", "/api/serving/measured"], ["GET", "/api/serving/facts-status"],
  ] as const;
  for (const [method, path] of [["GET", "/api/router/slots"], ["GET", "/api/nope"], ["GET", "/api"], ["DELETE", "/api/queue/x"], ["PUT", "/api/serving/visibility"], ...research] as const) {
    const reply = await get(target, "unknown api route", method, path);
    pin(reply, `${method} ${path}: JSON 404 {error:"not found"}`, (j) => reply?.status === 404 && contentType(reply) === "application/json" && j?.error === "not found");
  }
}

export async function routesPhase(main: Instance) {
  const r = await readBattery(main, "full env");
  pin(r.serving, "serving: 200 with models", (j) => r.serving?.status === 200 && Array.isArray(j?.models) && j.models.length > 0);
  pin(r.facts, "facts present: 200 available with the generatedAt + revision", (j) => r.facts?.status === 200 && j?.available === true && j?.generatedAt === "2026-10-25T12:00:00.000Z" && /^[0-9a-f]{64}$/.test(j?.servingConfigRevision ?? ""));
  pin(r.facts, "facts present: timing is the facts file's rows", (j) => Array.isArray(j?.timing) && j.timing.length === 1 && j.timing[0]?.modelKey === "harness-model");
  pin(r.facts, "facts present: measured is the facts file's list", (j) => diff(j?.measured, FACTS_MEASURED) === null);
  pin(r.facts, "facts present: researchPort defaults to 4816", (j) => j?.researchPort === 4816);
  if (routeIncluded("/api/router/telemetry")) {
    const telemetry = await request(main, "GET", "/api/router/telemetry");
    check("committed telemetry: newest day file, every in-flight aged out", telemetry.json?.available === true && telemetry.json?.file === "router-2026-09-13.10.jsonl" && telemetry.json?.inFlight?.length === 0, telemetry.text.slice(0, 300));
  }
  pin(r.live, "committed serving feed (2026-10-19): typed stale", (j) => r.live?.status !== 200 || j?.available === false && j?.reason === "stale");
  await notFoundBattery(main);

  const degraded = await startApp("degraded", image, degradedSpec());
  try {
    const d = await readBattery(degraded, "degraded env");
    pin(d.serving, "degraded serving: 502 {error} on an absent LLAMA_CONFIG_PATH", (j) => d.serving?.status === 502 && typeof j?.error === "string");
    pin(d.facts, "facts absent: 200 typed {available:false, reason:absent, researchPort}", (j) => d.facts?.status === 200 && diff(j, { available: false, reason: "absent", researchPort: 4816 }) === null);
    pin(d.facts, "facts absent: no timing or measured lists", (j) => j?.timing === undefined && j?.measured === undefined);
    for (const path of ["/api/router/telemetry", "/api/router/sidecar", "/api/serving/status", "/api/serving/live"]) {
      if (!routeIncluded(path)) continue;
      const reply = await request(degraded, "GET", path);
      checkEqual(`degraded ${path}: 200 disabled`, { status: reply.status, body: reply.json }, { status: 200, body: { available: false, reason: "disabled" } });
    }
    for (const path of ["/api/serving/visibility", "/api/catalog/add", "/api/catalog/remove", "/api/catalog/consumers"]) {
      const reply = await get(degraded, "unset CONTROL_REQUESTS_DIR", "POST", path, {});
      pin(reply, `degraded POST ${path}: 503`, (j) => reply?.status === 503 && j?.error === "control plane not configured: CONTROL_REQUESTS_DIR is unset");
    }
  } finally {
    await stopApp(degraded);
  }
}

export async function spaPhase(main: Instance) {
  const sha = (b: Uint8Array) => new Bun.CryptoHasher("sha256").update(b).digest("hex");
  const view = async (path: string) => {
    if (!routeIncluded(path)) {
      skip(`spa ${path}`, "outside --routes");
      return null;
    }
    return request(main, "GET", path);
  };
  const webDir = join(work, "subject-web");
  // Web root inside the image: /web. A missing web root fails loudly here.
  await run(["docker", "cp", `${main.name}:/web`, webDir]);
  const onDisk = async (rel: string) => new Uint8Array(await Bun.file(join(webDir, rel)).arrayBuffer());
  const index = await view("/");
  if (!index) return;
  check("spa / body == subject /web/index.html", sha(index.bytes) === sha(await onDisk("index.html")), `${index.bytes.length} bytes`);
  check("spa /: html, no-cache", index.status === 200 && contentType(index) === "text/html" && index.headers.get("cache-control") === "no-cache" && index.text.includes("<html"), `${index.status} ${index.headers.get("cache-control")}`);
  const assets = [...index.text.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]!);
  check("spa index references hashed assets", assets.length > 0, index.text.slice(0, 300));
  for (const asset of assets) {
    const reply = await view(asset);
    if (reply) check(`spa ${asset}: 200 immutable`, reply.status === 200 && reply.headers.get("cache-control") === "public, max-age=31536000, immutable" && reply.bytes.length > 0, `${reply.status} ${reply.headers.get("cache-control")}`);
    if (reply) check(`spa ${asset} body == subject /web${asset}`, (await Bun.file(join(webDir, asset)).exists()) && sha(reply.bytes) === sha(await onDisk(asset.slice(1))), `${reply.bytes.length} bytes`);
  }
  // app.ts spaResponse: anything not on disk inside the web root falls back to index.html, no-cache.
  for (const path of ["/runs", "/runs/fixture-v2-full", "/serving/some-model", "/assets/missing-0000.js", "/..%2f..%2f..%2fetc%2fpasswd", "/%2e%2e%2fpackage.json", "/assets/..%2f..%2f..%2fpackage.json"]) {
    const reply = await view(path);
    if (reply) check(`spa ${path}: index fallback, no-cache (no root escape)`, reply.status === 200 && reply.headers.get("cache-control") === "no-cache" && sha(reply.bytes) === sha(index.bytes), `${reply.status} ${reply.headers.get("cache-control")} ${reply.text.slice(0, 80)}`);
  }
}
