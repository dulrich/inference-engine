# Claude Code on local models via `:4811` — live-verified profile and wire findings

Plan: `plans/capability-surfacing-and-local-agent-endpoints.md` WP-4. All behavior below was observed live on 2026-08-24 against llama.cpp b10534 / llama-swap v243 (image `b10534-swap243`) with **Claude Code 2.1.243** in headless (`claude -p`, `query_source: "sdk"`) mode. Nothing here is assumed from docs; every claim was probed by response body or captured off the wire with a local sink.

## Verdict

- **Exit (a) — works end-to-end**: a real Claude Code session completed a multi-tool task (Write → Bash → Write, 4 turns, correct FizzBuzz output) against **`qwen36-a3b`** through `http://127.0.0.1:4811`, with llama.cpp prompt caching engaged (`cache_read_input_tokens` ≈ 18k on turns 2+ of a ~19.8k-token agent prompt — turn latency after the first is seconds, not minutes).
- **Exit (b) — negative result for `qwen38-27b-iq3s`** (the plan's target coding alias): every real harness request 500s at template render. Root cause and the mandatory WP-5 proxy requirement are below.

## Working profile (fixture-verified snippet)

Project `.claude/settings.json` in the session's working directory:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4811",
    "ANTHROPIC_AUTH_TOKEN": "local-dummy",
    "ANTHROPIC_MODEL": "qwen36-a3b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "nemotron-4b"
  },
  "permissions": {
    "deny": [
      "Write(/~/**)",
      "Edit(/~/**)",
      "Write(/**)",
      "Edit(/**)",
      "WebFetch",
      "WebSearch"
    ]
  }
}
```

This is documentation, not active config — the fixture that ran it was disposable and has been torn down. Two profile facts observed the hard way:

- **`settings.json` `env` beats the process environment**: exporting `ANTHROPIC_MODEL=...` in the shell does *not* override a value set in the workspace settings `env` block. Change the settings file, not the export.
- **An untrusted workspace silently ignores `permissions.allow`** (warning on stderr only). Trust is `projects["<dir>"].hasTrustDialogAccepted: true` in `~/.claude.json`, or one interactive session in that directory.

### Containment: deny rules work, allow scoping does not

In headless mode the harness happily wrote to absolute paths **outside** the session's working directory (observed: `~/fizzbuzz.py` created despite an allow-list of only `Write(./**)` — relative allow entries do not constrain absolute-path writes, and no permission prompt exists in `-p` mode). Explicit absolute `deny` rules **do** enforce: with `Write(/~/**)` denied, the same request was blocked and the model reported the denial. Rule: a headless local-model profile must carry explicit `deny` entries for every sensitive root (or run under a throwaway `$HOME`/container); do not rely on allow-list scoping.

**Errata (WP-5, 2026-08-25)**: 2.1.243 warns at startup that `Write(//path/**)` rules are *not* matched by file permission checks — only `Edit(path)` rules cover file-editing tools. The profile above enforced because it carries both forms; when writing deny rules, use `Edit(//path/**)` (optionally alongside `Write(...)`) — an `Edit` rule covers all file-editing tools including Write.

## The qwen38 blocker: mid-array `role:"system"` message

Claude Code 2.1.243 appends a trailing message with **`role: "system"`** inside the `messages` array of every request ("Available agent types for the Agent tool: …" — the subagent advertisement). Captured verbatim off the wire; sent unconditionally — `--disallowedTools "Agent"` removes the Agent *tool* but the trailing system message is still sent, and no env var, flag, or SDK option suppresses it (checked against current docs 2026-08-24).

Per-template tolerance, probed live with a minimal `[user, system]` messages array:

| model | behavior |
| --- | --- |
| `qwen38-27b-iq3s` / `-iq3xxs` / `-q3kxl` (Qwen3.8 template) | **HTTP 500** — `Jinja Exception: System message must be at the beginning` (raised in ~7 ms, pre-inference; harness retries ~10× then gives up, ~175 s wasted) |
| `qwen36-a3b` | tolerated; task loop unaffected |
| `ling-3-tiny-*` | tolerated |
| `gemma-4-12b-qat` | tolerated |
| `nemotron-4b` | tolerated by the template, **but the message lands after the user prompt and derailed the 4B model** — both attempts answered the agent-type listing instead of doing the task. Fine as the haiku slot (background/summarization calls), not as the main loop driver at this size. |

**Mandatory WP-5 proxy requirement (plan exit (b))**: an arm cannot win WP-5's routing evaluation unless it makes qwen38 usable — i.e. the proxy folds/relocates mid-array `system` messages (into the top-level `system` field or a position-0 system message) before forwarding. A secondary quality argument applies to every model: relocating that trailing system message ahead of the user prompt would also stop it from derailing small models.

## Per-gotcha observed behavior

| gotcha | observed |
| --- | --- |
| `POST /v1/messages?beta=true` | confirmed on the wire; llama-swap/llama.cpp accept the query param — no error, identical behavior |
| `HEAD /api/hello` warm-up | confirmed on the wire (first request of every session); llama-swap answers it; a non-200 here stalls the CLI in a long retry loop |
| adaptive thinking | harness sends `"thinking":{"type":"adaptive","display":"omitted"}`; llama.cpp **accepts it** (also accepts `{"type":"enabled","budget_tokens":N}`). `CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING=1` was **not needed** on this stack |
| unknown top-level keys | `context_management`, `output_config`, `metadata` all tolerated (llama.cpp ignores unknown keys) |
| `count_tokens` | `POST /v1/messages/count_tokens` returns a real count (`{"input_tokens":57}`); ~11 s when it triggers a cold model load, fast warm |
| thinking `signature: ""` | every `thinking` block carries an empty signature; sending it back verbatim in multi-turn is **accepted** (no signature validation server-side) |
| model echo | `message.model` echoes the gguf path (`/models/unsloth/...gguf`), not the alias — cosmetic; the CLI logs `[claude-code:unrecognized_model]` and cost/context-window numbers are meaningless for local aliases |
| tool loop wire shape | Anthropic `tools`/`input_schema` → `tool_use` block + `stop_reason:"tool_use"`; `tool_result` round-trip → correct final answer; verified standalone and through the harness |
| prompt caching | llama.cpp reports Anthropic-style `cache_read_input_tokens`; real harness turns hit ~18k cache reads — the local-agent latency story depends on it |
| SSE under long prefill | see below |

## SSE / 300 s silent-stream condition

llama.cpp sends **no bytes at all until prefill completes** — no `message_start`, no keepalive. Measured with a ~62k-token prompt on warm `qwen38-27b-iq3s`: first body byte at **104.9 s**, then a clean, correct event stream (`message_start` → `content_block_*` → `message_stop`) with no further gaps; needle recall at depth was correct. Implications:

- At this alias's 131072 ceiling, projected prefill silence is ~220 s warm — inside Claude Code's 300 s silent-stream abort, **but marginal once a cold model load (tens of seconds) stacks on top**. A cold near-ceiling first turn can plausibly cross 300 s and be aborted client-side while the server is still healthy.
- WP-5 proxy arms should be evaluated on whether they emit early SSE (or keepalives) during upstream prefill; that removes the condition entirely.

## Reproduction notes

- Wire capture: point `ANTHROPIC_BASE_URL` at a local HTTP sink that logs bodies and returns a non-retryable 400 (`invalid_request_error`) — a 500 sends the CLI into minutes of retries. The sink must 200 the `HEAD /api/hello` warm-up or the session stalls before the first real request.
- The `/v1/messages` 500 repro is two curl lines: any request whose `messages` array contains a `role:"system"` entry past index 0 against a qwen38 alias.
