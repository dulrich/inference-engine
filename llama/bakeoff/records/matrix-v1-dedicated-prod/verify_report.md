# Verifier report -- `llama/bakeoff/records/matrix-v1-dedicated-prod`

Generated: 2026-09-06T13:15:16+00:00  
State file: llama/bakeoff/records/matrix-v1-dedicated-prod/queue.state.json  
Exit code: **1**

## Summary

- total checks: 85
- pass: 55
- fail: 12
- unknown: 4
- informational: 14

## Cells

### `mxdp-gemma-qat-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-64f3cf26`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-gemma-qat-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-64f3cf26" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6716356800, "kv_bytes_per_token": 21504.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 9534929088.0, "measured_delta_bytes": 11484078080, "ratio": 1.204421970421685} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127091}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127110}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127731}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127164}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"truncated(benign)": 16, "correct": 8, "degenerate": 3} |

### `mxdp-ling-q8-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9e8a463f`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-ling-q8-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9e8a463f" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 8408187968, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 1107296256.0, "estimated_delta_bytes": 9515484224.0, "measured_delta_bytes": 9391644672, "ratio": 0.9869854703045325} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127829}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 129636}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127178}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 129219}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 24, "truncated(benign)": 3} |

### `mxdp-ling-ud-q4-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9f2929eb`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-ling-ud-q4-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9f2929eb" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 5340611328, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 1107296256.0, "estimated_delta_bytes": 6447907584.0, "measured_delta_bytes": 6382776320, "ratio": 0.9898988527438547} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127198}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127238}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 129742}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "truncated(benign)": 2} |

### `mxdp-ling-ud-q6-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-85d4f581`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-ling-ud-q6-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-85d4f581" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 7274546528, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 1107296256.0, "estimated_delta_bytes": 8381842784.0, "measured_delta_bytes": 8258244608, "ratio": 0.9852540569913891} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127221}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 128698}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 129708}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127221}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "truncated(benign)": 2} |

### `mxdp-qwen36-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-f330211a`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-f330211a" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | fail | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 22853663008, "kv_bytes_per_token": 83968.0, "kv_bytes_at_context": 11005853696.0, "estimated_delta_bytes": 33859516704.0, "measured_delta_bytes": 12491837440, "ratio": 0.36893135685319084} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 98196}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"truncated(benign)": 17, "correct": 10} |

### `mxdp-qwen36-c128k-f16-mtp-prod-nothink-ctkf16-ctvf16-faauto-c131072-2033109b`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-mtp-prod-nothink-ctkf16-ctvf16-faauto-c131072-2033109b" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | fail | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 22853663008, "kv_bytes_per_token": 83968.0, "kv_bytes_at_context": 11005853696.0, "estimated_delta_bytes": 33859516704.0, "measured_delta_bytes": 12490809344, "ratio": 0.3689009932774497} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 98511}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

### `mxdp-qwen36-c128k-f16-mtp-prod-rb128-ctkf16-ctvf16-faauto-c131072-feb547bf`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-mtp-prod-rb128-ctkf16-ctvf16-faauto-c131072-feb547bf" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12475510784, "ratio": 1.3408249549767803} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98899}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 98726}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 99109}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 99442}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 24, "wrong": 3} |

### `mxdp-qwen36-c128k-f16-timing-base-nothink-ctkf16-ctvf16-faauto-c131072-60601124`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-timing-base-nothink-ctkf16-ctvf16-faauto-c131072-60601124" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 10156896256, "ratio": 1.091628246806622} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mxdp-qwen36-c128k-f16-timing-base-think-ctkf16-ctvf16-faauto-c131072-447e38a2`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-timing-base-think-ctkf16-ctvf16-faauto-c131072-447e38a2" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 10156830720, "ratio": 1.0916212032229349} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mxdp-qwen36-c128k-f16-timing-mtp-nothink-ctkf16-ctvf16-faauto-c131072-27ef8a46`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-timing-mtp-nothink-ctkf16-ctvf16-faauto-c131072-27ef8a46" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12460371968, "ratio": 1.3391978871450059} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12460240896, "ratio": 1.339183799977631} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-ctkq4_0-ctvq4_0-faon-c131072-0d8108e6`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-ctkq4_0-ctvq4_0-faon-c131072-0d8108e6" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14527901696, "ratio": 0.664723547505054} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 98487}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 97878}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 98062}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98476}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 98216}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 23, "truncated(benign)": 3, "wrong": 1} |

### `mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-nothink-ctkq4_0-ctvq4_0-faon-c131072-41f57002`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-nothink-ctkq4_0-ctvq4_0-faon-c131072-41f57002" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14544216064, "ratio": 0.6654700107451824} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 97541}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 99400}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 97860}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 99682}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98180}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 99317}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

### `mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-rb128-ctkq4_0-ctvq4_0-faon-c131072-62da3ac9`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-rb128-ctkq4_0-ctvq4_0-faon-c131072-62da3ac9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14542118912, "ratio": 0.6653740556412543} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 98126}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98637}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 99284}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 98181}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98479}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 98065}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 24, "wrong": 3} |

