// supersede.test.mjs — WP-5 phase 1 spike: what a duplicate request does to a slow
// in-flight turn, and how that differs from a real client abort.
//
// These two tests document *current* behaviour on purpose. They are the pins phase 2
// flips (together with tools.test.mjs:185-216) once the supersede fix lands.
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
// into a turn that then thinks for seconds before producing anything.
async function slowToolTurn(t) {
  const h = await withSidecar({
    scenario: {
      turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { delay: 4000 }, { text: "done" }, { complete: {} }]],
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

test("a duplicate on a second connection supersedes a slow open stream: 200 + in-stream error, not a 409 on the wire", async t => {
  const { h, body } = await slowToolTurn(t);

  const older = rawStream(h, "/codex-a/v1/messages", body);
  await firstByte(older);

  // (1) The stream is *not* byte-silent while the model thinks: in tool-result mode
  // `sink.start()` runs synchronously after answerParked, so headers and message_start
  // are on the wire in milliseconds — long before the 4 s turn produces anything.
  assert.equal(older.marks.status, 200);
  assert.ok(older.marks.headersMs < 1000, `headers after ${older.marks.headersMs}ms`);
  assert.ok(older.marks.firstChunkMs < 1000, `first chunk after ${older.marks.firstChunkMs}ms`);
  assert.equal(eventsOf(older.marks)[0].event, "message_start");

  // (2) A second connection ~1 s in, with the first still open and still the owner.
  await sleep(1000);
  const newer = rawStream(h, "/codex-a/v1/messages", body);

  const a = await older.done;
  // The superseded request keeps its 200 — `SseSink.error` can only deliver the failure
  // as an in-stream `event: error` frame once message_start has gone out. The 409 lives
  // in the log row, never on the wire, so a streaming client decides retryability from
  // this body alone.
  assert.equal(a.status, 200);
  const last = eventsOf(a).at(-1);
  assert.equal(last.event, "error");
  assert.equal(last.data.error.type, "api_error");
  assert.match(last.data.error.message, /superseded/);
  assert.ok(a.endMs < 3000, `superseded stream ended after ${a.endMs}ms (turn takes 4 s)`);

  const b = await newer.done;
  assert.equal(b.status, 200);
  const bEvents = eventsOf(b).map(e => e.event);
  assert.ok(bEvents.includes("message_stop"), bEvents.join(","));
  assert.match(b.text, /done/);

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "http-start" && l.mode === "tool-result").length, 2);
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 1);
  assert.equal(log.filter(l => l.kind === "http-error" && l.status === 409).length, 1);
  assert.equal(log.filter(l => l.kind === "orphan-tool-result" && l.tool_use_id === "c1").length, 1);
  // The decisive contrast with the abort path below, and what falsifies H1 on the live
  // log: a duplicate arriving on its own connection produces no close/interrupt at all.
  assert.equal(log.filter(l => l.kind === "turn-interrupt").length, 0);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no fresh turn/start on the blocked thread");
});

test("a client abort of the same slow stream is a different signature: turn-interrupt, dropped session, a second turn/start", async t => {
  const { h, body } = await slowToolTurn(t);

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
  // turn rather than superseding anything.
  const reissue = await req(h, "/codex-a/v1/messages", { body: { ...body, stream: false } });
  assert.equal(reissue.status, 200, reissue.text);

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 0);
  assert.equal(log.filter(l => l.kind === "http-error" && l.status === 409).length, 0);
  assert.equal(log.filter(l => l.kind === "http-start" && l.mode === "new-thread").length, 2);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
  assert.equal(sentTo(h.trace(), "turn/start").length, 2);
});
