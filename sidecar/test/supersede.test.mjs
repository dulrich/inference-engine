// supersede.test.mjs — WP-5: what a duplicate request does to a slow in-flight turn
// (it JOINS it), what a straggler retry gets after the turn completed (a replay), what a
// real client abort still does, and what a genuinely *different* second request still
// does (supersede, unchanged). Phase 1's spike pinned the old behaviour here; phase 2
// flipped tests 1 and tools.test.mjs's supersede case to the join semantics; phase 3
// (acceptance audit B1/B2/B3) pins that "the same request" means the same DIGEST of the
// whole execution-affecting body, and that a joiner belongs to the owner it joined.
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { withSidecar, req, msgBody, sentTo, parseSse } from "./helpers.mjs";

const READ = [{ name: "Read", description: "read", input_schema: { type: "object" } }];
const call = (callId, tool, args) => ({ request: "item/tool/call", params: { callId, tool, arguments: args } });

// A raw http.request so the test can time the response *headers* against the first
// body chunk — `fetch` hides that boundary, and the boundary is the whole question
// for the "client saw no byte for N seconds" hypothesis.
function rawStream(h, pathname, body) {
  const payload = JSON.stringify(body);
  const t0 = Date.now();
  const marks = { status: null, headersMs: null, firstChunkMs: null, endMs: null, text: "" };
  let settle;
  const done = new Promise(r => {
    settle = () => { if (marks.endMs === null) marks.endMs = Date.now() - t0; r(marks); };
  });
  const request = http.request({
    host: "127.0.0.1", port: h.port, path: pathname, method: "POST",
    agent: false,                                  // no keep-alive pool to hold the loop open
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${h.key}`,
      "content-length": Buffer.byteLength(payload),
    },
  }, res => {
    marks.status = res.statusCode;
    marks.headersMs = Date.now() - t0;
    res.setEncoding("utf8");
    res.on("data", c => {
      if (marks.firstChunkMs === null) marks.firstChunkMs = Date.now() - t0;
      marks.text += c;
    });
    res.on("end", settle);
  });
  // `destroy()` on an in-flight request emits neither `end` nor, reliably, `error` —
  // `close` is the only event that always fires, so the abort path settles there.
  request.on("error", settle);
  request.on("close", settle);
  request.end(payload);
  return { marks, done, abort: () => request.destroy() };
}

const eventsOf = marks => parseSse(marks.text);
const textOf = marks => eventsOf(marks)
  .filter(e => e.data?.delta?.type === "text_delta").map(e => e.data.delta.text).join("");
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Wait for the response's first body byte (never silently forever — a hang here would
// itself be the finding, so it fails loudly instead).
async function firstByte(stream, timeoutMs = 3000) {
  const t0 = Date.now();
  while (stream.marks.firstChunkMs === null) {
    if (Date.now() - t0 > timeoutMs) throw new Error("no first byte within 3 s");
    await sleep(10);
  }
}

// The live 2026-09-13 04:00Z storm rides entirely on `tool-result` mode (191 of the 194
// http-starts in the window, and 89/89 supersedes), so the repro submits a tool_result
// into a turn that then thinks for seconds. It emits text at ~0.5 s and more at ~3 s, so
// a duplicate arriving at ~1 s is provably mid-stream: it must be replayed the first
// half and streamed the second.
async function slowToolTurn(t, turn) {
  const h = await withSidecar({
    scenario: {
      turns: [turn ?? [
        call("c1", "Read", { path: "a" }), { awaitToolResults: 1 },
        { delay: 500 }, { text: "early " }, { delay: 2500 }, { text: "late" }, { complete: {} },
      ]],
      defaultTurn: [{ text: "second" }, { complete: {} }],
    },
  });
  t.after(() => h.close());
  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  const body = msgBody({
    tools: READ, stream: true,
    messages: [
      { role: "user", content: "hi" },
      { role: "assistant", content: first.json.content },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] },
    ],
  });
  return { h, body };
}

test("a duplicate on a second connection joins the slow open stream: both get the same full text, no 409 anywhere", async t => {
  const { h, body } = await slowToolTurn(t);

  const older = rawStream(h, "/codex-a/v1/messages", body);
  await firstByte(older);

  // (1) The stream is *not* byte-silent while the model thinks: in tool-result mode
  // `sink.start()` runs synchronously after answerParked, so headers and message_start
  // are on the wire in milliseconds — long before the turn produces anything.
  assert.equal(older.marks.status, 200);
  assert.ok(older.marks.headersMs < 1000, `headers after ${older.marks.headersMs}ms`);
  assert.ok(older.marks.firstChunkMs < 1000, `first chunk after ${older.marks.firstChunkMs}ms`);
  assert.equal(eventsOf(older.marks)[0].event, "message_start");

  // (2) A second, identical request ~1 s in — after "early " has already streamed to the
  // first connection and long before "late". It joins; nothing is superseded.
  await sleep(1000);
  const newer = rawStream(h, "/codex-a/v1/messages", body);

  const a = await older.done;
  const b = await newer.done;
  assert.equal(a.status, 200);
  assert.equal(b.status, 200);
  assert.ok(a.endMs > 2500, `the joined-to stream must run to completion, ended at ${a.endMs}ms`);
  for (const [name, marks] of [["older", a], ["newer", b]]) {
    const events = eventsOf(marks).map(e => e.event);
    assert.ok(events.includes("message_start"), `${name}: ${events.join(",")}`);
    assert.ok(events.includes("message_stop"), `${name}: ${events.join(",")}`);
    assert.ok(!events.includes("error"), `${name} saw an error frame: ${events.join(",")}`);
    // The joiner is replayed the text it missed, so neither connection is truncated.
    assert.equal(textOf(marks), "early late", `${name} text`);
  }

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "http-start" && l.mode === "tool-result").length, 2);
  const joined = log.filter(l => l.kind === "joined-request");
  assert.equal(joined.length, 1);
  assert.equal(joined[0].mode, "tool-result");
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 0);
  assert.equal(log.filter(l => l.kind === "http-error").length, 0);
  assert.equal(log.filter(l => l.kind === "orphan-tool-result").length, 0);
  assert.equal(log.filter(l => l.kind === "turn-interrupt").length, 0);
  // One http row per request; exactly one of them is the joiner's, and it names its owner.
  const rows = log.filter(l => l.kind === "http" && l.mode === "tool-result");
  assert.equal(rows.length, 2);
  assert.equal(rows.filter(r => r.joinedTo === joined[0].ownerReqId).length, 1);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no fresh turn/start on the blocked thread");
});

test("the owner's connection dying with a joiner attached promotes the joiner: full outcome, no interrupt, session intact", async t => {
  const { h, body } = await slowToolTurn(t);

  const older = rawStream(h, "/codex-a/v1/messages", body);
  await firstByte(older);
  await sleep(1000);
  const newer = rawStream(h, "/codex-a/v1/messages", body);   // joins
  await sleep(200);
  older.abort();                                                   // the owner's socket goes

  const b = await newer.done;
  assert.equal(b.status, 200);
  assert.ok(eventsOf(b).map(e => e.event).includes("message_stop"));
  assert.equal(textOf(b), "early late", "the promoted joiner still receives the whole turn");

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "turn-interrupt").length, 0, "a live joiner means the turn is never interrupted");
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 0);
  assert.equal(log.filter(l => l.kind === "joined-request").length, 1);

  // The session survived: the next request continues the thread instead of starting one.
  const next = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ tools: READ, messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "early late" }, { role: "user", content: "more" }] }),
  });
  assert.equal(next.status, 200, next.text);
  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "the session was not dropped");
});

test("a straggler retry arriving after the turn completed is replayed the outcome, not given a fresh turn", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { text: "done" }, { complete: {} }]],
      defaultTurn: [{ text: "second" }, { complete: {} }],
    },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  const body = msgBody({
    tools: READ,
    messages: [
      { role: "user", content: "hi" },
      { role: "assistant", content: first.json.content },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] },
    ],
  });
  const answered = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(answered.json.stop_reason, "end_turn");
  assert.equal(answered.json.content[0].text, "done");

  // The same request again, minutes late in SDK-backoff terms: the turn it belongs to is
  // over and every id it echoes is answered. It gets that outcome back, charged nothing.
  const straggler = await req(h, "/codex-a/v1/messages", { body });
  assert.equal(straggler.status, 200, straggler.text);
  assert.equal(straggler.json.stop_reason, "end_turn");
  assert.equal(straggler.json.content[0].text, "done", "the real outcome, not a re-ask");
  assert.equal(straggler.json.usage.output_tokens, 0, "the ledger is charged once");

  const log = h.log();
  const replayed = log.filter(l => l.kind === "replayed-outcome");
  assert.equal(replayed.length, 1);
  assert.equal(replayed[0].ownerReqId, log.filter(l => l.kind === "http")[1].reqId);
  assert.equal(log.filter(l => l.kind === "orphan-tool-result").length, 0);
  assert.equal(log.filter(l => l.kind === "http-error").length, 0);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no second turn on the answered conversation");
});

test("a second request that is NOT a duplicate still supersedes the first loudly", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[
        call("c1", "Read", { path: "a" }), call("c2", "Read", { path: "b" }),
        { awaitToolResults: 2 }, { delay: 1200 }, { text: "done" }, { complete: {} },
      ]],
    },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.deepEqual(first.json.content.filter(b => b.type === "tool_use").map(b => b.id), ["c1", "c2"]);
  const hist = [{ role: "user", content: "hi" }, { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] }];

  const older = req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist }) });
  await sleep(400);
  // A different conversation state — one more message, a different tool_result id — so it
  // is a new request, not a retry, and the old supersede path is what applies.
  const newBody = msgBody({ tools: READ, messages: [...hist, { role: "user", content: [{ type: "tool_result", tool_use_id: "c2", content: "b" }] }] });
  const newer = req(h, "/codex-a/v1/messages", { body: newBody });
  await sleep(200);
  // …and a duplicate of the *new* owner joins it. The superseded request must not touch it.
  const joiner = req(h, "/codex-a/v1/messages", { body: newBody });

  const a = await older;
  assert.equal(a.status, 409, a.text);
  const b = await newer;
  assert.equal(b.status, 200, b.text);
  assert.equal(b.json.content[0].text, "done");
  const c = await joiner;
  assert.equal(c.status, 200, c.text);
  assert.equal(c.json.content[0].text, "done", "the joiner follows the new owner, not the superseded request");

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 1);
  assert.equal(log.filter(l => l.kind === "http-error" && l.status === 409).length, 1);
  assert.equal(log.filter(l => l.kind === "joined-request").length, 1);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no fresh turn/start on the blocked thread");
});

test("a client abort with nothing else attached is unchanged: turn-interrupt, dropped session, a second turn/start", async t => {
  const { h, body } = await slowToolTurn(t, [
    call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { delay: 4000 }, { text: "done" }, { complete: {} },
  ]);

  const older = rawStream(h, "/codex-a/v1/messages", body);
  await firstByte(older);
  await sleep(1000);
  older.abort();
  await older.done;
  await sleep(150);

  const log1 = h.log();
  assert.equal(log1.filter(l => l.kind === "turn-interrupt").length, 1, "the abort is visible as a turn-interrupt row");
  assert.equal(log1.filter(l => l.kind === "superseded-request").length, 0);

  // The re-issue after an abort finds no session: it starts a fresh thread and a fresh
  // turn rather than superseding or joining anything.
  const reissue = await req(h, "/codex-a/v1/messages", { body: { ...body, stream: false } });
  assert.equal(reissue.status, 200, reissue.text);

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 0);
  assert.equal(log.filter(l => l.kind === "joined-request").length, 0);
  assert.equal(log.filter(l => l.kind === "http-error" && l.status === 409).length, 0);
  assert.equal(log.filter(l => l.kind === "http-start" && l.mode === "new-thread").length, 2);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
  assert.equal(sentTo(h.trace(), "turn/start").length, 2);
});

// ---------------------------------------------------------------------------
// Phase 3 — remediation of the acceptance audit's blocking findings.
// ---------------------------------------------------------------------------

// One delivered tool call, then a turn that thinks for 1.2 s once the result lands: long
// enough that a second request provably arrives while the first is still in flight.
async function slowInFlight(t) {
  const h = await withSidecar({
    scenario: {
      turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { delay: 1200 }, { text: "done" }, { complete: {} }]],
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

test("B1: same message count and the same tool_use_id but DIFFERENT tool_result content is not a duplicate — it supersedes", async t => {
  const { h, hist } = await slowInFlight(t);

  const older = req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist("a") }) });
  await sleep(400);
  // Exactly the shape the pre-digest {mode, nMessages, toolResultIds} identity called a
  // duplicate: same mode, same count, same ids. Only the payload differs — and joining it
  // would have swallowed a request the model never saw (audit B1).
  const newer = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist("TOTALLY DIFFERENT") }) });
  const a = await older;

  assert.equal(a.status, 409, a.text);
  assert.equal(newer.status, 200, newer.text);
  assert.equal(newer.json.content[0].text, "done");

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "joined-request").length, 0, "a changed payload must never join");
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 1);
  assert.equal(log.filter(l => l.kind === "http-error" && l.status === 409).length, 1);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no fresh turn/start on the blocked thread");
});

test("B1: an otherwise identical body at a different model id, or a different effort, is not a duplicate", async t => {
  const check = async (label, variant) => {
    const { h, hist } = await slowInFlight(t);
    const older = req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist("a") }) });
    await sleep(400);
    const newer = await req(h, "/codex-a/v1/messages", { body: variant(hist("a")) });
    const a = await older;

    assert.equal(a.status, 409, `${label}: ${a.text}`);
    assert.equal(newer.status, 200, `${label}: ${newer.text}`);
    const log = h.log();
    assert.equal(log.filter(l => l.kind === "joined-request").length, 0, `${label}: must not join`);
    assert.equal(log.filter(l => l.kind === "superseded-request").length, 1, `${label}: superseded-request`);
    assert.equal(log.filter(l => l.kind === "http-error" && l.status === 409).length, 1, `${label}: http-error`);
  };
  // Same messages, same system, same tools — a different model id (and with it the suffix effort).
  await check("model id", messages => msgBody({ model: "gpt-5.6-luna-high", tools: READ, messages }));
  // Byte-identical model id; only the harness's requested effort differs.
  await check("effort", messages => msgBody({ tools: READ, messages, extra: { output_config: { effort: "high" } } }));
});

test("B2: after the turn completed, the same shape with changed tool_result content runs a real turn — never a replay", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { text: "done" }, { complete: {} }]],
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

  const answered = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist("a") }) });
  assert.equal(answered.json.content[0].text, "done");

  // Same mode, same count, same ids, turn over, every id delivered — the pre-digest straggler
  // test. The content changed, so it is a new request and gets a new turn, not a stale outcome.
  const changed = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist("b") }) });
  assert.equal(changed.status, 200, changed.text);
  assert.equal(changed.json.content[0].text, "second", "a changed request is never answered from lastOutcome");

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "replayed-outcome").length, 0, "no replay for a request that differs");
  assert.equal(log.filter(l => l.kind === "http-error").length, 0);
  assert.equal(sentTo(h.trace(), "turn/start").length, 2, "the changed request really ran");
});

test("B3: a superseded owner takes its joiner with it — the joiner errors and sees none of the successor's stream", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[
        call("c1", "Read", { path: "a" }), call("c2", "Read", { path: "b" }),
        { awaitToolResults: 2 }, { delay: 1800 }, { text: "for-C-only" }, { complete: {} },
      ]],
    },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.deepEqual(first.json.content.filter(b => b.type === "tool_use").map(b => b.id), ["c1", "c2"]);
  const hist = [{ role: "user", content: "hi" }, { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] }];

  const a = req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist }) });   // A owns the turn
  await sleep(300);
  const b = rawStream(h, "/codex-a/v1/messages", msgBody({ tools: READ, stream: true, messages: hist }));  // B joins A
  await firstByte(b);
  await sleep(200);
  // C differs from A (one more tool_result), so it supersedes — and must take A's joiner B
  // with it rather than silently adopting B as its own audience (audit B3).
  const c = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ tools: READ, messages: [...hist, { role: "user", content: [{ type: "tool_result", tool_use_id: "c2", content: "b" }] }] }),
  });
  const aRes = await a;
  const bMarks = await b.done;

  assert.equal(aRes.status, 409, aRes.text);
  assert.equal(c.status, 200, c.text);
  assert.equal(c.json.content[0].text, "for-C-only", "C completes normally");
  assert.ok(eventsOf(bMarks).map(e => e.event).includes("error"), `B must see an error frame: ${bMarks.text}`);
  assert.equal(textOf(bMarks), "", "B receives none of C's text");
  assert.ok(!bMarks.text.includes("for-C-only"), "C's output never reaches a joiner of the request it superseded");

  const log = h.log();
  const joined = log.filter(l => l.kind === "joined-request");
  assert.equal(joined.length, 1);
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 1);
  const errs = log.filter(l => l.kind === "http-error" && l.status === 409);
  assert.equal(errs.length, 2, "one row for the superseded owner, one for its joiner");
  assert.equal(errs.filter(e => e.joinedTo === joined[0].ownerReqId).length, 1, "the joiner's row names the owner it joined");
  assert.equal(errs.filter(e => e.reqId === joined[0].ownerReqId).length, 1, "and the owner has its own row");
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no fresh turn/start on the blocked thread");
});
