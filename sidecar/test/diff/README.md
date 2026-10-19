# sidecar/test/diff — differential wire-behaviour runner

Runs two codex-sidecar targets through the identical scenario corpus (`cases.mjs`) and compares their normalized transcripts. Plan: `plans/codex-sidecar-rust-port.md` WP-1. Loopback only, ephemeral ports (`SIDECAR_PORT=0`), temp dirs, the fake app-server (`test/fake-app-server.mjs`) — nothing metered, nothing touches `:4877`.

## Run (from `sidecar/`)

```
node test/diff/run.mjs --a "node codex-sidecar.mjs" --b "node codex-sidecar.mjs"            # self-check
node test/diff/run.mjs --a "node codex-sidecar.mjs" --b rs/target/release/codex-sidecar       # parity
node test/diff/run.mjs --a … --b … --case "tool"                                              # name substring filter
```

Each target is a whitespace-split command line (relative paths against `sidecar/`), started through `withSidecar({ target })` in `test/helpers.mjs` — the same spawn path as `SIDECAR_TARGET`. Output is TAP (`ok N - <case>` / `not ok N` with the first differing JSON path and both values); exit 1 on any difference or target start failure.

## Transcript

- Per step: HTTP status, the response headers `content-type`, `cache-control`, `retry-after` (all others are transport framing), and the JSON body, raw text, or SSE event list (event name + parsed data; `ping` events dropped — their cadence is timing).
- Fake app-server trace per persona, grouped by JSON-RPC method (responses grouped as `<kind>:response`), order kept within a group.
- JSONL log rows (read after the target has shut down, 300 ms after the last step) as a sorted multiset of normalized rows — compared by value and multiplicity, never by field-name sets.
- Outbox day-file rows (cases with `outbox: true`) as raw lines, byte-for-byte except the `ts` value.

## Masking (the complete list — anything not here is compared exactly)

- Keys, anywhere: `ts` (log/trace/outbox timestamps), `createdAt`, `updatedAt` (fake thread times), `reqId` (random per request), `port` (ephemeral), `pid`, `elapsedMs` (duration), `startedAtMs` (fake item start time).
- `retry-after` header and `retryAfter` log field: an integer becomes `<int>` (seconds relative to now; a non-integer is still compared).
- String patterns: `/tmp/codex-sidecar-test-<rand>` → `<TMP>`, `/tmp/codex-sidecar-diff-ob-<rand>` → `<OBTMP>`, `127.0.0.1:<digits>` → `127.0.0.1:<PORT>`, a whole-string `msg_<24 hex>` → `msg_<ID>` (random message id).
- Time-derived scenario input (the rate-limit `resetsAt`) is built once per case and fed to both targets, so it is never masked.
