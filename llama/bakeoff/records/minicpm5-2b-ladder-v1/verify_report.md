# Verifier report -- `llama/bakeoff/records/minicpm5-2b-ladder-v1`

Generated: 2026-09-08T11:07:59+00:00  
State file: llama/bakeoff/records/minicpm5-2b-ladder-v1/queue.state.json  
Exit code: **1**

## Summary

- total checks: 55
- pass: 37
- fail: 9
- unknown: 0
- informational: 9

## Cells

### `mc2-f16-c128k-f16-ctkf16-ctvf16-faauto-c131072-b1b3c95c`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-f16-c128k-f16-ctkf16-ctvf16-faauto-c131072-b1b3c95c" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 5039006688, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 5637144576.0, "estimated_delta_bytes": 10676151264.0, "measured_delta_bytes": 10379886592, "ratio": 0.9722498618955498} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 122701}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 126881}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 123217}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127961}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127150}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126911}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 122553}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 122629}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "truncated(benign)": 2} |

### `mc2-f16-c32k-f16-ctkf16-ctvf16-faauto-c32768-d8be8e51`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-f16-c32k-f16-ctkf16-ctvf16-faauto-c32768-d8be8e51" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 5039006688, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 1409286144.0, "estimated_delta_bytes": 6448292832.0, "measured_delta_bytes": 6052642816, "ratio": 0.9386426723618125} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30912}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31087}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30644}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30790}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30911}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

### `mc2-f16-c64k-f16-ctkf16-ctvf16-faauto-c65536-452b1a3e`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-f16-c64k-f16-ctkf16-ctvf16-faauto-c65536-452b1a3e" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 65536, "weights_bytes": 5039006688, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 7857578976.0, "measured_delta_bytes": 7495487488, "ratio": 0.9539181866188092} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 61694}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62167}, {"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 61988}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 61293}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 61731}, {"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 62949}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 62891}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62988}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "truncated(benign)": 2} |

### `mc2-q4km-c128k-f16-ctkf16-ctvf16-faauto-c131072-939ca587`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-q4km-c128k-f16-ctkf16-ctvf16-faauto-c131072-939ca587" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 1561318368, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 5637144576.0, "estimated_delta_bytes": 7198462944.0, "measured_delta_bytes": 7287427072, "ratio": 1.012358767238519} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 123356}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 122583}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 126726}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 122560}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 122828}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 23, "truncated(benign)": 4} |

### `mc2-q4km-c32k-f16-ctkf16-ctvf16-faauto-c32768-760418a9`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-q4km-c32k-f16-ctkf16-ctvf16-faauto-c32768-760418a9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 1561318368, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 1409286144.0, "estimated_delta_bytes": 2970604512.0, "measured_delta_bytes": 2911002624, "ratio": 0.9799361080348349} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 31062}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30997}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30962}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30987}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30897}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 26, "truncated(benign)": 1} |

### `mc2-q4km-c64k-f16-ctkf16-ctvf16-faauto-c65536-0c17f00c`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-q4km-c64k-f16-ctkf16-ctvf16-faauto-c65536-0c17f00c" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 65536, "weights_bytes": 1561318368, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 4379890656.0, "measured_delta_bytes": 4400652288, "ratio": 1.0047402169667314} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 61603}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 61308}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 62937}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 24, "truncated(benign)": 3} |

### `mc2-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-92526252`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-92526252" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 2679710688, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 5637144576.0, "estimated_delta_bytes": 8316855264.0, "measured_delta_bytes": 8272457728, "ratio": 0.9946617399737402} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 122562}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 122704}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 122587}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 122742}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 24, "truncated(benign)": 3} |

### `mc2-q8-c32k-f16-ctkf16-ctvf16-faauto-c32768-868011c9`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-q8-c32k-f16-ctkf16-ctvf16-faauto-c32768-868011c9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 2679710688, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 1409286144.0, "estimated_delta_bytes": 4088996832.0, "measured_delta_bytes": 3943919616, "ratio": 0.9645200957690544} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30867}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 30913}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30675}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31063}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 30938}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

### `mc2-q8-c64k-f16-ctkf16-ctvf16-faauto-c65536-846daf65`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/minicpm5-2b-ladder-v1/mc2-q8-c64k-f16-ctkf16-ctvf16-faauto-c65536-846daf65" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 65536, "weights_bytes": 2679710688, "kv_bytes_per_token": 43008.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 5498282976.0, "measured_delta_bytes": 5386919936, "ratio": 0.9797458514801621} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 61344}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 61294}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 61583}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 62868}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62967}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 61386}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "truncated(benign)": 2} |

