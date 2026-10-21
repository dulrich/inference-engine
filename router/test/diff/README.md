# router/test/diff — differential contract harness

Runs one gateway (an image or a host binary of the Rust gateway, `router/gateway/`) against a recording stub upstream and asserts, per case, on what the stub received and what the client got back. Plan: `plans/router-rust-gateway.md` WP-1. Nothing here touches the live router (`:4813`) or any fleet port, and nothing leaves the host: all credentials are fake (`test-gw-key`, `test-or-key`, `test-codex-key`, a fake OAuth bearer).

## Run

```
node router/test/diff/run.mjs --target bin:router/gateway/target/release/inference-router
bun router/test/diff/run.mjs --target image:fleet-tools-inference-engine-router:latest
```

Cross-version parity is checked against the deployed image `image:fleet-tools-inference-engine-router:latest`: run the harness against both the fresh `bin:` build and that image, and both must be green.

- `--target image:<tag>` creates a `docker network create --internal` network `rrdiff-net-*`, runs the stub as a container of `--stub-image <tag>` (`node /diff/stub.mjs`, this directory mounted read-only; default `node:24-slim`, since the scratch gateway image has no `node`), and runs the gateway container on that network with the generated config mounted at `/app/gateway.config.json:ro` and the generated catalog directory at `/app/catalog:ro` (the image default `GATEWAY_CATALOG_PATH=/app/catalog/catalog.json`). The runner reaches both by container IP from the host (the internal bridge is host-routable; no published port). The gateway runs as the invoking uid so it can write the telemetry dir. Everything is named `rrdiff-*` and removed on exit, including on failure or SIGINT/SIGTERM.
- `--target bin:<path>` spawns the stub on `127.0.0.1:<random>` and the binary with `HOST=127.0.0.1 PORT=<random> GATEWAY_CONFIG_PATH=<generated> GATEWAY_CATALOG_PATH=<generated>` plus the same fake env as image mode (the production env names from `fleet-tools/fleet/compose.fleet.yml`).
- `--case <glob>` filters ids, `--slow` adds `slow` cases (none exist today), `--config-source <path>` replaces the default source config `router/gateway.config.json`, `--catalog-source <path>` the default source catalog `router/catalog/catalog.json`. `RRDIFF_GW_LOG=<file>` saves the gateway's log.
- Output is one TAP line per case; exit is non-zero on any failure. The summary prints the generated config path and the per-run telemetry dir (`ROUTER_TELEMETRY_DIR`), kept for later validation (WP-4).

## Tags

Every `parity` and `changed` case runs by default; the tag only records provenance.

- `parity`: behaviour unchanged from the retired Node gateway.
- `changed`: the Decision 2/3 behaviour the Rust gateway deliberately changed (no protocol conversion, native local leg, transforms, keepalive).
- `slow`: only with `--slow`.

## Config generation

The test config is derived at run time and never committed. Every provider `baseurl` in the source config becomes `<stub>/p/<provider-name><original path suffix>`, for example `https://openrouter.ai/api/v1` → `<stub>/p/openrouter/api/v1`. This is a textual rewrite, so every other byte is kept. That is why an additive config key can be checked by re-running this harness on the edited config. `fixture-providers.json` appends two providers the production config lacks: `rrdiff-nocred` (an uncredentialed anthropic-type provider) and `rrdiff-decide-nocred` (an uncredentialed systemone provider). Neither changes the bare-model default target list, which is derived from provider *types* (first config of each type). The model catalog is generated the same way: the source catalog copied into `<tmp>/catalog/catalog.json` plus one entry per fixture provider (its `models[]`, with its `consumers` per model).

## Stub

`stub.mjs` (Node stdlib) records method, path+query, headers and body for every `/p/<provider>/…` request, and answers from the script the runner installs before each case (`POST /_script`, keyed by provider; an array is consumed in order). It can answer with JSON, chunked SSE with per-chunk delays, a delay before headers, any status with a non-JSON or empty body, or a socket destroy (connect-style error). `GET /_log` returns the log, including `clientClosedAtMs` when the gateway hung up before the response finished.
