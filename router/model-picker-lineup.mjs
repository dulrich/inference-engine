#!/usr/bin/env bun
// Generate Claude Code `modelPicker.options` rows from the router's model catalog.
//
// The catalog (`router/catalog/catalog.json`, schema `model-catalog-v1`) is the single source of
// truth for the id list and for visibility: there is no hand-maintained list here, only the
// family/label table below. Every `<provider>/<model>` id in the catalog becomes one picker row,
// except the hosted `anthropic` provider (its ids duplicate Claude Code's built-in rows, and the
// bare id is what Claude Code sends anyway) and any id Claude Code is not a declared consumer of.
// Provider order is the static `gateway.config.json`'s (the gateway's routing order).
// `replaceBuiltInOptions` is never written, so the built-in subscription rows stay first.
//
// VISIBILITY (plans/serving-control-redesign.md WP-3): every catalog entry may carry `consumers`.
// On `claude-local` it is REQUIRED (each entry carries the `llama/config.yaml` `modelKey` the
// serving dispatcher writes it by), and absent there is drift, not "every consumer". On every
// other provider an absent `consumers` means every consumer (see CLAUDE_CODE_CONSUMER below).
//
// Usage:
//   model-picker-lineup.mjs [--config <path>] [--catalog <path>] [--home <dir>]
//                           [--env-file <path>]   (default: the environment via
//                                                  `fleet-env run model-picker`)
//                           [--check | --write [--launch-profile] [--canonicalize]]
//
// No mode flag prints the rows array as JSON to stdout.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { fleetEnvReexec } from "./fleet-env.mjs";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG = path.join(SCRIPT_DIR, "gateway.config.json");
const DEFAULT_CATALOG = path.join(SCRIPT_DIR, "catalog", "catalog.json");
const GATEWAY_KEY_VAR = "INFERENCE_ROUTER_GATEWAY_KEY";
const ROUTER_URL = "http://127.0.0.1:4813";

// Providers whose ids never become picker rows.
const EXCLUDED_PROVIDERS = new Set(["anthropic"]);
// Consumer tagging (plans/local-leg-claude-code-serving-fit.md WP-2; moved onto catalog entries in
// plans/serving-control-redesign.md WP-2/WP-3). `consumers` is an optional per-entry list in the
// catalog. ABSENT MEANS EVERY CONSUMER on every provider except `claude-local` (DECLARED_PROVIDER),
// so the annotation stays additive. A row is emitted only when the id is untagged or carries this
// tag. Motivation: the OpenRouter legs and the codex effort-suffix ids are other consumers'
// routes, not Claude Code's, and every one of them used to land in the `/model` picker.
const CLAUDE_CODE_CONSUMER = "cc";
// Provider name -> the word used as the "leg" in a label.
const LEG_NAMES = { "claude-local": "local" };
// The one provider whose entries must declare `consumers` (and a `modelKey`): the serving
// dispatcher writes them. Same string the LEG_NAMES table and the label rules already key off.
const DECLARED_PROVIDER = "claude-local";
// The catalog schema this generator understands.
const CATALOG_SCHEMA = "model-catalog-v1";
// Per-model keys retired from gateway.config.json (they live in the catalog since WP-2/WP-3).
const RETIRED_CONFIG_KEYS = ["models", "effortKwargs", "maxOutputTokens", "consumers"];
// Credential env keys the generator must never write (OAuth stays the credential).
const FORBIDDEN_ENV_KEYS = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"];
// Static env keys the generator owns under --launch-profile, alongside the dynamic
// ANTHROPIC_BASE_URL / ANTHROPIC_CUSTOM_HEADERS pair. ENABLE_TOOL_SEARCH must be set explicitly:
// a custom ANTHROPIC_BASE_URL auto-disables tool search, which loads every tool schema up front
// instead of deferring (~10k tokens of assembly per session). CLAUDE_CODE_DISABLE_TERMINAL_TITLE
// suppresses the background terminal-title model call, which behind a custom base URL is not
// guaranteed to honour the haiku alias (anthropics/claude-code#92050, closed as not planned).
// CLAUDE_CODE_MAX_CONTEXT_TOKENS declares the window for gateway model ids Claude Code does not
// recognise (every `codex-*/...` row): without it the harness assumes 200,000 and trips its
// "Context limit reached" gate near 178k while the codex thread could hold far more. 258,400 is the
// sidecar's own clamp (`SIDECAR_CONTEXT_WINDOW_TOKENS`, 95 % of the 272,000 window every model in
// both persona catalogs declares — measured 2026-09-14), so the meter and the wire agree on one
// ceiling. The env is global, so the local leg's smaller windows (120k–131k `-c`) are over-declared
// too — they already were at the 200k default; llama's own length error remains their backstop.
const LAUNCH_PROFILE_ENV = {
  ENABLE_TOOL_SEARCH: "true",
  CLAUDE_CODE_DISABLE_TERMINAL_TITLE: "1",
  CLAUDE_CODE_MAX_CONTEXT_TOKENS: "258400",
};

