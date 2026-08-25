# router/Dockerfile — @the-next-ai/ai-gateway (https://github.com/The-NeXT-AI/ai-gateway),
# built from source at a pinned commit with one vendored patch. Never `npm install
# @the-next-ai/ai-gateway` and never a moving branch/tag — the pin below is a commit SHA.
#
# See router/AGENTS.md for the patch rationale and the never-set-ANTHROPIC_API_KEY constraint,
# and router/patches/0001-omit-x-api-key-for-passthrough-bearer.patch for the change itself.
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

# The repo's own package-lock.json — measured green 2026-08-25 (145 packages, <1s).
RUN npm ci

# build.js -> esbuild bundle to dist/index.js — measured green 2026-08-25 (dist byte-identical
# in size to the published npm 1.0.18 artifact, before this patch's small addition).
RUN npm run build

# Build-stage gate (repo Gates rule, "the relevant... run green"): the patched credential-header
# behavior is locked by common.test.ts (14/14, unchanged by 0002); the local-leg idle-gap
# keepalive added by 0002 is locked by streaming-conversion.test.ts (9 new cases). Baseline at
# 782cd51 with both patches applied is 23/23, 0 failing. A non-zero exit here fails the image build.
RUN npx vitest run src/adapters/builtins/common.test.ts src/gateway/streaming-conversion.test.ts

# esbuild bundles everything except fastify/undici/ws (build.js's `external` list) into
# dist/index.js, so pruning devDependencies is the only trim the runtime stage needs.
RUN npm prune --omit=dev

FROM node:24-slim AS runtime

LABEL org.opencontainers.image.title="inference-engine-router" \
      org.opencontainers.image.description="@the-next-ai/ai-gateway 1.0.18, source build at 782cd51, x-api-key passthrough-bearer patch"

WORKDIR /app

COPY --from=build /src/dist ./dist
COPY --from=build /src/bin ./bin
COPY --from=build /src/node_modules ./node_modules
COPY --from=build /src/package.json ./package.json

# gateway.config.json is bind-mounted here at runtime (compose, WP-2) — never baked into the
# image, matching this repo's config-is-never-baked convention (llama/config.yaml, onnx/models.yaml).
ENV GATEWAY_CONFIG_PATH=/app/gateway.config.json
ENV NODE_ENV=production

# The service environment must NEVER set ANTHROPIC_API_KEY here or in compose — with gateway
# auth enabled, a managed key is preferred over the incoming OAuth bearer and would silently
# hijack every subscription request onto API-key billing (probe doc, credential-resolution
# caveat; router/AGENTS.md restates this).

EXPOSE 4813

ENTRYPOINT ["node", "bin/next-ai-gateway.js"]
