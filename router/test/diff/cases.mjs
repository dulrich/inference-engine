// Case table for the differential harness. Tags: parity (behaviour unchanged from the retired
// Node gateway), changed (Decisions 2/3 deliberate changes), slow (needs --slow). Each case asserts on the upstream-received requests (ctx.upstream())
// and on the client-received response.
import assert from 'node:assert/strict';

const MSG = (model, extra = {}) => ({ model, max_tokens: 16, messages: [{ role: 'user', content: 'hi' }], ...extra });
const anthropicJson = (model, text = 'ok') => ({
  id: 'msg_rrdiff', type: 'message', role: 'assistant', model,
  content: [{ type: 'text', text }], stop_reason: 'end_turn', stop_sequence: null,
  usage: { input_tokens: 3, output_tokens: 1 },
});
const OK = (model, text) => ({ status: 200, json: anthropicJson(model, text) });

// Anthropic SSE with a tool_use block and input_json_delta deltas, split at awkward byte
// boundaries (mid-event, mid-line) so a re-framing relay would show up as a diff.
const SSE_EVENTS = [
  'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_rrdiff","type":"message","role":"assistant","model":"m","content":[],"stop_reason":null,"usage":{"input_tokens":5,"output_tokens":1}}}\n\n',
  'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"get_weather","input":{}}}\n\n',
  'event: ping\ndata: {"type": "ping"}\n\n',
  'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"city\\": \\"Par"}}\n\n',
  'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"is\\"}"}}\n\n',
  'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
  'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":9}}\n\n',
  'event: message_stop\ndata: {"type":"message_stop"}\n\n',
];
const SSE_TEXT = SSE_EVENTS.join('');
const sseChunks = (gapAt = -1, gapMs = 0) => {
  const out = [];
  for (let i = 0; i < SSE_TEXT.length; i += 37) out.push({ data: SSE_TEXT.slice(i, i + 37) });
  if (gapAt >= 0) out[gapAt].delayMs = gapMs;
  return out;
};
const SSE = (gapAt, gapMs) => ({ status: 200, headers: { 'content-type': 'text/event-stream' }, chunks: sseChunks(gapAt, gapMs) });

const only = (log, provider) => log.filter((r) => r.provider === provider);
async function one(ctx, provider) {
  const log = await ctx.upstream();
  assert.equal(log.length, 1, `expected exactly 1 upstream call, got ${log.map((r) => `${r.provider}${r.url}`).join(', ') || 'none'}`);
  assert.equal(log[0].provider, provider);
  return log[0];
}
async function none(ctx) {
  const log = await ctx.upstream();
  assert.equal(log.length, 0, `expected no upstream call, got ${log.map((r) => `${r.provider}${r.url}`).join(', ')}`);
}
const noCallerCreds = (rec, bearer) => {
  for (const [k, v] of Object.entries(rec.headers)) {
    assert.ok(!String(v).includes(bearer), `caller bearer leaked upstream in ${k}`);
    assert.ok(!String(v).includes('test-gw-key'), `gateway key leaked upstream in ${k}`);
  }
};
export const OAUTH = 'sk-ant-oat01-rrdiff-fake-oauth';

async function streamCase(ctx, model, provider, expectPath) {
  const r = await ctx.req('/v1/messages', { body: MSG(model, { stream: true }) });
  assert.equal(r.status, 200, r.text.slice(0, 300));
  assert.equal(r.text, SSE_TEXT, 'SSE relay is not byte-verbatim');
  const rec = await one(ctx, provider);
  assert.equal(rec.url, expectPath);
}

const LOCAL = 'claude-local/qwen38-27b-iq3s';
// A bare model id resolves to the deduped provider TYPE list [openai -> claude-local,
// anthropic -> anthropic], never by searching models[]; a prefixed id is a single target.
// x-target-providers is not read (crate tests cover multi-target).

