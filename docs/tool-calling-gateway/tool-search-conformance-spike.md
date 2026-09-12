# Deferred-tool-search conformance through the resident router — measured wire spike

Plan: `plans/router-observability-and-tool-search-conformance.md` WP-5. Measurement spike, run 2026-09-12 against the **resident** router at `http://127.0.0.1:4813` (observed only — never restarted or reconfigured) with Claude Code 2.1.270 in headless `claude -p` mode. Every number below was measured on this run through `POST /v1/messages/count_tokens` on the leg it describes; nothing is quoted from `fleet-tools/references/tool-search-routing.md`. Capture files are cited per claim and live in `/tmp/wp5-capture/` (throwaway, kept for orchestrator review).

## Verdict

| leg | routed id | verdict |
| --- | --- | --- |
| Claude passthrough | `claude-haiku-4-5` | **passthrough-clean** — all four invariants green |
| Local (OpenAI-conversion) | `claude-local/qwen38-27b-iq3s` | **passthrough-clean** — all four invariants green |
| Codex sidecar | `codex-b/gpt-5.6-luna` | **semantic-downgrade** — invariants 1 green, 2/3/4 fail, silently |

The reference's predicted order of difficulty is confirmed exactly: Claude→Claude is passthrough, and **the Codex App Server leg is the hard case**. The local conversion leg — named as the second suspect — is in fact clean.

The codex defect is **not** a token-accounting problem and **not** an eager-loading fallback. The deferred catalogue is silently *lost*: the model is handed a working `ToolSearch` tool but is never told a single deferred tool exists, so it has nothing to search for and reports the tool as unavailable. No error, no warning, no retry — the turn completes "successfully" with a wrong answer.

## How Claude Code actually represents deferred tools

Measured, not assumed. This is the protocol WP-6 must assert against.

With `ENABLE_TOOL_SEARCH=true`, request 1 carries **12** tools (`/tmp/wp5-capture/local-on/001.req.json`, `passthrough-on/001.req.json`, `codex-on/001.req.json` — byte-identical tool shape on all three legs):

- a fully-schema'd **`ToolSearch`** tool — the discovery tool;
- a single **`DeferredToolPlaceholder`** entry carrying `"defer_loading": true`, described as *"Reserved placeholder that keeps deferred tool loading active; never call this tool."* — this is the **only** `defer_loading` marker in the array;
- the ordinary always-on tools (`Agent`, `Bash`, `Edit`, `ListAgents`, `Read`, `ReportFindings`, `ScheduleWakeup`, `Skill`, `Workflow`, `Write`).

The deferred tools themselves are **absent from `tools` entirely**. Their *names* are advertised to the model in prose, inside a `role:"system"` message interleaved in `messages[]`:

```
The following deferred tools are now available via ToolSearch. Their schemas are NOT loaded —
calling them directly will fail with InputValidationError. Use ToolSearch with query
"select:<name>[,<name>...]" to load tool schemas before calling them:
CronCreate / CronDelete / CronList / DesignSync / EnterWorktree / ExitWorktree /
NotebookEdit / SendMessage / TaskOutput / TaskStop
```

Verified location, `codex-on/001.req.json`: the block is present in `messages[1]` whose `role` is `"system"`, and **absent from top-level `body.system`**. That placement is the whole defect — see "Named defect".

The discovery round-trip, captured on the clean legs (`local-on/003.req.json`, `passthrough-on/002.req.json`):

1. assistant emits `tool_use` `ToolSearch` with `{"query":"select:NotebookEdit","max_results":1}`;
2. the harness returns a `tool_result` whose content is the tool-reference block `[{"type":"tool_reference","tool_name":"NotebookEdit"}]`;
3. the **next** request's `tools` array has grown 12 → 13 with `NotebookEdit` present, carrying its full `input_schema` **and still carrying `defer_loading: true`** (identical on both clean legs — the flag is retained, not cleared);
4. the concrete tool executes.

