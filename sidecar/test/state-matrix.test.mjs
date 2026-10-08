// state-matrix.test.mjs — the replay / join / supersede decision matrix, characterized.
//
// `supersede.test.mjs` pins the cells that were designed alongside the implementation.
// This file pins the matrix ITSELF, as the decomposition harness the structural remediation
// (F1) needs: each test names one cell of "what does an arriving request do to a session in
// this state", so a refactor that turns one decision into threaded flags fails here rather
// than in production.
//
// The disposition of an arriving request is decided at exactly four sites:
//   REPLAY    — no owner, and the request is a straggler retry of the last completed turn
//   JOIN      — an owner is in flight and this request is its byte-equal (undecorated) twin
//   SUPERSEDE — an owner is in flight and this request is genuinely different
//   DRIVE     — anything else: this request becomes the owner and runs a turn
// plus two connection-lifecycle cells (promotion on owner disconnect, teardown when the
// last connection leaves). The guards on REPLAY are what keep it from swallowing real work,
// so each guard clause is exercised on its own below.
import test from "node:test";
import assert from "node:assert/strict";
import {
  withSidecar, req, msgBody, sentTo,
  rawStream, firstByte, sleep, sseEvents, sseText,
} from "./helpers.mjs";

const READ = [{ name: "Read", description: "read", input_schema: { type: "object" } }];
const call = (callId, tool, args) => ({ request: "item/tool/call", params: { callId, tool, arguments: args } });

const kinds = (log, kind) => log.filter(l => l.kind === kind);

// One delivered tool call, then a turn that answers immediately: the cheapest way to reach
// the "turn completed, every echoed id delivered" state a straggler retry needs.
async function answeredToolTurn(t, { defaultTurn = [{ text: "second" }, { complete: {} }] } = {}) {
  const h = await withSidecar({
    scenario: {
      turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { text: "done" }, { complete: {} }]],
      defaultTurn,
    },
  });
  t.after(() => h.close());
  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  const hist = result => [
    { role: "user", content: "hi" },
    { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: result }] },
  ];
  return { h, hist };
}

// A turn that blocks on one tool call and then thinks for 1.2 s, so a second request
// provably arrives while the first is still the owner.
async function slowInFlight(t, tail = [{ delay: 1200 }, { text: "done" }, { complete: {} }]) {
  const h = await withSidecar({
    scenario: {
      turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, ...tail]],
      defaultTurn: [{ text: "second" }, { complete: {} }],
    },
  });
  t.after(() => h.close());
  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  const hist = result => [
    { role: "user", content: "hi" },
    { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: result }] },
  ];
  return { h, hist };
}

// ------------------------------------------------------------------ REPLAY --

test("matrix: REPLAY needs echoed tool_result ids — a byte-identical re-send that echoes none runs a real turn", async t => {
  // The guard `token.toolResultIds.length > 0`. A plain conversational turn re-sent verbatim
  // digests the same as the last outcome and has no owner, so digest equality alone would
  // replay it. It must not: with nothing echoed there is no evidence the client ever saw the
  // answer, and answering "the same question again" from cache silently drops a real turn.
  const h = await withSidecar({
    scenario: { turns: [[{ text: "first" }, { complete: {} }]], defaultTurn: [{ text: "second" }, { complete: {} }] },
  });
  t.after(() => h.close());
  const body = msgBody({ messages: [{ role: "user", content: "hi" }] });

  const one = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(one.json.content[0].text, "first");
  const two = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(two.status, 200, two.text);
  assert.equal(two.json.content[0].text, "second", "the identical re-send really ran");

  const log = h.log();
  assert.equal(kinds(log, "replayed-outcome").length, 0, "no replay without echoed tool_result ids");
  assert.equal(sentTo(h.trace(), "turn/start").length, 2);
});

