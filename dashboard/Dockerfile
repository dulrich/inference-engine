# Single-process deploy: Bun serves the API + the built Svelte SPA on one port
# (model_scoreboard exemplar — plan bench-results-db-and-dashboard WP-3). The DB is
# derived state on the /data volume; the source of truth (llama/bakeoff/records/,
# llama/config.yaml) is bind-mounted read-only by compose.fleet.yml.
FROM oven/bun:1.3 AS build
WORKDIR /app
COPY package.json bun.lock tsconfig.base.json ./
COPY packages/shared/package.json packages/shared/
COPY packages/signalworks/package.json packages/signalworks/
COPY apps/api/package.json apps/api/
COPY apps/web/package.json apps/web/
RUN bun install --frozen-lockfile
COPY . .
RUN bun run --filter '@bench-dashboard/web' build

# ---- prod-deps: production-only dependency tree (no svelte-check/typescript/oxlint/biome/etc.) ----
FROM oven/bun:1.3 AS prod-deps
WORKDIR /app
COPY package.json bun.lock tsconfig.base.json ./
COPY packages/shared/package.json packages/shared/
COPY packages/signalworks/package.json packages/signalworks/
COPY apps/api/package.json apps/api/
COPY apps/web/package.json apps/web/
RUN bun install --frozen-lockfile --production

FROM oven/bun:1.3-slim
# Source commit baked in so the fleet view can compute how many commits behind
# master this running image is (see command_center fleet/status engine).
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=${GIT_SHA}
WORKDIR /app
# Base images lag debian point releases; this upgrade layer picks up published
# security fixes at build time (pairs with compose `pull: true`, so a rebuild
# always starts from the freshest base digest too).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY --from=prod-deps /app /app
COPY --from=build /app/apps/api apps/api
COPY --from=build /app/packages packages
COPY --from=build /app/apps/web/dist apps/web/dist
ENV PORT=4810
ENV APP_DB_PATH=/data/bench.db
EXPOSE 4810
VOLUME ["/data"]
RUN groupadd -g 1001 app && useradd -m -u 1001 -g app app \
  # /data must exist app-owned BEFORE the VOLUME is first materialized: a named volume
  # initializes from this directory's content AND ownership, and a root-owned mount
  # point leaves bun:sqlite unable to create /data/bench.db (SQLITE_CANTOPEN, observed
  # live at first bring-up).
  && mkdir -p /data && chown app:app /data
USER app
CMD ["bun", "run", "apps/api/src/index.ts"]
