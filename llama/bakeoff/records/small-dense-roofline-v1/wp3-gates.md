# WP-3 gate evidence — small-dense-decode-roofline-spike (captured 2026-09-04, re-run after the window)

## watchdog
```
$ systemctl is-active gpu-hang-watchdog.service
active
$ journalctl -k --since "2026-09-04 05:00" | grep -c -E "MES might be in unrecoverable state|Failed to evict process queues"
0
```

## dry-run fit
```
$ python3 llama/bakeoff/batch.py run --queue llama/bakeoff/queues/small-dense-roofline-v1.json --dry-run --vram-profile desktop-shared | grep -c "\"classification\": \"fits\""
12
```

## image identity
```
$ docker inspect inference-engine-llama bakeoff-scratch --format "{{.Name}} {{.Config.Image}}"
/inference-engine-llama crucible-llamacpp-vulkan:b10534-swap243
/bakeoff-scratch crucible-llamacpp-vulkan:b10534-swap243
$ docker exec bakeoff-scratch llama-server --version
version: 0.1.2-dev (build 1, commit 2b56210)
```

## queue state (state file lives beside the queue, not in the run dir — why verify_run reports it UNKNOWN)
```
queue_state: completed cells: {'completed': 12}
```

## verify_run.py (re-run)
```
{
  "fail": 0,
  "informational": 12,
  "pass": 48,
  "total": 73,
  "unknown": 13
}
exit=0
```

## smoke (re-run)
```
[smoke] PASS: /health returned 200 with body 'OK'
[smoke] PASS: /v1/models lists both aliases (bge-m3, bge-reranker-v2)
[smoke] PASS: /v1/embeddings returned a numeric vector of length 1024
[smoke] PASS: /v1/embeddings (bge-small) returned a numeric vector of length 384
[smoke] PASS: /rerank returned 3 results with a unique in-range index and a finite relevance_score each
[smoke] PASS: /api/v0/models failed fast with 404 in 0s
[smoke] PASS: /v1/chat/completions (gemma-4-12b-qat) returned non-empty content
[smoke] PASS: /v1/chat/completions (nemotron-4b) returned non-empty content
[smoke] SKIPPED: ttl-unload check (pass --wait-ttl to run it — it sleeps ~30 minutes for the retrieval group's 1800s ttl)
[smoke] all checks passed
exit=0
```

## dashboard ingest + export
```
$ docker exec inference-dashboard bun run apps/api/src/ingest/cli.ts | tail -1
{"scanned":50,"ingested":36,"skippedV1":0,"skippedInvalid":2,"legacyTimingIngested":12,"rebuilt":false}
$ curl -s http://192.0.2.100:4810/api/export/scoreboard-capture | … sdr-* experimentIds
12 of 48
sdr-minicpm5-1b-f16-ctkf16-ctvf16-faauto-c16384-3c124316
sdr-minicpm5-1b-q4km-ctkf16-ctvf16-faauto-c16384-cf888fc6
sdr-minicpm5-1b-q8-ctkf16-ctvf16-faauto-c16384-4d2ae2db
sdr-nemotron-4b-udq4xl-ctl-ctkf16-ctvf16-faauto-c16384-3c7a0785
sdr-qwen35-08b-bf16-ctkf16-ctvf16-faauto-c16384-3bf7fca9
sdr-qwen35-08b-q40-ctkf16-ctvf16-faauto-c16384-5783a120
sdr-qwen35-08b-q6k-ctkf16-ctvf16-faauto-c16384-107c38d7
sdr-qwen35-08b-q8-ctkf16-ctvf16-faauto-c16384-45c7f57b
sdr-qwen35-4b-bf16-ctkf16-ctvf16-faauto-c16384-08af6261
sdr-qwen35-4b-q40-ctkf16-ctvf16-faauto-c16384-68f790d1
sdr-qwen35-4b-q6k-ctkf16-ctvf16-faauto-c16384-6f308247
sdr-qwen35-4b-q8-ctkf16-ctvf16-faauto-c16384-6fb6a377
timing_geometry: {'prompt_tokens_target': 8000, 'max_tokens': 256, 'prompt_source': 'fixture-filler', 'instruction_pinned': True}
```

## file / NUL gate (touched text files)
```
docs/small-dense-decode-roofline-spike.md	file=Unicode text	nul-grep-exit=1
plans/small-dense-decode-roofline-spike.md	file=Unicode text	nul-grep-exit=1
INITIATIVE.md	file=Unicode text	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-analysis.py	file=Python script	nul-grep-exit=1
runs/dispatch/small-dense-roofline-v1-window.log	file=ASCII text	nul-grep-exit=1
runs/dispatch/small-dense-roofline-v1-window-a2.log	file=ASCII text	nul-grep-exit=1
runs/dispatch/small-dense-decode-roofline-spike-wp-3-review-prompt.txt	file=Unicode text	nul-grep-exit=1
runs/dispatch/small-dense-decode-roofline-spike-wp-3-review-plan-audit-lastmsg-a1-1788499101.txt	file=Unicode text	nul-grep-exit=1
llama/bakeoff/queues/small-dense-roofline-v1.state.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/verify_report.md	file=Unicode text	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/verify_report.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1-router-control/control.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1-router-control/control-run.log	file=CSV ASCII text	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/probe-minicpm5/server.log	file=ASCII text	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/probe-minicpm5/response.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-f16-ctkf16-ctvf16-faauto-c16384-3c124316/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-q4km-ctkf16-ctvf16-faauto-c16384-cf888fc6/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-q8-ctkf16-ctvf16-faauto-c16384-4d2ae2db/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-nemotron-4b-udq4xl-ctl-ctkf16-ctvf16-faauto-c16384-3c7a0785/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-bf16-ctkf16-ctvf16-faauto-c16384-3bf7fca9/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-q40-ctkf16-ctvf16-faauto-c16384-5783a120/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-q6k-ctkf16-ctvf16-faauto-c16384-107c38d7/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-08b-q8-ctkf16-ctvf16-faauto-c16384-45c7f57b/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-bf16-ctkf16-ctvf16-faauto-c16384-08af6261/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-q40-ctkf16-ctvf16-faauto-c16384-68f790d1/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-q6k-ctkf16-ctvf16-faauto-c16384-6f308247/config.json	file=JSON text data	nul-grep-exit=1
llama/bakeoff/records/small-dense-roofline-v1/sdr-qwen35-4b-q8-ctkf16-ctvf16-faauto-c16384-6fb6a377/config.json	file=JSON text data	nul-grep-exit=1
```
