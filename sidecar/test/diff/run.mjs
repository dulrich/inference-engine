#!/usr/bin/env node
// Differential wire-behaviour runner for codex-sidecar (test/diff/README.md).
//   node test/diff/run.mjs --a "<cmd>" --b "<cmd>" [--case <substring>]
// Run from sidecar/. Each case starts target A, then target B, through the same withSidecar
// machinery (fake app-server, ephemeral loopback port, temp dir), replays the identical request
// sequence, normalizes the transcript and compares. TAP out; exit 1 on any diff or start failure.
import fs from "node:fs";
import path from "node:path";
import { withSidecar, readJsonl, PERSONAS } from "../helpers.mjs";
import { logDirFor } from "../../outbox.mjs";
import { cases } from "./cases.mjs";

function parseArgs(argv) {
  const a = {};
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === "--a") a.a = argv[++i];
    else if (k === "--b") a.b = argv[++i];
    else if (k === "--case") a.only = argv[++i];
    else throw new Error(`unknown argument ${k}`);
  }
  if (!a.a?.trim() || !a.b?.trim()) throw new Error('usage: run.mjs --a "<cmd>" --b "<cmd>"');
  return a;
}

// ---- masking: explicit and narrow (every entry is documented in README.md) ----
// Keys whose value is masked wherever they appear (timestamps, random ids, ports, timings).
export const MASK_KEYS = new Set(["ts", "createdAt", "updatedAt", "reqId", "port", "pid", "elapsedMs", "startedAtMs"]);
// Response headers compared; everything else is transport framing (date, keep-alive, ...).
const HEADERS = ["content-type", "cache-control", "retry-after"];
// String patterns rewritten in every string value.
const PATTERNS = [
  [/\/tmp\/codex-sidecar-test-[A-Za-z0-9]+/g, "<TMP>"],        // per-run temp dir
  [/\/tmp\/codex-sidecar-diff-ob-[A-Za-z0-9]+/g, "<OBTMP>"],   // per-run outbox dir
  [/127\.0\.0\.1:\d+/g, "127.0.0.1:<PORT>"],                   // ephemeral port in binds/host
  [/^msg_[0-9a-f]{24}$/, "msg_<ID>"],                           // random Anthropic message id
];

function norm(v, key) {
  if (key && MASK_KEYS.has(key)) return v === null || v === undefined ? v : `<${key}>`;
  if (key === "retry-after" || key === "retryAfter") return /^\d+$/.test(String(v)) ? "<int>" : v;     // now-relative seconds
  if (typeof v === "string") { let s = v; for (const [re, to] of PATTERNS) s = s.replace(re, to); return s; }
  if (Array.isArray(v)) return v.map(x => norm(x));
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, norm(x, k)]));
  return v;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function send(h, s) {
  const headers = { "content-type": "application/json" };
  const key = s.key === undefined ? h.key : s.key;
  const kh = s.keyHeader ?? "authorization";
  if (key !== null) headers[kh] = kh === "authorization" ? `Bearer ${key}` : key;
  const method = s.method ?? "POST";
  const body = s.raw ?? (s.body === undefined ? undefined : JSON.stringify(s.body));
  const r = await fetch(`http://127.0.0.1:${h.port}${s.path}`, { method, headers, body: method === "GET" || method === "HEAD" ? undefined : body });
  const text = await r.text();
  const out = { step: `${method} ${s.path}`, status: r.status, headers: Object.fromEntries(HEADERS.filter(k => r.headers.has(k)).map(k => [k, r.headers.get(k)])) };
  if ((r.headers.get("content-type") || "").startsWith("text/event-stream")) {
    out.sse = text.split("\n\n").filter(b => b.trim()).map(b => {
      const ev = /^event: (.+)$/m.exec(b)?.[1] ?? null;
      const d = /^data: (.*)$/m.exec(b)?.[1];
      let data = d; try { data = JSON.parse(d); } catch { /* raw */ }
      return { event: ev, data };
    }).filter(e => e.event !== "ping");                          // keepalive cadence is timing
  } else {
    try { out.json = JSON.parse(text); } catch { out.text = text; }
  }
  return out;
}

