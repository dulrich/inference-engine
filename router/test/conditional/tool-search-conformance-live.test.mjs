// tool-search-conformance-live.test.mjs — the live cells of the deferred-tool (ENABLE_TOOL_SEARCH)
// conformance gate through the resident gateway, locked from the WP-5 wire spike
// (docs/tool-calling-gateway/tool-search-conformance-spike.md). Split out of
// router/test/tool-search-conformance.test.mjs, which keeps only the offline fixture self-check.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `bun test router/test/*.test.mjs` glob, because every cell sends a request to the live gateway
// (and warms the local model). Trigger surface (router/AGENTS.md "Gates" trigger table): gateway
// tool-search / `count_tokens` handling, the local relay (`router/gateway/src/local_leg.rs`), the
// codex unmetered leg (`sidecar/` request handling); this file. Runs approval-free at every sprint
// closeout (the codex cells need the live codex-sidecar unit but bill nothing).
//
// This is an explicitly BLACK-BOX test. The gateway's source lives only inside the Docker build
// checkout, so nothing here imports it: every assertion is made against a running gateway over
// HTTP. When that gateway is unreachable the tests FAIL, naming the base URL — they never skip.
// A conformance gate that quietly disappears when its target is down is not a gate.
//
// Bodies. Byte-fixed synthetic bodies of the spike's documented shape, built in-test (12 tools:
// a fully-schema'd ToolSearch, exactly one `defer_loading: true` DeferredToolPlaceholder, the
// always-on tools; the deferred names advertised in an interleaved role:"system" message; the
// eager control materialises all 20). The token thresholds are measured on these bodies per leg
// (LEGS[].synth in ../tool-search-shared.mjs), not the spike's real-profile figures; each leg asserts
// ordering plus its measured tools-only ratio floor (LEGS[].synth.minRatio). (The spike's captured
// /tmp bodies were throwaway and are no longer replayed, 2026-10-19.)
//
// Token metric. Exactly the spike's: POST /v1/messages/count_tokens with the cell's own
// `system` + `tools` and a one-word user message, on that cell's own leg. Cross-leg comparison
// is invalid (different catalogues, different tokenizers) and is never attempted.
//
// Cells: the unmetered count_tokens token gates for the local and codex legs, plus the local
// relay round-trip. The hosted passthrough leg lives in tool-search-hosted.test.mjs
// ($ROUTER_HOSTED_BEARER) and the codex relay in tool-search-metered.test.mjs (this directory).
// Shared helpers: ../tool-search-shared.mjs; gateway client: ../gateway-client.mjs.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE
// (default: `fleet-env path router`) and is never printed.
//
// Env: ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY.

import assert from "node:assert/strict";
import test from "node:test";
import { call, modelIds } from "../gateway-client.mjs";
import { LEGS, registerLegTests, roundTripBody } from "../tool-search-shared.mjs";

// Warm-up (unmeasured): load the local leg's model before any measured cell, so a cold llama-swap
// spawn never lands inside a measured test's default 5000 ms budget. It is a TEST, not a before()
// hook: bun's node:test hooks ignore a timeout option and die at 5000 ms (measured, bun 1.4.4).
test("warm-up: the local leg's model is loaded and answers (unmeasured)", { timeout: 240000 }, async t => {
  const { id: model } = LEGS.find(l => l.cell === "local").pick(await modelIds());
  t.diagnostic(`warm-up on ${model}`);
  const r = await call("/v1/messages", { model, max_tokens: 1, messages: [{ role: "user", content: "hi" }] }, { timeoutMs: 180000 });
  assert.equal(r.status, 200, `warm-up on ${model} failed with ${r.status}: ${r.text.slice(0, 400)}${r.hint}`);
});

for (const leg of LEGS.filter(l => !l.hosted)) registerLegTests(leg);

// ───────────────────────────────────────────────────────── relay of the discovery round-trip

// Layer 1 asserts that the wire shapes of the discovery round-trip survive the leg: a
// tool_reference tool_result and a grown catalogue are accepted and answered normally, with no
// 4xx and no malformed continuation. Whether a given model *chooses* to
// call ToolSearch is a behavioural property and belongs to router/smoke-tool-search.sh, which
// drives a real `claude -p` turn.

test("local OpenAI-conversion: a tool_reference result and the grown catalogue are relayed, not rejected", async t => {
  const ids = await modelIds();
  const { id: model } = LEGS.find(l => l.cell === "local").pick(ids);
  t.diagnostic(`local relay probe on ${model}`);
  const r = await call("/v1/messages", roundTripBody(model), { timeoutMs: 180000 });
  assert.equal(r.status, 200, `the local leg rejected the discovery round-trip with ${r.status}: ${r.text.slice(0, 600)}${r.hint}`);
  assert.equal(r.json?.type, "message", `expected a Messages response, got: ${r.text.slice(0, 400)}`);
  assert.ok(Array.isArray(r.json.content), "the response must carry a content array");
  assert.ok(["end_turn", "tool_use", "max_tokens", "stop_sequence"].includes(r.json.stop_reason),
    `malformed continuation: stop_reason=${String(r.json.stop_reason)}`);
  // No assertion on the model's TEXT: the round-trip body itself carries the deferred-tools
  // notice (which names InputValidationError), and a model may quote it back in thinking or
  // prose. A relay-level rejection is already the non-200 / non-message assertions above.
});
