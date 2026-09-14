import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import {
  DEFAULT_CONFIG_PATH,
  checkServingRecipesFile,
  formatFinding,
  parseConfigArg,
} from "../../../../scripts/check-serving-recipes.ts";

// The recipe-conformance gate (WP-1, `plans/bench-dashboard-serving-control.md`), exercised
// against fixtures only — never against the real `llama/config.yaml`, which does NOT conform
// until the recipe audit (WP-2) lands and whose failure count is therefore not a test fact.
// The gate lives in `dashboard/scripts/` and is imported by relative path here, the same way
// `src/router/slotsPoller.test.ts` imports the host-side poller: one definition, one test.
const FIXTURES = join(import.meta.dir, "../../fixtures/serving");

describe("check-serving-recipes", () => {
  test("a conforming config yields no findings", async () => {
    const findings = await checkServingRecipesFile(join(FIXTURES, "conforming.yaml"));
    expect(findings.map(formatFinding)).toEqual([]);
  });

  // One defect per entry, named by its own model key: this asserts both the rule coverage and
  // the "one line per non-conforming model" output contract (a cascade — one defect producing
  // a second finding on the same entry — fails here).
  test("each non-conforming entry yields exactly one finding, of the expected rule", async () => {
    const findings = await checkServingRecipesFile(join(FIXTURES, "non-conforming.yaml"));
    expect(findings.map((f) => [f.modelKey, f.rule])).toEqual([
      ["missing-mtp-chat", "missing-key"],
      ["unapproved-source-chat", "source-url"],
      ["gap-no-reason-chat", "source-reason"],
      ["bad-verified-chat", "source-verified"],
      ["context-mismatch-chat", "projection"],
      ["kv-claim-chat", "projection"],
      ["budget-claim-chat", "projection"],
      ["bad-reasoning-chat", "vocabulary"],
    ]);
    expect(formatFinding(findings[4] as (typeof findings)[number])).toBe(
      "context-mismatch-chat: projection — `context_length` 65536 ≠ `-c 131072`",
    );
  });

  // The embed entry in that same fixture carries no `sampling`, no `max_output` and no
  // `sources` at all — conforming for a non-chat entry, and silent here by design.
  test("embed/rerank entries are out of scope and never reported", async () => {
    const findings = await checkServingRecipesFile(join(FIXTURES, "non-conforming.yaml"));
    expect(findings.some((f) => f.modelKey === "fixture-embed-f16")).toBe(false);
  });

  test("the default config path resolves to the repo's own llama/config.yaml, not the cwd", () => {
    expect(DEFAULT_CONFIG_PATH.endsWith("/llama/config.yaml")).toBe(true);
    expect(parseConfigArg([])).toBe(DEFAULT_CONFIG_PATH);
    expect(parseConfigArg(["--config", "/tmp/a.yaml"])).toBe("/tmp/a.yaml");
    expect(parseConfigArg(["--config=/tmp/b.yaml"])).toBe("/tmp/b.yaml");
  });
});
