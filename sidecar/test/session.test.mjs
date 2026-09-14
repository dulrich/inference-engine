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

// ---- parent vs subagent under one session_id (Defect A) ---------------------
// Claude Code hands a subagent its parent's session_id; the only wire witness is the
// `cc_is_subagent=true;` token in the billing line that leads `system` (spike 2026-09-12 —
// the gateway strips every `x-claude-code-*` header, so the agent id never reaches us).
const billing = subagent =>
  `x-anthropic-billing-header: cc_version=2.1.270.1d1; cc_entrypoint=sdk-cli;${subagent ? " cc_is_subagent=true;" : ""}`;

const agentBody = ({ sessionId, subagent = false, system = "You are a helpful coding assistant.", messages }) =>
  msgBody({
    sessionId,
    system: [{ type: "text", text: billing(subagent) }, { type: "text", text: system }],
    messages,
  });

const PARENT_SID = "dddddddd-0000-0000-0000-000000000004";

test("a parent and its subagent under one session_id get two threads", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const parent = await req(h, "/codex-a/v1/messages", {
    body: agentBody({ sessionId: PARENT_SID, messages: [{ role: "user", content: "orchestrate the plan" }] }),
  });
  assert.equal(parent.status, 200, parent.text);

  const sub = await req(h, "/codex-a/v1/messages", {
    body: agentBody({
      sessionId: PARENT_SID, subagent: true, system: "You are an adversarial plan auditor.",
      messages: [{ role: "user", content: "audit this plan" }],
    }),
  });
  assert.equal(sub.status, 200, sub.text);

  assert.equal(sentTo(h.trace(), "thread/start").length, 2, "a subagent must never land on its parent's thread");
  const threads = new Set(sentTo(h.trace(), "turn/start").map(f => f.params.threadId));
  assert.equal(threads.size, 2);

  const rows = h.log().filter(l => l.kind === "config-verified");
  assert.equal(rows.length, 2);
  const [p, s] = rows;
  assert.equal(p.scope, "parent");
  assert.equal(p.key, PARENT_SID, "the parent's key stays the bare session_id");
  assert.equal(s.scope, "subagent");
  assert.match(s.key, new RegExp(`^${PARENT_SID}:[0-9a-f]{16}$`));
  assert.notEqual(s.key, p.key);
});

test("a subagent's later turns reuse its own thread", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const system = "You are an adversarial plan auditor.";
  const first = await req(h, "/codex-a/v1/messages", {
    body: agentBody({ sessionId: PARENT_SID, subagent: true, system, messages: [{ role: "user", content: "audit this plan" }] }),
  });
  assert.equal(first.status, 200, first.text);

  // same system + same first user text, more messages: the subagent's second request
  const second = await req(h, "/codex-a/v1/messages", {
    body: agentBody({
      sessionId: PARENT_SID, subagent: true, system,
      messages: [
        { role: "user", content: "audit this plan" },
        { role: "assistant", content: "hello world" },
        { role: "user", content: "now check the sizing" },
      ],
    }),
  });
  assert.equal(second.status, 200, second.text);

  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "the subagent's own turns must not fragment");
  const keys = new Set(h.log().filter(l => l.kind === "http").map(l => l.key));
  assert.equal(keys.size, 1);
  assert.equal([...h.log().filter(l => l.kind === "http")].every(l => l.scope === "subagent"), true);
});

test("a parent whose system prompt changes mid-session keeps one thread", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const turn1 = await req(h, "/codex-a/v1/messages", {
    body: agentBody({ sessionId: PARENT_SID, system: "You are a helpful coding assistant.", messages: [{ role: "user", content: "one" }] }),
  });
  assert.equal(turn1.status, 200, turn1.text);

  // a skill injected mid-session rewrites `system`; the parent's key must not move
  const turn2 = await req(h, "/codex-a/v1/messages", {
    body: agentBody({
      sessionId: PARENT_SID,
      system: "You are a helpful coding assistant.\n\n<skill>commit</skill>",
      messages: [
        { role: "user", content: "one" },
        { role: "assistant", content: "hello world" },
        { role: "user", content: "two" },
      ],
    }),
  });
  assert.equal(turn2.status, 200, turn2.text);

  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "a system-prompt change must not fragment the parent");
  const http = h.log().filter(l => l.kind === "http");
  assert.deepEqual(http.map(l => l.key), [PARENT_SID, PARENT_SID]);
  assert.deepEqual(http.map(l => l.scope), ["parent", "parent"]);
});

