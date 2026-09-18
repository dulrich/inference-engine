// config.mjs — the process's pinned inputs and its environment reading.
//
// Two kinds of value live here and the distinction is the point: the PINNED ones
// (preamble, thread config, the in-band item-type allowlist) are contract and are
// deliberately NOT env-overridable, and `readConfig` reads everything that is.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const VERSION = "0.1.0";
export const BODY_LIMIT = 32 * 1024 * 1024;

// Pinned, not env-overridable (plan, Public Interfaces).
export const PREAMBLE = fs.readFileSync(path.join(HERE, "preamble.txt"), "utf8").replace(/\n+$/, "");
export const THREAD_CONFIG = JSON.parse(fs.readFileSync(path.join(HERE, "thread-config.json"), "utf8"));
// The execution boundary: any other item type is out of band and trips it.
export const INBAND_ITEM_TYPES = new Set(["agentMessage", "reasoning", "dynamicToolCall", "userMessage"]);

export function readConfig(env = process.env) {
  const home = env.HOME || os.homedir();
  const stateDir = env.SIDECAR_STATE_DIR || path.join(home, ".local", "state", "codex-sidecar");
  const num = (v, d) => { const n = Number(v); return Number.isFinite(n) ? n : d; };
  return {
    key: env.SIDECAR_KEY || "",
    port: num(env.SIDECAR_PORT, 4877),
    // empty string means "skip the extra bind"; unset means the docker0 gateway.
    bindExtra: env.SIDECAR_BIND_EXTRA === undefined ? "172.17.0.1" : env.SIDECAR_BIND_EXTRA,
    personas: String(env.SIDECAR_PERSONAS ?? "codex-a,codex-b").split(",").map(s => s.trim()).filter(Boolean),
    cwdFallback: env.SIDECAR_CWD || path.join(stateDir, "empty-cwd"),
    stateDir,
    logDir: env.SIDECAR_LOG_DIR || path.join(stateDir, "logs"),
    logKeepDays: num(env.SIDECAR_LOG_KEEP_DAYS, 14),
    // SQLite event outbox for the fleet-tools initiative tracker. Unset = writer off.
    outboxPath: env.SIDECAR_OUTBOX_PATH || "",
    // Backstop for the wire usage clamp when the app-server reports no per-thread window:
    // the catalog's 272,000 context window at its 95% auto-compact scope limit.
    contextWindowTokens: num(env.SIDECAR_CONTEXT_WINDOW_TOKENS, 258400),
    // ---- test seams (documented in README; they never weaken an invariant) ----
    appServerCmd: env.SIDECAR_APP_SERVER_CMD || "",     // substitute the fake app-server
    pingMs: num(env.SIDECAR_PING_MS, 15000),            // SSE silence-ping interval
    batchMs: num(env.SIDECAR_BATCH_MS, 200),            // parallel tool-call batching window
    turnTimeoutMs: num(env.SIDECAR_TURN_TIMEOUT_MS, 300000),
    backoffMs: num(env.SIDECAR_BACKOFF_MS, 1000),       // child restart backoff floor
    // Extra-bind retry (the docker0 gateway does not exist until docker.service has started;
    // measured 3.6 s after this unit at boot). Interval is the first backoff step, doubling
    // to 5 s; budget is the total wall-clock before giving up. Both are test seams.
    bindRetryMs: num(env.SIDECAR_BIND_RETRY_MS, 500),
    bindRetryBudgetMs: num(env.SIDECAR_BIND_RETRY_BUDGET_MS, 60000),
    env,
  };
}

// The thread's working directory: the client's own `Primary working directory:` when it
// names a real one, else the configured fallback, else an empty directory created on
// demand so no stray AGENTS.md is loaded from somebody else's tree.
export function resolveCwd(config, logger, rawCwd) {
  const candidates = [rawCwd, config.cwdFallback];
  for (const c of candidates) {
    if (!c) continue;
    try { if (fs.statSync(c).isDirectory()) return c; } catch { /* next */ }
  }
  // last resort: an empty directory, so no stray AGENTS.md is loaded
  const empty = path.join(config.stateDir, "empty-cwd");
  fs.mkdirSync(empty, { recursive: true, mode: 0o700 });
  if (rawCwd) logger.write("cwd-fallback", { requested: rawCwd, used: empty });
  return empty;
}
