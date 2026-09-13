// effort-mapping.test.mjs — black-box gate for patch 0007: the client's reasoning intent
// (Anthropic `output_config.effort` / `thinking`) reaching the local leg's template as
// `chat_template_kwargs` (router/AGENTS.md, "The patches", 0007;
// plans/router-effort-mapping-claude-local.md WP-2).
//
// BLACK BOX, like tool-search-conformance.test.mjs: the gateway's source lives only inside the
// Docker build checkout, so nothing here imports it — every assertion is made over HTTP against
// a running gateway. When that gateway is unreachable the tests FAIL; they never skip. A gate
// that quietly disappears when its target is down is not a gate.
//
// What is asserted, and why it is shaped this way:
//   (a) effort low vs medium — a TOKEN-COUNT gate, median of N>=2 samples each, medium's median
//       at least MARGIN above low's. The plan's audit (M4) asked for N>=5 because its own n=2
//       ranges overlapped — but those were sampled at non-zero temperature; at temperature 0
//       every sample of a level is byte-identical (measured 2026-09-12: 5/5 identical per level),
//       so N=2 already proves determinism and N=5 only multiplies GPU minutes (~25 min per run
//       with this prompt). Amended to N>=2 at WP-2 review (user decision 2026-09-12);
//       EFFORT_SAMPLES raises it. Sampling is pinned with temperature 0: the conversion
//       leg builds the upstream body from a field whitelist (model/messages/temperature/top_p/
//       max_tokens/stop/tools), so a client `seed` does not reach llama-server — greedy decoding
//       at temperature 0 is what makes the samples comparable, and the seed below is sent only
//       so the intent is on the record.
//   (b) effort high — ACCEPTANCE only, no token gate: `high` maps to the template's `xhigh`,
//       which is the template default, and was not separable from no-kwargs at n=2 (audit M3).
//   (c) thinking:{type:"disabled"} — deterministic, one sample: the response must carry NO
//       `thinking` content block at all (the map sends `enable_thinking:false`).
//
// The prompt is deliberately SHORT with a short system prompt, so the router's info-level
// upstream-body preview (truncated at 32 KiB) shows the merged `chat_template_kwargs` for the
// evidence grep in the WP report. The plan's Context measured "a reasoning-heavy prompt" whose
// text was never recorded verbatim, so the puzzle below is this test's own pin — absolute token
// counts are therefore not comparable with the plan's, only the low-vs-medium direction is.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE (default
// <code-root>/fleet-tools/.env) and is never printed.
//
// Env: ROUTER_BASE_URL (default http://127.0.0.1:4813), ROUTER_ENV_FILE, GATEWAY_KEY,
//      EFFORT_SAMPLES (default 2, floor 2).

import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const BASE_URL = (process.env.ROUTER_BASE_URL || "http://127.0.0.1:4813").replace(/\/$/, "");
const ENV_FILE = process.env.ROUTER_ENV_FILE || "<code-root>/fleet-tools/.env";
const SAMPLES = Math.max(2, Number(process.env.EFFORT_SAMPLES || 2));
const MARGIN = 1.15;                 // medium median must exceed low median by >= 15%
const MODEL = "claude-local/qwen38-27b-iq3s";
// The engine's anthropic-type legs want *some* client bearer to build passthrough headers; the
// provider's own apikey replaces it before the upstream (smoke-router.sh checks 7 and 8).
const DUMMY_BEARER = "effort-mapping-dummy-client-bearer";
const REQUEST_TIMEOUT_MS = 300000;
const TEST_TIMEOUT_MS = 1800000;

const SYSTEM = "You are a careful reasoner. Give the final answer in at most three sentences.";
// The prompt must SATURATE reasoning, not merely be "hard": measured 2026-09-12 against this
// patch on :4819, the low->medium token direction is prompt-dependent on this template. A
// classic riddle the model has memorised, a divisor count and a friction problem all came out
// medium < low (594/557, 3229/1673, 1378/890 output_tokens); only a constraint puzzle whose
// deduction the model must actually walk separates the levels (2965 low vs 7919 medium,
// both end_turn at max_tokens 12000). Hence this prompt and this ceiling — changing either
// invalidates the margin below. See the WP-2 report for the full table.
const PROMPT =
  "Four friends — Ada, Ben, Cleo and Dan — each own one pet (cat, dog, fish, bird) and one car "
  + "colour (red, blue, green, white). Ada does not own the cat or the red car. The dog owner "
  + "drives blue. Ben drives green and does not own the fish. Cleo owns the bird or the fish, and "
  + "does not drive white. Dan does not own the dog. Work out every pairing and say why it is "
  + "forced.";

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

