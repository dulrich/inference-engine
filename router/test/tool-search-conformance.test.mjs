// tool-search-conformance.test.mjs — deferred-tool (ENABLE_TOOL_SEARCH) conformance through the
// resident gateway, locked from the WP-5 wire spike
// (docs/tool-calling-gateway/tool-search-conformance-spike.md).
//
// This is an explicitly BLACK-BOX test. The gateway's source lives only inside the Docker build
// checkout, so nothing here imports it: every assertion is made against a running gateway over
// HTTP. When that gateway is unreachable the tests FAIL — they never skip. A conformance gate
// that quietly disappears when its target is down is not a gate.
//
// Bodies. The spike's captured request-1 bodies are replayed when a capture directory is
// present ($TOOL_SEARCH_CAPTURE_DIR, default /tmp/wp5-capture); that is the only way to carry
// the spike's absolute token thresholds, which were measured against this fleet's real system
// prompt and its real always-on tool descriptions. Without captures the test builds bodies of
// the documented shape in-test (12 tools: a fully-schema'd ToolSearch, exactly one
// `defer_loading: true` DeferredToolPlaceholder, the always-on tools; the deferred names
// advertised in an interleaved role:"system" message) and asserts the same invariants
// relatively — a synthetic catalogue is necessarily smaller than the fleet's, so only the
// direction is meaningful. Which mode ran is printed, never inferred.
//
// Token metric. Exactly the spike's: POST /v1/messages/count_tokens with the cell's own
// `system` + `tools` and a one-word user message, on that cell's own leg. Cross-leg comparison
// is invalid (different catalogues, different tokenizers) and is never attempted.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE
// (default <code-root>/fleet-tools/.env) and is never printed. The hosted
// passthrough leg additionally forwards the caller's own bearer, so its cells need
// $ROUTER_HOSTED_BEARER; without it they fail loudly with instructions rather than skip.
//
// Env: ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY, ROUTER_HOSTED_BEARER,
//      TOOL_SEARCH_CAPTURE_DIR, TOOL_SEARCH_METERED.

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

const BASE_URL = (process.env.ROUTER_BASE_URL || "http://127.0.0.1:4813").replace(/\/$/, "");
const ENV_FILE = process.env.ROUTER_ENV_FILE || "<code-root>/fleet-tools/.env";
const CAPTURE_DIR = process.env.TOOL_SEARCH_CAPTURE_DIR || "/tmp/wp5-capture";
const METERED = process.env.TOOL_SEARCH_METERED === "1";
// The engine's anthropic-type legs want *some* client bearer to build passthrough headers; the
// provider's own apiKeyEnv replaces it before the upstream (smoke-router.sh checks 7 and 8).
const DUMMY_BEARER = "tool-search-conformance-dummy-client-bearer";

const ADVERT_PHRASE = "deferred tools are now available via ToolSearch";
const PROBE_TOOL = "NotebookEdit";

// ───────────────────────────────────────────────────────────────────────────── key + transport

function gatewayKey() {
  if (process.env.GATEWAY_KEY) return process.env.GATEWAY_KEY;
  let text = "";
  try {
    text = fs.readFileSync(ENV_FILE, "utf8");
  } catch (err) {
    assert.fail(`no gateway key: $GATEWAY_KEY is unset and ${ENV_FILE} is unreadable (${err.code}). `
      + "Set GATEWAY_KEY or point ROUTER_ENV_FILE at the env file holding INFERENCE_ROUTER_GATEWAY_KEY.");
  }
  const hit = /^INFERENCE_ROUTER_GATEWAY_KEY=(.*)$/m.exec(text);
  if (!hit || !hit[1].trim()) {
    assert.fail(`${ENV_FILE} carries no INFERENCE_ROUTER_GATEWAY_KEY; set $GATEWAY_KEY instead.`);
  }
  return hit[1].trim();          // never logged, never asserted on
}

const KEY = gatewayKey();

