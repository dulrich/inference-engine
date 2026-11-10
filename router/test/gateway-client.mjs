// gateway-client.mjs — the one black-box gateway client shared by the live router test files
// (conditional/tool-search-conformance-live, conditional/tool-search-hosted, conditional/tool-search-metered,
// conditional/effort-mapping, conditional/effort-mapping-codex). Not a test file: the `*.test.mjs`
// glob does not load it.
//
// Importing this module is inert: it reads no key, no env file and touches no network. The gateway
// key is resolved only on the first live call (`call`) or an explicit `gatewayKey()`; the default
// per-commit glob therefore needs no GATEWAY_KEY and no running gateway.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE (default: `fleet-env path router`)
// and is never printed. Env: ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY.

import { routerEnvFile } from "../fleet-env.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";

const BASE_URL = (process.env.ROUTER_BASE_URL || "http://127.0.0.1:4813").replace(/\/$/, "");
// ROUTER_ENV_FILE, else `fleet-env path router` — resolved lazily, only when $GATEWAY_KEY is unset
// (run under `fleet-env run router-client -- bun test …` and the file is never touched).
let envFileMemo;
const envFile = () => (envFileMemo ??= routerEnvFile());
// The engine's anthropic-type legs want *some* client bearer to build passthrough headers; the
// provider's own apiKeyEnv replaces it before the upstream (smoke-router.sh checks 7 and 8).
const DUMMY_BEARER = "router-live-test-dummy-client-bearer";

function gatewayKey() {
  if (process.env.GATEWAY_KEY) return process.env.GATEWAY_KEY;
  let text = "";
  try {
    text = fs.readFileSync(envFile(), "utf8");
  } catch (err) {
    assert.fail(`no gateway key: $GATEWAY_KEY is unset and ${envFile()} is unreadable (${err.code}). `
      + "Set GATEWAY_KEY (`fleet-env run router-client -- bun test …`) or point ROUTER_ENV_FILE at the env file holding INFERENCE_ROUTER_GATEWAY_KEY.");
  }
  const hit = /^INFERENCE_ROUTER_GATEWAY_KEY=(.*)$/m.exec(text);
  if (!hit || !hit[1].trim()) {
    assert.fail(`${envFile()} carries no INFERENCE_ROUTER_GATEWAY_KEY; set $GATEWAY_KEY instead.`);
  }
  return hit[1].trim();          // never logged, never asserted on
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

async function call(pathname, body, { bearer = DUMMY_BEARER, timeoutMs = 60000 } = {}) {
  const key = gatewayKey();
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`${BASE_URL}${pathname}`, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        "content-type": "application/json",
        "x-gateway-key": key,
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

export { BASE_URL, DUMMY_BEARER, call, envFile, gatewayKey, modelIds, upstreamHint };
