import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import type { ServingAlias } from "@bench-dashboard/shared";
import { readServingConfig } from "./llamaConfig.ts";
import {
  decodeServerFlags,
  INVALID_SERVER_FLAGS,
  normalizeModelPath,
  recipeEquals,
  recipeFromServedAlias,
  recipeFromServerFlags,
  resolveServedModel,
  type Recipe,
} from "./recipe.ts";

// The real committed `llama/config.yaml`, read-only, as a fixture source (sanctioned
// exception -- see `llamaConfig.test.ts`'s header comment for the same posture): these
// resolution-order fixtures are drawn from the ACTUAL served corpus (`llama/bakeoff/
// records/…`, cited per test) so the resolver is proven against real recipe shapes, not
// hand-authored stand-ins that could drift from them.
const REAL_CONFIG_PATH = join(import.meta.dir, "../../../../../llama/config.yaml");

function fullServingAlias(overrides: Partial<ServingAlias> & Pick<ServingAlias, "modelKey" | "aliases">): ServingAlias {
  return {
    name: null,
    ttl: null,
    checkEndpoint: null,
    flags: { contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null, allFlags: [] },
    capability: null,
    recipe: { modelPath: null, draftPath: null, specType: null, moeCpuLayers: null, kvK: "f16", kvV: "f16" },
    ...overrides,
  };
}

describe("normalizeModelPath", () => {
  test("rewrites the stale /srv/models/ prefix to /models/", () => {
    expect(normalizeModelPath("/srv/models/unsloth/Foo-GGUF/Foo.gguf")).toBe("/models/unsloth/Foo-GGUF/Foo.gguf");
  });

  test("a path already under /models/ passes through unchanged", () => {
    expect(normalizeModelPath("/models/unsloth/Foo-GGUF/Foo.gguf")).toBe("/models/unsloth/Foo-GGUF/Foo.gguf");
  });

  test("a path under neither prefix passes through unchanged (never a guessed rewrite)", () => {
    expect(normalizeModelPath("/some/other/path/model.gguf")).toBe("/some/other/path/model.gguf");
  });
});

describe("recipeFromServerFlags", () => {
  test("-ctk/-ctv absent resolve to f16, matching the config-side extractRecipe default", () => {
    const recipe = recipeFromServerFlags({ "-ngl": 99, "-c": 131072 });
    if (recipe === null) throw new Error("unreachable");
    expect(recipe.kvK).toBe("f16");
    expect(recipe.kvV).toBe("f16");
    expect(recipe.mtp).toBe(false);
    expect(recipe.specType).toBeNull();
    expect(recipe.moeCpuLayers).toBeNull();
  });

  test("--spec-type present marks mtp true and carries the spec type text verbatim", () => {
    const recipe = recipeFromServerFlags({ "--spec-type": "draft-mtp", "-ncmoe": 32 });
    if (recipe === null) throw new Error("unreachable");
    expect(recipe.mtp).toBe(true);
    expect(recipe.specType).toBe("draft-mtp");
    expect(recipe.moeCpuLayers).toBe(32);
  });

  test("-md present (sidecar draft, no --spec-type) also marks mtp true", () => {
    const recipe = recipeFromServerFlags({ "-md": "/models/foo/mtp-draft.gguf" });
    if (recipe === null) throw new Error("unreachable");
    expect(recipe.mtp).toBe(true);
    expect(recipe.specType).toBeNull();
  });

  test("null flags (no sweep_server_flags at all) resolve to the all-defaults recipe", () => {
    const recipe = recipeFromServerFlags(null);
    expect(recipe).toEqual({ mtp: false, specType: null, moeCpuLayers: null, kvK: "f16", kvV: "f16" });
  });

  test("a JSON-string-encoded flags object (the real DB column shape), decoded then projected, matches the plain object directly projected", () => {
    const flags = { "-ctk": "q4_0", "-ctv": "q4_0" };
    const asObject = recipeFromServerFlags(flags);
    const decoded = decodeServerFlags(JSON.stringify(flags));
    if (decoded === null || decoded === INVALID_SERVER_FLAGS) throw new Error("unreachable");
    const asDecodedString = recipeFromServerFlags(decoded);
    expect(asDecodedString).toEqual(asObject);
  });

  test("a present recipe-bearing key with the wrong type refuses (null), never a guessed default", () => {
    expect(recipeFromServerFlags({ "-ctk": 7 })).toBeNull();
    expect(recipeFromServerFlags({ "--spec-type": true })).toBeNull();
    expect(recipeFromServerFlags({ "-ncmoe": "3" })).toBeNull();
    expect(recipeFromServerFlags({ "-ncmoe": 3.5 })).toBeNull();
  });
});