async function call(pathname, body, { bearer = DUMMY_BEARER, timeoutMs = 60000 } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${BASE_URL}${pathname}`, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        "content-type": "application/json",
        "x-gateway-key": KEY,
        "anthropic-version": "2023-06-01",
        ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ac.signal,
    });
  } catch (err) {
    assert.fail(`gateway at ${BASE_URL} is unreachable (${pathname}): ${err.name}: ${err.message}. `
      + "This test is a black-box gate against the resident router and fails rather than skips when it is down. "
      + "Start it, or point ROUTER_BASE_URL elsewhere.");
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, text, json, hint: upstreamHint(text) };
}

// A leg whose upstream is simply absent (the llama container stopped, the sidecar unit down)
// returns a generic 502; say which upstream, so the red is diagnosable at a glance.
function upstreamHint(text) {
  if (!/upstream_connect|All target providers failed/.test(text)) return "";
  const leg = /"target_provider_names":\["([^"]+)"/.exec(text)?.[1] ?? "unknown";
  return ` — the "${leg}" upstream is not reachable from the gateway, so this leg cannot be exercised right now `
    + "(for claude-local/* that is the inference-engine-llama container; for codex-* the codex-sidecar unit). "
    + "This is an environment red, not a conformance red.";
}

let modelIdsCache = null;
async function modelIds() {
  if (modelIdsCache) return modelIdsCache;
  const r = await call("/v1/models");
  assert.equal(r.status, 200, `GET /v1/models returned ${r.status}: ${r.text.slice(0, 400)}`);
  const ids = (r.json?.data ?? []).map(d => d?.id).filter(Boolean);
  assert.ok(ids.length > 0, `GET /v1/models listed no ids: ${r.text.slice(0, 400)}`);
  modelIdsCache = ids;
  return ids;
}

// The user may re-jig the local model list at any time, so nothing here pins a claude-local id:
// the preferred id is used when the gateway reports it, otherwise the first id on that leg.
function resolveModel(ids, { preferred, prefix, listedAs = preferred }) {
  if (ids.includes(listedAs)) return { id: preferred, exact: true };
  const alt = ids.find(id => id.startsWith(prefix));
  assert.ok(alt, `GET /v1/models lists no ${prefix}* id, so this leg cannot be exercised. Got: ${ids.join(", ")}`);
  return { id: alt, exact: false };
}

// ─────────────────────────────────────────────────────────────────────────────── request bodies

const TOOL_SEARCH_TOOL = {
  name: "ToolSearch",
  description: "Fetches full schema definitions for deferred tools so they can be called.",
  input_schema: {
    type: "object",
    properties: { query: { type: "string" }, max_results: { type: "number" } },
    required: ["query"],
  },
};
const PLACEHOLDER_TOOL = {
  name: "DeferredToolPlaceholder",
  description: "Reserved placeholder that keeps deferred tool loading active; never call this tool.",
  input_schema: { type: "object", properties: {} },
  defer_loading: true,
};
const ALWAYS_ON = ["Agent", "Bash", "Edit", "ListAgents", "Read", "ReportFindings", "ScheduleWakeup", "Skill", "Workflow", "Write"]
  .map(name => ({
    name,
    description: `The ${name} tool, always loaded in this profile.`,
    input_schema: { type: "object", properties: { input: { type: "string" } } },
  }));
const DEFERRED_NAMES = ["CronCreate", "CronDelete", "CronList", "DesignSync", "EnterWorktree",
  "ExitWorktree", PROBE_TOOL, "SendMessage", "TaskOutput", "TaskStop"];

const ADVERT = `The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded — `
  + `calling them directly will fail with InputValidationError. Use ToolSearch with query `
  + `"select:<name>[,<name>...]" to load tool schemas before calling them:\n${DEFERRED_NAMES.join(" / ")}`;

const SYNTH_SYSTEM = [{ type: "text", text: "You are Claude Code, Anthropic's official CLI for Claude." }];

// ENABLE_TOOL_SEARCH=true: 12 tools, the deferred ones absent from `tools` and named in prose.
const synthOn = () => ({
  system: SYNTH_SYSTEM,
  tools: [TOOL_SEARCH_TOOL, PLACEHOLDER_TOOL, ...ALWAYS_ON],
  messages: [
    { role: "user", content: `What parameters does ${PROBE_TOOL} take?` },
    { role: "system", content: [{ type: "text", text: ADVERT }] },
  ],
});

// ENABLE_TOOL_SEARCH=false: the catalogue is eagerly materialised — no ToolSearch, no
// defer_loading, every deferred tool carrying its full schema.
const synthOff = () => ({
  system: SYNTH_SYSTEM,
  tools: [
    ...ALWAYS_ON,
    ...DEFERRED_NAMES.map(name => ({
      name,
      description: `The ${name} tool, eagerly loaded because tool search is disabled. `.repeat(4),
      input_schema: {
        type: "object",
        properties: { path: { type: "string" }, content: { type: "string" }, mode: { type: "string" } },
        required: ["path"],
      },
    })),
  ],
  messages: [{ role: "user", content: `What parameters does ${PROBE_TOOL} take?` }],
});

function capturedBody(cell) {
  const file = path.join(CAPTURE_DIR, cell, "001.req.json");
  try {
    const body = JSON.parse(fs.readFileSync(file, "utf8"))?.body;
    return body && Array.isArray(body.tools) ? body : null;
  } catch {
    return null;
  }
}

// The captured id is whatever was resident on spike day; the leg is what matters, so the model
// is always rewritten to the id this gateway reports now.
const countBody = (model, src, { withTools = true } = {}) => ({
  model,
  system: src.system,
  messages: [{ role: "user", content: "hi" }],
  ...(withTools ? { tools: src.tools } : {}),
});

// ──────────────────────────────────────────────────────────────────────────────────── the legs

const LEGS = [
  {
    cell: "passthrough",
    label: "Claude passthrough",
    hosted: true,
    pick: ids => resolveModel(ids, { preferred: "claude-haiku-4-5", listedAs: "anthropic/claude-haiku-4-5", prefix: "anthropic/" }),
    off: 29159, on: 17745,
  },
  {
    cell: "local",
    label: "local OpenAI-conversion",
    pick: ids => resolveModel(ids, { preferred: "claude-local/qwen38-27b-iq3s", prefix: "claude-local/" }),
    off: 15394, on: 8238,
  },
  {
    cell: "codex",
    label: "codex sidecar",
    pick: ids => resolveModel(ids, { preferred: "codex-b/gpt-5.6-luna", prefix: "codex-b/" }),
    off: 14549, on: 7875,
  },
];

function hostedBearer() {
  const b = process.env.ROUTER_HOSTED_BEARER;
  assert.ok(b && b.trim(), "the hosted passthrough leg forwards the caller's own OAuth bearer, and the gateway key alone "
    + "gets \"ANTHROPIC_API_KEY is missing.\" (measured, WP-5). Set $ROUTER_HOSTED_BEARER to a bearer from an "
    + "authenticated session before running the passthrough cells. This cell fails rather than skips on purpose: "
    + "a silently-skipped conformance cell is the failure mode this file exists to prevent.");
  return b.trim();
}

async function countTokens(leg, model, src, opts = {}) {
  const bearer = leg.hosted ? hostedBearer() : DUMMY_BEARER;
  const r = await call("/v1/messages/count_tokens", countBody(model, src, opts), { bearer });
  assert.equal(r.status, 200, `count_tokens on the ${leg.label} leg returned ${r.status}: ${r.text.slice(0, 400)}${r.hint}`);
  const n = r.json?.input_tokens;
  assert.equal(typeof n, "number", `count_tokens on the ${leg.label} leg returned no integer input_tokens: ${r.text.slice(0, 400)}`);
  return n;
}

const deferredEntries = tools => tools.filter(t => t && t.defer_loading === true);
const byName = (tools, name) => tools.find(t => t?.name === name);

for (const leg of LEGS) {
  test(`${leg.label}: request 1 carries the deferred shape, and the eager control does not`, async t => {
    const onSrc = capturedBody(`${leg.cell}-on`) ?? synthOn();
    const offSrc = capturedBody(`${leg.cell}-off`) ?? synthOff();
    t.diagnostic(`${leg.cell}: on=${capturedBody(`${leg.cell}-on`) ? "replayed capture" : "synthetic"}, `
      + `off=${capturedBody(`${leg.cell}-off`) ? "replayed capture" : "synthetic"} (capture dir ${CAPTURE_DIR})`);

    // Invariant 1 — exactly one defer_loading marker, a usable ToolSearch, the probe tool absent.
    const deferred = deferredEntries(onSrc.tools);
    assert.equal(deferred.length, 1, `request 1 must carry exactly one defer_loading entry, got ${deferred.length}: `
      + `${deferred.map(d => d.name).join(", ")}`);
    assert.equal(deferred[0].name, "DeferredToolPlaceholder");
    const search = byName(onSrc.tools, "ToolSearch");
    assert.ok(search, "request 1 must carry a ToolSearch tool");
    assert.ok(search.input_schema && Object.keys(search.input_schema).length > 0, "ToolSearch must carry a non-empty input_schema");
    assert.equal(byName(onSrc.tools, PROBE_TOOL), undefined, `${PROBE_TOOL} must be absent from request 1's tools`);

    // The deferred names reach the model only in prose, never in `tools`.
    const advertised = JSON.stringify(onSrc.messages ?? []) + JSON.stringify(onSrc.system ?? "");
    assert.ok(advertised.includes(ADVERT_PHRASE), "the deferred-tool name list must be advertised in the request");
    assert.ok(!JSON.stringify(onSrc.tools).includes(ADVERT_PHRASE), "the advertisement belongs in prose, not in `tools`");

    // Negative control — the same assertions must fail on an eager profile.
    assert.equal(deferredEntries(offSrc.tools).length, 0, "the eager control must carry no defer_loading entry");
    assert.equal(byName(offSrc.tools, "ToolSearch"), undefined, "the eager control must carry no ToolSearch tool");
    assert.ok(offSrc.tools.length > onSrc.tools.length, "the eager control must materialise a larger catalogue");
  });

  test(`${leg.label}: the deferred catalogue is not eagerly expanded (within-leg token gate)`, async t => {
    const ids = await modelIds();
    const { id: model, exact } = leg.pick(ids);
    const onCap = capturedBody(`${leg.cell}-on`);
    const offCap = capturedBody(`${leg.cell}-off`);
    const replayed = Boolean(onCap && offCap);
    const onSrc = onCap ?? synthOn();
    const offSrc = offCap ?? synthOff();

    const onTokens = await countTokens(leg, model, onSrc);
    const offTokens = await countTokens(leg, model, offSrc);
    const onToolsOnly = onTokens - await countTokens(leg, model, onSrc, { withTools: false });
    const offToolsOnly = offTokens - await countTokens(leg, model, offSrc, { withTools: false });
    t.diagnostic(`${leg.cell} (${model}): system+tools on=${onTokens} off=${offTokens} | `
      + `tools only on=${onToolsOnly} off=${offToolsOnly} | ratio ${(offToolsOnly / onToolsOnly).toFixed(2)}x | `
      + `${replayed ? "replayed" : "synthetic"} bodies`);

    // Direction 1 — the deferred profile stays at or under the WP-5 on-threshold. Replaying the
    // spike's own captured body reproduces its number exactly, so the bound is `<=` plus 2% of
    // drift headroom; anything above that is either eager expansion or a grown profile.
    const ceiling = Math.ceil(leg.on * 1.02);
    assert.ok(onTokens <= ceiling, `${leg.label}: the deferred profile cost ${onTokens} system+tools tokens, above the `
      + `WP-5 measured ${leg.on} (+2% = ${ceiling}). Either the catalogue is being eagerly expanded again, or the profile `
      + "grew and the threshold needs re-measuring — the spike record is the only place a new number may come from.");

    // Direction 2 — the gate must be able to fail: the eager control has to exceed that same
    // threshold. Absolute thresholds are only meaningful on replayed captures against the id the
    // spike measured; otherwise the within-leg ordering is what is asserted.
    if (replayed && exact) {
      assert.ok(offTokens > leg.on, `${leg.label}: the eager control cost only ${offTokens} system+tools tokens, which does `
        + `not exceed the WP-5 on-threshold ${leg.on} — the negative control is no longer a control.`);
      assert.ok(offTokens <= leg.off * 1.05, `${leg.label}: the eager control cost ${offTokens} tokens, more than 5% above the `
        + `WP-5 measured ${leg.off}; the fixture profile has drifted and the thresholds need re-measuring.`);
    } else {
      t.diagnostic(`${leg.cell}: absolute thresholds not asserted (replayed=${replayed}, exact model=${exact}); `
        + "within-leg ordering only. Point TOOL_SEARCH_CAPTURE_DIR at a WP-5-shaped capture to restore them.");
      assert.ok(offTokens > onTokens, `${leg.label}: the eager control (${offTokens}) did not cost more than the deferred `
        + `profile (${onTokens}) — the deferred path is saving nothing.`);
    }
    assert.ok(offToolsOnly > onToolsOnly, `${leg.label}: the eager tool block (${offToolsOnly}) did not exceed the deferred `
      + `one (${onToolsOnly}); deferral is not reducing the tool block at all.`);
  });
}

