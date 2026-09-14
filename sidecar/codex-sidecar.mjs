#!/usr/bin/env node
// codex-sidecar.mjs — Anthropic Messages front end over `codex app-server` children.
//
// Claude Code (via the router) -> POST /<persona>/v1/messages -> a persistent
// `<persona> app-server` child driven over JSON-RPC on its stdio pipe.
//
// Invariants (plan: plans/codex-appserver-sidecar.md):
//   * the adapter never intermediates the codex credential — the child owns $CODEX_HOME;
//   * the security boundary is the runtime item-type filter, not thread config;
//   * one codex thread per (Claude Code `session_id`, agent scope) — a parent and its
//     subagents share a session id on the wire and must never share a thread;
//   * no dependency outside node:*.
import http from "node:http";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createOutboxWriter } from "./outbox.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VERSION = "0.1.0";
const BODY_LIMIT = 32 * 1024 * 1024;

// Pinned, not env-overridable (plan, Public Interfaces).
const PREAMBLE = fs.readFileSync(path.join(HERE, "preamble.txt"), "utf8").replace(/\n+$/, "");
const THREAD_CONFIG = JSON.parse(fs.readFileSync(path.join(HERE, "thread-config.json"), "utf8"));
// The execution boundary: any other item type is out of band and trips it.
const INBAND_ITEM_TYPES = new Set(["agentMessage", "reasoning", "dynamicToolCall", "userMessage"]);

const MODEL_FAMILIES = { "gpt-5.6-luna": "low", "gpt-5.6-sol": "medium", "gpt-6-astra": "medium" };
// The harness's `output_config.effort` vocabulary folded onto the app-server's low|medium|high.
const EFFORT_LEVELS = { low: "low", medium: "medium", high: "high", max: "high", xhigh: "high" };
const TITLE_PHRASE = "You are naming a coding session so the user can pick it out of a long list of sessions";
const TITLE_MAX_SYSTEM_CHARS = 4000;
// Spike-era envelope was the bare <session> block; Claude Code 2.1.269 appends a language
// instruction after </session> (WP-4 G1). The envelope is a drift label, never the decision.
const SESSION_RE = /^\s*<session>[\s\S]*<\/session>/;
const CWD_RE = /^[ \t]*Primary working directory:[ \t]*(.+?)[ \t]*$/m;
// The subagent witness lives in the billing line Claude Code prepends to `system` (spike
// 2026-09-12): `x-anthropic-billing-header: cc_version=…; cc_entrypoint=…; cc_is_subagent=true;`.
// Matched as a token, never as a whole-line equality — the rest of the line drifts per release.
const BILLING_PREFIX = "x-anthropic-billing-header:";
const SUBAGENT_RE = /(?:^|[\s;])cc_is_subagent=true(?:[;\s]|$)/;

// ---------------------------------------------------------------- config ----

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

// ----------------------------------------------------------------- error ----

export class SidecarError extends Error {
  constructor(status, type, message, extra = {}) {
    super(message);
    this.status = status;
    this.anthropicType = type;
    this.extra = extra;
  }
  body() { return { type: "error", error: { type: this.anthropicType, message: this.message } }; }
}

const asSidecarError = e => (e instanceof SidecarError
  ? e
  : new SidecarError(502, "api_error", String(e?.message || e || "sidecar failure")));

// CodexErrorInfo string variants (schema) -> Anthropic error shape.
const CODEX_ERROR_MAP = {
  unauthorized: [401, "authentication_error"],
  rateLimitExceeded: [429, "rate_limit_error"],
  usageLimitExceeded: [429, "rate_limit_error"],
  sessionBudgetExceeded: [429, "rate_limit_error"],
  serverOverloaded: [502, "api_error"],
};

// Map an app-server JSON-RPC error / TurnError onto an Anthropic error shape.
export function mapUpstreamError(err, ctxLabel = "app-server") {
  const obj = (err && typeof err === "object") ? err : {};
  const text = typeof err === "string" ? err : JSON.stringify(err ?? {});
  const msg = obj.message ?? obj.error?.message ?? text;
  const info = obj.codexErrorInfo ?? obj.error?.codexErrorInfo ?? obj.data?.codexErrorInfo ?? null;
  const infoKey = typeof info === "string" ? info : (info && typeof info === "object" ? Object.keys(info)[0] : null);
  if (infoKey && Object.hasOwn(CODEX_ERROR_MAP, infoKey)) {
    const [status, type] = CODEX_ERROR_MAP[infoKey];
    return new SidecarError(status, type, `${ctxLabel}: ${msg}`);
  }
  // object CodexErrorInfo variants forward the upstream HTTP status verbatim
  const httpStatus = infoKey && info && typeof info === "object" ? info[infoKey]?.httpStatusCode : null;
  if (httpStatus === 401 || httpStatus === 403) return new SidecarError(401, "authentication_error", `${ctxLabel}: ${msg}`);
  if (httpStatus === 429) return new SidecarError(429, "rate_limit_error", `${ctxLabel}: ${msg}`);
  if (/rate.?limit|usage limit|quota|too many requests/i.test(text)) {
    return new SidecarError(429, "rate_limit_error", `${ctxLabel}: ${msg}`);
  }
  if (/unauthori[sz]ed|authentication|not logged in|login required|credential|token refresh|invalid.*api.?key/i.test(text)) {
    return new SidecarError(401, "authentication_error", `${ctxLabel}: ${msg}`);
  }
  return new SidecarError(502, "api_error", `${ctxLabel}: ${msg}`);
}

// RateLimitWindow.resetsAt is an absolute int64 (schema); tolerate s or ms epochs.
export function retryAfterSeconds(resetsAt, now = Date.now()) {
  if (typeof resetsAt !== "number" || !Number.isFinite(resetsAt)) return null;
  const ms = resetsAt > 1e12 ? resetsAt : resetsAt * 1000;
  const secs = Math.ceil((ms - now) / 1000);
  return secs > 0 ? secs : 1;
}

// A RateLimitSnapshot is "exhausted" when a window is at/over 100 % or a reached-type is set.
export function rateLimitExhaustion(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const windows = [snapshot.primary, snapshot.secondary].filter(w => w && typeof w === "object");
  const full = windows.filter(w => typeof w.usedPercent === "number" && w.usedPercent >= 100);
  if (!full.length && !snapshot.rateLimitReachedType) return null;
  const resets = (full.length ? full : windows).map(w => retryAfterSeconds(w.resetsAt)).filter(n => n != null);
  return {
    reason: snapshot.rateLimitReachedType || "rate_limit_reached",
    retryAfter: resets.length ? Math.min(...resets) : null,
  };
}

// ------------------------------------------------------------------ logs ----

export class Logger {
  constructor(dir, keepDays) {
    this.dir = dir;
    this.keepDays = keepDays;
    this.day = null;
    this.fd = null;
    this.file = null;
  }
  static dayOf(d = new Date()) { return d.toISOString().slice(0, 10); }
  rollover() {
    const day = Logger.dayOf();
    if (this.fd !== null && day === this.day) return;
    if (this.fd !== null) { try { fs.closeSync(this.fd); } catch { /* ignore */ } this.fd = null; }
    fs.mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    try { fs.chmodSync(this.dir, 0o700); } catch { /* ignore */ }
    const file = path.join(this.dir, `codex-sidecar-${day}.jsonl`);
    this.fd = fs.openSync(file, "a", 0o600);
    try { fs.chmodSync(file, 0o600); } catch { /* ignore */ }
    this.day = day;
    this.file = file;
    this.prune();
  }
  prune(now = Date.now()) {
    const cutoff = now - this.keepDays * 86400000;
    let names = [];
    try { names = fs.readdirSync(this.dir); } catch { return; }
    for (const n of names) {
      const m = /^codex-sidecar-(\d{4}-\d{2}-\d{2})\.jsonl$/.exec(n);
      if (!m || n === path.basename(this.file || "")) continue;
      const t = Date.parse(`${m[1]}T00:00:00Z`);
      if (Number.isFinite(t) && t < cutoff) { try { fs.unlinkSync(path.join(this.dir, n)); } catch { /* ignore */ } }
    }
  }
  write(kind, obj = {}) {
    try {
      this.rollover();
      fs.writeSync(this.fd, `${JSON.stringify({ ts: new Date().toISOString(), kind, ...obj })}\n`);
    } catch { /* logging must never break a request */ }
  }
  close() { if (this.fd !== null) { try { fs.closeSync(this.fd); } catch { /* ignore */ } this.fd = null; } }
}

export function redactHeaders(headers = {}) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    out[k] = /^(authorization|x-api-key|cookie|proxy-authorization)$/i.test(k) ? "<redacted>" : v;
  }
  return out;
}

// --------------------------------------------------------------- helpers ----

export function parseModelId(raw) {
  const id = String(raw ?? "").trim();
  if (!id) return null;
  // the gateway may hand us either `gpt-5.6-luna` or `codex-a/gpt-5.6-luna`
  const bare = id.includes("/") ? id.slice(id.lastIndexOf("/") + 1) : id;
  // `effortSource` records WHY this effort holds: an explicit `-low|-medium|-high` suffix, or the
  // MODEL_FAMILIES default. `resolveEffort` needs that distinction — a suffix wins over the
  // request's `output_config.effort`, a family default loses to it. The suffix grammar and the
  // `effort` values themselves are unchanged (contract).
  if (Object.hasOwn(MODEL_FAMILIES, bare)) return { model: bare, effort: MODEL_FAMILIES[bare], effortSource: "default" };
  const m = /^(.*)-(low|medium|high)$/.exec(bare);
  if (m && Object.hasOwn(MODEL_FAMILIES, m[1])) return { model: m[1], effort: m[2], effortSource: "suffix" };
  return null;
}

// Effort precedence for one turn (plan `codex-leg-effort-and-auditor-unpinning`, WP-1), highest
// first: the id suffix (explicit beats implicit, even against a conflicting request field) ->
// the harness's `output_config.effort` -> `thinking.type === "disabled"` -> the family default.
//
// `max`/`xhigh` fold onto `high`: the app-server's ladder is low|medium|high only. `disabled`
// maps to `low` because the app-server has no "off" level and `low` is its floor — and it ranks
// BELOW an explicit level here, a deliberate divergence from the local leg's patch 0007
// (`router/AGENTS.md:58`), which ranks `disabled` first because that leg has a real off switch.
// 0007's `thinking:{type:"enabled", budget_tokens}` rung is deliberately OMITTED on this leg
// (plan "Decisions locked"): the only client sends `type:"adaptive"` (measured 2026-09-13), so a
// budget->level table would be an unmeasured guess. `enabled`, `adaptive`, an unrecognised
// `effort` value and an absent field all fall to the family default.
//
// The no-op invariant, inherited from 0007: this never throws and never yields an error — no
// request shape that succeeded before may fail now.
export function resolveEffort(parsed, body) {
  if (parsed?.effortSource === "suffix") return { effort: parsed.effort, effortSource: "suffix" };
  const want = body?.output_config?.effort;
  if (typeof want === "string" && Object.hasOwn(EFFORT_LEVELS, want)) {
    return { effort: EFFORT_LEVELS[want], effortSource: "request" };
  }
  if (body?.thinking?.type === "disabled") return { effort: "low", effortSource: "request" };
  return { effort: parsed?.effort ?? null, effortSource: "default" };
}

