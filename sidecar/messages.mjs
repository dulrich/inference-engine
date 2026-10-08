// messages.mjs — POST /<persona>/v1/messages, as an explicit four-phase sequence.
//
//   1. NORMALIZATION      `normalizeRequest` + `resolveIdentity` (request.mjs, pure)
//   2. SESSION DISPOSITION `disposeSession` gives the session and its mode;
//                          `dispositionOf` (request.mjs) then returns ONE of replay | join |
//                          drive, and that value is the only branch on the three behaviours
//   3. TURN EXECUTION     `startThread` / `deliverToolResults` / `startTurn` (turn.mjs)
//   4. OUTCOME EMISSION   `emitReplay`, `runAsJoiner`, `emitOutcome` — every exit writes one
//                         `http` (or `http-error`) row and settles every attached connection
//
// `handleMessages` itself is now the sequence and nothing else. The previous shape — one
// 441-line function in which every feature landed — is the regression this file answers
// (review F1), so a new behaviour belongs in the phase that owns it, not here.
import crypto from "node:crypto";
import { SidecarError, asSidecarError } from "./errors.mjs";
import { redactHeaders } from "./logging.mjs";
import {
  isTitleRequest, titleEnvelope, hasImageBlock, usageOut, clampUsage,
} from "./protocol.mjs";
import { wireModelId, messageSkeleton, sendJson, BufferSink, SseSink } from "./sinks.mjs";
import { Session } from "./session.mjs";
import { outboxKeyOf, outboxTurn } from "./outbox-events.mjs";
import {
  normalizeRequest, resolveIdentity, requestToken, dispositionOf, shortDigest, msgDiff, messageDigests,
} from "./request.mjs";
import { startThread, deliverToolResults, startTurn } from "./turn.mjs";

const superseded = () => new SidecarError(409, "api_error", "superseded by a newer request on this session");

export async function handleMessages(ctx, persona, body, req, res) {
  // 1. normalization
  const norm = normalizeRequest(body, req.headers);

  // (i) local session-title answer — before model validation, no thread opened
  if (isTitleRequest(body, norm.systemText, norm.msgs)) return answerTitleLocally(ctx, persona, norm, res);
  if (hasImageBlock(body)) throw new SidecarError(400, "invalid_request_error", "images unsupported by the codex app-server leg");
  const ident = resolveIdentity(norm);
  if (ident.weak) ctx.logger.write("weak-session-key", { persona: persona.name, key: ident.key, reason: "metadata.user_id carried no session_id" });

  // (ii) local fork answer — a subagent's non-streaming strict-prefix side query (D3). A pure
  // read: it never touches any session, so an in-flight owner is neither interrupted nor 409'd.
  const digests = messageDigests(norm.msgs);
  const forkOf = findForkTarget(persona, norm, ident, digests);
  if (forkOf) return answerForkLocally(ctx, persona, norm, ident, forkOf, res);

  await persona.ensure();

  // 2. session disposition
  const { sess, mode } = disposeSession(ctx, persona, norm, ident, digests);
  const reqId = crypto.randomUUID().slice(0, 8);
  ctx.logger.write("http-start", { persona: persona.name, key: ident.key, reqId, mode, stream: norm.stream, nMessages: norm.msgs.length });

  const wireModel = wireModelId(persona.name, body.model);
  const sink = norm.stream
    ? new SseSink(res, wireModel, ctx.config.pingMs)
    : new BufferSink(res, wireModel);
  const token = requestToken({ reqId, mode, norm, ident });
  const rc = { ctx, persona, req, res, norm, ident, sess, mode, reqId, token, sink, cfg: ctx.config };
  rc.logHttp = makeHttpLogger(rc);

  const disposition = dispositionOf(sess, token);
  // 3 + 4. one of exactly three paths; only `drive` runs a turn.
  if (disposition.kind === "replay") return emitReplay(rc, disposition.outcome);
  if (disposition.kind === "join") return runAsJoiner(rc, disposition.owner);
  return runAsOwner(rc, disposition.supersedes);
}

// --------------------------------------------------------------- phase 1 ----

