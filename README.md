# Inference Engine

Local model-serving fleet for `<code-root>` consumers. All services are loopback-only (the bench dashboard may bind one trusted-LAN interface):

- **`inference-engine-llama`** (`llama/`) — llama-swap router over llama.cpp/Vulkan on the GPU. Embeddings, reranking, chat, vision. Port **4811** (the transitional 4806 publish was retired 2026-07-27). See `llama/README.md` for the build, the model/alias table, and the smoke test.
- **`inference-engine-onnx`** (`onnx/`) — always-resident CPU sidecar serving the `fastino/gliner2-family` entity/classification models that llama.cpp cannot. Port **4812**.
- ~~`inference-engine-decide`~~ — **retired 2026-10** (lev decision model, port 4815); superseded by the onnx sidecar `POST /v1/classify` (fleet-tools `plans/live-turn-state-classifier.md`).
- **`inference-engine-router`** (`router/`) — resident mixed local/subscription Anthropic-protocol gateway (`@the-next-ai/ai-gateway`, pinned source build + vendored patch series): `claude-local/*` → the llama tier, subscription Claude passthrough, OpenRouter legs, and the codex providers. Port **4813**.
- **`codex-sidecar`** (`sidecar/`) — Anthropic-Messages front end over `codex app-server`, one child per codex persona; a `systemd --user` service (not a container) behind the router. Port **4877**.
- **`inference-engine-tts`** (`tts/tier/`) — GPU TTS tier (Higgs TTS 3 4B render + MOSS-VoiceGenerator design, ROCm). Port **4814**, compose `manual` profile, run only inside `tts/tier/window.sh` render windows.
- **`inference-dashboard`** (`dashboard/`) — bench dashboard over the bake-off results and queue, plus the bake-off control panel. Port **4810** (dev listener 5810).

The bake-off harness and queue control plane live in `llama/bakeoff/`; the Open WebUI model-row sync lives in `scripts/`.

Consumers: **obsidian-crucible** (chat, embed, image, rerank), **news-ingestion** (embeddings + GLiNER2 extraction), **seven-afterlives** (local chat, conformance-validated optional backend), Open WebUI, and Claude Code via the router. The alias contract is append-only; the consumer registry lives in `llama/config.yaml`'s header.

Service blocks live in `fleet-tools/compose.fleet.yml`; ports are registered in `fleet-tools/SERVICES.md` (4810 decade).

Start here: `AGENTS.md` (contract), then the area `AGENTS.md`/`README.md` for whichever tier you are touching.
