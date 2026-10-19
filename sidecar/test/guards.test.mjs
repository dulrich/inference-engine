import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, waitFor, SIMPLE_TURN, sentTo } from "./helpers.mjs";

const TITLE_SYSTEM = "You are naming a coding session so the user can pick it out of a long list of sessions. Reply with a short title.";

test("an extra instructionSources entry fails the thread closed", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md", "/repo/AGENTS.md"] },
      defaultTurn: SIMPLE_TURN,
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 502);
  assert.equal(r.json.error.type, "api_error");
  assert.match(r.json.error.message, /thread config not honoured/);

  const line = h.log().find(l => l.kind === "config-unverified");
  assert.ok(line, "the fail-closed path is logged");
  assert.equal(line.stage, "thread/start");
  assert.equal(sentTo(h.trace(), "turn/start").length, 0, "no turn runs on an unverified thread");
});

test("a hook firing during thread/start fails the thread closed", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: {
        instructionSources: ["/home/u/.codex-a/AGENTS.md"],
        before: [{ notify: "hook/started", params: { run: { id: "h1", eventName: "SessionStart", status: "running" } } }],
      },
      defaultTurn: SIMPLE_TURN,
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 502);
  assert.match(r.json.error.message, /hook ran during thread\/start/);
  assert.ok(h.log().some(l => l.kind === "config-unverified"));
});

test("a feature flag the thread declared false but the app-server reports true fails the thread closed", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md"] },
      features: [
        { name: "multi_agent", enabled: true, stage: "stable", defaultEnabled: true },
        { name: "hooks", enabled: false, stage: "stable", defaultEnabled: false },
      ],
      defaultTurn: SIMPLE_TURN,
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 502);
  assert.equal(r.json.error.type, "api_error");
  assert.match(r.json.error.message, /thread config not honoured: features not honoured/);
  assert.match(r.json.error.message, /multi_agent=true \(declared false\)/);

  const line = h.log().find(l => l.kind === "config-unverified");
  assert.ok(line, "the drift is logged on the fail-closed path");
  assert.equal(line.stage, "thread/start");
  assert.match(line.problem, /multi_agent=true \(declared false\)/);
  assert.equal(line.features[0].name, "multi_agent", "the mismatching oracle entries ride the row");
  assert.equal(sentTo(h.trace(), "turn/start").length, 0, "no turn runs on a drifted thread");
});

test("an unavailable feature oracle fails the thread closed", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md"] },
      features: { error: { code: -32601, message: "method not found" } },
      defaultTurn: SIMPLE_TURN,
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 502);
  assert.match(r.json.error.message, /thread config not honoured: feature oracle unavailable/);

  const line = h.log().find(l => l.kind === "config-unverified");
  assert.ok(line, "an unreadable oracle is logged, not swallowed");
  assert.equal(line.stage, "thread/start");
  assert.match(line.problem, /^feature oracle unavailable/);
  assert.equal(sentTo(h.trace(), "turn/start").length, 0, "no turn runs on an unverifiable thread");
});

test("a honoured feature set is recorded on config-verified", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md"] },
      defaultTurn: SIMPLE_TURN,
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.json.model, "codex-a/gpt-5.6-luna", "the envelope restores the provider-prefixed id the client selected");

  const line = h.log().find(l => l.kind === "config-verified");
  assert.ok(line, "the honoured readings are recorded");
  assert.equal(line.features.multi_agent, false);
  assert.equal(line.features.standalone_web_search, false);
  assert.ok(Array.isArray(line.unknownFeatures), "declared keys the oracle never reported are counted, not fatal");
  assert.equal(line.features.unified_exec, undefined, "the catalog-pinned flag is exempt from the compare");
  assert.equal(line.multiAgentMode, null, "the fake reports no multiAgentMode");
});

