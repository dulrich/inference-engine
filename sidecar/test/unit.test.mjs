import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  parseModelId, isTitleRequest, titleEnvelope, extractCwd, conversational, sessionKeyOf, countTokensEstimate,
  verifyInstructionSources, mapUpstreamError, retryAfterSeconds, rateLimitExhaustion, presentedKey,
  toDynamicTools, Logger, usageOut,
} from "../codex-sidecar.mjs";

const TITLE_SYSTEM = "You are naming a coding session so the user can pick it out of a long list of sessions.";

test("effort lives in the model-id suffix", () => {
  // `effortSource` says whether the effort came from the suffix or the family default; the
  // `effort` values themselves are unchanged (precedence lives in resolveEffort — effort.test.mjs).
  assert.deepEqual(parseModelId("gpt-5.6-luna"), { model: "gpt-5.6-luna", effort: "low", effortSource: "default" });
  assert.deepEqual(parseModelId("gpt-5.6-sol"), { model: "gpt-5.6-sol", effort: "medium", effortSource: "default" });
  assert.deepEqual(parseModelId("gpt-6-astra"), { model: "gpt-6-astra", effort: "medium", effortSource: "default" });
  assert.deepEqual(parseModelId("gpt-6-astra-low"), { model: "gpt-6-astra", effort: "low", effortSource: "suffix" });
  assert.deepEqual(parseModelId("gpt-5.6-luna-high"), { model: "gpt-5.6-luna", effort: "high", effortSource: "suffix" });
  assert.deepEqual(parseModelId("gpt-5.6-sol-low"), { model: "gpt-5.6-sol", effort: "low", effortSource: "suffix" });
  // the gateway may or may not strip the provider segment
  assert.deepEqual(parseModelId("codex-a/gpt-5.6-luna-medium"), { model: "gpt-5.6-luna", effort: "medium", effortSource: "suffix" });
  // unknown suffix and unknown family are rejected (the caller answers 400)
  assert.equal(parseModelId("gpt-5.6-luna-ultra"), null);
  assert.equal(parseModelId("gpt-7-nova"), null);
  assert.equal(parseModelId(""), null);
  assert.equal(parseModelId(undefined), null);
});

test("the title signature needs the three robust conditions; the envelope is only a drift label", () => {
  const sessionMsg = [{ role: "user", content: "<session>chat</session>" }];
  // Claude Code 2.1.269 appends a language instruction after </session> (WP-4 G1)
  const cc2269 = [{ role: "user", content: "<session>chat</session>\n\nWrite the title in the predominant language of the session — a stray word or code token in another language doesn't change it, and neither does the English of these instructions." }];
  const drifted = [{ role: "user", content: "please read <session>x</session> for me" }];
  assert.equal(isTitleRequest({ tools: [] }, TITLE_SYSTEM, cc2269), true, "2.1.269 envelope");
  assert.equal(isTitleRequest({ tools: [] }, TITLE_SYSTEM, drifted), true, "envelope drift is fail-safe: still local");
  assert.equal(titleEnvelope(sessionMsg), "pinned");
  assert.equal(titleEnvelope(cc2269), "pinned");
  assert.equal(titleEnvelope(drifted), "drifted");
  assert.equal(isTitleRequest({ tools: [] }, TITLE_SYSTEM, sessionMsg), true);
  assert.equal(isTitleRequest({}, TITLE_SYSTEM, sessionMsg), true, "absent tools count as empty");
  assert.equal(isTitleRequest({ tools: [{ name: "Bash" }] }, TITLE_SYSTEM, sessionMsg), false, "tools present");
  assert.equal(isTitleRequest({ tools: [] }, "Summarise this chat", sessionMsg), false, "phrase missing");
  assert.equal(isTitleRequest({ tools: [] }, `${TITLE_SYSTEM}${"x".repeat(4000)}`, sessionMsg), false, "system too long");
  assert.equal(isTitleRequest({ tools: [] }, TITLE_SYSTEM, [...sessionMsg, { role: "assistant", content: "Title" }]), false, "more than one message");
});

test("cwd comes from the environment block and system-role messages are then dropped", () => {
  const msgs = [
    { role: "system", content: [{ type: "text", text: "Here is useful environment information:\nPrimary working directory: /home/u/repo\nIs a git repo: yes" }] },
    { role: "user", content: "hello" },
    { role: "assistant", content: "hi" },
    { role: "system", content: "<system-reminder>budget</system-reminder>" },
  ];
  assert.equal(extractCwd(msgs, ""), "/home/u/repo");
  assert.equal(conversational(msgs).length, 2);
  assert.equal(extractCwd([{ role: "user", content: "no env here" }], ""), null);
  assert.equal(extractCwd([], "Primary working directory: /srv/x"), "/srv/x");
});

test("sessions key on metadata.user_id and fall back to a warned hash", () => {
  const body = sid => ({ metadata: { user_id: JSON.stringify({ device_id: "d", account_uuid: "a", session_id: sid }) } });
  assert.deepEqual(sessionKeyOf(body("abc-123"), "sys", "hi"), { key: "abc-123", weak: false, scope: "parent" });

  const a = sessionKeyOf({}, "sys", "hi");
  const b = sessionKeyOf({}, "sys", "hi");
  const c = sessionKeyOf({}, "sys", "different");
  assert.equal(a.weak, true);
  assert.equal(a.key, b.key, "the fallback is stable for the same prompt");
  assert.notEqual(a.key, c.key);
  assert.equal(sessionKeyOf({ metadata: { user_id: "not json" } }, "sys", "hi").weak, true);
});

