#!/usr/bin/env node
// Generate Claude Code `modelPicker.options` rows from the router's gateway.config.json.
//
// The config is the single source of truth: there is no hand-maintained id list here, only the
// family/label table below. Every `<provider>/<model>` id in the config becomes one picker row,
// except the hosted `anthropic` provider (its ids duplicate Claude Code's built-in rows, and the
// bare id is what Claude Code sends anyway). `replaceBuiltInOptions` is never written, so the
// built-in subscription rows stay first.
//
// Usage:
//   model-picker-lineup.mjs [--config <path>] [--home <dir>] [--env-file <path>]
//                           [--check | --write [--launch-profile]]
//
// No mode flag prints the rows array as JSON to stdout.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_CONFIG = path.join(SCRIPT_DIR, "gateway.config.json");
const DEFAULT_ENV_FILE = "<code-root>/fleet-tools/.env";
const GATEWAY_KEY_VAR = "INFERENCE_ROUTER_GATEWAY_KEY";
const ROUTER_URL = "http://127.0.0.1:4813";

// Providers whose ids never become picker rows.
const EXCLUDED_PROVIDERS = new Set(["anthropic"]);
// Provider name -> the word used as the "leg" in a label.
const LEG_NAMES = { "claude-local": "local" };
// Credential env keys the generator must never write (OAuth stays the credential).
const FORBIDDEN_ENV_KEYS = ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"];

// Family/label table. `prefix` families may carry a `-low|-medium|-high` effort suffix; without
// one the persona default applies (router/AGENTS.md, "The codex sidecar leg").
const FAMILIES = [
  { kind: "prefix", id: "gpt-5.6-luna", family: "Luna", defaultEffort: "low" },
  { kind: "prefix", id: "gpt-5.6-sol", family: "Sol", defaultEffort: "medium" },
  { kind: "prefix", id: "gpt-6-astra", family: "Astra", defaultEffort: "medium" },
  { kind: "exact", id: "z-ai/glm-5.2:free", family: "GLM 5.2 free" },
  { kind: "exact", id: "z-ai/glm-5.3", family: "GLM 5.3" },
  { kind: "exact", id: "qwen36-a3b", family: "Qwen3.6 A3B" },
  { kind: "exact", id: "qwen38-27b-iq3s", family: "Qwen3.8 27B" },
  { kind: "exact", id: "nemotron-4b", family: "Nemotron 4B" },
];

function die(message, code = 2) {
  process.stderr.write(`model-picker-lineup: ${message}\n`);
  process.exit(code);
}

function parseArgs(argv) {
  const opts = {
    config: DEFAULT_CONFIG,
    home: os.homedir(),
    envFile: DEFAULT_ENV_FILE,
    mode: "print",
    launchProfile: false,
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
    else if (arg === "--home") opts.home = value();
    else if (arg === "--env-file") opts.envFile = value();
    else if (arg === "--check") opts.mode = "check";
    else if (arg === "--write") opts.mode = "write";
    else if (arg === "--launch-profile") opts.launchProfile = true;
    else if (arg === "--help" || arg === "-h") {
      process.stdout.write(
        "usage: model-picker-lineup.mjs [--config <path>] [--home <dir>] [--env-file <path>] [--check | --write [--launch-profile]]\n",
      );
      process.exit(0);
    } else die(`unknown argument: ${arg}`);
  }
  if (opts.mode === "print" && opts.launchProfile) die("--launch-profile needs --check or --write");
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
  const match = /-(low|medium|high)$/.exec(modelId);
  return match ? match[1] : entry.defaultEffort;
}

function describeRow(providerName, leg, modelId, effort) {
  if (providerName.startsWith("codex-")) {
    const tail = /-(low|medium|high)$/.test(modelId)
      ? `effort ${effort} is in the id suffix`
      : `no suffix means the ${effort} default`;
    return `${providerName} sidecar leg via the router (:4813); ${tail}`;
  }
  if (providerName === "claude-local") return "Local GPU tier via the router (:4813); claude-local leg";
  if (providerName === "openrouter") return "OpenRouter via the router (:4813); OpenAI-compat conversion leg";
  if (providerName === "or-anthropic") return "OpenRouter via the router (:4813); native Anthropic leg";
  return `${providerName} leg via the router (:4813)`;
}

export function buildRows(config) {
  const rows = [];
  for (const provider of config.providers ?? []) {
    if (!provider || typeof provider.name !== "string") continue;
    if (EXCLUDED_PROVIDERS.has(provider.name)) continue;
    const leg = LEG_NAMES[provider.name] ?? provider.name;
    for (const modelId of provider.models ?? []) {
      const entry = describeFamily(modelId);
      const effort = effortOf(modelId, entry);
      const label = `${[entry.family, effort].filter(Boolean).join(" ")} (${leg})`;
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

function assertCanonical(original) {
  if (original === null) return;
  if (serialize(JSON.parse(original)) !== original) {
    die("settings.json is not canonical 2-space JSON; refusing to rewrite");
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
  }
  return null;
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const rows = buildRows(readConfig(opts.config));

  if (opts.mode === "print") {
    process.stdout.write(`${JSON.stringify(rows, null, 2)}\n`);
    return;
  }

  const file = settingsPath(opts.home);
  const gatewayKey = opts.launchProfile ? readGatewayKey(opts.envFile) : null;

  if (opts.mode === "check") {
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
  assertCanonical(original);
  if (!parsed.modelPicker || typeof parsed.modelPicker !== "object" || Array.isArray(parsed.modelPicker)) {
    parsed.modelPicker = {};
  }
  parsed.modelPicker.options = rows;
  if (opts.launchProfile) applyLaunchProfile(parsed, gatewayKey);

  const next = serialize(parsed);
  if (next === original) {
    process.stdout.write(`model-picker-lineup: ${file} already current (${rows.length} rows)\n`);
    return;
  }
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, next);
  process.stdout.write(`model-picker-lineup: wrote ${rows.length} rows to ${file}\n`);
}

main();
