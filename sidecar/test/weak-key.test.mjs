// weak-key.test.mjs — plan codex-sidecar-weak-key-isolation: the thread guard (no next turn
// on a session whose thread/start is still awaited) and weak-key isolation (a request with no
// session_id shares a session only as a byte-identical twin or a continuation of the answer
// that session emitted). The fake app-server's `threadStart.before` delay opens the race.
import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, sentTo, rawStream, firstByte, sleep, waitFor } from "./helpers.mjs";

const URL = "/codex-a/v1/messages";
const SLOW_START = { before: [{ delay: 400 }] };
const turn = (text, delay = 0) => [...(delay ? [{ delay }] : []), { text }, { complete: {} }];
const weak = o => msgBody({ sessionId: null, ...o });
const textOf = r => r.json?.content?.map(b => b.text ?? "").join("");

// Every turn/start carried a string threadId (the fake also rejects one that does not).
function threadIdsOfTurns(h) {
  const ts = sentTo(h.trace(), "turn/start").map(f => f.params?.threadId);
  for (const t of ts) assert.equal(typeof t, "string", `turn/start threadId ${t}`);
  return ts;
}

test("strong key: a non-identical second request inside the thread/start window supersedes the first and runs on its own thread/start", async t => {
  const h = await withSidecar({ scenario: { threadStart: SLOW_START, defaultTurn: turn("ok") } });
  t.after(() => h.close());
  const first = req(h, URL, { body: msgBody({ model: "gpt-5.6-luna" }) });
  await sleep(100);
  const second = await req(h, URL, { body: msgBody({ model: "gpt-5.6-luna-low" }) });
  const a = await first;
  assert.equal(a.status, 409, a.text);
  assert.equal(second.status, 200, second.text);
  assert.equal(textOf(second), "ok");
  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
  assert.deepEqual(threadIdsOfTurns(h), ["thread_2"]);
  const log = h.log();
  assert.equal(log.filter(l => l.kind === "thread-guard").length, 1);
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 1);
});

test("weak key, concurrent: non-identical requests both succeed on two threads, no 409", async t => {
  const h = await withSidecar({ scenario: { threadStart: SLOW_START, turns: [turn("one"), turn("two")] } });
  t.after(() => h.close());
  const first = req(h, URL, { body: weak({ model: "gpt-5.6-luna" }) });
  await sleep(100);
  const second = req(h, URL, { body: weak({ model: "gpt-5.6-luna-low" }) });
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.status, 200, a.text);
  assert.equal(b.status, 200, b.text);
  assert.equal(sentTo(h.trace(), "thread/start").length, 2);
  assert.deepEqual(threadIdsOfTurns(h).sort(), ["thread_1", "thread_2"]);
  const log = h.log();
  assert.equal(log.filter(l => l.kind === "superseded-request").length, 0);
  assert.equal(log.filter(l => l.kind === "weak-session-isolated" && l.reason === "no-extension" && l.inFlight).length, 1);
  await waitFor(() => h.log().some(l => l.kind === "session-retired"));
});

test("weak key, concurrent: identical requests still join (one turn, the same text to both)", async t => {
  const h = await withSidecar({ scenario: { threadStart: SLOW_START, defaultTurn: turn("same", 200) } });
  t.after(() => h.close());
  const first = req(h, URL, { body: weak() });
  await sleep(100);
  const second = req(h, URL, { body: weak() });
  const [a, b] = await Promise.all([first, second]);
  assert.equal(a.status, 200, a.text);
  assert.equal(b.status, 200, b.text);
  assert.equal(textOf(a), "same");
  assert.equal(textOf(b), "same");
  assert.equal(sentTo(h.trace(), "thread/start").length, 1);
  assert.equal(sentTo(h.trace(), "turn/start").length, 1);
  const log = h.log();
  assert.equal(log.filter(l => l.kind === "joined-request").length, 1);
  assert.equal(log.filter(l => l.kind === "weak-session-isolated").length, 0);
});

test("weak key, sequential: a same-prompt request after completion opens a new thread; repeated isolation retires every displaced session", async t => {
  const h = await withSidecar({ scenario: { defaultTurn: turn("x") } });
  t.after(() => h.close());
  const models = ["gpt-5.6-luna", "gpt-5.6-luna-low", "gpt-5.6-luna-high", "gpt-5.6-luna"];
  for (const model of models) {
    const r = await req(h, URL, { body: weak({ model }) });
    assert.equal(r.status, 200, r.text);
  }
  assert.equal(sentTo(h.trace(), "thread/start").length, 4);
  assert.deepEqual(threadIdsOfTurns(h), ["thread_1", "thread_2", "thread_3", "thread_4"]);
  const log = h.log();
  assert.equal(log.filter(l => l.kind === "http-start" && l.mode === "next-turn").length, 0);
  assert.equal(log.filter(l => l.kind === "weak-session-isolated").length, 3);
  const retired = log.filter(l => l.kind === "session-retired");
  assert.deepEqual(retired.map(l => l.threadId).sort(), ["thread_1", "thread_2", "thread_3"]);
});

