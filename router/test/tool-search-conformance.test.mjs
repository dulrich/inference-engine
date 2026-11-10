// tool-search-conformance.test.mjs — the offline cell of the deferred-tool (ENABLE_TOOL_SEARCH)
// conformance gate (docs/tool-calling-gateway/tool-search-conformance-spike.md): a fixture self-check
// that the byte-fixed synthetic bodies (./tool-search-shared.mjs) carry the deferred shape and the
// eager control does not. It sends nothing — no gateway, no key, no network — so it belongs to the
// per-commit `bun test router/test/*.test.mjs` glob.
//
// Every cell that sends a request (the warm-up, the per-leg count_tokens token gates, the local relay
// round-trip) lives in conditional/tool-search-conformance-live.test.mjs, whose header carries the
// method notes (bodies, token metric, auth, fail-never-skip); the hosted passthrough leg is in
// conditional/tool-search-hosted.test.mjs and the codex relay in conditional/tool-search-metered.test.mjs.
// router/AGENTS.md "Gates" trigger table says when those run (plans/publication-and-test-gate-remediation.md WP-R1).

import { registerDeferredShapeCheck } from "./tool-search-shared.mjs";

registerDeferredShapeCheck();
