# Changelog — router

All notable changes to the inference-engine `router` component, released as `router-vX.Y.Z` tags. Release rule: the `publish` skill's `## Releases` section.

## Unreleased

- The `inference-engine-router` container is retired (plan `live-cutover-router-and-sidecar` WP-6): the router runs only as host generations (`router/install.sh`, `--rollback`); the image build stays a crate gate, and the differential harness's deployed target is the installed generation binary.
- A cut Anthropic-shape SSE stream ends with one terminal `event: error` frame (`api_error`) instead of stopping bare, for an upstream body that fails after headers and for a stream still open at the retire deadline (the deadline cancels each open stream inside the existing 5 s exit bound). A partial frame is terminated with `\n\n` first, a stream that already relayed `message_stop` gets nothing, and SIGTERM cuts, non-SSE bodies and the OpenAI route are unchanged (plan `live-cutover-router-and-sidecar` WP-5).
- Host generations (plan `live-cutover-router-and-sidecar` WP-2): the binary serves on sockets inherited from systemd (`LISTEN_FDS`), declares `READY=1` once serving, and retires on SIGUSR1 (stop accepting, finish in-flight requests, exit 0; exit 3 at `INFERENCE_ROUTER_RETIRE_DEADLINE_MS`, default 30 min). SIGTERM/SIGINT unchanged. `/health` adds `activation` (`INFERENCE_ROUTER_ACTIVATION`, else `unmanaged`) and `build`.
- `INFERENCE_ROUTER_GATEWAY_KEY` is the client key when `AUTH_STATIC_API_KEYS` is unset (the container sets both; unchanged).
- Telemetry: rotation and pruning are overlap-safe for two writers on one directory (state re-read from the directory per batch).
- New `router/install.sh` and units (`inference-router@.service.in`, `inference-router.socket`, `inference-router-hold.service`, opt-in `inference-router-wg.socket.in`): host install with rendered topology, `--preflight`, `--rollback`, `--rust-bin`. The container path is unchanged.
- The serving dispatcher's catalog validator uses the installed generation (`current`) when one exists, else the container.
- Units: `NonBlocking=yes` on the hold unit and the generation template, and `install.sh` starts the sockets before the hold unit. systemd refuses a socket whose hold unit is already active (a first install failed there), and each unit start cleared `O_NONBLOCK` on the listeners a running generation shares, which hung that generation.

- Security: the gateway lockfile moves to rustls-webpki 0.103.16 and webpki-root-certs 1.0.12.

## 0.1.0 — 2026-11-10

- Owned Rust gateway (`router/gateway/`, static `inference-router` binary) serving the Anthropic protocol on a single loopback port.
- Routes `claude-local/*` to the local GPU tier, passes subscription Claude through as a pure OAuth passthrough, and tees to OpenRouter on both the OpenAI-compatible and native Anthropic shapes.
- Routes codex provider ids to the codex sidecar.
- Ships `smoke-router.sh`, `smoke-tool-search.sh` and a model-picker lineup generator, with a Rust test suite under `router/gateway/` and a `router/test` suite.