// The title signature is answered here and never reaches the app-server: a title query that
// opened the session's thread would make the real task a continuation of "name this session"
// (WP-4 G1). Written straight to `res` — it predates any session, so it has no sink.
function answerTitleLocally(ctx, persona, norm, res) {
  ctx.logger.write("title-local", { persona: persona.name, stream: norm.stream, envelope: titleEnvelope(norm.msgs) });
  writeLocalText(persona, norm.body, "Session", res);
}

// A subagent's fork (D3): a non-streaming request whose messages are a strict undecorated
// prefix of a live session's history. The feature that sends it is unidentified and its reply
// is a placeholder, so it costs zero codex tokens and never touches the session it forks.
const FORK_TEXT = "(side query answered locally by codex-sidecar; no model call was made)";
function answerForkLocally(ctx, persona, norm, ident, sess, res) {
  ctx.logger.write("fork-local", { persona: persona.name, key: ident.key, forkOf: sess.key, n: norm.msgs.length, seen: sess.hist.length });
  writeLocalText(persona, norm.body, FORK_TEXT, res);
}

function writeLocalText(persona, body, text, res) {
  const usage = { input_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 0 };
  if (body.stream) {
    const msg = { ...messageSkeleton(wireModelId(persona.name, body.model)), usage };
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    const w = (t, o) => res.write(`event: ${t}\ndata: ${JSON.stringify(o)}\n\n`);
    w("message_start", { type: "message_start", message: msg });
    w("content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } });
    w("content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text } });
    w("content_block_stop", { type: "content_block_stop", index: 0 });
    w("message_delta", { type: "message_delta", delta: { stop_reason: "end_turn", stop_sequence: null }, usage });
    w("message_stop", { type: "message_stop" });
    res.end();
    return;
  }
  sendJson(res, 200, {
    ...messageSkeleton(wireModelId(persona.name, body.model)),
    content: [{ type: "text", text }],
    stop_reason: "end_turn",
    usage,
  });
}

// --------------------------------------------------------------- phase 2 ----

// Which session this request belongs to, and in what MODE it arrives. The mode is read from
// session state, never from the body, and it is the input both the turn-execution phase and
// the duplicate pre-check share.
function disposeSession(ctx, persona, norm, ident, digests) {
  const { key, weak } = ident;
  const { msgs, toolResults } = norm;
  let sess = persona.sessions.get(key);
  if (!sess) sess = adoptRekeyed(ctx, persona, norm, ident, digests);
  let mode;
  const cls = sess ? classifyAgainst(sess, digests, norm, ident) : null;
  if (!sess) {
    sess = new Session(persona, key);
    persona.sessions.set(key, sess);
    mode = "new-thread";
  } else if (cls.kind === "diverged") {
    // A genuine rewind, a mismatched prefix, or growth other than +1/+2 (D5): the thread did not
    // see this conversation, so `next-turn` would silently skip input. Start a rehydrated one.
    ctx.logger.write("rewind", {
      persona: persona.name, key, seen: sess.msgCount, now: msgs.length, threadId: sess.threadId,
      ...(cls.at !== undefined ? { at: cls.at } : {}), ...(cls.step !== undefined ? { step: cls.step } : {}),
    });
    persona.interrupt(sess, "rewind");
    persona.dropSession(sess);          // the abandoned thread is simply dropped (WP-1 settles its owner)
    sess = new Session(persona, key);
    persona.sessions.set(key, sess);
    mode = "new-thread-after-rewind";
  } else if (toolResults.length && (sess.parked.size
      || (sess.turnOpen && toolResults.some(tr => sess.delivered.has(tr.tool_use_id))))) {
    // Defect E1: an echo of an id we handed out belongs to the turn that is still open,
    // even if nothing is parked any more — never `turn/start` onto a blocked turn.
    mode = "tool-result";
  } else {
    if (sess.parked.size) {
      ctx.logger.write("dropped-tool-calls", { persona: persona.name, key, n: sess.parked.size });
      sess.abandonParked("no result returned by the client");
    }
    mode = "next-turn";
  }
  sess.msgCount = msgs.length;
  sess.hist = digests;
  sess.outboxKey = outboxKeyOf(key, weak);
  return { sess, mode };
}

// ---------------------------------------------------- history classification ----

// How an arriving request relates to the history `sess` last accepted (m = hist.length,
// n = request length). Pure read.
//   fork      — subagent, non-streaming, n < m, and the request is a strict prefix of hist
//   continues — hist[0..m-2] matches and either n == m (only the final message may differ:
//               supersede / join / replay cells) or n == m+2 with hist[m-1] matched (the
//               thread's own reply plus one user message — the only growth ever measured), or
//               n == m+1 with hist[m-1] matched (full prior history plus one message, rev 3)
//   diverged  — anything else; `at` is the first differing index, `step` the growth size
export function classifyAgainst(sess, digests, norm, ident) {
  const hist = sess.hist;
  const m = hist.length;
  const n = digests.length;
  const firstDiff = upto => {
    for (let i = 0; i < upto; i++) if (digests[i] !== hist[i]) return i;
    return -1;
  };
  if (n < m) {
    const at = firstDiff(n);
    if (at < 0 && ident.scope === "subagent" && norm.stream === false) return { kind: "fork" };
    return at < 0 ? { kind: "diverged", step: n - m } : { kind: "diverged", at };
  }
  const at = firstDiff(Math.max(0, m - 1));
  if (at >= 0) return { kind: "diverged", at };
  if (n === m) return { kind: "continues" };
  if ((n === m + 2 || n === m + 1) && (m === 0 || digests[m - 1] === hist[m - 1])) return { kind: "continues" };
  if (m > 0 && digests[m - 1] !== hist[m - 1]) return { kind: "diverged", at: m - 1 };
  return { kind: "diverged", step: n - m };
}

// Same-persona sessions a `session_id`-bearing subagent key could have been re-keyed from:
// another key with the same `:<disc>` suffix (D1). Read-only.
function rekeyCandidates(persona, ident) {
  if (ident.scope !== "subagent" || ident.weak) return [];
  const i = ident.key.indexOf(":");
  if (i < 0) return [];
  const suffix = ident.key.slice(i);
  return [...persona.sessions.values()].filter(s => s.key !== ident.key && !s.closed
    && s.key.indexOf(":") >= 0 && s.key.slice(s.key.indexOf(":")) === suffix);
}

// The session a request is a fork of — the keyed one, or (on a key miss) a re-key candidate.
// A fork is answered locally and never adopts.
function findForkTarget(persona, norm, ident, digests) {
  if (ident.scope !== "subagent" || norm.stream !== false) return null;
  const own = persona.sessions.get(ident.key);
  const pool = own ? [own] : rekeyCandidates(persona, ident);
  return pool.find(s => classifyAgainst(s, digests, norm, ident).kind === "fork") ?? null;
}

// Key miss: adopt a same-discriminator session only on lineage proof — the request continues
// its history AND echoes ≥1 tool_use id that session minted (`delivered`, never cleared). A
// same-prompt agent under another session_id holds no such id, so it gets its own thread.
function adoptRekeyed(ctx, persona, norm, ident, digests) {
  const ids = new Set();
  for (const m of norm.msgs) {
    if (m?.role !== "assistant" || !Array.isArray(m.content)) continue;
    for (const b of m.content) if (b?.type === "tool_use" && b.id) ids.add(b.id);
  }
  if (!ids.size) return null;
  const sess = rekeyCandidates(persona, ident).find(s =>
    classifyAgainst(s, digests, norm, ident).kind === "continues" && [...ids].some(id => s.delivered.has(id)));
  if (!sess) return null;
  const from = sess.key;
  persona.sessions.delete(from);
  persona.sessions.set(ident.key, sess);
  sess.key = ident.key;
  sess.outboxKey = outboxKeyOf(ident.key, ident.weak);
  ctx.logger.write("session-rekeyed", { persona: persona.name, from, to: ident.key, threadId: sess.threadId });
  return sess;
}

// One `http` row per request, same shape on every path (owner, joiner, replay).
// `usage` is what went on the wire (clamped when the clamp fired); `usageRaw`, when given,
// is the unclamped codex figure, so `usageCodex` is always the raw per-turn occupancy and
// `usageTotalCodex` the thread's cumulative spend — both in raw codex units.
function makeHttpLogger(rc) {
  const { ctx, persona, req, norm, ident, sess, mode, reqId } = rc;
  const { body, msgs, allMsgs } = norm;
  return ({ stopReason, toolCalls, usage, usageRaw, ...extra }) => {
    ctx.logger.write("http", {
      persona: persona.name, path: `/${persona.name}/v1/messages`, key: ident.key, reqId, scope: ident.scope, mode, stream: norm.stream,
      model: ident.parsed.model, effort: ident.eff.effort, effortSource: ident.eff.effortSource, threadId: sess.threadId, stop_reason: stopReason,
      nMessages: msgs.length, nSystemRole: allMsgs.length - msgs.length, nTools: (body.tools || []).length,
      toolCalls, usage: usageOut(usage), usageCodex: usageRaw ?? usage, usageTotalCodex: sess.usageTotal ?? null,
      headers: redactHeaders(req.headers), ...extra,
    });
    // A joiner is a second view of the owner's turn, not a turn of its own: no outbox row.
    if (!extra.joinedTo) outboxTurn(ctx, persona, sess, { model: ident.parsed.model, effort: ident.eff.effort, stopReason });
  };
}

// --------------------------------------------------------- REPLAY outcome ----

// A straggler retry of a turn that has already completed: replay its outcome, usage and
// all. Usage is occupancy, not a charge — the response the client actually accepts is this
// one, so it must carry the owner turn's occupancy or the client's meter reads zero.
function emitReplay(rc, lo) {
  const { ctx, persona, ident, sess, reqId, token, sink, logHttp } = rc;
  ctx.logger.write("replayed-outcome", { persona: persona.name, key: ident.key, reqId, ownerReqId: lo.reqId, reqDigest: shortDigest(token.digest) });
  sink.start(sess.lagUsage());
  for (const b of lo.blocks) {
    if (b.type === "text") sink.textDelta(b.text);
    else sink.toolUse(b);
  }
  sink.finish(lo.stopReason, lo.usage);
  logHttp({ stopReason: lo.stopReason, toolCalls: lo.toolCalls, usage: lo.usage });
}

// ----------------------------------------------------------- JOIN outcome ----

// Everything up to `sess.joiners.add` is synchronous, so a joiner is never registered after
// the owner has finished settling its joiners.
async function runAsJoiner(rc, owner) {
  const { ctx, persona, req, res, norm, ident, sess, mode, reqId, token, sink, logHttp } = rc;
  const ownerReqId = owner.reqId;
  ctx.logger.write("joined-request", {
    persona: persona.name, key: ident.key, reqId, ownerReqId, mode, stream: norm.stream, reqDigest: shortDigest(token.digest),
  });
  let settleJoin;
  const joined = new Promise(r => { settleJoin = r; });
  // `ownerToken` binds this joiner to the exact request it joined. `sess.joiners` is
  // session-global and may hold another owner's joiners, so every consumer filters on it
  // (audit defect B3): nobody inherits a joiner, and nobody else's joiner is written to.
  const joiner = { token, ownerToken: owner, sink, settled: false, settle(o) { if (this.settled) return; this.settled = true; settleJoin(o); } };
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
        persona: persona.name, key: ident.key, reqId, mode, status: o.error.status, type: o.error.anthropicType,
        message: o.error.message, headers: redactHeaders(req.headers), joinedTo: ownerReqId,
      });
      return;
    }
    // Same figures as the owner's row: a joiner is a second view of one turn, and the
    // occupancy of that turn is the same however many connections are watching it.
    logHttp({ stopReason: o.stopReason, toolCalls: o.toolCalls, usage: o.usage, joinedTo: ownerReqId });
  } finally {
    sess.joiners.delete(joiner);
    res.off("close", onJoinerClose);
  }
}