export const blocksOf = content => (Array.isArray(content)
  ? content
  : (typeof content === "string" ? [{ type: "text", text: content }] : []));

// Client-side thinking/reasoning blocks are dropped; only text survives.
export const textOf = content => blocksOf(content)
  .filter(b => b && b.type === "text" && typeof b.text === "string")
  .map(b => b.text).join("\n");

export function systemTextOf(body) {
  const s = body?.system;
  if (typeof s === "string") return s;
  if (Array.isArray(s)) return s.map(x => (typeof x === "string" ? x : (x?.text ?? ""))).join("\n\n");
  return "";
}

// Claude Code interleaves role:"system" entries into messages[]; they are never a new turn.
export const conversational = msgs => (Array.isArray(msgs) ? msgs : [])
  .filter(m => m && (m.role === "user" || m.role === "assistant"));

// ...but their *text* is not disposable. Claude Code advertises the deferred-tool name list
// (ENABLE_TOOL_SEARCH), the agent-type listing and the installed-skills listing ONLY inside those
// interleaved entries — never in top-level `system`. Dropping them hands the model a working
// `ToolSearch` tool with nothing to search for, so it answers "no such tool" with no error
// (measured: docs/tool-calling-gateway/tool-search-conformance-spike.md, "Named defect").
// Collected here in message order and folded into the thread's baseInstructions.
export const interleavedSystemText = msgs => (Array.isArray(msgs) ? msgs : [])
  .filter(m => m && m.role === "system")
  .map(m => textOf(m.content).trim())
  .filter(Boolean)
  .join("\n\n");

// preamble + top-level `system` + the interleaved role:"system" text, in that order. With no
// interleaved text this is byte-identical to what the thread used to start with. It is computed
// only at `thread/start`, and every input is stable across an agent's own turns, so the WP-3
// subagent discriminator — which hashes top-level `system` minus the billing line, not this —
// is unaffected.
export function baseInstructionsOf(systemText, interleaved) {
  const head = systemText || "You are a coding assistant driven by an external harness.";
  return [PREAMBLE, head, interleaved].filter(Boolean).join("\n\n");
}

export function hasImageBlock(body) {
  for (const m of (Array.isArray(body?.messages) ? body.messages : [])) {
    for (const b of blocksOf(m?.content)) {
      if (b?.type === "image") return true;
      if (b?.type === "tool_result") { for (const c of blocksOf(b.content)) if (c?.type === "image") return true; }
    }
  }
  for (const b of (Array.isArray(body?.system) ? body.system : [])) if (b?.type === "image") return true;
  return false;
}

// The cwd is parsed *before* the system-role filter drops the environment message.
export function extractCwd(allMsgs, systemText) {
  for (const m of (Array.isArray(allMsgs) ? allMsgs : [])) {
    if (m?.role !== "system") continue;
    const hit = CWD_RE.exec(textOf(m.content));
    if (hit) return hit[1];
  }
  const hit = CWD_RE.exec(systemText || "");
  return hit ? hit[1] : null;
}

// Claude Code gives a subagent its parent's `session_id` by design, so the id alone cannot
// separate them (measured 2026-09-12, CC 2.1.270). The gateway strips every `x-claude-code-*`
// header before the sidecar sees the request — `x-claude-code-agent-id` is therefore NOT
// available and the agent id appears nowhere in the body. The one body-level witness is the
// billing line Claude Code prepends to the system prompt: a subagent's carries
// `cc_is_subagent=true;`, a parent's never does.
export function billingLineOf(body) {
  const s = body?.system;
  let head = "";
  if (typeof s === "string") head = s;
  else if (Array.isArray(s)) { const f = s[0]; head = typeof f === "string" ? f : (f?.text ?? ""); }
  if (typeof head !== "string" || !head) return "";
  const line = head.split("\n", 1)[0];
  return line.trimStart().toLowerCase().startsWith(BILLING_PREFIX) ? line : "";
}

export const isSubagentRequest = body => SUBAGENT_RE.test(billingLineOf(body));

// The billing line is the only part of `system` that moves between a parent and its subagent
// (and it carries a per-agent `cc_version` suffix), so the subagent discriminator hashes the
// system prompt *without* it — everything else is byte-identical across an agent's own turns.
export function systemTextWithoutBillingLine(body, systemText) {
  const line = billingLineOf(body);
  if (!line || (systemText || "").indexOf(line) !== 0) return systemText;
  return systemText.slice(line.length).replace(/^\n+/, "");
}

// One thread per (session_id, agent scope). The parent's key stays the bare `session_id` so a
// mid-session system-prompt change cannot fragment its thread; a subagent gets a suffix derived
// from the fields that are stable across its own turns.
export function sessionKeyOf(body, systemText, firstUserText, headers = null) {
  const uid = body?.metadata?.user_id;
  let parsed = null;
  if (typeof uid === "string") { try { parsed = JSON.parse(uid); } catch { parsed = null; } }
  else if (uid && typeof uid === "object") parsed = uid;
  const sid = parsed && typeof parsed.session_id === "string" ? parsed.session_id.trim() : "";
  const scope = isSubagentRequest(body) ? "subagent" : "parent";
  if (sid) {
    if (scope === "parent") return { key: sid, weak: false, scope };
    // Future-proofing only: if the gateway allowlist ever forwards the agent id, prefer it.
    const agentId = String(headers?.["x-claude-code-agent-id"] ?? "").trim();
    if (agentId) return { key: `${sid}:${agentId}`, weak: false, scope };
    const disc = crypto.createHash("sha256")
      .update(`${systemTextWithoutBillingLine(body, systemText)}\0${firstUserText}`)
      .digest("hex").slice(0, 16);
    return { key: `${sid}:${disc}`, weak: false, scope };
  }
  const hash = crypto.createHash("sha256").update(`${systemText}\0${firstUserText}`).digest("hex").slice(0, 32);
  return { key: hash, weak: true, scope };
}

// ---------------------------------------------------------------- outbox ----
// Event rows for the fleet-tools initiative tracker (plan `tracker-event-seam.md`).
// Every call here is fail-open: the writer swallows, so an outbox failure can never fail,
// delay or log-as-error a request (the `Logger.write` contract).

// A session key split into the tracker's (session_id, agent_id) pair: the parent's key is a
// bare Claude Code session id, a subagent's is `<sid>:<suffix>`. A weak key (no
// `metadata.user_id.session_id` at all) is not attributable to a session — it writes nothing.
export function outboxKeyOf(key, weak) {
  if (weak || typeof key !== "string" || key.length === 0) return null;
  const i = key.indexOf(":");
  return i < 0
    ? { session_id: key, agent_id: null }
    : { session_id: key.slice(0, i), agent_id: key.slice(i + 1) };
}

// The codex cumulative total for a thread, in the tracker's names. `usageOut` documents the
// raw codex fields; `total` is the spend sum (input includes its cached share, as codex
// reports it), not a context-window occupancy figure.
const outboxUsage = u => {
  const input = u?.inputTokens ?? 0;
  const output = u?.outputTokens ?? 0;
  return { input, output, total: input + output };
};

// One `turn.completed` per completed turn, plus `child.spawned` the first time a subagent key
// is seen, the thread's cumulative `usage` when codex has reported one, and the `rate_limit`
// recovery row that clears a persona-wide exhaustion. Never any message content.
function outboxTurn(ctx, persona, sess, { model, effort, stopReason }) {
  const okey = sess.outboxKey;
  if (!okey || !ctx.config.outboxPath) return;
  const base = {
    harness: "codex-sidecar", leg: persona.name,
    session_id: okey.session_id, agent_id: okey.agent_id, agent_type: null,
  };
  if (okey.agent_id && !ctx.outboxSpawned.has(sess.key)) {
    ctx.outboxSpawned.add(sess.key);
    ctx.outbox.record({ ...base, kind: "child.spawned", payload: { threadId: sess.threadId } });
  }
  ctx.outbox.record({
    ...base, kind: "turn.completed",
    payload: { model, effort, threadId: sess.threadId, stop_reason: stopReason },
  });
  if (sess.usageTotal) {
    ctx.outbox.record({ ...base, kind: "usage", payload: { model, effort, ...outboxUsage(sess.usageTotal) } });
  }
  if (persona.rateLimited) {
    persona.rateLimited = false;
    ctx.outbox.record({ ...base, kind: "rate_limit", payload: { state: "ok" } });
  }
}

// The title signature: empty tools, a short system prompt carrying the naming phrase, a single
// user message. A miss must fail safe — a title query that reaches the app-server opens the
// session's thread and the real task then lands as a continuation of "name this session"
// (WP-4 G1) — so the user-message envelope is deliberately NOT a clause; titleEnvelope() labels it.
export function isTitleRequest(body, systemText, msgs) {
  const tools = Array.isArray(body?.tools) ? body.tools : [];
  if (tools.length !== 0) return false;
  if (!systemText || systemText.length >= TITLE_MAX_SYSTEM_CHARS) return false;
  if (!systemText.includes(TITLE_PHRASE)) return false;
  return msgs.length === 1 && msgs[0].role === "user";
}

// "pinned" when the user message opens with the <session> block (spike-era and 2.1.269 shapes
// both qualify); "drifted" once Claude Code rewords the envelope. Logged so drift is observable.
export function titleEnvelope(msgs) {
  return SESSION_RE.test(textOf(msgs[0]?.content)) ? "pinned" : "drifted";
}

export function countTokensEstimate(body) {
  let chars = systemTextOf(body).length;
  for (const m of (Array.isArray(body?.messages) ? body.messages : [])) {
    for (const b of blocksOf(m?.content)) {
      if (typeof b?.text === "string") chars += b.text.length;
      else if (typeof b?.content === "string") chars += b.content.length;
      else if (b?.content || b?.input) chars += JSON.stringify(b.content ?? b.input).length;
    }
  }
  for (const t of (Array.isArray(body?.tools) ? body.tools : [])) {
    chars += String(t?.name ?? "").length + String(t?.description ?? "").length
      + JSON.stringify(t?.input_schema ?? t?.inputSchema ?? {}).length;
  }
  return Math.ceil(chars / 4);
}

