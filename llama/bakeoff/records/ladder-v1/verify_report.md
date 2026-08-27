# Verifier report -- `runs/bakeoff/ladder-v1`

Generated: 2026-08-24T21:41:10+00:00  
State file: runs/bakeoff/ladder-v1/queue.state.json  
Exit code: **1**

## Summary

- total checks: 269
- pass: 64
- fail: 2
- unknown: 150
- informational: 53

## Cells

### `gemma-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-bb2eef21`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-b2a4c43c`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-7f234f36`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c256k-f16-capa-ctkf16-ctvf16-faon-c262144-6df1b1f7`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c256k-f16-prod-ctkf16-ctvf16-faon-c262144-a7a1c3d5`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-e76b86cd`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-e4fe5b21`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/ladder-v1/gemma-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-e4fe5b21" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 6716356800, "kv_bytes_per_token": 36864.0, "kv_bytes_at_context": 1207959552.0, "estimated_delta_bytes": 7924316352.0, "measured_delta_bytes": 8964435968, "ratio": 1.1312567002373002} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30893}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30850}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 23, "truncated(benign)": 4} |

### `gemma-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-ec13812f`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/ladder-v1/gemma-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-ec13812f" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 6716356800, "kv_bytes_per_token": 36864.0, "kv_bytes_at_context": 1207959552.0, "estimated_delta_bytes": 7924316352.0, "measured_delta_bytes": 8984145920, "ratio": 1.1337439749906644} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 31259}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31170}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30958}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30728}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 22, "truncated(benign)": 5} |

### `gemma-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-3c64912f`

- state: `pending`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/ladder-v1/gemma-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-3c64912f" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q4_0", "ctv": "q4_0", "context": 32768, "weights_bytes": 6716356800, "kv_bytes_per_token": 10368.0, "kv_bytes_at_context": 339738624.0, "estimated_delta_bytes": 7056095424.0, "measured_delta_bytes": 7494148096, "ratio": 1.0620814552068054} |
| prompt_tokens_tolerance | pass | usage.prompt_tokens within ±10% of target_tokens | {"n": 24, "violations": []} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 24, "wrong": 3} |

### `gemma-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-84485fc0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-bba3c2c3`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-b70d3e46`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-b6c81454`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-f60f531f`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-0776bda2`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-b1de72a2`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-1a3f57f4`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-e12c9fd0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-49b92124`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-3f61ac3d`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-1b5231ea`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-63621011`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-bd7384d0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `ling-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-0f80680c`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-627adeb0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-d80d1b92`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-f640c235`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c256k-f16-capa-ctkf16-ctvf16-faon-c262144-34ffbcc8`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-c8eb9ef0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c256k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c262144-912bb1f4`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-6ccca239`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-b0b368f9`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-f3b891ea`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c512k-f16-capa-ctkf16-ctvf16-faon-c524288-26602b59`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c512k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c524288-993577ed`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c512k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c524288-ff2e2d4c`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-c0c6ed03`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-6a755c5e`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `nemotron-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-84231024`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-d648829d`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-439280de`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-d25b7512`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-3b7aca95`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-a4523784`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c256k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c262144-f72351c0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-de58b7e6`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-500122c1`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-f44a970b`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-4e03948f`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-62c043a9`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-6bcf1af0`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-12812fed`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `qwen-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-5e4b4847`

- state: `pending`
- has_records: `False`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | unknown | records optional for state 'pending' | "no records subdirectory found" |
| vram_arithmetic | unknown | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | "config.json missing/unparseable" |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

