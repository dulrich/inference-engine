// tool-search-hosted.test.mjs — the hosted Claude passthrough leg of the deferred-tool
// (ENABLE_TOOL_SEARCH) conformance gate, split out of router/test/tool-search-conformance.test.mjs
// (plans/router-gate-consolidation.md WP-1). Same cells, same method notes as that file's header;
// helpers from ../tool-search-shared.mjs.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `node --test router/test/*.test.mjs` glob. Trigger surface (router/AGENTS.md "Gates" trigger
// table): router/gateway.config.json providers; any router patch; router/Dockerfile; this file
// (plus `router/smoke-tool-search.sh` default and `--passthrough`).
//
// Credential. The passthrough leg forwards the caller's own OAuth bearer, so its token-gate cell
// needs $ROUTER_HOSTED_BEARER; without it that cell FAILS loudly naming the variable — never
// skips. A triggered run without the bearer is reported to the user as not run, naming
// ROUTER_HOSTED_BEARER.
//
// Env: ROUTER_HOSTED_BEARER, ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY, TOOL_SEARCH_CAPTURE_DIR.

import { LEGS, registerLegTests } from "../tool-search-shared.mjs";

for (const leg of LEGS.filter(l => l.hosted)) registerLegTests(leg);
