// live-test-pins.test.mjs — the pin lint: a live-test file must not name a model outside a comment.
// Models come from test-slots.json (scripts/lib/test-slots.{sh,mjs}) or are derived from the
// catalog. No network, no credentials; part of the default per-commit glob.
//
// The forbidden set is computed at run time, never listed here (root AGENTS.md gate 6):
//   (a) every routed `<provider>/<id>` of every catalog entry;
//   (b) every slot value in test-slots.json (OPENROUTER_ADD_PROBE included, bare);
//   (c) every bare catalog id that "looks like a model id": a digit AND one of `-` `.` `/`, length >= 5.
// Matching is on token boundaries (the neighbours are not [A-Za-z0-9._/-]), so `gpt-6-luna-high`
// does not match `gpt-6-luna`. A catalog edit turns this red only if a listed file literally names
// the edited model.
//
// Comment stripping (conservative heuristics, line numbers preserved):
//   .sh  — a line whose first non-blank char is `#` is blank; a trailing ` #...` (the `#` preceded by
//          whitespace) is cut unless an odd number of `'` or `"` precede it on the line (inside quotes).
//   .mjs — a small state machine over the whole file: `//` line comments and `/* */` blocks are
//          removed, but never inside '...', "..." or `...` string literals (so URLs survive).
//          Template-literal `${}` nesting is not tracked (a `//` inside one counts as string text).

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { SLOT_FILE } from "../../scripts/lib/test-slots.mjs";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const CATALOG_FILE = path.join(ROOT, "router", "catalog", "catalog.json");
const CONDITIONAL_DIR = path.join(ROOT, "router", "test", "conditional");

const FIXED_FILES = [
  "router/smoke-router.sh",
  "router/smoke-tool-search.sh",
  "router/smoke-catalog.sh",
  "router/test/tool-search-shared.mjs",
  "sidecar/status.sh",
  "llama/smoke-inference.sh",
];

function listFiles() {
  const conditional = fs.existsSync(CONDITIONAL_DIR)
    ? fs.readdirSync(CONDITIONAL_DIR).filter((f) => f.endsWith(".test.mjs")).sort().map((f) => `router/test/conditional/${f}`)
    : [];
  return [...FIXED_FILES, ...conditional];
}

export function stripShComments(text) {
  return text.split("\n").map((line) => {
    if (/^\s*#/.test(line)) return "";
    for (let i = 0; i < line.length; i++) {
      if (line[i] !== "#" || i === 0 || !/\s/.test(line[i - 1])) continue;
      const before = line.slice(0, i);
      const inQuote = (before.match(/'/g) ?? []).length % 2 === 1 || (before.match(/"/g) ?? []).length % 2 === 1;
      if (!inQuote) return before;
    }
    return line;
  }).join("\n");
}

export function stripMjsComments(text) {
  let out = "";
  let i = 0;
  let quote = null;
  while (i < text.length) {
    const c = text[i];
    const n = text[i + 1];
    if (quote) {
      out += c;
      if (c === "\\") { out += n ?? ""; i += 2; continue; }
      if (c === quote) quote = null;
      i++;
    } else if (c === "'" || c === '"' || c === "`") {
      quote = c; out += c; i++;
    } else if (c === "/" && n === "/") {
      while (i < text.length && text[i] !== "\n") i++;
    } else if (c === "/" && n === "*") {
      const end = text.indexOf("*/", i + 2);
      const stop = end === -1 ? text.length : end + 2;
      out += text.slice(i, stop).replace(/[^\n]/g, "");
      i = stop;
    } else {
      out += c; i++;
    }
  }
  return out;
}

const looksLikeModelId = (id) => id.length >= 5 && /\d/.test(id) && /[-./]/.test(id);

function forbiddenSet() {
  const catalog = JSON.parse(fs.readFileSync(CATALOG_FILE, "utf8"));
  const slots = JSON.parse(fs.readFileSync(SLOT_FILE, "utf8"));
  const forbidden = new Set();
  for (const [provider, p] of Object.entries(catalog.providers ?? {})) {
    for (const e of p.models ?? []) {
      forbidden.add(`${provider}/${e.id}`);
      if (looksLikeModelId(e.id)) forbidden.add(e.id);
    }
  }
  for (const [name, value] of Object.entries(slots)) {
    if (!name.startsWith("_") && typeof value === "string" && value) forbidden.add(value);
  }
  return forbidden;
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const BOUND_L = "(?<![A-Za-z0-9._/-])";
const BOUND_R = "(?![A-Za-z0-9._/-])";

export function findPins(text, ext, forbidden) {
  const stripped = ext === ".sh" ? stripShComments(text) : stripMjsComments(text);
  const ids = [...forbidden].sort((a, b) => b.length - a.length);
  const re = new RegExp(`${BOUND_L}(${ids.map(escapeRe).join("|")})${BOUND_R}`, "g");
  const hits = [];
  stripped.split("\n").forEach((line, idx) => {
    for (const m of line.matchAll(re)) hits.push({ line: idx + 1, id: m[1] });
  });
  return hits;
}

test("the file list is non-empty, every listed file exists, the forbidden set is non-empty", () => {
  const files = listFiles();
  assert.ok(files.length > 0);
  for (const f of files) assert.ok(fs.existsSync(path.join(ROOT, f)), `listed live-test file is missing: ${f}`);
  assert.ok(forbiddenSet().size > 0, "forbidden model set is empty");
});

test("comment stripping and token boundaries behave", () => {
  const forbidden = new Set(["gpt-6-luna", "claude-local/nemotron-4b"]);
  assert.deepEqual(findPins('x="gpt-6-luna-high" # gpt-6-luna\n# gpt-6-luna\n', ".sh", forbidden), []);
  assert.deepEqual(findPins('a "gpt-6-luna"\n', ".sh", forbidden), [{ line: 1, id: "gpt-6-luna" }]);
  assert.deepEqual(findPins("// gpt-6-luna\n/* gpt-6-luna\ngpt-6-luna */ const u = 'http://x/a'; const m = 'claude-local/nemotron-4b';\n", ".mjs", forbidden), [{ line: 3, id: "claude-local/nemotron-4b" }]);
});

test("no live-test file names a model outside a comment", () => {
  const forbidden = forbiddenSet();
  const failures = [];
  for (const f of listFiles()) {
    for (const h of findPins(fs.readFileSync(path.join(ROOT, f), "utf8"), path.extname(f), forbidden)) {
      failures.push(`${f}:${h.line}: names model ${h.id} outside a comment — read it from test-slots.json (scripts/lib/test-slots.{sh,mjs}) or derive it from the catalog`);
    }
  }
  assert.deepEqual(failures, [], failures.join("\n"));
});