test("matrix: REPLAY needs every echoed id to have been delivered — an id the sidecar never handed out is not a straggler", async t => {
  // The guard `toolResultIds.every(id => sess.delivered.has(id))`, isolated. A client can
  // complete a turn whose last message echoes an id this process never issued (an orphan —
  // e.g. after a restart); its digest is then a perfectly good `lastOutcome.digest`, so only
  // the delivered-set check separates "a retry of my turn" from "a request I have not run".
  const h = await withSidecar({
    scenario: { turns: [[{ text: "first" }, { complete: {} }]], defaultTurn: [{ text: "second" }, { complete: {} }] },
  });
  t.after(() => h.close());
  const body = msgBody({
    tools: READ,
    messages: [{ role: "user", content: [{ type: "tool_result", tool_use_id: "never-issued", content: "x" }] }],
  });

  const one = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(one.status, 200, one.text);
  assert.equal(one.json.content[0].text, "first");
  const two = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(two.status, 200, two.text);
  assert.equal(two.json.content[0].text, "second", "an undelivered id is never answered from lastOutcome");

  const log = h.log();
  assert.equal(kinds(log, "replayed-outcome").length, 0);
  assert.equal(sentTo(h.trace(), "turn/start").length, 2);
});

test("matrix: REPLAY does not consume the outcome — two stragglers of one turn both get it", async t => {
  const { h, hist } = await answeredToolTurn(t);
  const body = msgBody({ tools: READ, messages: hist("a") });

  const answered = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(answered.json.content[0].text, "done");
  const s1 = await req(h, "/codex-a/v1/messages", { body });
  const s2 = await req(h, "/codex-a/v1/messages", { body });

  for (const [label, r] of [["first straggler", s1], ["second straggler", s2]]) {
    assert.equal(r.status, 200, `${label}: ${r.text}`);
    assert.equal(r.json.content[0].text, "done", label);
    assert.deepEqual(r.json.usage, answered.json.usage, `${label} carries the owner turn's occupancy`);
  }
  assert.equal(kinds(h.log(), "replayed-outcome").length, 2, "the replay is repeatable, not one-shot");
  // Only the opening request ran a `turn/start`; the answered retry delivered a tool result
  // into that same open turn, and neither straggler reached the app-server at all.
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no straggler ran a turn");
});

test("matrix: REPLAY never applies while an owner is in flight — the straggler supersedes instead", async t => {
  // The guard `!sess.owner`. `lastOutcome` from turn 1 stays on the session, so a request
  // matching it arriving while a *different* request owns the session must take the owner
  // path (supersede), not quietly answer itself from the stale outcome.
  const { h, hist } = await slowInFlight(t);

  const owner = req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist("a") }) });
  await sleep(400);
  const straggler = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ tools: READ, messages: [...hist("a"), { role: "user", content: "and now something else" }] }),
  });
  const o = await owner;

  assert.equal(o.status, 409, o.text);
  assert.equal(straggler.status, 200, straggler.text);
  const log = h.log();
  assert.equal(kinds(log, "replayed-outcome").length, 0, "an in-flight owner disables the replay path");
  assert.equal(kinds(log, "superseded-request").length, 1);
});

// -------------------------------------------------------------------- JOIN --

test("matrix: JOIN — a joiner's own disconnect leaves the owner's turn and session untouched", async t => {
  // The mirror of the promotion cell: the joiner goes, the owner stays. Nothing is
  // interrupted and the session is not dropped, because a live owner still holds the turn.
  const { h, hist } = await slowInFlight(t, [{ delay: 1500 }, { text: "done" }, { complete: {} }]);
  const body = msgBody({ tools: READ, messages: hist("a") });

  const owner = req(h, "/codex-a/v1/messages", { body });
  await sleep(300);
  const joiner = rawStream(h, "/codex-a/v1/messages", { ...body, stream: true });
  await firstByte(joiner);
  joiner.abort();
  await joiner.done;

  const o = await owner;
  assert.equal(o.status, 200, o.text);
  assert.equal(o.json.content[0].text, "done", "the owner completes normally after its joiner leaves");

  const log = h.log();
  assert.equal(kinds(log, "joined-request").length, 1);
  assert.equal(kinds(log, "turn-interrupt").length, 0, "a live owner means no interrupt");
  assert.equal(kinds(log, "superseded-request").length, 0);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1);
});

