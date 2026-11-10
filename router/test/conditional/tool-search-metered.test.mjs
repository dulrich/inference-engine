// tool-search-metered.test.mjs — the codex-sidecar relay cell of the deferred-tool
// (ENABLE_TOOL_SEARCH) conformance gate, split out of the commit-suite file
// router/test/tool-search-conformance.test.mjs (plans/router-gate-consolidation.md WP-1; its sibling live cells now
// live in ./tool-search-conformance-live.test.mjs). Helpers from ../tool-search-shared.mjs and ../gateway-client.mjs.
//
// The cell POSTs one real /v1/messages turn on <codex persona>/<test model> (test-slots.json, CODEX_SIDECAR_CHEAP)
// carrying a tool_reference tool_result and a grown catalogue. The model must be listed exactly:
// an unlisted pinned id fails the cell naming it, before any turn is spent.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `bun test router/test/*.test.mjs` glob. Trigger surface (router/AGENTS.md "Gates" trigger
// table): `sidecar/` request handling; any router patch; this file (plus
// `router/smoke-tool-search.sh … --codex`). Runs approval-free at every sprint closeout.
//
// Env: ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY, TEST_SLOT_CODEX_SIDECAR_CHEAP (one-run override), CODEX_TEST_PERSONA.

import assert from "node:assert/strict";
import test from "node:test";
import { codexPersona, slotNotServed, testSlot } from "../../../scripts/lib/test-slots.mjs";
import { call, modelIds } from "../gateway-client.mjs";
import { roundTripBody } from "../tool-search-shared.mjs";

test("codex sidecar: a tool_reference result and the grown catalogue are relayed, not rejected", async t => {
  const ids = await modelIds();
  const slot = testSlot("CODEX_SIDECAR_CHEAP");
  const model = `${codexPersona(ids)}/${slot}`;
  assert.ok(ids.includes(model), `${slotNotServed("CODEX_SIDECAR_CHEAP", slot)} (GET /v1/models does not list ${model})`);
  t.diagnostic(`codex relay probe on ${model}`);
  const r = await call("/v1/messages", roundTripBody(model), { timeoutMs: 300000 });
  assert.equal(r.status, 200, `the codex leg rejected the discovery round-trip with ${r.status}: ${r.text.slice(0, 600)}${r.hint}`);
  assert.equal(r.json?.type, "message", `expected a Messages response, got: ${r.text.slice(0, 400)}`);
  assert.ok(Array.isArray(r.json.content), "the response must carry a content array");
  // No assertion on the model's TEXT (it may quote the body's own deferred-tools notice); a
  // relay-level rejection is the non-200 / non-message assertions above.
});