async function messages(body, { timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${BASE_URL}/v1/messages`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-gateway-key": KEY,
        "anthropic-version": "2023-06-01",
        authorization: `Bearer ${DUMMY_BEARER}`,
      },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
  } catch (err) {
    assert.fail(`gateway at ${BASE_URL} is unreachable (POST /v1/messages): ${err.name}: ${err.message}. `
      + "This test is a black-box gate against the router and fails rather than skips when it is down. "
      + "Start it, or point ROUTER_BASE_URL elsewhere.");
  } finally {
    clearTimeout(timer);
  }
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, text, json, hint: upstreamHint(text) };
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

function probeBody(extra) {
  return {
    model: MODEL,
    max_tokens: 12000,          // must not truncate: a clipped medium sample hides the margin
    temperature: 0,
    seed: 20260912,                  // intent on the record; the conversion leg's whitelist drops it
    system: SYSTEM,
    messages: [{ role: "user", content: PROMPT }],
    ...extra,
  };
}

function outputTokens(r, label) {
  assert.ok(r.status >= 200 && r.status < 300, `${label}: expected 2xx, got ${r.status}: ${r.text.slice(0, 400)}${r.hint}`);
  const tokens = r.json?.usage?.output_tokens;
  assert.equal(typeof tokens, "number", `${label}: response carried no usage.output_tokens: ${r.text.slice(0, 400)}`);
  return tokens;
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

async function sampleEffort(effort) {
  const samples = [];
  for (let i = 0; i < SAMPLES; i += 1) {
    const r = await messages(probeBody({ output_config: { effort } }));
    samples.push(outputTokens(r, `effort ${effort} sample ${i + 1}`));
  }
  return samples;
}

test(`output_config.effort medium outputs at least ${Math.round((MARGIN - 1) * 100)}% more tokens than low`,
  { timeout: TEST_TIMEOUT_MS }, async () => {
    // Sequential, never parallel: this shares one GPU with the rest of the fleet.
    const low = await sampleEffort("low");
    const medium = await sampleEffort("medium");
    const lowMedian = median(low);
    const mediumMedian = median(medium);

    console.log(`[effort] low    samples=${JSON.stringify(low)} median=${lowMedian}`);
    console.log(`[effort] medium samples=${JSON.stringify(medium)} median=${mediumMedian}`);
    console.log(`[effort] ratio=${(mediumMedian / lowMedian).toFixed(3)} (gate >= ${MARGIN})`);

    assert.ok(lowMedian > 0, `low median output_tokens was ${lowMedian}`);
    assert.ok(
      mediumMedian >= lowMedian * MARGIN,
      `medium median output_tokens (${mediumMedian}) is not >= ${MARGIN}x low median (${lowMedian}); `
      + `low=${JSON.stringify(low)} medium=${JSON.stringify(medium)}. Either the mapping is not reaching `
      + "the template, or the template's vocabulary changed (check the router's upstream-body preview "
      + "for chat_template_kwargs).",
    );
  });

test("output_config.effort high is accepted (maps to the template's xhigh)",
  { timeout: TEST_TIMEOUT_MS }, async () => {
    // Acceptance only: xhigh is the template default and was not separable at n=2 (audit M3).
    // An unmapped value would make the Jinja template 500, so a 2xx here is the real assertion.
    const r = await messages(probeBody({ output_config: { effort: "high" } }));
    const tokens = outputTokens(r, "effort high");
    console.log(`[effort] high   output_tokens=${tokens} stop_reason=${r.json?.stop_reason}`);
  });

test("thinking:{type:\"disabled\"} yields a response with no thinking block",
  { timeout: TEST_TIMEOUT_MS }, async () => {
    const r = await messages(probeBody({ thinking: { type: "disabled" } }));
    assert.ok(r.status >= 200 && r.status < 300, `thinking disabled: expected 2xx, got ${r.status}: ${r.text.slice(0, 400)}${r.hint}`);
    const blocks = Array.isArray(r.json?.content) ? r.json.content : [];
    const kinds = blocks.map((b) => b?.type);
    console.log(`[effort] disabled blocks=${JSON.stringify(kinds)} output_tokens=${r.json?.usage?.output_tokens}`);
    assert.ok(blocks.length > 0, `thinking disabled: response carried no content blocks: ${r.text.slice(0, 400)}`);
    assert.ok(
      !kinds.includes("thinking"),
      `thinking disabled: response still carried a thinking block (${JSON.stringify(kinds)}); `
      + "enable_thinking:false did not reach the template.",
    );
  });
