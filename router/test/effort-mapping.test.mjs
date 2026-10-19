// effort-mapping.test.mjs — black-box, render-level gate for patch 0007 (+ 0009): the client's
// reasoning intent (Anthropic `output_config.effort` / `thinking`) reaching the local leg's
// template as `chat_template_kwargs` (router/AGENTS.md, "The patches", 0007 and 0009;
// plans/router-gate-consolidation.md WP-1 rewrote it from a live generation ladder).
//
// BLACK BOX: the gateway's source lives only inside the Docker build checkout, so nothing here
// imports it — every assertion is made over HTTP against a running gateway and the llama-server
// upstream it fronts. When either is unreachable the tests FAIL; they never skip. A gate that
// quietly disappears when its target is down is not a gate.
//
// The oracle. Every probe is ONE pinned short body (`system:"Be brief."`, user `"Say hi."`,
// `max_tokens:1`), so nothing generates beyond one token and the whole file runs in seconds.
// The Qwen3.8 chat template renders a different prompt per `chat_template_kwargs`, so the
// PROMPT TOKEN COUNT is a fingerprint of which kwargs reached the template. For each row the
// router's occupancy (`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`,
// absent columns = 0 — the usage-projection contract in
// docs/tool-calling-gateway/codex-sidecar.md "Usage reconciliation, restated") must EQUAL the
// upstream-direct `prompt_tokens` for the kwargs that row is expected to deliver. Both legs are
// measured in the same run; no count is hardcoded.
//
// Coverage (the six-row `effortKwargs` map of router/gateway.config.json plus the no-op):
//   - low, medium, high (→ medium since 0009), xhigh, max (→ xhigh), thinking disabled
//     (→ enable_thinking:false): router occupancy == upstream prompt_tokens for the expected kwargs.
//   - an unknown effort value: the router forwards nothing (no-op invariant), so occupancy ==
//     upstream with NO kwargs — and the upstream-direct negative control proves forwarding it
//     would have mattered: llama 500s with `Jinja Exception: Unexpected reasoning effort`.
//   - upstream low / medium / absent are pairwise distinct, so the oracle can tell those rows apart.
//   - the contract table below equals the config's effortKwargs row (config drift is a red).
// Limit: xhigh (and max → xhigh) is the template DEFAULT, so it renders identically to absent
// kwargs — this oracle cannot tell a delivered xhigh from a dropped one. Forwarding identity for
// those two rows is owned by the build-stage `effort-kwargs.test.ts` vitest cases (audit M2).
// Whether the model actually FOLLOWS the ladder (medium thinks longer than low) is not gated: the
// live generation gate that tried flaked on identical code and was retired (router/AGENTS.md 0007).
//
// Oracle selection (explicit, never sniffed): ROUTER_IMPL=node|rust, default rust since the
// WP-6 cutover (plans/router-rust-gateway.md, audit E1); node is for the node-final rollback image.
//   node — the upstream-direct oracle is llama's OpenAI `/v1/chat/completions` `prompt_tokens`,
//          because the Node gateway converts Anthropic → chat-completions for the local leg.
//   rust — the upstream-direct oracle is llama's NATIVE `/v1/messages` at max_tokens:1 with the
//          expected `chat_template_kwargs` in the Anthropic body, occupancy
//          `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` — the same path
//          the Rust gateway's local leg uses (Decision 3). Exact equality either way, no tolerance.
// The bogus-effort Jinja control stays on chat-completions for both.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE (default
// <code-root>/fleet-tools/.env) and is never printed. The upstream needs no key.
//
// Env: ROUTER_IMPL (node|rust, default rust), ROUTER_BASE_URL (default http://127.0.0.1:4813), ROUTER_ENV_FILE, GATEWAY_KEY,
//      LLAMA_UPSTREAM_URL (default http://127.0.0.1:4811 — the host port of the llama container),
//      EFFORT_UPSTREAM_MODEL (default: the single model keyed in claude-local's effortKwargs).

