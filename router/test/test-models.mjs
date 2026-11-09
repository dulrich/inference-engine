// test-models.mjs — the one place the live codex tests read their model from
// (router/test/test-models.json; $CODEX_TEST_MODEL overrides it for one run). The smoke scripts
// read the same file with jq. Not a test file: the `*.test.mjs` glob does not load it.

import fs from "node:fs";

const FILE = new URL("./test-models.json", import.meta.url);

/**
 * The codex persona the live tests send turns to: $CODEX_TEST_PERSONA, else (when `ids`, the
 * router's /v1/models id list, is given) the first `codex-*` provider listed. Fails loudly when
 * neither names one -- never a silent default.
 */
export function codexPersona(ids) {
  const fromEnv = process.env.CODEX_TEST_PERSONA?.trim();
  if (fromEnv) return fromEnv;
  const hit = (ids ?? []).map(id => /^(codex-[^/]+)\//.exec(id)?.[1]).find(Boolean);
  if (hit) return hit;
  throw new Error("test-models: no codex persona: set $CODEX_TEST_PERSONA (or list a codex-* provider in /v1/models)");
}

/** The routed codex id for `persona`, e.g. codexTestModel("codex-a") → "codex-a/gpt-6-luna". */
export function codexTestModel(persona) {
  const fromEnv = process.env.CODEX_TEST_MODEL?.trim();
  const model = fromEnv || JSON.parse(fs.readFileSync(FILE, "utf8")).codex;
  if (typeof model !== "string" || !model || model.includes("/")) {
    throw new Error(`test-models: codex model must be a bare id without a persona prefix, got ${JSON.stringify(model)} (${fromEnv ? "$CODEX_TEST_MODEL" : FILE.pathname})`);
  }
  return `${persona}/${model}`;
}
