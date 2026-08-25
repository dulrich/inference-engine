import { describe, expect, test } from "bun:test";
import { join } from "node:path";
import { readServingConfig } from "./llamaConfig.ts";

// Every assertion here reads `fixtures/llama-config.yaml`, never the real
// `llama/config.yaml` — brief hard constraint: "tests must pass from fixtures alone."
const FIXTURE_PATH = join(import.meta.dir, "../../fixtures/llama-config.yaml");

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
      },
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
      },
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
