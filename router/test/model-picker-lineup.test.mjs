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
const FIXTURE_CATALOG = path.join(HERE, "fixtures", "catalog.fixture.json");
// Every fixture run pins BOTH files: ids and visibility come from the catalog
// (plans/serving-control-redesign.md WP-3), so a run that named only --config would silently
// read the repo's own catalog against a fixture config.
const FIXTURE_ARGS = ["--config", FIXTURE_CONFIG, "--catalog", FIXTURE_CATALOG];
const REAL_CONFIG = path.join(HERE, "..", "gateway.config.json");
const REAL_CATALOG = path.join(HERE, "..", "catalog", "catalog.json");
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

// The real catalog's `consumers` lists are OPERATOR STATE: the serving dispatcher rewrites them
// on every dashboard `set-visibility`, and the user toggles them by hand. So this test never pins
// which ids are rows, nor how many — only what holds for every valid visibility choice: the
// rendered rows are exactly the catalog's Claude-Code-visible set, and the append-only/tombstone
// id contracts hold. The exact-lineup and label behaviour is pinned against the fixture catalog.
test("the real config renders exactly the catalog's Claude-Code-visible ids, whatever they are", () => {
  const rows = JSON.parse(run(["--config", REAL_CONFIG, "--catalog", REAL_CATALOG]).stdout);
  const config = JSON.parse(fs.readFileSync(REAL_CONFIG, "utf8"));
  const catalog = JSON.parse(fs.readFileSync(REAL_CATALOG, "utf8"));
  const models = rows.map((r) => r.model);
  assert.equal(new Set(models).size, models.length, "no duplicate picker rows");
  assert.equal(models.filter((m) => m.startsWith("anthropic/")).length, 0);
  // Derive the expected set from the live catalog itself: an explicit `cc` tag is a row on every
  // non-anthropic provider the static config serves; an explicit tag list without `cc` never is.
  // (An absent `consumers` — legal off claude-local — is covered by the fixture tests.)
  const served = new Set((config.providers ?? []).map((p) => p.name).filter((n) => n !== "anthropic"));
  const expected = new Set();
  const excluded = new Set();
  for (const [name, provider] of Object.entries(catalog.providers)) {
    if (!served.has(name)) continue;
    for (const e of provider.models ?? []) {
      if (!Array.isArray(e.consumers)) continue;
      (e.consumers.includes("cc") ? expected : excluded).add(`${name}/${e.id}`);
    }
  }
  for (const m of expected) assert.ok(models.includes(m), `${m} is cc-tagged in the catalog but is not a picker row`);
  for (const m of excluded) assert.equal(models.includes(m), false, `${m} is not cc-tagged but is a picker row`);
  for (const row of rows) {
    assert.ok(row.description.length < 90, `description too long: ${row.description}`);
    // A bare codex id (whichever are visible) takes its effort from the harness.
    if (/^codex-[^/]+\/(?!.*-(low|medium|high)$)/.test(row.model)) assert.match(row.description, /effort via the harness/);
  }
  // Alias contract (append-only): visibility may hide an id, never remove it — the qwen38 effort
  // ids and the codex suffix ids stay RESOLVABLE in their provider's models[] (an id dropped from
  // there 400s at model_resolution). /v1/models on the live router is smoke-router.sh check 1's job.
  const idsOf = (name) => catalog.providers[name].models.map((e) => e.id);
  for (const effort of ["off", "low", "medium", "high"]) {
    assert.ok(
      idsOf("claude-local").includes(`qwen38-27b-iq3s-${effort}`),
      `effort id ${effort} must stay resolvable in claude-local.models[]`,
    );
  }
  const CODEX_SUFFIX_IDS = [
    "gpt-5.6-luna-medium",
    "gpt-5.6-luna-high",
    "gpt-5.6-sol-low",
    "gpt-5.6-sol-high",
    "gpt-6-astra-low",
    "gpt-6-astra-high",
    "gpt-6-sol-low",
    "gpt-6-sol-high",
    "gpt-6.1-sol-low",
    "gpt-6.1-sol-high",
    "gpt-6-luna-medium",
    "gpt-6-luna-high",
  ];
  for (const persona of ["codex-a", "codex-b"]) {
    for (const id of CODEX_SUFFIX_IDS) {
      assert.ok(idsOf(persona).includes(id), `${persona}/${id} must stay resolvable in models[]`);
    }
  }
  // TOMBSTONE GATE (user decision 2026-09-14, router/AGENTS.md): z-ai/glm-5.2:free and z-ai/glm-5.3
  // were retired from serving. Assert they are gone from the CATALOG entirely, on both legs, so a
  // future edit cannot quietly re-add one. The names stay reserved forever; re-adding either string
  // for different weights is the append-only violation this gate exists to catch.
  for (const legName of ["openrouter", "or-anthropic"]) {
    for (const id of ["z-ai/glm-5.2:free", "z-ai/glm-5.3"]) {
      assert.equal(idsOf(legName).includes(id), false, `retired id ${legName}/${id} must stay removed`);
    }
  }
  // The conversion leg keeps a resolvable id: an empty models[] is a WILDCARD on a credentialed
  // OR provider.
  assert.ok(idsOf("openrouter").length > 0, "openrouter models[] must never be empty (wildcard)");
});

