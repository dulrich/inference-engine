import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, sentTo } from "./helpers.mjs";

// Claude Code's classifier and parser for the reactive-compaction path.
const PARSE = /prompt is too long[^0-9]*(\d+)\s*tokens?\s*>\s*(\d+)/i;
const SHAPE = /^prompt is too long: \d+ tokens > \d+ maximum$/;

function assertTooLong(error, maxM) {
  assert.equal(error.type, "invalid_request_error");
  assert.match(error.message, SHAPE);
  const [, n, m] = error.message.match(PARSE).map(Number);
  assert.ok(n > m, `${n} > ${m}`);
  assert.equal(m, maxM);
}

const OVERFLOW_INFO = { message: "context window exceeded", codexErrorInfo: "contextWindowExceeded" };
const INPUT_TOO_LONG = { code: -32602, message: "Input exceeds the maximum length of 1048576 characters." };

test("contextWindowExceeded on a buffered request is a 400 prompt-too-long", async t => {
  const h = await withSidecar({
    env: { SIDECAR_CONTEXT_WINDOW_TOKENS: "1000" },
    scenario: { defaultTurn: [{ complete: { status: "failed", error: OVERFLOW_INFO } }] },
  });
  t.after(() => h.close());
  const r = await req(h, "/codex-a/v1/messages", { body: msgBody() });
  assert.equal(r.status, 400, r.text);
  assert.equal(r.json.type, "error");
  assertTooLong(r.json.error, 1000);
  const row = h.log().find(l => l.kind === "http-error");
  assert.equal(row.status, 400);
  assert.match(row.upstreamMessage, /context window exceeded/, "the codex text stays in the log row");
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "not retried");
});

test("contextWindowExceeded after the stream started is an SSE error event", async t => {
  const h = await withSidecar({
    scenario: { defaultTurn: [
      { usage: { last: { inputTokens: 10 }, total: { inputTokens: 10 }, modelContextWindow: 5000 } },
      { text: "starting" }, { delay: 20 },
      { notify: "error", params: { error: OVERFLOW_INFO, willRetry: false } }, { delay: 3000 },
    ] },
  });
  t.after(() => h.close());
  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: true }) });
  assert.equal(r.status, 200, "the stream had already started with 200");
  const last = r.events.at(-1);
  assert.equal(last.event, "error");
  assert.equal(last.data.type, "error");
  assertTooLong(last.data.error, 5000);
});

test("an app-server Input-exceeds JSON-RPC error is a 400 prompt-too-long", async t => {
  const h = await withSidecar({
    env: { SIDECAR_CONTEXT_WINDOW_TOKENS: "2000" },
    scenario: { turns: [{ error: INPUT_TOO_LONG }] },
  });
  t.after(() => h.close());
  const r = await req(h, "/codex-a/v1/messages", { body: msgBody() });
  assert.equal(r.status, 400, r.text);
  assertTooLong(r.json.error, 2000);
  const row = h.log().find(l => l.kind === "http-error");
  assert.match(row.upstreamMessage, /Input exceeds the maximum length of 1048576 characters\./);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "not retried");
});

test("an Input-exceeds error after the stream started is an SSE error event", async t => {
  const h = await withSidecar({
    env: { SIDECAR_CONTEXT_WINDOW_TOKENS: "3000" },
    scenario: { defaultTurn: [
      { text: "starting" }, { delay: 20 },
      { notify: "error", params: { error: { message: INPUT_TOO_LONG.message }, willRetry: false } }, { delay: 3000 },
    ] },
  });
  t.after(() => h.close());
  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: true }) });
  assert.equal(r.status, 200);
  const last = r.events.at(-1);
  assert.equal(last.event, "error");
  assertTooLong(last.data.error, 3000);
});
