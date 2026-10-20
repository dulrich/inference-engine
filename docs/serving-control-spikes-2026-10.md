# Serving-control spikes — 2026-10 (plan `serving-control-redesign` WP-0)

Measured 2026-10-19 on a scratch llama-swap (the live image's binary, `v243 (4c50064)`, copied out with `docker cp`; listening on 127.0.0.1:4899/4898; stub Python HTTP upstreams instead of llama-server; `ttl: 20`; no GPU; the live `inference-engine-llama` container untouched). Stubs answer `/health`, `/slots` and `/v1/chat/completions` and record their pid, so a reload or eviction shows up as a new pid.

## WP-0a — passivity, eviction, mixed groups, `-watch-config`

Group layout under test: `excl` (`chat-a`, swap+exclusive), `ne1` (`chat-b`) and `ne2` (`chat-c`) (swap, non-exclusive), and `retrieval` (`retr`; swap/exclusive/persistent all false, as in the live `retrieval` group).

| Probe | Result |
|---|---|
| `GET /running`, `/v1/models` and `/health` every 4 s, with `chat-b` loaded at ttl 20 | **Passive.** `chat-b` unloaded on schedule at t+20 s. |
| `GET /upstream/chat-b/slots` every 4 s | **Refreshes ttl.** `chat-b` still resident after 40 s. |
| `GET /running` with `retr` loaded | Passive: `retr` stays. |
| `GET /upstream/chat-a/slots` with `retr` loaded and `chat-a` **not** loaded | **Spawns `chat-a` and evicts `retr`.** `/upstream` is a full proxy request. |
| `retr` + `chat-b` (ne1), then `chat-c` (ne2) | **All three resident.** Two non-exclusive groups never evict each other. |
| then `chat-a` (exclusive) | Everything else evicted; `chat-a` alone. |
| then `chat-b` (ne1) with `chat-a` resident | **`chat-a` stays.** A non-exclusive load does not evict an exclusive group. |
| then `retr` | **`chat-a` + `chat-b` + `retr` all resident.** Today's live layout has the same hole: a retrieval request beside the 27B (13.72 GiB) goes over budget. |
| `-watch-config`: edit only `chat-c`'s ttl while `chat-b` and `chat-c` are resident and a `chat-b` request is in flight (8 s) | **Reload stops every resident model** (log: `reloading configuration` → both stopped → `configuration reloaded`), and **the in-flight request fails with 502**. `chat-c` comes back with a new pid. |

Raw driver output: `drive.py` / `results.txt` in the scratch dir (not kept).

### `matrix` (found in the v243 binary; not documented in this repo)

v243 accepts a top-level `matrix:` with `vars:` (short name → model id) and `sets:` (name → `&`/`|` expression of the models allowed to be resident together). It refuses a config that has both `groups` and `matrix` (`config cannot use both 'groups' and 'matrix'`). There are also `evict_costs` keys; they were not exercised.

Probe: `sets: {big: "a", small_b: "b & r", small_c: "c & r"}`.

| Sequence | Resident after |
|---|---|
| `retr`, `chat-b` | `chat-b`, `retr` |
| `chat-c` | `chat-c`, `retr` (`chat-b` evicted; `retr` kept) |
| `chat-b` | `chat-b`, `retr` |
| `chat-a` | `chat-a` alone |
| `retr` | `retr` alone (`chat-a` evicted) |
| `chat-b` | `chat-b`, `retr` |

So `matrix` expresses the budget directly: a small chat model sits beside retrieval, chat models never sit beside each other, and the 27B is always alone. No `groups` layout can do that: an exclusive group is not evicted by a later non-exclusive load, and two non-exclusive groups always coexist.

### Consequences for the plan

- **WP-6:** `/running` (and `/v1/models`, `/health`) are passive, so the collector reads `/running`. `/upstream/*` must never be polled.
- **WP-5:** the groups-based design ("serving groups whose `exclusive` flag follows the measured fit") cannot meet the plan's own budget invariant. The mechanism becomes a `matrix` that replaces every `groups:` entry, research ones included (a research alias is a solo set; that gives the same quiet-GPU eviction `harness_transport.py:417` relies on today).
- **`-watch-config` is not enabled.** A reload is as disruptive as a container restart and drops in-flight requests, so it buys nothing. llama config changes stay restart-applied.

## WP-0b — OpenRouter preset id on both legs

One request per leg, sent straight to the upstream URL each leg uses (`router/gateway.config.json`: `openrouter` = `openai_chat_completions` at `https://openrouter.ai/api/v1`, `or-anthropic` = `anthropic` at `https://openrouter.ai/api` + `/v1/messages`), with the router's key and `"model": "@preset/gpt-6-luna"` (the user's preset). Today the gateway itself would 400 an id outside `models[]`, so the probe bypasses it on purpose.

| Leg | Result |
|---|---|
| chat completions | 200; `model: "openai/gpt-6-luna"`, provider Azure, content `pong`, cost $0.0000038 |
| messages | 200; `type: message`, `model: "openai/gpt-6-luna"`, `stop_reason: end_turn`, content `pong`, cost $0.0000038 |

**Both legs accept a preset id.** The response's `model` is the resolved slug, not the preset id, so telemetry and any response-model check must expect the requested and returned ids to differ.

## WP-0c — picker reload in a running session

Not measurable by an agent: it needs a human to open `/model` in a session that was already running before `modelPicker.options` was rewritten. Acceptance test 3 already uses a new session, so nothing in the plan depends on this. Left as an optional user observation during WP-4's smoke.