// Family/label table. `prefix` families may carry an effort suffix (`-low|-medium|-high`, plus
// `-off` on the local leg); without one the family's own default applies. On the codex leg that
// is a LABELLING rule only — a hand-typed suffix id still renders `GPT-5.6 Luna high (codex-b)` — not
// the selector: effort there is resolved per turn by the sidecar under the precedence rule
// (suffix > `output_config.effort`/`thinking` > family default), see
// `docs/tool-calling-gateway/codex-sidecar.md` "Effort precedence" and router/AGENTS.md, "The
// codex sidecar leg".
const FAMILIES = [
  // Codex families (WP-2, plans/codex-leg-effort-and-auditor-unpinning.md): the six suffix ids per
  // persona are tagged `["router"]` in gateway.config.json, so only the bare id is a picker row and
  // the harness's own effort control (`/model` effort, `--effort`, agent frontmatter `effort:`,
  // `modelSettings.effortLevel`) chooses the level. `defaultEffort` is what applies when the
  // request carries no effort at all — it still labels the bare row, and it still labels a
  // hand-typed suffix id from its suffix.
  { kind: "prefix", id: "gpt-5.6-luna", family: "GPT-5.6 Luna", defaultEffort: "low" },
  { kind: "prefix", id: "gpt-5.6-sol", family: "GPT-5.6 Sol", defaultEffort: "medium" },
  { kind: "prefix", id: "gpt-6-astra", family: "GPT-6 Astra", defaultEffort: "medium" },
  { kind: "prefix", id: "gpt-6-sol", family: "GPT-6 Sol", defaultEffort: "medium" },
  { kind: "prefix", id: "gpt-6.1-sol", family: "GPT-6.1 Sol", defaultEffort: "medium" },
  { kind: "prefix", id: "gpt-6-luna", family: "GPT-6 Luna", defaultEffort: "low" },
  // glm-5.2:free and glm-5.3 were RETIRED from serving 2026-09-14 (user decision) — see the
  // TOMBSTONE in router/AGENTS.md. Their label rows stay: this table is a label lookup, not the
  // alias registry, the fixture suite still exercises both ids, and keeping them here records the
  // reserved names in the one file that enumerates model families. The NAMES are reserved forever
  // under the append-only contract — never re-point either string at different weights.
  { kind: "exact", id: "z-ai/glm-5.2:free", family: "GLM 5.2 free" },
  { kind: "exact", id: "z-ai/glm-5.3", family: "GLM 5.3" },
  // Exact, not prefix: a prefix rule on `z-ai/glm-5.3` would swallow this id's label.
  { kind: "exact", id: "z-ai/glm-5.3-flash", family: "GLM 5.3 Flash" },
  { kind: "exact", id: "qwen/qwen3.8-flash", family: "Qwen3.8 Flash" },
  { kind: "exact", id: "qwen36-a3b", family: "Qwen3.6 A3B" },
  // Local-leg effort ids (WP-2): on this leg the id suffix IS the effort selector, because the
  // router's Anthropic->OpenAI conversion leg forwards no per-request effort field of any shape
  // (WP-1 spike, probe 3) — each suffixed id selects a llama-swap entry whose own `cmd:` carries
  // the reasoning flags. The unsuffixed id is deliberately NOT one of the four levels: it is the
  // default entry carrying the runaway-output backstop (`--reasoning-budget 8192`), between
  // `-medium` (4096) and `-high` (unrestricted). Its `defaultEffort` is therefore the word
  // "default" rather than a level word — which is also what keeps its picker label distinct from
  // the `-high` row's, the collision the codex families avoid by never listing an id for the
  // persona's own default level.
  { kind: "prefix", id: "qwen38-27b-iq3s", family: "Qwen3.8 27B", defaultEffort: "default" },
  { kind: "exact", id: "nemotron-4b", family: "Nemotron 4B" },
];

