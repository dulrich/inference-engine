import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, streamSse, msgBody, SIMPLE_TURN } from "./helpers.mjs";

const usage = (input, output, total = null) => ({
  last: { inputTokens: input, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: output, reasoningOutputTokens: 0, totalTokens: input + output },
  total: total ?? { inputTokens: input, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: output, reasoningOutputTokens: 0, totalTokens: input + output },
});

test("a silent turn still emits ping frames inside the client watchdog", async t => {
  const h = await withSidecar({
    scenario: { turns: [[{ text: "thinking" }, { delay: 800 }, { complete: {} }]] },
    env: { SIDECAR_PING_MS: "100" },
  });
  t.after(() => h.close());

  const r = await streamSse(h, "/codex-a/v1/messages", msgBody({ stream: true }), {
    until: evs => evs.filter(e => e.event === "ping").length >= 2,
  });
  assert.equal(r.status, 200);
  const pings = r.events.filter(e => e.event === "ping");
  assert.ok(pings.length >= 2, `expected repeating pings, got ${pings.length}`);
  assert.deepEqual(pings[0].data, { type: "ping" });
  assert.equal(r.events[0].event, "message_start", "pings follow message_start");
});

test("stream:false returns one Messages JSON body", async t => {
  const h = await withSidecar({ scenario: { defaultTurn: [...SIMPLE_TURN.slice(0, 2), { usage: usage(42, 7) }, { complete: {} }] } });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: false }) });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.headers["content-type"], "application/json");
  assert.ok(!r.text.includes("event:"), "no SSE frames in a non-streamed answer");
  assert.equal(r.json.type, "message");
  assert.equal(r.json.role, "assistant");
  assert.equal(r.json.content[0].text, "hello world");
  assert.equal(r.json.stop_reason, "end_turn");
  assert.equal(r.json.usage.input_tokens, 42);
  assert.equal(r.json.usage.output_tokens, 7);
});

test("usage is one call lagged at message_start and the turn's own occupancy at message_delta", async t => {
  const h = await withSidecar({
    scenario: {
      turns: [
        [{ delay: 30 }, { usage: usage(100, 10) }, { text: "one" }, { complete: {} }],
        [{ delay: 30 }, { usage: usage(50, 20, { inputTokens: 150, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: 30, reasoningOutputTokens: 0, totalTokens: 180 }) }, { text: "two" }, { complete: {} }],
      ],
    },
  });
  t.after(() => h.close());

  const sid = "dddddddd-0000-0000-0000-000000000004";
  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: true, sessionId: sid }) });
  const second = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ stream: true, sessionId: sid, messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "one" }, { role: "user", content: "again" }] }),
  });

  const startUsage = r => r.events.find(e => e.event === "message_start").data.message.usage;
  const deltaUsage = r => r.events.find(e => e.event === "message_delta").data.usage;

  assert.equal(startUsage(first).input_tokens, 0, "no previous call to lag on");
  assert.equal(deltaUsage(first).input_tokens, 100);
  assert.equal(deltaUsage(first).output_tokens, 10);

  assert.equal(startUsage(second).input_tokens, 100, "message_start carries the previous call's usage");
  assert.equal(deltaUsage(second).input_tokens, 50);
  assert.equal(deltaUsage(second).output_tokens, 20);

  // Each message reports its own turn, never the thread's running sum: the cumulative figure
  // (150/30 here) leaves only on the http row, as `usageTotalCodex`.
  const rows = h.log().filter(l => l.kind === "http");
  assert.equal(rows.at(-1).usageTotalCodex.inputTokens, 150);
  assert.equal(rows.at(-1).usageTotalCodex.outputTokens, 30);
  assert.equal(rows.at(-1).usageCodex.inputTokens, 50, "the http row's per-turn figure is the turn's own");
});

test("the wire usage is the Anthropic projection; the http row keeps the raw codex delta beside it", async t => {
  // Codex counts the whole prompt in `inputTokens` with the cached share a subset of it.
  // On the wire `input_tokens` is the uncached share (6463 - 3840 = 2623), so a client that
  // sums input + cache_read + cache_creation sees the prompt once, not twice.
  const cached = {
    last: { inputTokens: 6463, cachedInputTokens: 3840, cacheWriteInputTokens: 0, outputTokens: 12, reasoningOutputTokens: 0, totalTokens: 6475 },
    total: { inputTokens: 6463, cachedInputTokens: 3840, cacheWriteInputTokens: 0, outputTokens: 12, reasoningOutputTokens: 0, totalTokens: 6475 },
  };
  const h = await withSidecar({ scenario: { turns: [[{ text: "one" }, { usage: cached }, { complete: {} }]] } });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({ stream: true }) });
  const delta = r.events.find(e => e.event === "message_delta").data.usage;
  assert.deepEqual(delta, {
    input_tokens: 2623, cache_creation_input_tokens: 0, cache_read_input_tokens: 3840, output_tokens: 12,
  });
  assert.equal(delta.input_tokens + delta.cache_read_input_tokens + delta.cache_creation_input_tokens, 6463);

  const row = h.log().filter(e => e.kind === "http").at(-1);
  assert.deepEqual(row.usage, delta, "the http row logs what went on the wire");
  assert.deepEqual(row.usageCodex, {
    inputTokens: 6463, cachedInputTokens: 3840, cacheWriteInputTokens: 0, outputTokens: 12,
  }, "the raw codex delta stays recoverable for subscription-burn reading");
});
