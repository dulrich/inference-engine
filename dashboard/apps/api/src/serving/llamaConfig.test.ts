import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { readServingConfig } from "./llamaConfig.ts";

// Every assertion in the `readServingConfig` describe block below reads
// `fixtures/llama-config.yaml`, never the real `llama/config.yaml` — brief hard
// constraint: "tests must pass from fixtures alone." The separate `recipe projection`
// describe block further down is a sanctioned, scoped exception (WP-1 dispatch brief: "may
// read the real `llama/config.yaml` text read-only as a fixture source") -- it reads the
// real committed file as a READ-ONLY fixture, verifying the recipe projection against the
// actual served corpus rather than a hand-authored stand-in; it never writes the file
// (`readServingConfig` itself is read-only regardless of path).
const FIXTURE_PATH = join(import.meta.dir, "../../fixtures/llama-config.yaml");
const REAL_CONFIG_PATH = join(import.meta.dir, "../../../../../llama/config.yaml");

describe("readServingConfig", () => {
  test("parses served aliases with macro-expanded flags", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    expect(response.configPath).toBe(FIXTURE_PATH);

    const embed = response.models.find((m) => m.modelKey === "fixture-embed-f16");
    expect(embed).toBeDefined();
    expect(embed?.aliases).toEqual(["fixture-embed"]);
    // Flags come from the macro (`${llama_server_common}`), not the model's own cmd lines.
    expect(embed?.flags.contextLength).toBe(8192);
    expect(embed?.flags.gpuLayers).toBe(99);
  });

  test("parses a model's own cmd flags (no macro)", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    const chat = response.models.find((m) => m.modelKey === "fixture-chat-q4");
    expect(chat?.flags).toEqual({
      contextLength: 32768,
      kvCacheTypeK: "q4_0",
      kvCacheTypeV: "q4_0",
      flashAttention: "on",
      gpuLayers: 99,
      allFlags: [
        { flag: "--host", value: "0.0.0.0" },
        { flag: "--port", value: "${PORT}" },
        { flag: "-m", value: "/models/example-org/fixture-chat-gguf/fixture-chat-Q4_K_M.gguf" },
        { flag: "-c", value: "32768" },
        { flag: "-ctk", value: "q4_0" },
        { flag: "-ctv", value: "q4_0" },
        { flag: "-fa", value: "on" },
        { flag: "-ngl", value: "99" },
      ],
    });
    expect(chat?.ttl).toBe(1800);
    expect(chat?.checkEndpoint).toBe("/health");
  });

  test("allFlags: ordering preserved, macro-expanded flags included, pre-flag binary token excluded, trailing boolean flag", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    const embed = response.models.find((m) => m.modelKey === "fixture-embed-f16");
    // The macro (`${llama_server_common}`) expands to `/usr/local/bin/llama-server --host
    // 0.0.0.0 --port ${PORT} -ngl 99 -c 8192 -b 8192 -ub 8192`; the model's own cmd then
    // appends `-m <path>` and a trailing boolean `--embeddings` (no token follows it at
    // all -- the other boolean sub-case from `fixture-flags-edge`, where a boolean flag is
    // instead followed immediately by another flag).
    expect(embed?.flags.allFlags).toEqual([
      { flag: "--host", value: "0.0.0.0" },
      { flag: "--port", value: "${PORT}" },
      { flag: "-ngl", value: "99" },
      { flag: "-c", value: "8192" },
      { flag: "-b", value: "8192" },
      { flag: "-ub", value: "8192" },
      { flag: "-m", value: "/models/example-org/fixture-embed-gguf/fixture-embed-f16.gguf" },
      { flag: "--embeddings", value: null },
    ]);
    // The binary path itself (before the first flag) is never a flag/value pair.
    expect(embed?.flags.allFlags.some((entry) => entry.flag.includes("llama-server"))).toBe(false);
  });

  test("allFlags: negative number and decimal are values (never mistaken for flags), boolean flag followed by another flag", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    const edge = response.models.find((m) => m.modelKey === "fixture-flags-edge");
    expect(edge?.flags.allFlags).toEqual([
      { flag: "--host", value: "0.0.0.0" },
      { flag: "--port", value: "${PORT}" },
      { flag: "-m", value: "/models/example-org/fixture-edge-gguf/fixture-edge.gguf" },
      { flag: "--jinja", value: null },
      { flag: "--temp", value: "0.8" },
      { flag: "--top-p", value: "-1" },
      { flag: "-ngl", value: "99" },
    ]);
  });

  test("filters out a model with no aliases (brief's \"filtered\" pin)", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    expect(response.models.some((m) => m.modelKey === "fixture-unaliased")).toBe(false);
  });

  test("parses a full capability block, incl. both optional serving keys", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    const chat = response.models.find((m) => m.modelKey === "fixture-chat-q4");
    expect(chat?.capability).toEqual({
      contextLength: 32768,
      inputModalities: ["text"],
      outputModalities: ["text"],
      toolCalling: true,
      reasoning: "request-controllable",
      serving: {
        quant: "Q4_K_M",
        kvCacheType: "q4_0",
        flashAttention: true,
        mtp: false,
        moeCpuOffloadLayers: 8,
        ubatch: 2048,
        reasoningMode: null,
        reasoningBudget: null,
      },
      // WP-1 (`plans/bench-dashboard-serving-control.md`): absent `sampling:`/`max_output:`/
      // `sources:` blocks resolve to `null`, never `{}` -- "no recipe research yet" is a
      // different fact from "researched, and the card is silent", which is what a `sources`
      // entry with `url: "none"` records.
      sampling: null,
      maxOutput: null,
      sources: null,
    });
  });

  test("a capability block with no optional serving keys resolves them to null, never a guessed 0", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    const embed = response.models.find((m) => m.modelKey === "fixture-embed-f16");
    expect(embed?.capability).toEqual({
      contextLength: 8192,
      inputModalities: ["text"],
      outputModalities: ["embedding"],
      toolCalling: false,
      reasoning: "none",
      serving: {
        quant: "F16",
        kvCacheType: "f16",
        flashAttention: false,
        mtp: false,
        moeCpuOffloadLayers: null,
        ubatch: null,
        reasoningMode: null,
        reasoningBudget: null,
      },
      sampling: null,
      maxOutput: null,
      sources: null,
    });
  });

  // WP-1 (`plans/bench-dashboard-serving-control.md`): the advisory block and its provenance
  // map reach the parsed capability at all -- `sampling` camelCased leaf by leaf, `sources`
  // carried through VERBATIM (dotted YAML keys, unnormalized entries), because validating
  // provenance belongs to `scripts/check-serving-recipes.ts` and a reader that repaired a
  // malformed entry would make that gate blind to it.
  test("carries sampling, max_output and the sources provenance map through to capability", async () => {
    const response = await readServingConfig(join(import.meta.dir, "../../fixtures/serving/conforming.yaml"));
    const chat = response.models.find((m) => m.modelKey === "fixture-chat-UD-Q4_K_XL");
    expect(chat?.capability?.sampling).toEqual({
      temperature: 0.6,
      topP: 0.95,
      topK: 20,
      minP: 0,
      presencePenalty: null,
      repetitionPenalty: null,
    });
    expect(chat?.capability?.maxOutput).toEqual({ reasoningBudget: -1, answerTokens: 32000 });
    expect(chat?.capability?.sources?.["sampling.temperature"]).toEqual({
      url: "https://huggingface.co/fixturelab/Fixture-Chat-12B",
      verified: "2026-09-13",
    });
    expect(chat?.capability?.sources?.["sampling.presence_penalty"]).toEqual({
      url: "none",
      reason: "the card is silent on this value",
    });
  });

  test("a model with no metadata block at all resolves capability to null", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    const edge = response.models.find((m) => m.modelKey === "fixture-flags-edge");
    expect(edge?.capability).toBeNull();
  });

  test("never writes the source file (read-only over serving config)", async () => {
    const before = await Bun.file(FIXTURE_PATH).text();
    await readServingConfig(FIXTURE_PATH);
    const after = await Bun.file(FIXTURE_PATH).text();
    expect(after).toBe(before);
  });
});

