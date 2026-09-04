# Verifier report -- `llama/bakeoff/records/qwen36-resident-v1`

Generated: 2026-09-04T22:04:14+00:00  
State file: **NOT FOUND**  
Exit code: **0**

## Queue state: UNKNOWN (no state file found)

## Summary

- total checks: 13
- pass: 8
- fail: 0
- unknown: 3
- informational: 2

## Cells

### `q36r-ud-iq2-m-ctkf16-ctvf16-faauto-c16384-d847b684`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen36-resident-v1/q36r-ud-iq2-m-ctkf16-ctvf16-faauto-c16384-d847b684" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 11522702304, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 335544320.0, "estimated_delta_bytes": 11858246624.0, "measured_delta_bytes": 11915481088, "ratio": 1.0048265536900003} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

### `q36r-ud-iq3-xxs-ctkf16-ctvf16-faauto-c16384-7b8941de`

- state: `None`
- has_records: `True`

| check | verdict | expected | observed |
| --- | --- | --- | --- |
| records_present | pass | records subdirectory present | "llama/bakeoff/records/qwen36-resident-v1/q36r-ud-iq3-xxs-ctkf16-ctvf16-faauto-c16384-7b8941de" |
| schema_conformant | pass | schema-v2-conformant records (4-file split + required config/sample fields) | "conformant" |
| vram_arithmetic | pass | estimated weights+KV delta within [0.4x, 3.0x] of measured vram.loaded_bytes - vram.baseline_bytes | {"ctk": "f16", "ctv": "f16", "context": 16384, "weights_bytes": 13211155424, "kv_bytes_per_token": 20480.0, "kv_bytes_at_context": 335544320.0, "estimated_delta_bytes": 13546699744.0, "measured_delta_bytes": 13536358400, "ratio": 0.999236615249808} |
| prompt_tokens_tolerance | unknown | usage.prompt_tokens within ±10% of target_tokens | {"n": 0, "reason": "no trials with both target_tokens and prompt_tokens recorded"} |
| wilson_min_trials | pass | >= 10 trials whenever a wilson_summary verdict is claimed | {"wilson_summary": null, "reason": "no wilson_summary claimed for this cell"} |
| classifier_distribution | informational | None | {} |

