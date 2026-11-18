// generation.test.mjs — two sidecar generations on one inherited listening socket (README
// "Generations"). The test owns ONE listening socket (ephemeral port, loopback) and hands it to
// both generations as fd 3 with LISTEN_FDS=1 and LISTEN_PID set to the generation's own pid,
// the way systemd does: a `sh` gate sets LISTEN_PID=$$ and `exec`s the binary (exec keeps the
// pid). Generation B's gate is spawned up front (so it can inherit the fd) and blocks on stdin
// until the test releases it; the test's own copy of the socket is closed before any traffic,
// so only the generations ever accept.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { FAKE, PERSONAS, msgBody, parseSse, readJsonl, sentTo, sleep, waitFor } from "./helpers.mjs";

const SIDECAR_DIR = path.dirname(import.meta.dirname);
const BIN = path.resolve(SIDECAR_DIR, process.env.SIDECAR_TARGET?.trim() || "rs/target/release/codex-sidecar");
const KEY = "test-key";
const GATE = 'read -r _go; LISTEN_PID=$$; LISTEN_FDS=1; export LISTEN_PID LISTEN_FDS; exec "$@"';

const call = (callId, tool, args) => ({ request: "item/tool/call", params: { callId, tool, arguments: args } });

// One generation, gated: `release()` lets it exec; `ready` resolves on its listening line.
function gated(dir, name, fd, scenario, env = {}) {
  // `scenario` is one scenario for every persona, or `{ perPersona: { <persona>: scenario } }`.
  for (const p of PERSONAS) fs.writeFileSync(path.join(dir, `${name}-${p}.json`), JSON.stringify(scenario.perPersona?.[p] ?? scenario));
  const child = spawn("sh", ["-c", GATE, "sh", BIN], {
    cwd: SIDECAR_DIR,
    stdio: ["pipe", "pipe", "pipe", fd],
    env: {
      PATH: process.env.PATH,
      HOME: dir,
      SIDECAR_KEY: KEY,
      SIDECAR_PORT: "0",
      SIDECAR_BIND_EXTRA: "",
      SIDECAR_PERSONAS: PERSONAS.join(","),
      SIDECAR_ACTIVATION: name,
      SIDECAR_APP_SERVER_CMD: `${process.execPath} ${FAKE} ${dir}/${name}-{persona}.json ${dir}/${name}-{persona}.trace.jsonl`,
      SIDECAR_LOG_DIR: path.join(dir, `${name}-logs`),
      SIDECAR_STATE_DIR: path.join(dir, `${name}-state`),
      SIDECAR_PING_MS: "60000",
      SIDECAR_BATCH_MS: "80",
      SIDECAR_TURN_TIMEOUT_MS: "20000",
      SIDECAR_BACKOFF_MS: "150",
      ...env,
    },
  });
  let out = "", err = "";
  child.stdout.on("data", c => { out += c; });
  child.stderr.on("data", c => { err += c; });
  const exited = new Promise(r => child.once("exit", (code, signal) => r({ code, signal, at: Date.now() })));
  return {
    name,
    child,
    exited,
    err: () => err,
    release() { child.stdin.write("go\n"); },
    ready: () => waitFor(() => /listening on /.test(out), 10000),
    retired: () => waitFor(() => /retiring \(stopped accepting\)/.test(err), 10000),
    trace: (p = PERSONAS[0]) => readJsonl(path.join(dir, `${name}-${p}.trace.jsonl`)),
    kill() { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); },
  };
}