function die(message, code = 2) {
  process.stderr.write(`model-picker-lineup: ${message}\n`);
  process.exit(code);
}

function parseArgs(argv) {
  const opts = {
    config: DEFAULT_CONFIG,
    catalog: DEFAULT_CATALOG,
    home: os.homedir(),
    envFile: null, // null = default source (see gatewayKeySource)
    mode: "print",
    launchProfile: false,
    canonicalize: false,
    quiet: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const value = () => {
      const v = argv[i + 1];
      if (v === undefined) die(`${arg} needs a value`);
      i += 1;
      return v;
    };
    if (arg === "--config") opts.config = value();
    else if (arg === "--catalog") opts.catalog = value();
    else if (arg === "--home") opts.home = value();
    else if (arg === "--env-file") opts.envFile = value();
    else if (arg === "--check") opts.mode = "check";
    else if (arg === "--write") opts.mode = "write";
    else if (arg === "--launch-profile") opts.launchProfile = true;
    else if (arg === "--canonicalize") opts.canonicalize = true;
    else if (arg === "--quiet") opts.quiet = true;
    else if (arg === "--help" || arg === "-h") {
      process.stdout.write(
        "usage: model-picker-lineup.mjs [--config <path>] [--catalog <path>] [--home <dir>] [--env-file <path>] [--check | --write [--launch-profile] [--canonicalize] [--quiet]]\n",
      );
      process.exit(0);
    } else die(`unknown argument: ${arg}`);
  }
  if (opts.mode === "print" && opts.launchProfile) die("--launch-profile needs --check or --write");
  // --check never rewrites anything, so canonicalisation has nothing to act on there.
  if (opts.mode !== "write" && opts.canonicalize) die("--canonicalize needs --write");
  return opts;
}

function describeFamily(modelId) {
  for (const entry of FAMILIES) {
    if (entry.kind === "exact" && modelId === entry.id) return entry;
    if (entry.kind === "prefix" && modelId.startsWith(entry.id)) return entry;
  }
  // Unknown ids are the norm — the config is additive — so render the bare id, never fail.
  return { kind: "unknown", family: modelId };
}

function effortOf(modelId, entry) {
  if (entry.kind !== "prefix") return undefined;
  // `off` is a local-leg level only (llama-server's `--reasoning off`); no codex id carries it.
  const match = /-(off|low|medium|high)$/.exec(modelId);
  return match ? match[1] : entry.defaultEffort;
}

function describeRow(providerName, leg, modelId, effort) {
  if (providerName.startsWith("codex-")) {
    // Bare id: effort comes from the harness and is resolved per turn by the sidecar; the family
    // default applies only when the request carries none. A hand-typed suffix id still wins.
    // (Descriptions are asserted < 90 chars by the test, hence the compressed wording.)
    const tail = /-(low|medium|high)$/.test(modelId)
      ? `effort ${effort} is in the id suffix`
      : `effort via the harness, default ${effort}`;
    return `${providerName} sidecar leg via the router (:4813); ${tail}`;
  }
  if (providerName === "claude-local") {
    if (!effort) return "Local GPU tier via the router (:4813); claude-local leg";
    return /-(off|low|medium|high)$/.test(modelId)
      ? `Local GPU tier via the router (:4813); reasoning ${effort} is in the id suffix`
      : "Local GPU tier via the router (:4813); no suffix means the default budget";
  }
  if (providerName === "openrouter") return "OpenRouter via the router (:4813); OpenAI-compat conversion leg";
  if (providerName === "or-anthropic") return "OpenRouter via the router (:4813); native Anthropic leg";
  return `${providerName} leg via the router (:4813)`;
}

// The catalog entries of one static provider ([] when the catalog has none).
function catalogEntries(catalog, providerName) {
  const models = catalog.providers?.[providerName]?.models;
  return Array.isArray(models) ? models.filter((e) => e && typeof e.id === "string") : [];
}

