#!/usr/bin/env bun
// fake-app-server.mjs — a scripted stand-in for `codex app-server` (test seam only).
//
//   bun fake-app-server.mjs <scenario.json> [<trace.jsonl>] app-server
//
// The sidecar appends the literal `app-server` argument, exactly as it does for the
// real persona wrapper, so this is spawned through the same code path.
//
// Scenario shape (every field optional):
//   {
//     "initialize":  { ...InitializeResponse overrides... },
//     "threadStart": { "instructionSources": [..], "before": [action..], "error": {code,message} },
//     "turns":       [ [action..] | {"error":{code,message}} , ... ],   // consumed in order
//     "defaultTurn": [action..]                                        // when `turns` runs out
//     "neverRespond": ["turn/start", ..]   // methods the server accepts and never answers
//     "features":    [ {name, enabled, ..}, .. ] | { "error": {code,message} }
//   }
// `experimentalFeature/list` (the per-thread flag oracle) answers, by default, with the
// `config.features` block the last `thread/start` declared, echoed back as honoured:
// `{name, enabled: <declared>, stage: "stable", defaultEnabled: true}` per key. A `features`
// array in the scenario replaces that list entirely (drift cases); `{"error": {...}}` makes
// the method reply a JSON-RPC error (unavailable-oracle case).
// Actions:
//   {"delay": ms}                         pause
//   {"text": "..."}                       item/agentMessage/delta
//   {"message": "..."}                    item/completed with an agentMessage item (no deltas)
//   {"item": {...}}                       item/started with the given item
//   {"usage": {"last":{...},"total":{...}}}  thread/tokenUsage/updated
//   {"notify": "method", "params": {...}} raw notification (threadId/turnId auto-filled)
//   {"request": "method", "params": {...}}  server -> client request (callId auto-filled)
//   {"awaitToolResults": n}               block until n tool-call responses have arrived
//   {"complete": {...}}                   turn/completed (status defaults to "completed")
//   {"exit": code}                        die (child-death test)
import fs from "node:fs";

const argv = process.argv.slice(2).filter(a => a !== "app-server");
// A test runner that treats every .mjs under test/ as a test file would run this one (`bun test` collects only *.test.*); without a
// scenario this is not a server run, so exit cleanly instead of waiting on stdin.
if (!argv[0]) process.exit(0);
const scenario = JSON.parse(fs.readFileSync(argv[0], "utf8"));
const tracePath = argv[1] || null;

const state = { threads: 0, turns: 0, items: 0, rpcId: 1000, toolResponses: 0, thread: null, turn: null, interrupted: false, interruptedThreads: new Set(), threadConfig: null };

const trace = (kind, obj) => {
  if (!tracePath) return;
  try { fs.appendFileSync(tracePath, `${JSON.stringify({ kind, ts: Date.now(), ...obj })}\n`); } catch { /* ignore */ }
};
const out = obj => { process.stdout.write(`${JSON.stringify(obj)}\n`); trace("out", { frame: obj }); };
const notify = (method, params) => out({ jsonrpc: "2.0", method, params });
const reply = (id, result) => out({ jsonrpc: "2.0", id, result });
const replyError = (id, error) => out({ jsonrpc: "2.0", id, error });
const sleep = ms => new Promise(r => setTimeout(r, ms));

// `ctx` carries the thread/turn ids of the request the actions belong to, so overlapping
// thread/starts and turns stay distinguishable; it defaults to the latest ones.
async function play(actions, ctx = null) {
  const ids = () => ctx ?? { thread: state.thread, turn: state.turn };
  const stopped = () => (ctx ? state.interruptedThreads.has(ctx.thread) : state.interrupted);
  for (const a of (Array.isArray(actions) ? actions : [])) {
    if (stopped()) return;
    if (a.delay) { await sleep(a.delay); continue; }
    if (a.exit !== undefined) { process.exit(a.exit); }
    if (a.text !== undefined) {
      notify("item/agentMessage/delta", { threadId: ids().thread, turnId: ids().turn, itemId: `item_${state.items}`, delta: a.text });
      continue;
    }
    if (a.message !== undefined) {
      state.items += 1;
      notify("item/completed", {
        threadId: ids().thread, turnId: ids().turn, completedAtMs: Date.now(),
        item: { id: `item_${state.items}`, type: "agentMessage", text: a.message },
      });
      continue;
    }
    if (a.item !== undefined) {
      state.items += 1;
      notify("item/started", {
        threadId: ids().thread, turnId: ids().turn, startedAtMs: Date.now(),
        item: { id: `item_${state.items}`, ...a.item },
      });
      continue;
    }
    if (a.usage !== undefined) {
      notify("thread/tokenUsage/updated", { threadId: ids().thread, turnId: ids().turn, tokenUsage: a.usage });
      continue;
    }
    if (a.notify !== undefined) {
      notify(a.notify, { threadId: ids().thread, turnId: ids().turn, ...(a.params || {}) });
      continue;
    }
    if (a.request !== undefined) {
      const id = state.rpcId++;
      const params = { threadId: ids().thread, turnId: ids().turn, callId: `call_${id}`, ...(a.params || {}) };
      out({ jsonrpc: "2.0", id, method: a.request, params });
      continue;
    }
    if (a.awaitToolResults !== undefined) {
      const want = state.toolResponses + a.awaitToolResults;
      while (state.toolResponses < want && !stopped()) await sleep(5);
      continue;
    }
    if (a.complete !== undefined) {
      notify("turn/completed", {
        threadId: ids().thread,
        turn: { id: ids().turn, items: [], status: "completed", ...(a.complete || {}) },
      });
      continue;
    }
  }
}