test("an out-of-band commandExecution item trips the runtime execution boundary", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md"] },
      defaultTurn: [
        { text: "let me check" },
        { item: { type: "commandExecution", command: ["rm", "-rf", "/"], commandActions: [], cwd: "/tmp", status: "inProgress" } },
        { delay: 3000 },
        { complete: {} },
      ],
    },
    env: { SIDECAR_BACKOFF_MS: "5000" },   // keep the degraded window observable
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 502);
  assert.equal(r.json.error.type, "api_error");
  assert.match(r.json.error.message, /out-of-band execution attempt \(commandExecution\)/);

  const sec = h.log().find(l => l.kind === "security-event");
  assert.equal(sec.itemType, "commandExecution");
  assert.equal(sec.persona, "codex-a");
  // the fake records the interrupt asynchronously; the 502 can reach us first
  await waitFor(() => sentTo(h.trace(), "turn/interrupt").length > 0, 3000);
  assert.equal(sentTo(h.trace(), "turn/interrupt").length, 1, "the turn is interrupted");

  const health = await req(h, "/healthz", { method: "GET" });
  assert.equal(health.status, 503);
  assert.equal(health.json.personas["codex-a"].degraded, true);
  assert.equal(health.json.personas["codex-b"].degraded, false);

  const again = await req(h, "/codex-a/v1/messages", { body: msgBody({ sessionId: "eeeeeeee-0000-0000-0000-000000000005" }) });
  assert.equal(again.status, 503);
  assert.match(again.json.error.message, /degraded/);

  // the child is killed so the normal restart path clears the flag
  await waitFor(async () => h.log().some(l => l.kind === "child-exit"), 3000);
});

test("the session-title signature is answered locally, and only that signature", async t => {
  const h = await withSidecar({ scenario: { defaultTurn: SIMPLE_TURN } });
  t.after(() => h.close());

  const title = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      model: "gpt-5.6-luna",
      system: TITLE_SYSTEM,
      tools: [],
      // Claude Code 2.1.269 shape: language instruction after </session> (WP-4 G1)
      messages: [{ role: "user", content: "<session>user: fix the parser\nassistant: done</session>\n\nWrite the title in the predominant language of the session." }],
    }),
  });
  assert.equal(title.status, 200, title.text);
  assert.deepEqual(title.json.content, [{ type: "text", text: "Session" }]);
  assert.equal(title.json.stop_reason, "end_turn");
  assert.deepEqual(title.json.usage, {
    input_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 0,
  }, "a locally answered title still carries all four Anthropic usage keys");
  assert.equal(sentTo(h.trace(), "thread/start").length, 0, "no thread is opened for a title query");
  assert.ok(h.log().some(l => l.kind === "title-local" && l.envelope === "pinned"));

  // negative: an ordinary message that merely contains <session>, with tools present
  const ordinary = await req(h, "/codex-a/v1/messages", {
    body: msgBody({
      system: TITLE_SYSTEM,
      tools: [{ name: "Bash", description: "run", input_schema: { type: "object" } }],
      messages: [{ role: "user", content: "<session>what does this tag do?</session>" }],
    }),
  });
  assert.equal(ordinary.status, 200, ordinary.text);
  assert.equal(ordinary.json.content[0].text, "hello world");
  assert.equal(sentTo(h.trace(), "thread/start").length, 1, "the normal path opens a thread");
});

test("a mid-turn contextCompaction item is in band: the turn completes and the persona stays healthy", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md"] },
      defaultTurn: [{ text: "hello " }, { item: { type: "contextCompaction" } }, { text: "world" }, { complete: {} }],
    },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 200, r.text);
  assert.equal(r.json.content[0].text, "hello world");
  assert.equal(h.log().filter(l => l.kind === "security-event").length, 0);
  assert.equal(sentTo(h.trace(), "turn/interrupt").length, 0);
  const health = await req(h, "/healthz", { method: "GET" });
  assert.equal(health.json.personas["codex-a"].degraded, false);
});

test("an unknown item type still trips the execution boundary (allowlist, not denylist)", async t => {
  const h = await withSidecar({
    scenario: {
      threadStart: { instructionSources: ["/home/u/.codex-a/AGENTS.md"] },
      defaultTurn: [{ text: "x" }, { item: { type: "fooBar" } }, { delay: 3000 }, { complete: {} }],
    },
    env: { SIDECAR_BACKOFF_MS: "5000" },
  });
  t.after(() => h.close());

  const r = await req(h, "/codex-a/v1/messages", { body: msgBody({}) });
  assert.equal(r.status, 502);
  assert.match(r.json.error.message, /out-of-band execution attempt \(fooBar\)/);
  assert.equal(h.log().find(l => l.kind === "security-event").itemType, "fooBar");
  const health = await req(h, "/healthz", { method: "GET" });
  assert.equal(health.json.personas["codex-a"].degraded, true);
});
