import { describe, expect, test } from "bun:test";
import type { ExperimentSummary, ServingAlias } from "@bench-dashboard/shared";
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

function experiment(overrides: Partial<ExperimentSummary>): ExperimentSummary {
  return {
    id: "exp-1",
    schemaVersion: 2,
    description: null,
    timestamp: "2026-08-23T00:00:00Z",
    lineage: { hfRepo: null, hfRevision: null, filename: null, fileSize: null, sha256: null, llamaCppBuild: null },
    sweepCell: null,
    hasWilsonSummary: false,
    samplingArm: null,
    battery: null,
    vramProfile: null,
    trialCount: 0,
    metricCount: 0,
    ...overrides,
  };
}

describe("matchServingToMeasured", () => {
  test("matches an alias to the experiment whose sweepCell.alias it contains", () => {
    const exp = experiment({
      id: "exp-1",
      sweepCell: { cellId: "cell-1", alias: "fixture-alias-f16-c8192", serverFlags: {}, command: null },
    });
    const rows = matchServingToMeasured([servingAlias({ aliases: ["fixture-alias-f16-c8192"] })], [exp]);
    expect(rows).toHaveLength(1);
    expect(rows[0]?.measured).toBe(exp);
  });

  test("an alias with no matching sweep cell is unmatched (most aliases today)", () => {
    const rows = matchServingToMeasured([servingAlias({ aliases: ["unmeasured-alias"] })], []);
    expect(rows[0]?.measured).toBeNull();
  });

  test("an experiment with no sweepCell (non-sweep full-CLI record) never matches", () => {
    const exp = experiment({ sweepCell: null });
    const rows = matchServingToMeasured([servingAlias({ aliases: ["anything"] })], [exp]);
    expect(rows[0]?.measured).toBeNull();
  });

  test("with two experiments sharing an alias, the first (newest-first order) wins", () => {
    const newer = experiment({ id: "newer", sweepCell: { cellId: "c2", alias: "shared", serverFlags: {}, command: null } });
    const older = experiment({ id: "older", sweepCell: { cellId: "c1", alias: "shared", serverFlags: {}, command: null } });
    const rows = matchServingToMeasured([servingAlias({ aliases: ["shared"] })], [newer, older]);
    expect(rows[0]?.measured?.id).toBe("newer");
  });

  test("preserves alias list order and length", () => {
    const rows = matchServingToMeasured(
      [servingAlias({ modelKey: "a" }), servingAlias({ modelKey: "b" })],
      [],
    );
    expect(rows.map((r) => r.alias.modelKey)).toEqual(["a", "b"]);
  });
});
