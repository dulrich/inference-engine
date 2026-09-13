# Verifier report -- `llama/bakeoff/records/gemma4-26b-a4b-ladder-v1`

Generated: 2026-09-13T11:10:49+00:00  
State file: llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/queue.state.json  
Exit code: **1**

## Summary

- total checks: 37
- pass: 25
- fail: 6
- unknown: 0
- informational: 6

## Cells

### `g26-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-2bd65ba0`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/g26-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-2bd65ba0" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 14439362752, "kv_bytes_per_token": 23680.0, "kv_bytes_at_context": 3103784960.0, "estimated_delta_bytes": 17543147712.0, "measured_delta_bytes": 14326636544, "ratio": 0.8166514230624748} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127292}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127090}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 128399}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 126960}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127175}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 9, "truncated(benign)": 16, "wrong": 1, "degenerate": 1} |

### `g26-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9ee03266`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/g26-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9ee03266" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 14249047104, "kv_bytes_per_token": 23680.0, "kv_bytes_at_context": 3103784960.0, "estimated_delta_bytes": 17352832064.0, "measured_delta_bytes": 12912197632, "ratio": 0.7440974236584418} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 126970}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 13, "truncated(benign)": 10, "degenerate": 4} |

### `g26-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-398b1f3f`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/g26-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-398b1f3f" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 14249047104, "kv_bytes_per_token": 23680.0, "kv_bytes_at_context": 3103784960.0, "estimated_delta_bytes": 17352832064.0, "measured_delta_bytes": 13956411392, "ratio": 0.804272832268908} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126909}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 126940}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127197}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127211}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 128175}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127090}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 13, "truncated(benign)": 13, "degenerate": 1} |

### `g26-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-89c80044`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/g26-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-89c80044" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 262144, "weights_bytes": 14249047104, "kv_bytes_per_token": 22080.0, "kv_bytes_at_context": 5788139520.0, "estimated_delta_bytes": 20037186624.0, "measured_delta_bytes": 13917507584, "ratio": 0.6945839176508934} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 222822, "prompt_tokens": 251673}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"truncated(benign)": 15, "correct": 8, "degenerate": 4} |

### `g26-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-043ba5a9`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/g26-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-043ba5a9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 14249047104, "kv_bytes_per_token": 33280.0, "kv_bytes_at_context": 1090519040.0, "estimated_delta_bytes": 15339566144.0, "measured_delta_bytes": 12905684992, "ratio": 0.8413331166506296} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30966}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"truncated(benign)": 10, "correct": 17} |

### `g26-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-02135724`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/gemma4-26b-a4b-ladder-v1/g26-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-02135724" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 65536, "weights_bytes": 14249047104, "kv_bytes_per_token": 26880.0, "kv_bytes_at_context": 1761607680.0, "estimated_delta_bytes": 16010654784.0, "measured_delta_bytes": 13847535616, "ratio": 0.8648950216476043} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 63181}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 63160}, {"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 63105}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"truncated(benign)": 12, "correct": 12, "degenerate": 2, "wrong": 1} |

