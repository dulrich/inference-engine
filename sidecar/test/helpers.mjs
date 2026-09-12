// helpers.mjs — shared harness for the codex-sidecar unit tests.
// Every sidecar started here binds an ephemeral port on loopback only
// (SIDECAR_PORT=0, SIDECAR_BIND_EXTRA empty) and keeps all state under a temp dir.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { startSidecar } from "../codex-sidecar.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const FAKE = path.join(HERE, "fake-app-server.mjs");
export const PERSONAS = ["codex-a", "codex-b"];

// A scenario whose single turn streams two deltas and completes.
export const SIMPLE_TURN = [{ text: "hello " }, { text: "world" }, { complete: {} }];

export async function withSidecar({ scenario = {}, perPersona = null, env = {}, personas = PERSONAS } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sidecar-test-"));
  for (const p of personas) {
    fs.writeFileSync(path.join(dir, `${p}.json`), JSON.stringify(perPersona?.[p] ?? scenario));
  }
  const handle = await startSidecar({
    HOME: dir,                                   // nothing may touch the real home
    SIDECAR_KEY: "test-key",
    SIDECAR_PORT: "0",
    SIDECAR_BIND_EXTRA: "",
    SIDECAR_PERSONAS: personas.join(","),
    SIDECAR_APP_SERVER_CMD: `${process.execPath} ${FAKE} ${dir}/{persona}.json ${dir}/{persona}.trace.jsonl`,
    SIDECAR_LOG_DIR: path.join(dir, "logs"),
    SIDECAR_STATE_DIR: path.join(dir, "state"),
    SIDECAR_PING_MS: "60000",
    SIDECAR_BATCH_MS: "80",
    SIDECAR_TURN_TIMEOUT_MS: "8000",
    SIDECAR_BACKOFF_MS: "150",
    ...env,
  });
  return {
    port: handle.port,
    key: "test-key",
    dir,
    handle,
    trace(persona = PERSONAS[0]) { return readJsonl(path.join(dir, `${persona}.trace.jsonl`)); },
    log() { return readLog(path.join(dir, "logs")); },
    async close() {
      await handle.close();
      try { fs.rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
    },
  };
}

function readJsonl(file) {
  let text = "";
  try { text = fs.readFileSync(file, "utf8"); } catch { return []; }
  return text.split("\n").filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
}

function readLog(logDir) {
  let names = [];
  try { names = fs.readdirSync(logDir); } catch { return []; }
  return names.flatMap(n => readJsonl(path.join(logDir, n)));
}

// The frames the sidecar sent to the app-server, by method.
export const sentTo = (trace, method) => trace
  .filter(e => e.kind === "in" && e.frame?.method === method)
  .map(e => e.frame);

export async function req(h, pathname, { method = "POST", body, key = h.key, headers = {}, keyHeader = "authorization" } = {}) {
  const hdr = { "content-type": "application/json", ...headers };
  if (key !== null) hdr[keyHeader] = keyHeader === "authorization" ? `Bearer ${key}` : key;
  const r = await fetch(`http://127.0.0.1:${h.port}${pathname}`, {
    method,
    headers: hdr,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: r.status, headers: Object.fromEntries(r.headers), text, json, events: parseSse(text) };
}

export function parseSse(text) {
  const out = [];
  for (const block of String(text).split("\n\n")) {
    const ev = /^event: (.+)$/m.exec(block);
    const data = /^data: (.+)$/m.exec(block);
    if (!ev) continue;
    let parsed = null;
    try { parsed = data ? JSON.parse(data[1]) : null; } catch { /* ignore */ }
    out.push({ event: ev[1], data: parsed });
  }
  return out;
}

// Read an SSE response incrementally; `until` decides when to stop.
export async function streamSse(h, pathname, body, { until, key = h.key } = {}) {
  const ac = new AbortController();
  const r = await fetch(`http://127.0.0.1:${h.port}${pathname}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: JSON.stringify(body),
    signal: ac.signal,
  });
  const events = [];
  let buf = "";
  try {
    for await (const chunk of r.body) {
      buf += Buffer.from(chunk).toString("utf8");
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i);
        buf = buf.slice(i + 2);
        const [ev] = parseSse(`${block}\n\n`);
        if (ev) events.push(ev);
      }
      if (until && until(events)) { ac.abort(); break; }
    }
  } catch (e) {
    if (e?.name !== "AbortError") throw e;
  }
  return { status: r.status, events };
}

const DEVICE = "device-0000";
export function msgBody({
  model = "gpt-5.6-luna",
  system = "You are a helpful coding assistant.",
  messages = [{ role: "user", content: "hi" }],
  tools,
  stream = false,
  sessionId = "11111111-2222-3333-4444-555555555555",
  extra = {},
} = {}) {
  const body = {
    model,
    max_tokens: 4096,
    system: typeof system === "string" ? [{ type: "text", text: system }] : system,
    messages,
    stream,
    ...extra,
  };
  if (tools !== undefined) body.tools = tools;
  if (sessionId !== null) {
    body.metadata = { user_id: JSON.stringify({ device_id: DEVICE, account_uuid: "acct-1", session_id: sessionId }) };
  }
  return body;
}

export const envMessage = cwd => ({
  role: "system",
  content: [{ type: "text", text: `Here is useful environment information:\n\nPrimary working directory: ${cwd}\nPlatform: linux` }],
});

export const waitFor = async (fn, timeoutMs = 4000) => {
  const t0 = Date.now();
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > timeoutMs) throw new Error("waitFor timed out");
    await new Promise(r => setTimeout(r, 20));
  }
};
