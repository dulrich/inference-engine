import { describe, expect, test } from "bun:test";
import type { ServingAlias, ServingFactsStatusResponse, ServingResponse, ServingTimingRow } from "@bench-dashboard/shared";
import { factsUnavailableText, researchRunUrl, resolveServingLoad } from "./facts.ts";

function alias(modelKey: string, aliases: string[]): ServingAlias {
  return {
    modelKey,
    name: null,
    aliases,
    ttl: null,
    checkEndpoint: null,
    flags: { contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null, allFlags: [] },
    capability: null,
    recipe: null,
  } as ServingAlias;
}

const serving: ServingResponse = { configPath: "/config/llama.yaml", models: [alias("a", ["a-1"]), alias("b", ["b-1"])] };
const timingRow: ServingTimingRow = {
  modelKey: "a",
  aliases: ["a-1"],
  p50PrefillTokS: 10,
  p90PrefillTokS: null,
  p50DecodeTokS: null,
  p90DecodeTokS: null,
  samplingArm: null,
  sampleCount: 1,
  mtp: false,
};
const ok = <T>(value: T): PromiseSettledResult<T> => ({ status: "fulfilled", value });
const fail = <T>(message: string): PromiseSettledResult<T> => ({ status: "rejected", reason: new Error(message) });
const unavailable = (reason: "absent" | "stale-config"): PromiseSettledResult<ServingFactsStatusResponse> =>
  ok({ available: false, reason });

describe("researchRunUrl", () => {
  test("links to the research dashboard's /runs/:id on the page's own host", () => {
    expect(researchRunUrl({ protocol: "http:", hostname: "host-a" }, "exp-1")).toBe("http://host-a:4816/runs/exp-1");
  });

  test("URI-encodes the experiment id", () => {
    expect(researchRunUrl({ protocol: "https:", hostname: "10.0.0.2" }, "a/b c")).toBe(
      "https://10.0.0.2:4816/runs/a%2Fb%20c",
    );
  });
});

describe("resolveServingLoad", () => {
  test("facts available: rows joined to measured, timing passed through, no note", () => {
    const load = resolveServingLoad(
      ok(serving),
      ok([timingRow]),
      ok([{ alias: "a-1", experimentId: "exp-1" }]),
      ok({ available: true, generatedAt: "2026-10-25T12:00:00Z", servingConfigRevision: "ab" }),
    );
    expect(load.rows?.map((r) => r.measured?.experimentId ?? null)).toEqual(["exp-1", null]);
    expect(load.timingRows).toEqual([timingRow]);
    expect(load.factsNote).toBe("");
    expect(load.servingError).toBe("");
  });

  test("facts typed unavailable: the table still renders every row, the note says why in words", () => {
    const load = resolveServingLoad(ok(serving), ok([]), ok([]), unavailable("stale-config"));
    expect(load.rows?.map((r) => r.alias.modelKey)).toEqual(["a", "b"]);
    expect(load.rows?.every((r) => r.measured === null)).toBe(true);
    expect(load.servingError).toBe("");
    expect(load.factsNote).toContain(factsUnavailableText("stale-config"));
  });

  test("timing/measured/facts-status fetches failing never blank the table", () => {
    const load = resolveServingLoad(ok(serving), fail("timing down"), fail("measured down"), fail("status down"));
    expect(load.rows).toHaveLength(2);
    expect(load.timingRows).toEqual([]);
    expect(load.factsNote).toContain("status down");
  });

  test("only the serving config failing withholds the rows", () => {
    const load = resolveServingLoad(fail("bad config"), ok([timingRow]), ok([]), unavailable("absent"));
    expect(load.rows).toBeNull();
    expect(load.servingError).toBe("bad config");
  });
});
