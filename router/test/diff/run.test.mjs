import { test, expect } from 'bun:test';
import { isLongCase, maxScriptDelayMs, LONG_CASE_MS } from './run.mjs';
import { cases } from './cases.mjs';

test('maxScriptDelayMs finds the largest nested head/chunk delay', () => {
  expect(maxScriptDelayMs({})).toBe(0);
  expect(maxScriptDelayMs({ a: { headDelayMs: 16000 } })).toBe(16000);
  expect(maxScriptDelayMs({ a: { chunks: [{ data: 'x' }, { delayMs: 17000 }, { delayMs: 5 }] } })).toBe(17000);
  expect(maxScriptDelayMs({ a: [{ status: 200 }, { headDelayMs: 3 }] })).toBe(3);
});

test('isLongCase flags exactly the cases that hold a connection open >= LONG_CASE_MS', () => {
  expect(isLongCase({ script: { a: { headDelayMs: LONG_CASE_MS } } })).toBe(true);
  expect(isLongCase({ script: { a: { headDelayMs: LONG_CASE_MS - 1 } } })).toBe(false);
  expect(isLongCase({})).toBe(false);
  const long = cases.filter(isLongCase).map((c) => c.id).sort();
  expect(long).toEqual([
    'client-disconnect-closes-upstream',
    'local-keepalive-ping-after-15s',
    'slow-headers-16s-no-gateway-timeout',
    'slow-headers-16s-stream',
    'sse-midstream-silence-16s-relay-untouched',
  ]);
});
