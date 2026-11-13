# Changelog — llama

All notable changes to the inference-engine `llama` component, released as `llama-vX.Y.Z` tags. Release rule: the `publish` skill's `## Releases` section.

## Unreleased

- Security: the build stage moves to golang 1.27.4 (CVE-2026-97054) for the llama-swap build; llama.cpp and llama-swap versions are unchanged.

## 0.1.0 — 2026-11-10

- llama.cpp (Vulkan build) plus llama-swap in one container, built from a multi-stage Dockerfile with both upstream versions baked into the image tag.
- llama-swap spawns, swaps and unloads per-model `llama-server` children in-container from a bind-mounted `config.yaml` (models, aliases, ttl, groups), serving embedding, reranking, chat and vision models.
- `entrypoint.sh` refuses to start on a software rasteriser or a glibc-skewed image; the image is dual-use and runs whatever command it is given.
- Ships `smoke-inference.sh`, `deep-health-check.sh` and `tools/hf-gguf-fetch.sh`, a resumable, size-verified GGUF fetcher.
