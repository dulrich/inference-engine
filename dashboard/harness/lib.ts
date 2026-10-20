// Shared plumbing for the bench-dashboard black-box harness (plans/bench-dashboard-rust-port.md
// WP-1): CLI, PASS/FAIL/SKIP accounting, HTTP client, docker lifecycle with unconditional per-run
// cleanup, and the ingest command seam (Decision 3). Talks to images only over HTTP on a throwaway
// docker network (no host port publish), env, bind-mounted fixture files, `docker exec` of the
// ingest CLI, and the files left in /data and the drop-box. Never imports API code.
import { chmod, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { normalizeResponse } from "./dump.ts";

export const ALL_PHASES = ["routes", "ingest", "control", "feeds", "spa"];
/** apps/api/src/config.ts `port: Number(process.env.PORT ?? 4810)`; the compose block sets 4810. */
export const PORT = 4810;

const args = process.argv.slice(2);
function argValue(flag: string): string | undefined {
  const index = args.indexOf(flag);
  return index >= 0 ? args[index + 1] : undefined;
}
function fail(message: string): never {
  console.error(`harness: ${message}`);
  process.exit(2);
}
export const image = argValue("--image") ?? "inference-dashboard:dev";
export const phases = (argValue("--phases") ?? ALL_PHASES.join(",")).split(",").filter(Boolean);
for (const phase of phases) if (!ALL_PHASES.includes(phase)) fail(`unknown phase ${phase}`);
export const wants = (...names: string[]) => names.some((name) => phases.includes(name));
/**
 * --subject-db fresh|golden: `fresh` (default) ingests the fixture records through each twin's own
 * ingest CLI; `golden` serves the subject (and its reference twin) a copy of the /data tree the
 * --image built by the same ingest, so a partial port can be gated on reads. The ingest phase
 * always runs its own fresh pair (it is the thing under test there).
 */
export const subjectDb = argValue("--subject-db") ?? "fresh";
if (subjectDb !== "fresh" && subjectDb !== "golden") fail(`--subject-db must be fresh or golden, got ${subjectDb}`);
/** --routes <prefixes>: path-prefix subset (`<path>$` = exact path); out-of-subset checks count as skipped. */
export const routePrefixes = argValue("--routes")?.split(",").filter(Boolean);
export function routeIncluded(path: string): boolean {
  if (!routePrefixes) return true;
  const bare = path.split("?")[0] ?? path;
  return routePrefixes.some((p) => p.endsWith("$") ? bare === p.slice(0, -1)
    : bare === p || (bare.startsWith(p) && (p.endsWith("/") || bare[p.length] === "/")));
}

export const prefix = `bd-harness-${Math.random().toString(36).slice(2, 10)}`;
export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- assertions --------------------------------------------------------------------------
export const counts = { passed: 0, failed: 0, skipped: 0 };
const perPhase = new Map<string, { passed: number; failed: number; skipped: number }>();
let currentPhase = "setup";
export const setPhase = (phase: string) => {
  currentPhase = phase;
  console.log(`--- phase ${phase}`);
};
const tally = () => {
  const t = perPhase.get(currentPhase) ?? { passed: 0, failed: 0, skipped: 0 };
  perPhase.set(currentPhase, t);
  return t;
};
export function check(name: string, ok: boolean, detail = "") {
  const t = tally();
  if (ok) {
    counts.passed += 1;
    t.passed += 1;
  } else {
    counts.failed += 1;
    t.failed += 1;
  }
  console.log(`${ok ? "PASS" : "FAIL"} ${currentPhase}: ${name}${!ok && detail ? ` -- ${detail.slice(0, 600)}` : ""}`);
}
export function skip(name: string, reason: string) {
  counts.skipped += 1;
  tally().skipped += 1;
  console.log(`SKIP ${currentPhase}: ${name} (${reason})`);
}
export const phaseSummary = () =>
  [...perPhase].map(([phase, t]) => `${phase} ${t.passed}/${t.passed + t.failed}${t.skipped ? ` (${t.skipped} skipped)` : ""}`).join(", ");
export const idlePhases = () => phases.filter((phase) => {
  const t = perPhase.get(phase);
  return !t || t.passed + t.failed === 0;
});

export function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) out[key] = sortKeys((value as Record<string, unknown>)[key]);
    return out;
  }
  return value;
}
export const canonical = (value: unknown) => JSON.stringify(sortKeys(value), null, 1);
export function diff(actual: unknown, expected: unknown): string | null {
  const a = canonical(actual);
  const e = canonical(expected);
  if (a === e) return null;
  const al = a.split("\n");
  const el = e.split("\n");
  let i = 0;
  while (i < al.length && al[i] === el[i]) i += 1;
  return `first diff at line ${i}: got ${al[i] ?? "<eof>"} | want ${el[i] ?? "<eof>"}`;
}
export function checkEqual(name: string, actual: unknown, expected: unknown) {
  const d = diff(actual, expected);
  check(name, d === null, d ?? "");
}