import { routerEnvFile } from "../fleet-env.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const BASE_URL = (process.env.ROUTER_BASE_URL || "http://127.0.0.1:4813").replace(/\/$/, "");
const ROUTER_IMPL = process.env.ROUTER_IMPL || "rust";
if (!["node", "rust"].includes(ROUTER_IMPL)) throw new Error(`ROUTER_IMPL must be node or rust, got ${ROUTER_IMPL}`);
const UPSTREAM_URL = (process.env.LLAMA_UPSTREAM_URL || "http://127.0.0.1:4811").replace(/\/$/, "");
// ROUTER_ENV_FILE, else `fleet-env path router` under the fleet-env cutover, else the legacy file.
const ENV_FILE = routerEnvFile();
const CONFIG_FILE = new URL("../gateway.config.json", import.meta.url);
// The engine's anthropic-type legs want *some* client bearer to build passthrough headers; the
// provider's own apikey replaces it before the upstream (smoke-router.sh checks 7 and 8).
const DUMMY_BEARER = "effort-mapping-dummy-client-bearer";
const REQUEST_TIMEOUT_MS = 120000;

const SYSTEM = "Be brief.";
const USER = "Say hi.";

// The contract: what each client intent must deliver to the template.
const EXPECTED = {
  low: { reasoning_effort: "low" },
  medium: { reasoning_effort: "medium" },
  high: { reasoning_effort: "medium" },
  xhigh: { reasoning_effort: "xhigh" },
  max: { reasoning_effort: "xhigh" },
  disabled: { enable_thinking: false },
};
const BOGUS = "bogus-effort-level";

const CONFIG = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
const LOCAL = CONFIG.providers.find((p) => p.name === "claude-local");
const EFFORT_MODELS = Object.keys(LOCAL?.effortKwargs ?? {});
const UPSTREAM_MODEL = process.env.EFFORT_UPSTREAM_MODEL || EFFORT_MODELS[0];
const ROUTER_MODEL = `claude-local/${UPSTREAM_MODEL}`;

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
  return hit[1].trim();              // never logged, never asserted on
}

const KEY = gatewayKey();

async function post(url, headers, body, what) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(url, { method: "POST", headers, body: JSON.stringify(body), signal: ac.signal });
  } catch (err) {
    assert.fail(`${what} at ${url} is unreachable: ${err.name}: ${err.message}. `
      + "This test is a black-box gate and fails rather than skips when its target is down. "
      + "Start it, or point ROUTER_BASE_URL / LLAMA_UPSTREAM_URL elsewhere.");
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, text, json };
}

// A leg whose upstream is simply absent (the llama container stopped) returns a generic 502;
// say which upstream, so the red is diagnosable at a glance.
function upstreamHint(text) {
  if (!/upstream_connect|All target providers failed/.test(text)) return "";
  const leg = /"target_provider_names":\["([^"]+)"/.exec(text)?.[1] ?? "unknown";
  return ` — the "${leg}" upstream is not reachable from the gateway (for claude-local/* that is the `
    + "inference-engine-llama container), so this leg cannot be exercised right now. "
    + "This is an environment red, not a mapping red.";
}

function upstreamCall(kwargs) {
  return post(`${UPSTREAM_URL}/v1/chat/completions`, { "content-type": "application/json" }, {
    model: UPSTREAM_MODEL,
    max_tokens: 1,
    temperature: 0,
    messages: [{ role: "system", content: SYSTEM }, { role: "user", content: USER }],
    ...(kwargs ? { chat_template_kwargs: kwargs } : {}),
  }, "llama upstream");
}

function upstreamNativeCall(kwargs) {
  return post(`${UPSTREAM_URL}/v1/messages`, { "content-type": "application/json", "anthropic-version": "2023-06-01" }, {
    model: UPSTREAM_MODEL,
    max_tokens: 1,
    temperature: 0,
    system: SYSTEM,
    messages: [{ role: "user", content: USER }],
    ...(kwargs ? { chat_template_kwargs: kwargs } : {}),
  }, "llama upstream (native /v1/messages)");
}

const occupancy = (u) => u.input_tokens + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);

// The upstream-direct oracle for ROUTER_IMPL (header "Oracle selection").
const upstreamMemo = new Map();
async function upstreamPromptTokens(kwargs) {
  const k = JSON.stringify(kwargs ?? null);
  if (!upstreamMemo.has(k)) {
    let n;
    if (ROUTER_IMPL === "rust") {
      const r = await upstreamNativeCall(kwargs);
      assert.equal(r.status, 200, `upstream native ${k}: expected 200, got ${r.status}: ${r.text.slice(0, 400)}`);
      assert.equal(typeof r.json?.usage?.input_tokens, "number", `upstream native ${k}: no usage.input_tokens: ${r.text.slice(0, 400)}`);
      n = occupancy(r.json.usage);
    } else {
      const r = await upstreamCall(kwargs);
      assert.equal(r.status, 200, `upstream ${k}: expected 200, got ${r.status}: ${r.text.slice(0, 400)}`);
      n = r.json?.usage?.prompt_tokens;
      assert.equal(typeof n, "number", `upstream ${k}: no usage.prompt_tokens: ${r.text.slice(0, 400)}`);
    }
    upstreamMemo.set(k, n);
  }
  return upstreamMemo.get(k);
}

