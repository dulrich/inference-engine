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
const FIXTURE_VISIBILITY = path.join(HERE, "fixtures", "visibility.fixture.json");
// Every fixture run pins BOTH files: `claude-local` rows come from the declaration since WP-3
// (plans/bench-dashboard-serving-control.md), so a run that named only --config would silently
// read the repo's own visibility.json against a fixture config.
const FIXTURE_ARGS = ["--config", FIXTURE_CONFIG, "--visibility", FIXTURE_VISIBILITY];
const REAL_CONFIG = path.join(HERE, "..", "gateway.config.json");
const REAL_VISIBILITY = path.join(HERE, "..", "visibility.json");
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

test("renders one row per Claude-Code-visible non-anthropic id, with the documented labels", () => {
  const rows = JSON.parse(run([...FIXTURE_ARGS]).stdout);
  assert.equal(rows.length, 11);
  assert.equal(rows.filter((r) => r.model.startsWith("anthropic/")).length, 0);
  const byModel = Object.fromEntries(rows.map((r) => [r.model, r]));
  // The local qwen38 family is a prefix family: the FAMILIES entry still labels a hand-typed
  // suffix id (the fixture declaration gives all five `cc`; the real declaration gives the four
  // suffixed ones `[]` — see the real-config test), and the unsuffixed id renders as the
  // "default" entry rather than duplicating the -high label.
  assert.equal(byModel["claude-local/qwen38-27b-iq3s"].label, "Qwen3.8 27B default (local)");
  assert.equal(byModel["claude-local/qwen38-27b-iq3s-off"].label, "Qwen3.8 27B off (local)");
  assert.equal(byModel["claude-local/qwen38-27b-iq3s-low"].label, "Qwen3.8 27B low (local)");
  assert.equal(byModel["claude-local/qwen38-27b-iq3s-medium"].label, "Qwen3.8 27B medium (local)");
  assert.equal(byModel["claude-local/qwen38-27b-iq3s-high"].label, "Qwen3.8 27B high (local)");
  assert.match(byModel["claude-local/qwen38-27b-iq3s-high"].description, /reasoning high is in the id suffix/);
  assert.match(byModel["claude-local/qwen38-27b-iq3s"].description, /no suffix means the default budget/);
  // Unknown ids are additive-config normal: bare id verbatim, never a failure.
  assert.equal(byModel["claude-local/some-future-model-9b"].label, "some-future-model-9b (local)");
  assert.equal(byModel["or-anthropic/z-ai/glm-5.3"].label, "GLM 5.3 (or-anthropic)");
  // Suffix-less codex ids take the persona default; suffixed ones take the suffix.
  assert.equal(byModel["codex-a/gpt-5.6-luna"].label, "GPT-5.6 Luna (codex-a)");
  assert.equal(byModel["codex-a/gpt-6-astra-low"].label, "GPT-6 Astra low (codex-a)");
  assert.equal(byModel["codex-b/gpt-5.6-sol"].label, "GPT-5.6 Sol (codex-b)");
  assert.equal(byModel["codex-b/gpt-5.6-luna-high"].label, "GPT-5.6 Luna high (codex-b)");
  for (const row of rows) {
    assert.ok(row.description.length < 90, `description too long: ${row.description}`);
    assert.equal(row.behavesAs, undefined);
  }
});