// The shared socket and two gated generations; the test's copy of the socket is closed here.
async function pair(t, scenarioA, scenarioB, envA = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sidecar-gen-"));
  const server = net.createServer();
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  const port = server.address().port;
  const fd = server._handle?.fd;
  assert.ok(Number.isInteger(fd) && fd > 2, "the runtime exposes the listening fd");
  const a = gated(dir, "gen-a", fd, scenarioA, envA);
  const b = gated(dir, "gen-b", fd, scenarioB);
  await new Promise(r => server.close(r));
  t.after(() => {
    a.kill();
    b.kill();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  a.release();
  await a.ready();
  return { port, a, b };
}

function post(port, pathname, body, agent) {
  const payload = JSON.stringify(body);
  return new Promise(resolve => {
    const r = http.request({
      host: "127.0.0.1", port, path: pathname, method: "POST", agent,
      headers: { "content-type": "application/json", authorization: `Bearer ${KEY}`, "content-length": Buffer.byteLength(payload) },
    }, res => {
      let text = "";
      res.setEncoding("utf8");
      res.on("data", c => { text += c; });
      res.on("end", () => {
        let json = null;
        try { json = JSON.parse(text); } catch { /* not json */ }
        resolve({ ok: true, status: res.statusCode, text, json, events: parseSse(text) });
      });
      res.on("error", e => resolve({ ok: false, error: String(e) }));
    });
    r.on("error", e => resolve({ ok: false, error: String(e) }));
    r.end(payload);
  });
}

const textOf = r => (r.json?.content ?? []).filter(b => b.type === "text").map(b => b.text).join("");

test("a retire under load: the slow stream completes intact, zero failures, B answers after the ack, A exits retired", async t => {
  const slow = [];
  for (let i = 0; i < 12; i++) slow.push({ text: `s${i} ` }, { delay: 150 });
  slow.push({ complete: {} });
  const quickA = { defaultTurn: [{ text: "from-A" }, { complete: {} }] };
  const { port, a, b } = await pair(t,
    { perPersona: { "codex-a": { turns: [slow], ...quickA }, "codex-b": quickA } },
    { defaultTurn: [{ text: "from-B" }, { complete: {} }] });

  const stream = post(port, "/codex-a/v1/messages", msgBody({ stream: true, sessionId: "aaaaaaaa-0000-0000-0000-00000000000a" }), false);
  await waitFor(() => a.trace().some(e => e.kind === "in" && e.frame?.method === "turn/start"), 5000);

  // Two request loops across the switch: fresh connections, and one kept-alive connection.
  const keep = new http.Agent({ keepAlive: true, maxSockets: 1 });
  t.after(() => keep.destroy());
  let ackAt = null, stop = false, n = 0;
  const rows = { fresh: [], reused: [] };
  // The stated window (README "Generations", Retire): the retiring generation closes its idle
  // kept-alive connection, and a request the client writes at that instant gets a
  // connection-level error with no response byte. A client retries it; so does this loop, once,
  // and only that error on the reused connection. Anything else is a failure.
  const idleCloseRace = r => !r.ok && r.status === undefined && /socket hang up|ECONNRESET|EPIPE/.test(r.error ?? "");
  let idleCloses = 0;
  const loop = (kind, agent) => (async () => {
    while (!stop) {
      const sentAt = Date.now();
      const body = msgBody({ sessionId: `bbbbbbbb-0000-0000-0000-${String(++n).padStart(12, "0")}` });
      let r = await post(port, "/codex-b/v1/messages", body, agent);
      if (kind === "reused" && idleCloseRace(r)) {
        idleCloses++;
        r = await post(port, "/codex-b/v1/messages", body, agent);
      }
      rows[kind].push({ sentAt, afterAck: ackAt !== null && sentAt > ackAt, ok: r.ok && r.status === 200, by: textOf(r), err: r.error ?? r.text });
      await sleep(5);
    }
  })();
  const loops = [loop("fresh", false), loop("reused", keep)];

  await sleep(300);
  b.release();
  await b.ready();
  await sleep(300);
  a.child.kill("SIGUSR1");
  await a.retired();
  ackAt = Date.now();
  await sleep(600);
  stop = true;
  await Promise.all(loops);

  for (const kind of ["fresh", "reused"]) {
    const failed = rows[kind].filter(r => !r.ok);
    assert.deepEqual(failed, [], `${kind}: no request fails across the switch`);
    assert.ok(rows[kind].length > 5, `${kind}: the loop ran (${rows[kind].length})`);
    const late = rows[kind].filter(r => r.afterAck);
    assert.ok(late.length > 0, `${kind}: requests were sent after the acknowledged retire`);
    assert.deepEqual(late.filter(r => r.by !== "from-B"), [], `${kind}: only B answers after the ack`);
  }
  assert.ok([...rows.fresh, ...rows.reused].some(r => r.by === "from-A"), "A answered before the switch");
  // One kept-alive connection, closed once by the one retire: at most one retried request.
  assert.ok(idleCloses <= 1, `the idle-close race was hit at most once (${idleCloses})`);

  const s = await stream;
  assert.equal(s.ok && s.status, 200, s.error);
  const text = s.events.filter(e => e.data?.delta?.type === "text_delta").map(e => e.data.delta.text).join("");
  assert.equal(text, Array.from({ length: 12 }, (_, i) => `s${i} `).join(""), "the stream is intact");
  assert.equal(s.events.at(-1)?.event, "message_stop");

  const ex = await a.exited;
  assert.equal(ex.code, 0, a.err());
  assert.match(a.err(), /codex-sidecar: retired/);
  assert.doesNotMatch(a.err(), /retire deadline/);
  assert.equal(b.child.exitCode, null, "B keeps serving");
});

test("a tool-result continuation whose call A delivered is completed by B on a transcript-seeded thread", async t => {
  const tools = [{ name: "Bash", description: "run", input_schema: { type: "object" } }];
  const { port, a, b } = await pair(t,
    { turns: [[call("call_gen", "Bash", { command: "ls" }), { awaitToolResults: 1 }, { text: "A-done" }, { complete: {} }]] },
    { defaultTurn: [{ text: "B-done" }, { complete: {} }] });
  const sid = "cccccccc-0000-0000-0000-00000000000c";

  const first = await post(port, "/codex-a/v1/messages", msgBody({ tools, sessionId: sid }), false);
  assert.equal(first.status, 200, first.text);
  assert.equal(first.json.stop_reason, "tool_use");
  const block = first.json.content.find(x => x.type === "tool_use");
  assert.equal(block.id, "call_gen");

  b.release();
  await b.ready();
  a.child.kill("SIGUSR1");
  await a.retired();

  const second = await post(port, "/codex-a/v1/messages", msgBody({
    tools,
    sessionId: sid,
    messages: [
      { role: "user", content: "hi" },
      { role: "assistant", content: [block] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "call_gen", content: "file list" }] },
    ],
  }), false);
  assert.equal(second.status, 200, second.text);
  assert.equal(second.json.stop_reason, "end_turn");
  assert.equal(textOf(second), "B-done");
  const [turn] = sentTo(b.trace(), "turn/start");
  const input = turn.params.input[0].text;
  assert.ok(input.includes("[tool_use Bash id=call_gen]"), input);
  assert.ok(input.includes("file list"), input);

  const ex = await a.exited;
  assert.equal(ex.code, 0, a.err());
  assert.match(a.err(), /codex-sidecar: retired/);
});

test("SIGTERM to a retiring generation exits within 5 s", async t => {
  const long = [];
  for (let i = 0; i < 200; i++) long.push({ text: "x" }, { delay: 100 });
  long.push({ complete: {} });
  const { port, a } = await pair(t, { turns: [long] }, { defaultTurn: [{ complete: {} }] });

  const stream = post(port, "/codex-a/v1/messages", msgBody({ stream: true, sessionId: "dddddddd-0000-0000-0000-00000000000d" }), false);
  await waitFor(() => a.trace().some(e => e.kind === "in" && e.frame?.method === "turn/start"), 5000);
  a.child.kill("SIGUSR1");
  await a.retired();
  await sleep(200);
  assert.equal(a.child.exitCode, null, "the stream holds the retiring generation open");
  const t0 = Date.now();
  a.child.kill("SIGTERM");
  const ex = await a.exited;
  assert.ok(ex.at - t0 < 5000, `exited ${ex.at - t0} ms after SIGTERM`);
  assert.equal(ex.code, 0);
  await stream;
});
