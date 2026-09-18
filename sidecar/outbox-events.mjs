// outbox-events.mjs — the sidecar's rows in the fleet-tools initiative tracker.
//
// Plan `tracker-event-seam.md`. Every call here is fail-open: the writer swallows, so an
// outbox failure can never fail, delay or log-as-error a request (the `Logger.write`
// contract). Never any message content — only identity, model/effort and token totals.

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
export function outboxTurn(ctx, persona, sess, { model, effort, stopReason }) {
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
