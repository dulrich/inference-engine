import { describe, expect, test } from "bun:test";
import type { ExperimentSummary } from "@bench-dashboard/shared";
import { deriveHfRepos } from "./hfRepos.ts";

function experiment(id: string, hfRepo: string | null): ExperimentSummary {
  return {
    id,
    schemaVersion: 2,
    description: null,
    timestamp: null,
    lineage: { hfRepo, hfRevision: null, filename: null, fileSize: null, sha256: null, llamaCppBuild: null },
    sweepCell: null,
    hasWilsonSummary: false,
    samplingArm: null,
    battery: null,
    vramProfile: null,
    trialCount: 0,
    metricCount: 0,
  };
}

describe("deriveHfRepos", () => {
  test("returns unique repos, sorted", () => {
    const repos = deriveHfRepos([
      experiment("a", "org/repo-b"),
      experiment("b", "org/repo-a"),
      experiment("c", "org/repo-b"),
    ]);
    expect(repos).toEqual(["org/repo-a", "org/repo-b"]);
  });

  test("drops null and empty hfRepo values", () => {
    const repos = deriveHfRepos([experiment("a", null), experiment("b", ""), experiment("c", "org/repo")]);
    expect(repos).toEqual(["org/repo"]);
  });

  test("empty input yields an empty list", () => {
    expect(deriveHfRepos([])).toEqual([]);
  });
});
