# Changelog — onnx

All notable changes to the inference-engine `onnx` component, released as `onnx-vX.Y.Z` tags. Release rule: the `publish` skill's `## Releases` section.

## 0.1.0 — 2026-11-10

- CPU HTTP service serving the GLiNER2-family models through onnxruntime, with no torch and no GPU.
- `POST /v1/extract` for entity extraction and `POST /v1/classify` for zero-shot and sequence classification.
- `models.yaml` is an append-only, bind-mounted alias registry; adding a model is a config append plus a weights fetch, with hot models resident and rare ones lazily loaded with idle unload.
- `entrypoint.sh` asserts torch is absent, a CPU provider resolved and the registry mount is real; ships host-run `smoke-extract.sh` and `smoke-classify.sh` and a torch-parity-gated ONNX exporter in `tools/`.
