# Verifier report -- `llama/bakeoff/records/matrix-v1-shared`

Generated: 2026-09-07T02:24:48+00:00  
State file: llama/bakeoff/records/matrix-v1-shared/queue.state.json  
Exit code: **1**

## Summary

- total checks: 102
- pass: 52
- fail: 11
- unknown: 21
- informational: 18

## Cells

### `mx-gemma-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9afffa7f`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-gemma-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9afffa7f" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6716356800, "kv_bytes_per_token": 21504.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 9534929088.0, "measured_delta_bytes": 11036233728, "ratio": 1.157453152104659} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126982}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127307}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127041}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127182}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127015}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127162}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 17, "truncated(benign)": 9, "degenerate": 1} |

### `mx-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-1cd55be3`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-1cd55be3" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6716356800, "kv_bytes_per_token": 21504.0, "kv_bytes_at_context": 2818572288.0, "estimated_delta_bytes": 9534929088.0, "measured_delta_bytes": 11567915008, "ratio": 1.2132145820107436} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127133}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127052}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127238}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 127230}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 19, "truncated(benign)": 7, "degenerate": 1} |

### `mx-gemma-qat-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-bf0431b6`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-gemma-qat-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-df739795`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-gemma-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-1afee66e`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-ling-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-2291772d`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-ling-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-2291772d" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 8408187968, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 1107296256.0, "estimated_delta_bytes": 9515484224.0, "measured_delta_bytes": 9377910784, "ratio": 0.9855421503770653} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 129747}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 129798}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 129034}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"truncated(benign)": 1, "correct": 25, "wrong": 1} |

### `mx-ling-ud-q4-c128k-f16-ctkf16-ctvf16-faauto-c131072-de5d379e`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-ling-ud-q4-c128k-f16-ctkf16-ctvf16-faauto-c131072-de5d379e" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 5340611328, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 1107296256.0, "estimated_delta_bytes": 6447907584.0, "measured_delta_bytes": 6383009792, "ratio": 0.9899350616995444} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 129774}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 129479}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 26, "truncated(benign)": 1} |

### `mx-ling-ud-q4-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c18f80b4`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-ling-ud-q6-c128k-f16-ctkf16-ctvf16-faauto-c131072-7a1d7924`

- state: `failed` (reason: `degenerate`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-ling-ud-q6-c128k-f16-ctkf16-ctvf16-faauto-c131072-7a1d7924" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 7274546528, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 1107296256.0, "estimated_delta_bytes": 8381842784.0, "measured_delta_bytes": 8244330496, "ratio": 0.9835940268096539} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127098}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 126542}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127167}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 127153}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 128377}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 26, "degenerate": 1} |

### `mx-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-74439058`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-74439058" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 3133118624, "kv_bytes_per_token": 16384.0, "kv_bytes_at_context": 2147483648.0, "estimated_delta_bytes": 5280602272.0, "measured_delta_bytes": 5683748864, "ratio": 1.0763448128138062} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 126852}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 126324}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 127127}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "wrong": 2} |

### `mx-nemotron-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-6a866129`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-nemotron-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-6a866129" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 3133118624, "kv_bytes_per_token": 4608.0, "kv_bytes_at_context": 603979776.0, "estimated_delta_bytes": 3737098400.0, "measured_delta_bytes": 4058910720, "ratio": 1.0861128837281886} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 124858}, {"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 123699}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "wrong": 2} |

### `mx-nemotron-c256k-f16-ctkf16-ctvf16-faauto-c262144-4e6d7906`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-qwen36-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-41baab12`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8d32c2d0`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8d32c2d0" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 131072, "weights_bytes": 6620000000, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 2684354560.0, "estimated_delta_bytes": 9304354560.0, "measured_delta_bytes": 12779188224, "ratio": 1.3734631608879704} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 98027}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 99426}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 14, "truncated(benign)": 13} |

### `mx-qwen36-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-5d38307a`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `mx-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-fee2cb77`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-fee2cb77" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 12040883104, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 21855554464.0, "measured_delta_bytes": 14575562752, "ratio": 0.6669042771716707} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 111411, "prompt_tokens": 98445}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 99761}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "truncated(benign)": 2} |

### `mx-qwen38-q3kxl-c64k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c65536-cd3baabe`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-qwen38-q3kxl-c64k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c65536-cd3baabe" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 65536, "weights_bytes": 13146393504, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 4907335680.0, "estimated_delta_bytes": 18053729184.0, "measured_delta_bytes": 14337040384, "ratio": 0.7941317961447051} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 49219}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 49261}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 49329}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 26, "truncated(benign)": 1} |

### `mx-qwen38-xxs-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c2573bb7`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/matrix-v1-shared/mx-qwen38-xxs-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c2573bb7" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 131072, "weights_bytes": 10934860704, "kv_bytes_per_token": 74880.0, "kv_bytes_at_context": 9814671360.0, "estimated_delta_bytes": 20749532064.0, "measured_delta_bytes": 13309734912, "ratio": 0.6414474731742076} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98321}, {"trial_index": 1, "target_tokens": 111411, "prompt_tokens": 98375}, {"trial_index": 2, "target_tokens": 111411, "prompt_tokens": 97658}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 27} |

