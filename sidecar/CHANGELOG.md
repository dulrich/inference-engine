# Changelog — sidecar

All notable changes to the inference-engine `sidecar` component, released as `sidecar-vX.Y.Z` tags. Release rule: the `publish` skill's `## Releases` section.

## 0.1.0 — 2026-11-10

- Anthropic-Messages front end over `codex app-server`, driving one long-lived child per codex persona over JSON-RPC on stdio.
- Single static Rust binary `codex-sidecar` (`sidecar/rs`), installed as a `systemd --user` service by `install.sh`.
- One codex thread per (session id, agent scope); runtime item-type filter as the security boundary; the sidecar never handles the codex credential.
- Pinned `thread-config.json` and `preamble.txt`, with the thread configuration verified on start; black-box test suite under `test/` runs with `bun test ./test`.