test("consumer tags still govern every provider EXCEPT claude-local, unchanged", () => {
  const rows = JSON.parse(run([...FIXTURE_ARGS]).stdout);
  const models = new Set(rows.map((r) => r.model));
  // Untagged provider: unchanged, every id renders (the pre-tag behaviour of every provider).
  assert.ok(models.has("or-anthropic/z-ai/glm-5.3"));
  assert.ok(models.has("codex-a/gpt-5.6-luna"));
  // ["openwebui"] on every id of a provider: every id of that provider disappears.
  assert.equal([...models].some((m) => m.startsWith("openrouter/")), false);
  // ["cc","openwebui"]: a multi-consumer tag that includes cc keeps its rows.
  assert.ok(models.has("codex-b/gpt-5.6-sol"));
  assert.ok(models.has("codex-b/gpt-5.6-luna-high"));
});

// For claude-local an absent tag is NOT "every consumer". `some-future-model-9b` is the case that
// proves it: it renders only because the fixture catalog declares it `cc` — see the
// undeclared-id case below for the other half.
test("claude-local rows come from declared catalog consumers: cc declared in, openwebui-only out", () => {
  const rows = JSON.parse(run([...FIXTURE_ARGS]).stdout);
  const models = new Set(rows.map((r) => r.model));
  // Declared ["cc","openwebui"] and ["cc"]: rows.
  assert.ok(models.has("claude-local/qwen38-27b-iq3s"));
  assert.ok(models.has("claude-local/qwen38-27b-iq3s-low"));
  assert.ok(models.has("claude-local/some-future-model-9b"));
  // Declared ["openwebui"] only: not a row.
  assert.equal(models.has("claude-local/nemotron-4b"), false);
});

test("a claude-local id with no declared consumers is not a row, and --check says so", (t) => {
  const { dir } = tempHome(t);
  const catalog = JSON.parse(fs.readFileSync(FIXTURE_CATALOG, "utf8"));
  const low = catalog.providers["claude-local"].models.find((e) => e.id === "qwen38-27b-iq3s-low");
  delete low.consumers;
  delete low.modelKey;
  const file = path.join(dir, "catalog.undeclared.json");
  fs.writeFileSync(file, `${JSON.stringify(catalog, null, 2)}\n`);

  const rows = JSON.parse(run(["--config", FIXTURE_CONFIG, "--catalog", file]).stdout);
  const models = new Set(rows.map((r) => r.model));
  assert.equal(models.has("claude-local/qwen38-27b-iq3s-low"), false);
  assert.equal(rows.length, 10);

  // Drift, not a quiet omission: --check is the gate that says it.
  const res = run(["--config", FIXTURE_CONFIG, "--catalog", file, "--home", dir, "--check"], 1);
  assert.match(res.stderr, /qwen38-27b-iq3s-low.*no declared `consumers`/);
});

