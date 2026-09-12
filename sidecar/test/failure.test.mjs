import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, waitFor, SIMPLE_TURN, sentTo } from "./helpers.mjs";

test("a child that dies on a parked call fails the request 502 and restarts", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[{ request: "item/tool/call", params: { callId: "c1", tool: "Bash", arguments: {} } }, { awaitToolResults: 1 }, { delay: 20 }, { exit: 1 }]],
      defaultTurn: SIMPLE_TURN,
    },
  });
  t.after(() => h.close());

  const tools = [{ name: "Bash", description: "run", input_schema: { type: "object" } }];
  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools }) });
  assert.equal(first.status, 200, first.text);
  assert.equal(first.json.stop_reason, "tool_use");

  const answer = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      tools,
      messages: [
        { role: "user", content: "hi" },
        { role: "assistant", content: first.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "ok" }] },
      ],
    }),
  });
  assert.equal(answer.status, 502, answer.text);
  assert.match(answer.json.error.message, /exited/);
  assert.ok(h.log().some(l => l.kind === "child-exit" && l.persona === "codex-a"));

  // the persona restarts behind the backoff and serves the next session
  const after = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools, sessionId: "ffffffff-0000-0000-0000-000000000006" }) });
  assert.equal(after.status, 200, after.text);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2, "the restarted child opened a fresh thread");
});

test("account rate-limit exhaustion maps to 429 with retry-after", async t => {
  const resetsAt = Math.floor(Date.now() / 1000) + 120;
  const h = await withSidecar({
    scenario: {
      defaultTurn: [
        { delay: 20 },
        { notify: "account/rateLimits/updated", params: { rateLimits: { primary: { usedPercent: 100, resetsAt, windowDurationMins: 300 }, rateLimitReachedType: "rate_limit_reached" } } },
        { delay: 3000 },
        { complete: {} },
      ],
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 429, r.text);
  assert.equal(r.json.error.type, "rate_limit_error");
  const retry = Number(r.headers["retry-after"]);
  assert.ok(retry > 60 && retry <= 121, `retry-after ${retry} should be derived from resetsAt`);
  assert.ok(h.log().some(l => l.kind === "rate-limited"));
});

test("upstream auth and usage-limit errors map to 401 and 429", async t => {
  const h = await withSidecar({
    perPersona: {
      "codex-a": { threadStart: { error: { code: -32001, message: "unauthorized", data: { codexErrorInfo: "unauthorized" } } } },
      "codex-b": { defaultTurn: [{ complete: { status: "failed", error: { message: "usage limit reached", codexErrorInfo: "rateLimitExceeded" } } }] },
    },
  });
  t.after(() => h.close());

  const auth = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(auth.status, 401, auth.text);
  assert.equal(auth.json.error.type, "authentication_error");

  const limited = await req(h, "/codex-b/v1/messages", { body: msgBody({ model: "gpt-5.6-sol" }) });
  assert.equal(limited.status, 429, limited.text);
  assert.equal(limited.json.error.type, "rate_limit_error");
});

test("an error after message_start arrives as an SSE error frame", async t => {
  const h = await withSidecar({
    scenario: { defaultTurn: [{ text: "starting" }, { delay: 20 }, { notify: "error", params: { error: { message: "upstream blew up" }, willRetry: false } }, { delay: 3000 }] },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: true }) });
  assert.equal(r.status, 200, "the stream had already started with 200");
  const last = r.events.at(-1);
  assert.equal(last.event, "error");
  assert.equal(last.data.type, "error");
  assert.equal(last.data.error.type, "api_error");
  assert.match(last.data.error.message, /upstream blew up/);
  assert.ok(!r.events.some(e => e.event === "message_stop"), "a failed stream does not claim completion");
});
