# Verifier report -- `runs/bakeoff/wp5-validation`

Generated: 2026-08-23T23:49:23+00:00  
State file: runs/bakeoff/wp5-validation/queue.state.json  
Exit code: **1**

## Summary

- total checks: 31
- pass: 21
- fail: 4
- unknown: 1
- informational: 5

## Cells

### `gemma-qat-c262144-f16-fitcell-ctkf16-ctvf16-faon-c262144-459ad496`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/wp5-validation/gemma-qat-c262144-f16-fitcell-ctkf16-ctvf16-faon-c262144-459ad496" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 262144, "weights_bytes": 6716356800, "kv_bytes_per_token": 18944.0, "kv_bytes_at_context": 4966055936.0, "estimated_delta_bytes": 11682412736.0, "measured_delta_bytes": 12921274368, "ratio": 1.1060450148437557} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `gemma-qat-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-608a27f9`

- state: `completed`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/wp5-validation/gemma-qat-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-608a27f9" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 6716356800, "kv_bytes_per_token": 36864.0, "kv_bytes_at_context": 1207959552.0, "estimated_delta_bytes": 7924316352.0, "measured_delta_bytes": 8988626944, "ratio": 1.1343094526673434} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 27, "violations": [{"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31265}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 30905}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 31578}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31153}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31352}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31049}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 13, "truncated(benign)": 14} |

### `gemma-qat-c32768-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-74917c4b`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/wp5-validation/gemma-qat-c32768-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-74917c4b" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "q8_0", "ctv": "q8_0", "context": 32768, "weights_bytes": 6716356800, "kv_bytes_per_token": 19584.0, "kv_bytes_at_context": 641728512.0, "estimated_delta_bytes": 7358085312.0, "measured_delta_bytes": 8040497152, "ratio": 1.0927431269228536} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 24, "violations": [{"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 31245}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31335}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31333}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31707}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31392}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 31015}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31900}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31133}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 14, "wrong": 3, "truncated(benign)": 10} |

### `ling-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-8c361420`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/wp5-validation/ling-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-8c361420" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 32768, "weights_bytes": 4823894880, "kv_bytes_per_token": 8448.0, "kv_bytes_at_context": 276824064.0, "estimated_delta_bytes": 5100718944.0, "measured_delta_bytes": 5148565504, "ratio": 1.0093803560880927} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 22, "violations": [{"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 30915}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 32449}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30872}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 32499}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31524}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30903}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31163}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 32339}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31714}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 31981}, {"trial_index": 0, "target_tokens": 27852, "prompt_tokens": 30970}, {"trial_index": 1, "target_tokens": 27852, "prompt_tokens": 31324}, {"trial_index": 2, "target_tokens": 27852, "prompt_tokens": 30903}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"wrong": 5, "correct": 15, "truncated(benign)": 7} |

### `nemotron-c65536-f16-prodfallback-ctkf16-ctvf16-faon-c65536-26b464a7`

- state: `failed` (reason: `needle_failure`)
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "runs/bakeoff/wp5-validation/nemotron-c65536-f16-prodfallback-ctkf16-ctvf16-faon-c65536-26b464a7" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 65536, "weights_bytes": 2837072896, "kv_bytes_per_token": 16384.0, "kv_bytes_at_context": 1073741824.0, "estimated_delta_bytes": 3910814720.0, "measured_delta_bytes": 4041150464, "ratio": 1.0333270055810775} |
| prompt_tokens_tolerance | fail | usage.prompt_tokens within ±10% of target_tokens | {"n": 25, "violations": [{"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62655}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 65050}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 61819}, {"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 61948}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62383}, {"trial_index": 1, "target_tokens": 55705, "prompt_tokens": 62606}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62481}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 63484}, {"trial_index": 2, "target_tokens": 55705, "prompt_tokens": 62293}, {"trial_index": 0, "target_tokens": 55705, "prompt_tokens": 62067}]} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {"correct": 25, "wrong": 2} |

