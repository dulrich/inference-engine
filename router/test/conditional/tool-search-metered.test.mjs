// tool-search-metered.test.mjs — the metered codex-sidecar relay cell of the deferred-tool
// (ENABLE_TOOL_SEARCH) conformance gate, split out of router/test/tool-search-conformance.test.mjs
// (plans/router-gate-consolidation.md WP-1). Helpers from ../tool-search-shared.mjs.
//
// METERED — the cell POSTs one real /v1/messages turn on codex-b/gpt-6-luna (one
// Codex-subscription turn) carrying a tool_reference tool_result and a grown catalogue. Without
// TOOL_SEARCH_METERED=1 the file registers one test and it FAILS loudly naming the flag — it was
// a `t.skip()` before the split, and a silently-skipped gate is the failure mode these files exist
// to prevent.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `bun test router/test/*.test.mjs` glob. Trigger surface (router/AGENTS.md "Gates" trigger
// table): `sidecar/` request handling; any router patch; this file (plus
// `router/smoke-tool-search.sh … --codex`). A triggered run without the flag is reported to the
// user as not run, naming TOOL_SEARCH_METERED.
//
// Env: TOOL_SEARCH_METERED (must be "1"), ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY.

import assert from "node:assert/strict";
import test from "node:test";

const METERED = process.env.TOOL_SEARCH_METERED === "1";

if (!METERED) {
  test("codex sidecar tool-search relay (metered): not run — set TOOL_SEARCH_METERED=1", () => {
    assert.fail("not run — set TOOL_SEARCH_METERED=1 to bill one Codex-subscription turn "
      + "(POST /v1/messages on codex-b/gpt-6-luna with a tool_reference tool_result and a grown catalogue). "
      + "This file fails rather than skips without it; report a triggered run without the flag as not run.");
  });
} else {
  const { call, modelIds, resolveModel, roundTripBody } = await import("../tool-search-shared.mjs");

  test("codex sidecar: a tool_reference result and the grown catalogue are relayed, not rejected", async t => {
    const ids = await modelIds();
    const { id: model } = resolveModel(ids, { preferred: "codex-b/gpt-6-luna", prefix: "codex-b/" });
    t.diagnostic(`codex relay probe on ${model} (metered)`);
    const r = await call("/v1/messages", roundTripBody(model), { timeoutMs: 300000 });
    assert.equal(r.status, 200, `the codex leg rejected the discovery round-trip with ${r.status}: ${r.text.slice(0, 600)}${r.hint}`);
    assert.equal(r.json?.type, "message", `expected a Messages response, got: ${r.text.slice(0, 400)}`);
    assert.ok(Array.isArray(r.json.content), "the response must carry a content array");
    // No assertion on the model's TEXT (it may quote the body's own deferred-tools notice); a
    // relay-level rejection is the non-200 / non-message assertions above.
  });
}
