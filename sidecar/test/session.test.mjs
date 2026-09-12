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