test("the real config yields 8 Claude-Code rows: every effort id out, the openwebui ids out", () => {
  const rows = JSON.parse(run(["--config", REAL_CONFIG, "--visibility", REAL_VISIBILITY]).stdout);
  assert.equal(rows.length, 8);
  assert.equal(rows.filter((r) => r.model.startsWith("anthropic/")).length, 0);
  const models = rows.map((r) => r.model);
  // The exact lineup: two local rows plus one bare codex id per family per persona.
  assert.deepEqual(models, [
    "claude-local/qwen36-a3b",
    "claude-local/qwen38-27b-iq3s",
    "codex-a/gpt-5.6-luna",
    "codex-a/gpt-5.6-sol",
    "codex-a/gpt-6-astra",
    "codex-b/gpt-5.6-luna",
    "codex-b/gpt-5.6-sol",
    "codex-b/gpt-6-astra",
  ]);
  // Reasoning effort on the local leg is a request field since router patch 0007
  // (plans/router-effort-mapping-claude-local.md): the harness's own effort control maps onto
  // chat_template_kwargs, so the picker carries exactly ONE Qwen3.8 row and the four suffix ids —
  // the server-enforced hard-cap surface, declared `consumers: []` in visibility.json since
  // WP-3 — are not rows.
  assert.ok(models.includes("claude-local/qwen38-27b-iq3s"), "the unsuffixed qwen38 row must stay");
  assert.equal(models.filter((m) => m.startsWith("claude-local/qwen38-27b-iq3s")).length, 1);
  for (const effort of ["off", "low", "medium", "high"]) {
    assert.equal(
      models.includes(`claude-local/qwen38-27b-iq3s-${effort}`),
      false,
      `effort id ${effort} must not be a picker row`,
    );
  }
  // Not a picker row is not "gone": the four must stay RESOLVABLE ids, i.e. still listed in the
  // same provider's models[] (an id dropped from there 400s at model_resolution for the callers
  // that want the hard cap). /v1/models on the live router is smoke-router.sh check 1's job.
  const config = JSON.parse(fs.readFileSync(REAL_CONFIG, "utf8"));
  const claudeLocal = config.providers.find((p) => p.name === "claude-local");
  for (const effort of ["off", "low", "medium", "high"]) {
    assert.ok(
      claudeLocal.models.includes(`qwen38-27b-iq3s-${effort}`),
      `effort id ${effort} must stay resolvable in claude-local.models[]`,
    );
  }
  // Same story on the codex leg since plans/codex-leg-effort-and-auditor-unpinning.md WP-2: effort
  // is the harness's own control, resolved per turn by the sidecar, so the picker carries exactly
  // ONE row per family per persona (the bare id) and the 12 suffix ids are tagged ["router"].
  const CODEX_PERSONAS = ["codex-a", "codex-b"];
  const CODEX_SUFFIX_IDS = [
    "gpt-5.6-luna-medium",
    "gpt-5.6-luna-high",
    "gpt-5.6-sol-low",
    "gpt-5.6-sol-high",
    "gpt-6-astra-low",
    "gpt-6-astra-high",
  ];
  for (const persona of CODEX_PERSONAS) {
    const provider = config.providers.find((p) => p.name === persona);
    for (const id of CODEX_SUFFIX_IDS) {
      assert.equal(models.includes(`${persona}/${id}`), false, `${persona}/${id} must not be a picker row`);
      // Not a picker row is not "gone" — the suffix ids stay resolvable (alias contract) and are
      // the explicit-effort escape hatch that outranks the request field.
      assert.ok(provider.models.includes(id), `${persona}/${id} must stay resolvable in models[]`);
    }
    for (const bare of ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra"]) {
      const row = rows.find((r) => r.model === `${persona}/${bare}`);
      assert.ok(row, `${persona}/${bare} must be a picker row`);
      assert.match(row.description, /effort via the harness/);
    }
  }
  // Tagged for OpenWebUI only — these are the rows the tag exists to remove from the picker.
  assert.equal(models.filter((m) => m.startsWith("openrouter/") || m.startsWith("or-anthropic/")).length, 0);
  assert.equal(models.includes("claude-local/nemotron-4b"), false);
});

test("consumer tags still govern every provider EXCEPT claude-local, unchanged", () => {
  const rows = JSON.parse(run([...FIXTURE_ARGS]).stdout);
  const models = new Set(rows.map((r) => r.model));
  // Untagged provider: unchanged, every id renders (the pre-tag behaviour of every provider).
  assert.ok(models.has("or-anthropic/z-ai/glm-5.3"));
  assert.ok(models.has("codex-a/gpt-5.6-luna"));
  // Provider-level ["openwebui"]: every id of that provider disappears.
  assert.equal([...models].some((m) => m.startsWith("openrouter/")), false);
  // Provider-level ["cc","openwebui"]: a multi-consumer tag that includes cc keeps its rows.
  assert.ok(models.has("codex-b/gpt-5.6-sol"));
  assert.ok(models.has("codex-b/gpt-5.6-luna-high"));
});