const NEVER_RESPOND = new Set(scenario.neverRespond || []);

async function onMessage(m) {
  trace("in", { frame: m });
  // responses to our server -> client requests
  if (m.id !== undefined && m.method === undefined) { state.toolResponses += 1; return; }
  if (m.method === "initialized") return;
  // a hung app-server: the request is accepted and simply never answered
  if (NEVER_RESPOND.has(m.method)) { trace("never-respond", { method: m.method }); return; }

  if (m.method === "initialize") {
    reply(m.id, {
      userAgent: "fake-app-server/0.1",
      codexHome: "/tmp/fake-codex-home",
      platformFamily: "unix",
      platformOs: "linux",
      ...(scenario.initialize || {}),
    });
    return;
  }

  if (m.method === "thread/start") {
    const ts = scenario.threadStart || {};
    state.threads += 1;
    const tid = `thread_${state.threads}`;
    state.thread = tid;
    state.threadConfig = m.params?.config ?? null;
    if (ts.before) await play(ts.before, { thread: tid, turn: state.turn });
    if (ts.error) { replyError(m.id, ts.error); return; }
    reply(m.id, {
      thread: {
        id: tid, cwd: m.params?.cwd ?? null, ephemeral: true, status: { type: "idle" },
        createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
        cliVersion: "0.0.0-fake", modelProvider: "fake", preview: "", projectId: null,
        sessionId: tid, source: "startup", turns: [],
      },
      model: m.params?.model ?? "gpt-5.6-luna",
      modelProvider: "fake",
      cwd: m.params?.cwd ?? null,
      approvalPolicy: m.params?.approvalPolicy ?? "never",
      approvalsReviewer: "user",
      sandbox: { type: "readOnly" },
      instructionSources: ts.instructionSources ?? [],
    });
    return;
  }

  if (m.method === "turn/start") {
    // the real app-server rejects a turn/start without a string threadId
    if (typeof m.params?.threadId !== "string") {
      replyError(m.id, { code: -32600, message: "Invalid request: invalid type: null, expected a string" });
      return;
    }
    state.turns += 1;
    const turn = `turn_${state.turns}`;
    state.turn = turn;
    state.thread = m.params.threadId;
    const ctx = { thread: m.params.threadId, turn };
    state.interrupted = false;
    state.interruptedThreads.delete(m.params.threadId);
    const script = (scenario.turns || [])[state.turns - 1] ?? scenario.defaultTurn ?? [];
    if (script && !Array.isArray(script) && script.error) { replyError(m.id, script.error); return; }
    reply(m.id, { turn: { id: turn, items: [], status: "inProgress" } });
    play(Array.isArray(script) ? script : script.actions || [], ctx);
    return;
  }

  if (m.method === "experimentalFeature/list") {
    const fx = scenario.features;
    if (fx && !Array.isArray(fx) && fx.error) { replyError(m.id, fx.error); return; }
    const declared = state.threadConfig?.features ?? {};
    const data = Array.isArray(fx)
      ? fx
      : Object.entries(declared).map(([name, enabled]) => ({ name, enabled: enabled === true, stage: "stable", defaultEnabled: true }));
    reply(m.id, { data, nextCursor: null });
    return;
  }

  if (m.method === "turn/interrupt") { state.interrupted = true; state.interruptedThreads.add(m.params?.threadId); reply(m.id, {}); return; }

  if (m.id !== undefined) reply(m.id, {});
}

let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", d => {
  buf += d;
  let i;
  while ((i = buf.indexOf("\n")) >= 0) {
    const line = buf.slice(0, i);
    buf = buf.slice(i + 1);
    if (!line.trim()) continue;
    let m;
    try { m = JSON.parse(line); } catch { continue; }
    onMessage(m);
  }
});
process.stdin.on("end", () => process.exit(0));
