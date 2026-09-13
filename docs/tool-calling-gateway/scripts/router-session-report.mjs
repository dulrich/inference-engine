#!/usr/bin/env node
// Per-session TTFT / prefill / decode report over the router telemetry jsonl sink
// (patch 0006, `kind:"request"` terminal records).
//
// The local leg drops `metadata.user_id` before the upstream body, so `sessionId` is
// null on every claude-local row: a local-leg "session" is a TIME WINDOW, selected with
// --since/--until (+ --leg/--model). --session stays available for the hosted legs,
// where the field is populated.
//
// Node >= 24 stdlib only. Read-only on its inputs. Streams line by line: the sink
// rotates at 16 MiB and is never slurped whole.

import { createReadStream } from 'node:fs';
import { createInterface } from 'node:readline';

/** prompt_n below this on a warm turn is an incremental "small delta" turn. */
const WARM_LARGE_PROMPT_TOKENS = 1000;

/** Routes that legitimately carry no llama-side `timings` object. */
const NON_TIMING_ROUTES = new Set(['/v1/messages/count_tokens']);

const USAGE = `usage: router-session-report.mjs <jsonl...> [options]

  --since <iso>     include rows with ts >= this ISO-8601 UTC instant
  --until <iso>     include rows with ts <= this ISO-8601 UTC instant
  --leg <leg>       filter on the telemetry \`leg\` field (e.g. claude-local)
  --model <id>      filter on modelRequested or modelResolved
  --session <id>    filter on \`sessionId\` (hosted legs only; null on the local leg)
  --md              markdown report (default)
  --json            machine-readable JSON report
  -h, --help        this text

exit 0 report written | 1 bad invocation / read error | 2 no matching rows`;

// --------------------------------------------------------------------------- args

export function parseArgs(argv) {
  const opts = { files: [], since: null, until: null, leg: null, model: null, session: null, format: 'md' };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const needsValue = () => {
      const value = argv[i + 1];
      if (value === undefined || value.startsWith('--')) throw new Error(`${arg} needs a value`);
      i += 1;
      return value;
    };
    switch (arg) {
      case '--since': opts.since = parseInstant(needsValue(), '--since'); break;
      case '--until': opts.until = parseInstant(needsValue(), '--until'); break;
      case '--leg': opts.leg = needsValue(); break;
      case '--model': opts.model = needsValue(); break;
      case '--session': opts.session = needsValue(); break;
      case '--md': opts.format = 'md'; break;
      case '--json': opts.format = 'json'; break;
      case '-h': case '--help': opts.help = true; break;
      default:
        if (arg.startsWith('-')) throw new Error(`unknown flag ${arg}`);
        opts.files.push(arg);
    }
  }
  if (!opts.help && opts.files.length === 0) throw new Error('at least one jsonl file is required');
  return opts;
}

function parseInstant(value, flag) {
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) throw new Error(`${flag}: not an ISO-8601 instant: ${value}`);
  return ms;
}

// -------------------------------------------------------------------------- reading

export function matches(row, opts) {
  if (row.kind !== 'request') return false;
  const ts = Date.parse(row.ts);
  if (Number.isNaN(ts)) return false;
  if (opts.since !== null && ts < opts.since) return false;
  if (opts.until !== null && ts > opts.until) return false;
  if (opts.leg !== null && row.leg !== opts.leg) return false;
  if (opts.model !== null && row.modelRequested !== opts.model && row.modelResolved !== opts.model) return false;
  if (opts.session !== null && row.sessionId !== opts.session) return false;
  return true;
}

/** Stream one file, appending matching `request` rows to `acc.rows`. */
async function readFile(path, opts, acc) {
  const rl = createInterface({ input: createReadStream(path, 'utf8'), crlfDelay: Infinity });
  for await (const line of rl) {
    if (line.trim() === '') continue;
    acc.lines += 1;
    let row;
    try {
      row = JSON.parse(line);
    } catch {
      acc.malformed += 1;
      continue;
    }
    if (row === null || typeof row !== 'object' || typeof row.kind !== 'string') {
      acc.malformed += 1;
      continue;
    }
    if (row.kind === 'progress') acc.progress += 1;
    if (row.kind !== 'request') continue;
    acc.requests += 1;
    if (matches(row, opts)) acc.rows.push(row);
  }
}

export async function readRows(files, opts) {
  const acc = { lines: 0, malformed: 0, progress: 0, requests: 0, rows: [] };
  for (const path of files) await readFile(path, opts, acc);
  acc.rows.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts) || a.reqId.localeCompare(b.reqId));
  return acc;
}

// ------------------------------------------------------------------------ analysis

/** cold = nothing reused from the kv cache; otherwise warm, split on the prefill delta. */
export function classify(timings) {
  if (!timings) return null;
  if (timings.cache_n === 0) return 'cold';
  return timings.prompt_n < WARM_LARGE_PROMPT_TOKENS ? 'warm-small' : 'warm-large';
}

