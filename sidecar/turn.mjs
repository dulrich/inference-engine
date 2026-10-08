// turn.mjs — phase 3 of the request path: make the app-server do the work.
//
// Three mutually exclusive ways a request reaches the child, one function each:
//
//   startThread()         `new-thread` / `new-thread-after-rewind` — open a thread and VERIFY it
//   deliverToolResults()  `tool-result` — answer the parked calls of a turn that is still open
//   startTurn()           `next-turn` (and any new thread) — run a fresh turn on the thread
//
// The verification in `startThread` is the security posture, and it fails CLOSED: an extra
// instruction source, a hook or MCP server that ran during `thread/start`, a feature flag that
// reads back differently from what was declared, or an oracle that errors, all drop the session
// and 502 the request. A thread that cannot be proved to match its declaration is never used.
//
// `stillMine` is the caller's supersession check, threaded in rather than re-derived: every
// await here is a point at which a newer request may have taken the session.
import { THREAD_CONFIG, PREAMBLE, resolveCwd } from "./config.mjs";
import { SidecarError } from "./errors.mjs";
import { baseInstructionsOf, toDynamicTools, textOf } from "./protocol.mjs";
import { verifyInstructionSources, verifyFeatures, featureReadings } from "./thread-config.mjs";
import { turnInputText, rendersTranscript } from "./request.mjs";

export async function startThread(rc, stillMine) {
  const { ctx, persona, sess, norm, ident, cfg } = rc;
  const { systemText, interleavedText, rawCwd, body } = norm;
  const cwd = resolveCwd(cfg, ctx.logger, rawCwd);
  const hookBefore = persona.hookEvents;
  const mcpBefore = persona.mcpEvents;
  const params = {
    cwd,
    model: ident.parsed.model,
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
  sess.model = ident.parsed.model;
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

  // (e) feature-drift check — the declared posture read back from the app-server's own
  // per-thread flag oracle. WP-1 (2026-09-14) measured that these flags do NOT remove the
  // collab tools or code mode in codex-cli 0.154.0, so this detects *config* drift only;
  // `tripBoundary` stays the runtime enforcement line. Bounded by `turnTimeoutMs` like
  // every other rpc, and an oracle that errors fails the thread closed, never open.
  let reportedFeatures = [];
  let oracleError = null;
  try {
    const listed = await persona.rpc("experimentalFeature/list", { threadId: sess.threadId, limit: 500 }, cfg.turnTimeoutMs);
    reportedFeatures = Array.isArray(listed?.data) ? listed.data : [];
  } catch (e) {
    oracleError = e?.message ? String(e.message) : String(e);
  }
  const readings = featureReadings(THREAD_CONFIG.features, reportedFeatures);
  const featureProblem = oracleError
    ? `feature oracle unavailable: ${oracleError}`
    : verifyFeatures(THREAD_CONFIG.features, reportedFeatures);
  if (featureProblem) {
    ctx.logger.write("config-unverified", {
      persona: persona.name, threadId: sess.threadId, stage: "thread/start", problem: featureProblem,
      instructionSources: started?.instructionSources ?? null, features: readings.mismatches.map(m => m.entry),
    });
    persona.dropSession(sess);
    throw new SidecarError(502, "api_error", `thread config not honoured: ${featureProblem}`);
  }
  stillMine();
  ctx.logger.write("config-verified", {
    persona: persona.name, threadId: sess.threadId, key: ident.key, scope: ident.scope, cwd, model: ident.parsed.model,
    effort: ident.eff.effort, effortSource: ident.eff.effortSource,
    instructionSources: started?.instructionSources ?? [], nTools: params.dynamicTools.length,
    preambleChars: PREAMBLE.length, systemChars: systemText.length, interleavedSystemChars: interleavedText.length,
    features: readings.features, unknownFeatures: readings.unknownFeatures,
    multiAgentMode: started?.multiAgentMode ?? null,
  });
}

// Answers the parked calls this request echoes. Returns the number of re-sent results
// silently absorbed: an id already answered on this thread is a no-op (93 such rows in the
// 2026-09-13 storm window), not an orphan. Only an id we never handed out is one.
export function deliverToolResults(rc) {
  const { ctx, persona, sess, norm, ident } = rc;
  let dedupedToolResults = 0;
  for (const tr of norm.toolResults) {
    const text = typeof tr.content === "string" ? tr.content : textOf(tr.content) || JSON.stringify(tr.content ?? "");
    if (sess.answerParked(tr.tool_use_id, text, !tr.is_error)) continue;
    if (sess.delivered.has(tr.tool_use_id)) { dedupedToolResults += 1; continue; }
    ctx.logger.write("orphan-tool-result", { persona: persona.name, key: ident.key, tool_use_id: tr.tool_use_id });
  }
  sess.abandonDelivered("no result returned by the client");
  return dedupedToolResults;
}

export async function startTurn(rc, stillMine) {
  const { ctx, persona, sess, norm, ident, mode, cfg } = rc;
  const text = turnInputText(mode, norm.msgs, norm.last, norm.lastBlocks);
  if (rendersTranscript(mode, norm.msgs)) {
    ctx.logger.write("rehydrated", { persona: persona.name, key: ident.key, reqId: rc.reqId, nMessages: norm.msgs.length, chars: text.length });
  }
  const turnParams = { threadId: sess.threadId, input: [{ type: "text", text }], effort: ident.eff.effort };
  if (sess.model && ident.parsed.model !== sess.model) { turnParams.model = ident.parsed.model; sess.model = ident.parsed.model; }
  // A mid-thread effort change is legal and costs exactly one prefix-cache miss on the
  // changed turn (measured 2026-09-13: cachedInputTokens 0 on that turn, neighbours cached).
  // So it is logged and nothing else — no new thread, no interrupt.
  if (sess.lastEffort !== null && sess.lastEffort !== ident.eff.effort) {
    ctx.logger.write("effort-changed", {
      persona: persona.name, threadId: sess.threadId, key: ident.key,
      from: sess.lastEffort, to: ident.eff.effort, source: ident.eff.effortSource,
    });
  }
  sess.deltaItems.clear();
  sess.pendingOutcomes = [];      // a stray outcome never leaks into a fresh turn
  const turn = await persona.rpc("turn/start", turnParams, cfg.turnTimeoutMs);
  sess.lastEffort = ident.eff.effort;   // what this thread last ran at, for the next turn's compare
  stillMine();
  sess.turnId = turn?.turn?.id ?? turn?.turnId ?? sess.turnId;
}