export function safeEqual(a, b) {
  const A = Buffer.from(String(a ?? ""), "utf8");
  const B = Buffer.from(String(b ?? ""), "utf8");
  if (A.length === 0 || B.length === 0 || A.length !== B.length) return false;
  return crypto.timingSafeEqual(A, B);
}

export function presentedKey(headers = {}) {
  const a = headers.authorization;
  if (typeof a === "string" && a.trim()) {
    const m = /^bearer\s+(.*)$/i.exec(a.trim());
    return (m ? m[1] : a).trim();
  }
  const x = headers["x-api-key"];
  if (typeof x === "string" && x.trim()) return x.trim();
  return null;
}

// Anthropic tools -> app-server DynamicToolSpec (function variant).
export function toDynamicTools(tools) {
  return (Array.isArray(tools) ? tools : [])
    .filter(t => t && typeof t.name === "string" && t.name)
    .map(t => ({
      type: "function",
      name: t.name,
      description: String(t.description || t.name).slice(0, 4000),
      inputSchema: t.input_schema ?? t.inputSchema ?? { type: "object", properties: {} },
    }));
}

const ZERO_USAGE = { inputTokens: 0, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: 0 };

// Raw codex usage -> Anthropic usage semantics. Codex's `inputTokens` is the WHOLE prompt,
// with the cached share a subset of it; Anthropic's three input columns sit side by side, so
// `input_tokens` is the *uncached* share only. Claude Code's context meter SUMS the three, so
// relaying codex's raw figure double-counted the prompt — measured 2026-09-13: the same
// 111-message conversation read 152,791 on one row and 79,952 eighteen seconds later, and a
// failed audit's "327k" was 155,775 + 151,808 for a ~66k conversation (audit E1/E2).
// Clamped at 0: a reconciled delta whose cache-read share exceeds its input share reports 0,
// never a negative. `output_tokens` is unchanged on every path.
export const usageOut = u => {
  const cacheRead = u?.cachedInputTokens ?? 0;
  const cacheWrite = u?.cacheWriteInputTokens ?? 0;
  return {
    input_tokens: Math.max(0, (u?.inputTokens ?? 0) - cacheRead - cacheWrite),
    cache_creation_input_tokens: cacheWrite,
    cache_read_input_tokens: cacheRead,
    output_tokens: u?.outputTokens ?? 0,
  };
};

// ----------------------------------------------------------------- sinks ----

function messageSkeleton(model) {
  return {
    id: `msg_${crypto.randomBytes(12).toString("hex")}`,
    type: "message",
    role: "assistant",
    model,
    content: [],
    stop_reason: null,
    stop_sequence: null,
  };
}

// Buffers the whole message and answers with one Messages JSON body (stream:false).
class BufferSink {
  constructor(res, model) {
    this.res = res;
    this.msg = messageSkeleton(model);
    this.text = "";
    this.blocks = [];
    this.started = false;
    this.done = false;
  }
  start() { this.started = true; }
  abort() { this.done = true; }
  textDelta(t) { if (t) this.text += t; }
  toolUse(block) { this.#flushText(); this.blocks.push(block); }
  #flushText() { if (this.text) { this.blocks.push({ type: "text", text: this.text }); this.text = ""; } }
  finish(stopReason, usage) {
    if (this.done) return;
    this.done = true;
    this.#flushText();
    if (!this.blocks.length) this.blocks.push({ type: "text", text: "(no text)" });
    const body = { ...this.msg, content: this.blocks, stop_reason: stopReason, usage: usageOut(usage) };
    this.res.writeHead(200, { "content-type": "application/json" });
    this.res.end(JSON.stringify(body));
  }
  error(err) {
    if (this.done) return;
    this.done = true;
    const headers = { "content-type": "application/json" };
    if (err.extra?.retryAfter != null) headers["retry-after"] = String(err.extra.retryAfter);
    this.res.writeHead(err.status, headers);
    this.res.end(JSON.stringify(err.body()));
  }
}

// Real SSE: message_start as soon as the turn is running, deltas as they arrive,
// `event: ping` after every `pingMs` of silence (Claude Code's 300 s watchdog).
class SseSink {
  constructor(res, model, pingMs) {
    this.res = res;
    this.msg = messageSkeleton(model);
    this.pingMs = pingMs;
    this.index = 0;
    this.textOpen = false;
    this.anyBlock = false;
    this.started = false;
    this.done = false;
    this.timer = null;
  }
  #raw(text) {
    if (this.done || this.res.writableEnded || this.res.destroyed) return false;
    try { this.res.write(text); } catch { this.abort(); return false; }
    return true;
  }
  #write(type, obj) {
    if (this.#raw(`event: ${type}\ndata: ${JSON.stringify(obj)}\n\n`)) this.#arm();
  }
  #arm() {
    if (this.timer) clearTimeout(this.timer);
    if (this.done || !this.started) return;
    this.timer = setTimeout(() => {
      if (this.#raw(`event: ping\ndata: ${JSON.stringify({ type: "ping" })}\n\n`)) this.#arm();
    }, this.pingMs);
    this.timer.unref?.();
  }
  abort() {
    this.done = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
  }
  start(usage) {
    if (this.started) return;
    this.started = true;
    this.res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    this.#write("message_start", { type: "message_start", message: { ...this.msg, usage: usageOut(usage) } });
  }
  textDelta(t) {
    if (!t || this.done) return;
    if (!this.textOpen) {
      this.#write("content_block_start", { type: "content_block_start", index: this.index, content_block: { type: "text", text: "" } });
      this.textOpen = true;
      this.anyBlock = true;
    }
    this.#write("content_block_delta", { type: "content_block_delta", index: this.index, delta: { type: "text_delta", text: t } });
  }
  #closeText() {
    if (!this.textOpen) return;
    this.#write("content_block_stop", { type: "content_block_stop", index: this.index });
    this.textOpen = false;
    this.index += 1;
  }
  toolUse(block) {
    if (this.done) return;
    this.#closeText();
    this.#write("content_block_start", { type: "content_block_start", index: this.index, content_block: { type: "tool_use", id: block.id, name: block.name, input: {} } });
    this.#write("content_block_delta", { type: "content_block_delta", index: this.index, delta: { type: "input_json_delta", partial_json: JSON.stringify(block.input ?? {}) } });
    this.#write("content_block_stop", { type: "content_block_stop", index: this.index });
    this.index += 1;
    this.anyBlock = true;
  }
  finish(stopReason, usage) {
    if (this.done) return;
    if (!this.anyBlock) this.textDelta("(no text)");
    this.#closeText();
    this.#write("message_delta", { type: "message_delta", delta: { stop_reason: stopReason, stop_sequence: null }, usage: usageOut(usage) });
    this.#write("message_stop", { type: "message_stop" });
    this.done = true;
    if (this.timer) clearTimeout(this.timer);
    if (!this.res.writableEnded) this.res.end();
  }
  // Before message_start an error is an HTTP status; after it, an `event: error` frame.
  error(err) {
    if (this.done) return;
    this.done = true;
    if (this.timer) clearTimeout(this.timer);
    if (!this.started) {
      const headers = { "content-type": "application/json" };
      if (err.extra?.retryAfter != null) headers["retry-after"] = String(err.extra.retryAfter);
      this.res.writeHead(err.status, headers);
      this.res.end(JSON.stringify(err.body()));
      return;
    }
    if (!this.res.writableEnded && !this.res.destroyed) {
      try {
        this.res.write(`event: error\ndata: ${JSON.stringify(err.body())}\n\n`);
        this.res.end();
      } catch { /* the client is gone */ }
    }
  }
}

// --------------------------------------------------------------- session ----

class Session {
  constructor(persona, key) {
    this.persona = persona;
    this.key = key;
    this.threadId = null;
    this.turnId = null;
    this.model = null;
    this.lastEffort = null;       // effort sent on this thread's previous turn/start (effort-changed)
    this.parked = new Map();      // callId -> JSON-RPC request id
    this.delivered = new Set();   // callIds already emitted to the client as tool_use blocks
    this.turnOpen = false;        // the codex turn is still running, blocked on a tool call
    this.batch = [];              // tool calls collected inside the batching window
    this.batchTimer = null;
    this.msgCount = 0;
    this.deltaItems = new Set();  // item ids already streamed as deltas
    this.usageLast = null;        // previous call's tokenUsage.last (the lag)
    this.usageTotal = null;       // codex-side cumulative for this thread
    this.outboxKey = null;        // {session_id, agent_id} for the tracker outbox; null = skip
    this.sent = { inputTokens: 0, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: 0 };
    this.sink = null;
    this.owner = null;            // the request currently driving this session (a request token)
    this.ownerGone = false;       // the owner's socket closed but a joiner keeps the turn alive
    this.joiners = new Set();     // duplicate requests attached to the owner's turn (WP-5)
    this.turnText = [];           // text emitted to the owner's sink since this request attached
    this.lastOutcome = null;      // the last completed request's outcome, for straggler replay
    this.pendingOutcomes = [];    // settled outcomes not yet handed to a request, in order
    this.resolveOutcome = null;
    this.timeoutTimer = null;
    this.closed = false;
  }

  attach(sink) { this.sink = sink; }
  detach(sink) { if (!sink || this.sink === sink) this.sink = null; }

  // The joiners bound to one owner (its request token, by identity). `joiners` is
  // session-global, so every read filters: a request only ever writes to, drains, promotes
  // or settles the duplicates that joined *it* (audit defect B3, 2026-09-13).
  joinersOf(owner) { return owner ? [...this.joiners].filter(j => j.ownerToken === owner) : []; }

  // Every byte of assistant text goes through here: to the owner's sink, to every
  // duplicate joined to *the current owner*, and into `turnText` so a joiner attaching
  // mid-stream can be replayed the text it missed. A promoted joiner is both `sink` and a
  // member of `joiners`, so it is written exactly once.
  emitText(t) {
    if (!t) return;
    this.turnText.push(t);
    this.sink?.textDelta(t);
    for (const j of this.joinersOf(this.owner)) if (j.sink !== this.sink) j.sink.textDelta(t);
  }

  // A newer request on the same session takes over: the request parked in
  // waitOutcome is resolved with the given error (its timer is cleared by the
  // resolver) instead of being silently orphaned with its slot overwritten.
  preempt(error) { this.resolveOutcome?.({ kind: "error", error }); }