test("weak key, strict extension with this session's own answer (decorated echo) is a next turn on the same thread", async t => {
  const h = await withSidecar({ scenario: { turns: [turn("first answer"), turn("second answer")] } });
  t.after(() => h.close());
  const a = await req(h, URL, { body: weak() });
  assert.equal(a.status, 200, a.text);
  const echo = a.json.content.map(b => ({ ...b, cache_control: { type: "ephemeral" } }));
  const b = await req(h, URL, {
    body: weak({ messages: [{ role: "user", content: "hi" }, { role: "assistant", content: echo }, { role: "user", content: "more" }] }),
  });
  assert.equal(b.status, 200, b.text);
  assert.equal(textOf(b), "second answer");
  assert.equal(sentTo(h.trace(), "thread/start").length, 1);
  assert.deepEqual(threadIdsOfTurns(h), ["thread_1", "thread_1"]);
  assert.equal(h.log().filter(l => l.kind === "weak-session-isolated").length, 0);
});

test("weak key, displaced follow-up: A's follow-up never enters B's thread; B continues only while mapped, then rehydrates", async t => {
  const h = await withSidecar({ scenario: { turns: [turn("ansA"), turn("ansB"), turn("ansB2"), turn("ansA2"), turn("ansB3")] } });
  t.after(() => h.close());
  const hi = { role: "user", content: "hi" };
  const A = await req(h, URL, { body: weak({ model: "gpt-5.6-luna" }) });
  const B = await req(h, URL, { body: weak({ model: "gpt-5.6-luna-low" }) });
  assert.equal(textOf(A), "ansA");
  assert.equal(textOf(B), "ansB");
  const bHist = [hi, { role: "assistant", content: "ansB" }, { role: "user", content: "next" }];
  const B2 = await req(h, URL, { body: weak({ model: "gpt-5.6-luna-low", messages: bHist }) });
  assert.equal(textOf(B2), "ansB2");
  const aHist = [hi, { role: "assistant", content: "ansA" }, { role: "user", content: "next" }];
  const A2 = await req(h, URL, { body: weak({ model: "gpt-5.6-luna", messages: aHist }) });
  assert.equal(textOf(A2), "ansA2");
  const B3 = await req(h, URL, {
    body: weak({ model: "gpt-5.6-luna-low", messages: [...bHist, { role: "assistant", content: "ansB2" }, { role: "user", content: "again" }] }),
  });
  assert.equal(B3.status, 200, B3.text);
  // A, B, B2 (continues B), A2 (new thread with A's transcript), B3 (new thread, not A2's)
  assert.deepEqual(threadIdsOfTurns(h), ["thread_1", "thread_2", "thread_2", "thread_3", "thread_4"]);
  const starts = sentTo(h.trace(), "turn/start");
  const inputOf = f => JSON.stringify(f.params?.input ?? "");
  assert.ok(inputOf(starts[3]).includes("ansA"), "A's follow-up renders A's transcript");
  assert.ok(!inputOf(starts[3]).includes("ansB"), "A's follow-up carries nothing of B");
  assert.ok(inputOf(starts[4]).includes("ansB2"), "B's follow-up renders B's transcript");
  assert.ok(h.log().some(l => l.kind === "weak-session-isolated"));
});

test("weak key, displaced follow-up against the successor's shorter history: not owned, so a new thread with A's transcript", async t => {
  const h = await withSidecar({ scenario: { turns: [turn("ansA"), turn("ansB"), turn("ansA2")] } });
  t.after(() => h.close());
  await req(h, URL, { body: weak({ model: "gpt-5.6-luna" }) });
  await req(h, URL, { body: weak({ model: "gpt-5.6-luna-low" }) });
  const A2 = await req(h, URL, {
    body: weak({ model: "gpt-5.6-luna", messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "ansA" }, { role: "user", content: "next" }] }),
  });
  assert.equal(textOf(A2), "ansA2");
  assert.deepEqual(threadIdsOfTurns(h), ["thread_1", "thread_2", "thread_3"]);
  assert.ok(JSON.stringify(sentTo(h.trace(), "turn/start")[2].params.input).includes("ansA"));
  assert.ok(h.log().some(l => l.kind === "weak-session-isolated" && l.reason === "not-owned"));
});

test("weak key: aborting the displaced in-flight owner leaves the successor mapped (identical request joins it, follow-up continues it)", async t => {
  const h = await withSidecar({ scenario: { turns: [turn("slowA", 1500), turn("slowB", 800), turn("B-next")] } });
  t.after(() => h.close());
  const a = rawStream(h, URL, weak({ model: "gpt-5.6-luna", stream: true }));
  await firstByte(a);
  const bBody = weak({ model: "gpt-5.6-luna-low" });
  const b = req(h, URL, { body: bBody });
  await waitFor(() => sentTo(h.trace(), "turn/start").length === 2);
  a.abort();
  await waitFor(() => h.log().some(l => l.kind === "turn-interrupt"));
  const twin = req(h, URL, { body: bBody });
  const [rb, rt] = await Promise.all([b, twin]);
  assert.equal(rb.status, 200, rb.text);
  assert.equal(rt.status, 200, rt.text);
  assert.equal(textOf(rt), "slowB");
  assert.equal(h.log().filter(l => l.kind === "joined-request").length, 1);
  const next = await req(h, URL, {
    body: weak({ model: "gpt-5.6-luna-low", messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "slowB" }, { role: "user", content: "go" }] }),
  });
  assert.equal(next.status, 200, next.text);
  assert.equal(textOf(next), "B-next");
  assert.deepEqual(threadIdsOfTurns(h), ["thread_1", "thread_2", "thread_2"]);
});