test("--check fails when the static config carries a retired per-model key", (t) => {
  const { dir } = tempHome(t);
  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  run([...FIXTURE_ARGS, "--home", dir, "--check"]);

  for (const [key, value] of [["consumers", { "qwen38-27b-iq3s": ["cc"] }], ["models", ["qwen38-27b-iq3s"]]]) {
    const config = JSON.parse(fs.readFileSync(FIXTURE_CONFIG, "utf8"));
    config.providers.find((p) => p.name === "claude-local")[key] = value;
    const file = path.join(dir, `gateway.${key}.json`);
    fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
    const res = run(["--config", file, "--catalog", FIXTURE_CATALOG, "--home", dir, "--check"], 1);
    assert.match(res.stderr, new RegExp(`still carries a \`${key}\` key`));
  }
});

test("--check fails when a claude-local id has no maxOutputTokens entry (the local clamp)", (t) => {
  const { dir } = tempHome(t);
  const catalog = JSON.parse(fs.readFileSync(FIXTURE_CATALOG, "utf8"));
  delete catalog.providers["claude-local"].models.find((e) => e.id === "nemotron-4b").maxOutputTokens;
  const file = path.join(dir, "catalog.uncapped.json");
  fs.writeFileSync(file, `${JSON.stringify(catalog, null, 2)}\n`);

  const res = run(["--config", FIXTURE_CONFIG, "--catalog", file, "--home", dir, "--check"], 1);
  assert.match(res.stderr, /nemotron-4b.*no `maxOutputTokens` entry/);
});

test("reverse drift: a catalog provider no static provider serves fails --check", (t) => {
  const { dir } = tempHome(t);
  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  const catalog = JSON.parse(fs.readFileSync(FIXTURE_CATALOG, "utf8"));
  catalog.providers["codex-gone"] = { models: [{ id: "gpt-x", consumers: ["cc"] }] };
  const file = path.join(dir, "catalog.orphan.json");
  fs.writeFileSync(file, `${JSON.stringify(catalog, null, 2)}\n`);
  // Not a row: rows follow the static providers only.
  const rows = JSON.parse(run(["--config", FIXTURE_CONFIG, "--catalog", file]).stdout);
  assert.equal(rows.some((r) => r.model.startsWith("codex-gone/")), false);
  const res = run(["--config", FIXTURE_CONFIG, "--catalog", file, "--home", dir, "--check"], 1);
  assert.match(res.stderr, /codex-gone.*no provider .* serves: codex-gone\/gpt-x/);
});

