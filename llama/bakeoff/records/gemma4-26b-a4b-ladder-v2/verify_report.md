# Verifier report -- `llama/bakeoff/records/gemma4-26b-a4b-ladder-v2`

Generated: 2026-09-13T15:15:22+00:00  
State file: llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/queue.state.json  
Exit code: **1**

## Summary

- total checks: 37
- pass: 25
- fail: 6
- unknown: 0
- informational: 6

## Cells

### `g26nt-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-f55449e1`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/g26nt-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-f55449e1" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 14439362752, "kv_bytes_per_token": 23680.0, "kv_bytes_at_context": 3103784960.0, "estimated_delta_bytes": 17543147712.0, "measured_delta_bytes": 14127972352, "ratio": 0.8053271045729197} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127879}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127197}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126963}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127161}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127166}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "wrong": 2} |

### `g26nt-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-8f9356bd`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/g26nt-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-8f9356bd" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 14249047104, "kv_bytes_per_token": 23680.0, "kv_bytes_at_context": 3103784960.0, "estimated_delta_bytes": 17352832064.0, "measured_delta_bytes": 12911357952, "ratio": 0.7440490350151988} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126970}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126989}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 26, "wrong": 1} |

### `g26nt-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8b16b9dc`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/g26nt-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8b16b9dc" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 14249047104, "kv_bytes_per_token": 23680.0, "kv_bytes_at_context": 3103784960.0, "estimated_delta_bytes": 17352832064.0, "measured_delta_bytes": 13958164480, "ratio": 0.8043738583143127} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127173}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 126966}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 22, "wrong": 5} |

### `g26nt-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-c14c8df3`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/g26nt-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-c14c8df3" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 262144, "weights_bytes": 14249047104, "kv_bytes_per_token": 22080.0, "kv_bytes_at_context": 5788139520.0, "estimated_delta_bytes": 20037186624.0, "measured_delta_bytes": 13934637056, "ratio": 0.6954388017382375} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 222822, "prompt_tokens": 245233}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 23, "wrong": 4} |

### `g26nt-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-887b1d4e`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/g26nt-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-887b1d4e" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 14249047104, "kv_bytes_per_token": 33280.0, "kv_bytes_at_context": 1090519040.0, "estimated_delta_bytes": 15339566144.0, "measured_delta_bytes": 12904808448, "ratio": 0.841275973965382} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30897}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

### `g26nt-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-e2862bd9`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v2/g26nt-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-e2862bd9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 65536, "weights_bytes": 14249047104, "kv_bytes_per_token": 26880.0, "kv_bytes_at_context": 1761607680.0, "estimated_delta_bytes": 16010654784.0, "measured_delta_bytes": 13846372352, "ratio": 0.8648223660307234} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 63033}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 62911}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

