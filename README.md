# Inference Engine

Local model-serving fleet for `<code-root>` consumers. Two loopback-only services:

- **`inference-engine-llama`** (`llama/`) — llama-swap router over llama.cpp/Vulkan on the GPU.
  Embeddings, reranking, chat, vision. Canonical port **4811**; **4806** is a permanent
  compatibility alias (Crucible vaults store it). See `llama/README.md` for the build, the
  model/alias table, and the smoke test.
- **`inference-engine-onnx`** (`onnx/`) — always-resident CPU sidecar serving the
  `fastino/gliner2-family` entity/classification models that llama.cpp cannot. Port **4812**.

Consumers at launch: **obsidian-crucible** (chat, embed, image, rerank), **news-ingestion**
(embeddings + GLiNER2 extraction), **seven-afterlives** (local chat, conformance-validated
optional backend). The alias contract is append-only; the consumer registry lives in
`llama/config.yaml`'s header.

Service blocks live in `fleet-tools/compose.fleet.yml`; ports are registered in
`fleet-tools/SERVICES.md` (4810 decade; 4810/5810 reserved for a management dashboard).

Start here: `AGENTS.md` (contract), then the area `AGENTS.md`/`README.md` for whichever tier
you are touching.
