# router/Dockerfile — @the-next-ai/ai-gateway (https://github.com/The-NeXT-AI/ai-gateway),
# built from source at a pinned commit with a seven-patch vendored series. Never `npm install
# @the-next-ai/ai-gateway` and never a moving branch/tag — the pin below is a commit SHA.
#
# See router/AGENTS.md for the patch rationale and the never-set-ANTHROPIC_API_KEY constraint,
# and router/patches/000{1,2,3,4,5,6,7}-*.patch for the changes themselves: 0001 omits x-api-key
# for the passthrough OAuth bearer, 0002 adds the local-leg prefill/idle-gap ping keepalive,
# 0003 relays upstream error bodies byte-verbatim to Anthropic-protocol clients on the final
# attempt, 0004 adds the POST /v1/messages/count_tokens route, 0005 is a dependency-only
# lockfile bump (find-my-way 9.9.0, fast-uri 3.1.6, brace-expansion 5.0.9 — transitive
# dependencies, within their parents' already-declared semver ranges, no source change),
# 0006 adds the bounded, rotated per-request telemetry jsonl sink, and 0007 maps the client's
# reasoning intent (thinking / output_config.effort) onto per-model chat_template_kwargs on the
# openai_chat_completions conversion leg, from the new gateway.config.json `effortKwargs` key.
# The replace-vs-integrate design spike the stabilization series
# (plans/router-prefill-keepalive-and-protocol-gaps.md) required before any further semantic
# patch was run and answered "continue vendoring on this pin" with evidence —
# docs/tool-calling-gateway/router-telemetry-baseline.md, section "Replace-vs-integrate
# verdict" — which is what authorises 0006; 0007 is authorised by that section's dated second
# entry ("Second entry — 2026-09-12, before semantic patch 0007 (effort mapping)"), which
# re-ran the spike for a semantic conversion patch and again answered continue-vendoring. The
# next semantic patch after 0007, or the first nontrivial pin-upgrade rebase, re-triggers that
# spike.
#
# Deviation from the plan's literal example command (recorded in
# runs/dispatch/resident-router-wp-1-report.md): `git fetch --depth 1 origin 782cd51` (the short
# form used throughout plans/resident-router-ai-gateway.md and the brief) does NOT work against
# GitHub's smart-HTTP fetch protocol — "fatal: couldn't find remote ref 782cd51", measured
# live 2026-08-25. GitHub's allowReachableSHA1InWant-style fetch-by-commit only resolves a
# FULL 40-character SHA, not an abbreviation. AI_GATEWAY_COMMIT below is therefore the full SHA
# that `782cd51` resolves to (confirmed via `git log -1` after fetch: "Bump package version to
# 1.0.18") — same pinned commit, just spelled out in the only form the fetch protocol accepts.

FROM node:24-slim AS build

# Full SHA for 782cd51 (782cd51941e5da198ea6543feb7dc9e6e8ee3915) — see the deviation note
# above for why the short form cannot be used in the `git fetch` step itself.
ARG AI_GATEWAY_COMMIT=782cd51941e5da198ea6543feb7dc9e6e8ee3915

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Shallow fetch of the exact pinned commit — never a moving branch/tag.
RUN git init -q \
    && git remote add origin https://github.com/The-NeXT-AI/ai-gateway.git \
    && git fetch --depth 1 origin "${AI_GATEWAY_COMMIT}" \
    && git checkout -q FETCH_HEAD

COPY patches/0001-omit-x-api-key-for-passthrough-bearer.patch /tmp/0001-omit-x-api-key-for-passthrough-bearer.patch
RUN git apply --check /tmp/0001-omit-x-api-key-for-passthrough-bearer.patch \
    && git apply /tmp/0001-omit-x-api-key-for-passthrough-bearer.patch

COPY patches/0002-prefill-keepalive-ping.patch /tmp/0002-prefill-keepalive-ping.patch
RUN git apply --check /tmp/0002-prefill-keepalive-ping.patch \
    && git apply /tmp/0002-prefill-keepalive-ping.patch

COPY patches/0003-upstream-error-body-passthrough.patch /tmp/0003-upstream-error-body-passthrough.patch
RUN git apply --check /tmp/0003-upstream-error-body-passthrough.patch \
    && git apply /tmp/0003-upstream-error-body-passthrough.patch

COPY patches/0004-count-tokens-route.patch /tmp/0004-count-tokens-route.patch
RUN git apply --check /tmp/0004-count-tokens-route.patch \
    && git apply /tmp/0004-count-tokens-route.patch

COPY patches/0005-dependency-security-bump.patch /tmp/0005-dependency-security-bump.patch
RUN git apply --check /tmp/0005-dependency-security-bump.patch \
    && git apply /tmp/0005-dependency-security-bump.patch

COPY patches/0006-request-telemetry-jsonl.patch /tmp/0006-request-telemetry-jsonl.patch
RUN git apply --check /tmp/0006-request-telemetry-jsonl.patch \
    && git apply /tmp/0006-request-telemetry-jsonl.patch