// ---- HTTP --------------------------------------------------------------------------------
// oxlint-disable-next-line no-explicit-any -- black-box JSON, asserted field by field
export type Json = any;
export interface Reply {
  status: number;
  headers: Headers;
  bytes: Uint8Array;
  text: string;
  json: Json;
}
export interface Instance {
  name: string;
  ip: string;
  dataDir: string;
  tag: string;
  /** Host path of this instance's rw drop-box (bind-mounted at CONTROL_REQUESTS_DIR), if any. */
  requestsDir?: string;
}
export type Pair = [Instance, Instance];

export async function request(target: Instance, method: string, path: string, body?: unknown): Promise<Reply> {
  const init: RequestInit = { method, headers: {} };
  if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const response = await fetch(`http://${target.ip}:${PORT}${path}`, init);
  const bytes = new Uint8Array(await response.arrayBuffer());
  const text = new TextDecoder().decode(bytes);
  let json: Json = null;
  try {
    json = JSON.parse(text);
  } catch {
    json = null;
  }
  return { status: response.status, headers: response.headers, bytes, text, json };
}

export const contentType = (reply: Reply): string => (reply.headers.get("content-type") ?? "").split(";")[0] ?? "";
/** The comparable form of a JSON reply: status, content type and the clock-normalized body. */
export const shape = (reply: Reply) => ({
  status: reply.status,
  type: contentType(reply),
  body: reply.json === null ? reply.text : reply.status === 502 && typeof reply.json?.error === "string" && Object.keys(reply.json).length === 1
    // routes/{serving,servingTiming,export}.ts answer 502 `{error: error.message}` on a config read
    // failure: the message is the runtime's I/O error text (Bun: "ENOENT: no such file or
    // directory, open '…'"), compared by shape only.
    ? { error: "<io error message>" }
    : normalizeResponse(reply.json),
});

/**
 * Subject vs reference twin: one request against each (sequentially), compared on status + type +
 * normalized body. Returns the subject reply for pinned facts; out-of-subset paths are skipped.
 */
export async function both(pair: Pair, name: string, method: string, path: string, body?: unknown): Promise<Reply | null> {
  if (!routeIncluded(path)) {
    skip(name, `${path} outside --routes`);
    return null;
  }
  const s = await request(pair[0], method, path, body);
  const g = await request(pair[1], method, path, body);
  const d = diff(shape(s), shape(g));
  check(`${name} [${method} ${path}] matches reference`, d === null, d ?? "");
  return s;
}
export function expectStatus(reply: Reply | null, name: string, status: number) {
  if (reply) check(`${name}: status ${status}`, reply.status === status, `got ${reply.status}: ${reply.text.slice(0, 200)}`);
}
/** Pinned fact on a subject reply (skipped silently when `both` skipped the route). */
export function pin(reply: Reply | null, name: string, ok: (json: Json) => boolean) {
  if (reply) check(name, ok(reply.json), reply.text.slice(0, 300));
}

// ---- process + docker --------------------------------------------------------------------
export async function runFull(cmd: string[]): Promise<{ code: number; out: string; err: string }> {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" });
  const [out, err, code] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);
  return { code, out: out.trim(), err: err.trim() };
}
export async function run(cmd: string[], allowFail = false): Promise<string> {
  const { code, out, err } = await runFull(cmd);
  if (code !== 0 && !allowFail) throw new Error(`${cmd.slice(0, 4).join(" ")} failed (${code}): ${err}`);
  return allowFail ? `${out}${err ? `\n${err}` : ""}`.trim() : out;
}

export const work = await mkdtemp(join(tmpdir(), `${prefix}-`));
await chmod(work, 0o755);
export const network = prefix;
const containers = new Set<string>();
let cleaning: Promise<void> | null = null;