// ---------------------------------------------------------- DRIVE the turn ----

// A superseded owner takes its joiners with it (audit defect B3). They joined *that*
// request; they are no audience for this one, and leaving them attached would deliver
// this request's text and outcome to a connection that never asked for it.
function supersede(rc, oldOwner) {
  const { ctx, persona, ident, norm, sess, reqId, token, mode } = rc;
  ctx.logger.write("superseded-request", { persona: persona.name, key: ident.key, reqId, supersededReqId: oldOwner.reqId, mode, nMessages: norm.msgs.length, ownerNMessages: oldOwner.nMessages, reqDigest: shortDigest(token.digest), ownerDigest: shortDigest(oldOwner.digest), parts: token.parts, ownerParts: oldOwner.parts, msgDiff: msgDiff(token.msgShape, oldOwner.msgShape) });
  for (const j of sess.joinersOf(oldOwner)) {
    sess.joiners.delete(j);
    const err = superseded();
    j.sink.error(err);              // its own await path logs the 409 `http-error` row
    j.settle({ kind: "error", error: err });
  }
  sess.preempt(superseded());
}

async function runAsOwner(rc, supersedes) {
  const { ctx, persona, req, res, ident, sess, mode, reqId, token, sink, cfg, logHttp } = rc;
  if (supersedes) supersede(rc, supersedes);
  sess.owner = token;
  sess.ownerGone = false;
  sess.turnText.length = 0;       // per request: a joiner is replayed only what *this* request emitted
  sess.attach(sink);
  const stillMine = () => { if (sess.owner !== token) throw superseded(); };
  let joiners = [];               // duplicates this request answers alongside its own sink
  let dedupedToolResults = 0;     // re-sent tool_results silently absorbed

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
    // 3. turn execution
    if (mode.startsWith("new-thread")) await startThread(rc, stillMine);
    if (mode === "tool-result") dedupedToolResults = deliverToolResults(rc);
    else await startTurn(rc, stillMine);

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
    const audience = audienceOf(sink, joiners);
    // The client-abort path is unchanged only when nothing else is attached.
    if (closedByClient && !joiners.length) return;

    // 4. outcome emission
    if (out.kind === "error") throw out.error;
    emitOutcome(rc, out, audience, joiners, dedupedToolResults);
  } catch (e) {
    if (sess.owner === token) {
      for (const j of sess.joinersOf(token)) { joiners.push(j); sess.joiners.delete(j); }
      sess.turnOpen = false;
      sess.detach(sess.sink);
    }
    sess.detach(sink);
    const err = asSidecarError(e);
    for (const j of joiners) j.settle({ kind: "error", error: err });
    const audience = audienceOf(sink, joiners);
    if (closedByClient && !joiners.length) return;
    ctx.logger.write("http-error", {
      persona: persona.name, key: ident.key, reqId, mode, status: err.status, type: err.anthropicType,
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

// Every connection still attached to this turn, each exactly once (a promoted joiner is the
// session's sink as well, hence the dedupe).
const audienceOf = (sink, joiners) => [sink, ...joiners.map(j => j.sink)]
  .filter((sk, i, a) => sk && !sk.done && a.indexOf(sk) === i);

// -------------------------------------------------------- phase 4: emission --

function emitOutcome(rc, out, audience, joiners, dedupedToolResults) {
  const { ctx, persona, sess, ident, reqId, token, cfg, logHttp } = rc;
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
  // This turn's occupancy, once per turn, whatever is attached — clamped to the window the
  // app-server reported for the thread, else to the configured default.
  const usageRaw = sess.reconcile();
  const window = sess.contextWindow ?? cfg.contextWindowTokens;
  const clamped = clampUsage(usageRaw, window);
  const usage = clamped ?? usageRaw;
  if (clamped) {
    ctx.logger.write("usage-clamped", {
      persona: persona.name, key: ident.key, threadId: sess.threadId, window,
      windowSource: sess.contextWindow == null ? "env" : "app-server",
      raw: usageRaw, clamped: usageOut(clamped),
    });
  }
  for (const sk of audience) sk.finish(stopReason, usage);
  // What a straggler retry of this exact request gets replayed, should one still be in a
  // client backoff queue (blocks in emission order: this request's text, then its calls).
  const emitted = sess.turnText.join("");
  sess.lastOutcome = {
    reqId, digest: token.digest, stopReason, toolCalls, usage,
    blocks: [
      ...(emitted ? [{ type: "text", text: emitted }] : []),
      ...(out.kind === "tool_use" ? out.batch.map(c => ({ type: "tool_use", id: c.params.callId, name: c.params.tool, input: c.params.arguments ?? {} })) : []),
    ],
  };
  logHttp({ stopReason, toolCalls, usage, usageRaw, ...(dedupedToolResults ? { dedupedToolResults } : {}) });
  for (const j of joiners) j.settle({ kind: "done", stopReason, toolCalls, usage });
}
