import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, sentTo, waitFor } from "./helpers.mjs";

const call = (callId, tool, args) => ({ request: "item/tool/call", params: { callId, tool, arguments: args } });
const toolResponses = h => h.trace().filter(e => e.kind === "in" && e.frame?.result?.contentItems).map(e => e.frame.result);

test("a codex callId round-trips as the tool_use id and the tool_result answers it", async t => {
  const h = await withSidecar({
    scenario: { turns: [[call("call_abc", "Bash", { command: "ls -la" }), { awaitToolResults: 1 }, { text: "done" }, { complete: {} }]] },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: [{ name: "Bash", description: "run", input_schema: { type: "object" } }] }) });
  assert.equal(first.status, 200, first.text);
  assert.equal(first.json.stop_reason, "tool_use");
  const block = first.json.content.find(b => b.type === "tool_use");
  assert.deepEqual({ id: block.id, name: block.name, input: block.input }, { id: "call_abc", name: "Bash", input: { command: "ls -la" } });

  const second = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      tools: [{ name: "Bash", description: "run", input_schema: { type: "object" } }],
      messages: [
        { role: "user", content: "hi" },
        { role: "assistant", content: [block] },
        { role: "user", content: [{ type: "tool_result", tool_use_id: "call_abc", content: "file list" }] },
      ],
    }),
  });
  assert.equal(second.status, 200, second.text);
  assert.equal(second.json.stop_reason, "end_turn");
  assert.equal(second.json.content[0].text, "done");

  const answers = toolResponses(h);
  assert.equal(answers.length, 1);
  assert.deepEqual(answers[0], { contentItems: [{ type: "inputText", text: "file list" }], success: true });
});

test("tool calls inside the batching window are relayed as one message", async t => {
  const h = await withSidecar({
    scenario: { turns: [[call("c1", "Read", { path: "a" }), { delay: 10 }, call("c2", "Read", { path: "b" }), { awaitToolResults: 2 }, { complete: {} }]] },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: [{ name: "Read", description: "read", input_schema: { type: "object" } }] }) });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.json.stop_reason, "tool_use");
  const ids = r.json.content.filter(b => b.type === "tool_use").map(b => b.id);
  assert.deepEqual(ids, ["c1", "c2"]);

  const batch = h.log().find(l => l.kind === "parallel-tool-batch");
  assert.equal(batch.n, 2);
});

test("streamed deltas keep their order and precede the tool_use block", async t => {
  const h = await withSidecar({
    scenario: { turns: [[{ text: "a" }, { text: "b" }, { text: "c" }, call("c1", "Bash", {}), { awaitToolResults: 1 }, { complete: {} }]] },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: true, tools: [{ name: "Bash", description: "run", input_schema: { type: "object" } }] }) });
  assert.equal(r.status, 200);

  const names = r.events.map(e => e.event);
  assert.deepEqual(names, [
    "message_start",
    "content_block_start", "content_block_delta", "content_block_delta", "content_block_delta", "content_block_stop",
    "content_block_start", "content_block_delta", "content_block_stop",
    "message_delta", "message_stop",
  ]);
  const text = r.events.filter(e => e.data?.delta?.type === "text_delta").map(e => e.data.delta.text).join("");
  assert.equal(text, "abc");
  const toolStart = r.events.find(e => e.data?.content_block?.type === "tool_use");
  assert.equal(toolStart.data.index, 1);
  assert.equal(toolStart.data.content_block.id, "c1");
  assert.equal(r.events.at(-2).data.delta.stop_reason, "tool_use");
});

const READ = [{ name: "Read", description: "read", input_schema: { type: "object" } }];

test("a tool_result for an unknown id is an orphan, never a fresh turn/start", async t => {
  const h = await withSidecar({
    scenario: { turns: [[call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { text: "done" }, { complete: {} }]] },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");

  const second = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      tools: READ,
      messages: [
        { role: "user", content: "hi" },
        { role: "assistant", content: first.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: "no-such-call", content: "stale" }] },
      ],
    }),
  });

  assert.equal(second.status, 200, second.text);
  assert.ok(h.log().some(l => l.kind === "orphan-tool-result" && l.tool_use_id === "no-such-call"));
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "an unmatched tool_result never opens a turn on a blocked thread");
});

