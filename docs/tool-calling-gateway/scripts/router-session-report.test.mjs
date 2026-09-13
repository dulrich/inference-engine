// Gate for router-session-report.mjs, against the real measured local-leg session slice
// (Claude Code 2.1.270, claude-local/qwen38-27b-iq3s, 2026-09-13 16:59-17:04 UTC).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import {
  parseArgs, matches, readRows, classify, analyseRow, summarise, renderMarkdown, run,
} from './router-session-report.mjs';

const FIXTURE = new URL('./fixtures/local-leg-session-2026-09-13.jsonl', import.meta.url).pathname;
const WINDOW = ['--since', '2026-09-13T16:59:00Z', '--until', '2026-09-13T17:05:00Z'];

function collect() {
  let text = '';
  return { stdout: { write: (s) => { text += s; return true; } }, read: () => text };
}

async function report(extraArgs = []) {
  const opts = parseArgs([FIXTURE, ...WINDOW, '--leg', 'claude-local', ...extraArgs]);
  const acc = await readRows(opts.files, opts);
  const analysed = acc.rows.map(analyseRow);
  return { opts, acc, analysed, summary: summarise(analysed, acc) };
}

test('selects the session window: 6 request rows, 306 progress rows skipped, 0 malformed', async () => {
  const { acc, summary } = await report();
  assert.equal(acc.lines, 312);
  assert.equal(acc.requests, 6);
  assert.equal(acc.rows.length, 6);
  assert.equal(summary.progressRowsSkipped, 306);
  assert.equal(summary.malformedLines, 0);
  // progress rows share the reqId of a request row but must never become report rows
  assert.ok(acc.rows.every((r) => r.kind === 'request'));
});

test('5 /v1/messages rows carry timings; count_tokens is listed but not timed', async () => {
  const { analysed, summary } = await report();
  const messages = analysed.filter((r) => r.route === '/v1/messages');
  assert.equal(messages.length, 5);
  assert.ok(messages.every((r) => r.hasTimings));
  assert.equal(summary.turnsTimed, 5);

  const countTokens = analysed.filter((r) => r.route === '/v1/messages/count_tokens');
  assert.equal(countTokens.length, 1);
  assert.equal(countTokens[0].hasTimings, false);
  assert.equal(countTokens[0].timingsExpected, false);
  assert.equal(summary.turnsUntimedRoute, 1);
  assert.equal(summary.untimedRoutes[0].route, '/v1/messages/count_tokens');
  // listed in the table, excluded from every timing statistic
  assert.equal(countTokens[0].class, null);
  assert.equal(summary.promptTokensProcessed, 40828);
});

test('no conversion-leg timings loss: the null-timings defect list is empty', async () => {
  const { summary } = await report();
  assert.deepEqual(summary.missingTimings, []);
});

test('cold/warm classification: 1 cold, 3 warm-small, 1 warm-large', async () => {
  const { summary } = await report();
  assert.equal(summary.classes.cold.turns, 1);
  assert.equal(summary.classes['warm-small'].turns, 3);
  assert.equal(summary.classes['warm-large'].turns, 1);

  // cold is cache_n === 0, warm splits at prompt_n = 1000
  assert.equal(classify({ cache_n: 0, prompt_n: 12791 }), 'cold');
  assert.equal(classify({ cache_n: 12787, prompt_n: 999 }), 'warm-small');
  assert.equal(classify({ cache_n: 12787, prompt_n: 1000 }), 'warm-large');
  assert.equal(classify(null), null);
});

test('turn 1 is the cold turn: 12,791 tokens at 733 tok/s prefill, 33.3 tok/s decode', async () => {
  const { analysed } = await report();
  const t1 = analysed[0];
  assert.equal(t1.class, 'cold');
  assert.equal(t1.reqId, '9007eb63-0303-41bc-b86e-1e31415adb84');
  assert.equal(t1.cacheTokens, 0);
  assert.equal(t1.promptTokens, 12791);
  assert.equal(Math.round(t1.prefillTps), 733);
  assert.equal(t1.decodeTps.toFixed(1), '33.3');
  assert.equal(t1.predictedTokens, 293);
  assert.equal(t1.ttftMs, 28860);
  assert.equal(t1.wallMs, 38037);
  // router-measured TTFT minus llama-side prefill = model load + queue (~11.4 s)
  assert.equal(Math.round(t1.prefillMs), 17456);
  assert.equal(Math.round(t1.loadQueueMs), 11404);
});

test('warm turns: TTFT 0.8-1.3 s, prefill 233-361 tok/s, decode 30.2-31.0 tok/s', async () => {
  const { summary } = await report();
  const w = summary.classes['warm-small'];
  assert.equal(w.ttftMs.min, 794);
  assert.equal(w.ttftMs.max, 1318);
  assert.equal(Math.round(w.prefillTps.min), 233);
  assert.equal(Math.round(w.prefillTps.max), 361);
  assert.equal(w.decodeTps.min.toFixed(1), '30.2');
  assert.equal(w.decodeTps.max.toFixed(1), '31.0');

  // the big-Read turn: 27,229 new tokens at 523 tok/s, TTFT 52 s
  const l = summary.classes['warm-large'];
  assert.equal(Math.round(l.prefillTps.median), 523);
  assert.equal(l.ttftMs.median, 52148);
});

test('final answer turn decodes 2,846 tokens at 30.2 tok/s; peak context 40,812', async () => {
  const { analysed, summary } = await report();
  const final = analysed[analysed.length - 1];
  assert.equal(final.stopReason, 'stop');
  assert.equal(final.predictedTokens, 2846);
  assert.equal(final.decodeTps.toFixed(1), '30.2');
  assert.equal(final.wallMs, 95429);
  assert.equal(summary.peakContextTokens, 40812);
  assert.equal(summary.cacheTokensReused, 106743);
});

