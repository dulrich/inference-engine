// protocol.mjs — the Anthropic wire boundary, as pure functions.
//
// Everything in this file answers a question about ONE request body or ONE usage figure and
// touches nothing else: no session, no child process, no socket, no clock. That is what makes
// it the protocol boundary rather than a grab bag — the request path can be read as a sequence
// precisely because these decisions are values, not effects.
//
// Three groups: model/effort identity, message and system-prompt reading (including the
// session-key discriminator and the title signature), and the codex -> Anthropic usage
// projection with its context-window clamp.
import crypto from "node:crypto";
import { PREAMBLE } from "./config.mjs";


export const MODEL_FAMILIES = { "gpt-5.6-luna": "low", "gpt-5.6-sol": "medium", "gpt-6-astra": "medium", "gpt-6-sol": "medium", "gpt-6-luna": "low" };
// The harness's `output_config.effort` vocabulary folded onto the app-server's low|medium|high.
export const EFFORT_LEVELS = { low: "low", medium: "medium", high: "high", max: "high", xhigh: "high" };
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

export const ZERO_USAGE = { inputTokens: 0, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: 0 };

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

// Backstop on the occupancy figure: no message may claim more context than the model has.
// A correct `reconcile()` already cannot exceed the window, so this only ever fires on a
// codex-side figure we do not understand — it is a guard against re-inflating Claude Code's
// meter, not a correction. Returns null when nothing needs clamping, else the RAW-units
// figure whose `usageOut` projection has its three input columns scaled down proportionally
// (integer floor, remainder to the largest columns) to sum to exactly `window`.
// `output_tokens` is never clamped and no column ever goes negative.
export function clampUsage(raw, window) {
  if (!Number.isFinite(window) || window <= 0) return null;
  const proj = usageOut(raw);
  const cols = ["input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"];
  const sum = cols.reduce((a, k) => a + proj[k], 0);
  if (sum <= window) return null;
  const scaled = cols.map(k => Math.floor((proj[k] * window) / sum));
  let rest = window - scaled.reduce((a, b) => a + b, 0);
  for (const i of cols.map((_, i) => i).sort((a, b) => proj[cols[b]] - proj[cols[a]])) {
    if (rest <= 0) break;
    scaled[i] += 1;
    rest -= 1;
  }
  const [input, cacheRead, cacheWrite] = scaled;
  return {
    inputTokens: input + cacheRead + cacheWrite,   // codex counts the cached share INSIDE inputTokens
    cachedInputTokens: cacheRead,
    cacheWriteInputTokens: cacheWrite,
    outputTokens: raw?.outputTokens ?? 0,
  };
}
