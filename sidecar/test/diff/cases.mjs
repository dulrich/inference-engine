// cases.mjs — the differential scenario corpus (README.md beside this file).
// Each case: { name, scenario | perPersona, env?, outbox?, steps: [step..] }.
// A step is { path, method?, body?, raw?, key?, keyHeader?, headers? }; `raw` sends a literal
// string body (bad JSON). Every step's response lands in the transcript in order.
import { SIMPLE_TURN, msgBody } from "../helpers.mjs";

const M = "/codex-a/v1/messages";
const bash = [{ name: "Bash", description: "run", input_schema: { type: "object" } }];
const call = (callId, tool, args) => ({ request: "item/tool/call", params: { callId, tool, arguments: args } });
const sid = n => `dddddddd-0000-0000-0000-00000000000${n}`;

export const cases = [
  {
    name: "simple non-stream turn",
    scenario: { defaultTurn: SIMPLE_TURN },
    steps: [{ path: M, body: msgBody({ sessionId: sid(1) }) }],
  },
  {
    name: "simple streamed turn",
    scenario: { defaultTurn: SIMPLE_TURN },
    steps: [{ path: M, body: msgBody({ stream: true, sessionId: sid(2) }) }],
  },
  {
    name: "auth failure 401 (none, wrong, x-api-key wrong)",
    scenario: { defaultTurn: SIMPLE_TURN },
    steps: [
      { path: M, body: msgBody({}), key: null },
      { path: M, body: msgBody({}), key: "wrong" },
      { path: "/codex-a/api/hello", method: "GET", key: "wrong", keyHeader: "x-api-key" },
    ],
  },
  {
    name: "unknown persona, unknown route, healthz, hello, count_tokens",
    scenario: { defaultTurn: SIMPLE_TURN },
    steps: [
      { path: "/codex-nobody/v1/messages", body: msgBody({}) },
      { path: "/codex-a/v1/complete", body: {} },
      { path: "/healthz", method: "GET" },
      { path: "/codex-a/api/hello", method: "GET" },
      { path: "/codex-a/v1/messages/count_tokens", body: msgBody({ messages: [{ role: "user", content: "abcdefgh" }] }) },
    ],
  },
  {
    name: "bad JSON, bad model suffix, image block 400s",
    scenario: { defaultTurn: SIMPLE_TURN },
    steps: [
      { path: M, raw: "{not json" },
      { path: M, body: msgBody({ model: "gpt-5.6-luna-ultra" }) },
      { path: M, body: msgBody({ messages: [{ role: "user", content: [{ type: "image", source: { type: "base64", data: "x" } }] }] }) },
    ],
  },
  {
    name: "tool-call round trip",
    scenario: { turns: [[call("call_abc", "Bash", { command: "ls -la" }), { awaitToolResults: 1 }, { text: "done" }, { complete: {} }]] },
    steps: [
      { path: M, body: msgBody({ tools: bash, sessionId: sid(3) }) },
      {
        path: M,
        body: msgBody({
          tools: bash,
          sessionId: sid(3),
          messages: [
            { role: "user", content: "hi" },
            { role: "assistant", content: [{ type: "tool_use", id: "call_abc", name: "Bash", input: { command: "ls -la" } }] },
            { role: "user", content: [{ type: "tool_result", tool_use_id: "call_abc", content: "file list" }] },
          ],
        }),
      },
    ],
  },
  {
    name: "streamed tool call",
    scenario: { turns: [[{ text: "a" }, { text: "b" }, call("c1", "Bash", {}), { awaitToolResults: 1 }, { complete: {} }]] },
    steps: [{ path: M, body: msgBody({ tools: bash, stream: true, sessionId: sid(4) }) }],
  },
  {
    name: "security boundary: non-allowlisted item.type -> 502 + security-event",
    scenario: {
      defaultTurn: [
        { text: "let me check" },
        { item: { type: "commandExecution", command: ["rm", "-rf", "/"], commandActions: [], cwd: "/tmp", status: "inProgress" } },
        { delay: 3000 },
        { complete: {} },
      ],
    },
    env: { SIDECAR_BACKOFF_MS: "5000" },
    steps: [
      { path: M, body: msgBody({ sessionId: sid(5) }) },
      { path: "/healthz", method: "GET" },
    ],
  },
  {
    name: "session continuation (second turn, same session)",
    scenario: { turns: [[{ text: "one" }, { complete: {} }], [{ text: "two" }, { complete: {} }]] },
    steps: [
      { path: M, body: msgBody({ sessionId: sid(6) }) },
      {
        path: M,
        body: msgBody({
          sessionId: sid(6),
          messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "one" }, { role: "user", content: "again" }],
        }),
      },
    ],
  },
  {
    name: "effort-suffix model id",
    scenario: { defaultTurn: SIMPLE_TURN },
    steps: [{ path: M, body: msgBody({ model: "gpt-5.6-luna-high", sessionId: sid(7) }) }],
  },
  {
    name: "upstream auth error 401 and usage limit 429",
    perPersona: {
      "codex-a": { threadStart: { error: { code: -32001, message: "unauthorized", data: { codexErrorInfo: "unauthorized" } } } },
      "codex-b": { defaultTurn: [{ complete: { status: "failed", error: { message: "usage limit reached", codexErrorInfo: "rateLimitExceeded" } } }] },
    },
    steps: [
      { path: M, body: msgBody({ sessionId: sid(8) }) },
      { path: "/codex-b/v1/messages", body: msgBody({ sessionId: sid(8) }) },
    ],
  },
  {
    name: "rate-limit notification -> 429 retry-after",
    scenario: () => ({
      defaultTurn: [
        { delay: 20 },
        { notify: "account/rateLimits/updated", params: { rateLimits: { primary: { usedPercent: 100, resetsAt: Math.floor(Date.now() / 1000) + 120, windowDurationMins: 300 }, rateLimitReachedType: "rate_limit_reached" } } },
        { delay: 3000 },
        { complete: {} },
      ],
    }),
    steps: [{ path: M, body: msgBody({ sessionId: sid(9) }) }],
  },
  {
    name: "error notification mid-stream",
    scenario: { defaultTurn: [{ text: "starting" }, { delay: 20 }, { notify: "error", params: { error: { message: "upstream blew up" }, willRetry: false } }, { delay: 3000 }] },
    steps: [{ path: M, body: msgBody({ stream: true, sessionId: sid(0) }) }],
  },
  {
    name: "outbox row for a parent turn",
    scenario: { defaultTurn: SIMPLE_TURN },
    outbox: true,
    steps: [{ path: M, body: msgBody({ sessionId: "aaaaaaaa-1111-1111-1111-111111111111" }) }],
  },
];