test("upstream errors, rate-limit windows and instruction sources map as the schema says", () => {
  assert.equal(mapUpstreamError({ message: "boom" }).status, 502);
  assert.equal(mapUpstreamError({ message: "nope", data: { codexErrorInfo: "unauthorized" } }).status, 401);
  assert.equal(mapUpstreamError({ message: "nope", codexErrorInfo: "rateLimitExceeded" }).status, 429);
  assert.equal(mapUpstreamError({ message: "x", codexErrorInfo: { httpConnectionFailed: { httpStatusCode: 429 } } }).status, 429);
  assert.equal(mapUpstreamError({ message: "usage limit reached for this account" }).anthropicType, "rate_limit_error");

  const now = 1_700_000_000_000;
  assert.equal(retryAfterSeconds(now / 1000 + 60, now), 60);
  assert.equal(retryAfterSeconds(now + 60_000, now), 60);
  assert.equal(retryAfterSeconds(now - 5000, now), 1, "a past reset still asks for one second");
  assert.equal(retryAfterSeconds(null), null);

  assert.equal(rateLimitExhaustion({ primary: { usedPercent: 40 } }), null);
  assert.equal(rateLimitExhaustion({ primary: { usedPercent: 100 } }).reason, "rate_limit_reached");
  assert.equal(rateLimitExhaustion({ secondary: { usedPercent: 10 }, rateLimitReachedType: "workspace_owner_credits_depleted" }).reason, "workspace_owner_credits_depleted");

  assert.equal(verifyInstructionSources([]), null);
  assert.equal(verifyInstructionSources(["/home/u/.codex-a/AGENTS.md"]), null);
  assert.match(verifyInstructionSources(["/a/AGENTS.md", "/b/AGENTS.md"]), /at most/);
  assert.match(verifyInstructionSources(["/repo/README.md"]), /unexpected instruction source/);

  assert.equal(presentedKey({ authorization: "Bearer sekret" }), "sekret");
  assert.equal(presentedKey({ "x-api-key": "sekret" }), "sekret");
  assert.equal(presentedKey({}), null);

  assert.deepEqual(toDynamicTools([{ name: "Bash", description: "run", input_schema: { type: "object" } }]),
    [{ type: "function", name: "Bash", description: "run", inputSchema: { type: "object" } }]);
  assert.ok(countTokensEstimate({ system: [{ type: "text", text: "12345678" }], messages: [] }) === 2);
});

test("the JSONL log is 0600 in a 0700 dir and prunes past the retention window", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "codex-sidecar-log-"));
  const stale = path.join(dir, "codex-sidecar-2020-01-01.jsonl");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(stale, "{}\n");

  const logger = new Logger(dir, 14);
  logger.write("listen", { port: 0 });
  logger.close();

  const today = path.join(dir, `codex-sidecar-${Logger.dayOf()}.jsonl`);
  assert.equal(fs.existsSync(stale), false, "files past SIDECAR_LOG_KEEP_DAYS are pruned");
  assert.equal(fs.statSync(today).mode & 0o777, 0o600);
  assert.equal(fs.statSync(dir).mode & 0o777, 0o700);
  assert.equal(JSON.parse(fs.readFileSync(today, "utf8").trim()).kind, "listen");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("usage is projected into Anthropic semantics: input_tokens is the uncached share, clamped at 0", () => {
  // Codex reports the whole prompt in `inputTokens` with the cached share a subset of it;
  // Claude Code's meter sums input + cache_read + cache_creation, so the raw figure counted
  // the prompt twice (measured 2026-09-13: 152,791 vs 79,952 for one conversation).
  assert.deepEqual(usageOut(undefined), {
    input_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 0,
  }, "no usage yet");
  // first turn of a thread: nothing cached, so the uncached share is the whole prompt
  assert.deepEqual(usageOut({ inputTokens: 6499, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: 0 }), {
    input_tokens: 6499, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, output_tokens: 0,
  });
  // the normal case: a warm prefix cache. 6463 - 3840 = 2623 uncached, summing back to 6463.
  const warm = usageOut({ inputTokens: 6463, cachedInputTokens: 3840, cacheWriteInputTokens: 0, outputTokens: 12 });
  assert.deepEqual(warm, {
    input_tokens: 2623, cache_creation_input_tokens: 0, cache_read_input_tokens: 3840, output_tokens: 12,
  });
  assert.equal(warm.input_tokens + warm.cache_read_input_tokens + warm.cache_creation_input_tokens, 6463,
    "a client summing the three input columns gets the prompt exactly once");
  // cache writes come out of the same prompt total
  assert.deepEqual(usageOut({ inputTokens: 10000, cachedInputTokens: 6000, cacheWriteInputTokens: 1500, outputTokens: 40 }), {
    input_tokens: 2500, cache_creation_input_tokens: 1500, cache_read_input_tokens: 6000, output_tokens: 40,
  });
  // clamp: a reconciled delta whose cache-read share exceeds its input share reports 0, never negative
  assert.deepEqual(usageOut({ inputTokens: 128, cachedInputTokens: 3840, cacheWriteInputTokens: 0, outputTokens: 7 }), {
    input_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 3840, output_tokens: 7,
  });
  // output_tokens passes through untouched on every path
  assert.equal(usageOut({ inputTokens: 0, cachedInputTokens: 0, cacheWriteInputTokens: 0, outputTokens: 5418 }).output_tokens, 5418);
});