export function analyseRow(row) {
  const t = row.upstreamTimings ?? null;
  const out = {
    ts: row.ts,
    reqId: row.reqId,
    route: row.route,
    leg: row.leg ?? null,
    modelResolved: row.modelResolved ?? null,
    sessionId: row.sessionId ?? null,
    status: row.status ?? null,
    stopReason: row.stopReason ?? null,
    error: row.error ?? null,
    ttftMs: row.ttftMs ?? null,
    wallMs: row.wallMs ?? null,
    usage: row.usage ?? null,
    hasTimings: t !== null,
    timingsExpected: !NON_TIMING_ROUTES.has(row.route),
    class: classify(t),
    cacheTokens: t ? t.cache_n : null,
    promptTokens: t ? t.prompt_n : null,
    prefillMs: t ? t.prompt_ms : null,
    prefillTps: t ? t.prompt_per_second : null,
    predictedTokens: t ? t.predicted_n : null,
    decodeMs: t ? t.predicted_ms : null,
    decodeTps: t ? t.predicted_per_second : null,
    contextTokens: t ? t.cache_n + t.prompt_n : null,
    // router-measured TTFT includes model load and queueing; prompt_ms is llama-side
    // prefill only. The difference is the load/queue cost of the turn.
    loadQueueMs: t !== null && typeof row.ttftMs === 'number' ? row.ttftMs - t.prompt_ms : null,
  };
  return out;
}

