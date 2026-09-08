# Verifier report -- `llama/bakeoff/records/lm-ab-v1`

Generated: 2026-09-08T18:29:07+00:00  
State file: llama/bakeoff/queues/lm-ab-v1.state.json  
Exit code: **0**

## Summary

- total checks: 25
- pass: 17
- fail: 0
- unknown: 4
- informational: 4

## Cells

### `lmab-qwen36-mmap-c128k-ctkf16-ctvf16-faauto-c131072-f18b0d5f`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/lm-ab-v1/lmab-qwen36-mmap-c128k-ctkf16-ctvf16-faauto-c131072-f18b0d5f" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12489101312, "ratio": 1.3422856181439413} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `lmab-qwen36-none-c128k-ctkf16-ctvf16-faauto-c131072-bb080a73`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/lm-ab-v1/lmab-qwen36-none-c128k-ctkf16-ctvf16-faauto-c131072-bb080a73" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12505747456, "ratio": 1.3440746884005246} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `lmab-qwen38-mmap-c128k-ctkq4_0-ctvq4_0-faon-c131072-f9631d23`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/lm-ab-v1/lmab-qwen38-mmap-c128k-ctkq4_0-ctvq4_0-faon-c131072-f9631d23" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14486892544, "ratio": 0.6628471754337094} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `lmab-qwen38-none-c128k-ctkq4_0-ctvq4_0-faon-c131072-1f202b21`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/lm-ab-v1/lmab-qwen38-none-c128k-ctkq4_0-ctvq4_0-faon-c131072-1f202b21" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14485831680, "ratio": 0.6627986356448083} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

