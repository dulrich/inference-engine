// tool-search-hosted.test.mjs — the hosted Claude passthrough leg of the deferred-tool
// (ENABLE_TOOL_SEARCH) conformance gate, split out of router/test/tool-search-conformance.test.mjs
// (plans/router-gate-consolidation.md WP-1). Same cells, same method notes as the header of
// ./tool-search-conformance-live.test.mjs; helpers from ../tool-search-shared.mjs.
//
// CONDITIONAL — lives in router/test/conditional/, outside the per-commit
// `bun test router/test/*.test.mjs` glob. Trigger surface (router/AGENTS.md "Gates" trigger
// table): router/gateway.config.json providers; any change under router/gateway/**; this file
// (plus `router/smoke-tool-search.sh` default and `--passthrough`).
//
// Credential. The passthrough leg forwards the caller's own OAuth bearer, so its token-gate cell
// needs $ROUTER_HOSTED_BEARER, else a ROUTER_HOSTED_BEARER line in the router env file (the same
// file and order gatewayKey() uses); without either that cell FAILS loudly naming the variable — never
// skips. A triggered run without the bearer is reported to the user as not run, naming
// ROUTER_HOSTED_BEARER.
//
// Env: ROUTER_HOSTED_BEARER, ROUTER_BASE_URL, ROUTER_ENV_FILE, GATEWAY_KEY.

import { LEGS, registerLegTests } from "../tool-search-shared.mjs";

for (const leg of LEGS.filter(l => l.hosted)) registerLegTests(leg);