function median(values) {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

function stat(values) {
  const clean = values.filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (clean.length === 0) return null;
  return { n: clean.length, median: median(clean), min: Math.min(...clean), max: Math.max(...clean) };
}

export function summarise(analysed, acc) {
  const timed = analysed.filter((r) => r.hasTimings);
  const untimedRoutes = analysed.filter((r) => !r.hasTimings && !r.timingsExpected);
  // The plan's defect hook: a completion row that SHOULD carry llama timings but does
  // not means the conversion leg lost them upstream of the sink.
  const missingTimings = analysed.filter((r) => !r.hasTimings && r.timingsExpected);

  const classes = {};
  for (const name of ['cold', 'warm-small', 'warm-large']) {
    const rows = timed.filter((r) => r.class === name);
    classes[name] = {
      turns: rows.length,
      ttftMs: stat(rows.map((r) => r.ttftMs)),
      prefillTps: stat(rows.map((r) => r.prefillTps)),
      decodeTps: stat(rows.map((r) => r.decodeTps)),
      loadQueueMs: stat(rows.map((r) => r.loadQueueMs)),
    };
  }

  const first = analysed[0];
  const last = analysed[analysed.length - 1];
  // `ts` is the terminal record, written when the request finishes; the request began
  // wallMs earlier. Span = first request's start -> last request's end.
  const spanMs = Date.parse(last.ts) - (Date.parse(first.ts) - (first.wallMs ?? 0));

  return {
    turnsTimed: timed.length,
    turnsUntimedRoute: untimedRoutes.length,
    firstTs: first.ts,
    lastTs: last.ts,
    spanMs,
    classes,
    promptTokensProcessed: timed.reduce((a, r) => a + r.promptTokens, 0),
    cacheTokensReused: timed.reduce((a, r) => a + r.cacheTokens, 0),
    predictedTokens: timed.reduce((a, r) => a + r.predictedTokens, 0),
    peakContextTokens: timed.length ? Math.max(...timed.map((r) => r.contextTokens)) : null,
    missingTimings: missingTimings.map((r) => ({ ts: r.ts, reqId: r.reqId, route: r.route, status: r.status })),
    untimedRoutes: untimedRoutes.map((r) => ({ ts: r.ts, reqId: r.reqId, route: r.route, status: r.status })),
    linesRead: acc.lines,
    requestRows: acc.requests,
    progressRowsSkipped: acc.progress,
    malformedLines: acc.malformed,
  };
}

// ------------------------------------------------------------------------ rendering

const n0 = (v) => (typeof v === 'number' ? String(Math.round(v)) : '—');
const n1 = (v) => (typeof v === 'number' ? v.toFixed(1) : '—');
const s1 = (v) => (typeof v === 'number' ? (v / 1000).toFixed(1) : '—');
const txt = (v) => (v === null || v === undefined ? '—' : String(v));

function usageCell(u) {
  if (!u) return '—';
  const part = (v) => (typeof v === 'number' ? String(v) : '—');
  return `${part(u.input)}/${part(u.output)}/${part(u.cacheRead)}`;
}

function statCell(st, fmt) {
  if (!st) return '—';
  return st.n === 1 ? fmt(st.median) : `${fmt(st.median)} (${fmt(st.min)}–${fmt(st.max)})`;
}

export function renderMarkdown(analysed, summary, opts) {
  const out = [];
  const filters = [
    opts.leg ? `leg=${opts.leg}` : null,
    opts.model ? `model=${opts.model}` : null,
    opts.session ? `session=${opts.session}` : null,
  ].filter(Boolean);
  out.push(`**Window** ${opts.since === null ? '(open)' : new Date(opts.since).toISOString()} → ${opts.until === null ? '(open)' : new Date(opts.until).toISOString()}${filters.length ? ` · ${filters.join(' · ')}` : ''}`);
  out.push(`**Input** ${opts.files.length} file(s) · ${summary.linesRead} lines · ${summary.requestRows} request · ${summary.progressRowsSkipped} progress skipped · ${summary.malformedLines} malformed`);
  out.push('');
  out.push('| ts (UTC) | req | route | st | stop | class | cache_n | prompt_n | prefill tok/s | TTFT ms | load/queue ms | predicted_n | decode tok/s | wall ms | usage in/out/cacheRead |');
  out.push('| --- | --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |');
  for (const r of analysed) {
    out.push(`| ${r.ts.slice(5, 23).replace('T', ' ')} | ${r.reqId.slice(0, 8)} | ${r.route} | ${txt(r.status)} | ${txt(r.stopReason)} | ${txt(r.class)} | ${n0(r.cacheTokens)} | ${n0(r.promptTokens)} | ${n0(r.prefillTps)} | ${n0(r.ttftMs)} | ${n0(r.loadQueueMs)} | ${n0(r.predictedTokens)} | ${n1(r.decodeTps)} | ${n0(r.wallMs)} | ${usageCell(r.usage)} |`);
  }
  out.push('');
  out.push(`**Turns** ${summary.turnsTimed} timed · ${summary.turnsUntimedRoute} untimed route(s) · span ${summary.firstTs} → ${summary.lastTs} (~${s1(summary.spanMs)} s, first start to last end)`);
  out.push('');
  out.push('| class | turns | TTFT s med (min–max) | prefill tok/s med (min–max) | decode tok/s med (min–max) |');
  out.push('| --- | ---: | --- | --- | --- |');
  for (const [name, c] of Object.entries(summary.classes)) {
    if (c.turns === 0) continue;
    out.push(`| ${name} | ${c.turns} | ${statCell(c.ttftMs, s1)} | ${statCell(c.prefillTps, n0)} | ${statCell(c.decodeTps, n1)} |`);
  }
  out.push('');
  out.push(`**Tokens** ${summary.promptTokensProcessed} prompt tokens prefilled · ${summary.cacheTokensReused} reused from kv cache · ${summary.predictedTokens} decoded · peak context ${txt(summary.peakContextTokens)} tokens`);
  out.push('');
  if (summary.missingTimings.length === 0) {
    out.push('**Upstream timings** present on every completion row — no conversion-leg loss.');
  } else {
    out.push(`**Upstream timings MISSING on ${summary.missingTimings.length} completion row(s)** — conversion-leg loss, report upstream:`);
    for (const r of summary.missingTimings) out.push(`- ${r.ts} \`${r.reqId}\` ${r.route} (status ${txt(r.status)})`);
  }
  if (summary.untimedRoutes.length > 0) {
    out.push('');
    out.push(`**Untimed routes** (no llama timings by design, excluded from timing stats): ${summary.untimedRoutes.map((r) => `\`${r.reqId.slice(0, 8)}\` ${r.route}`).join(', ')}`);
  }
  return out.join('\n');
}

// ----------------------------------------------------------------------------- main

export async function run(argv, stdout = process.stdout) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (err) {
    process.stderr.write(`router-session-report: ${err.message}\n\n${USAGE}\n`);
    return 1;
  }
  if (opts.help) {
    stdout.write(`${USAGE}\n`);
    return 0;
  }
  let acc;
  try {
    acc = await readRows(opts.files, opts);
  } catch (err) {
    process.stderr.write(`router-session-report: ${err.message}\n`);
    return 1;
  }
  if (acc.rows.length === 0) {
    process.stderr.write('router-session-report: no matching request rows in the selected window\n');
    return 2;
  }
  const analysed = acc.rows.map(analyseRow);
  const summary = summarise(analysed, acc);
  if (opts.format === 'json') {
    stdout.write(`${JSON.stringify({ filters: { since: opts.since === null ? null : new Date(opts.since).toISOString(), until: opts.until === null ? null : new Date(opts.until).toISOString(), leg: opts.leg, model: opts.model, session: opts.session }, rows: analysed, summary }, null, 2)}\n`);
  } else {
    stdout.write(`${renderMarkdown(analysed, summary, opts)}\n`);
  }
  return 0;
}

if (import.meta.filename === process.argv[1]) {
  process.exitCode = await run(process.argv.slice(2));
}