test("a missing or malformed catalog is fail-closed, never an empty map", (t) => {
  const { dir } = tempHome(t);
  const missing = run([...["--config", FIXTURE_CONFIG, "--catalog", path.join(dir, "nope.json")]], 2);
  assert.match(missing.stderr, /cannot read model catalog/);

  const badJson = path.join(dir, "bad.json");
  fs.writeFileSync(badJson, "{not json");
  assert.match(run(["--config", FIXTURE_CONFIG, "--catalog", badJson], 2).stderr, /is not valid JSON/);

  const badSchema = path.join(dir, "schema.json");
  fs.writeFileSync(badSchema, JSON.stringify({ schema: "model-catalog-v2", providers: {} }));
  assert.match(
    run(["--config", FIXTURE_CONFIG, "--catalog", badSchema], 2).stderr,
    /expected model-catalog-v1/,
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

test("--write --quiet omits only the already-current line; a real write still prints", (t) => {
  const { dir } = tempHome(t);
  const first = run([...FIXTURE_ARGS, "--home", dir, "--write", "--quiet"]);
  assert.match(first.stdout, /wrote \d+ rows to /);
  const second = run([...FIXTURE_ARGS, "--home", dir, "--write", "--quiet"]);
  assert.equal(second.stdout, "");
  assert.equal(second.stderr, "");
  const loud = run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  assert.match(loud.stdout, /already current/);
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

test("Python ensure_ascii escapes (\\uXXXX for non-ASCII) still count as canonical 2-space JSON", (t) => {
  // Fleet installers rewrite settings.json with Python's json.dump, which escapes non-ASCII;
  // the first live serving-control apply failed on exactly this (2026-09-14).
  const { dir, settings } = tempHome(t);
  const before = `${JSON.stringify({ permissions: { allow: ["Bash(ls:*)"] }, note: "a \u2014 b \u00e9" }, null, 2)}\n`
    .replace(/[\u0080-\uffff]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`);
  assert.match(before, /\\u2014/);
  fs.writeFileSync(settings, before);
  run([...FIXTURE_ARGS, "--home", dir, "--write"]);
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  assert.equal(parsed.note, "a \u2014 b \u00e9");
  assert.equal(parsed.modelPicker.options.length, 11);
  run([...FIXTURE_ARGS, "--home", dir, "--check"]);
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
  assert.equal(env.CLAUDE_CODE_MAX_CONTEXT_TOKENS, "258400", "the codex-leg window is declared (sidecar clamp)");
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
      CLAUDE_CODE_MAX_CONTEXT_TOKENS: "258400",
    },
  };
  fs.writeFileSync(settings, `${JSON.stringify(before, null, 2)}\n`);
  run([...FIXTURE_ARGS, "--home", dir, "--env-file", envFile, "--write", "--launch-profile"]);
  const first = fs.readFileSync(settings, "utf8");
  const env = JSON.parse(first).env;
  // No duplication: the five generated keys, in their original order, and nothing added.
  assert.deepEqual(Object.keys(env), Object.keys(before.env));
  assert.equal(env.ENABLE_TOOL_SEARCH, "true");
  assert.equal(env.CLAUDE_CODE_DISABLE_TERMINAL_TITLE, "1");
  assert.equal(env.CLAUDE_CODE_MAX_CONTEXT_TOKENS, "258400");

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

// fleet-env store (fleet-tools plans/env-management.md): a stubbed fleet-env binary only;
// no env file is read by these tests (the key comes from the stub or FLEET_ENV_RUN's environment).
function runFleetEnv(t, extraEnv, args) {
  const { dir, settings } = tempHome(t);
  const stub = path.join(dir, "fleet-env");
  fs.writeFileSync(stub, `#!/usr/bin/env bash
echo "$1 $2" >> "${dir}/calls"
case "$1" in
  run) shift 3; INFERENCE_ROUTER_GATEWAY_KEY=${DUMMY_KEY} FLEET_ENV_RUN=model-picker exec "$@" ;;
esac
exit 2
`);
  fs.chmodSync(stub, 0o755);
  const { spawnSync } = process.getBuiltinModule("node:child_process");
  const r = spawnSync(process.execPath, [SCRIPT, ...FIXTURE_ARGS, "--home", dir, ...args], {
    encoding: "utf8",
    env: { PATH: process.env.PATH, HOME: dir, FLEET_ENV_BIN: stub, ...extraEnv },
  });
  const calls = fs.existsSync(path.join(dir, "calls")) ? fs.readFileSync(path.join(dir, "calls"), "utf8") : "";
  return { ...r, calls, settings };
}

test("fleet-env store: --launch-profile re-execs under fleet-env run model-picker", (t) => {
  const r = runFleetEnv(t, {}, ["--write", "--launch-profile"]);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.calls, "run model-picker\n");
  const parsed = JSON.parse(fs.readFileSync(r.settings, "utf8"));
  assert.equal(parsed.env.ANTHROPIC_CUSTOM_HEADERS, `x-gateway-key: ${DUMMY_KEY}`);
  assert.ok(!r.stdout.includes(DUMMY_KEY) && !r.stderr.includes(DUMMY_KEY));
});

test("fleet-env store: bare --write never consults fleet-env", (t) => {
  const r = runFleetEnv(t, {}, ["--write"]);
  assert.equal(r.status, 0, r.stderr);
  assert.equal(r.calls, "");
});

test("fleet-env store: under fleet-env run an empty key fails closed", (t) => {
  const r = runFleetEnv(t, { FLEET_ENV_RUN: "model-picker", INFERENCE_ROUTER_GATEWAY_KEY: "" }, ["--write", "--launch-profile"]);
  assert.equal(r.status, 2);
  assert.match(r.stderr, /missing or empty in the fleet-env environment/);
});

test("fleet-env store: with the binary missing fails closed", (t) => {
  const r = runFleetEnv(t, { FLEET_ENV_BIN: "/nonexistent/fleet-env" }, ["--write", "--launch-profile"]);
  assert.equal(r.status, 1);
  assert.match(r.stderr, /is missing/);
});
