# inference-router: one static musl Rust binary (plans/router-rust-gateway.md WP-5).
# Build context = router/gateway/ (the compose build context for the router service).
# TLS: reqwest's `rustls` feature verifies via rustls-platform-verifier, which on Linux reads
# the OS trust store, so the scratch image carries the alpine CA bundle.
FROM rust:1.99.1-alpine3.24 AS rust-base
RUN apk add --no-cache musl-dev ca-certificates \
 && cargo install cargo-auditable --locked --version 0.7.7
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates crates
COPY clippy.toml ./
COPY build.rs ./
COPY src src
COPY tests tests
RUN cargo fetch --locked

FROM rust-base AS rust-build
# Forwarded to build.rs (FLEET_BUILD_ID); empty when not passed, then build.rs computes or falls back to unknown.
ARG GIT_SHA
ENV GIT_SHA=$GIT_SHA
RUN cargo auditable build --release --locked --bin inference-router \
 && cp target/release/inference-router /inference-router

FROM scratch
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=${GIT_SHA}
COPY --from=rust-base /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=rust-build /inference-router /inference-router
ENV GATEWAY_CONFIG_PATH=/app/gateway.config.json GATEWAY_CATALOG_PATH=/app/catalog/catalog.json SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
USER 1001:1001
EXPOSE 4813
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 CMD ["/inference-router","healthcheck"]
ENTRYPOINT ["/inference-router"]