  // Outcome protocol: the HTTP handler awaits one outcome per request.
  waitOutcome(timeoutMs) {
    return new Promise(resolve => {
      this.resolveOutcome = o => {
        if (!this.resolveOutcome) return;
        this.resolveOutcome = null;
        if (this.timeoutTimer) { clearTimeout(this.timeoutTimer); this.timeoutTimer = null; }
        resolve(o);
      };
      this.timeoutTimer = setTimeout(() => {
        this.resolveOutcome?.({ kind: "error", error: new SidecarError(502, "api_error", "timed out waiting for the codex app-server") });
      }, timeoutMs);
      this.timeoutTimer.unref?.();
      this.drain();
    });
  }

  // Outcomes queue in order (a tool-call batch settled while no request is attached
  // must not drop the next one — each parked call has to be delivered exactly once);
  // an error replaces whatever is queued.
  settle(outcome) {
    if (outcome.kind === "error") this.pendingOutcomes = [outcome];
    else this.pendingOutcomes.push(outcome);
    this.drain();
  }

  drain() {
    if (!this.pendingOutcomes.length || !this.resolveOutcome) return;
    this.resolveOutcome(this.pendingOutcomes.shift());
  }

  #armBatch() {
    if (this.batchTimer) clearTimeout(this.batchTimer);
    this.batchTimer = setTimeout(() => {
      this.batchTimer = null;
      if (!this.batch.length) return;
      const batch = this.batch;
      this.batch = [];
      this.settle({ kind: "tool_use", batch });
    }, this.persona.ctx.config.batchMs);
    this.batchTimer.unref?.();
  }

  // Every frame the app-server sends for this thread lands here.
  onFrame(ev) {
    if (this.closed) return;
    if (ev.type === "child-exit") { this.settle({ kind: "error", error: ev.error }); return; }
    if (ev.type === "toolcall") {
      const p = ev.rpc.params || {};
      this.turnId = p.turnId || this.turnId;
      this.parked.set(p.callId, ev.rpc.id);
      this.batch.push(ev.rpc);
      this.#armBatch();
      return;
    }
    const m = ev.frame;
    const p = m.params || {};
    switch (m.method) {
      case "turn/started":
        this.turnId = p.turn?.id || this.turnId;
        break;
      case "item/started": {
        const type = p.item?.type;
        this.turnId = p.turnId || this.turnId;
        if (type && !INBAND_ITEM_TYPES.has(type)) { this.persona.tripBoundary(type, this); return; }
        break;
      }
      case "item/agentMessage/delta":
        if (typeof p.delta === "string" && p.delta) {
          if (p.itemId) this.deltaItems.add(p.itemId);
          this.emitText(p.delta);
        }
        break;
      case "item/completed": {
        const item = p.item;
        if (item?.type === "agentMessage" && typeof item.text === "string" && item.text && !this.deltaItems.has(item.id)) {
          this.emitText(item.text);          // buffered fallback when no deltas arrived
        }
        break;
      }
      case "thread/tokenUsage/updated":
        if (p.tokenUsage) {
          this.usageLast = p.tokenUsage.last ?? this.usageLast;
          this.usageTotal = p.tokenUsage.total ?? this.usageTotal;
        }
        break;
      case "hook/started":
      case "mcpServer/startupStatus/updated":
        this.persona.log("config-unverified", {
          persona: this.persona.name, threadId: this.threadId, stage: "runtime", method: m.method,
        });
        this.settle({ kind: "error", error: new SidecarError(502, "api_error", `thread config not honoured: unexpected ${m.method}`) });
        break;
      case "error":
        this.settle({ kind: "error", error: mapUpstreamError(p.error ?? p, "app-server error") });
        break;
      case "turn/completed": {
        const turn = p.turn || {};
        this.turnId = turn.id || this.turnId;
        // the schema puts tokenUsage on thread/tokenUsage/updated only; tolerate it here too
        if (p.tokenUsage) {
          this.usageLast = p.tokenUsage.last ?? this.usageLast;
          this.usageTotal = p.tokenUsage.total ?? this.usageTotal;
        }
        if (turn.status === "failed" || turn.error) {
          this.settle({ kind: "error", error: mapUpstreamError(turn.error ?? { message: `turn ${turn.status}` }, "turn failed") });
          return;
        }
        if (this.batchTimer) return;   // a tool-call batch is still settling
        this.settle({ kind: "end_turn" });
        break;
      }
      default:
        break;
    }
  }

  // Reconciled usage, in RAW CODEX UNITS: the ledger (`this.sent`) tracks what has already
  // been reported per raw key, so cumulative reported == the codex-side total after every
  // message. What leaves on the wire is the Anthropic projection of each delta (`usageOut`),
  // which subtracts the cached share out of `input_tokens`; the ledger deliberately stays raw
  // so the codex-side totals remain recoverable (the `http` row carries them as `usageCodex`).
  reconcile() {
    const target = this.usageTotal || this.usageLast || ZERO_USAGE;
    const out = {};
    for (const k of Object.keys(ZERO_USAGE)) {
      out[k] = Math.max(0, (target[k] ?? 0) - (this.sent[k] ?? 0));
      this.sent[k] = (this.sent[k] ?? 0) + out[k];
    }
    return out;
  }

  // The lagged preview carried by message_start (previous call's tokenUsage.last).
  // It is a preview only: Anthropic's message_delta finalises the message's usage,
  // so the preview is never added to the reconciliation ledger.
  lagUsage() { return this.usageLast || ZERO_USAGE; }

  answerParked(callId, text, success) {
    const id = this.parked.get(callId);
    if (id === undefined) return false;
    this.parked.delete(callId);
    this.persona.send({ jsonrpc: "2.0", id, result: { contentItems: [{ type: "inputText", text: text || "(empty)" }], success: !!success } });
    return true;
  }

  // End-of-request cleanup (Defect E1). Only a call the client has already been
  // shown can be "unanswered": a call parked after the last response was written has
  // never crossed the request boundary, so it must survive to be delivered on the
  // next one instead of being answered with a refusal the model then has to absorb.
  abandonDelivered(reason) {
    for (const callId of [...this.parked.keys()]) {
      if (this.delivered.has(callId)) this.answerParked(callId, reason, false);
    }
  }

  abandonParked(reason) {
    for (const callId of [...this.parked.keys()]) this.answerParked(callId, reason, false);
    this.batch = [];
    if (this.batchTimer) { clearTimeout(this.batchTimer); this.batchTimer = null; }
  }

  close(reason) {
    if (this.closed) return;
    this.closed = true;
    this.abandonParked(reason || "aborted");
    if (this.timeoutTimer) clearTimeout(this.timeoutTimer);
    if (this.threadId) this.persona.bus.delete(this.threadId);
  }
}

// --------------------------------------------------------------- persona ----

class Persona {
  constructor(name, ctx) {
    this.name = name;
    this.ctx = ctx;
    this.child = null;
    this.childAlive = false;
    this.buf = "";
    this.rpcId = 1;
    this.pending = new Map();     // rpc id -> {resolve, reject}
    this.bus = new Map();         // threadId -> Session
    this.sessions = new Map();    // session key -> Session
    this.initPromise = null;
    this.degraded = false;
    this.degradedReason = null;
    this.backoffMs = ctx.config.backoffMs;
    this.restartTimer = null;
    this.stableTimer = null;
    this.hookEvents = 0;
    this.mcpEvents = 0;
    this.rateLimit = null;
    this.rateLimited = false;     // an exhaustion row is outstanding; the next turn clears it
    this.stopped = false;
  }

  log(kind, obj) { this.ctx.logger.write(kind, obj); }

  get alive() { return this.childAlive; }

  async ensure() {
    if (this.degraded) {
      throw new SidecarError(503, "api_error",
        `persona ${this.name} is degraded after an out-of-band execution attempt (${this.degradedReason}); its codex app-server is being restarted`);
    }
    if (!this.initPromise) this.setInit(this.boot());
    return this.initPromise;
  }

  // One in-flight boot per persona: a request arriving mid-backoff waits for the
  // scheduled restart instead of spawning a second child.
  setInit(p) {
    this.initPromise = p;
    p.catch(() => { if (this.initPromise === p) this.initPromise = null; });
    return p;
  }

  boot() {
    return (async () => {
      this.spawnChild();
      const r = await this.rpc("initialize", {
        clientInfo: { name: "codex-sidecar", version: VERSION },
        capabilities: { experimentalApi: true },
      });
      this.send({ jsonrpc: "2.0", method: "initialized", params: {} });
      this.degraded = false;
      this.degradedReason = null;
      this.log("initialized", { persona: this.name, userAgent: r?.userAgent ?? null });
      return r;
    })();
  }

  spawnChild() {
    const cmd = this.ctx.config.appServerCmd;
    let file, args;
    if (cmd) {
      const parts = cmd.replace(/\{persona\}/g, this.name).split(/\s+/).filter(Boolean);
      file = parts[0];
      args = [...parts.slice(1), "app-server"];
    } else {
      file = this.name;            // the wrapper on PATH; never a hard-coded codex path
      args = ["app-server"];
    }
    const env = { ...process.env, ...this.ctx.config.env, SIDECAR_PERSONA: this.name };
    delete env.SIDECAR_KEY;        // the child never needs the bridge key
    const child = spawn(file, args, { stdio: ["pipe", "pipe", "pipe"], env });
    this.child = child;
    this.childAlive = true;
    this.buf = "";
    // a write that races the child's death must never become an uncaught EPIPE
    child.stdin.on("error", e => this.log("child-stdin-error", { persona: this.name, err: String(e?.message || e) }));
    child.stdout.on("error", () => { /* closed with the child */ });
    child.stderr.on("error", () => { /* closed with the child */ });
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", d => this.onData(d));
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", d => this.log("child-stderr", { persona: this.name, text: String(d).slice(0, 2000) }));
    child.on("error", e => this.onExit(null, null, String(e?.message || e)));
    child.on("exit", (code, signal) => this.onExit(code, signal, null));
    this.log("child-started", { persona: this.name, cmd: file, args });
    if (this.stableTimer) clearTimeout(this.stableTimer);
    this.stableTimer = setTimeout(() => { this.backoffMs = this.ctx.config.backoffMs; }, 60000);
    this.stableTimer.unref?.();
  }