// WP-3: for claude-local the declaration, not the tag, decides. `some-future-model-9b` is the
// case that proves the semantics CHANGED rather than merely moved: an id nothing declares used
// to render (absent tag = every consumer) and now renders only because the fixture declares it
// `cc` — see the undeclared-id case below for the other half.
test("claude-local rows come from visibility.json: cc declared in, openwebui-only out", () => {
  const rows = JSON.parse(run([...FIXTURE_ARGS]).stdout);
  const models = new Set(rows.map((r) => r.model));
  // Declared ["cc","openwebui"] and ["cc"]: rows.
  assert.ok(models.has("claude-local/qwen38-27b-iq3s"));
  assert.ok(models.has("claude-local/qwen38-27b-iq3s-low"));
  assert.ok(models.has("claude-local/some-future-model-9b"));
  // Declared ["openwebui"] only: not a row.
  assert.equal(models.has("claude-local/nemotron-4b"), false);
});

test("a claude-local id no declaration entry claims is not a row, and --check says so", (t) => {
  const { dir } = tempHome(t);
  const visibility = JSON.parse(fs.readFileSync(FIXTURE_VISIBILITY, "utf8"));
  delete visibility.models["Fixture-Qwen-27B-UD-IQ3_S-reasoning-low"];
  const file = path.join(dir, "visibility.undeclared.json");
  fs.writeFileSync(file, `${JSON.stringify(visibility, null, 2)}\n`);

  const rows = JSON.parse(run(["--config", FIXTURE_CONFIG, "--visibility", file]).stdout);
  const models = new Set(rows.map((r) => r.model));
  assert.equal(models.has("claude-local/qwen38-27b-iq3s-low"), false);
  assert.equal(rows.length, 10);

  // Drift, not a quiet omission: the two files disagree and --check is the gate that says it.
  const res = run(["--config", FIXTURE_CONFIG, "--visibility", file, "--home", dir, "--check"], 1);
  assert.match(res.stderr, /qwen38-27b-iq3s-low.*no matching `gateway_id`/);
});

test("--check fails when claude-local carries the retired `consumers` key", (t) => {
  const { dir } = tempHome(t);
  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  run([...FIXTURE_ARGS, "--home", dir, "--check"]);

  const config = JSON.parse(fs.readFileSync(FIXTURE_CONFIG, "utf8"));
  config.providers.find((p) => p.name === "claude-local").consumers = { "qwen38-27b-iq3s": ["cc"] };
  const file = path.join(dir, "gateway.consumers.json");
  fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);

  const res = run(["--config", file, "--visibility", FIXTURE_VISIBILITY, "--home", dir, "--check"], 1);
  assert.match(res.stderr, /still carries a `consumers` key/);
});

test("--check fails when a claude-local id has no maxOutputTokens entry (patch 0010's clamp)", (t) => {
  const { dir } = tempHome(t);
  const config = JSON.parse(fs.readFileSync(FIXTURE_CONFIG, "utf8"));
  delete config.providers.find((p) => p.name === "claude-local").maxOutputTokens["nemotron-4b"];
  const file = path.join(dir, "gateway.uncapped.json");
  fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);

  const res = run(["--config", file, "--visibility", FIXTURE_VISIBILITY, "--home", dir, "--check"], 1);
  assert.match(res.stderr, /nemotron-4b.*no `maxOutputTokens` entry/);
});

