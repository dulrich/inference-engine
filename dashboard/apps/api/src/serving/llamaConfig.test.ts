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
    });
    expect(chat?.ttl).toBe(1800);
    expect(chat?.checkEndpoint).toBe("/health");
  });

  test("filters out a model with no aliases (brief's \"filtered\" pin)", async () => {
    const response = await readServingConfig(FIXTURE_PATH);
    expect(response.models.some((m) => m.modelKey === "fixture-unaliased")).toBe(false);
  });

  test("never writes the source file (read-only over serving config)", async () => {
    const before = await Bun.file(FIXTURE_PATH).text();
    await readServingConfig(FIXTURE_PATH);
    const after = await Bun.file(FIXTURE_PATH).text();
    expect(after).toBe(before);
  });
});
