# Changelog — sidecar

All notable changes to the inference-engine `sidecar` component, released as `sidecar-vX.Y.Z` tags. Release rule: the `publish` skill's `## Releases` section.

## Unreleased

- The docker0 listener is retired with the router container (plan `live-cutover-router-and-sidecar` WP-6): generations inherit only `codex-sidecar.socket` (loopback), `install.sh` no longer renders `codex-sidecar-docker0.socket` and stops, disables and removes it once no older generation that inherited it is still running (a test home deletes the file only), and `status.sh` probes loopback only.
- Generations: `install.sh` deploys live. Each run writes `~/.local/lib/codex-sidecar/generations/<activation>/` (binary + pinned files), starts `codex-sidecar@<activation>.service` on sockets held by `codex-sidecar.socket` (loopback) and `codex-sidecar-docker0.socket` (docker0, `FreeBind`), both tied to the no-op `codex-sidecar-hold.service`; verifies the new activation id on `/healthz`, switches the boot generation, retires the old one and prunes. `--rollback` starts a new generation from the previous one. The legacy `codex-sidecar.service` is migrated once (stop-then-start).
- The binary serves inherited systemd sockets (`LISTEN_FDS`/`LISTEN_PID`) when given them, else binds as before; signals readiness with `READY=1` (`Type=notify`); reports `activation` (`SIDECAR_ACTIVATION`, placeholder `unmanaged`), `build` and `retiring` on `/healthz`.
- SIGUSR1 retires a generation: stop admitting (idle keep-alive connections closed), acknowledge (`retiring` row, `STATUS=retiring`), finish in-flight requests, exit 0; at `SIDECAR_RETIRE_DEADLINE_MS` (default 30 min) cut open streams and exit 3 within 5 s. SIGTERM/SIGINT unchanged.
- `status.sh` reports the accepting and any retiring generation (activation id, build id) and probes the docker0 listener while its socket unit exists.
- Units: `NonBlocking=yes` on the hold unit and the generation template, and `install.sh` starts the sockets before the hold unit. systemd refuses a socket whose hold unit is already active (a first install failed there), and each unit start cleared `O_NONBLOCK` on the listeners a running generation shares, which hung that generation.

## 0.1.0 — 2026-11-10

- Anthropic-Messages front end over `codex app-server`, driving one long-lived child per codex persona over JSON-RPC on stdio.
- Single static Rust binary `codex-sidecar` (`sidecar/rs`), installed as a `systemd --user` service by `install.sh`.
- One codex thread per (session id, agent scope); runtime item-type filter as the security boundary; the sidecar never handles the codex credential.
- Pinned `thread-config.json` and `preamble.txt`, with the thread configuration verified on start; black-box test suite under `test/` runs with `bun test ./test`.
