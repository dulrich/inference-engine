// File-plane phases: ingest (through the command seam), control (drop-box POSTs + host status
// reads), feeds (router telemetry / slots / sidecar over clock-relative jsonl).
import { chmod, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { contentOf, dumpOf, ISO_8601, normalizeResponse } from "./dump.ts";
import { COMPOSE_ENV, composeMounts, composeSpec, FIXTURE_ROOT, FRESH, requestsBox, STALE, TODAY, writeHostFiles, type Fx } from "./fixtures.ts";
import { both, check, checkEqual, contentType, diff, execIngest, request, routeIncluded, skip, startPair, stopPair, work, type Json, type Pair } from "./lib.ts";

// ---- ingest ------------------------------------------------------------------------------
const INVALID_PREFIX = "[ingest] invalid: ";
/**
 * The comparable form of one ingest run: exit code, stdout (the final JSON summary line, ingest/
 * cli.ts), and the `[ingest] invalid: <dir> (<reason>)` stderr lines. A reason that is a JSON parse
 * failure (`config.json: ${error.message}`, ingest/record.ts:250 and :374) carries the engine's
 * JSON.parse text, so only its `config.json: ` prefix is compared; every other reason is exact.
 * Non-zero exits compare by code only (Bun prints an uncaught-error trace; the message is pinned
 * separately).
 */
function ingestShape(result: { code: number; out: string; err: string }) {
  if (result.code !== 0) return { code: result.code === 0 ? 0 : "nonzero" };
  const invalid = result.err.split("\n").filter((l) => l.startsWith(INVALID_PREFIX)).map((l) => {
    const m = /^(\[ingest\] invalid: \S+) \((.*)\)$/.exec(l);
    if (!m) return l;
    const parseError = m[2]!.startsWith("config.json: ") && !m[2]!.startsWith("config.json: wilson_summary");
    return `${m[1]} (${parseError ? "config.json: <JSON.parse message>" : m[2]})`;
  });
  return { code: 0, stdout: result.out.split("\n"), invalid };
}

export async function ingestPhase(fx: Fx) {
  const pair = await startPair("ingest", composeSpec(fx));
  try {
    const step = async (label: string, args: string[]) => {
      const [s, r] = [await execIngest(pair[0], args), await execIngest(pair[1], args)];
      const [ss, rs] = [ingestShape(s), ingestShape(r)];
      const d = diff(ss, rs);
      check(`ingest ${label}: exit, stdout and invalid lines match reference`, d === null, d ?? "");
      let summary: Json = null;
      try {
        summary = s.code === 0 ? JSON.parse(s.out.split("\n").at(-1) ?? "") : null;
      } catch {
        summary = null;
      }
      const [ds, dr] = [await dumpOf(pair[0].dataDir), await dumpOf(pair[1].dataDir)];
      checkEqual(`ingest ${label}: subject dump equals reference`, ds, dr);
      return { s, summary, dump: ds };
    };
    const first = await step("fresh", []);
    const sum = first.summary;
    check("fresh: one stdout line, the JSON summary", first.s.out.split("\n").length === 1 && sum !== null, first.s.out.slice(0, 300));
    check("fresh: summary counts", sum?.scanned > 0 && sum?.ingested > 0 && sum?.skippedV1 >= 1 && sum?.skippedInvalid === 2 && sum?.legacyTimingIngested >= 1 && sum?.rebuilt === false, JSON.stringify(sum));
    const invalidLines = first.s.err.split("\n").filter((l) => l.startsWith(INVALID_PREFIX));
    check("fresh: invalid lines for invalid-json + invalid-array", invalidLines.length === 2 && invalidLines.some((l) => l.startsWith(`${INVALID_PREFIX}/records/invalid-json (config.json: `))
      && invalidLines.includes(`${INVALID_PREFIX}/records/invalid-array (config.json is not a JSON object)`), invalidLines.join(" | "));
    const health = await request(pair[0], "GET", "/health");
    check("fresh: /health experimentCount = ingested + legacyTimingIngested", health.json?.experimentCount === sum?.ingested + sum?.legacyTimingIngested, health.text);
    check("fresh: v1-timing provenance written", (first.dump.tables.experiments ?? []).some((row) => row.schema_origin === "v1-timing"), "");
    const again = await step("re-run", []);
    checkEqual("re-run: same summary", again.summary, sum);
    checkEqual("re-run: idempotent content", contentOf(again.dump), contentOf(first.dump));
    const rebuilt = await step("--rebuild", ["--rebuild"]);
    checkEqual("--rebuild: summary with rebuilt true", rebuilt.summary, { ...sum, rebuilt: true });
    checkEqual("--rebuild: same content", contentOf(rebuilt.dump), contentOf(first.dump));
    const dir = await step("--dir /records2", ["--dir", "/records2"]);
    // records-two-level: batch-run/cell-{a,b} + flat-record (v1 configs), neither-dir + nested-too-deep invalid.
    checkEqual("--dir: two-level batch layout scanned", dir.summary, { scanned: 5, ingested: 0, skippedV1: 3, skippedInvalid: 2, legacyTimingIngested: 0, rebuilt: false });
    const eq = await step("--dir=/records2", ["--dir=/records2"]);
    checkEqual("--dir=: same summary as --dir", eq.summary, dir.summary);
    checkEqual("--dir=: same content as --dir", contentOf(eq.dump), contentOf(dir.dump));
    const both2 = await step("--rebuild --dir=/records2", ["--rebuild", "--dir=/records2"]);
    check("--rebuild --dir: only the two-level set remains", (both2.dump.tables.experiments ?? []).length === both2.summary?.ingested + both2.summary?.legacyTimingIngested, JSON.stringify(both2.summary));
    for (const [args, message] of [[["--bogus"], "unknown argument: --bogus"], [["--dir"], "--dir requires a value"], [["--rebuild", "-x"], "unknown argument: -x"], [["--dir", "/absent"], ""]] as const) {
      const result = await step(`error ${args.join(" ")}`, [...args]);
      check(`error ${args.join(" ")}: non-zero exit${message ? `, stderr names "${message}"` : ""}`, result.s.code !== 0 && result.s.err.includes(message), `${result.s.code}: ${result.s.err.slice(0, 200)}`);
    }
  } finally {
    await stopPair(pair);
  }
}

// ---- control -----------------------------------------------------------------------------
/** shared/src/control.ts CONTROL_REQUEST_ID_PATTERN, shared/src/servingRequest.ts SERVING_REQUEST_ID_PATTERN. */
const ID_PATTERN = { req: /^req-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/, srv: /^srv-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}$/ };