COPY patches/0007-effort-mapping-claude-local.patch /tmp/0007-effort-mapping-claude-local.patch
RUN git apply --check /tmp/0007-effort-mapping-claude-local.patch \
    && git apply /tmp/0007-effort-mapping-claude-local.patch

# The repo's own package-lock.json — measured green 2026-08-25 (145 packages, <1s).
RUN npm ci

# build.js -> esbuild bundle to dist/index.js — measured green 2026-08-25 (dist byte-identical
# in size to the published npm 1.0.18 artifact, before this patch's small addition).
RUN npm run build

# Build-stage gate (repo Gates rule, "the relevant... run green"): the patched credential-header
# behavior is locked by common.test.ts (14/14, unchanged since 0001); the local-leg idle-gap
# keepalive added by 0002 is locked by streaming-conversion.test.ts (9 cases); the upstream
# error-body passthrough (0003) and the count_tokens route (0004) are locked by
# routes.test.ts, which also carries the engine's own pre-existing envelope tests unmodified
# (the M2 cross-check that other source protocols/non-final attempts still get the envelope);
# the telemetry sink added by 0006 — record fields, redaction (a request carrying a bearer, the
# gateway key header and message text must leave none of them in the written line), the 1000 ms
# progress cadence, and size rotation/retention — is locked by request-telemetry.test.ts (12).
# The effort->chat_template_kwargs mapping added by 0007 — the level table
# (output_config.effort incl. max/xhigh, thinking disabled, the budget_tokens cut points),
# the no-op cases (no map, no level, non-chat target, no
# standardRequest) and the body-keys-win merge, end to end through the Responses target build
# and through parseProvidersFromRaw — is locked by effort-kwargs.test.ts (21). Baseline at
# 782cd51 with 0001-0005 applied is 163/163; with 0006 it is 175/175; with 0007 it is 196/196,
# 0 failing. A non-zero exit here fails the image build.
RUN npx vitest run src/adapters/builtins/common.test.ts src/gateway/streaming-conversion.test.ts src/gateway/routes.test.ts src/gateway/request-telemetry.test.ts src/adapters/builtins/target/openai-chat-compat/effort-kwargs.test.ts

# esbuild bundles everything except fastify/undici/ws (build.js's `external` list) into
# dist/index.js, so pruning devDependencies is the only trim the runtime stage needs.
RUN npm prune --omit=dev

FROM node:24-slim AS runtime

LABEL org.opencontainers.image.title="inference-engine-router" \
      org.opencontainers.image.description="@the-next-ai/ai-gateway 1.0.18, source build at 782cd51, seven-patch series: x-api-key passthrough-bearer omission, prefill/idle-gap ping keepalive, upstream error-body passthrough, /v1/messages/count_tokens route, dependency-only lockfile bump (find-my-way/fast-uri/brace-expansion), per-request telemetry jsonl sink, effort -> chat_template_kwargs mapping on the openai_chat_completions leg"

WORKDIR /app

COPY --from=build /src/dist ./dist
COPY --from=build /src/bin ./bin
COPY --from=build /src/node_modules ./node_modules
COPY --from=build /src/package.json ./package.json

# The bundled npm CLI is dead weight at runtime — the entrypoint is `node bin/next-ai-gateway.js`,
# never `npm`/`npx`, and it was the source of 1 CRITICAL + 7 HIGH findings in
# fleet-tools/data/security-scan/inference-engine.images.json.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack

# gateway.config.json is bind-mounted here at runtime (compose, WP-2) — never baked into the
# image, matching this repo's config-is-never-baked convention (llama/config.yaml, onnx/models.yaml).
ENV GATEWAY_CONFIG_PATH=/app/gateway.config.json
ENV NODE_ENV=production

# Patch 0006's per-request telemetry sink. The directory is a host bind mount in compose
# (~/.local/state/inference-router/logs -> here, with the service running as
# 1001:1001); when it is missing or unwritable the sink logs one warning and disables itself —
# the proxy path never fails because of telemetry. Records are whole jsonl lines in
# router-YYYY-MM-DD.N.jsonl, rotated at MAX_BYTES and pruned to KEEP_FILES files.
ENV ROUTER_TELEMETRY_ENABLED=true
ENV ROUTER_TELEMETRY_DIR=/var/log/inference-router
ENV ROUTER_TELEMETRY_MAX_BYTES=16777216
ENV ROUTER_TELEMETRY_KEEP_FILES=14

# The service environment must NEVER set ANTHROPIC_API_KEY here or in compose — with gateway
# auth enabled, a managed key is preferred over the incoming OAuth bearer and would silently
# hijack every subscription request onto API-key billing (probe doc, credential-resolution
# caveat; router/AGENTS.md restates this).

EXPOSE 4813

ENTRYPOINT ["node", "bin/next-ai-gateway.js"]