test("matrix: JOIN — the owner's failure is the joiner's failure, and the joiner's row names its owner", async t => {
  const { h, hist } = await slowInFlight(t, [
    { delay: 1000 }, { complete: { status: "failed", error: { message: "boom" } } },
  ]);
  const body = msgBody({ tools: READ, messages: hist("a") });

  const owner = req(h, "/codex-a/v1/messages", { body });
  await sleep(400);
  const joiner = await req(h, "/codex-a/v1/messages", { body });
  const o = await owner;

  assert.equal(o.status, 502, o.text);
  assert.equal(joiner.status, 502, joiner.text);
  assert.equal(joiner.json.error.type, "api_error");

  const log = h.log();
  const joined = kinds(log, "joined-request");
  assert.equal(joined.length, 1);
  const errs = kinds(log, "http-error").filter(e => e.status === 502);
  assert.equal(errs.length, 2, "one row for the owner, one for the joiner");
  assert.equal(errs.filter(e => e.joinedTo === joined[0].ownerReqId).length, 1);
  assert.equal(kinds(log, "replayed-outcome").length, 0);
});

// ------------------------------------------------- connection lifecycle -----

test("matrix: the LAST connection leaving after a promotion interrupts the turn and drops the session", async t => {
  // Promotion (`supersede.test.mjs`) keeps the turn alive when the owner's socket dies with
  // a joiner attached. This is the other half: when that promoted joiner *also* leaves, the
  // turn has no audience left, so it takes the same teardown a lone owner's abort takes.
  const { h, hist } = await slowInFlight(t, [{ delay: 4000 }, { text: "done" }, { complete: {} }]);
  const body = msgBody({ tools: READ, stream: true, messages: hist("a") });

  const owner = rawStream(h, "/codex-a/v1/messages", body);
  await firstByte(owner);
  const joiner = rawStream(h, "/codex-a/v1/messages", body);
  await firstByte(joiner);
  owner.abort();                       // promotes the joiner; the turn keeps running
  await owner.done;
  await sleep(150);
  assert.equal(kinds(h.log(), "turn-interrupt").length, 0, "promotion must not interrupt");

  joiner.abort();                      // now nothing is attached
  await joiner.done;
  await sleep(200);

  const log = h.log();
  assert.equal(kinds(log, "turn-interrupt").length, 1, "the last connection leaving interrupts the turn");
  assert.equal(kinds(log, "joined-request").length, 1);
  assert.equal(kinds(log, "superseded-request").length, 0);

  // The session was dropped with it: the next request starts a fresh thread.
  const next = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(next.status, 200, next.text);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2, "the session was dropped");
});

// ------------------------------------------------------------------- DRIVE --

test("matrix: DRIVE — the four request modes are classified from session state, not from the body", async t => {
  // `new-thread` -> `next-turn` -> `tool-result` -> `new-thread-after-rewind`, on one
  // session, each labelled on its own `http-start` row. The mode is the input the disposition
  // sites share, so it is pinned here as a sequence rather than inside any one feature test.
  const h = await withSidecar({
    scenario: {
      turns: [
        [{ text: "one" }, { complete: {} }],
        [call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { text: "two" }, { complete: {} }],
      ],
      defaultTurn: [{ text: "more" }, { complete: {} }],
    },
  });
  t.after(() => h.close());

  const m1 = [{ role: "user", content: "hi" }];
  await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m1 }) });
  const m2 = [...m1, { role: "assistant", content: "one" }, { role: "user", content: "again" }];
  const second = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m2 }) });
  assert.equal(second.json.stop_reason, "tool_use");
  const m3 = [...m2, { role: "assistant", content: second.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] }];
  await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m3 }) });
  // Fewer messages than the session has seen: a rewind, which drops the thread and opens a new one.
  await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m1 }) });

  const modes = kinds(h.log(), "http-start").map(l => l.mode);
  assert.deepEqual(modes, ["new-thread", "next-turn", "tool-result", "new-thread-after-rewind"]);
  assert.equal(kinds(h.log(), "rewind").length, 1);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
});

