#!/usr/bin/env node
// fake-app-server.mjs — a scripted stand-in for `codex app-server` (test seam only).
//
//   node fake-app-server.mjs <scenario.json> [<trace.jsonl>] app-server
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
//   }
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
// `node --test test/` treats every .mjs under test/ as a test file; without a
// scenario this is not a server run, so exit cleanly instead of waiting on stdin.
if (!argv[0]) process.exit(0);
const scenario = JSON.parse(fs.readFileSync(argv[0], "utf8"));
const tracePath = argv[1] || null;

const state = { threads: 0, turns: 0, items: 0, rpcId: 1000, toolResponses: 0, thread: null, turn: null, interrupted: false };

const trace = (kind, obj) => {
  if (!tracePath) return;
  try { fs.appendFileSync(tracePath, `${JSON.stringify({ kind, ts: Date.now(), ...obj })}\n`); } catch { /* ignore */ }
};
const out = obj => { process.stdout.write(`${JSON.stringify(obj)}\n`); trace("out", { frame: obj }); };
const notify = (method, params) => out({ jsonrpc: "2.0", method, params });
const reply = (id, result) => out({ jsonrpc: "2.0", id, result });
const replyError = (id, error) => out({ jsonrpc: "2.0", id, error });
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function play(actions) {
  for (const a of (Array.isArray(actions) ? actions : [])) {
    if (state.interrupted) return;
    if (a.delay) { await sleep(a.delay); continue; }
    if (a.exit !== undefined) { process.exit(a.exit); }
    if (a.text !== undefined) {
      notify("item/agentMessage/delta", { threadId: state.thread, turnId: state.turn, itemId: `item_${state.items}`, delta: a.text });
      continue;
    }
    if (a.message !== undefined) {
      state.items += 1;
      notify("item/completed", {
        threadId: state.thread, turnId: state.turn, completedAtMs: Date.now(),
        item: { id: `item_${state.items}`, type: "agentMessage", text: a.message },
      });
      continue;
    }
    if (a.item !== undefined) {
      state.items += 1;
      notify("item/started", {
        threadId: state.thread, turnId: state.turn, startedAtMs: Date.now(),
        item: { id: `item_${state.items}`, ...a.item },
      });
      continue;
    }
    if (a.usage !== undefined) {
      notify("thread/tokenUsage/updated", { threadId: state.thread, turnId: state.turn, tokenUsage: a.usage });
      continue;
    }
    if (a.notify !== undefined) {
      notify(a.notify, { threadId: state.thread, turnId: state.turn, ...(a.params || {}) });
      continue;
    }
    if (a.request !== undefined) {
      const id = state.rpcId++;
      const params = { threadId: state.thread, turnId: state.turn, callId: `call_${id}`, ...(a.params || {}) };
      out({ jsonrpc: "2.0", id, method: a.request, params });
      continue;
    }
    if (a.awaitToolResults !== undefined) {
      const want = state.toolResponses + a.awaitToolResults;
      while (state.toolResponses < want && !state.interrupted) await sleep(5);
      continue;
    }
    if (a.complete !== undefined) {
      notify("turn/completed", {
        threadId: state.thread,
        turn: { id: state.turn, items: [], status: "completed", ...(a.complete || {}) },
      });
      continue;
    }
  }
}

async function onMessage(m) {
  trace("in", { frame: m });
  // responses to our server -> client requests
  if (m.id !== undefined && m.method === undefined) { state.toolResponses += 1; return; }
  if (m.method === "initialized") return;

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
    state.thread = `thread_${state.threads}`;
    if (ts.before) await play(ts.before);
    if (ts.error) { replyError(m.id, ts.error); return; }
    reply(m.id, {
      thread: {
        id: state.thread, cwd: m.params?.cwd ?? null, ephemeral: true, status: { type: "idle" },
        createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
        cliVersion: "0.0.0-fake", modelProvider: "fake", preview: "", projectId: null,
        sessionId: state.thread, source: "startup", turns: [],
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
    state.turns += 1;
    state.turn = `turn_${state.turns}`;
    state.thread = m.params?.threadId ?? state.thread;
    state.interrupted = false;
    const script = (scenario.turns || [])[state.turns - 1] ?? scenario.defaultTurn ?? [];
    if (script && !Array.isArray(script) && script.error) { replyError(m.id, script.error); return; }
    reply(m.id, { turn: { id: state.turn, items: [], status: "inProgress" } });
    play(Array.isArray(script) ? script : script.actions || []);
    return;
  }

  if (m.method === "turn/interrupt") { state.interrupted = true; reply(m.id, {}); return; }

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
