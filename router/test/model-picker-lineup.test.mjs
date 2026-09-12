// Tests for router/model-picker-lineup.mjs. The CLI is driven as a child process; every run uses a
// temp --home, so the real ~/.claude/settings.json is never read or written, and the gateway key
// always comes from a temp fixture env file with a dummy value.

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(HERE, "..", "model-picker-lineup.mjs");
const FIXTURE_CONFIG = path.join(HERE, "fixtures", "gateway.config.fixture.json");
const REAL_CONFIG = path.join(HERE, "..", "gateway.config.json");
const DUMMY_KEY = "fixture-dummy-key-not-a-secret";

function tempHome(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "model-picker-test-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dir, ".claude"), { recursive: true });
  const envFile = path.join(dir, "env.fixture");
  fs.writeFileSync(envFile, `OTHER=1\nINFERENCE_ROUTER_GATEWAY_KEY=${DUMMY_KEY}\n`);
  return { dir, envFile, settings: path.join(dir, ".claude", "settings.json") };
}

let stderrSeq = 0;

// execFileSync inherits the child's stderr unless it is given an fd, and returns only stdout — so
// stderr goes to a scratch file the assertions can read (the warning cases are stderr-only).
function run(args, expectedStatus = 0) {
  const errFile = path.join(os.tmpdir(), `model-picker-stderr-${process.pid}-${(stderrSeq += 1)}`);
  const fd = fs.openSync(errFile, "w");
  let status = 0;
  let stdout = "";
  try {
    stdout = execFileSync(process.execPath, [SCRIPT, ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", fd],
    });
  } catch (err) {
    status = err.status;
    stdout = err.stdout ?? "";
  } finally {
    fs.closeSync(fd);
  }
  const stderr = fs.readFileSync(errFile, "utf8");
  fs.rmSync(errFile, { force: true });
  assert.equal(status, expectedStatus, `exit ${status}, expected ${expectedStatus}: ${stderr}`);
  return { status, stdout, stderr };
}

test("renders one row per non-anthropic id, with the documented labels", () => {
  const rows = JSON.parse(run(["--config", FIXTURE_CONFIG]).stdout);
  assert.equal(rows.length, 9);
  assert.equal(rows.filter((r) => r.model.startsWith("anthropic/")).length, 0);
  const byModel = Object.fromEntries(rows.map((r) => [r.model, r]));
  assert.equal(byModel["claude-local/qwen38-27b-iq3s"].label, "Qwen3.8 27B (local)");
  assert.equal(byModel["claude-local/nemotron-4b"].label, "Nemotron 4B (local)");
  // Unknown ids are additive-config normal: bare id verbatim, never a failure.
  assert.equal(byModel["claude-local/some-future-model-9b"].label, "some-future-model-9b (local)");
  assert.equal(byModel["openrouter/z-ai/glm-5.2:free"].label, "GLM 5.2 free (openrouter)");
  assert.equal(byModel["or-anthropic/z-ai/glm-5.3"].label, "GLM 5.3 (or-anthropic)");
  // Suffix-less codex ids take the persona default; suffixed ones take the suffix.
  assert.equal(byModel["codex-a/gpt-5.6-luna"].label, "Luna low (codex-a)");
  assert.equal(byModel["codex-a/gpt-6-astra-low"].label, "Astra low (codex-a)");
  assert.equal(byModel["codex-b/gpt-5.6-sol"].label, "Sol medium (codex-b)");
  assert.equal(byModel["codex-b/gpt-5.6-luna-high"].label, "Luna high (codex-b)");
  for (const row of rows) {
    assert.ok(row.description.length < 90, `description too long: ${row.description}`);
    assert.equal(row.behavesAs, undefined);
  }
});

test("the real config yields 25 rows and no anthropic id", () => {
  const rows = JSON.parse(run(["--config", REAL_CONFIG]).stdout);
  assert.equal(rows.length, 25);
  assert.equal(rows.filter((r) => r.model.startsWith("anthropic/")).length, 0);
});

test("--write preserves unrelated keys and their order, and is idempotent", (t) => {
  const { dir, settings } = tempHome(t);
  const before = { permissions: { allow: ["Bash(ls:*)"] }, statusLine: { type: "command" }, zzz: 1 };
  fs.writeFileSync(settings, `${JSON.stringify(before, null, 2)}\n`);

  run(["--config", FIXTURE_CONFIG, "--home", dir, "--write"]);
  const first = fs.readFileSync(settings, "utf8");
  const parsed = JSON.parse(first);
  assert.deepEqual(Object.keys(parsed), ["permissions", "statusLine", "zzz", "modelPicker"]);
  assert.deepEqual(parsed.permissions, before.permissions);
  assert.equal(parsed.modelPicker.options.length, 9);
  assert.equal(parsed.modelPicker.replaceBuiltInOptions, undefined);
  assert.equal(parsed.env, undefined);

  run(["--config", FIXTURE_CONFIG, "--home", dir, "--write"]);
  assert.equal(fs.readFileSync(settings, "utf8"), first, "second --write must produce zero diff");
});