test("matrix: DRIVE — a turn on a fresh session is neither a replay, a join nor a supersede", async t => {
  // The default cell, pinned so a refactor cannot make the empty-session path fall into one
  // of the three special dispositions by accident.
  const h = await withSidecar({ scenario: { defaultTurn: [{ text: "hello" }, { complete: {} }] } });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody() });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.json.content[0].text, "hello");

  const log = h.log();
  for (const kind of ["replayed-outcome", "joined-request", "superseded-request", "http-error"]) {
    assert.equal(kinds(log, kind).length, 0, kind);
  }
  assert.equal(kinds(log, "http").length, 1);
});

// ------------------------------------------------------- transport boundary --

test("matrix: an SSE turn whose first delta races the turn/start reply still opens with message_start", async t => {
  // WP-R0's regression guard, stated as a matrix cell: the app-server routinely emits the
  // `turn/start` reply and the turn's first delta in one stdout chunk, so the sink receives
  // text before `start()` has written the headers. Those frames are queued and flushed after
  // `message_start`; without that queue the turn dies as an `event: error` with no
  // `message_start` at all (and no armed ping timer — `stream.test.mjs` covers the ping).
  const h = await withSidecar({
    scenario: { defaultTurn: [{ text: "alpha " }, { text: "beta" }, { complete: {} }] },
  });
  t.after(() => h.close());

  const s = rawStream(h, "/codex-a/v1/messages", msgBody({ stream: true }));
  const marks = await s.done;

  assert.equal(marks.status, 200, marks.text);
  const events = sseEvents(marks).map(e => e.event);
  assert.equal(events[0], "message_start", `first frame was ${events[0]}: ${marks.text}`);
  assert.ok(!events.includes("error"), `no error frame: ${events.join(",")}`);
  assert.ok(events.includes("message_stop"), events.join(","));
  assert.equal(sseText(marks), "alpha beta", "no delta is lost to the pre-header queue");
});

// ------------------------------------------------------ CLOSE settles owner --

test("matrix: a rewind that closes a session settles its mid-turn owner promptly with a retryable 409", async t => {
  // WP-1 (D4): close() used to disarm the timer and leave the owner parked in waitOutcome
  // until the turn timeout. The rewind branch closes the session under a live owner.
  const h = await withSidecar({
    scenario: {
      turns: [
        [{ text: "warm" }, { complete: {} }],
        [call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { delay: 5000 }, { text: "done" }, { complete: {} }],
      ],
      defaultTurn: [{ text: "fresh" }, { complete: {} }],
    },
  });
  t.after(() => h.close());
  const m1 = [{ role: "user", content: "hi" }];
  const r1 = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m1 }) });
  assert.equal(r1.status, 200);
  const m3 = [...m1, { role: "assistant", content: r1.json.content }, { role: "user", content: "read a" }];
  const r2 = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m3 }) });
  assert.equal(r2.json.stop_reason, "tool_use");
  const m5 = [...m3, { role: "assistant", content: r2.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "x" }] }];
  const t0 = Date.now();
  const owner = req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: m5 }) });
  await sleep(300);
  const rewound = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: [{ role: "user", content: "start over" }] }) });
  const o = await owner;
  const elapsed = Date.now() - t0;
  assert.equal(rewound.status, 200);
  assert.equal(o.status, 409);
  assert.ok(elapsed < 2000, `owner settled in ${elapsed} ms`);
  assert.equal(kinds(h.log(), "rewind").length, 1);
});

// ------------------------------------------- HISTORY classification (WP-3) --

