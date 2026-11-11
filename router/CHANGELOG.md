# Changelog — router

All notable changes to the inference-engine `router` component, released as `router-vX.Y.Z` tags. Release rule: the `publish` skill's `## Releases` section.

## 0.1.0 — 2026-11-10

- Owned Rust gateway (`router/gateway/`, static `inference-router` binary) serving the Anthropic protocol on a single loopback port.
- Routes `claude-local/*` to the local GPU tier, passes subscription Claude through as a pure OAuth passthrough, and tees to OpenRouter on both the OpenAI-compatible and native Anthropic shapes.
- Routes codex provider ids to the codex sidecar.
- Ships `smoke-router.sh`, `smoke-tool-search.sh` and a model-picker lineup generator, with a Rust test suite under `router/gateway/` and a `router/test` suite.