  onExit(code, signal, err) {
    if (!this.childAlive) return;
    this.childAlive = false;
    if (this.stableTimer) { clearTimeout(this.stableTimer); this.stableTimer = null; }
    this.log("child-exit", { persona: this.name, code, signal, err });
    const e = new SidecarError(502, "api_error",
      `the codex app-server for ${this.name} exited (code=${code} signal=${signal})`);
    for (const [, p] of this.pending) p.reject(e);
    this.pending.clear();
    for (const s of [...this.bus.values()]) { s.parked.clear(); s.onFrame({ type: "child-exit", error: e }); s.close("app-server exited"); }
    this.bus.clear();
    this.sessions.clear();
    this.initPromise = null;
    this.child = null;
    this.buf = "";
    if (this.stopped) return;
    const wait = this.backoffMs;
    this.backoffMs = Math.min(this.backoffMs * 2, 30000);
    // bypasses the degraded gate on purpose: a completed restart is what clears `degraded`
    this.setInit((async () => {
      await new Promise(r => { this.restartTimer = setTimeout(r, wait); this.restartTimer.unref?.(); });
      if (this.stopped) throw new SidecarError(503, "api_error", `persona ${this.name} is shutting down`);
      return this.boot();
    })());
  }

  onData(chunk) {
    this.buf += chunk;
    let i;
    while ((i = this.buf.indexOf("\n")) >= 0) {
      const line = this.buf.slice(0, i);
      this.buf = this.buf.slice(i + 1);
      if (!line.trim()) continue;
      let m;
      try { m = JSON.parse(line); } catch { this.log("unparsed-frame", { persona: this.name, line: line.slice(0, 400) }); continue; }
      try { this.onFrame(m); } catch (e) { this.log("frame-error", { persona: this.name, err: String(e?.stack || e) }); }
    }
  }

  onFrame(m) {
    // response to one of our requests
    if (m.id !== undefined && m.method === undefined) {
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      if (m.error) p.reject(mapUpstreamError(m.error, "app-server"));
      else p.resolve(m.result);
      return;
    }
    // server -> client request
    if (m.method && m.id !== undefined) {
      if (m.method === "item/tool/call") {
        const s = this.bus.get(m.params?.threadId);
        if (s) { s.onFrame({ type: "toolcall", rpc: m }); return; }
        this.send({ jsonrpc: "2.0", id: m.id, result: { contentItems: [{ type: "inputText", text: "no active session for this thread" }], success: false } });
        return;
      }
      if (/approval/i.test(m.method)) {
        // approvalPolicy is "never": an approval request means codex tried to execute.
        this.send({ jsonrpc: "2.0", id: m.id, result: { decision: "denied" } });
        const s = this.bus.get(m.params?.threadId);
        this.tripBoundary(m.method, s || null);
        return;
      }
      this.send({ jsonrpc: "2.0", id: m.id, result: {} });
      return;
    }
    // notification
    if (!m.method) return;
    if (m.method === "hook/started") this.hookEvents += 1;
    if (m.method === "mcpServer/startupStatus/updated") this.mcpEvents += 1;
    if (m.method === "account/rateLimits/updated") {
      const hit = rateLimitExhaustion(m.params?.rateLimits);
      this.rateLimit = hit;
      if (hit) {
        const err = new SidecarError(429, "rate_limit_error", `codex account rate limit reached (${hit.reason})`);
        if (hit.retryAfter != null) err.extra.retryAfter = hit.retryAfter;
        this.log("rate-limited", { persona: this.name, reason: hit.reason, retryAfter: hit.retryAfter ?? null });
        // One outbox row per session settled by the exhaustion; the flag makes the next
        // successful turn on this persona publish the matching `{state:'ok'}`.
        this.rateLimited = true;
        for (const s of [...this.bus.values()]) {
          if (!s.outboxKey || !this.ctx.config.outboxPath) continue;
          this.ctx.outbox.record({
            harness: "codex-sidecar", leg: this.name,
            session_id: s.outboxKey.session_id, agent_id: s.outboxKey.agent_id, agent_type: null,
            kind: "rate_limit",
            payload: { state: "exhausted", reason: hit.reason, retry_after_s: hit.retryAfter ?? undefined },
          });
        }
        for (const s of [...this.bus.values()]) s.settle({ kind: "error", error: err });
      }
      return;
    }
    const s = this.bus.get(m.params?.threadId);
    if (s) s.onFrame({ type: "note", frame: m });
  }

  send(obj) {
    if (!this.childAlive || !this.child?.stdin?.writable) return false;
    try { this.child.stdin.write(`${JSON.stringify(obj)}\n`); } catch { return false; }
    return true;
  }

  // `timeoutMs > 0` bounds the await (Defect E2): an app-server that never answers
  // `thread/start`/`turn/start` used to park the HTTP handler forever, because the only
  // guard (`Session.waitOutcome`) is armed after this await returns. The rejection is the
  // same 502 the outcome timeout raises, so it surfaces the ordinary `http-error` row.
  rpc(method, params, timeoutMs = 0) {
    return new Promise((resolve, reject) => {
      const id = this.rpcId++;
      let timer = null;
      // settling always drops the pending entry, so a late response is ignored, never a crash
      const once = fn => v => {
        if (timer) { clearTimeout(timer); timer = null; }
        this.pending.delete(id);
        fn(v);
      };
      const entry = { resolve: once(resolve), reject: once(reject) };
      this.pending.set(id, entry);
      if (timeoutMs > 0) {
        timer = setTimeout(() => {
          entry.reject(new SidecarError(502, "api_error",
            `timed out waiting for the codex app-server (${method})`));
        }, timeoutMs);
        timer.unref?.();
      }
      if (!this.send({ jsonrpc: "2.0", id, method, params })) {
        entry.reject(new SidecarError(502, "api_error", `the codex app-server for ${this.name} is not running`));
      }
    });
  }

  // The security boundary: an out-of-band item type (or an approval request) means
  // codex has its own execution surface live. Interrupt, fail closed, degrade, restart.
  tripBoundary(what, session) {
    this.log("security-event", {
      persona: this.name, itemType: what, threadId: session?.threadId ?? null,
      action: "turn/interrupt + degrade + restart",
    });
    this.degraded = true;
    this.degradedReason = what;
    const err = new SidecarError(502, "api_error",
      `out-of-band execution attempt (${what}) from the codex app-server; the turn was interrupted and persona ${this.name} marked degraded`);
    for (const s of [...this.bus.values()]) {
      if (s.threadId && s.turnId) this.send({ jsonrpc: "2.0", id: this.rpcId++, method: "turn/interrupt", params: { threadId: s.threadId, turnId: s.turnId } });
      s.settle({ kind: "error", error: err });
    }
    // an interrupted child does not exit on its own; kill it so the restart path runs
    setTimeout(() => { try { this.child?.kill("SIGTERM"); } catch { /* ignore */ } }, 50).unref?.();
  }

  interrupt(session, reason) {
    if (session?.threadId && session?.turnId) {
      this.send({ jsonrpc: "2.0", id: this.rpcId++, method: "turn/interrupt", params: { threadId: session.threadId, turnId: session.turnId } });
      this.log("turn-interrupt", { persona: this.name, threadId: session.threadId, reason });
    }
  }

  dropSession(session) {
    if (!session) return;
    session.close("session dropped");
    this.sessions.delete(session.key);
  }

  stop() {
    this.stopped = true;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    if (this.stableTimer) clearTimeout(this.stableTimer);
    for (const s of [...this.sessions.values()]) s.close("sidecar shutting down");
    try { this.child?.kill("SIGTERM"); } catch { /* ignore */ }
  }
}

// ------------------------------------------------ thread config verifier ----

// `config` silently ignores unknown keys, so the trim is verified at every thread/start.
export function verifyInstructionSources(sources) {
  const list = Array.isArray(sources) ? sources : [];
  if (list.length > 1) return `expected at most the persona AGENTS.md, got ${list.length} instruction sources`;
  const bad = list.filter(s => !/AGENTS\.md$/i.test(typeof s === "string" ? s : JSON.stringify(s)));
  if (bad.length) return `unexpected instruction source: ${String(bad[0]).slice(0, 200)}`;
  return null;
}

// ------------------------------------------------------------ HTTP layer ----

function sendJson(res, status, obj, headers = {}) {
  res.writeHead(status, { "content-type": "application/json", ...headers });
  res.end(JSON.stringify(obj));
}

function sendError(res, err) {
  const headers = {};
  if (err.extra?.retryAfter != null) headers["retry-after"] = String(err.extra.retryAfter);
  sendJson(res, err.status, err.body(), headers);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let n = 0;
    req.on("data", d => {
      n += d.length;
      if (n > BODY_LIMIT) { reject(new SidecarError(413, "invalid_request_error", "request body exceeds 32 MiB")); req.destroy(); return; }
      chunks.push(d);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", e => reject(asSidecarError(e)));
  });
}

// A request's identity is a DIGEST of everything that decides what the model would do
// with it — model id, resolved effort, system text, the tool list, and the whole parsed
// message array (every content byte, `is_error` included). Shape is not identity: the same
// message count echoing the same `tool_use_id`s with a *different* `tool_result.content` is
// a genuinely different request, and joining it to an in-flight turn (or answering it from
// a replayed outcome) would silently drop it and return a stale answer — acceptance-audit
// defects B1/B2, 2026-09-13. Only a byte-for-byte re-send of the execution-affecting request
// earns a JOIN or a replay; anything else supersedes.
//
// `canon` gives JSON.stringify a stable key order so two structurally identical bodies that
// differ only in property order digest the same (no dependency; arrays keep their order,
// because message and block order is semantic).
const canon = v => Array.isArray(v)
  ? v.map(canon)
  : (v && typeof v === "object")
    ? Object.fromEntries(Object.keys(v).sort().map(k => [k, canon(v[k])]))
    : v;
