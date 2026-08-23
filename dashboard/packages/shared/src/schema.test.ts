import { describe, expect, test } from "bun:test";
import {
  CaptureEnvelopeSchema,
  CurveSeriesSchema,
  ExperimentDetailSchema,
  ExperimentSummarySchema,
  HealthResponseSchema,
  LadderRowSchema,
  ServingResponseSchema,
  TrialRowSchema,
} from "./index.ts";

// Wire-schema round-trip tests: every shared schema this WP defines parses a realistic
// wire object (a value the API would actually produce) without throwing, and rejects an
// object missing a required field. Not exhaustive per field — the shape/required-key
// contract is what matters here, not every zod refinement.

describe("ExperimentSummarySchema", () => {
  const valid = {
    id: "exp-1",
    schemaVersion: 2 as const,
    description: "a fixture experiment",
    timestamp: "2026-08-23T00:00:00Z",
    lineage: {
      hfRepo: "org/repo",
      hfRevision: null,
      filename: "model.gguf",
      fileSize: 123,
      sha256: null,
      llamaCppBuild: "b10534",
    },
    sweepCell: {
      cellId: "cell-1",
      alias: "alias-1",
      serverFlags: { "-c": 8192 },
      command: "llama-server ...",
    },
    hasWilsonSummary: false,
    trialCount: 3,
    metricCount: 3,
  };

  test("round-trips a valid summary", () => {
    expect(ExperimentSummarySchema.parse(valid)).toEqual(valid);
  });

  test("accepts a null sweepCell (frozen contract: null for a non-sweep full CLI record)", () => {
    expect(() => ExperimentSummarySchema.parse({ ...valid, sweepCell: null })).not.toThrow();
  });

  test("rejects a summary missing lineage", () => {
    const invalid: Record<string, unknown> = { ...valid };
    delete invalid.lineage;
    expect(() => ExperimentSummarySchema.parse(invalid)).toThrow();
  });

  test("ExperimentDetailSchema extends the summary with a nullable wilsonSummary", () => {
    const detail = { ...valid, wilsonSummary: { n: 30, k: 0, rate: 0, sided: "one", confidence: 0.95, lower: null, upper: 0.08 } };
    expect(() => ExperimentDetailSchema.parse(detail)).not.toThrow();
    expect(() => ExperimentDetailSchema.parse({ ...valid, wilsonSummary: null })).not.toThrow();
  });
});

describe("TrialRowSchema", () => {
  test("round-trips a minimal trial row (every optional field null)", () => {
    const row = {
      id: 1,
      experimentId: "exp-1",
      lineIndex: 0,
      type: "future_probe_type",
      trial: null,
      samplingArm: null,
      temperature: null,
      topP: null,
      topK: null,
      samplerSeed: null,
      haystackSeed: null,
      corpusManifestId: null,
      haystackSha256: null,
      classifierVerdict: null,
      classifierShape: null,
      promptTokens: null,
      completionTokens: null,
      finishReason: null,
      positionFraction: null,
      task: null,
      cellId: null,
      correct: null,
      raw: { type: "future_probe_type", some_future_field: 42 },
    };
    expect(() => TrialRowSchema.parse(row)).not.toThrow();
  });
});

describe("LadderRowSchema", () => {
  test("round-trips a ladder row", () => {
    const row = {
      experimentId: "exp-1",
      alias: "alias-1",
      cellId: "cell-1",
      filename: "model.gguf",
      contextLength: 8192,
      kvCacheTypeK: "f16",
      kvCacheTypeV: "f16",
      flashAttention: "on",
      medianDecodeTokS: 62.1,
      medianFirstGenerationTokenMs: 900.2,
      correctnessRate: 0.9,
      trialCount: 10,
    };
    expect(() => LadderRowSchema.parse(row)).not.toThrow();
  });
});

describe("CurveSeriesSchema", () => {
  test("round-trips a curve series with a one-sided Wilson point", () => {
    const series = {
      experimentId: "exp-1",
      points: [
        { task: "single_needle", positionFraction: 0.1, n: 4, k: 1, failureRate: 0.25, sided: "two", confidence: 0.95, lower: 0.01, upper: 0.65 },
      ],
    };
    expect(() => CurveSeriesSchema.parse(series)).not.toThrow();
  });
});

describe("ServingResponseSchema", () => {
  test("round-trips a served-alias response", () => {
    const response = {
      configPath: "/fixtures/llama-config.yaml",
      models: [
        {
          modelKey: "fixture-chat-q4",
          name: "Fixture chat model",
          aliases: ["fixture-chat"],
          ttl: 1800,
          checkEndpoint: "/health",
          flags: { contextLength: 32768, kvCacheTypeK: "q4_0", kvCacheTypeV: "q4_0", flashAttention: "on", gpuLayers: 99 },
        },
      ],
    };
    expect(() => ServingResponseSchema.parse(response)).not.toThrow();
  });
});

describe("CaptureEnvelopeSchema", () => {
  test("requires stability to be exactly \"experimental\"", () => {
    const envelope = { stability: "experimental", generatedAt: "2026-08-23T00:00:00Z", recordCount: 0, records: [] };
    expect(() => CaptureEnvelopeSchema.parse(envelope)).not.toThrow();
    expect(() => CaptureEnvelopeSchema.parse({ ...envelope, stability: "stable" })).toThrow();
  });
});

describe("HealthResponseSchema", () => {
  test("requires status to be exactly \"ok\"", () => {
    expect(() => HealthResponseSchema.parse({ status: "ok", experimentCount: 0 })).not.toThrow();
    expect(() => HealthResponseSchema.parse({ status: "degraded", experimentCount: 0 })).toThrow();
  });
});