describe("decodeServerFlags", () => {
  test("a null column decodes to null (the genuinely-absent, all-defaults case)", () => {
    expect(decodeServerFlags(null)).toBeNull();
  });

  test("a valid JSON object string decodes to the parsed object", () => {
    expect(decodeServerFlags('{"-ctk":"q4_0"}')).toEqual({ "-ctk": "q4_0" });
  });

  test("malformed JSON decodes to INVALID_SERVER_FLAGS, never {}", () => {
    expect(decodeServerFlags("{broken")).toBe(INVALID_SERVER_FLAGS);
  });

  test("serialized non-object JSON (array/string/number) decodes to INVALID_SERVER_FLAGS, never {}", () => {
    expect(decodeServerFlags("[1,2]")).toBe(INVALID_SERVER_FLAGS);
    expect(decodeServerFlags('"x"')).toBe(INVALID_SERVER_FLAGS);
    expect(decodeServerFlags("7")).toBe(INVALID_SERVER_FLAGS);
  });
});

describe("recipeEquals", () => {
  const base: Recipe = { modelPath: "/models/a.gguf", mtp: false, moeCpuLayers: null, kvK: "f16", kvV: "f16" };

  test("identical tuples are equal", () => {
    expect(recipeEquals(base, { ...base })).toBe(true);
  });

  test("context length and -fa are not part of the tuple -- recipeEquals never even sees them (compile-time proof: Recipe has no such fields)", () => {
    // The type itself carries no contextLength/-fa field -- this test documents the
    // exclusion rather than exercising a runtime branch.
    expect(Object.keys(base).sort()).toEqual(["kvK", "kvV", "modelPath", "moeCpuLayers", "mtp"].sort());
  });

  test("differing kvK/kvV makes the tuple unequal", () => {
    expect(recipeEquals(base, { ...base, kvK: "q4_0", kvV: "q4_0" })).toBe(false);
  });

  test("differing mtp makes the tuple unequal", () => {
    expect(recipeEquals(base, { ...base, mtp: true })).toBe(false);
  });

  test("differing moeCpuLayers makes the tuple unequal", () => {
    expect(recipeEquals(base, { ...base, moeCpuLayers: 32 })).toBe(false);
  });

  test("differing modelPath makes the tuple unequal", () => {
    expect(recipeEquals(base, { ...base, modelPath: "/models/b.gguf" })).toBe(false);
  });
});

describe("resolveServedModel: Qwen3.6 MTP-vs-plain path disambiguation (S2)", () => {
  // Basename alone is ambiguous: `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` exists under both the
  // MTP-GGUF repo (served, embedded MTP heads) and the plain GGUF repo (pre-2026-08-25
  // records) -- only the full path disambiguates. Synthetic served list (the real
  // `config.yaml` only carries the MTP entry today; both existed at once historically).
  const mtpAlias = fullServingAlias({
    modelKey: "Qwen3.6-35B-A3B-UD-Q4_K_XL-mtp",
    aliases: ["qwen36-a3b"],
    recipe: {
      modelPath: "/models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
      draftPath: null,
      specType: "draft-mtp",
      moeCpuLayers: 32,
      kvK: "f16",
      kvV: "f16",
    },
  });
  const plainAlias = fullServingAlias({
    modelKey: "Qwen3.6-35B-A3B-UD-Q4_K_XL-plain",
    aliases: ["qwen36-a3b-plain"],
    recipe: {
      modelPath: "/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
      draftPath: null,
      specType: null,
      moeCpuLayers: null,
      kvK: "f16",
      kvV: "f16",
    },
  });
  const servedAliases = [mtpAlias, plainAlias];

  test("a record path under the plain GGUF dir resolves to the plain model, never the MTP one", () => {
    const resolved = resolveServedModel(
      {
        modelHostPath: "/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        sweepAlias: null,
        schemaOrigin: "v2",
        sweepServerFlags: null,
      },
      servedAliases,
    );
    expect(resolved?.modelKey).toBe("Qwen3.6-35B-A3B-UD-Q4_K_XL-plain");
    expect(resolved?.mtp).toBe(false);
  });

  test("a record path under the MTP GGUF dir resolves to the MTP model, never the plain one", () => {
    const resolved = resolveServedModel(
      {
        modelHostPath: "/srv/models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        sweepAlias: null,
        schemaOrigin: "v2",
        sweepServerFlags: JSON.stringify({ "--spec-type": "draft-mtp", "-ncmoe": 32 }),
      },
      servedAliases,
    );
    expect(resolved?.modelKey).toBe("Qwen3.6-35B-A3B-UD-Q4_K_XL-mtp");
    expect(resolved?.recipeMatch).toBe(true);
  });
});