test("non-canonical settings JSON is refused with exit 2 and the file is untouched", (t) => {
  const { dir, settings } = tempHome(t);
  const original = '{"permissions":{"allow":[]}}';
  fs.writeFileSync(settings, original);
  const res = run(["--config", FIXTURE_CONFIG, "--home", dir, "--write"], 2);
  assert.match(res.stderr, /not canonical 2-space JSON; refusing to rewrite/);
  assert.equal(fs.readFileSync(settings, "utf8"), original);
});

test("--check is 1 when stale and 0 when fresh", (t) => {
  const { dir, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ modelPicker: { options: [] } }, null, 2)}\n`);
  const stale = run(["--config", FIXTURE_CONFIG, "--home", dir, "--check"], 1);
  assert.match(stale.stderr, /modelPicker\.options stale/);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--write"]);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--check"]);
});

test("--launch-profile writes the router env keys and never a token key", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: { FOO: "bar" } }, null, 2)}\n`);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  assert.equal(parsed.env.FOO, "bar");
  assert.equal(parsed.env.ANTHROPIC_BASE_URL, "http://127.0.0.1:4813");
  assert.equal(parsed.env.ANTHROPIC_CUSTOM_HEADERS, `x-gateway-key: ${DUMMY_KEY}`);
  assert.equal(parsed.env.ANTHROPIC_AUTH_TOKEN, undefined);
  assert.equal(parsed.env.ANTHROPIC_API_KEY, undefined);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--check", "--launch-profile"]);
});

test("a pre-existing ANTHROPIC_AUTH_TOKEN is left untouched with a warning", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: { ANTHROPIC_AUTH_TOKEN: "preexisting" } }, null, 2)}\n`);
  const res = run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  assert.match(res.stderr, /env\.ANTHROPIC_AUTH_TOKEN already present/);
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  assert.equal(parsed.env.ANTHROPIC_AUTH_TOKEN, "preexisting");
  assert.equal(parsed.env.ANTHROPIC_BASE_URL, "http://127.0.0.1:4813");
});

test("a missing gateway key is exit 2 and writes nothing", (t) => {
  const { dir, settings } = tempHome(t);
  const emptyEnv = path.join(dir, "empty.env");
  fs.writeFileSync(emptyEnv, "OTHER=1\n");
  const res = run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", emptyEnv, "--write", "--launch-profile"], 2);
  assert.match(res.stderr, /INFERENCE_ROUTER_GATEWAY_KEY is missing or empty/);
  assert.equal(fs.existsSync(settings), false);
});

test("--launch-profile adds the tool-search and title keys when the input lacks them", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: { FOO: "bar" } }, null, 2)}\n`);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const env = JSON.parse(fs.readFileSync(settings, "utf8")).env;
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");
  assert.equal(env.FOO, "bar");
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--check", "--launch-profile"]);
});

test("regenerating over an input that already carries both keys is a byte-for-byte no-op", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  const before = {
    env: {
      ANTHROPIC_BASE_URL: "http://127.0.0.1:4813",
      ANTHROPIC_CUSTOM_HEADERS: `x-gateway-key: ${DUMMY_KEY}`,
      ENABLE_TOOL_SEARCH: "true",
      CLAUDE_CODE_DISABLE_TERMINAL_TITLE: "1",
    },
  };
  fs.writeFileSync(settings, `${JSON.stringify(before, null, 2)}\n`);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const first = fs.readFileSync(settings, "utf8");
  const env = JSON.parse(first).env;
  // No duplication: the four generated keys, in their original order, and nothing added.
  assert.deepEqual(Object.keys(env), Object.keys(before.env));
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");

  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  assert.equal(fs.readFileSync(settings, "utf8"), first, "second --write must produce zero diff");
});

test("a drifted generated env key fails --check --launch-profile and --write restores it", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: {} }, null, 2)}\n`);
  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);

  const drifted = JSON.parse(fs.readFileSync(settings, "utf8"));
  drifted.env.ENABLE_TOOL_SEARCH = "false";
  delete drifted.env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE;
  fs.writeFileSync(settings, `${JSON.stringify(drifted, null, 2)}\n`);

  const stale = run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--check", "--launch-profile"], 1);
  assert.match(stale.stderr, /env\.ENABLE_TOOL_SEARCH .* is "false", expected "true"/);

  run(["--config", FIXTURE_CONFIG, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const env = JSON.parse(fs.readFileSync(settings, "utf8")).env;
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");
});