test("two subagents of one parent with different first prompts get two threads", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const system = "You are a general-purpose subagent.";
  for (const prompt of ["sweep the router for background calls", "restate the superseded invariant"]) {
    const r = await req(h, "/codex-a/v1/messages", {
      body: agentBody({ sessionId: PARENT_SID, subagent: true, system, messages: [{ role: "user", content: prompt }] }),
    });
    assert.equal(r.status, 200, r.text);
  }

  assert.equal(sentTo(h.trace(), "thread/start").length, 2, "sibling subagents must not share a thread");
  const keys = new Set(h.log().filter(l => l.kind === "config-verified").map(l => l.key));
  assert.equal(keys.size, 2);
  assert.equal([...keys].every(k => k.startsWith(`${PARENT_SID}:`)), true);
});

// ---- per-turn effort (WP-1) ------------------------------------------------
// Effort is a turn-level field, not a thread-level one (measured 2026-09-13: low->high->low on
// one open thread gave 0/87/0 reasoning tokens). A change between turns therefore costs one
// prefix-cache miss and nothing else — no new thread, no interrupt, one `effort-changed` row.
const EFFORT_SID = "eeeeeeee-0000-0000-0000-000000000005";

test("a mid-session effort change re-sends effort on the same thread and logs effort-changed", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const turn = (effort, messages) => req(h, "/codex-a/v1/messages", {
    body: msgBody({ model: "gpt-5.6-sol", sessionId: EFFORT_SID, messages, extra: { output_config: { effort } } }),
  });

  const first = await turn("low", [{ role: "user", content: "one" }]);
  assert.equal(first.status, 200, first.text);
  const second = await turn("high", [
    { role: "user", content: "one" },
    { role: "assistant", content: "hello world" },
    { role: "user", content: "two" },
  ]);
  assert.equal(second.status, 200, second.text);

  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "an effort change must never open a new thread");
  const turns = sentTo(h.trace(), "turn/start");
  assert.deepEqual(turns.map(f => f.params.effort), ["low", "high"], "effort rides on every turn/start");
  assert.equal(new Set(turns.map(f => f.params.threadId)).size, 1, "both turns run on the same thread");

  const changed = h.log().filter(l => l.kind === "effort-changed");
  assert.equal(changed.length, 1, "exactly one effort-changed row");
  assert.equal(changed[0].from, "low");
  assert.equal(changed[0].to, "high");
  assert.equal(changed[0].source, "request");
  assert.equal(changed[0].key, EFFORT_SID);
  assert.equal(changed[0].threadId, turns[0].params.threadId);
});

test("config-verified and http rows carry effortSource alongside effort", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  // the request field is honoured on a bare id …
  const asked = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ model: "gpt-5.6-sol", sessionId: EFFORT_SID, extra: { output_config: { effort: "max" } } }),
  });
  assert.equal(asked.status, 200, asked.text);
  // … and loses to an explicit id suffix (separate session, so no effort-changed row)
  const suffixed = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ model: "gpt-5.6-sol-low", sessionId: "ffffffff-0000-0000-0000-000000000006", extra: { output_config: { effort: "high" } } }),
  });
  assert.equal(suffixed.status, 200, suffixed.text);

  const verified = h.log().filter(l => l.kind === "config-verified");
  assert.deepEqual(verified.map(l => [l.effort, l.effortSource]), [["high", "request"], ["low", "suffix"]]);
  const http = h.log().filter(l => l.kind === "http");
  assert.deepEqual(http.map(l => [l.model, l.effort, l.effortSource]),
    [["gpt-5.6-sol", "high", "request"], ["gpt-5.6-sol", "low", "suffix"]]);
  assert.equal(h.log().filter(l => l.kind === "effort-changed").length, 0, "two sessions, no change within either");
});

// A codex "usage figure", in the app-server's raw shape.
const raw = (input, cached, output) => ({
  inputTokens: input, cachedInputTokens: cached, cacheWriteInputTokens: 0,
  outputTokens: output, reasoningOutputTokens: 0, totalTokens: input + output,
});