test("a missing or malformed declaration is fail-closed, never an empty map", (t) => {
  const { dir } = tempHome(t);
  const missing = run([...["--config", FIXTURE_CONFIG, "--visibility", path.join(dir, "nope.json")]], 2);
  assert.match(missing.stderr, /cannot read visibility declaration/);

  const badJson = path.join(dir, "bad.json");
  fs.writeFileSync(badJson, "{not json");
  assert.match(run(["--config", FIXTURE_CONFIG, "--visibility", badJson], 2).stderr, /is not valid JSON/);

  const badSchema = path.join(dir, "schema.json");
  fs.writeFileSync(badSchema, JSON.stringify({ schema: "serving-visibility-v2", models: {} }));
  assert.match(
    run(["--config", FIXTURE_CONFIG, "--visibility", badSchema], 2).stderr,
    /expected serving-visibility-v1/,
  );
});

test("--write preserves unrelated keys and their order, and is idempotent", (t) => {
  const { dir, settings } = tempHome(t);
  const before = { permissions: { allow: ["Bash(ls:*)"] }, statusLine: { type: "command" }, zzz: 1 };
  fs.writeFileSync(settings, `${JSON.stringify(before, null, 2)}\n`);

  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  const first = fs.readFileSync(settings, "utf8");
  const parsed = JSON.parse(first);
  assert.deepEqual(Object.keys(parsed), ["permissions", "statusLine", "zzz", "modelPicker"]);
  assert.deepEqual(parsed.permissions, before.permissions);
  assert.equal(parsed.modelPicker.options.length, 11);
  assert.equal(parsed.modelPicker.replaceBuiltInOptions, undefined);
  assert.equal(parsed.env, undefined);

  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  assert.equal(fs.readFileSync(settings, "utf8"), first, "second --write must produce zero diff");
});

test("non-canonical settings JSON is refused with exit 2, naming --canonicalize, file untouched", (t) => {
  const { dir, settings } = tempHome(t);
  const original = '{"permissions":{"allow":[]}}';
  fs.writeFileSync(settings, original);
  const res = run([...FIXTURE_ARGS, "--home", dir, "--write"], 2);
  assert.match(res.stderr, /not canonical 2-space JSON; refusing to rewrite/);
  assert.match(res.stderr, /--canonicalize/);
  assert.equal(fs.readFileSync(settings, "utf8"), original);
  // --check never rewrites, so the flag is rejected there rather than silently accepted.
  const misuse = run([...FIXTURE_ARGS, "--home", dir, "--check", "--canonicalize"], 2);
  assert.match(misuse.stderr, /--canonicalize needs --write/);
  assert.equal(fs.readFileSync(settings, "utf8"), original);
});

test("--canonicalize reformats a non-canonical file in place, preserving semantic content", (t) => {
  const { dir, settings } = tempHome(t);
  fs.writeFileSync(settings, '{"permissions":{"allow":["Bash(ls:*)"]},\n\t"zzz":1}');
  run([...FIXTURE_ARGS, "--home", dir, "--write", "--canonicalize"]);
  const written = fs.readFileSync(settings, "utf8");
  const parsed = JSON.parse(written);
  // Canonical serialisation, unrelated keys and their order intact, rows applied.
  assert.equal(written, `${JSON.stringify(parsed, null, 2)}\n`);
  assert.deepEqual(Object.keys(parsed), ["permissions", "zzz", "modelPicker"]);
  assert.deepEqual(parsed.permissions, { allow: ["Bash(ls:*)"] });
  assert.equal(parsed.zzz, 1);
  assert.equal(parsed.modelPicker.options.length, 11);
  run([...FIXTURE_ARGS, "--home", dir, "--check"]);
});

test("--canonicalize on an already-canonical file is byte-identical to --write without it", (t) => {
  const { dir, settings } = tempHome(t);
  const before = `${JSON.stringify({ permissions: { allow: [] }, zzz: 1 }, null, 2)}\n`;

  fs.writeFileSync(settings, before);
  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  const plain = fs.readFileSync(settings, "utf8");

  fs.writeFileSync(settings, before);
  run([...FIXTURE_ARGS, "--home", dir, "--write", "--canonicalize"]);
  assert.equal(fs.readFileSync(settings, "utf8"), plain, "--canonicalize must not change canonical output");

  // And it stays a no-op on a second pass over its own output.
  run([...FIXTURE_ARGS, "--home", dir, "--write", "--canonicalize"]);
  assert.equal(fs.readFileSync(settings, "utf8"), plain);
});