With `ENABLE_TOOL_SEARCH=false` the array is eagerly materialised, contains **no** `ToolSearch` and **no** `defer_loading` on any entry.

## The 6-cell matrix

Token columns are `POST /v1/messages/count_tokens` **through the router on that cell's own leg**, replaying the cell's captured request-1 `system` + `tools` with a one-word user message. `system+tools` is the headline figure; `tools only` isolates the tool block.

| cell | routed model | tools in req 1 | `ToolSearch` | `defer_loading` | system+tools | tools only | discovery call | concrete tool ran | capture |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| passthrough-off | `claude-haiku-4-5` | 25 (all eager) | absent | none | **29,159** | 22,958 | n/a | yes | `passthrough-off/001.req.json` |
| passthrough-on | `claude-haiku-4-5` | 12 | present | `DeferredToolPlaceholder` | **17,745** | 11,544 | **yes** | **yes** (12→13) | `passthrough-on/001-002.req.json` |
| local-off | `claude-local/qwen38-27b-iq3s` | 20 (all eager) | absent | none | 15,394 | 14,036 | n/a | yes | `local-off/001.req.json` |
| local-on | `claude-local/qwen38-27b-iq3s` | 12 | present | `DeferredToolPlaceholder` | 8,238 | 6,880 | **yes** | **yes** (12→13) | `local-on/001-003.req.json` |
| codex-off | `codex-b/gpt-5.6-luna` | 20 (all eager) | absent | none | 14,549 | 13,070 | n/a | yes | `codex-off/001.req.json` |
| codex-on | `codex-b/gpt-5.6-luna` | 12 | present | `DeferredToolPlaceholder` | 7,875 | 6,396 | **no** | **no** | `codex-on/001-002.req.json` |

`system only` is 6,208 (passthrough), 1,411 (local), ~1,480 (codex) — subtract to reconstruct the tool block.