// WP-1 (`plans/served-recipe-p50-prefill-and-decode.md`) recipe projection -- reads the
// REAL committed `llama/config.yaml`, read-only, as a fixture source (sanctioned exception,
// see the header comment above): the point of these assertions is that the projection
// matches the ACTUAL served corpus's two MTP artifact forms (sidecar draft + embedded
// heads) and its q4/q4 KV recipe, not a hand-authored stand-in that could drift from it.
describe("readServingConfig: recipe projection (WP-1, real config)", () => {
  test("gemma-4-12b-qat: sidecar MTP draft (-md + --spec-type draft-mtp)", async () => {
    const response = await readServingConfig(REAL_CONFIG_PATH);
    const gemma = response.models.find((m) => m.modelKey === "gemma-4-12B-it-qat-UD-Q4_K_XL");
    expect(gemma?.recipe).toEqual({
      modelPath: "/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
      draftPath: "/models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
      specType: "draft-mtp",
      moeCpuLayers: null,
      kvK: "f16",
      kvV: "f16",
    });
  });

  test("qwen36-a3b: embedded MTP heads (--spec-type draft-mtp, no -md) plus -ncmoe 32", async () => {
    const response = await readServingConfig(REAL_CONFIG_PATH);
    const qwen36 = response.models.find((m) => m.modelKey === "Qwen3.6-35B-A3B-UD-Q4_K_XL");
    expect(qwen36?.recipe).toEqual({
      modelPath: "/models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf",
      draftPath: null,
      specType: "draft-mtp",
      moeCpuLayers: 32,
      kvK: "f16",
      kvV: "f16",
    });
  });

  test("qwen38-27b-iq3s: no MTP, quantized q4_0/q4_0 KV (the served recipe, not f16)", async () => {
    const response = await readServingConfig(REAL_CONFIG_PATH);
    const qwen38 = response.models.find((m) => m.modelKey === "Qwen3.8-27B-UD-IQ3_S");
    expect(qwen38?.recipe).toEqual({
      modelPath: "/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf",
      draftPath: null,
      specType: null,
      moeCpuLayers: null,
      kvK: "q4_0",
      kvV: "q4_0",
    });
  });

  test("ling-3-tiny-q8: plain -- no MTP, no -ncmoe, -ctk/-ctv absent so kvK/kvV default to f16", async () => {
    const response = await readServingConfig(REAL_CONFIG_PATH);
    const ling = response.models.find((m) => m.modelKey === "Ling-3.0-tiny-Q8_0");
    expect(ling?.recipe).toEqual({
      modelPath: "/models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q8_0.gguf",
      draftPath: null,
      specType: null,
      moeCpuLayers: null,
      kvK: "f16",
      kvV: "f16",
    });
  });

  test("every served model: recipe MTP (specType or draftPath present) agrees with capability.serving.mtp", async () => {
    const response = await readServingConfig(REAL_CONFIG_PATH);
    expect(response.models.length).toBeGreaterThan(0);
    for (const model of response.models) {
      const recipeMtp = model.recipe !== null && (model.recipe.specType !== null || model.recipe.draftPath !== null);
      expect(recipeMtp).toBe(model.capability?.serving.mtp ?? false);
    }
  });
});