describe("resolveServedModel: resolution order against the real config + real corpus fixtures", () => {
  test("a v2 matrix cell (real qwen36-a3b production MTP cell, matrix-v1-dedicated-prod) matches with recipeMatch:true", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    // llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen36-c128k-f16-mtp-prod-…/config.json
    const resolved = resolveServedModel(
      {
        modelHostPath: "/srv/models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
        sweepAlias: "mxdp-qwen36-c128k-f16-mtp-prod",
        schemaOrigin: "v2",
        sweepServerFlags: JSON.stringify({ "-ctk": "f16", "-ctv": "f16", "-fa": "auto", "-ngl": 99, "-ncmoe": 32, "--spec-type": "draft-mtp", "-ub": 2048, "-c": 131072 }),
      },
      models,
    );
    expect(resolved).toMatchObject({ modelKey: "Qwen3.6-35B-A3B-UD-Q4_K_XL", recipeMatch: true, mtp: true, moeCpuLayers: 32, specType: "draft-mtp" });
    expect(resolved?.aliases).toEqual(["qwen36-a3b"]);
  });

  test("an unmatched sdr-* scratch cell (MiniCPM5 path, not in config.yaml) resolves to null", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    // llama/bakeoff/records/small-dense-roofline-v1/sdr-minicpm5-1b-f16-…/config.json
    const resolved = resolveServedModel(
      {
        modelHostPath: "/models/openbmb/MiniCPM5-1B-GGUF/MiniCPM5-1B-F16.gguf",
        sweepAlias: "sdr-minicpm5-1b-f16",
        schemaOrigin: "v2",
        sweepServerFlags: JSON.stringify({ "-ctk": "f16", "-ctv": "f16", "-fa": "auto", "-ngl": 99, "-c": 16384 }),
      },
      models,
    );
    expect(resolved).toBeNull();
  });

  test("the matched sdr-nemotron-…-ctkf16 cell resolves to nemotron with recipeMatch:true, arm null", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    // llama/bakeoff/records/small-dense-roofline-v1/sdr-nemotron-4b-udq4xl-ctl-…/config.json
    const resolved = resolveServedModel(
      {
        modelHostPath: "/models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf",
        sweepAlias: "sdr-nemotron-4b-udq4xl-ctl",
        schemaOrigin: "v2",
        sweepServerFlags: JSON.stringify({ "-ctk": "f16", "-ctv": "f16", "-fa": "auto", "-ngl": 99, "-c": 16384 }),
      },
      models,
    );
    expect(resolved).toMatchObject({ modelKey: "nemotron-3-nano-4B-UD-Q4_K_XL", recipeMatch: true, mtp: false, moeCpuLayers: null, specType: null });
  });

  test("a v1 ling -full row (schema_origin v1-timing, alias ling-3-tiny-q8) is admitted -- served recipe is the v1 baseline", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    // llama/bakeoff/records/ling-3-tiny-q8-full/config.json (top-level `alias`, no sweep_cell)
    const resolved = resolveServedModel(
      { modelHostPath: null, sweepAlias: "ling-3-tiny-q8", schemaOrigin: "v1-timing", sweepServerFlags: null },
      models,
    );
    expect(resolved).toMatchObject({ modelKey: "Ling-3.0-tiny-Q8_0", recipeMatch: true, mtp: false, moeCpuLayers: null, specType: null });
  });

  test("a v1 gemma -full row against gemma's MTP served recipe is excluded (never a mismatched recipeMatch:false)", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    // llama/bakeoff/records/gemma-4-12b-qat-full/config.json (top-level `alias`, pre-MTP measurement)
    const resolved = resolveServedModel(
      { modelHostPath: null, sweepAlias: "gemma-4-12b-qat", schemaOrigin: "v1-timing", sweepServerFlags: null },
      models,
    );
    expect(resolved).toBeNull();
  });

  test("a q4/q4 cell against a synthetic f16 served recipe resolves with recipeMatch:false", () => {
    const syntheticServedAlias = fullServingAlias({
      modelKey: "fixture-synthetic-f16",
      aliases: ["fixture-synthetic"],
      recipe: { modelPath: "/models/synthetic/fixture-model.gguf", draftPath: null, specType: null, moeCpuLayers: null, kvK: "f16", kvV: "f16" },
    });
    const resolved = resolveServedModel(
      {
        modelHostPath: "/models/synthetic/fixture-model.gguf",
        sweepAlias: "fixture-scratch-q4q4",
        schemaOrigin: "v2",
        sweepServerFlags: JSON.stringify({ "-ctk": "q4_0", "-ctv": "q4_0" }),
      },
      [syntheticServedAlias],
    );
    expect(resolved).toMatchObject({ modelKey: "fixture-synthetic-f16", recipeMatch: false });
  });

  test("the real qwen38-27b-iq3s production cell against the real config matches with recipeMatch:true (served recipe IS q4/q4, not f16)", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    // llama/bakeoff/records/matrix-v1-dedicated-prod/mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-…/config.json
    const resolved = resolveServedModel(
      {
        modelHostPath: "/srv/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf",
        sweepAlias: "mxdp-qwen38-iq3s-c128k-q4q4-fa-prod",
        schemaOrigin: "v2",
        sweepServerFlags: JSON.stringify({ "-ctk": "q4_0", "-ctv": "q4_0", "-fa": "on", "-ngl": 99, "-c": 131072 }),
      },
      models,
    );
    expect(resolved).toMatchObject({ modelKey: "Qwen3.8-27B-UD-IQ3_S", recipeMatch: true, mtp: false, moeCpuLayers: null, specType: null });
    expect(resolved?.aliases).toEqual(["qwen38-27b-iq3s"]);
  });

  test("a v2 row with no model_host_path and no v1-timing origin (a pre-campaign probe with no path) resolves to null", () => {
    const resolved = resolveServedModel(
      { modelHostPath: null, sweepAlias: "some-scratch-alias", schemaOrigin: "v2", sweepServerFlags: null },
      [],
    );
    expect(resolved).toBeNull();
  });

  test("recipeFromServedAlias reduces the real config's own recipe the same way resolveServedModel does internally", async () => {
    const { models } = await readServingConfig(REAL_CONFIG_PATH);
    const qwen38 = models.find((m) => m.modelKey === "Qwen3.8-27B-UD-IQ3_S");
    expect(qwen38).toBeDefined();
    if (qwen38 === undefined) throw new Error("unreachable");
    expect(recipeFromServedAlias(qwen38)).toEqual({
      modelPath: "/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf",
      mtp: false,
      moeCpuLayers: null,
      kvK: "q4_0",
      kvV: "q4_0",
    });
  });
});

