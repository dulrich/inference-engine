// test-models.mjs — the one place the live codex tests read their model from
// (router/test/test-models.json; $CODEX_TEST_MODEL overrides it for one run). The smoke scripts
// read the same file with jq. Not a test file: the `*.test.mjs` glob does not load it.

import fs from "node:fs";

const FILE = new URL("./test-models.json", import.meta.url);

/** The routed codex id for `persona`, e.g. codexTestModel("codex-b") → "codex-b/gpt-6-luna". */
export function codexTestModel(persona) {
  const fromEnv = process.env.CODEX_TEST_MODEL?.trim();
  const model = fromEnv || JSON.parse(fs.readFileSync(FILE, "utf8")).codex;
  if (typeof model !== "string" || !model || model.includes("/")) {
    throw new Error(`test-models: codex model must be a bare id without a persona prefix, got ${JSON.stringify(model)} (${fromEnv ? "$CODEX_TEST_MODEL" : FILE.pathname})`);
  }
  return `${persona}/${model}`;
}