/**
 * One POST against each twin. Response: `requestId` compared by grammar (clock + randomUUID,
 * routes/control.ts:41-48), everything else exact. Drop-box: exactly one new entry named
 * `<requestId>.json` (temp file unlinked), its bytes compared with `request_id`/`requested_at`
 * replaced by placeholders after their shape checks, and its parsed body pinned to `expected`.
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

async function post(pair: Pair, name: string, path: string, body: unknown, expected: Record<string, unknown> | null, custom: readonly string[] = []) {
  if (!routeIncluded(path)) {
    skip(name, `${path} outside --routes`);
    return;
  }
  const kind = path === "/api/serving/visibility" ? "srv" : "req";
  const views: unknown[] = [];
  for (const twin of pair) {
    const box = twin.requestsDir!;
    const before = new Set(await readdir(box));
    const reply = await request(twin, "POST", path, body);
    const fresh = (await readdir(box)).filter((f) => !before.has(f));
    const json = reply.json ?? {};
    const id = json.requestId;
    let file: unknown = fresh;
    if (reply.status === 200 && typeof id === "string" && fresh.length === 1 && fresh[0] === `${id}.json`) {
      const text = await readFile(join(box, fresh[0]), "utf8");
      const parsed = JSON.parse(text);
      const at = parsed.requested_at;
      const shapeOk = ID_PATTERN[kind].test(id) && parsed.request_id === id && typeof at === "string" && ISO_8601.test(at);
      file = { shapeOk, bytes: text.replace(JSON.stringify(id), '"<request_id>"').replace(JSON.stringify(at), '"<requested_at>"') };
      if (twin === pair[0]) checkEqual(`${name}: request file body`, { ...parsed, request_id: "<id>", requested_at: "<at>" }, { ...expected, request_id: "<id>", requested_at: "<at>" });
    }
    if (isBadRequestBody(reply.status, reply.json)) {
      for (const message of custom) check(`${name}: ${twin === pair[0] ? "subject" : "reference"} 400 names "${message}"`, json.error.includes(message), json.error);
      views.push({ status: reply.status, type: contentType(reply), body: { error: BAD_REQUEST_ERROR }, file });
      continue;
    }
    views.push({ status: reply.status, type: contentType(reply), body: normalizeResponse({ ...json, ...(typeof id === "string" ? { requestId: ID_PATTERN[kind].test(id) ? "<id>" : `bad:${id}` } : {}) }), file });
  }
  const d = diff(views[0], views[1]);
  check(`${name} [POST ${path}] response + drop-box match reference`, d === null, d ?? "");
  const v = views[0] as Json;
  if (expected) check(`${name}: 200 accepted, one well-formed file`, v.status === 200 && v.body?.accepted === true && v.file?.shapeOk === true, JSON.stringify(v).slice(0, 300));
  else check(`${name}: 400 {error}, no file`, v.status === 400 && v.body?.error === BAD_REQUEST_ERROR && Array.isArray(v.file) && v.file.length === 0, JSON.stringify(v).slice(0, 300));
}

export async function controlPhase(fx: Fx) {
  const ctl = join(work, "ctl");
  const host = join(ctl, "host");
  await writeHostFiles(host, FRESH);
  await chmod(ctl, 0o755);
  const pair = await startPair("control", async () => ({
    env: { ...COMPOSE_ENV, CONTROL_HOST_DIR: "/ctl/host" },
    mounts: [...composeMounts(fx), `${ctl}:/ctl:ro`],
    requestsDir: await requestsBox(),
  }));
  try {
    const schema = { schema: "control-request-v1" };
    for (const action of ["arm", "disarm", "stop"]) await post(pair, `control ${action}`, `/api/control/${action}`, undefined, { ...schema, action });
    for (const [label, body, minutes] of [["valid", { queueStem: "contract-queue", minutes: 60 }, 60], ["min 15", { queueStem: "a", minutes: 15 }, 15], ["max 480", { queueStem: "q-1", minutes: 480 }, 480], ["extra key stripped", { queueStem: "contract-queue", minutes: 90, extra: true }, 90]] as const) {
      await post(pair, `control start ${label}`, "/api/control/start", body, { ...schema, action: "start", queue_stem: body.queueStem, minutes });
    }
    // Third element: the domain-authored messages each input triggers (shared/src/control.ts).
    const stemMessage = "queueStem must match ^[a-z0-9][a-z0-9-]*$";
    for (const [label, body, custom] of [["minutes 14", { queueStem: "q", minutes: 14 }, []], ["minutes 481", { queueStem: "q", minutes: 481 }, []], ["minutes 60.5", { queueStem: "q", minutes: 60.5 }, []], ["minutes string", { queueStem: "q", minutes: "60" }, []], ["stem upper", { queueStem: "Bad_Stem", minutes: 60 }, [stemMessage]], ["stem leading dash", { queueStem: "-q", minutes: 60 }, [stemMessage]], ["missing body", {}, []], ["non-JSON body", "not json", []], ["both invalid", { queueStem: "", minutes: 0 }, [stemMessage]]] as const) {
      await post(pair, `control start ${label}`, "/api/control/start", body, null, custom);
    }
    const target = "req-20260827T120000Z-00000000000000a1";
    await post(pair, "control cancel valid", "/api/control/cancel", { targetRequestId: target }, { ...schema, action: "cancel", target_request_id: target });
    const targetMessage = "targetRequestId must match the request-id grammar";
    for (const [label, body, custom] of [["nightly target", { targetRequestId: "nightly-20260827" }, [targetMessage]], ["uppercase hex", { targetRequestId: "req-20260827T120000Z-00000000000000A1" }, [targetMessage]], ["missing", {}, []]] as const) {
      await post(pair, `control cancel ${label}`, "/api/control/cancel", body, null, custom);
    }
    const srv = { schema: "serving-request-v1", action: "set-visibility" };
    for (const [label, consumers] of [["both consumers", ["cc", "openwebui"]], ["none", []], ["reverse order kept", ["openwebui", "cc"]]] as const) {
      await post(pair, `serving visibility ${label}`, "/api/serving/visibility", { modelKey: "Qwen3.6-35B-A3B-UD-Q4_K_XL", consumers }, { ...srv, model_key: "Qwen3.6-35B-A3B-UD-Q4_K_XL", consumers });
    }
    // shared/src/servingRequest.ts: the one custom message, `modelKey`'s `.min(1, …)`.
    const modelKeyMessage = "modelKey must be a non-empty llama/config.yaml model key";
    for (const [label, body, custom] of [["empty modelKey", { modelKey: "", consumers: [] }, [modelKeyMessage]], ["unknown consumer", { modelKey: "m", consumers: ["bogus"] }, []], ["consumers not array", { modelKey: "m", consumers: "cc" }, []], ["missing", {}, []], ["non-JSON", "{", []]] as const) {
      await post(pair, `serving visibility ${label}`, "/api/serving/visibility", body, null, custom);
    }

    // Host status reads: GET /api/serving/status (routes/servingControl.ts readServingStatus) and
    // the queue control section (routes/queue.ts buildControlSection), mutated host-side per step.
    const states: [string, () => Promise<void>, Json, (control: Json) => boolean][] = [
      ["fresh", () => writeHostFiles(host, FRESH), "available", (c) => c?.stale === false && c?.armed === true],
      ["stale", () => writeHostFiles(host, STALE), { available: false, reason: "stale" }, (c) => c?.stale === true],
      ["unparseable generated_at", () => writeHostFiles(host, "not a date"), { available: false, reason: "unreadable" }, (c) => c?.stale === true],
      ["empty", async () => {
        await writeFile(join(host, "serving-status.json"), " \n");
        await writeFile(join(host, "status.json"), "");
      }, { available: false, reason: "empty" }, (c) => c === null],
      ["malformed JSON", async () => {
        await writeFile(join(host, "serving-status.json"), "{");
        await writeFile(join(host, "status.json"), await readFile(join(FIXTURE_ROOT, "control/malformed-json/status.json")));
      }, { available: false, reason: "unreadable" }, (c) => c === null],
      ["schema-invalid", async () => {
        await writeFile(join(host, "serving-status.json"), JSON.stringify({ schema: "serving-status-v0", generated_at: FRESH }));
        await writeFile(join(host, "status.json"), await readFile(join(FIXTURE_ROOT, "control/schema-invalid/status.json")));
      }, { available: false, reason: "unreadable" }, (c) => c === null],
      ["files missing", async () => {
        await rm(join(host, "serving-status.json"));
        await rm(join(host, "status.json"));
      }, { available: false, reason: "unreadable" }, (c) => c === null],
      ["host dir missing", () => rm(host, { recursive: true, force: true }), { available: false, reason: "unreadable" }, (c) => c === null],
    ];
    for (const [label, apply, servingExpect, controlOk] of states) {
      await apply();
      if (label !== "host dir missing") await chmod(host, 0o755);
      const status = await both(pair, `host ${label}: serving status`, "GET", "/api/serving/status");
      if (status) check(`host ${label}: serving status verdict`, servingExpect === "available" ? status.json?.available === true && status.json?.schema === "serving-status-v1" : diff(status.json, servingExpect) === null, status.text.slice(0, 200));
      const queue = await both(pair, `host ${label}: queue control`, "GET", "/api/queue");
      if (queue) check(`host ${label}: queue control section`, controlOk(queue.json?.control), JSON.stringify(queue.json?.control).slice(0, 200));
    }
  } finally {
    await stopPair(pair);
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
  const committed = join(FIXTURE_ROOT, "router");
  const routerLines = await lines(join(committed, "logs/router-2026-09-13.10.jsonl"));
  const requestTpl = routerLines.find((l: Json) => l.kind === "request");
  const progressTpl = routerLines.find((l: Json) => l.kind === "progress");
  const slotsTpl = (await lines(join(committed, "logs/slots-2026-09-13.jsonl"))).at(-1);
  const sidecarLines = await lines(join(committed, "sidecar/codex-sidecar-2026-09-13.jsonl"));
  const progress = (reqId: string, ts: string, elapsedMs: number, tokens: number) => ({ ...progressTpl, reqId, ts, elapsedMs, responseTokens: tokens, reasoningTokens: tokens });
  const telemetryFile = join(router, `router-${TODAY}.10.jsonl`);
  const slotsFile = join(router, `slots-${TODAY}.jsonl`);
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
   * 256 KiB slots), with a fresh-looking record before the window that must never be read. */
  const writeLive = async (progressTs: string, sampledAt: string) => {
    await mkdir(router, { recursive: true });
    await mkdir(sidecar, { recursive: true });
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
    await writeFile(slotsFile, jsonl([
      { ...slotsTpl, sampledAt: FRESH, ts: FRESH, models: [] },
      ...pad((i) => ({ ...slotsTpl, sampledAt: STALE, ts: STALE, models: slotsTpl.models.slice(i % 2) }), 300 * 1024),
      { ...slotsTpl, sampledAt, ts: sampledAt },
    ]));
    await writeFile(sidecarFile, jsonl([
      { ...sidecarLines[0], persona: "pre-window-persona" },
      ...pad((i) => ({ ...sidecarLines[i % sidecarLines.length], persona: i % 2 ? "codex-b" : "codex-a" }), 1.1 * MiB),
      ...sidecarLines,
    ]));
  };
  await writeLive(FRESH, FRESH);
  await chmod(feeds, 0o755);
  const pair = await startPair("feeds", () => ({ env: { ...COMPOSE_ENV, ROUTER_TELEMETRY_DIR: "/feeds/router", CODEX_SIDECAR_LOG_DIR: "/feeds/sidecar" }, mounts: [`${feeds}:/feeds:ro`] }));
  try {
    const read = async (label: string) => {
      const t = await both(pair, `${label}: telemetry`, "GET", "/api/router/telemetry");
      await both(pair, `${label}: telemetry ?staleMs=600000`, "GET", "/api/router/telemetry?staleMs=600000");
      const s = await both(pair, `${label}: slots`, "GET", "/api/router/slots");
      await both(pair, `${label}: slots ?staleMs=1000`, "GET", "/api/router/slots?staleMs=1000");
      const c = await both(pair, `${label}: sidecar`, "GET", "/api/router/sidecar");
      return { t: t?.json, s: s?.json, c: c?.json, ran: Boolean(t && s && c) };
    };
    const unavailable = (r: Json, reason: string) => r?.available === false && r?.reason === reason && Object.keys(r).length === 2;
    let r = await read("live");
    if (r.ran) {
      check("live telemetry: today's highest index, 1 MiB window, recent capped at 200", r.t?.file === `router-${TODAY}.10.jsonl` && r.t?.windowBytes === MiB && r.t?.recent?.length === 200, JSON.stringify({ file: r.t?.file, w: r.t?.windowBytes, n: r.t?.recent?.length }));
      checkEqual("live telemetry: in-flight = the fresh progress-only ids (pre-window line never read)", r.t?.inFlight?.map((x: Json) => x.reqId).sort(), ["live-1", "live-2"]);
      check("live telemetry: tokPerSec from the last two samples", r.t?.inFlight?.find((x: Json) => x.reqId === "live-1")?.tokPerSec === 40, JSON.stringify(r.t?.inFlight).slice(0, 300));
      check("live slots: newest record, not stale", r.s?.available === true && r.s?.stale === false && r.s?.sampledAt === FRESH && Array.isArray(r.s?.models) && r.s.models.length > 0, JSON.stringify(r.s).slice(0, 300));
      check("live sidecar: pre-window persona never read", r.c?.available === true && JSON.stringify(r.c).includes("codex-b") && !JSON.stringify(r.c).includes("pre-window-persona"), JSON.stringify(r.c).slice(0, 300));
    }
    await writeLive(STALE, STALE);
    r = await read("stale");
    if (r.ran) {
      check("stale telemetry: every in-flight aged out", r.t?.available === true && r.t?.inFlight?.length === 0, JSON.stringify(r.t?.inFlight));
      check("stale slots: stale", r.s?.available === true && r.s?.stale === true, JSON.stringify(r.s).slice(0, 200));
    }
    // Only the pre-window line is a record: a whole-file read would find it, the window must not.
    await writeFile(telemetryFile, `${JSON.stringify(progress("pre-window-inflight", FRESH, 1, 1))}\n${"x".repeat(80)}\n`.padEnd(1.2 * MiB, "y\n"));
    await writeFile(slotsFile, `${JSON.stringify({ ...slotsTpl, sampledAt: FRESH })}\n`.padEnd(300 * 1024, "z\n"));
    await writeFile(sidecarFile, `${JSON.stringify(sidecarLines[0])}\n`.padEnd(1.1 * MiB, "w\n"));
    r = await read("records only before the window");
    if (r.ran) check("window-bounded: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    for (const f of [telemetryFile, slotsFile, sidecarFile]) await writeFile(f, "");
    r = await read("zero-byte files");
    if (r.ran) check("zero-byte: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    for (const f of [telemetryFile, slotsFile, sidecarFile]) await writeFile(f, "{bad\n[1,2]\n{\"kind\":\"request\"}\n");
    r = await read("schema-invalid lines");
    if (r.ran) check("invalid lines: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    await rm(router, { recursive: true });
    await rm(sidecar, { recursive: true });
    await mkdir(router);
    await mkdir(sidecar);
    await writeFile(join(router, "notes.txt"), "x\n");
    await writeFile(join(router, "router-today.jsonl"), "{}\n");
    for (const d of [router, sidecar]) await chmod(d, 0o755);
    r = await read("no matching file names");
    if (r.ran) check("no sink file: all three empty", unavailable(r.t, "empty") && unavailable(r.s, "empty") && unavailable(r.c, "empty"), JSON.stringify(r));
    await writeLive(FRESH, FRESH);
    for (const f of [telemetryFile, slotsFile, sidecarFile]) await chmod(f, 0o000);
    r = await read("selected file unreadable");
    if (r.ran) check("unreadable file: all three unreadable", unavailable(r.t, "unreadable") && unavailable(r.s, "unreadable") && unavailable(r.c, "unreadable"), JSON.stringify(r));
    for (const f of [telemetryFile, slotsFile, sidecarFile]) await chmod(f, 0o644);
    await rm(router, { recursive: true });
    await rm(sidecar, { recursive: true });
    r = await read("dirs missing");
    if (r.ran) check("missing dir: all three unreadable", unavailable(r.t, "unreadable") && unavailable(r.s, "unreadable") && unavailable(r.c, "unreadable"), JSON.stringify(r));
  } finally {
    await stopPair(pair);
  }
}