test("--check is 1 when stale and 0 when fresh", (t) => {
  const { dir, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ modelPicker: { options: [] } }, null, 2)}\n`);
  const stale = run([...FIXTURE_ARGS, "--home", dir, "--check"], 1);
  assert.match(stale.stderr, /modelPicker\.options stale/);
  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  run([...FIXTURE_ARGS, "--home", dir, "--check"]);
});

test("--launch-profile writes the router env keys and never a token key", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: { FOO: "bar" } }, null, 2)}\n`);
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  assert.equal(parsed.env.FOO, "bar");
  assert.equal(parsed.env.ANTHROPIC_BASE_URL, "http://127.0.0.1:4813");
  assert.equal(parsed.env.ANTHROPIC_CUSTOM_HEADERS, `x-gateway-key: ${DUMMY_KEY}`);
  assert.equal(parsed.env.ANTHROPIC_AUTH_TOKEN, undefined);
  assert.equal(parsed.env.ANTHROPIC_API_KEY, undefined);
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--check", "--launch-profile"]);
});

test("a pre-existing ANTHROPIC_AUTH_TOKEN is left untouched with a warning", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: { ANTHROPIC_AUTH_TOKEN: "preexisting" } }, null, 2)}\n`);
  const res = run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  assert.match(res.stderr, /env\.ANTHROPIC_AUTH_TOKEN already present/);
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  assert.equal(parsed.env.ANTHROPIC_AUTH_TOKEN, "preexisting");
  assert.equal(parsed.env.ANTHROPIC_BASE_URL, "http://127.0.0.1:4813");
});

test("a missing gateway key is exit 2 and writes nothing", (t) => {
  const { dir, settings } = tempHome(t);
  const emptyEnv = path.join(dir, "empty.env");
  fs.writeFileSync(emptyEnv, "OTHER=1\n");
  const res = run([...FIXTURE_ARGS, "--home", dir, "--env-file", emptyEnv, "--write", "--launch-profile"], 2);
  assert.match(res.stderr, /INFERENCE_ROUTER_GATEWAY_KEY is missing or empty/);
  assert.equal(fs.existsSync(settings), false);
});

test("--launch-profile adds the tool-search and title keys when the input lacks them", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: { FOO: "bar" } }, null, 2)}\n`);
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const env = JSON.parse(fs.readFileSync(settings, "utf8")).env;
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");
  assert.equal(env.FOO, "bar");
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--check", "--launch-profile"]);
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
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const first = fs.readFileSync(settings, "utf8");
  const env = JSON.parse(first).env;
  // No duplication: the four generated keys, in their original order, and nothing added.
  assert.deepEqual(Object.keys(env), Object.keys(before.env));
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");

  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  assert.equal(fs.readFileSync(settings, "utf8"), first, "second --write must produce zero diff");
});

test("a drifted generated env key fails --check --launch-profile and --write restores it", (t) => {
  const { dir, envFile, settings } = tempHome(t);
  fs.writeFileSync(settings, `${JSON.stringify({ env: {} }, null, 2)}\n`);
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);

  const drifted = JSON.parse(fs.readFileSync(settings, "utf8"));
  drifted.env.ENABLE_TOOL_SEARCH = "false";
  delete drifted.env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE;
  fs.writeFileSync(settings, `${JSON.stringify(drifted, null, 2)}\n`);

  const stale = run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--check", "--launch-profile"], 1);
  assert.match(stale.stderr, /env\.ENABLE_TOOL_SEARCH .* is "false", expected "true"/);

  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const env = JSON.parse(fs.readFileSync(settings, "utf8")).env;
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");
});
