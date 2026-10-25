import { describe, expect, test } from "bun:test";
import type { ServingAlias, ServingMeasuredEntry } from "@bench-dashboard/shared";
import { matchServingToMeasured } from "./matchMeasured.ts";

function servingAlias(overrides: Partial<ServingAlias>): ServingAlias {
  return {
    modelKey: "some-key",
    name: "Some model",
    aliases: ["some-alias"],
    ttl: 1800,
    checkEndpoint: "/health",
    flags: {
      contextLength: null,
      kvCacheTypeK: null,
      kvCacheTypeV: null,
      flashAttention: null,
      gpuLayers: null,
      allFlags: [],
    },
    capability: null,
    recipe: null,
    ...overrides,
  };
}

function entry(alias: string, experimentId = "exp-1"): ServingMeasuredEntry {
  return { alias, experimentId };
}

describe("matchServingToMeasured", () => {
  test("matches an alias to the measured entry whose alias it contains", () => {
    const exp = entry("fixture-alias-f16-c8192");
    const rows = matchServingToMeasured([servingAlias({ aliases: ["fixture-alias-f16-c8192"] })], [exp]);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.measured).toBe(exp);
  });

  test("an alias with no matching sweep cell is unmatched (most aliases today)", () => {
    const rows = matchServingToMeasured([servingAlias({ aliases: ["unmeasured-alias"] })], []);
    expect(rows[0]?.measured).toBeNull();
  });

  test("an entry for a different alias never matches", () => {
    const rows = matchServingToMeasured([servingAlias({ aliases: ["anything"] })], [entry("other")]);
    expect(rows[0]?.measured).toBeNull();
  });

  test("with two experiments sharing an alias, the first (newest-first order) wins", () => {
    const newer = entry("shared", "newer");
    const older = entry("shared", "older");
    const rows = matchServingToMeasured([servingAlias({ aliases: ["shared"] })], [newer, older]);
    expect(rows[0]?.measured?.experimentId).toBe("newer");
  });

  test("preserves alias list order and length", () => {
    const rows = matchServingToMeasured(
      [servingAlias({ modelKey: "a" }), servingAlias({ modelKey: "b" })],
      [],
    );
    expect(rows.map((r) => r.alias.modelKey)).toEqual(["a", "b"]);
  });
});
