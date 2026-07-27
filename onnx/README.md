# inference-engine-onnx — the GLiNER2-family CPU sidecar

An always-resident HTTP service on **127.0.0.1:4812** that serves the
[`fastino/gliner2-family`](https://huggingface.co/collections/fastino/gliner2-family) models for
**entity extraction + zero-shot classification**, on CPU, through onnxruntime. It is the CPU half
of this repo's two-tier design: everything llama.cpp cannot serve — encoder + span-head models,
classifier heads — lives here, while `inference-engine-llama` keeps the GPU.

Launch consumer: news-ingestion's `enrich` (crisis24 feature materialisation). The PII and
guardrails members of the family have upcoming eval-harness / news-ingestion sprints, and adding
one is a **config append plus a weights fetch — no code**.

## Layout

| File | What it is |
| --- | --- |
| `models.yaml` | The alias registry. Bind-mounted read-only; **append-only** API surface with a consumer registry in its header. |
| `Dockerfile` | `python:3.12-slim` + onnxruntime/numpy/tokenizers/PyYAML. No torch, no GPU. |
| `entrypoint.sh` | Start-up assertions: torch is absent, onnxruntime resolved a CPU provider, the registry mount is real. |
| `server/` | The service. `app.py` (routes), `registry.py` (aliases + residency), `runtime.py` (the torch-free GLiNER2 inference path). |
| `tools/export_gliner2.py` | Host-side ONNX exporter **with a torch-parity gate**. Never shipped in the image. |
| `smoke-extract.sh` | Host-run smoke test. Shape assertions, body probes, one negative check. |
| `compose-block.yml` | The proposed `compose.fleet.yml` service block. A snippet — not wired. |

## API

### `POST /v1/extract`

```jsonc
{
  "model": "gliner2-base",                    // registry alias; unknown -> 404, never a default
  "texts": ["Security forces in Nairobi ..."],
  "schema": {                                 // request-supplied: the label set is the CLIENT's
    "entities": {                             // {label: description} (descriptions improve accuracy)
      "person": "Names of people or named individuals",
      "location": "Cities, regions, countries, or named places"
    },
    "classifications": [
      {"name": "severity", "labels": ["Info", "Caution", "Warning", "Critical"]}
    ]
  },
  "threshold": 0.5                            // optional, default 0.5
}
```

Response — `results[i]` is the **gliner2 library's native output shape**, verbatim, so
news-ingestion's `features.py` reads it unchanged:

```jsonc
{
  "model": "gliner2-base",
  "results": [
    {
      "entities": {
        "person": [],
        "location": [{"text": "Nairobi", "confidence": 0.9992522597312927}]
      },
      "severity": {"label": "Critical", "confidence": 0.8317713737487793}
    }
  ]
}
```

Notes:

- Classifications are **single-label by default** (softmax, best label). Pass `"multi_label":
  true` on a classification for the sigmoid/threshold behaviour; the result is then a *list* of
  `{label, confidence}`, again matching the library.
- `relations` and JSON `structures` are **not served** and return `400 bad_schema` rather than a
  plausible-looking wrong answer — see [AGENTS.md](AGENTS.md) for why.
- Schemas are compiled and cached per `(alias, schema-hash)`; label **order** is part of the
  prompt, so two orderings are two schemas.

### `GET /health`

```jsonc
{"ok": true, "models": {"gliner2-base": {"loaded": true, "load_seconds": 1.51, ...}}}
```

`ok` is true only when every `preload: true` alias is resident. The compose healthcheck reads
this field, not the status code.

### `GET /v1/models`

The registry as served, one entry per alias, with the export manifest's provenance
(`counting_layer`, `encoder_model`, `exported_at`).

## Adding a model

```bash
# 1. Export on a host with torch (a venv is fine; the image has no torch and never will).
python onnx/tools/export_gliner2.py \
  --model fastino/gliner2-multi-v1 \
  --out   /srv/models/fastino/gliner2-multi-v1-onnx
#    ... exports 5 graphs + tokenizer + manifest, then asserts its own ONNX output matches the
#    torch library on a fixture set. Non-zero exit = do not ship it.

# 2. Append the alias to models.yaml (path is the container-side /models path).
# 3. Restart the container (configs are mounted, not baked) and raise mem_limit if needed.
docker compose up -d --force-recreate inference-engine-onnx
onnx/smoke-extract.sh --models "gliner2-base gliner2-pii gliner2-multi"
```

Verified exportable and parity-checked on this host (2026-07-26): `gliner2-base-v1`,
`gliner2-privacy-filter-PII-multi`, `gliguard-LLMGuardrails-300M` — covering both encoders in the
family (DeBERTa-v3-base / mDeBERTa-v3-base) and both counting layers (`count_lstm_v2` /
`count_lstm`), which is what makes the family-wide claim more than an assumption.

## Measured on this host (2026-07-26)

| | |
| --- | --- |
| Cold load, warm page cache | `gliner2-base` 1.5 s, `gliner2-pii` 2.0 s (preloaded before the listener opens) |
| Peak RSS, both models resident, 8-way concurrent batch | 2.86 GiB VmHWM (`mem_limit: 4g`) |
| Throughput, crisis24-length paragraphs, `gliner2-base` | 9.1–12.0 texts/s in a single request stream (32-text and 16-text batches respectively), 25.7 texts/s at 8-way concurrency |
| Export artifacts on disk | 805 MB (`gliner2-base`), 1.2 GB (`gliner2-pii`) |

Torch/CPU reference for the same model and schema, measured the same day: 2.5 s load, ~2.07 GB
RSS, ~10 texts/s. The point of the ONNX path is not raw speed — it is that the runtime image
carries no torch and the service is multi-model and always-resident.

## Gates

Per the repo `AGENTS.md`: `bash -n` on the shell scripts, executables `0755`, `file` + `LC_ALL=C
grep -caP '\0'` clean on everything touched, and `smoke-extract.sh` green against the running
container after any change that affects it. Model or runtime changes additionally re-run
`tools/export_gliner2.py --verify-only` for every configured alias.
