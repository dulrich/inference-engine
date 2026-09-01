# Verifier report -- `llama/bakeoff/records/qwen4exp-viability-v1`

Generated: 2026-09-01T06:57:54+00:00  
State file: llama/bakeoff/records/qwen4exp-viability-v1/queue.state.json  
Exit code: **1**

## Summary

- total checks: 25
- pass: 18
- fail: 1
- unknown: 2
- informational: 4

## Cells

### `q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-viability-v1/q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 3536007987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 553648128.0, "estimated_delta_bytes": 4089656115.0, "measured_delta_bytes": 5748047872, "ratio": 1.405508852178883} |
| prompt_tokens_tolerance | pass | usage.prompt_tokens within ±10% of target_tokens | {"n": 9, "violations": []} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 9} |

### `q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-viability-v1/q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 4096, "weights_bytes": 3536007987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 138412032.0, "estimated_delta_bytes": 3674420019.0, "measured_delta_bytes": 4841017344, "ratio": 1.3174915548488362} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-viability-v1/q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 13104263987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 553648128.0, "estimated_delta_bytes": 13657912115.0, "measured_delta_bytes": 15332405248, "ratio": 1.122602424067509} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 9, "violations": [{"trial_index": 0, "target_tokens": 13926, "prompt_tokens": 12320}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 9} |

### `q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-viability-v1/q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 4096, "weights_bytes": 13104263987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 138412032.0, "estimated_delta_bytes": 13242676019.0, "measured_delta_bytes": 14425030656, "ratio": 1.089283664064847} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