//
// Phase 4 (measured 2026-09-14 via `msgDiff`): the client issues the SAME turn twice within
// ~15 ms in two decorations — one send carries `cache_control` on the last `tool_use` block
// and a harness-injected trailing `text` block after the last `tool_result` (a system
// reminder), the other carries neither. Byte-identity called them different, so they
// superseded each other in a perfect ping-pong (53/53 on one audit; 0 joins) and the head of
// every long turn's text was lost. Identity therefore digests the *undecorated* request:
// `cache_control` is dropped everywhere (a caching hint, never model input the turn depends
// on) and `text` blocks that FOLLOW a `tool_result` in a user message are dropped (harness
// decoration beside the result, not the result). A changed `tool_result` payload, a changed
// user prompt, a changed model/effort/system/tool list still supersede (B1/B2 hold).
const undecorateBlock = b => {
  if (!b || typeof b !== "object" || !("cache_control" in b)) return b;
  const { cache_control: _cc, ...rest } = b;
  return rest;
};
const undecorateMessage = m => {
  if (!m || !Array.isArray(m.content)) return m;
  let afterResult = false;
  const content = [];
  for (const b of m.content) {
    if (m.role === "user" && afterResult && b?.type === "text") continue;
    if (b?.type === "tool_result") afterResult = true;
    content.push(undecorateBlock(b));
  }
  return { ...m, content };
};
const undecorate = ({ model, effort, system, tools, messages }) => ({
  model, effort, system,
  tools: Array.isArray(tools) ? tools.map(undecorateBlock) : tools,
  messages: Array.isArray(messages) ? messages.map(undecorateMessage) : messages,
});
const requestDigest = fields => crypto.createHash("sha256")
  .update(JSON.stringify(canon(undecorate(fields))))
  .digest("hex");
const shortDigest = d => (d ? d.slice(0, 12) : null);

const isDuplicateRequest = (owner, token) => !!owner
  && owner.mode === token.mode          // cheap pre-check; the digest is the decision
  && owner.digest === token.digest;

// A straggler: a retry still in the client's backoff queue when the turn it belongs to
// completed. Every id it echoes is answered, nothing is parked, no turn is open, and it is
// the same request by digest — so its answer is that turn's outcome, replayed.
const isStragglerRetry = (sess, token) => {
  const lo = sess.lastOutcome;
  return !!lo && !sess.turnOpen && sess.parked.size === 0
    && token.toolResultIds.length > 0
    && token.digest === lo.digest
    && token.toolResultIds.every(id => sess.delivered.has(id));
};