function readOutbox(file) {
  const dir = logDirFor(file);
  let names = [];
  try { names = fs.readdirSync(dir).filter(n => n.endsWith(".jsonl")).sort(); } catch { return []; }
  // Byte-level: keep each raw line, masked as text (only the ts field may differ).
  return names.flatMap(n => fs.readFileSync(path.join(dir, n), "utf8").split("\n").filter(Boolean))
    .map(l => l.replace(/"ts":"[^"]*"/, '"ts":"<ts>"'));
}

const multiset = rows => rows.map(r => JSON.stringify(norm(r))).sort();

function groupTrace(frames) {
  const g = {};
  for (const e of frames) {
    const m = e.frame?.method ?? `${e.kind}:response`;
    (g[m] ??= []).push(norm({ kind: e.kind, frame: e.frame }));
  }
  return Object.fromEntries(Object.keys(g).sort().map(k => [k, g[k]]));
}

async function transcript(target, c) {
  const obDir = c.outbox ? fs.mkdtempSync("/tmp/codex-sidecar-diff-ob-") : null;
  const env = { ...(c.env ?? {}), ...(obDir ? { SIDECAR_OUTBOX_PATH: path.join(obDir, "outbox", "events.db") } : {}) };
  const scenario = c.scenario;
  const h = await withSidecar({ scenario, perPersona: c.perPersona ?? null, env, target });
  try {
    const responses = [];
    for (const s of c.steps) responses.push(norm(await send(h, s)));
    await sleep(300);                                            // let async log rows land
    await h.handle.close();
    const trace = Object.fromEntries(PERSONAS.map(p => [p, groupTrace(readJsonl(path.join(h.dir, `${p}.trace.jsonl`)))]));
    const t = { responses, trace, log: multiset(h.log()) };
    if (obDir) t.outbox = readOutbox(path.join(obDir, "outbox", "events.db"));
    return t;
  } finally {
    await h.close().catch(() => {});
    if (obDir) fs.rmSync(obDir, { recursive: true, force: true });
  }
}

// First difference as a path + both values.
function firstDiff(a, b, at = "$") {
  if (JSON.stringify(a) === JSON.stringify(b)) return null;
  if (a && b && typeof a === "object" && typeof b === "object" && Array.isArray(a) === Array.isArray(b)) {
    const keys = Array.isArray(a) ? [...Array(Math.max(a.length, b.length)).keys()] : [...new Set([...Object.keys(a), ...Object.keys(b)])];
    for (const k of keys) {
      const d = firstDiff(a[k], b[k], `${at}${Array.isArray(a) ? `[${k}]` : `.${k}`}`);
      if (d) return d;
    }
  }
  return { at, a, b };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const list = cases.filter(c => !args.only || c.name.includes(args.only));
  console.log("TAP version 13");
  console.log(`1..${list.length}`);
  let failed = 0;
  for (let [i, c] of list.entries()) {
    const n = i + 1;
    try {
      // A time-derived scenario is built once so both targets replay identical upstream input.
      if (typeof c.scenario === "function") c = { ...c, scenario: c.scenario() };
      const ta = await transcript(args.a, c);
      const tb = await transcript(args.b, c);
      const d = firstDiff(ta, tb);
      if (!d) { console.log(`ok ${n} - ${c.name}`); continue; }
      failed++;
      console.log(`not ok ${n} - ${c.name}`);
      console.log(`  # first difference at ${d.at}`);
      console.log(`  # a: ${JSON.stringify(d.a)}`);
      console.log(`  # b: ${JSON.stringify(d.b)}`);
    } catch (e) {
      failed++;
      console.log(`not ok ${n} - ${c.name}`);
      for (const l of String(e?.stack ?? e).split("\n").slice(0, 12)) console.log(`  # ${l}`);
    }
  }
  console.log(`# ${list.length - failed}/${list.length} ok`);
  process.exit(failed ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(2); });