function isClaudeCodeModel(providerName, entry) {
  const tags = Array.isArray(entry.consumers) ? entry.consumers : null;
  // claude-local: an undeclared entry is drift (--check reports it), never "every consumer", so a
  // stale catalog can never silently add a row.
  if (providerName === DECLARED_PROVIDER) return tags !== null && tags.includes(CLAUDE_CODE_CONSUMER);
  return tags === null || tags.includes(CLAUDE_CODE_CONSUMER);
}

export function buildRows(config, catalog) {
  const rows = [];
  for (const provider of config.providers ?? []) {
    if (!provider || typeof provider.name !== "string") continue;
    if (EXCLUDED_PROVIDERS.has(provider.name)) continue;
    const leg = LEG_NAMES[provider.name] ?? provider.name;
    for (const catalogEntry of catalogEntries(catalog, provider.name)) {
      const modelId = catalogEntry.id;
      if (!isClaudeCodeModel(provider.name, catalogEntry)) continue;
      const entry = describeFamily(modelId);
      const effort = effortOf(modelId, entry);
      // Codex bare ids carry the proper model name only (user, 2026-09-13): effort is the
      // harness's per-turn control there, so the family default stays out of the label and
      // lives in the description. A hand-typed suffix id still shows its pinned level.
      const labelEffort = provider.name.startsWith("codex-") && !/-(low|medium|high)$/.test(modelId) ? undefined : effort;
      const label = `${[entry.family, labelEffort].filter(Boolean).join(" ")} (${leg})`;
      rows.push({
        model: `${provider.name}/${modelId}`,
        label,
        description: describeRow(provider.name, leg, modelId, effort),
      });
    }
  }
  return rows;
}

function readConfig(configPath) {
  let raw;
  try {
    raw = fs.readFileSync(configPath, "utf8");
  } catch {
    die(`cannot read config ${configPath}`);
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    die(`config ${configPath} is not valid JSON: ${err.message}`);
  }
}

