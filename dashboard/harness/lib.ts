// Shared plumbing for the serving-dashboard black-box harness: CLI, PASS/FAIL/SKIP accounting, HTTP client, docker lifecycle with unconditional per-run
// cleanup. Talks to images only over HTTP on a throwaway docker network (no host port publish),
// env, bind-mounted fixture files, and the files left in the drop-box. Never imports API code.
import { chmod, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const ALL_PHASES = ["routes", "control", "feeds", "spa"];
/** The port the image listens on (the compose block sets 4810). */
export const PORT = 4810;
/** An ISO-8601 UTC timestamp with optional milliseconds (the drop-box `requested_at` grammar). */
export const ISO_8601 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/;

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
  tag: string;
  /** Host path of this instance's rw drop-box (bind-mounted at CONTROL_REQUESTS_DIR), if any. */
  requestsDir?: string;
}

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
/** One request against `target`; an out-of-`--routes` path is skipped (counted) and returns null. */
export async function get(target: Instance, name: string, method: string, path: string, body?: unknown): Promise<Reply | null> {
  if (!routeIncluded(path)) {
    skip(name, `${path} outside --routes`);
    return null;
  }
  return request(target, method, path, body);
}
export function expectStatus(reply: Reply | null, name: string, status: number) {
  if (reply) check(`${name}: status ${status}`, reply.status === status, `got ${reply.status}: ${reply.text.slice(0, 200)}`);
}
/** Pinned fact on a subject reply (skipped silently when `get` skipped the route). */
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

/** Starts `tag` with `spec`; ready = /health 200. */
export async function startApp(key: string, tag: string, spec: AppSpec): Promise<Instance> {
  const mountArgs = [...spec.mounts, ...(spec.requestsDir ? [`${spec.requestsDir}:/control/requests`] : [])].flatMap((m) => ["-v", m]);
  const envArgs = Object.entries(spec.env).flatMap(([k, v]) => ["-e", `${k}=${v}`]);
  const { name, ip } = await startContainer(key, [...mountArgs, ...envArgs, tag]);
  const instance: Instance = { name, ip, tag, requestsDir: spec.requestsDir };
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (await fetch(`http://${ip}:${PORT}/health`).then((r) => r.ok, () => false)) return instance;
    const state = await run(["docker", "inspect", "-f", "{{.State.Running}}", name], true);
    if (state === "false") break;
    await sleep(150);
  }
  throw new Error(`${name} never became ready:\n${await logs(instance)}`);
}
export async function stopApp(instance: { name: string }) {
  await run(["docker", "stop", "-t", "10", instance.name], true);
  await run(["docker", "rm", "-f", "-v", instance.name], true);
  containers.delete(instance.name);
}
export const logs = (instance: { name: string }) => run(["docker", "logs", "--tail", "60", instance.name], true);

