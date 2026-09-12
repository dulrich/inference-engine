import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, envMessage, SIMPLE_TURN, sentTo } from "./helpers.mjs";

const scenario = { defaultTurn: SIMPLE_TURN };

test("role:system messages are filtered out after the cwd is extracted from them", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      messages: [
        envMessage(h.dir),
        { role: "user", content: "list the files" },
        { role: "system", content: [{ type: "text", text: "<system-reminder>token budget</system-reminder>" }] },
      ],
    }),
  });
  assert.equal(r.status, 200, r.text);

  const [start] = sentTo(h.trace(), "thread/start");
  assert.equal(start.params.cwd, h.dir, "cwd comes from the environment block");

  const [turn] = sentTo(h.trace(), "turn/start");
  assert.equal(turn.params.input[0].text, "list the files");
  assert.ok(!turn.params.input[0].text.includes("system-reminder"), "system-role entries are never a turn");
  assert.ok(!turn.params.input[0].text.includes("Primary working directory"));

  const http = h.log().find(l => l.kind === "http");
  assert.equal(http.nSystemRole, 2);
  assert.equal(http.nMessages, 1);
});

test("two concurrent sessions with identical prompts get two threads", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const body = sid => msgBody({ sessionId: sid, messages: [{ role: "user", content: "same prompt" }] });
  const [a, b] = await Promise.all([
    req(h, "/codex-a/v1/messages", { body: body("aaaaaaaa-0000-0000-0000-000000000001") }),
    req(h, "/codex-a/v1/messages", { body: body("bbbbbbbb-0000-0000-0000-000000000002") }),
  ]);
  assert.equal(a.status, 200, a.text);
  assert.equal(b.status, 200, b.text);

  const starts = sentTo(h.trace(), "thread/start");
  assert.equal(starts.length, 2, "identical prompts must not share a codex thread");
  const threads = new Set(sentTo(h.trace(), "turn/start").map(f => f.params.threadId));
  assert.equal(threads.size, 2);

  // the same session_id reuses its thread
  const again = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ sessionId: "aaaaaaaa-0000-0000-0000-000000000001", messages: [{ role: "user", content: "same prompt" }, { role: "assistant", content: "hello world" }, { role: "user", content: "more" }] }),
  });
  assert.equal(again.status, 200, again.text);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2, "an existing session must not reopen a thread");
});

test("a request without metadata.session_id falls back to a warned prompt hash", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ sessionId: null }) });
  assert.equal(r.status, 200, r.text);

  const warn = h.log().find(l => l.kind === "weak-session-key");
  assert.ok(warn, "the hash fallback is warned, never silent");
  assert.match(warn.key, /^[0-9a-f]{32}$/);
});

test("a rewind starts a fresh thread and is logged", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const sid = "cccccccc-0000-0000-0000-000000000003";
  const long = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ sessionId: sid, messages: [{ role: "user", content: "one" }, { role: "assistant", content: "two" }, { role: "user", content: "three" }] }),
  });
  assert.equal(long.status, 200, long.text);

  const rewound = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ sessionId: sid, messages: [{ role: "user", content: "one" }] }),
  });
  assert.equal(rewound.status, 200, rewound.text);

  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
  const line = h.log().find(l => l.kind === "rewind");
  assert.ok(line && line.seen === 3 && line.now === 1);
});