export const cases = [
  // ---- auth matrix: the gateway reads only the raw x-gateway-key value (always on). ----
  { id: 'auth-missing', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { auth: 'none', body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 401, r.text);
      await none(ctx);
    } },
  { id: 'auth-wrong', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { auth: 'none', headers: { 'x-gateway-key': 'wrong-key' }, body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 401, r.text);
      await none(ctx);
    } },
  { id: 'auth-bare', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 200, r.text);
      await one(ctx, 'anthropic');
    } },
  { id: 'auth-bearer-form-rejected', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      // Measured on Node with AUTH_STATIC_API_KEY_BEARER_ONLY=false: a "Bearer <key>" value in
      // x-gateway-key is compared whole, so it is a 401 "Invalid API key.".
      const r = await ctx.req('/v1/messages', { auth: 'none', headers: { 'x-gateway-key': `Bearer ${ctx.gwKey}`, authorization: `Bearer ${OAUTH}` }, body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 401, r.text);
      await none(ctx);
    } },
  ...[['x-api-key', (k) => k], ['authorization', (k) => `Bearer ${k}`], ['api-key', (k) => k], ['x-goog-api-key', (k) => k]].map(([h, v]) => ({
    id: `auth-key-in-${h}-refused`, tags: ['changed'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      // Only x-gateway-key is read; a valid key in any other header is a 401.
      const r = await ctx.req('/v1/messages', { auth: 'none', headers: { [h]: v(ctx.gwKey) }, body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 401, r.text);
      assert.equal(JSON.parse(r.text).error.message, 'Missing auth token header: x-gateway-key');
      await none(ctx);
    } })),
  { id: 'auth-oauth-bearer-is-not-gateway-key', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { auth: 'none', headers: { authorization: `Bearer ${OAUTH}` }, body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 401, r.text);
      await none(ctx);
    } },

  // ---- 0001 OAuth bearer / credential rules -----------------------------------------
  { id: 'oauth-anthropic-passthrough', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { headers: { authorization: `Bearer ${OAUTH}` }, body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, 'anthropic');
      assert.equal(rec.url, '/v1/messages');
      assert.equal(rec.headers.authorization, `Bearer ${OAUTH}`);
      assert.equal(rec.headers['x-api-key'], undefined, 'x-api-key must be absent on the OAuth passthrough');
      assert.equal(rec.headers['x-gateway-key'], undefined, 'gateway key forwarded');
      assert.equal(JSON.parse(rec.body).model, 'claude-opus-5-5');
    } },
  { id: 'oauth-anthropic-prefixed-model', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { headers: { authorization: `Bearer ${OAUTH}` }, body: MSG('anthropic/claude-opus-5-5') });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, 'anthropic');
      assert.equal(JSON.parse(rec.body).model, 'claude-opus-5-5', 'provider prefix must be stripped upstream');
    } },
  ...['codex-a', 'codex-b'].map((p) => ({ id: `cred-replace-${p}`, tags: ['parity'], script: { [p]: OK('gpt-6-luna') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { headers: { authorization: `Bearer ${OAUTH}`, 'x-api-key': 'caller-x-api-key' }, body: MSG(`${p}/gpt-6-luna`) });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, p);
      assert.equal(rec.url, `/${p}/v1/messages`);
      assert.equal(rec.headers.authorization, 'Bearer test-codex-key');
      assert.equal(rec.headers['x-api-key'], 'test-codex-key', 'managed key duplicated into x-api-key (0001 keeps it)');
      noCallerCreds(rec, OAUTH);
      assert.ok(!JSON.stringify(rec.headers).includes('caller-x-api-key'));
      assert.equal(JSON.parse(rec.body).model, 'gpt-6-luna');
    } })),
  { id: 'cred-replace-or-anthropic', tags: ['parity'], script: { 'or-anthropic': OK('z-ai/glm-5.3-flash') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { headers: { authorization: `Bearer ${OAUTH}` }, body: MSG('or-anthropic/z-ai/glm-5.3-flash') });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, 'or-anthropic');
      assert.equal(rec.url, '/api/v1/messages');
      assert.equal(rec.headers.authorization, 'Bearer test-or-key');
      noCallerCreds(rec, OAUTH);
      assert.equal(JSON.parse(rec.body).model, 'z-ai/glm-5.3-flash');
    } },
  { id: 'cred-replace-claude-local', tags: ['parity'],
    script: { 'claude-local': { status: 200, json: { id: 'c1', object: 'chat.completion', model: 'qwen38-27b-iq3s', choices: [{ index: 0, message: { role: 'assistant', content: 'ok' }, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 } } } },
    async run(ctx) {
      // Path-agnostic (Node converts to /v1/chat/completions, Rust relays native): credential only.
      await ctx.req('/v1/messages', { headers: { authorization: `Bearer ${OAUTH}` }, body: MSG(LOCAL) });
      const rec = (await ctx.upstream()).find((x) => x.provider === 'claude-local');
      assert.ok(rec, 'no claude-local upstream call');
      assert.equal(rec.headers.authorization, 'Bearer local-dummy-key-not-a-secret');
      noCallerCreds(rec, OAUTH);
    } },
  { id: 'cred-nocred-provider-refused', tags: ['changed'], script: { 'rrdiff-nocred': OK('rrdiff-nocred-model') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { headers: { authorization: `Bearer ${OAUTH}` }, body: MSG('rrdiff-nocred/rrdiff-nocred-model') });
      assert.ok(r.status >= 400 && r.status < 600, `expected refusal, got ${r.status}`);
      await none(ctx);
    } },

  // ---- header forwarding --------------------------------------------------------------
  { id: 'headers-anthropic-version-beta', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages?beta=true', { headers: { authorization: `Bearer ${OAUTH}`, 'anthropic-version': '2023-06-01', 'anthropic-beta': 'oauth-2025-04-20,interleaved-thinking-2025-05-14' }, body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, 'anthropic');
      // Measured on Node: the ?beta=true query is NOT forwarded upstream; headers are.
      assert.equal(rec.url, '/v1/messages');
      assert.equal(rec.headers['anthropic-version'], '2023-06-01');
      assert.equal(rec.headers['anthropic-beta'], 'oauth-2025-04-20,interleaved-thinking-2025-05-14');
    } },

  // ---- byte-verbatim SSE relay ---------------------------------------------------------
  { id: 'sse-verbatim-anthropic', tags: ['parity'], script: { anthropic: SSE() },
    run: (ctx) => streamCase(ctx, 'claude-opus-5-5', 'anthropic', '/v1/messages') },
  { id: 'sse-verbatim-or-anthropic', tags: ['parity'], script: { 'or-anthropic': SSE() },
    run: (ctx) => streamCase(ctx, 'or-anthropic/z-ai/glm-5.3-flash', 'or-anthropic', '/api/v1/messages') },
  { id: 'sse-verbatim-codex', tags: ['parity'], script: { 'codex-a': SSE() },
    run: (ctx) => streamCase(ctx, 'codex-a/gpt-6-sol', 'codex-a', '/codex-a/v1/messages') },
  { id: 'sse-midstream-silence-16s-relay-untouched', tags: ['parity'], script: { anthropic: SSE(5, 16000) },
    run: (ctx) => streamCase(ctx, 'claude-opus-5-5', 'anthropic', '/v1/messages') },

  // ---- E3(b) upstream timeout: Node default upstreamTimeoutMs=0 (none) ---------------------
  { id: 'slow-headers-16s-no-gateway-timeout', tags: ['parity'], script: { anthropic: { ...OK('claude-opus-5-5'), headDelayMs: 16000 } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 200, r.text);
      assert.equal(JSON.parse(r.text).content[0].text, 'ok');
    } },
  { id: 'slow-headers-16s-stream', tags: ['parity'], script: { anthropic: { ...SSE(), headDelayMs: 16000 } },
    run: (ctx) => streamCase(ctx, 'claude-opus-5-5', 'anthropic', '/v1/messages') },

  // ---- 0003 final-attempt error relay / E3(d) ------------------------------------------------
  ...[
    ['nonjson-529', { status: 529, headers: { 'content-type': 'text/plain' }, body: 'upstream overloaded <html>not json</html>' }],
    ['json-400', { status: 400, json: { type: 'error', error: { type: 'invalid_request_error', message: 'System message must be at the beginning' } } }],
    ['nonjson-500-stream', { status: 500, headers: { 'content-type': 'text/plain' }, body: 'boom' }, true],
  ].map(([n, spec, stream]) => ({ id: `final-error-relay-${n}`, tags: ['parity'], script: { 'codex-b': spec },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('codex-b/gpt-6-luna', stream ? { stream: true } : {}) });
      assert.equal(r.status, spec.status);
      assert.equal(r.text, spec.body ?? JSON.stringify(spec.json), 'final error body not relayed byte-verbatim');
      await one(ctx, 'codex-b');
    } })),
  { id: 'final-error-empty-body-envelope', tags: ['parity'], script: { 'codex-b': { status: 503, body: '' } },
    async run(ctx) {
      // 0003 relays only a non-empty body; an empty final body keeps the gateway envelope.
      const r = await ctx.req('/v1/messages', { body: MSG('codex-b/gpt-6-luna') });
      // Measured on Node: the upstream status is kept (503) with the engine's envelope body.
      assert.equal(r.status, 503, r.text);
      assert.equal(JSON.parse(r.text).error.message, 'All target providers failed.');
      await one(ctx, 'codex-b');
    } },
  { id: 'final-error-relay-bare-claude-model', tags: ['parity'], script: { anthropic: { status: 400, headers: { 'content-type': 'application/json' }, body: '{"type":"error","error":{"type":"invalid_request_error","message":"rrdiff"}}' } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('claude-opus-5-5') });
      assert.equal(r.status, 400);
      assert.equal(r.text, '{"type":"error","error":{"type":"invalid_request_error","message":"rrdiff"}}');
    } },

  // ---- E3(a) multi-target fallthrough --------------------------------------------------------
  { id: 'reset-after-write-not-retried-single-target', tags: ['changed'],
    script: { 'codex-a': { destroy: true } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('codex-a/gpt-6-luna') });
      assert.ok(r.status >= 500, `${r.status} ${r.text}`);
      // The request reached codex-a before the reset, so it is not retried.
      await one(ctx, 'codex-a');
    } },
  { id: 'fallthrough-prefixed-model-single-target', tags: ['parity'],
    script: { 'codex-a': { status: 500, body: 'david down' }, 'codex-b': OK('gpt-6-luna') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('codex-a/gpt-6-luna') });
      assert.equal(r.status, 500);
      assert.equal(r.text, 'david down');
      await one(ctx, 'codex-a');
    } },
  { id: 'target-providers-header-ignored', tags: ['changed'],
    script: { 'codex-a': OK('gpt-6-luna', 'from-david'), 'codex-b': OK('gpt-6-luna', 'from-b') },
    async run(ctx) {
      // x-target-providers / x-target-provider are not read; the prefix alone routes.
      const r = await ctx.req('/v1/messages', { headers: { 'x-target-providers': 'codex-b', 'x-target-provider': 'nope' }, body: MSG('codex-a/gpt-6-luna') });
      assert.equal(r.status, 200, r.text);
      assert.equal(JSON.parse(r.text).content[0].text, 'from-david');
      await one(ctx, 'codex-a');
    } },

  // ---- E3(c) client disconnect mid-stream -------------------------------------------------
  { id: 'client-disconnect-closes-upstream', tags: ['parity'], script: { anthropic: SSE(5, 20000) },
    async run(ctx) {
      const ac = new AbortController();
      const res = await ctx.stream('/v1/messages', { body: MSG('claude-opus-5-5', { stream: true }), signal: ac.signal });
      assert.equal(res.status, 200);
      const reader = res.body.getReader();
      await reader.read();
      const abortedAt = Date.now();
      ac.abort();
      // Bound measured on Node (well under 100 ms on loopback); 3 s gives margin.
      let rec;
      while (Date.now() - abortedAt < 3000) {
        rec = only(await ctx.upstream(), 'anthropic')[0];
        if (rec?.clientClosedAtMs !== undefined) break;
        await ctx.sleep(50);
      }
      console.log(`  # measured: upstream closed ${rec?.clientClosedAtMs - rec?.receivedAtMs} ms after request start, ${Date.now() - abortedAt} ms poll after abort`);
      assert.ok(rec?.clientClosedAtMs !== undefined, 'stub never saw the upstream connection close within 3 s');
      assert.ok(!rec.streamCompleted);
    } },

  // ---- 0004 count_tokens URL rule ---------------------------------------------------------
  { id: 'count-tokens-anthropic', tags: ['parity'], script: { anthropic: { status: 200, json: { input_tokens: 7 } } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages/count_tokens', { headers: { authorization: `Bearer ${OAUTH}` }, body: { model: 'claude-opus-5-5', messages: [{ role: 'user', content: 'hi' }] } });
      assert.equal(r.status, 200, r.text);
      assert.equal(JSON.parse(r.text).input_tokens, 7);
      const rec = await one(ctx, 'anthropic');
      assert.equal(rec.url, '/v1/messages/count_tokens');
      assert.equal(rec.headers.authorization, `Bearer ${OAUTH}`);
      assert.equal(rec.headers['x-api-key'], undefined);
    } },
  { id: 'count-tokens-local-url', tags: ['parity'], script: { 'claude-local': { status: 200, json: { input_tokens: 11 } } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages/count_tokens', { headers: { authorization: `Bearer ${OAUTH}` }, body: { model: LOCAL, messages: [{ role: 'user', content: 'hi' }] } });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, 'claude-local');
      assert.equal(rec.url, '/v1/messages/count_tokens', 'baseurl already ends in /v1: no second /v1');
      assert.equal(rec.headers.authorization, 'Bearer local-dummy-key-not-a-secret');
      noCallerCreds(rec, OAUTH);
    } },
  { id: 'count-tokens-error-verbatim', tags: ['parity'], script: { anthropic: { status: 404, headers: { 'content-type': 'text/plain' }, body: 'no count for you' } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages/count_tokens', { body: { model: 'claude-opus-5-5', messages: [{ role: 'user', content: 'hi' }] } });
      assert.equal(r.status, 404);
      assert.equal(r.text, 'no count for you');
    } },
  { id: 'count-tokens-unknown-model-404', tags: ['parity'], script: {},
    async run(ctx) {
      const r = await ctx.req('/v1/messages/count_tokens', { body: { model: 'nope/never-a-model', messages: [{ role: 'user', content: 'hi' }] } });
      assert.equal(r.status, 404, r.text);
      await none(ctx);
    } },

  // ---- 0013 systemone ---------------------------------------------------------------------
  { id: 'systemone-allowlist-hit', tags: ['parity'], script: { 'openrouter-decide': { status: 200, json: { decision: 'continue' } } },
    async run(ctx) {
      const body = '{"model":"openrouter-decide/typesafe/jev-1.13","state":"s","questions":{"q":1}}';
      const r = await ctx.req('/v1/systemone', { headers: { authorization: `Bearer ${OAUTH}`, 'x-api-key': 'caller-x', 'content-type': 'application/json' }, body });
      assert.equal(r.status, 200, r.text);
      assert.equal(r.text, '{"decision":"continue"}');
      const rec = await one(ctx, 'openrouter-decide');
      assert.equal(rec.url, '/api/v1/systemone');
      assert.deepEqual(JSON.parse(rec.body), { model: 'typesafe/jev-1.13', state: 's', questions: { q: 1 } });
      assert.equal(rec.headers.authorization, 'Bearer test-or-key');
      assert.equal(rec.headers['x-api-key'], undefined);
      noCallerCreds(rec, OAUTH);
    } },
  ...[['unknown-provider', 'nope/typesafe/jev-1.13'], ['non-systemone-provider', 'anthropic/claude-opus-5-5'], ['unlisted-id', 'openrouter-decide/typesafe/other'], ['bare-id', 'typesafe/jev-1.13']].map(([n, model]) => ({
    id: `systemone-miss-${n}`, tags: ['parity'], script: { 'openrouter-decide': { status: 200, json: {} }, anthropic: OK('x') },
    async run(ctx) {
      const r = await ctx.req('/v1/systemone', { body: { model, state: 's', questions: {} } });
      assert.equal(r.status, 404, r.text);
      assert.match(r.text, /model_not_found/);
      await none(ctx);
    } })),
  { id: 'systemone-no-credential-503', tags: ['parity'], script: { 'rrdiff-decide-nocred': { status: 200, json: {} } },
    async run(ctx) {
      const r = await ctx.req('/v1/systemone', { body: { model: 'rrdiff-decide-nocred/rrdiff/decide-1', state: 's', questions: {} } });
      assert.equal(r.status, 503, r.text);
      assert.match(r.text, /provider_not_configured/);
      await none(ctx);
    } },
  { id: 'systemone-error-verbatim', tags: ['parity'], script: { 'openrouter-decide': { status: 502, headers: { 'content-type': 'text/plain' }, body: 'decide upstream broke' } },
    async run(ctx) {
      const r = await ctx.req('/v1/systemone', { body: { model: 'openrouter-decide/typesafe/jev-1.13', state: 's', questions: {} } });
      assert.equal(r.status, 502);
      assert.equal(r.text, 'decide upstream broke');
    } },
  { id: 'systemone-not-on-messages', tags: ['parity'], script: { 'openrouter-decide': { status: 200, json: {} } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('openrouter-decide/typesafe/jev-1.13') });
      assert.ok(r.status >= 400, `status ${r.status}`);
      await none(ctx);
    } },

  // ---- /v1/models ---------------------------------------------------------------------------
  { id: 'models-list', tags: ['parity'], script: {},
    async run(ctx) {
      const r = await ctx.req('/v1/models', { method: 'GET' });
      assert.equal(r.status, 200, r.text);
      const j = JSON.parse(r.text);
      const ids = (j.data || []).map((m) => m.id);
      for (const id of ['claude-local/qwen38-27b-iq3s', 'anthropic/claude-opus-5-5', 'codex-a/gpt-6-luna', 'or-anthropic/qwen/qwen3.8-flash']) {
        assert.ok(ids.includes(id), `/v1/models lacks ${id}; has ${ids.slice(0, 8).join(', ')}...`);
      }
      await none(ctx);
    } },

  // ---- Decision 2 protocol-mismatch -------------------------------------------------------------
  { id: 'mismatch-openrouter-on-messages-refused', tags: ['changed'], script: { openrouter: { status: 200, json: {} } },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG('openrouter/qwen/qwen3.8-flash') });
      assert.ok(r.status >= 400 && r.status < 500, `expected 4xx, got ${r.status}`);
      await none(ctx);
    } },
  { id: 'openai-ingress-local-chat-completions', tags: ['parity'],
    script: { 'claude-local': { status: 200, json: { id: 'c1', object: 'chat.completion', model: 'qwen38-27b-iq3s', choices: [{ index: 0, message: { role: 'assistant', content: 'pong' }, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 } } } },
    async run(ctx) {
      const r = await ctx.req('/v1/chat/completions', { body: { model: LOCAL, max_tokens: 8, messages: [{ role: 'user', content: 'ping' }] } });
      assert.equal(r.status, 200, r.text);
      assert.equal(JSON.parse(r.text).choices[0].message.content, 'pong');
      const rec = await one(ctx, 'claude-local');
      assert.equal(rec.url, '/v1/chat/completions');
      assert.equal(JSON.parse(rec.body).model, 'qwen38-27b-iq3s');
    } },

  // ---- Decision 3 local leg (changed: green after WP-3) ------------------------------------------
  { id: 'local-native-messages-path', tags: ['changed'], script: { 'claude-local': OK('qwen38-27b-iq3s') },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG(LOCAL) });
      assert.equal(r.status, 200, r.text);
      const rec = await one(ctx, 'claude-local');
      assert.equal(rec.url, '/v1/messages');
      assert.equal(JSON.parse(rec.body).model, 'qwen38-27b-iq3s');
    } },
  { id: 'local-trailing-system-merged', tags: ['changed'], script: { 'claude-local': OK('qwen38-27b-iq3s') },
    async run(ctx) {
      await ctx.req('/v1/messages', { body: { model: LOCAL, max_tokens: 16, system: 'top', messages: [{ role: 'user', content: 'question' }, { role: 'system', content: 'late reminder' }] } });
      const rec = await one(ctx, 'claude-local');
      const b = JSON.parse(rec.body);
      assert.ok(b.messages.every((m) => m.role !== 'system'), 'a system message survived inside messages');
      assert.equal(b.messages.length, 1);
      const text = JSON.stringify(b.messages[0].content);
      assert.ok(text.includes('question') && text.includes('late reminder') && text.indexOf('question') < text.indexOf('late reminder'));
    } },
  ...[['low', { output_config: { effort: 'low' } }, { reasoning_effort: 'low' }], ['high-maps-medium', { output_config: { effort: 'high' } }, { reasoning_effort: 'medium' }], ['max', { output_config: { effort: 'max' } }, { reasoning_effort: 'xhigh' }], ['disabled', { thinking: { type: 'disabled' } }, { enable_thinking: false }], ['budget-1024-low', { thinking: { type: 'enabled', budget_tokens: 1024 } }, { reasoning_effort: 'low' }]].map(([n, extra, kw]) => ({
    id: `local-effort-${n}`, tags: ['changed'], script: { 'claude-local': OK('qwen38-27b-iq3s') },
    async run(ctx) {
      await ctx.req('/v1/messages', { body: MSG(LOCAL, extra) });
      const b = JSON.parse((await one(ctx, 'claude-local')).body);
      assert.deepEqual(b.chat_template_kwargs, kw);
    } })),
  { id: 'local-effort-body-kwargs-win', tags: ['changed'], script: { 'claude-local': OK('qwen38-27b-iq3s') },
    async run(ctx) {
      await ctx.req('/v1/messages', { body: MSG(LOCAL, { output_config: { effort: 'low' }, chat_template_kwargs: { reasoning_effort: 'medium' } }) });
      assert.deepEqual(JSON.parse((await one(ctx, 'claude-local')).body).chat_template_kwargs, { reasoning_effort: 'medium' });
    } },
  ...[['clamp-down', 64000, 32000], ['under-cap-untouched', 1000, 1000], ['absent-gets-cap', undefined, 32000]].map(([n, given, want]) => ({
    id: `local-max-tokens-${n}`, tags: ['changed'], script: { 'claude-local': OK('qwen38-27b-iq3s') },
    async run(ctx) {
      const body = MSG(LOCAL, { max_tokens: given });
      if (given === undefined) delete body.max_tokens;
      await ctx.req('/v1/messages', { body });
      assert.equal(JSON.parse((await one(ctx, 'claude-local')).body).max_tokens, want);
    } })),
  { id: 'local-keepalive-ping-after-15s', tags: ['changed'], script: { 'claude-local': SSE(5, 17000) },
    async run(ctx) {
      const r = await ctx.req('/v1/messages', { body: MSG(LOCAL, { stream: true }) });
      assert.equal(r.status, 200, r.text.slice(0, 200));
      const extra = r.text.length - SSE_TEXT.length;
      assert.ok(extra > 0 && /event: ping\n/.test(r.text.replace(SSE_TEXT.slice(0, 37 * 5), '')), 'no keepalive ping in the 17 s gap');
    } },

  // ---- request body size (Node bodyLimitBytes default 50 MiB) ---------------------------------------
  { id: 'body-20mib-accepted', tags: ['parity'], script: { anthropic: OK('claude-opus-5-5') },
    async run(ctx) {
      const big = 'x'.repeat(20 * 1024 * 1024);
      const r = await ctx.req('/v1/messages', { body: MSG('claude-opus-5-5', { messages: [{ role: 'user', content: big }] }) });
      assert.equal(r.status, 200, r.text.slice(0, 200));
      const rec = await one(ctx, 'anthropic');
      assert.ok(rec.bodyLength > 20 * 1024 * 1024);
    } },
];