// Fail-closed: an unreadable or malformed catalog is an error, never an empty map. Silently
// treating it as empty would drop every row from the picker and report "in sync".
function readCatalog(catalogPath) {
  let raw;
  try {
    raw = fs.readFileSync(catalogPath, "utf8");
  } catch {
    die(`cannot read model catalog ${catalogPath}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    die(`model catalog ${catalogPath} is not valid JSON: ${err.message}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    die(`model catalog ${catalogPath} is not a JSON object`);
  }
  if (parsed.schema !== CATALOG_SCHEMA) {
    die(`model catalog ${catalogPath} carries schema ${JSON.stringify(parsed.schema ?? null)}, expected ${CATALOG_SCHEMA}`);
  }
  if (parsed.providers === null || typeof parsed.providers !== "object" || Array.isArray(parsed.providers)) {
    die(`model catalog ${catalogPath} has no \`providers\` map`);
  }
  return parsed;
}

// The drift modes between the static config and the catalog, both directions:
//   - a retired per-model key reappearing in gateway.config.json (the primitive the catalog
//     replaced; the gateway's strict loader refuses it too);
//   - REVERSE: a catalog provider no static provider serves — its ids and `consumers` name routes
//     nothing serves;
//   - a claude-local entry with no declared `consumers`/`modelKey`, or no `maxOutputTokens` (the
//     local leg's clamp reads it; value = the key's `max_output.answer_tokens`).
function configDriftFailure(config, catalog, configPath, catalogPath) {
  for (const provider of config.providers ?? []) {
    const retired = RETIRED_CONFIG_KEYS.filter((k) => provider?.[k] !== undefined);
    if (retired.length > 0) {
      return `provider ${provider.name} in ${configPath} still carries a \`${retired[0]}\` key — per-model data lives in ${catalogPath} since WP-3; delete the key`;
    }
  }
  const served = new Set((config.providers ?? []).map((p) => p?.name));
  const orphans = Object.keys(catalog.providers).filter((name) => !served.has(name));
  if (orphans.length > 0) {
    const ids = orphans.flatMap((name) => catalogEntries(catalog, name).map((e) => `${name}/${e.id}`));
    return `catalog provider(s) ${orphans.join(", ")} in ${catalogPath} name id(s) no provider in ${configPath} serves: ${ids.join(", ") || "(none)"}`;
  }
  const local = catalogEntries(catalog, DECLARED_PROVIDER);
  const undeclared = local.filter((e) => !Array.isArray(e.consumers) || typeof e.modelKey !== "string").map((e) => e.id);
  if (undeclared.length > 0) {
    return `${DECLARED_PROVIDER} model id(s) ${undeclared.join(", ")} have no declared \`consumers\`/\`modelKey\` in ${catalogPath}`;
  }
  const uncapped = local.filter((e) => typeof e.maxOutputTokens !== "number").map((e) => e.id);
  if (uncapped.length > 0) {
    return `${DECLARED_PROVIDER} model id(s) ${uncapped.join(", ")} have no \`maxOutputTokens\` entry in ${catalogPath} — the local leg's clamp reads it (value = the key's \`max_output.answer_tokens\`)`;
  }
  return null;
}

function readGatewayKey(envFile) {
  let raw;
  try {
    raw = fs.readFileSync(envFile, "utf8");
  } catch {
    die(`cannot read env file ${envFile}`);
  }
  let value = "";
  for (const line of raw.split("\n")) {
    const match = new RegExp(`^\\s*(?:export\\s+)?${GATEWAY_KEY_VAR}\\s*=\\s*(.*)$`).exec(line);
    if (!match) continue;
    value = match[1].trim().replace(/^(['"])(.*)\1$/, "$2");
  }
  // Never echo the value — only its presence.
  if (!value) die(`${GATEWAY_KEY_VAR} is missing or empty in ${envFile}`);
  return value;
}

function settingsPath(home) {
  return path.join(home, ".claude", "settings.json");
}

function readSettings(file) {
  let original;
  try {
    original = fs.readFileSync(file, "utf8");
  } catch {
    return { original: null, parsed: {} };
  }
  let parsed;
  try {
    parsed = JSON.parse(original);
  } catch (err) {
    die(`settings.json is not valid JSON: ${err.message}`);
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    die("settings.json is not a JSON object; refusing to rewrite");
  }
  return { original, parsed };
}

function serialize(obj) {
  return `${JSON.stringify(obj, null, 2)}\n`;
}

// --write refuses a settings file it did not serialise itself, so a hand-formatted file is never
// silently reflowed under the user. `--canonicalize` is the explicit opt-in to that reflow: the
// rewrite below re-serialises the parsed object anyway, so all this has to do is stand aside.
// Semantic content is unchanged either way, and `--check` never reaches here.
// "Canonical" means 2-space layout, not one JSON encoder's escape choice: Python's json.dump
// (ensure_ascii, the fleet installers that also maintain this file) writes `\u2014` where
// JSON.stringify writes `—`. Both decode identically, so the guard compares with non-ASCII
// escaped the way Python does it (lowercase hex, UTF-16 code units).
function escapeNonAscii(text) {
  return text.replace(/[\u0080-\uffff]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`);
}

function assertCanonical(original, canonicalize) {
  if (original === null || canonicalize) return;
  const canonical = serialize(JSON.parse(original));
  if (canonical !== original && escapeNonAscii(canonical) !== original) {
    die("settings.json is not canonical 2-space JSON; refusing to rewrite — re-run with --canonicalize to reformat it in place");
  }
}

function applyLaunchProfile(parsed, gatewayKey) {
  if (!parsed.env || typeof parsed.env !== "object" || Array.isArray(parsed.env)) parsed.env = {};
  for (const key of FORBIDDEN_ENV_KEYS) {
    if (key in parsed.env) {
      process.stderr.write(
        `model-picker-lineup: warning: env.${key} already present — left untouched; OAuth is the credential (router/AGENTS.md)\n`,
      );
    }
  }
  parsed.env.ANTHROPIC_BASE_URL = ROUTER_URL;
  parsed.env.ANTHROPIC_CUSTOM_HEADERS = `x-gateway-key: ${gatewayKey}`;
  // Assignment, not insert-if-absent: these keys are generated, so a drifted hand-edit is
  // corrected rather than preserved, and a re-run over an already-current block is a no-op.
  for (const [key, value] of Object.entries(LAUNCH_PROFILE_ENV)) parsed.env[key] = value;
}

function checkFailure(rows, parsed, launchProfile, gatewayKey, file) {
  const actual = parsed?.modelPicker?.options;
  if (!Array.isArray(actual)) return `modelPicker.options missing in ${file}; ${rows.length} rows generated`;
  if (actual.length !== rows.length) {
    return `modelPicker.options stale in ${file}: ${actual.length} rows present, ${rows.length} generated`;
  }
  for (let i = 0; i < rows.length; i += 1) {
    if (JSON.stringify(actual[i]) !== JSON.stringify(rows[i])) {
      return `modelPicker.options stale in ${file}: row ${i} is ${JSON.stringify(actual[i]?.model ?? actual[i])}, expected ${JSON.stringify(rows[i].model)}`;
    }
  }
  if (launchProfile) {
    const env = parsed.env ?? {};
    if (env.ANTHROPIC_BASE_URL !== ROUTER_URL) {
      return `env.ANTHROPIC_BASE_URL in ${file} is ${JSON.stringify(env.ANTHROPIC_BASE_URL ?? null)}, expected ${ROUTER_URL}`;
    }
    if (env.ANTHROPIC_CUSTOM_HEADERS !== `x-gateway-key: ${gatewayKey}`) {
      return `env.ANTHROPIC_CUSTOM_HEADERS in ${file} does not carry the ${GATEWAY_KEY_VAR} value`;
    }
    for (const [key, value] of Object.entries(LAUNCH_PROFILE_ENV)) {
      if (env[key] !== value) {
        return `env.${key} in ${file} is ${JSON.stringify(env[key] ?? null)}, expected ${JSON.stringify(value)}`;
      }
    }
  }
  return null;
}

// The gateway key for --launch-profile. An explicit --env-file always wins. Otherwise the script
// re-execs itself under `fleet-env run model-picker` and reads the key from its environment
// (no file); a missing fleet-env binary fails closed.
// Only --launch-profile needs the key, so a bare --write/--check never consults fleet-env.
function gatewayKeySource(envFile) {
  if (envFile) return readGatewayKey(envFile);
  fleetEnvReexec("model-picker");
  const value = (process.env[GATEWAY_KEY_VAR] || "").trim();
  if (!value) die(`${GATEWAY_KEY_VAR} is missing or empty in the fleet-env environment`);
  return value;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const config = readConfig(opts.config);
  const catalog = readCatalog(opts.catalog);
  const rows = buildRows(config, catalog);

  if (opts.mode === "print") {
    process.stdout.write(`${JSON.stringify(rows, null, 2)}\n`);
    return;
  }

  const file = settingsPath(opts.home);
  const gatewayKey = opts.launchProfile ? gatewayKeySource(opts.envFile) : null;

  if (opts.mode === "check") {
    const drift = configDriftFailure(config, catalog, opts.config, opts.catalog);
    if (drift) {
      process.stderr.write(`model-picker-lineup: ${drift}\n`);
      process.exit(1);
    }
    const { original, parsed } = readSettings(file);
    if (original === null) {
      process.stderr.write(`model-picker-lineup: ${file} does not exist; ${rows.length} rows generated\n`);
      process.exit(1);
    }
    const failure = checkFailure(rows, parsed, opts.launchProfile, gatewayKey, file);
    if (failure) {
      process.stderr.write(`model-picker-lineup: ${failure}\n`);
      process.exit(1);
    }
    process.stdout.write(`model-picker-lineup: ${file} is in sync (${rows.length} rows)\n`);
    return;
  }

  const { original, parsed } = readSettings(file);
  assertCanonical(original, opts.canonicalize);
  if (!parsed.modelPicker || typeof parsed.modelPicker !== "object" || Array.isArray(parsed.modelPicker)) {
    parsed.modelPicker = {};
  }
  parsed.modelPicker.options = rows;
  if (opts.launchProfile) applyLaunchProfile(parsed, gatewayKey);

  const next = serialize(parsed);
  if (next === original) {
    if (!opts.quiet) process.stdout.write(`model-picker-lineup: ${file} already current (${rows.length} rows)\n`);
    return;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, next);
  process.stdout.write(`model-picker-lineup: wrote ${rows.length} rows to ${file}\n`);
}

main();
