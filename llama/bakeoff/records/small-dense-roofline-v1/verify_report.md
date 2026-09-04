# Verifier report -- `llama/bakeoff/records/small-dense-roofline-v1`

Generated: 2026-09-04T05:24:18+00:00  
State file: **NOT FOUND**  
Exit code: **0**

## Queue state: UNKNOWN (no state file found)

## Summary

- total checks: 73
- pass: 48
- fail: 0
- unknown: 13
- informational: 12

## Cells

### `sdr-minicpm5-1b-f16-ctkf16-ctvf16-faauto-c16384-3c124316`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-f16-ctkf16-ctvf16-faauto-c16384-3c124316" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 2166551936, "kv_bytes_per_token": 24576.0, "kv_bytes_at_context": 402653184.0, "estimated_delta_bytes": 2569205120.0, "measured_delta_bytes": 2258464768, "ratio": 0.8790519489545466} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-minicpm5-1b-q4km-ctkf16-ctvf16-faauto-c16384-cf888fc6`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-q4km-ctkf16-ctvf16-faauto-c16384-cf888fc6" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 688065920, "kv_bytes_per_token": 24576.0, "kv_bytes_at_context": 402653184.0, "estimated_delta_bytes": 1090719104.0, "measured_delta_bytes": 1068310528, "ratio": 0.9794552273653034} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-minicpm5-1b-q8-ctkf16-ctvf16-faauto-c16384-4d2ae2db`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-q8-ctkf16-ctvf16-faauto-c16384-4d2ae2db" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 1153529216, "kv_bytes_per_token": 24576.0, "kv_bytes_at_context": 402653184.0, "estimated_delta_bytes": 1556182400.0, "measured_delta_bytes": 1431416832, "ratio": 0.9198258713117434} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-nemotron-4b-udq4xl-ctl-ctkf16-ctvf16-faauto-c16384-3c7a0785`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-nemotron-4b-udq4xl-ctl-ctkf16-ctvf16-faauto-c16384-3c7a0785" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 3133118624, "kv_bytes_per_token": 16384.0, "kv_bytes_at_context": 268435456.0, "estimated_delta_bytes": 3401554080.0, "measured_delta_bytes": 3568062464, "ratio": 1.0489506796258257} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-08b-bf16-ctkf16-ctvf16-faauto-c16384-3bf7fca9`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-bf16-ctkf16-ctvf16-faauto-c16384-3bf7fca9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 1516744736, "kv_bytes_per_token": 49152.0, "kv_bytes_at_context": 805306368.0, "estimated_delta_bytes": 2322051104.0, "measured_delta_bytes": 1902493696, "ratio": 0.8193160317284731} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-08b-q40-ctkf16-ctvf16-faauto-c16384-5783a120`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-q40-ctkf16-ctvf16-faauto-c16384-5783a120" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 507154688, "kv_bytes_per_token": 49152.0, "kv_bytes_at_context": 805306368.0, "estimated_delta_bytes": 1312461056.0, "measured_delta_bytes": 888942592, "ratio": 0.677309690779884} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-08b-q6k-ctkf16-ctvf16-faauto-c16384-107c38d7`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-q6k-ctkf16-ctvf16-faauto-c16384-107c38d7" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 639029504, "kv_bytes_per_token": 49152.0, "kv_bytes_at_context": 805306368.0, "estimated_delta_bytes": 1444335872.0, "measured_delta_bytes": 1020813312, "ratio": 0.7067700330577956} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-08b-q8-ctkf16-ctvf16-faauto-c16384-45c7f57b`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-q8-ctkf16-ctvf16-faauto-c16384-45c7f57b" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 811843840, "kv_bytes_per_token": 49152.0, "kv_bytes_at_context": 805306368.0, "estimated_delta_bytes": 1617150208.0, "measured_delta_bytes": 1195196416, "ratio": 0.7390756963004391} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-4b-bf16-ctkf16-ctvf16-faauto-c16384-08af6261`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-bf16-ctkf16-ctvf16-faauto-c16384-08af6261" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 8424393632, "kv_bytes_per_token": 131072.0, "kv_bytes_at_context": 2147483648.0, "estimated_delta_bytes": 10571877280.0, "measured_delta_bytes": 9322651648, "ratio": 0.8818350233441227} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-4b-q40-ctkf16-ctvf16-faauto-c16384-68f790d1`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-q40-ctkf16-ctvf16-faauto-c16384-68f790d1" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 2583221408, "kv_bytes_per_token": 131072.0, "kv_bytes_at_context": 2147483648.0, "estimated_delta_bytes": 4730705056.0, "measured_delta_bytes": 3471720448, "ratio": 0.7338695621272737} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-4b-q6k-ctkf16-ctvf16-faauto-c16384-6f308247`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-q6k-ctkf16-ctvf16-faauto-c16384-6f308247" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 3525956768, "kv_bytes_per_token": 131072.0, "kv_bytes_at_context": 2147483648.0, "estimated_delta_bytes": 5673440416.0, "measured_delta_bytes": 4414353408, "ratio": 0.7780734588400408} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `sdr-qwen35-4b-q8-ctkf16-ctvf16-faauto-c16384-6fb6a377`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-q8-ctkf16-ctvf16-faauto-c16384-6fb6a377" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 4482403488, "kv_bytes_per_token": 131072.0, "kv_bytes_at_context": 2147483648.0, "estimated_delta_bytes": 6629887136.0, "measured_delta_bytes": 5368324096, "ratio": 0.8097157592397362} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