async function handleMessages(ctx, persona, body, req, res) {
  const cfg = ctx.config;
  const systemText = systemTextOf(body);
  const allMsgs = Array.isArray(body.messages) ? body.messages : [];
  const rawCwd = extractCwd(allMsgs, systemText);
  const interleavedText = interleavedSystemText(allMsgs);
  const msgs = conversational(allMsgs);

  // (i) local session-title answer — before model validation, no thread opened
  if (isTitleRequest(body, systemText, msgs)) {
    ctx.logger.write("title-local", { persona: persona.name, stream: !!body.stream, envelope: titleEnvelope(msgs) });
    const usage = { input_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 0 };
    if (body.stream) {
      const msg = { ...messageSkeleton(body.model), usage };
      res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
      const w = (t, o) => res.write(`event: ${t}\ndata: ${JSON.stringify(o)}\n\n`);
      w("message_start", { type: "message_start", message: msg });
      w("content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } });
      w("content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Session" } });
      w("content_block_stop", { type: "content_block_stop", index: 0 });
      w("message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage });
      w("message_stop", { type: "message_stop" });
      res.end();
      return;
    }
    sendJson(res, 200, {
      ...messageSkeleton(body.model),
      content: [{ type: "text", text: "Session" }],
      stop_reason: "end_turn",
      usage,
    });
    return;
  }

  if (hasImageBlock(body)) throw new SidecarError(400, "invalid_request_error", "images unsupported by the codex app-server leg");

  const parsed = parseModelId(body.model);
  if (!parsed) {
    throw new SidecarError(400, "invalid_request_error",
      `unknown model id "${String(body.model ?? "")}" — expected one of ${Object.keys(MODEL_FAMILIES).join(", ")} with an optional -low/-medium/-high suffix`);
  }
  // Effort is resolved per request, not per thread: it rides on every `turn/start` and takes
  // effect for that turn only (measured 2026-09-13 — low/high/low on one open thread gave
  // 0/87/0 reasoning tokens). `thread/start` carries no effort field.
  const eff = resolveEffort(parsed, body);

  const firstUser = msgs.find(m => m.role === "user");
  const { key, weak, scope } = sessionKeyOf(body, systemText, textOf(firstUser?.content), req.headers);
  if (weak) ctx.logger.write("weak-session-key", { persona: persona.name, key, reason: "metadata.user_id carried no session_id" });

  await persona.ensure();

  const last = msgs[msgs.length - 1];
  const lastBlocks = blocksOf(last?.content);
  const toolResults = lastBlocks.filter(b => b?.type === "tool_result");

  let sess = persona.sessions.get(key);
  let mode;
  if (!sess) {
    sess = new Session(persona, key);
    persona.sessions.set(key, sess);
    mode = "new-thread";
  } else if (toolResults.length && (sess.parked.size
      || (sess.turnOpen && toolResults.some(tr => sess.delivered.has(tr.tool_use_id))))) {
    // Defect E1: an echo of an id we handed out belongs to the turn that is still open,
    // even if nothing is parked any more — never `turn/start` onto a blocked turn.
    mode = "tool-result";
  } else if (msgs.length < sess.msgCount) {
    ctx.logger.write("rewind", { persona: persona.name, key, seen: sess.msgCount, now: msgs.length, threadId: sess.threadId });
    persona.interrupt(sess, "rewind");
    persona.dropSession(sess);          // the abandoned thread is simply dropped
    sess = new Session(persona, key);
    persona.sessions.set(key, sess);
    mode = "new-thread-after-rewind";
  } else {
    if (sess.parked.size) {
      ctx.logger.write("dropped-tool-calls", { persona: persona.name, key, n: sess.parked.size });
      sess.abandonParked("no result returned by the client");
    }
    mode = "next-turn";
  }
  sess.msgCount = msgs.length;
  sess.outboxKey = outboxKeyOf(key, weak);
  const reqId = crypto.randomUUID().slice(0, 8);
  ctx.logger.write("http-start", { persona: persona.name, key, reqId, mode, stream: !!body.stream, nMessages: msgs.length });

  const sink = body.stream
    ? new SseSink(res, body.model, cfg.pingMs)
    : new BufferSink(res, body.model);
  // One `http` row per request, same shape on every path (owner, joiner, replay).
  const logHttp = ({ stopReason, toolCalls, usage, ...extra }) => {
    ctx.logger.write("http", {
      persona: persona.name, path: `/${persona.name}/v1/messages`, key, reqId, scope, mode, stream: !!body.stream,
      model: parsed.model, effort: eff.effort, effortSource: eff.effortSource, threadId: sess.threadId, stop_reason: stopReason,
      nMessages: msgs.length, nSystemRole: allMsgs.length - msgs.length, nTools: (body.tools || []).length,
      toolCalls, usage: usageOut(usage), usageCodex: usage, headers: redactHeaders(req.headers), ...extra,
    });
    // A joiner is a second view of the owner's turn, not a turn of its own: no outbox row.
    if (!extra.joinedTo) outboxTurn(ctx, persona, sess, { model: parsed.model, effort: eff.effort, stopReason });
  };

  // One request drives a session at a time. A second in-flight request on the same session
  // is, in practice, the client re-sending the *same* body: the 2026-09-13 storm was 89
  // byte-identical `tool-result` retries in 13 minutes, each superseding the previous with a
  // 409 the Anthropic SDK treats as retryable — a livelock (spike record, WP-5 phase 1).
  // So a duplicate JOINS the in-flight turn as a second sink (fix shape (ii), user decision
  // 2026-09-13): no 409, no error frame, no orphan row, no retry signal of any kind, and
  // every attached connection receives the same outcome. Only a genuinely different second
  // request still supersedes the older one loudly (409 + `http-error` row).
  const token = {
    reqId, mode, nMessages: msgs.length,
    // ids only for the straggler guards ("every id it echoes is already answered"); they are
    // never the identity — the digest is.
    toolResultIds: toolResults.map(tr => tr.tool_use_id).filter(Boolean).sort(),
    digest: requestDigest({
      model: body.model, effort: eff.effort, system: systemText, tools: body.tools ?? [], messages: msgs,
    }),
    // Diagnostic (2026-09-14): per-component digests, logged on `superseded-request` only, so a
    // storm of shape-identical non-joining duplicates names the component that differs.
    parts: {
      model: shortDigest(requestDigest({ model: body.model })),
      effort: shortDigest(requestDigest({ effort: eff.effort })),
      system: shortDigest(requestDigest({ system: systemText })),
      tools: shortDigest(requestDigest({ tools: body.tools ?? [] })),
      messages: shortDigest(requestDigest({ messages: msgs })),  // canonical (undecorated) — equal here means the raw shapes below differ only by decoration
    },
    // Per-message structural summary (never content): digest, role, and per block its type,
    // sorted key list and text length — enough to say WHERE two shape-identical sends differ.
    msgShape: msgs.map(m => ({
      d: crypto.createHash("sha256").update(JSON.stringify(canon(m))).digest("hex").slice(0, 12), role: m.role,   // RAW, so decoration-only differences still show in msgDiff
      blocks: (Array.isArray(m.content) ? m.content : [{ type: "text", text: String(m.content ?? "") }]).map(b => ({
        type: b.type, keys: Object.keys(b).sort().join(","),
        len: typeof b.text === "string" ? b.text.length : typeof b.content === "string" ? b.content.length : Array.isArray(b.content) ? b.content.length : null,
      })),
    })),
  };
  // The indices at which two per-message summaries differ, with both sides' block summaries.
  const msgDiff = (a, b) => (a && b) ? a.map((m, i) => [i, m, b[i]]).filter(([, x, y]) => !y || x.d !== y.d).slice(0, 4)
    .map(([i, x, y]) => ({ i, role: x.role, req: x.blocks, owner: y?.blocks ?? null })) : null;
  const superseded = () => new SidecarError(409, "api_error", "superseded by a newer request on this session");

  // A straggler retry of a turn that has already completed: replay its outcome with ZERO
  // usage — the reconciliation ledger was charged once, when the turn actually ran.
  if (!sess.owner && isStragglerRetry(sess, token)) {
    const lo = sess.lastOutcome;
    ctx.logger.write("replayed-outcome", { persona: persona.name, key, reqId, ownerReqId: lo.reqId, reqDigest: shortDigest(token.digest) });
    sink.start(sess.lagUsage());
    for (const b of lo.blocks) {
      if (b.type === "text") sink.textDelta(b.text);
      else sink.toolUse(b);
    }
    sink.finish(lo.stopReason, ZERO_USAGE);
    logHttp({ stopReason: lo.stopReason, toolCalls: lo.toolCalls, usage: ZERO_USAGE });
    return;
  }

  // The join. Everything up to `sess.joiners.add` is synchronous, so a joiner is never
  // registered after the owner has finished settling its joiners.
  if (isDuplicateRequest(sess.owner, token)) {
    const ownerReqId = sess.owner.reqId;
    ctx.logger.write("joined-request", {
      persona: persona.name, key, reqId, ownerReqId, mode, stream: !!body.stream, reqDigest: shortDigest(token.digest),
    });
    let settleJoin;
    const joined = new Promise(r => { settleJoin = r; });
    // `ownerToken` binds this joiner to the exact request it joined. `sess.joiners` is
    // session-global and may hold another owner's joiners, so every consumer filters on it
    // (audit defect B3): nobody inherits a joiner, and nobody else's joiner is written to.
    const joiner = { token, ownerToken: sess.owner, sink, settled: false, settle(o) { if (this.settled) return; this.settled = true; settleJoin(o); } };
    sink.start(sess.lagUsage());
    const missed = sess.turnText.join("");      // text this request already emitted — never truncate a late joiner
    if (missed) sink.textDelta(missed);
    sess.joiners.add(joiner);
    const onJoinerClose = () => {
      if (sink.done) return;                    // the outcome is already on the wire
      sink.abort();
      sess.joiners.delete(joiner);
      // The owner is gone and this was the last connection holding the turn open: only now
      // does the abort path apply (with a live connection attached it never does).
      if (sess.ownerGone && !sess.joinersOf(joiner.ownerToken).length) {
        persona.interrupt(sess, "client disconnect");
        persona.dropSession(sess);
        sess.settle({ kind: "error", error: new SidecarError(499, "api_error", "client disconnected") });
      }
      joiner.settle({ kind: "closed" });
    };
    res.on("close", onJoinerClose);
    try {
      const o = await joined;
      if (o.kind === "closed") return;
      if (o.kind === "error") {
        ctx.logger.write("http-error", {
          persona: persona.name, key, reqId, mode, status: o.error.status, type: o.error.anthropicType,
          message: o.error.message, headers: redactHeaders(req.headers), joinedTo: ownerReqId,
        });
        return;
      }
      // Same figures as the owner's row: the ledger is charged once per turn, and a joiner
      // row is a second view of that one charge, not a second charge.
      logHttp({ stopReason: o.stopReason, toolCalls: o.toolCalls, usage: o.usage, joinedTo: ownerReqId });
    } finally {
      sess.joiners.delete(joiner);
      res.off("close", onJoinerClose);
    }
    return;
  }

  if (sess.owner) {
    const oldOwner = sess.owner;
    ctx.logger.write("superseded-request", { persona: persona.name, key, reqId, supersededReqId: oldOwner.reqId, mode, nMessages: msgs.length, ownerNMessages: oldOwner.nMessages, reqDigest: shortDigest(token.digest), ownerDigest: shortDigest(oldOwner.digest), parts: token.parts, ownerParts: oldOwner.parts, msgDiff: msgDiff(token.msgShape, oldOwner.msgShape) });
    // A superseded owner takes its joiners with it (audit defect B3). They joined *that*
    // request; they are no audience for this one, and leaving them attached would deliver
    // this request's text and outcome to a connection that never asked for it.
    for (const j of sess.joinersOf(oldOwner)) {
      sess.joiners.delete(j);
      const err = superseded();
      j.sink.error(err);              // its own await path logs the 409 `http-error` row
      j.settle({ kind: "error", error: err });
    }
    sess.preempt(superseded());
  }
  sess.owner = token;
  sess.ownerGone = false;
  sess.turnText.length = 0;       // per request: a joiner is replayed only what *this* request emitted
  sess.attach(sink);
  const stillMine = () => { if (sess.owner !== token) throw superseded(); };
  let joiners = [];               // duplicates this request answers alongside its own sink
  let dedupedToolResults = 0;     // re-sent tool_results silently absorbed (see below)

  let closedByClient = false;
  const onClose = () => {
    if (sink.done) return;
    closedByClient = true;
    sink.abort();
    if (sess.owner !== token) return;   // a superseded request never tears down its successor's session
    const mine = sess.joinersOf(token);
    if (mine.length) {
      // Promotion: the owner's socket is gone but a duplicate is still holding on, so the
      // turn keeps running and the first live joiner's sink becomes the session's sink.
      // The request token is deliberately NOT transferred — this handler is still the one
      // driving the turn, and it delivers the outcome to everyone attached.
      sess.ownerGone = true;
      sess.detach(sink);
      sess.attach(mine[0].sink);
      return;
    }
    persona.interrupt(sess, "client disconnect");
    persona.dropSession(sess);
    sess.settle({ kind: "error", error: new SidecarError(499, "api_error", "client disconnected") });
  };
  res.on("close", onClose);

  try {
    if (mode.startsWith("new-thread")) {
      const cwd = resolveCwd(ctx, rawCwd);
      const hookBefore = persona.hookEvents;
      const mcpBefore = persona.mcpEvents;
      const params = {
        cwd,
        model: parsed.model,
        approvalPolicy: "never",
        sandbox: "read-only",
        ephemeral: true,
        baseInstructions: baseInstructionsOf(systemText, interleavedText),
        dynamicTools: toDynamicTools(body.tools),
        config: THREAD_CONFIG,
      };
      const started = await persona.rpc("thread/start", params, cfg.turnTimeoutMs);
      stillMine();
      sess.threadId = started?.thread?.id ?? started?.threadId ?? null;
      sess.model = parsed.model;
      if (!sess.threadId) throw new SidecarError(502, "api_error", "thread/start returned no thread id");
      persona.bus.set(sess.threadId, sess);

      // (d) post-start verification — fail closed
      const problem = verifyInstructionSources(started?.instructionSources)
        || (persona.hookEvents !== hookBefore ? "a hook ran during thread/start" : null)
        || (persona.mcpEvents !== mcpBefore ? "an MCP server started during thread/start" : null);
      if (problem) {
        ctx.logger.write("config-unverified", { persona: persona.name, threadId: sess.threadId, stage: "thread/start", problem, instructionSources: started?.instructionSources ?? null });
        persona.dropSession(sess);
        throw new SidecarError(502, "api_error", `thread config not honoured: ${problem}`);
      }
      ctx.logger.write("config-verified", {
        persona: persona.name, threadId: sess.threadId, key, scope, cwd, model: parsed.model,
        effort: eff.effort, effortSource: eff.effortSource,
        instructionSources: started?.instructionSources ?? [], nTools: params.dynamicTools.length,
        preambleChars: PREAMBLE.length, systemChars: systemText.length, interleavedSystemChars: interleavedText.length,
      });
    }

    if (mode === "tool-result") {
      for (const tr of toolResults) {
        const text = typeof tr.content === "string" ? tr.content : textOf(tr.content) || JSON.stringify(tr.content ?? "");
        if (sess.answerParked(tr.tool_use_id, text, !tr.is_error)) continue;
        // Already answered on this thread (a re-sent result, 93 such rows in the storm
        // window): a silent no-op, not an orphan. Only an id we never handed out is one.
        if (sess.delivered.has(tr.tool_use_id)) { dedupedToolResults += 1; continue; }
        ctx.logger.write("orphan-tool-result", { persona: persona.name, key, tool_use_id: tr.tool_use_id });
      }
      sess.abandonDelivered("no result returned by the client");
    } else {
      const text = turnInputText(mode, msgs, last, lastBlocks);
      const turnParams = { threadId: sess.threadId, input: [{ type: "text", text }], effort: eff.effort };
      if (sess.model && parsed.model !== sess.model) { turnParams.model = parsed.model; sess.model = parsed.model; }
      // A mid-thread effort change is legal and costs exactly one prefix-cache miss on the
      // changed turn (measured 2026-09-13: cachedInputTokens 0 on that turn, neighbours cached).
      // So it is logged and nothing else — no new thread, no interrupt.
      if (sess.lastEffort !== null && sess.lastEffort !== eff.effort) {
        ctx.logger.write("effort-changed", {
          persona: persona.name, threadId: sess.threadId, key,
          from: sess.lastEffort, to: eff.effort, source: eff.effortSource,
        });
      }
      sess.deltaItems.clear();
      sess.pendingOutcomes = [];      // a stray outcome never leaks into a fresh turn
      const turn = await persona.rpc("turn/start", turnParams, cfg.turnTimeoutMs);
      sess.lastEffort = eff.effort;   // what this thread last ran at, for the next turn's compare
      stillMine();
      sess.turnId = turn?.turn?.id ?? turn?.turnId ?? sess.turnId;
    }

    stillMine();
    sink.start(sess.lagUsage());
    const out = await sess.waitOutcome(cfg.turnTimeoutMs);
    // The audience is everyone still attached: this request's sink plus every duplicate
    // that joined the turn (a promoted joiner is `sess.sink` as well, hence the dedupe).
    // Only the request still driving the session owns the joiners — a request that was
    // superseded meanwhile answers its own sink and nobody else's.
    if (sess.owner === token) {
      joiners = sess.joinersOf(token);
      for (const j of joiners) sess.joiners.delete(j);
      sess.detach(sess.sink);                          // a promoted joiner's sink, if any
    }
    sess.detach(sink);
    const audience = [sink, ...joiners.map(j => j.sink)].filter((sk, i, a) => sk && !sk.done && a.indexOf(sk) === i);
    // The client-abort path is unchanged only when nothing else is attached.
    if (closedByClient && !joiners.length) return;

    if (out.kind === "error") throw out.error;
    let stopReason = "end_turn";
    const toolCalls = out.kind === "tool_use" ? out.batch.map(c => c.params.tool) : [];
    if (out.kind === "tool_use") {
      stopReason = "tool_use";
      for (const call of out.batch) {
        sess.delivered.add(call.params.callId);
        const block = { type: "tool_use", id: call.params.callId, name: call.params.tool, input: call.params.arguments ?? {} };
        for (const sk of audience) sk.toolUse(block);
      }
      if (out.batch.length > 1) ctx.logger.write("parallel-tool-batch", { persona: persona.name, threadId: sess.threadId, n: out.batch.length });
    }
    sess.turnOpen = stopReason === "tool_use";
    const usage = sess.reconcile();                     // once per turn, whatever is attached
    for (const sk of audience) sk.finish(stopReason, usage);
    // What a straggler retry of this exact request gets replayed, should one still be in a
    // client backoff queue (blocks in emission order: this request's text, then its calls).
    const emitted = sess.turnText.join("");
    sess.lastOutcome = {
      reqId, digest: token.digest, stopReason, toolCalls,
      blocks: [
        ...(emitted ? [{ type: "text", text: emitted }] : []),
        ...(out.kind === "tool_use" ? out.batch.map(c => ({ type: "tool_use", id: c.params.callId, name: c.params.tool, input: c.params.arguments ?? {} })) : []),
      ],
    };
    logHttp({ stopReason, toolCalls, usage, ...(dedupedToolResults ? { dedupedToolResults } : {}) });
    for (const j of joiners) j.settle({ kind: "done", stopReason, toolCalls, usage });
  } catch (e) {
    if (sess.owner === token) {
      for (const j of sess.joinersOf(token)) { joiners.push(j); sess.joiners.delete(j); }
      sess.turnOpen = false;
      sess.detach(sess.sink);
    }
    sess.detach(sink);
    const err = asSidecarError(e);
    for (const j of joiners) j.settle({ kind: "error", error: err });
    const audience = [sink, ...joiners.map(j => j.sink)].filter((sk, i, a) => sk && !sk.done && a.indexOf(sk) === i);
    if (closedByClient && !joiners.length) return;
    ctx.logger.write("http-error", {
      persona: persona.name, key, reqId, mode, status: err.status, type: err.anthropicType,
      message: err.message, headers: redactHeaders(req.headers),
    });
    for (const sk of audience) sk.error(err);
  } finally {
    const driving = sess.owner === token;
    if (driving) { sess.owner = null; sess.ownerGone = false; }
    res.off("close", onClose);
    // No joiner outlives the turn that was driving it (settle is idempotent).
    const stalled = new SidecarError(502, "api_error", "the turn driving this joined request ended without an outcome");
    for (const j of [...sess.joinersOf(token), ...joiners]) {
      sess.joiners.delete(j);
      if (!j.settled) { j.sink.error(stalled); j.settle({ kind: "error", error: stalled }); }
    }
  }
}

