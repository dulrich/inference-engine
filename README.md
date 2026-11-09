# Inference Engine

Local model-serving fleet for sibling-repo consumers. All services are loopback-only (the bench dashboard may bind one trusted-LAN interface):

- **`inference-engine-llama`** (`llama/`) — llama-swap router over llama.cpp/Vulkan on the GPU. Embeddings, reranking, chat, vision. Port **4811** (the transitional 4806 publish was retired 2026-07-27). See `llama/README.md` for the build, the model/alias table, and the smoke test.
- **`inference-engine-onnx`** (`onnx/`) — always-resident CPU sidecar serving the `fastino/gliner2-family` entity/classification models that llama.cpp cannot. Port **4812**.
- ~~`inference-engine-decide`~~ — **retired 2026-10** (lev decision model, port 4815); superseded by the onnx sidecar `POST /v1/classify` (live-turn-state classifier).
- **`inference-engine-router`** (`router/`) — resident mixed local/subscription Anthropic-protocol gateway (the owned Rust gateway, static `inference-router` binary): `claude-local/*` → the llama tier, subscription Claude passthrough, OpenRouter legs, and the codex providers. Port **4813**.
- **`codex-sidecar`** (`sidecar/`) — Anthropic-Messages front end over `codex app-server`, one child per codex persona; a `systemd --user` service (not a container) behind the router. Port **4877**.
- **`inference-engine-tts`** — GPU TTS tier (Higgs TTS 3 4B render + MOSS-VoiceGenerator design, ROCm). Port **4814**, compose `manual` profile, run only inside its render windows.
- **`inference-console`** — Inference Console (Serving/Router views over `serving-facts-v1`). Port **4810** (dev listener 5810).

The bake-off harness, queue control plane, and research dashboard (:4816) live in eval-harness `inference_research/` (`eval-harness/inference_research/harness`); the Open WebUI model-row sync lives in `scripts/`.

Consumers: **obsidian-crucible** (chat, embed, image, rerank), **news-ingestion** (embeddings + GLiNER2 extraction), **seven-afterlives** (local chat, conformance-validated optional backend), Open WebUI, and Claude Code via the router. The alias contract is append-only; the consumer registry lives in `llama/config.yaml`'s header.

Service blocks live in the fleet compose; ports are registered in the fleet's service registry (4810 decade).

Start with the area `README.md` for whichever tier you are touching: `llama/README.md`, `onnx/README.md`, `sidecar/README.md`.
