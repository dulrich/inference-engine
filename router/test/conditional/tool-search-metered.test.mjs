// tool-search-metered.test.mjs — the codex-sidecar relay cell of the deferred-tool
// (ENABLE_TOOL_SEARCH) conformance gate, split out of router/test/tool-search-conformance.test.mjs
// (plans/router-gate-consolidation.md WP-1). Helpers from ../tool-search-shared.mjs.
//
// The cell POSTs one real /v1/messages turn on codex-b/<test model> (router/test/test-models.json)
// carrying a tool_reference tool_result and a grown catalogue. The model must be listed exactly:
// an unlisted pinned id fails the cell naming it, before any turn is spent.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `bun test router/test/*.test.mjs` glob. Trigger surface (router/AGENTS.md "Gates" trigger
// table): `sidecar/` request handling; any router patch; this file (plus
// `router/smoke-tool-search.sh … --codex`). Runs approval-free at every sprint closeout.
//
// Env: ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY, CODEX_TEST_MODEL (one-run override).

import assert from "node:assert/strict";
import test from "node:test";
import { codexTestModel } from "../test-models.mjs";
import { call, modelIds, resolveModel, roundTripBody } from "../tool-search-shared.mjs";

test("codex sidecar: a tool_reference result and the grown catalogue are relayed, not rejected", async t => {
  const ids = await modelIds();
  const { id: model } = resolveModel(ids, { preferred: codexTestModel("codex-b"), prefix: "codex-b/" });
  t.diagnostic(`codex relay probe on ${model}`);
  const r = await call("/v1/messages", roundTripBody(model), { timeoutMs: 300000 });
  assert.equal(r.status, 200, `the codex leg rejected the discovery round-trip with ${r.status}: ${r.text.slice(0, 600)}${r.hint}`);
  assert.equal(r.json?.type, "message", `expected a Messages response, got: ${r.text.slice(0, 400)}`);
  assert.ok(Array.isArray(r.json.content), "the response must carry a content array");
  // No assertion on the model's TEXT (it may quote the body's own deferred-tools notice); a
  // relay-level rejection is the non-200 / non-message assertions above.
});
