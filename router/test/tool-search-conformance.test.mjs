// tool-search-conformance.test.mjs — deferred-tool (ENABLE_TOOL_SEARCH) conformance through the
// resident gateway, locked from the WP-5 wire spike
// (docs/tool-calling-gateway/tool-search-conformance-spike.md).
//
// This is an explicitly BLACK-BOX test. The gateway's source lives only inside the Docker build
// checkout, so nothing here imports it: every assertion is made against a running gateway over
// HTTP. When that gateway is unreachable the tests FAIL — they never skip. A conformance gate
// that quietly disappears when its target is down is not a gate.
//
// Bodies. Byte-fixed synthetic bodies of the spike's documented shape, built in-test (12 tools:
// a fully-schema'd ToolSearch, exactly one `defer_loading: true` DeferredToolPlaceholder, the
// always-on tools; the deferred names advertised in an interleaved role:"system" message; the
// eager control materialises all 20). The token thresholds are measured on these bodies per leg
// (LEGS[].synth in ./tool-search-shared.mjs), not the spike's real-profile figures, and apply only
// on the exact model measured; a leg without them asserts ordering only. (The spike's captured
// /tmp bodies were throwaway and are no longer replayed, 2026-10-19.)
//
// Token metric. Exactly the spike's: POST /v1/messages/count_tokens with the cell's own
// `system` + `tools` and a one-word user message, on that cell's own leg. Cross-leg comparison
// is invalid (different catalogues, different tokenizers) and is never attempted.
//
// Split (plans/router-gate-consolidation.md WP-1): this per-commit file carries the offline
// shape cells and the unmetered count_tokens token gates for the local and codex legs (the codex
// cells need the live codex-sidecar unit, but bill nothing), plus the local relay round-trip.
// The hosted passthrough leg lives in conditional/tool-search-hosted.test.mjs
// ($ROUTER_HOSTED_BEARER) and the metered codex relay in conditional/tool-search-metered.test.mjs
// (TOOL_SEARCH_METERED=1); router/AGENTS.md "Gates" trigger table says when those run. Shared
// helpers: ./tool-search-shared.mjs.
//
// Auth. The gateway key comes from $GATEWAY_KEY or $ROUTER_ENV_FILE
// (default <code-root>/fleet-tools/.env) and is never printed.
//
// Env: ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY.

import assert from "node:assert/strict";
import test from "node:test";
import { LEGS, call, modelIds, registerLegTests, roundTripBody } from "./tool-search-shared.mjs";

for (const leg of LEGS.filter(l => !l.hosted)) registerLegTests(leg);

// ───────────────────────────────────────────────────────── relay of the discovery round-trip

// Layer 1 asserts that the wire shapes of the discovery round-trip survive the leg: a
// tool_reference tool_result and a grown catalogue are accepted and answered normally, with no
// 4xx, no "tool not found" and no malformed continuation. Whether a given model *chooses* to
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
  const text = JSON.stringify(r.json.content);
  assert.ok(!/tool[ _]not[ _]found|unknown tool|InputValidationError/i.test(text),
    `the leg reported the deferred tool as unavailable after discovery: ${text.slice(0, 400)}`);
});