async function routerOccupancy(extra, label) {
  const r = await post(`${BASE_URL}/v1/messages`, {
    "content-type": "application/json",
    "x-gateway-key": KEY,
    "anthropic-version": "2023-06-01",
    authorization: `Bearer ${DUMMY_BEARER}`,
  }, {
    model: ROUTER_MODEL,
    max_tokens: 1,
    temperature: 0,
    system: SYSTEM,
    messages: [{ role: "user", content: USER }],
    ...extra,
  }, "gateway");
  assert.ok(r.status >= 200 && r.status < 300,
    `${label}: router expected 2xx, got ${r.status}: ${r.text.slice(0, 400)}${upstreamHint(r.text)}`);
  const u = r.json?.usage;
  assert.equal(typeof u?.input_tokens, "number", `${label}: router response carried no usage.input_tokens: ${r.text.slice(0, 400)}`);
  return occupancy(u);
}

test("config: claude-local's effortKwargs row equals the contract table", () => {
  assert.ok(UPSTREAM_MODEL, `router/gateway.config.json claude-local carries no effortKwargs model (got ${JSON.stringify(EFFORT_MODELS)})`);
  assert.deepEqual(LOCAL.effortKwargs[UPSTREAM_MODEL], EXPECTED,
    `gateway.config.json effortKwargs.${UPSTREAM_MODEL} drifted from this test's contract table`);
});

test("upstream control: an unknown reasoning_effort is a 500 Jinja exception at llama", async () => {
  const r = await upstreamCall({ reasoning_effort: BOGUS });
  assert.equal(r.status, 500, `upstream bogus effort: expected 500, got ${r.status}: ${r.text.slice(0, 400)}`);
  assert.match(r.text, /Jinja Exception: Unexpected reasoning effort/,
    `upstream bogus effort: 500 body lacks the Jinja exception: ${r.text.slice(0, 400)}`);
});

test("upstream control: low, medium and absent kwargs render pairwise-distinct prompts", async () => {
  const low = await upstreamPromptTokens(EXPECTED.low);
  const medium = await upstreamPromptTokens(EXPECTED.medium);
  const absent = await upstreamPromptTokens(undefined);
  console.log(`[effort] oracle=${ROUTER_IMPL} upstream occupancy low=${low} medium=${medium} absent=${absent}`);
  assert.equal(new Set([low, medium, absent]).size, 3,
    `upstream low/medium/absent are not pairwise distinct (${low}/${medium}/${absent}); the render oracle cannot separate them`);
});

for (const level of ["low", "medium", "high", "xhigh", "max"]) {
  test(`output_config.effort ${level} delivers ${JSON.stringify(EXPECTED[level])}`, async () => {
    const want = await upstreamPromptTokens(EXPECTED[level]);
    const got = await routerOccupancy({ output_config: { effort: level } }, `effort ${level}`);
    console.log(`[effort] ${level}: router occupancy=${got} upstream=${want}`);
    assert.equal(got, want, `effort ${level}: router occupancy ${got} != upstream prompt_tokens ${want} for `
      + `${JSON.stringify(EXPECTED[level])} — the mapping is not delivering that row's kwargs to the template.`);
  });
}

test(`thinking:{type:"disabled"} delivers ${JSON.stringify(EXPECTED.disabled)}`, async () => {
  const want = await upstreamPromptTokens(EXPECTED.disabled);
  const got = await routerOccupancy({ thinking: { type: "disabled" } }, "thinking disabled");
  console.log(`[effort] disabled: router occupancy=${got} upstream=${want}`);
  assert.equal(got, want, `thinking disabled: router occupancy ${got} != upstream prompt_tokens ${want} `
    + "for enable_thinking:false.");
});

test("an unknown output_config.effort is a no-op (renders as absent kwargs, never a 500)", async () => {
  const want = await upstreamPromptTokens(undefined);
  const got = await routerOccupancy({ output_config: { effort: BOGUS } }, "effort bogus");
  console.log(`[effort] bogus: router occupancy=${got} upstream(absent)=${want}`);
  assert.equal(got, want, `bogus effort: router occupancy ${got} != upstream absent-kwargs prompt_tokens ${want}; `
    + "the router forwarded something for an unmapped level.");
});