const BILL = "x-anthropic-billing-header: cc_version=2.1.270.1d1; cc_entrypoint=sdk-cli; cc_is_subagent=true;";
const SUB_SYS = [{ type: "text", text: BILL }, { type: "text", text: "You are an adversarial plan auditor." }];
const subBody = (messages, opts = {}) => msgBody({ tools: READ, system: SUB_SYS, messages, ...opts });

// A subagent that has run one tool turn to completion: hist = [user, assistant(tool_use c1), tool_result].
async function subagentToolTurn(t, { tail = [{ text: "done" }, { complete: {} }], defaultTurn = [{ text: "more" }, { complete: {} }] } = {}) {
  const h = await withSidecar({ scenario: { turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, ...tail]], defaultTurn } });
  t.after(() => h.close());
  const m1 = [{ role: "user", content: "audit this plan" }];
  const first = await req(h, "/codex-a/v1/messages", { body: subBody(m1) });
  assert.equal(first.json.stop_reason, "tool_use");
  const m3 = [...m1, { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] }];
  return { h, m1, m3 };
}

test("matrix: FORK while an owner is mid-turn is answered locally — the owner completes, no 409, no interrupt", async t => {
  const { h, m1, m3 } = await subagentToolTurn(t, { tail: [{ delay: 1200 }, { text: "done" }, { complete: {} }] });
  const owner = req(h, "/codex-a/v1/messages", { body: subBody(m3) });
  await sleep(300);
  const fork = await req(h, "/codex-a/v1/messages", { body: subBody(m1) });
  assert.equal(fork.status, 200, fork.text);
  assert.equal(fork.json.usage.output_tokens, 0);
  const o = await owner;
  assert.equal(o.status, 200, o.text);
  assert.equal(o.json.content[0].text, "done");
  const log = h.log();
  assert.equal(kinds(log, "fork-local").length, 1);
  for (const k of ["turn-interrupt", "superseded-request", "rewind"]) assert.equal(kinds(log, k).length, 0, k);
});

test("matrix: FORK while idle leaves the session untouched — the next request continues the same thread", async t => {
  const { h, m1, m3 } = await subagentToolTurn(t);
  const r = await req(h, "/codex-a/v1/messages", { body: subBody(m3) });
  assert.equal(r.status, 200);
  const fork = await req(h, "/codex-a/v1/messages", { body: subBody(m1) });
  assert.equal(fork.status, 200);
  const m5 = [...m3, { role: "assistant", content: r.json.content }, { role: "user", content: "next" }];
  const next = await req(h, "/codex-a/v1/messages", { body: subBody(m5) });
  assert.equal(next.status, 200, next.text);
  const modes = kinds(h.log(), "http-start").map(l => l.mode);
  assert.deepEqual(modes, ["new-thread", "tool-result", "next-turn"]);
  assert.equal(kinds(h.log(), "fork-local").length, 1);
  assert.equal(sentTo(h.trace(), "thread/start").length, 1);
});

test("matrix: RE-KEY — a session_id change echoing a delivered tool id adopts the thread", async t => {
  const { h, m3 } = await subagentToolTurn(t);
  const r = await req(h, "/codex-a/v1/messages", { body: subBody(m3) });
  assert.equal(r.status, 200);
  const m5 = [...m3, { role: "assistant", content: r.json.content }, { role: "user", content: "next" }];
  const next = await req(h, "/codex-a/v1/messages", { body: subBody(m5, { sessionId: "99999999-2222-3333-4444-555555555555" }) });
  assert.equal(next.status, 200, next.text);
  const rk = kinds(h.log(), "session-rekeyed");
  assert.equal(rk.length, 1);
  assert.ok(rk[0].to.startsWith("99999999-"));
  assert.equal(kinds(h.log(), "http-start").at(-1).mode, "next-turn");
  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "same thread");
});

test("matrix: RE-KEY needs lineage — a same-prompt agent under another session_id gets its own thread", async t => {
  const { h, m1 } = await subagentToolTurn(t);
  const other = await req(h, "/codex-a/v1/messages", { body: subBody(m1, { sessionId: "99999999-2222-3333-4444-555555555555" }) });
  assert.equal(other.status, 200, other.text);
  assert.equal(kinds(h.log(), "session-rekeyed").length, 0);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
  assert.equal(kinds(h.log(), "http-start").at(-1).mode, "new-thread");
});

