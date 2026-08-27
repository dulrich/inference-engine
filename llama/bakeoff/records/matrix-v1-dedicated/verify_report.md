# Verifier report -- `../../runs/bakeoff/matrix-v1-dedicated`

Generated: 2026-08-26T05:21:38+00:00  
State file: ../../runs/bakeoff/matrix-v1-dedicated/queue.state.json  
Exit code: **1**

## Summary

- total checks: 25
- pass: 16
- fail: 5
- unknown: 0
- informational: 4

## Cells

### `mxd-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-e5a6c2af`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "../../runs/bakeoff/matrix-v1-dedicated/mxd-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-e5a6c2af" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6716356800, "kv_bytes_per_token": 21504.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 9534929088.0, "measured_delta_bytes": 11486683136, "ratio": 1.204695182311984} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127116}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 128174}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127091}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127107}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 19, "degenerate": 2, "truncated(benign)": 6} |

### `mxd-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-535f7178`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "../../runs/bakeoff/matrix-v1-dedicated/mxd-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-535f7178" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 3133118624, "kv_bytes_per_token": 16384.0, "kv_bytes_at_context": 2147483648.0, "estimated_delta_bytes": 5280602272.0, "measured_delta_bytes": 5600743424, "ratio": 1.0606258785475142} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 126523}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 126273}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127082}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126255}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "wrong": 2} |

### `mxd-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-6ea21d2e`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "../../runs/bakeoff/matrix-v1-dedicated/mxd-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-6ea21d2e" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | fail | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 22853663008, "kv_bytes_per_token": 83968.0, "kv_bytes_at_context": 11005853696.0, "estimated_delta_bytes": 33859516704.0, "measured_delta_bytes": 12491571200, "ratio": 0.3689234937758077} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 97988}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 97825}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 97603}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 10, "truncated(benign)": 17} |

### `mxd-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-eedc64d6`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "../../runs/bakeoff/matrix-v1-dedicated/mxd-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-eedc64d6" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14542118912, "ratio": 0.6653740556412543} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 98135}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 26, "truncated(benign)": 1} |