// ───────────────────────────────────────────────────────── relay of the discovery round-trip

// Layer 1 asserts that the wire shapes of the discovery round-trip survive the leg: a
// tool_reference tool_result and a grown catalogue are accepted and answered normally, with no
// 4xx, no "tool not found" and no malformed continuation. Whether a given model *chooses* to
// call ToolSearch is a behavioural property and belongs to router/smoke-tool-search.sh, which
// drives a real `claude -p` turn.
function roundTripBody(model) {
  const src = synthOn();
  return {
    model,
    max_tokens: 64,
    stream: false,
    system: src.system,
    metadata: { user_id: JSON.stringify({ session_id: "tool-search-conformance-layer1" }) },
    tools: [...src.tools, {
      name: PROBE_TOOL,
      description: "Replace a cell in a Jupyter notebook.",
      input_schema: {
        type: "object",
        properties: { notebook_path: { type: "string" }, new_source: { type: "string" } },
        required: ["notebook_path", "new_source"],
      },
      defer_loading: true,            // retained after discovery, measured on both clean legs
    }],
    messages: [
      { role: "user", content: `What parameters does ${PROBE_TOOL} take?` },
      { role: "system", content: [{ type: "text", text: ADVERT }] },
      { role: "assistant", content: [{ type: "tool_use", id: "toolu_layer1_probe", name: "ToolSearch", input: { query: `select:${PROBE_TOOL}`, max_results: 1 } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "toolu_layer1_probe", content: [{ type: "tool_reference", tool_name: PROBE_TOOL }] }] },
    ],
  };
}

