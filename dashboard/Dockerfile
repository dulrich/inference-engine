FROM rust:1.99.0-alpine3.24 AS rust-base
RUN apk add --no-cache musl-dev \
 && cargo install cargo-auditable --locked --version 0.7.7
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates crates
RUN cargo fetch --locked

FROM rust-base AS rust-build
RUN cargo auditable build --release --locked -p bench-dashboard-server --bin inference-dashboard \
 && cp target/release/inference-dashboard /inference-dashboard

FROM oven/bun:1.3 AS web
WORKDIR /app
COPY package.json bun.lock tsconfig.base.json ./
COPY packages/shared/package.json packages/shared/
COPY packages/signalworks/package.json packages/signalworks/
COPY packages/webkit/package.json packages/webkit/
COPY apps/web/package.json apps/web/
RUN bun install --frozen-lockfile
COPY packages packages
COPY apps/web apps/web
RUN bun run --filter '@bench-dashboard/web' build

FROM alpine:3.24
# Source commit baked in so the fleet view can compute commits-behind-master. Declared before the
# apk layer so a new commit re-runs `apk upgrade` instead of serving a stale cached layer.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=${GIT_SHA}
# No ca-certificates: the API makes no outbound connection (plan Decision 8). No /data volume:
# the serving half keeps no database (the research half moved to eval-harness).
RUN apk upgrade --no-cache \
 && addgroup -g 1001 app && adduser -D -u 1001 -G app app
COPY --from=rust-build /inference-dashboard /inference-dashboard
COPY --from=web /app/apps/web/dist /web
ENV PORT=4810 APP_WEB_DIR=/web
EXPOSE 4810
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["/inference-dashboard","--healthcheck"]
CMD ["/inference-dashboard"]
