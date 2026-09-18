// request.mjs — phase 1 of the request path: what IS this request, and what should
// happen to the session it names?
//
// Three pure steps, in order, and nothing here writes to a socket, a child or the log:
//
//   normalizeRequest()  the body read once into the view every later phase uses
//   requestToken()      that view's IDENTITY — the digest that decides join vs supersede
//   dispositionOf()     the ONE decision site: replay | join | drive(+supersedes)
//
// `dispositionOf` is deliberately a single function returning a single value. The three
// outcomes are mutually exclusive readings of one session state, and the moment they become
// separate booleans threaded through the handler the replay/join/supersede behaviour stops
// being characterizable — which is the regression this file exists to prevent.
import crypto from "node:crypto";
import {
  blocksOf, textOf, systemTextOf, conversational, interleavedSystemText, extractCwd,
  parseModelId, resolveEffort, sessionKeyOf, MODEL_FAMILIES,
} from "./protocol.mjs";
import { SidecarError } from "./errors.mjs";

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
export const requestDigest = fields => crypto.createHash("sha256")
  .update(JSON.stringify(canon(undecorate(fields))))
  .digest("hex");
export const shortDigest = d => (d ? d.slice(0, 12) : null);

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

// ---------------------------------------------------------- normalization ----

// The body, read once. Every later phase reads this view instead of re-deriving fields from
// `body`, so "what the request says" is decided in exactly one place.
export function normalizeRequest(body, headers) {
  const systemText = systemTextOf(body);
  const allMsgs = Array.isArray(body.messages) ? body.messages : [];
  const msgs = conversational(allMsgs);
  const last = msgs[msgs.length - 1];
  const lastBlocks = blocksOf(last?.content);
  return {
    body,
    headers: headers ?? {},
    systemText,
    allMsgs,
    msgs,
    last,
    lastBlocks,
    // the cwd is parsed *before* the system-role filter drops the environment message
    rawCwd: extractCwd(allMsgs, systemText),
    interleavedText: interleavedSystemText(allMsgs),
    toolResults: lastBlocks.filter(b => b?.type === "tool_result"),
    stream: !!body.stream,
  };
}

// Model, effort and session key: who this request is for. Throws the 400 an unknown model id
// has always thrown. Effort is resolved per request, not per thread: it rides on every
// `turn/start` and takes effect for that turn only (measured 2026-09-13 — low/high/low on one
// open thread gave 0/87/0 reasoning tokens). `thread/start` carries no effort field.
export function resolveIdentity(norm) {
  const { body, systemText, msgs, headers } = norm;
  const parsed = parseModelId(body.model);
  if (!parsed) {
    throw new SidecarError(400, "invalid_request_error",
      `unknown model id "${String(body.model ?? "")}" — expected one of ${Object.keys(MODEL_FAMILIES).join(", ")} with an optional -low/-medium/-high suffix`);
  }
  const eff = resolveEffort(parsed, body);
  const firstUser = msgs.find(m => m.role === "user");
  const { key, weak, scope } = sessionKeyOf(body, systemText, textOf(firstUser?.content), headers);
  return { parsed, eff, key, weak, scope };
}

// ---------------------------------------------------------------- identity ----

// One request's identity and its diagnostics. `toolResultIds` exist only for the straggler
// guards ("every id it echoes is already answered"); they are never the identity — the digest is.
export function requestToken({ reqId, mode, norm, ident }) {
  const { body, systemText, msgs, toolResults } = norm;
  return {
    reqId, mode, nMessages: msgs.length,
    toolResultIds: toolResults.map(tr => tr.tool_use_id).filter(Boolean).sort(),
    digest: requestDigest({
      model: body.model, effort: ident.eff.effort, system: systemText, tools: body.tools ?? [], messages: msgs,
    }),
    // Diagnostic (2026-09-14): per-component digests, logged on `superseded-request` only, so a
    // storm of shape-identical non-joining duplicates names the component that differs.
    parts: {
      model: shortDigest(requestDigest({ model: body.model })),
      effort: shortDigest(requestDigest({ effort: ident.eff.effort })),
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
}

// The indices at which two per-message summaries differ, with both sides' block summaries.
export const msgDiff = (a, b) => (a && b) ? a.map((m, i) => [i, m, b[i]]).filter(([, x, y]) => !y || x.d !== y.d).slice(0, 4)
  .map(([i, x, y]) => ({ i, role: x.role, req: x.blocks, owner: y?.blocks ?? null })) : null;

// ------------------------------------------------------------- disposition ----

// THE decision. Exactly one of three readings of the session this request named:
//
//   replay — nothing is driving the session and this is a straggler retry of the turn that
//            already completed, so its answer is that turn's outcome, replayed. Guarded by
//            `isStragglerRetry`: every guard there keeps a real request out of the cache.
//   join   — a request IS driving the session and this is its byte-equal (undecorated) twin.
//            The 2026-09-13 storm was 89 such retries in 13 minutes, each superseding the
//            previous with a 409 the Anthropic SDK treats as retryable — a livelock. So a
//            duplicate attaches as a second sink instead (user decision 2026-09-13): no 409,
//            no error frame, no orphan row, no retry signal of any kind.
//   drive  — this request runs the turn. `supersedes` is the owner it displaces, if any;
//            a genuinely different second request still supersedes the older one loudly
//            (409 + `http-error` row), taking that owner's joiners with it.
export function dispositionOf(sess, token) {
  if (!sess.owner && isStragglerRetry(sess, token)) return { kind: "replay", outcome: sess.lastOutcome };
  if (isDuplicateRequest(sess.owner, token)) return { kind: "join", owner: sess.owner };
  return { kind: "drive", supersedes: sess.owner ?? null };
}

// ------------------------------------------------------------- turn input ----

// The text one turn carries: on a new thread the whole user-side conversation, otherwise the
// last message (falling back to its tool_result payloads when the thread was dropped under it).
export function turnInputText(mode, msgs, last, lastBlocks) {
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