export function cleanup(): Promise<void> {
  cleaning ??= (async () => {
    const named = (await run(["docker", "ps", "-aq", "--filter", `name=^${prefix}-`], true)).split("\n").filter((s) => /^[0-9a-f]+$/.test(s));
    if (containers.size + named.length > 0) await run(["docker", "rm", "-f", "-v", ...containers, ...named], true);
    await run(["docker", "network", "rm", network], true);
    // chmod 000 fixtures (feeds phase) must not survive: restore before removal.
    await run(["chmod", "-R", "u+rwX", work], true);
    await rm(work, { recursive: true, force: true });
  })();
  return cleaning;
}
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    void cleanup().finally(() => process.exit(130));
  });
}

let serial = 0;
export async function startContainer(key: string, dockerArgs: string[]): Promise<{ name: string; ip: string }> {
  if (cleaning) throw new Error("harness interrupted");
  serial += 1;
  const name = `${prefix}-${key}-${serial}`;
  containers.add(name);
  await run(["docker", "run", "-d", "--name", name, "--network", network, ...dockerArgs]);
  const ip = await run(["docker", "inspect", "-f", `{{(index .NetworkSettings.Networks "${network}").IPAddress}}`, name]);
  return { name, ip };
}

export interface AppSpec {
  env: Record<string, string>;
  /** `-v` specs (`host:container[:ro]`). */
  mounts: string[];
  /** Host dir bind-mounted rw at /control/requests (compose's one write surface), if any. */
  requestsDir?: string;
}

/** Starts `tag` with `spec` and `dataDir` bind-mounted at /data (fresh unless given); ready = /health 200. */
export async function startApp(key: string, tag: string, spec: AppSpec, dataDir?: string): Promise<Instance> {
  const data = dataDir ?? join(work, "data", `${key}-${serial + 1}`);
  await mkdir(data, { recursive: true });
  await chmod(data, 0o777);
  const mountArgs = [...spec.mounts, ...(spec.requestsDir ? [`${spec.requestsDir}:/control/requests`] : [])].flatMap((m) => ["-v", m]);
  const envArgs = Object.entries(spec.env).flatMap(([k, v]) => ["-e", `${k}=${v}`]);
  const { name, ip } = await startContainer(key, ["-v", `${data}:/data`, ...mountArgs, ...envArgs, tag]);
  const instance: Instance = { name, ip, dataDir: data, tag, requestsDir: spec.requestsDir };
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (await fetch(`http://${ip}:${PORT}/health`).then((r) => r.ok, () => false)) return instance;
    const state = await run(["docker", "inspect", "-f", "{{.State.Running}}", name], true);
    if (state === "false") break;
    await sleep(150);
  }
  throw new Error(`${name} never became ready:\n${await logs(instance)}`);
}
/** Subject (`--image`) + reference twin (the same `--image`); `specFor(i)` lets each twin own its drop-box. */
export async function startPair(key: string, specFor: (twin: number) => AppSpec | Promise<AppSpec>, dirs: [string?, string?] = []): Promise<Pair> {
  const [s, r] = [await specFor(0), await specFor(1)];
  return Promise.all([startApp(`${key}-s`, image, s, dirs[0]), startApp(`${key}-r`, image, r, dirs[1])]);
}
export async function stopApp(instance: { name: string }) {
  await run(["docker", "stop", "-t", "10", instance.name], true);
  await run(["docker", "rm", "-f", "-v", instance.name], true);
  containers.delete(instance.name);
}
export const stopPair = (pair: Pair) => Promise.all(pair.map(stopApp));
export const logs = (instance: { name: string }) => run(["docker", "logs", "--tail", "60", instance.name], true);

/**
 * The ingest CLI argv prefix inside `tag` (plan Decision 3 command seam): an image whose
 * entrypoint/cmd runs `bun` is the Bun oracle (`bun run apps/api/src/ingest/cli.ts`, the compose
 * comment's operator command); anything else is the Rust binary (`/inference-dashboard ingest`,
 * Decision 6).
 */
const seamCache = new Map<string, string[]>();
export async function ingestCommand(tag: string): Promise<string[]> {
  const cached = seamCache.get(tag);
  if (cached) return cached;
  const config = await run(["docker", "image", "inspect", "-f", "{{json .Config.Entrypoint}} {{json .Config.Cmd}}", tag]);
  const command = /\bbun\b/.test(config) ? ["bun", "run", "apps/api/src/ingest/cli.ts"] : ["/inference-dashboard", "ingest"];
  seamCache.set(tag, command);
  return command;
}
export async function execIngest(instance: Instance, cliArgs: string[] = []) {
  return runFull(["docker", "exec", instance.name, ...(await ingestCommand(instance.tag)), ...cliArgs]);
}