test("a call parked after the response boundary survives the next request", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[
        call("c1", "Read", { path: "a" }), { delay: 400 }, call("c2", "Read", { path: "b" }),
        { awaitToolResults: 2 }, { text: "done" }, { complete: {} },
      ]],
    },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  // c2 is minted while no request is in flight: it has never crossed the boundary
  await waitFor(() => h.trace().some(e => e.kind === "out" && e.frame?.params?.callId === "c2"));

  const second = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      tools: READ,
      messages: [
        { role: "user", content: "hi" },
        { role: "assistant", content: first.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] },
      ],
    }),
  });
  assert.equal(second.status, 200, second.text);
  const block = second.json.content.find(b => b.type === "tool_use");
  assert.equal(block.id, "c2", "the call parked after the boundary is delivered, not abandoned");

  const third = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      tools: READ,
      messages: [
        { role: "user", content: "hi" },
        { role: "assistant", content: first.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] },
        { role: "assistant", content: second.json.content },
        { role: "user", content: [{ type: "tool_result", tool_use_id: "c2", content: "b" }] },
      ],
    }),
  });

  assert.equal(third.status, 200, third.text);
  assert.equal(third.json.stop_reason, "end_turn");
  assert.equal(third.json.content[0].text, "done");
  assert.ok(!h.log().some(l => l.kind === "orphan-tool-result"), "the echoed id still matched its park");
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "the follow-up was a tool-result, not a turn on a blocked thread");
  assert.deepEqual(toolResponses(h).map(r => r.success), [true, true]);
});

test("three parallel calls spaced beyond batchMs are each delivered exactly once (no dropped outcome)", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[
        call("c1", "Read", { path: "a" }), { delay: 400 }, call("c2", "Read", { path: "b" }), { delay: 400 }, call("c3", "Read", { path: "c" }),
        { awaitToolResults: 3 }, { text: "done" }, { complete: {} },
      ]],
    },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  // both c2 and c3 settle while no request is attached — the second used to be dropped
  await waitFor(() => h.trace().some(e => e.kind === "out" && e.frame?.params?.callId === "c3"));
  await new Promise(r => setTimeout(r, 300));

  const hist = [{ role: "user", content: "hi" }, { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] }];
  const second = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist }) });
  assert.deepEqual(second.json.content.filter(b => b.type === "tool_use").map(b => b.id), ["c2"]);
  hist.push({ role: "assistant", content: second.json.content }, { role: "user", content: [{ type: "tool_result", tool_use_id: "c2", content: "b" }] });
  const third = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ, messages: hist }) });
  assert.equal(third.status, 200, third.text);
  assert.deepEqual(third.json.content.filter(b => b.type === "tool_use").map(b => b.id), ["c3"]);
  assert.ok(!h.log().some(l => l.kind === "http-error"));
});

test("a duplicate second request joins the in-flight turn: both connections get the outcome, no 409, no orphan row", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [[
        call("c1", "Read", { path: "a" }), { awaitToolResults: 1 }, { delay: 1500 }, call("c2", "Read", { path: "b" }),
        { awaitToolResults: 1 }, { text: "done" }, { complete: {} },
      ]],
    },
  });
  t.after(() => h.close());

  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ tools: READ }) });
  assert.equal(first.json.stop_reason, "tool_use");
  const body = msgBody({ tools: READ, messages: [{ role: "user", content: "hi" }, { role: "assistant", content: first.json.content },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "c1", content: "a" }] }] });
  const older = req(h, "/codex-a/v1/messages", { body });
  await new Promise(r => setTimeout(r, 400));
  const newer = req(h, "/codex-a/v1/messages", { body });   // byte-identical: a retry, not a new request

  // Both connections are answered, with the same outcome, when the turn actually produces one.
  for (const [name, p] of [["older", older], ["newer", newer]]) {
    const r = await p;
    assert.equal(r.status, 200, `${name}: ${r.text}`);
    assert.deepEqual(r.json.content.filter(x => x.type === "tool_use").map(x => x.id), ["c2"], name);
  }

  const log = h.log();
  assert.equal(log.filter(l => l.kind === "joined-request").length, 1);
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 0);
  assert.equal(log.filter(l => l.kind === "http-error").length, 0);
  assert.equal(log.filter(l => l.kind === "orphan-tool-result").length, 0, "the duplicate's echo of c1 is absorbed, not reported");
  assert.equal(log.filter(l => l.kind === "http" && l.mode === "tool-result").length, 2, "one http row per request");
  assert.equal(sentTo(h.trace(), "turn/start").length, 1, "no fresh turn/start on the blocked thread");
});