test("matrix: a FORK arriving on a key miss is answered locally and nothing is re-keyed", async t => {
  const { h, m1, m3 } = await subagentToolTurn(t);
  const r = await req(h, "/codex-a/v1/messages", { body: subBody(m3) });
  assert.equal(r.status, 200);
  const fork = await req(h, "/codex-a/v1/messages", { body: subBody(m1, { sessionId: "99999999-2222-3333-4444-555555555555" }) });
  assert.equal(fork.status, 200);
  assert.equal(kinds(h.log(), "fork-local").length, 1);
  assert.equal(kinds(h.log(), "session-rekeyed").length, 0);
  assert.equal(sentTo(h.trace(), "thread/start").length, 1);
});

test("matrix: D5 — +4 growth and a mismatched prefix each start a rehydrated thread, not next-turn", async t => {
  const h = await withSidecar({ scenario: { defaultTurn: [{ text: "one" }, { complete: {} }] } });
  t.after(() => h.close());
  const m1 = [{ role: "user", content: "hi" }];
  await req(h, "/codex-a/v1/messages", { body: msgBody({ messages: m1 }) });
  const m5 = [...m1, { role: "assistant", content: "one" }, { role: "user", content: "a" }, { role: "assistant", content: "x" }, { role: "user", content: "b" }];
  await req(h, "/codex-a/v1/messages", { body: msgBody({ messages: m5 }) });
  const bad = [m5[0], { role: "assistant", content: "EDITED" }, ...m5.slice(2), { role: "assistant", content: "one" }, { role: "user", content: "c" }];
  await req(h, "/codex-a/v1/messages", { body: msgBody({ messages: bad }) });
  const modes = kinds(h.log(), "http-start").map(l => l.mode);
  assert.deepEqual(modes, ["new-thread", "new-thread-after-rewind", "new-thread-after-rewind"]);
  const rw = kinds(h.log(), "rewind");
  assert.equal(rw[0].step, 4);
  assert.equal(rw[1].at, 1);
  assert.equal(kinds(h.log(), "rehydrated").length, 2);
});

test("matrix: a shorter STREAMING subagent request is a rewind, not a fork", async t => {
  const { h, m1, m3 } = await subagentToolTurn(t);
  const r = await req(h, "/codex-a/v1/messages", { body: subBody(m3) });
  assert.equal(r.status, 200);
  const s = rawStream(h, "/codex-a/v1/messages", subBody(m1, { stream: true }));
  const marks = await s.done;
  assert.equal(marks.status, 200);
  assert.equal(kinds(h.log(), "fork-local").length, 0);
  assert.equal(kinds(h.log(), "rewind").length, 1);
  assert.equal(kinds(h.log(), "http-start").at(-1).mode, "new-thread-after-rewind");
});

test("matrix: +1 growth over the full prior history while idle continues on the same thread", async t => {
  const h = await withSidecar({ scenario: { defaultTurn: [{ text: "one" }, { complete: {} }] } });
  t.after(() => h.close());
  const m2 = [{ role: "user", content: "hi" }, { role: "assistant", content: "one" }];
  await req(h, "/codex-a/v1/messages", { body: msgBody({ messages: m2.slice(0, 1) }) });
  await req(h, "/codex-a/v1/messages", { body: msgBody({ messages: [...m2, { role: "user", content: "a" }] }) });
  await req(h, "/codex-a/v1/messages", { body: msgBody({ messages: [...m2, { role: "user", content: "a" }, { role: "user", content: "b" }] }) });
  assert.deepEqual(kinds(h.log(), "http-start").map(l => l.mode), ["new-thread", "next-turn", "next-turn"]);
  assert.equal(kinds(h.log(), "rewind").length, 0);
  assert.equal(sentTo(h.trace(), "thread/start").length, 1);
});