function turnInputText(mode, msgs, last, lastBlocks) {
  let text;
  if (mode.startsWith("new-thread")) {
    text = msgs.filter(m => m.role === "user").map(m => textOf(m.content)).filter(Boolean).join("\n\n");
  } else {
    text = textOf(last?.content);
    if (!text.trim()) {
      // the client may answer with tool_result blocks after the thread was dropped
      text = lastBlocks.filter(b => b?.type === "tool_result")
        .map(b => (typeof b.content === "string" ? b.content : textOf(b.content))).filter(Boolean).join("\n\n");
    }
  }
  return text.trim() ? text : "(empty user message)";
}

function resolveCwd(ctx, rawCwd) {
  const candidates = [rawCwd, ctx.config.cwdFallback];
  for (const c of candidates) {
    if (!c) continue;
    try { if (fs.statSync(c).isDirectory()) return c; } catch { /* next */ }
  }
  // last resort: an empty directory, so no stray AGENTS.md is loaded
  const empty = path.join(ctx.config.stateDir, "empty-cwd");
  fs.mkdirSync(empty, { recursive: true, mode: 0o700 });
  if (rawCwd) ctx.logger.write("cwd-fallback", { requested: rawCwd, used: empty });
  return empty;
}

function healthBody(ctx) {
  const personas = {};
  let degraded = false;
  for (const p of ctx.personas.values()) {
    personas[p.name] = { alive: p.alive, degraded: p.degraded };
    if (p.degraded) degraded = true;
  }
  // A bind shortfall is a lost capability (the router container reaches us only through
  // the docker0 bind), so it degrades health exactly like a degraded persona would. The
  // lists are exposed so the verdict is checkable rather than asserted.
  const expected = ctx.binds?.expected ?? [];
  const bound = ctx.binds?.bound ?? [];
  const missing = expected.filter(b => !bound.includes(b));
  if (missing.length) degraded = true;
  return { status: degraded ? "degraded" : "ok", version: VERSION, personas, binds: { expected, bound, missing }, degraded };
}

function makeHandler(ctx) {
  return async (req, res) => {
    const url = new URL(req.url || "/", "http://sidecar.invalid");
    const pathname = url.pathname;          // the query string (?beta=true) is ignored
    try {
      // auth runs before body parsing: the shared key is the boundary for bridge peers
      if (!safeEqual(presentedKey(req.headers), ctx.config.key)) {
        ctx.logger.write("auth-failed", { path: pathname, method: req.method, headers: redactHeaders(req.headers) });
        req.resume();
        sendError(res, new SidecarError(401, "authentication_error", "missing or invalid sidecar key"));
        return;
      }

      if (pathname === "/healthz") {
        req.resume();
        const body = healthBody(ctx);
        sendJson(res, body.degraded ? 503 : 200, body);
        return;
      }

      const seg = pathname.split("/").filter(Boolean);
      const persona = seg.length ? ctx.personas.get(seg[0]) : null;
      const rest = `/${seg.slice(1).join("/")}`;
      if (!persona) {
        req.resume();
        sendError(res, new SidecarError(404, "not_found_error", `unknown persona in path ${pathname}`));
        return;
      }

      if (rest === "/api/hello" && (req.method === "GET" || req.method === "HEAD")) {
        req.resume();
        ctx.logger.write("hello", { persona: persona.name, method: req.method });
        res.writeHead(200, { "content-type": "application/json" });
        res.end(req.method === "HEAD" ? undefined : "{}");
        return;
      }

      if (req.method !== "POST" || (rest !== "/v1/messages" && rest !== "/v1/messages/count_tokens")) {
        req.resume();
        sendError(res, new SidecarError(404, "not_found_error", `unsupported path ${pathname}`));
        return;
      }

      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch { throw new SidecarError(400, "invalid_request_error", "request body is not valid JSON"); }

      if (rest === "/v1/messages/count_tokens") {
        const n = countTokensEstimate(body);
        ctx.logger.write("count_tokens", { persona: persona.name, input_tokens: n });
        sendJson(res, 200, { input_tokens: n });
        return;
      }

      await handleMessages(ctx, persona, body, req, res);
    } catch (e) {
      const err = asSidecarError(e);
      ctx.logger.write("http-error", { path: pathname, status: err.status, type: err.anthropicType, message: err.message });
      if (!res.headersSent) sendError(res, err);
      else if (!res.writableEnded) res.end();
    }
  };
}

// ------------------------------------------------------------------ boot ----

export async function startSidecar(env = process.env) {
  const config = readConfig(env);
  if (!config.key) throw new Error("SIDECAR_KEY is required");
  const logger = new Logger(config.logDir, config.logKeepDays);
  // One outbox writer per process; an unset `SIDECAR_OUTBOX_PATH` makes it a no-op that
  // opens nothing. `outboxSpawned` is the per-process set of subagent keys already announced.
  const outbox = createOutboxWriter({ path: config.outboxPath });
  const ctx = { config, logger, outbox, outboxSpawned: new Set(), personas: new Map() };
  for (const name of config.personas) ctx.personas.set(name, new Persona(name, ctx));

  const handler = makeHandler(ctx);
  const servers = [];
  const listen = (server, host, port) => new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => { server.off("error", reject); resolve(server.address().port); });
  });

  const primary = http.createServer(handler);
  const port = await listen(primary, "127.0.0.1", config.port);
  servers.push(primary);
  const bindOf = s => `${s.address().address}:${s.address().port}`;
  // Truth about which binds are up lives here and feeds /healthz (bind-aware health).
  const binds = { expected: [bindOf(primary)], bound: [bindOf(primary)] };
  ctx.binds = binds;

  // The extra bind (docker0 gateway) can legitimately fail at boot: a user unit cannot
  // order after the system docker.service, and until docker creates docker0 the address
  // does not exist (EADDRNOTAVAIL). The first attempt is inline so the happy path is
  // unchanged; a failure starts a bounded background retry while /healthz reports
  // degraded, so the shortfall is never silent. The primary loopback bind stays fail-fast.
  let bindRetryTimer = null;
  if (config.bindExtra) {
    const target = `${config.bindExtra}:${port}`;
    binds.expected.push(target);
    const tryExtra = async () => {
      const extra = http.createServer(handler);
      await listen(extra, config.bindExtra, port);
      servers.push(extra);
      binds.bound.push(bindOf(extra));
    };
    try {
      await tryExtra();
    } catch (first) {
      const startedAt = Date.now();
      let attempts = 1;
      let delay = Math.max(1, config.bindRetryMs);
      logger.write("bind-extra-retrying", {
        host: config.bindExtra, port, err: String(first?.message || first), budgetMs: config.bindRetryBudgetMs,
      });
      const schedule = () => {
        bindRetryTimer = setTimeout(async () => {
          bindRetryTimer = null;
          attempts += 1;
          try {
            await tryExtra();
            logger.write("bind-extra-recovered", { host: config.bindExtra, port, attempts, elapsedMs: Date.now() - startedAt });
          } catch (e) {
            if (Date.now() - startedAt + delay > config.bindRetryBudgetMs) {
              logger.write("bind-extra-failed", {
                host: config.bindExtra, port, attempts, elapsedMs: Date.now() - startedAt, err: String(e?.message || e),
              });
              return;
            }
            delay = Math.min(delay * 2, 5000);
            schedule();
          }
        }, delay);
        bindRetryTimer.unref?.();
      };
      schedule();
    }
  }

  logger.write("listen", {
    port, binds: binds.bound, expected: binds.expected,
    personas: [...ctx.personas.keys()], logDir: config.logDir, keepDays: config.logKeepDays,
  });

  const close = async () => {
    if (bindRetryTimer) { clearTimeout(bindRetryTimer); bindRetryTimer = null; }
    for (const p of ctx.personas.values()) p.stop();
    await Promise.all(servers.map(s => new Promise(resolve => {
      s.close(resolve);
      // server.close() on its own waits forever on idle keep-alive sockets, which
      // would hang `systemctl stop`: let in-flight requests land, then cut the rest.
      try { s.closeIdleConnections(); } catch { /* ignore */ }
      const t = setTimeout(() => { try { s.closeAllConnections(); } catch { /* ignore */ } }, 250);
      t.unref?.();
    })));
    logger.write("shutdown", {});
    logger.close();
    outbox.close();
  };
  return { ctx, config, logger, outbox, port, servers, close, personas: ctx.personas };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  startSidecar().then(h => {
    process.stdout.write(`codex-sidecar ${VERSION} listening on 127.0.0.1:${h.port} personas=${[...h.personas.keys()].join(",")}\n`);
    let stopping = false;
    const bye = () => {
      if (stopping) process.exit(0);
      stopping = true;
      setTimeout(() => process.exit(0), 5000);   // a hung shutdown still exits
      h.close().finally(() => process.exit(0));
    };
    process.on("SIGTERM", bye);
    process.on("SIGINT", bye);
  }).catch(e => {
    process.stderr.write(`codex-sidecar: ${e?.message || e}\n`);
    process.exit(1);
  });
}