test('sessionId is null on every local-leg row, so --session selects nothing there', async () => {
  const { analysed } = await report();
  assert.ok(analysed.every((r) => r.sessionId === null));
  const opts = parseArgs([FIXTURE, ...WINDOW, '--session', '567ffdd1-e620-4710-96d6-69fc8dac6a7c']);
  const acc = await readRows(opts.files, opts);
  assert.equal(acc.rows.length, 0);
  // but --session does select a hosted-leg row where the field is populated
  const hosted = { kind: 'request', ts: '2026-09-13T17:00:00Z', sessionId: 'abc', leg: 'claude-hosted' };
  assert.equal(matches(hosted, parseArgs([FIXTURE, '--session', 'abc'])), true);
  assert.equal(matches(hosted, parseArgs([FIXTURE, '--session', 'zzz'])), false);
});

test('--since/--until and --leg/--model narrow the window', async () => {
  const narrow = parseArgs([FIXTURE, '--since', '2026-09-13T17:02:00Z', '--until', '2026-09-13T17:05:00Z']);
  assert.equal((await readRows(narrow.files, narrow)).rows.length, 3);
  const otherLeg = parseArgs([FIXTURE, ...WINDOW, '--leg', 'claude-hosted']);
  assert.equal((await readRows(otherLeg.files, otherLeg)).rows.length, 0);
  const byModel = parseArgs([FIXTURE, ...WINDOW, '--model', 'qwen38-27b-iq3s']);
  assert.equal((await readRows(byModel.files, byModel)).rows.length, 6);
  assert.throws(() => parseArgs([FIXTURE, '--since', 'not-a-date']), /ISO-8601/);
  assert.throws(() => parseArgs([]), /at least one jsonl file/);
});

test('a completion row with upstreamTimings:null is reported, not crashed', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'router-session-report-'));
  const path = join(dir, 'synthetic.jsonl');
  writeFileSync(path, [
    JSON.stringify({
      kind: 'request', ts: '2026-09-13T17:00:00.000Z', reqId: 'dead0000-0000-0000-0000-000000000000',
      route: '/v1/messages', leg: 'claude-local', modelResolved: 'qwen38-27b-iq3s', sessionId: null,
      status: 200, stopReason: 'stop', usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0 },
      ttftMs: 120, wallMs: 900, upstreamTimings: null,
    }),
    '{ this is not json',
    JSON.stringify({ kind: 'progress', ts: '2026-09-13T17:00:00.500Z', reqId: 'dead0000', phase: 'decode' }),
  ].join('\n'), 'utf8');

  const opts = parseArgs([path, ...WINDOW]);
  const acc = await readRows(opts.files, opts);
  assert.equal(acc.malformed, 1);
  assert.equal(acc.rows.length, 1);
  const analysed = acc.rows.map(analyseRow);
  const summary = summarise(analysed, acc);
  assert.equal(analysed[0].hasTimings, false);
  assert.equal(analysed[0].timingsExpected, true);
  assert.equal(analysed[0].loadQueueMs, null);
  assert.equal(summary.missingTimings.length, 1);
  assert.equal(summary.missingTimings[0].reqId, 'dead0000-0000-0000-0000-000000000000');
  const md = renderMarkdown(analysed, summary, opts);
  assert.match(md, /Upstream timings MISSING on 1 completion row/);
  assert.match(md, /dead0000-0000-0000-0000-000000000000/);
});

test('--md output is stable and --json carries the same numbers', async () => {
  const md = collect();
  assert.equal(await run([FIXTURE, ...WINDOW, '--leg', 'claude-local', '--md'], md.stdout), 0);
  const text = md.read();
  assert.match(text, /\| 09-13 16:59:46\.927 \| 9007eb63 \| \/v1\/messages \| 200 \| tool_calls \| cold \| 0 \| 12791 \| 733 \| 28860 \| 11404 \| 293 \| 33\.3 \| 38037 \| 12791\/293\/0 \|/);
  assert.match(text, /no conversion-leg loss/);
  assert.match(text, /peak context 40812 tokens/);

  const js = collect();
  assert.equal(await run([FIXTURE, ...WINDOW, '--leg', 'claude-local', '--json'], js.stdout), 0);
  const parsed = JSON.parse(js.read());
  assert.equal(parsed.rows.length, 6);
  assert.equal(parsed.summary.peakContextTokens, 40812);
  assert.equal(parsed.filters.leg, 'claude-local');
});

test('exit 2 when no rows match the window', async () => {
  const out = collect();
  assert.equal(await run([FIXTURE, '--since', '2027-01-01T00:00:00Z'], out.stdout), 2);
  assert.equal(out.read(), '');
});

test('the fixture is read-only: the reader never writes to it', async () => {
  const before = readFileSync(FIXTURE);
  await report();
  assert.deepEqual(readFileSync(FIXTURE), before);
});

test('the embedded doc table matches the reader output byte-for-byte', async () => {
  const out = collect();
  await run([FIXTURE, ...WINDOW, '--leg', 'claude-local', '--md'], out.stdout);
  const generated = out.read().trimEnd();
  const doc = readFileSync(new URL('../local-leg-session-report-2026-09-13.md', import.meta.url).pathname, 'utf8');
  const block = doc.split('<!-- BEGIN router-session-report --md -->')[1]?.split('<!-- END router-session-report --md -->')[0];
  assert.ok(block, 'report doc must carry the generated block between the marker comments');
  assert.equal(block.trim(), generated.trim());
});