**Cross-leg token comparison is invalid** and WP-6 must not attempt it: Claude Code's own eager catalogue differs per leg (25 tools hosted vs 20 on both local and codex — the hosted profile additionally carries `RemoteTrigger`, `TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`), and each leg's `count_tokens` uses a different tokenizer (hosted = real Anthropic; local = llama.cpp; codex = the sidecar's chars/4 estimate). Only **within-leg on/off pairs** are meaningful.

### Headline reproduction

The reference's 29.2k figure reproduces on the passthrough leg to within 0.2%:

- **off: 29,159** system+tools tokens (`passthrough-off/001.req.json`)
- **on: 17,745** system+tools tokens (`passthrough-on/001.req.json`)
- saving: **11,414 tokens (39.1%)** per fresh context; tool block alone 22,958 → 11,544 (**2.0×**)

The off-cell matches the quoted 29.2k almost exactly, which confirms the metric is request-1 `system`+`tools`. The on-cell measures **17,745, not the quoted 9.5k**. The gap is accounted for by fixture profile, not by router behaviour: this fleet's always-on `Agent` and `Skill` tool descriptions enumerate every agent type and every installed skill, and neither is deferrable. **WP-6's threshold is the measured number, not the quoted one.** The same ratio holds on the other two legs (local 15,394 → 8,238, 1.87×; codex 14,549 → 7,875, 1.85×), so the ~2× tool-block reduction is a stable, leg-independent property of the mechanism.

## Named defect — the codex sidecar drops the deferred-tool advertisement

**`sidecar/codex-sidecar.mjs:216-217`**

```js
// Claude Code interleaves role:"system" entries into messages[]; they are never a new turn.
export const conversational = msgs => (Array.isArray(msgs) ? msgs : [])
  .filter(m => m && (m.role === "user" || m.role === "assistant"));
```

The filter discards **every** `role:"system"` entry in `messages[]`. Tracing its one consumer (`:964` `const msgs = conversational(allMsgs)`), the dropped content is never re-injected: `baseInstructions` at `:1064` is built from `PREAMBLE` plus **top-level** `systemTextOf(body)` only, and `nSystemRole` at `:1129` is a bare count for the log line. The sole surviving use of those messages is `extractCwd` (`:231`), whose own comment states the situation outright — *"The cwd is parsed before the system-role filter drops the environment message."*

Because Claude Code puts the deferred-tool name list **only** in that message (verified above), the codex leg forwards a perfectly good `ToolSearch` tool to a model that has no idea what to search for.

**Evidence chain, all pinned:**

1. Claude Code sent the correct deferred shape — `codex-on/001.req.json`: 12 tools, `ToolSearch` present, `DeferredToolPlaceholder` deferred, `NotebookEdit` absent. So the client is not at fault.
2. The sidecar received the full array — sidecar log `~/.local/state/codex-sidecar/logs/codex-sidecar-2026-09-12.jsonl`, record `key: a60beb04-…`, `"nTools": 12`, `"nSystemRole": 1`. **Nothing was dropped from `tools`** — `toDynamicTools` is not the culprit, contrary to the plan's stated suspicion.
3. The model never issued a discovery call — same log record, `"toolCalls": ["Bash"]`; and `codex-on/002.req.json` still shows **12** tools, never growing to 13.
4. The turn ended `end_turn`, `is_error: false`, with the answer *"No `NotebookEdit` tool is available in this session, so I can't provide its parameter requirements."* (`/tmp/wp5-capture/codex-on.result.json`). Silent wrong answer.
5. **Control, same leg, same prompt, same model:** `codex-off` answered the `NotebookEdit` parameters correctly (`/tmp/wp5-capture/codex-off.result.json`). The failure is specific to the deferred path, not to the codex leg's tool calling in general.

Severity: silent. It produces neither a 4xx nor a "tool not found" — the two failure modes the reference told us to watch for. It degrades capability invisibly, and it also silently discards the agent-type listing and skills listing that share that message, so the codex leg's `Agent` and `Skill` tools are impaired by the same line.

Fix scope for WP-6 is a `sidecar/codex-sidecar.mjs` change plus its `node --test` suite — a **unit restart**, not a router rebuild or a `gateway.config.json` change. The shape suggested by the evidence is to fold the dropped `role:"system"` text into `baseInstructions` alongside `systemText` (which also repairs `Agent`/`Skill`), rather than to special-case the deferred-tools paragraph.

## Incidental finding — the router already fixes the qwen38 blocker

`docs/claude-code-local-profile.md` records `qwen38-27b-iq3s` returning **HTTP 500** (`Jinja Exception: System message must be at the beginning`) on every real harness request against `:4811`, and makes folding mid-array `system` messages a mandatory proxy requirement. Through the resident router at `:4813` that blocker is **gone**: both local cells completed a full 3-turn tool loop on `qwen38-27b-iq3s` with no 500 and no fallback to `qwen36-a3b` (`local-on/001-003`, `local-off/001-003`; `claude-local/qwen36-a3b` was never needed). The profile doc's exit (b) negative result is superseded on this path.

## What WP-6's `smoke-tool-search.sh` must assert

Four invariants, per leg, all asserted on **response body** never status alone. The passthrough leg asserts byte preservation; the local and codex legs assert semantic equivalence.

1. **Deferred references present in request 1.** Request 1's `tools` contains exactly one entry with `defer_loading: true` (`DeferredToolPlaceholder`) **and** a `ToolSearch` entry with a non-empty `input_schema`, **and** does not contain `NotebookEdit`. Negative control: the same assertion must fail against an `ENABLE_TOOL_SEARCH=false` profile, where `tools` has no `defer_loading` and no `ToolSearch`.
2. **Discovery call made.** The assistant emits a `tool_use` block named `ToolSearch`. On the codex leg this is the assertion that fails today.
3. **References relayed.** The `tool_result` for that call carries `{"type":"tool_reference","tool_name":"<name>"}` unchanged, and the following request's `tools` array has grown by exactly one entry which has both a full `input_schema` and `defer_loading: true` retained.
4. **Concrete tool executed.** The named deferred tool actually runs and its output reaches the final answer. Use a deferred built-in — **not** `WebFetch`/`WebSearch`, which the contained fixture denies. `NotebookEdit` is the right probe: it is in the deferred set on all three legs and its parameter names are a checkable witness.

Plus the token gate, **per leg, within-leg only**: request 1's `system`+`tools` `count_tokens` on the on-profile must be below the measured on-value with headroom, and the off-profile must exceed it — thresholds **29,159 / 17,745** (passthrough), **15,394 / 8,238** (local), **14,549 / 7,875** (codex). A conformance test that cannot fail is not a gate: assert both directions.

## Fixture profile used (documentation, not active config)

The fixture dirs and throwaway `$HOME`s were removed at the end of the run. Reproduced here so WP-6 can rebuild them.

Project `.claude/settings.json` in a disposable `/tmp` working dir, with `HOME` pointed at a fresh throwaway dir:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4795",
    "ANTHROPIC_CUSTOM_HEADERS": "x-gateway-key: <INFERENCE_ROUTER_GATEWAY_KEY>",
    "ANTHROPIC_MODEL": "claude-local/qwen38-27b-iq3s",
    "ANTHROPIC_AUTH_TOKEN": "local-dummy",
    "ENABLE_TOOL_SEARCH": "true",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1"
  },
  "permissions": {
    "deny": [
      "Edit(/~/**)",
      "Write(/~/**)",
      "Edit(/<code-root>/**)",
      "Write(/<code-root>/**)",
      "WebFetch",
      "WebSearch"
    ],
    "allow": ["Bash(ls)", "Bash(ls:*)"]
  }
}
```

Profile facts confirmed again on this run:

- `ANTHROPIC_AUTH_TOKEN: "local-dummy"` is sufficient for the local and codex legs — the router authenticates on `x-gateway-key`, not the bearer. The **passthrough** leg forwards the caller's own OAuth bearer: a bare `curl` with only the gateway key gets `{"error":{"message":"ANTHROPIC_API_KEY is missing."}}`, so hosted-leg `count_tokens` must be issued from inside an authenticated session.
- Trust must be seeded for the **project root Claude Code resolves**, which for a `/tmp` working dir is `/tmp`, not the working dir itself — seed both keys in `$HOME/.claude.json` or `permissions.allow` is silently ignored (observed: *"Ignoring 2 permissions.allow entries … this workspace has not been trusted"*).
- 2.1.270 repeats the `docs/claude-code-local-profile.md` erratum at startup: `Write(//path/**)` deny rules are not matched by file permission checks — only `Edit(path)` rules cover file-editing tools. Keep both forms.
- Bare `claude-haiku-4-5` routes to the passthrough leg; the `anthropic/` prefix is not required (both resolve identically).

## Capture harness

Throwaway pass-through proxy, `/tmp/wp5/capture-proxy.mjs`, **not promoted** — WP-6's fixture drives `claude -p` directly and replays captured bodies, so it needs no proxy. Three lines: a `node:http` server on `:4795` (4790/4791 were already occupied by sibling captures) logs each request's method/path/headers-minus-secrets/body to `/tmp/wp5-capture/<cell>/<n>.req.json` and forwards to `:4813` unchanged; it streams the response back byte-for-byte while logging it to `<n>.res.txt`; and it can replay `count_tokens` for previously captured bodies **in-process** using a live request's own `Authorization` header, so the hosted leg can be measured without the OAuth secret ever being copied to disk or into this document. Proven before use: `count_tokens` for `claude-local/qwen36-a3b` returned `{"input_tokens":17}` identically direct-to-`:4813` and through the proxy (`/tmp/wp5-capture/proxy-proof/001.req.json`).
