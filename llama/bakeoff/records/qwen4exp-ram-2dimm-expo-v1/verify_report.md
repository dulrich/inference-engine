# Verifier report -- `llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1`

Generated: 2026-09-07T05:17:41+00:00  
State file: llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/queue.state.json  
Exit code: **1**

## Summary

- total checks: 31
- pass: 22
- fail: 1
- unknown: 3
- informational: 5

## Cells

### `mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12460240896, "ratio": 1.339183799977631} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 3536007987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 553648128.0, "estimated_delta_bytes": 4089656115.0, "measured_delta_bytes": 5747994624, "ratio": 1.4054958320132498} |
| prompt_tokens_tolerance | pass | usage.prompt_tokens within ±10% of target_tokens | {"n": 9, "violations": []} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 9} |

### `q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 4096, "weights_bytes": 3536007987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 138412032.0, "estimated_delta_bytes": 3674420019.0, "measured_delta_bytes": 4907855872, "ratio": 1.3356817801508936} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 13104263987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 553648128.0, "estimated_delta_bytes": 13657912115.0, "measured_delta_bytes": 15332274176, "ratio": 1.1225928272858856} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 9, "violations": [{"trial_index": 0, "target_tokens": 13926, "prompt_tokens": 12320}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 9} |

### `q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen4exp-ram-2dimm-expo-v1/q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 4096, "weights_bytes": 13104263987, "kv_bytes_per_token": 33792.0, "kv_bytes_at_context": 138412032.0, "estimated_delta_bytes": 13242676019.0, "measured_delta_bytes": 14425030656, "ratio": 1.089283664064847} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

