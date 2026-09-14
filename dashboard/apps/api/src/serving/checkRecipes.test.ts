import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import {
  DEFAULT_CONFIG_PATH,
  checkServingRecipes,
  checkServingRecipesFile,
  formatFinding,
  parseConfigArg,
  parsePathArg,
  resolveGatewayPath,
  resolveVisibilityPath,
} from "../../../../scripts/check-serving-recipes.ts";

// The recipe-conformance gate (WP-1, `plans/bench-dashboard-serving-control.md`), exercised
// against fixtures only — never against the real `llama/config.yaml`, whose contents are a
// serving fact, not a test fact (the real file is gated by `bun run check:recipes` itself).
// The gate lives in `dashboard/scripts/` and is imported by relative path here, the same way
// `src/router/slotsPoller.test.ts` imports the host-side poller: one definition, one test.
const FIXTURES = join(import.meta.dir, "../../fixtures/serving");
// WP-3: the gate now reads three files. The fixtures carry their own declaration and gateway
// config beside the YAML, named explicitly here — the default resolution is relative to the
// CONFIG's repo, which for a fixture would point at a directory that does not exist.
const conformingArgs = [
  join(FIXTURES, "conforming.yaml"),
  join(FIXTURES, "conforming-visibility.json"),
  join(FIXTURES, "conforming-gateway.json"),
] as const;
const nonConformingArgs = [
  join(FIXTURES, "non-conforming.yaml"),
  join(FIXTURES, "non-conforming-visibility.json"),
  join(FIXTURES, "non-conforming-gateway.json"),
] as const;

describe("check-serving-recipes", () => {
  test("a conforming config yields no findings", async () => {
    const findings = await checkServingRecipesFile(...conformingArgs);
    expect(findings.map(formatFinding)).toEqual([]);
  });

  // One defect per entry, named by its own model key: this asserts both the rule coverage and
  // the "one line per non-conforming model" output contract (a cascade — one defect producing
  // a second finding on the same entry — fails here).
  test("each non-conforming entry yields exactly one finding, of the expected rule", async () => {
    const findings = await checkServingRecipesFile(...nonConformingArgs);
    expect(findings.map((f) => [f.modelKey, f.rule])).toEqual([
      ["missing-mtp-chat", "missing-key"],
      ["unapproved-source-chat", "source-url"],
      ["gap-no-reason-chat", "source-reason"],
      ["bad-verified-chat", "source-verified"],
      ["context-mismatch-chat", "projection"],
      ["kv-claim-chat", "projection"],
      ["budget-claim-chat", "projection"],
      ["answer-cap-chat", "answer-cap"],
      ["bad-reasoning-chat", "vocabulary"],
      // WP-3 visibility rules, on the same one-line-per-entry contract. `dup-first-chat` is
      // absent from this list on purpose: it is the FIRST claimant of the shared gateway id and
      // conforms; only the second claimant is the drift.
      ["undeclared-chat", "visibility-missing"],
      ["bad-consumers-chat", "visibility-vocabulary"],
      ["alien-gateway-id-chat", "visibility-gateway-id"],
      ["cc-null-gateway-chat", "visibility-cc-requires-gateway-id"],
      ["dup-second-chat", "visibility-duplicate-gateway-id"],
      ["crosswalk-chat", "max-tokens-crosswalk"],
    ]);
    expect(formatFinding(findings[4] as (typeof findings)[number])).toBe(
      "context-mismatch-chat: projection — `context_length` 65536 ≠ `-c 131072`",
    );
  });

  // The embed entry in that same fixture carries no `sampling`, no `max_output` and no
  // `sources` at all — conforming for a non-chat entry, and silent here by design.
  test("embed/rerank entries are out of scope and never reported", async () => {
    const findings = await checkServingRecipesFile(...nonConformingArgs);
    expect(findings.some((f) => f.modelKey === "fixture-embed-f16")).toBe(false);
  });

  test("the default config path resolves to the repo's own llama/config.yaml, not the cwd", () => {
    expect(DEFAULT_CONFIG_PATH.endsWith("/llama/config.yaml")).toBe(true);
    expect(parseConfigArg([])).toBe(DEFAULT_CONFIG_PATH);
    expect(parseConfigArg(["--config", "/tmp/a.yaml"])).toBe("/tmp/a.yaml");
    expect(parseConfigArg(["--config=/tmp/b.yaml"])).toBe("/tmp/b.yaml");
  });

  // The companions resolve off the CONFIG, so `--config <some other repo>/llama/config.yaml`
  // checks that repo's declaration rather than this one's; both stay overridable.
  test("the visibility and gateway paths resolve relative to the config, and are overridable", () => {
    expect(resolveVisibilityPath(DEFAULT_CONFIG_PATH).endsWith("/router/visibility.json")).toBe(true);
    expect(resolveGatewayPath(DEFAULT_CONFIG_PATH).endsWith("/router/gateway.config.json")).toBe(true);
    expect(parsePathArg(["--visibility", "/tmp/v.json"], "visibility", "fallback")).toBe("/tmp/v.json");
    expect(parsePathArg(["--gateway=/tmp/g.json"], "gateway", "fallback")).toBe("/tmp/g.json");
    expect(parsePathArg([], "visibility", "fallback")).toBe("fallback");
  });

  // Fail-closed: an unusable declaration is a finding, never "no visibility rules apply".
  test("a missing, malformed or wrong-schema declaration is one finding, not silence", async () => {
    const configText = await Bun.file(join(FIXTURES, "conforming.yaml")).text();
    const gatewayText = await Bun.file(join(FIXTURES, "conforming-gateway.json")).text();

    for (const declaration of [null, "{not json", '{"schema":"serving-visibility-v2","models":{}}', "[]"]) {
      const findings = checkServingRecipes(configText, declaration, gatewayText);
      expect(findings.map((f) => f.rule)).toEqual(["visibility-unreadable"]);
      expect(findings[0]?.modelKey).toBe("router/visibility.json");
    }
  });

  // The crosswalk is the seam patch 0010's clamp depends on: an id the gateway does not cap at
  // all is as much a defect as one it caps at the wrong number.
  test("a gateway config with no maxOutputTokens map fails the crosswalk for every declared id", async () => {
    const configText = await Bun.file(join(FIXTURES, "conforming.yaml")).text();
    const visibilityText = await Bun.file(join(FIXTURES, "conforming-visibility.json")).text();
    const findings = checkServingRecipes(configText, visibilityText, '{"providers":[]}');

    expect(findings.map((f) => [f.modelKey, f.rule])).toEqual([
      ["fixture-chat-UD-Q4_K_XL", "max-tokens-crosswalk"],
      ["fixture-moe-UD-IQ3_S", "max-tokens-crosswalk"],
    ]);
    expect(formatFinding(findings[0] as (typeof findings)[number])).toContain("is missing from");
  });
});