test("N samplings between two client messages report the turn's own occupancy, never their sum", async t => {
  // The 2026-09-14 incident shape: code-mode `wait` polling re-sampled the same ~107k cached
  // prompt ten times inside one client gap. `last` is flat (the context did not grow), `total`
  // climbs by a whole prompt each poll. The wire must carry `last`; only the log carries `total`.
  const last = raw(107000, 100000, 120);
  const polls = [1, 2, 3, 4, 5].map(n => ({ usage: { last, total: raw(107000 * n, 0, 120 * n) } }));
  const h = await withSidecar({
    scenario: {
      turns: [
        [{ usage: { last: raw(6000, 0, 40), total: raw(6000, 0, 40) } }, { text: "one" }, { complete: {} }],
        [{ text: "two" }, ...polls, { complete: {} }],
      ],
    },
  });
  t.after(() => h.close());

  const sid = "eeeeeeee-0000-0000-0000-000000000007";
  const first = await req(h, "/codex-a/v1/messages", { body: msgBody({ sessionId: sid }) });
  assert.equal(first.status, 200, first.text);
  const second = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ sessionId: sid, messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "one" }, { role: "user", content: "again" }] }),
  });
  assert.equal(second.status, 200, second.text);

  assert.deepEqual(second.json.usage, {
    input_tokens: 7000, cache_creation_input_tokens: 0, cache_read_input_tokens: 100000, output_tokens: 120,
  }, "the last sampling's occupancy, not five of them summed");
  const occupancy = second.json.usage.input_tokens + second.json.usage.cache_read_input_tokens + second.json.usage.cache_creation_input_tokens;
  assert.equal(occupancy, 107000, "what Claude Code's context meter reads is the real codex context");

  const row = h.log().filter(l => l.kind === "http").at(-1);
  assert.deepEqual(row.usageCodex, { inputTokens: 107000, cachedInputTokens: 100000, cacheWriteInputTokens: 0, outputTokens: 120 });
  assert.equal(row.usageTotalCodex.inputTokens, 535000, "cumulative spend stays recoverable from the log");
  assert.equal(row.usageTotalCodex.outputTokens, 600);
  assert.equal(h.log().filter(l => l.kind === "usage-clamped").length, 0, "107k is well inside the window");
});

test("a figure above the context window is clamped proportionally and logged", async t => {
  // 5000 raw input of which 4000 cached: the projection is 1000 + 4000 side by side.
  const big = raw(5000, 4000, 77);
  const h = await withSidecar({
    scenario: {
      // the second thread's app-server reports its own window, which wins over the env default
      turns: [
        [{ usage: { last: big, total: big } }, { text: "one" }, { complete: {} }],
        [{ usage: { last: big, total: big, modelContextWindow: 2000 } }, { text: "two" }, { complete: {} }],
      ],
    },
    env: { SIDECAR_CONTEXT_WINDOW_TOKENS: "1000" },
  });
  t.after(() => h.close());

  const a = await req(h, "/codex-a/v1/messages", { body: msgBody({ sessionId: "eeeeeeee-0000-0000-0000-000000000008" }) });
  assert.equal(a.status, 200, a.text);
  assert.deepEqual(a.json.usage, {
    input_tokens: 200, cache_creation_input_tokens: 0, cache_read_input_tokens: 800, output_tokens: 77,
  }, "the three input columns scale down to exactly the window; output is untouched");
  assert.equal(a.json.usage.input_tokens + a.json.usage.cache_read_input_tokens + a.json.usage.cache_creation_input_tokens, 1000);

  const b = await req(h, "/codex-a/v1/messages", { body: msgBody({ sessionId: "eeeeeeee-0000-0000-0000-000000000009" }) });
  assert.equal(b.status, 200, b.text);
  assert.deepEqual(b.json.usage, {
    input_tokens: 400, cache_creation_input_tokens: 0, cache_read_input_tokens: 1600, output_tokens: 77,
  }, "the app-server's per-thread window wins over the env default");

  const rows = h.log().filter(l => l.kind === "usage-clamped");
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map(r => [r.window, r.windowSource]), [[1000, "env"], [2000, "app-server"]]);
  assert.deepEqual(rows[0].raw, { inputTokens: 5000, cachedInputTokens: 4000, cacheWriteInputTokens: 0, outputTokens: 77 },
    "the raw codex figure is preserved on the clamp row");
  assert.deepEqual(rows[0].clamped, a.json.usage, "and the clamp row records what went on the wire");
  const http = h.log().filter(l => l.kind === "http");
  assert.deepEqual(http[0].usageCodex, { inputTokens: 5000, cachedInputTokens: 4000, cacheWriteInputTokens: 0, outputTokens: 77 },
    "the http row keeps the raw figure, not the clamped one");
});