test("local OpenAI-conversion: a tool_reference result and the grown catalogue are relayed, not rejected", async t => {
  const ids = await modelIds();
  const { id: model } = resolveModel(ids, { preferred: "claude-local/qwen38-27b-iq3s", prefix: "claude-local/" });
  t.diagnostic(`local relay probe on ${model}`);
  const r = await call("/v1/messages", roundTripBody(model), { timeoutMs: 180000 });
  assert.equal(r.status, 200, `the local leg rejected the discovery round-trip with ${r.status}: ${r.text.slice(0, 600)}${r.hint}`);
  assert.equal(r.json?.type, "message", `expected a Messages response, got: ${r.text.slice(0, 400)}`);
  assert.ok(Array.isArray(r.json.content), "the response must carry a content array");
  assert.ok(["end_turn", "tool_use", "max_tokens", "stop_sequence"].includes(r.json.stop_reason),
    `malformed continuation: stop_reason=${String(r.json.stop_reason)}`);
  const text = JSON.stringify(r.json.content);
  assert.ok(!/tool[ _]not[ _]found|unknown tool|InputValidationError/i.test(text),
    `the leg reported the deferred tool as unavailable after discovery: ${text.slice(0, 400)}`);
});

test("codex sidecar: a tool_reference result and the grown catalogue are relayed, not rejected", async t => {
  if (!METERED) {
    t.diagnostic("TOOL_SEARCH_METERED=1 not set: SKIPPING the metered codex turn (one Codex-subscription turn). "
      + "It would POST /v1/messages on codex-b/gpt-5.6-luna with a tool_reference tool_result and a grown catalogue.");
    t.skip("metered: set TOOL_SEARCH_METERED=1 to run one Codex-subscription turn");
    return;
  }
  const ids = await modelIds();
  const { id: model } = resolveModel(ids, { preferred: "codex-b/gpt-5.6-luna", prefix: "codex-b/" });
  t.diagnostic(`codex relay probe on ${model} (metered)`);
  const r = await call("/v1/messages", roundTripBody(model), { timeoutMs: 300000 });
  assert.equal(r.status, 200, `the codex leg rejected the discovery round-trip with ${r.status}: ${r.text.slice(0, 600)}${r.hint}`);
  assert.equal(r.json?.type, "message", `expected a Messages response, got: ${r.text.slice(0, 400)}`);
  assert.ok(Array.isArray(r.json.content), "the response must carry a content array");
  const text = JSON.stringify(r.json.content);
  assert.ok(!/tool[ _]not[ _]found|unknown tool|InputValidationError/i.test(text),
    `the codex leg reported the deferred tool as unavailable after discovery: ${text.slice(0, 400)}`);
});
