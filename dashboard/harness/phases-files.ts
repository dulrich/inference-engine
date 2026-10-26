// File-plane phases: control (serving drop-box POSTs + the serving host status read), feeds (router telemetry / sidecar / serving live residency over clock-relative jsonl).
import { chmod, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { COMPOSE_ENV, composeMounts, FIXTURE_ROOT, FRESH, requestsBox, STALE, TODAY, writeHostFiles, type Fx } from "./fixtures.ts";
import { check, checkEqual, diff, get, image, ISO_8601, request, routeIncluded, skip, startApp, stopApp, work, type Instance, type Json } from "./lib.ts";

// ---- control -----------------------------------------------------------------------------
/** shared/src/servingRequest.ts SERVING_REQUEST_ID_PATTERN. */
const SRV_ID = /^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/;

/**
 * One POST. Response: `requestId` checked by grammar (clock + randomUUID, routes/control.ts:41-48).
 * Drop-box: exactly one new entry named `<requestId>.json` (temp file unlinked) whose
 * `request_id`/`requested_at` pass their shape checks, and whose parsed body is pinned to `expected`.
 */
/**
 * A 400 `{error}` body. routes/control.ts:136,156 and routes/servingControl.ts:86 answer
 * `{error: parsed.error.issues.map((issue) => issue.message).join("; ")}`. Per the user decision of
 * 2026-10-15 (plan Decision 4 exception) the zod DEFAULT issue messages ("Required", the min/max/int
 * texts, "Expected …, received …") are runtime text and are not compared verbatim; the body must be
 * exactly `{error: <non-empty string>}` and every domain-authored custom message the input
 * triggers (`regex_msg`/`.min(…, msg)` in shared/src/{control,servingRequest}.ts) is pinned verbatim
 * through `custom` on the subject.
 */
const BAD_REQUEST_ERROR = "<400 issue messages>";
const isBadRequestBody = (status: number, json: Json): boolean =>
  status === 400 && json !== null && typeof json === "object" && Object.keys(json).length === 1 && typeof json.error === "string" && json.error.length > 0;

async function post(target: Instance, name: string, path: string, body: unknown, expected: Record<string, unknown> | null, custom: readonly string[] = []) {
  if (!routeIncluded(path)) {
    skip(name, `${path} outside --routes`);
    return;
  }
  const box = target.requestsDir!;
  const before = new Set(await readdir(box));
  const reply = await request(target, "POST", path, body);
  const fresh = (await readdir(box)).filter((f) => !before.has(f));
  const json = reply.json ?? {};
  const id = json.requestId;
  let file: unknown = fresh;
  if (reply.status === 200 && typeof id === "string" && fresh.length === 1 && fresh[0] === `${id}.json`) {
    const parsed = JSON.parse(await readFile(join(box, fresh[0]), "utf8"));
    const at = parsed.requested_at;
    file = { shapeOk: SRV_ID.test(id) && parsed.request_id === id && typeof at === "string" && ISO_8601.test(at) };
    checkEqual(`${name}: request file body`, { ...parsed, request_id: "<id>", requested_at: "<at>" }, { ...expected, request_id: "<id>", requested_at: "<at>" });
  }
  let view: Json;
  if (isBadRequestBody(reply.status, reply.json)) {
    for (const message of custom) check(`${name}: subject 400 names "${message}"`, json.error.includes(message), json.error);
    view = { status: reply.status, body: { error: BAD_REQUEST_ERROR }, file };
  } else {
    view = { status: reply.status, body: json, file };
  }
  if (expected) check(`${name}: 200 accepted, one well-formed file`, view.status === 200 && view.body?.accepted === true && view.file?.shapeOk === true, JSON.stringify(view).slice(0, 300));
  else check(`${name}: 400 {error}, no file`, view.status === 400 && view.body?.error === BAD_REQUEST_ERROR && Array.isArray(view.file) && view.file.length === 0, JSON.stringify(view).slice(0, 300));
}

export async function controlPhase(fx: Fx) {
  const ctl = join(work, "ctl");
  const host = join(ctl, "host");
  await writeHostFiles(host, FRESH);
  await chmod(ctl, 0o755);
  const main = await startApp("control", image, {
    env: { ...COMPOSE_ENV, CONTROL_HOST_DIR: "/ctl/host" },
    mounts: [...composeMounts(fx), `${ctl}:/ctl:ro`],
    requestsDir: await requestsBox(),
  });
  try {
    const srv = { schema: "serving-request-v1", action: "set-visibility" };
    for (const [label, consumers] of [["both consumers", ["cc", "openwebui"]], ["none", []], ["reverse order kept", ["openwebui", "cc"]]] as const) {
      await post(main, `serving visibility ${label}`, "/api/serving/visibility", { modelKey: "Qwen3.6-35B-A3B-UD-Q4_K_XL", consumers }, { ...srv, model_key: "Qwen3.6-35B-A3B-UD-Q4_K_XL", consumers });
    }
    // shared/src/servingRequest.ts: the one custom message, `modelKey`'s `.min(1, …)`.
    const modelKeyMessage = "modelKey must be a non-empty llama/config.yaml model key";
    for (const [label, body, custom] of [["empty modelKey", { modelKey: "", consumers: [] }, [modelKeyMessage]], ["unknown consumer", { modelKey: "m", consumers: ["bogus"] }, []], ["consumers not array", { modelKey: "m", consumers: "cc" }, []], ["missing", {}, []], ["non-JSON", "{", []]] as const) {
      await post(main, `serving visibility ${label}`, "/api/serving/visibility", body, null, custom);
    }

    // Host status read: GET /api/serving/status (routes/servingControl.ts readServingStatus),
    // mutated host-side per step.
    const states: [string, () => Promise<void>, Json][] = [
      ["fresh", () => writeHostFiles(host, FRESH), "available"],
      ["stale", () => writeHostFiles(host, STALE), { available: false, reason: "stale" }],
      ["unparseable generated_at", () => writeHostFiles(host, "not a date"), { available: false, reason: "unreadable" }],
      ["empty", () => writeFile(join(host, "serving-status.json"), " \n"), { available: false, reason: "empty" }],
      ["malformed JSON", () => writeFile(join(host, "serving-status.json"), "{"), { available: false, reason: "unreadable" }],
      ["schema-invalid", () => writeFile(join(host, "serving-status.json"), JSON.stringify({ schema: "serving-status-v0", generated_at: FRESH })), { available: false, reason: "unreadable" }],
      ["file missing", () => rm(join(host, "serving-status.json")), { available: false, reason: "unreadable" }],
      ["host dir missing", () => rm(host, { recursive: true, force: true }), { available: false, reason: "unreadable" }],
    ];
    for (const [label, apply, servingExpect] of states) {
      await apply();
      if (label !== "host dir missing") await chmod(host, 0o755);
      const status = await get(main, `host ${label}: serving status`, "GET", "/api/serving/status");
      if (status) check(`host ${label}: serving status verdict`, servingExpect === "available" ? status.json?.available === true && status.json?.schema === "serving-status-v1" : diff(status.json, servingExpect) === null, status.text.slice(0, 200));
    }
  } finally {
    await stopApp(main);
  }
}

// ---- feeds -------------------------------------------------------------------------------
/** Parseable lines of a committed jsonl (the .10 fixture carries one deliberately truncated line). */
const lines = async (path: string): Promise<Json[]> => (await readFile(path, "utf8")).split("\n").flatMap((l) => {
  try {
    return [JSON.parse(l)];
  } catch {
    return [];
  }
});
const jsonl = (records: unknown[]) => `${records.map((r) => JSON.stringify(r)).join("\n")}\n`;

export async function feedsPhase() {
  const feeds = join(work, "feeds");
  const router = join(feeds, "router");
  const sidecar = join(feeds, "sidecar");
  const serving = join(feeds, "serving");
  const committed = join(FIXTURE_ROOT, "router");
  const routerLines = await lines(join(committed, "logs/router-2026-09-13.10.jsonl"));
  const requestTpl = routerLines.find((l: Json) => l.kind === "request");
  const progressTpl = routerLines.find((l: Json) => l.kind === "progress");
  const servingLines = await lines(join(FIXTURE_ROOT, "serving-feed/serving-feed-2026-10-19.jsonl"));
  const gpuTpl = servingLines.find((l: Json) => l.kind === "snapshot" && l.tier === "gpu");
  const onnxTpl = servingLines.find((l: Json) => l.kind === "snapshot" && l.tier === "onnx");
  const loadTpl = servingLines.find((l: Json) => l.kind === "load");
  const sidecarLines = await lines(join(committed, "sidecar/codex-sidecar-2026-09-13.jsonl"));
  const progress = (reqId: string, ts: string, elapsedMs: number, tokens: number) => ({ ...progressTpl, reqId, ts, elapsedMs, responseTokens: tokens, reasoningTokens: tokens });
  const telemetryFile = join(router, `router-${TODAY}.10.jsonl`);
  const servingFile = join(serving, `serving-feed-${TODAY}.jsonl`);
  const sidecarFile = join(sidecar, `codex-sidecar-${TODAY}.jsonl`);
  const MiB = 1024 * 1024;
  /** Records with distinct ids until the byte size passes `bytes`. */
  const pad = (make: (i: number) => unknown, bytes: number) => {
    const out: unknown[] = [];
    let size = 0;
    for (let i = 0; size < bytes; i += 1) {
      const r = make(i);
      out.push(r);
      size += JSON.stringify(r).length + 1;
    }
    return out;
  };
  /** Live state: each jsonl past its tail window (router/tailReader.ts: 1 MiB telemetry/sidecar,
   * and the serving feed's 1 MiB), with a fresh-looking record before the window that must never be read. */
  const writeLive = async (progressTs: string, sampledAt: string) => {
    await mkdir(router, { recursive: true });
    await mkdir(sidecar, { recursive: true });
    await mkdir(serving, { recursive: true });
    await writeFile(join(router, "router-2026-01-01.99.jsonl"), jsonl([{ ...requestTpl, reqId: "older-day" }]));
    await writeFile(join(router, `router-${TODAY}.2.jsonl`), jsonl([{ ...requestTpl, reqId: "lower-index" }]));
    await writeFile(join(router, "notes.txt"), "not a sink file\n");
    await writeFile(telemetryFile, jsonl([
      progress("pre-window-inflight", FRESH, 100, 1),
      ...pad((i) => ({ ...requestTpl, reqId: `pad-${i}`, ts: STALE, leg: i % 3 === 0 ? "anthropic" : "claude-local", ttftMs: 100 + (i % 17) }), 1.2 * MiB),
      ...routerLines.filter((l: Json) => l.kind === "request"),
      "not json at all",
      progress("live-1", progressTs, 1000, 10), progress("live-1", progressTs, 3000, 50),
      progress("live-2", progressTs, 500, 3),
      progress("abandoned", STALE, 9000, 99),
      progress("bad-ts", "yesterday-ish", 100, 1),
    ]));
    await writeFile(join(serving, "serving-feed-2026-01-01.jsonl"), jsonl([{ ...gpuTpl, ts: FRESH }]));
    await writeFile(servingFile, jsonl([
      { ...gpuTpl, ts: FRESH, models: [{ model: "pre-window", state: "ready", ttl: 1 }] },
      ...pad((i) => ({ ...(i % 2 ? onnxTpl : gpuTpl), ts: STALE }), 1.1 * MiB),
      { ...gpuTpl, ts: sampledAt },
      { ...onnxTpl, ts: sampledAt },
      { ...loadTpl, ts: sampledAt },
    ]));
    await writeFile(sidecarFile, jsonl([
      { ...sidecarLines[0], persona: "pre-window-persona" },
      ...pad((i) => ({ ...sidecarLines[i % sidecarLines.length], persona: i % 2 ? "codex-b" : "codex-a" }), 1.1 * MiB),
      ...sidecarLines,
    ]));
  };
  await writeLive(FRESH, FRESH);
  await chmod(feeds, 0o755);
  const main = await startApp("feeds", image, { env: { ...COMPOSE_ENV, ROUTER_TELEMETRY_DIR: "/feeds/router", CODEX_SIDECAR_LOG_DIR: "/feeds/sidecar", SERVING_FEED_DIR: "/feeds/serving" }, mounts: [`${feeds}:/feeds:ro`] });
  try {
    const read = async (label: string) => {
      const t = await get(main, `${label}: telemetry`, "GET", "/api/router/telemetry");
      await get(main, `${label}: telemetry ?staleMs=600000`, "GET", "/api/router/telemetry?staleMs=600000");
      const s = await get(main, `${label}: serving live`, "GET", "/api/serving/live");
      const c = await get(main, `${label}: sidecar`, "GET", "/api/router/sidecar");
      return { t: t?.json, s: s?.json, c: c?.json, ran: Boolean(t && s && c) };
    };
    const unavailable = (r: Json, reason: string) => r?.available === false && r?.reason === reason && Object.keys(r).length === 2;
    let r = await read("live");
    if (r.ran) {
      check("live telemetry: today's highest index, 1 MiB window, recent capped at 200", r.t?.file === `router-${TODAY}.10.jsonl` && r.t?.windowBytes === MiB && r.t?.recent?.length === 200, JSON.stringify({ file: r.t?.file, w: r.t?.windowBytes, n: r.t?.recent?.length }));
      checkEqual("live telemetry: in-flight = the fresh progress-only ids (pre-window line never read)", r.t?.inFlight?.map((x: Json) => x.reqId).sort(), ["live-1", "live-2"]);
      check("live telemetry: tokPerSec from the last two samples", r.t?.inFlight?.find((x: Json) => x.reqId === "live-1")?.tokPerSec === 40, JSON.stringify(r.t?.inFlight).slice(0, 300));
      check("live serving: today's file, newest snapshot per tier, pre-window line never read", r.s?.available === true && r.s?.file === `serving-feed-${TODAY}.jsonl` && r.s?.gpu?.sampled_at === FRESH && r.s?.gpu?.models?.length > 0 && r.s?.onnx?.available === true && !JSON.stringify(r.s).includes("pre-window"), JSON.stringify(r.s).slice(0, 300));
      check("live serving: the load event is recent", r.s?.recent?.[0]?.kind === "load" && r.s?.recent?.[0]?.ts === FRESH, JSON.stringify(r.s?.recent).slice(0, 300));
      check("live sidecar: pre-window persona never read", r.c?.available === true && JSON.stringify(r.c).includes("codex-b") && !JSON.stringify(r.c).includes("pre-window-persona"), JSON.stringify(r.c).slice(0, 300));
    }
    await writeLive(STALE, STALE);
    r = await read("stale");
    if (r.ran) {
      check("stale telemetry: every in-flight aged out", r.t?.available === true && r.t?.inFlight?.length === 0, JSON.stringify(r.t?.inFlight));
      check("stale serving: typed stale", unavailable(r.s, "stale"), JSON.stringify(r.s).slice(0, 200));
    }
    // Only the pre-window line is a record: a whole-file read would find it, the window must not.
    await writeFile(telemetryFile, `${JSON.stringify(progress("pre-window-inflight", FRESH, 1, 1))}\n${"x".repeat(80)}\n`.padEnd(1.2 * MiB, "y\n"));
    await writeFile(servingFile, `${JSON.stringify({ ...gpuTpl, ts: FRESH })}\n`.padEnd(1.1 * MiB, "z\n"));
    await writeFile(sidecarFile, `${JSON.stringify(sidecarLines[0])}\n`.padEnd(1.1 * MiB, "w\n"));
    r = await read("records only before the window");
    if (r.ran) check("window-bounded: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    for (const f of [telemetryFile, servingFile, sidecarFile]) await writeFile(f, "");
    r = await read("zero-byte files");
    if (r.ran) check("zero-byte: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    for (const f of [telemetryFile, servingFile, sidecarFile]) await writeFile(f, "{bad\n[1,2]\n{\"kind\":\"request\"}\n");
    r = await read("schema-invalid lines");
    if (r.ran) check("invalid lines: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    await rm(router, { recursive: true });
    await rm(sidecar, { recursive: true });
    await rm(serving, { recursive: true });
    await mkdir(router);
    await mkdir(sidecar);
    await mkdir(serving);
    await writeFile(join(router, "notes.txt"), "x\n");
    await writeFile(join(router, "router-today.jsonl"), "{}\n");
    await writeFile(join(serving, "serving-feed-today.jsonl"), "{}\n");
    for (const d of [router, sidecar, serving]) await chmod(d, 0o755);
    r = await read("no matching file names");
    if (r.ran) check("no sink file: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    await writeLive(FRESH, FRESH);
    for (const f of [telemetryFile, servingFile, sidecarFile]) await chmod(f, 0o000);
    r = await read("selected file unreadable");
    if (r.ran) check("unreadable file: all three unreadable", unavailable(r.t, "unreadable") && unavailable(r.s, "unreadable") && unavailable(r.c, "unreadable"), JSON.stringify(r));
    for (const f of [telemetryFile, servingFile, sidecarFile]) await chmod(f, 0o644);
    await rm(router, { recursive: true });
    await rm(sidecar, { recursive: true });
    await rm(serving, { recursive: true });
    r = await read("dirs missing");
    if (r.ran) check("missing dir: all three unreadable", unavailable(r.t, "unreadable") && unavailable(r.s, "unreadable") && unavailable(r.c, "unreadable"), JSON.stringify(r));
  } finally {
    await stopApp(main);
  }
}