// F1 (`reviews/2026-09-05-tn-code-review.md`, `plans/served-recipe-boundary-remediation.md`
// WP-R1): invalid stored evidence must resolve to `null` -- never a defaulted/guessed
// match -- through the actual resolver, not merely through `recipeFromServerFlags` in
// isolation.
describe("resolveServedModel: F1 -- invalid stored evidence never resolves, never defaults", () => {
  const path = "/models/fixture-org/fixture-boundary-GGUF/model.gguf";
  const servedAlias = fullServingAlias({
    modelKey: "fixture-boundary-f16",
    aliases: ["fixture-boundary"],
    recipe: { modelPath: path, draftPath: null, specType: null, moeCpuLayers: null, kvK: "f16", kvV: "f16" },
  });

  function resolveWithFlags(sweepServerFlags: string | null): ReturnType<typeof resolveServedModel> {
    return resolveServedModel({ modelHostPath: path, sweepAlias: null, schemaOrigin: "v2", sweepServerFlags }, [servedAlias]);
  }

  test("malformed stored JSON ('{broken') resolves to null, never {} defaults", () => {
    expect(resolveWithFlags("{broken")).toBeNull();
  });

  test("a serialized JSON array ('[1,2]') resolves to null", () => {
    expect(resolveWithFlags("[1,2]")).toBeNull();
  });

  test("a serialized JSON string ('\"x\"') resolves to null", () => {
    expect(resolveWithFlags('"x"')).toBeNull();
  });

  test("a serialized JSON number ('7') resolves to null", () => {
    expect(resolveWithFlags("7")).toBeNull();
  });

  test("a numeric -ctk ('{\"-ctk\":7}') resolves to null, never defaults kvK to f16", () => {
    expect(resolveWithFlags(JSON.stringify({ "-ctk": 7 }))).toBeNull();
  });

  test("a boolean --spec-type resolves to null, never defaults mtp to false", () => {
    expect(resolveWithFlags(JSON.stringify({ "--spec-type": true }))).toBeNull();
  });

  test("a string -ncmoe ('{\"-ncmoe\":\"3\"}') resolves to null, never defaults moeCpuLayers to null", () => {
    expect(resolveWithFlags(JSON.stringify({ "-ncmoe": "3" }))).toBeNull();
  });

  test("a non-integer -ncmoe (3.5) resolves to null", () => {
    expect(resolveWithFlags(JSON.stringify({ "-ncmoe": 3.5 }))).toBeNull();
  });

  test("a null sweep_server_flags column resolves all-defaults with recipeMatch:true against an f16 served recipe", () => {
    expect(resolveWithFlags(null)).toMatchObject({ recipeMatch: true, mtp: false, moeCpuLayers: null });
  });

  test("'{}' resolves identically to a null column -- all-defaults, recipeMatch:true", () => {
    expect(resolveWithFlags("{}")).toMatchObject({ recipeMatch: true, mtp: false, moeCpuLayers: null });
  });

  test("a valid numeric -ncmoe (32) matched against the served recipe's own -ncmoe 32 resolves recipeMatch:true", () => {
    const mtpServedAlias = fullServingAlias({
      modelKey: "fixture-boundary-mtp",
      aliases: ["fixture-boundary-mtp-alias"],
      recipe: { modelPath: path, draftPath: null, specType: "draft-mtp", moeCpuLayers: 32, kvK: "f16", kvV: "f16" },
    });
    const resolved = resolveServedModel(
      { modelHostPath: path, sweepAlias: null, schemaOrigin: "v2", sweepServerFlags: JSON.stringify({ "--spec-type": "draft-mtp", "-ncmoe": 32 }) },
      [mtpServedAlias],
    );
    expect(resolved).toMatchObject({ recipeMatch: true, mtp: true, moeCpuLayers: 32 });
  });

  test("-md (sidecar draft, no --spec-type) also marks mtp:true through the resolver", () => {
    const resolved = resolveServedModel(
      { modelHostPath: path, sweepAlias: null, schemaOrigin: "v2", sweepServerFlags: JSON.stringify({ "-md": "/models/fixture-org/fixture-boundary-GGUF/draft.gguf" }) },
      [servedAlias],
    );
    expect(resolved?.mtp).toBe(true);
    // the served recipe itself has no MTP -- off-recipe, never a guessed match
    expect(resolved?.recipeMatch).toBe(false);
  });

  test("a served alias with recipe:null is never joined on the path branch (find skips it, never manufactures a match)", () => {
    const noRecipeAlias = fullServingAlias({ modelKey: "fixture-no-recipe", aliases: ["fixture-no-recipe-alias"], recipe: null });
    const resolved = resolveServedModel(
      { modelHostPath: path, sweepAlias: null, schemaOrigin: "v2", sweepServerFlags: null },
      [noRecipeAlias],
    );
    expect(resolved).toBeNull();
  });

  test("a served alias with recipe:null is never admitted as the v1 baseline (a served model with no recipe is not the v1 baseline)", () => {
    const noRecipeAlias = fullServingAlias({ modelKey: "fixture-no-recipe-v1", aliases: ["fixture-no-recipe-v1-alias"], recipe: null });
    const resolved = resolveServedModel(
      { modelHostPath: null, sweepAlias: "fixture-no-recipe-v1-alias", schemaOrigin: "v1-timing", sweepServerFlags: null },
      [noRecipeAlias],
    );
    expect(resolved).toBeNull();
  });

  test("recipeFromServedAlias itself returns null for recipe:null and for a null modelPath", () => {
    expect(recipeFromServedAlias(fullServingAlias({ modelKey: "k1", aliases: ["a1"], recipe: null }))).toBeNull();
    expect(
      recipeFromServedAlias(
        fullServingAlias({
          modelKey: "k2",
          aliases: ["a2"],
          recipe: { modelPath: null, draftPath: null, specType: null, moeCpuLayers: null, kvK: "f16", kvV: "f16" },
        }),
      ),
    ).toBeNull();
  });
});
