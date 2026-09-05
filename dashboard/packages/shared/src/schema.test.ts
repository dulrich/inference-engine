import { describe, expect, test } from "bun:test";
import {
  CaptureEnvelopeSchema,
  CurveSeriesSchema,
  ExperimentDetailSchema,
  ExperimentSummarySchema,
  HealthResponseSchema,
  LadderRowSchema,
  QueueListResponseSchema,
  ServingResponseSchema,
  ServingTimingRowSchema,
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
    samplingArm: "production",
    battery: ["needle", "vram"],
    vramProfile: "desktop-shared",
    trialCount: 3,
    metricCount: 3,
  };

  test("round-trips a valid summary", () => {
    expect(ExperimentSummarySchema.parse(valid)).toEqual(valid);
  });

  test("accepts a null sweepCell (frozen contract: null for a non-sweep full CLI record)", () => {
    expect(() => ExperimentSummarySchema.parse({ ...valid, sweepCell: null })).not.toThrow();
  });

  test("accepts null samplingArm/battery/vramProfile (absent on any record predating WP-A)", () => {
    expect(() =>
      ExperimentSummarySchema.parse({ ...valid, samplingArm: null, battery: null, vramProfile: null }),
    ).not.toThrow();
  });

  test("rejects a summary missing lineage", () => {
    const invalid: Record<string, unknown> = { ...valid };
    delete invalid.lineage;
    expect(() => ExperimentSummarySchema.parse(invalid)).toThrow();
  });

  test("ExperimentDetailSchema extends the summary with a nullable wilsonSummary and responseCaps", () => {
    const detail = {
      ...valid,
      wilsonSummary: { n: 30, k: 0, rate: 0, sided: "one", confidence: 0.95, lower: null, upper: 0.08 },
      responseCaps: { needle: 4096, default: 2048 },
    };
    expect(() => ExperimentDetailSchema.parse(detail)).not.toThrow();
    expect(() => ExperimentDetailSchema.parse({ ...valid, wilsonSummary: null, responseCaps: null })).not.toThrow();
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
  test("round-trips a served-alias response with an explicit null capability", () => {
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
          capability: null,
        },
      ],
    };
    const parsed = ServingResponseSchema.parse(response);
    expect(parsed.models[0]?.capability).toBeNull();
  });

  test("round-trips a full capability block (WP-2, incl. optional serving keys)", () => {
    const response = {
      configPath: "/fixtures/llama-config.yaml",
      models: [
        {
          modelKey: "gemma-4-12B-it-qat-UD-Q4_K_XL",
          name: "Gemma 4 12B it QAT (UD-Q4_K_XL)",
          aliases: ["gemma-4-12b-qat"],
          ttl: 1800,
          checkEndpoint: "/health",
          flags: { contextLength: 131072, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: 99 },
          capability: {
            contextLength: 131072,
            inputModalities: ["text", "image"],
            outputModalities: ["text"],
            toolCalling: true,
            reasoning: "request-controllable",
            serving: {
              quant: "UD-Q4_K_XL",
              kvCacheType: "f16",
              flashAttention: false,
              mtp: true,
              moeCpuOffloadLayers: null,
              ubatch: null,
            },
          },
        },
      ],
    };
    const parsed = ServingResponseSchema.parse(response);
    expect(parsed.models[0]?.capability).toEqual(response.models[0]?.capability);
  });

  test("accepts a `capability` entirely absent from the payload (backward-compat default)", () => {
    const response = {
      configPath: "/fixtures/llama-config.yaml",
      models: [
        {
          modelKey: "fixture-chat-q4",
          name: null,
          aliases: ["fixture-chat"],
          ttl: null,
          checkEndpoint: null,
          flags: { contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null },
        },
      ],
    };
    const parsed = ServingResponseSchema.parse(response);
    expect(parsed.models[0]?.capability).toBeNull();
  });

  // WP-1 (`plans/served-recipe-p50-prefill-and-decode.md`): `recipe` is additive the same
  // way `capability` is above -- a payload predating it still round-trips as `recipe: null`.
  test("accepts a `recipe` entirely absent from the payload (backward-compat default)", () => {
    const response = {
      configPath: "/fixtures/llama-config.yaml",
      models: [
        {
          modelKey: "fixture-chat-q4",
          name: null,
          aliases: ["fixture-chat"],
          ttl: null,
          checkEndpoint: null,
          flags: { contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null },
        },
      ],
    };
    const parsed = ServingResponseSchema.parse(response);
    expect(parsed.models[0]?.recipe).toBeNull();
  });

  test("round-trips a full recipe (sidecar MTP draft + moeCpuLayers)", () => {
    const response = {
      configPath: "/fixtures/llama-config.yaml",
      models: [
        {
          modelKey: "gemma-4-12B-it-qat-UD-Q4_K_XL",
          name: "Gemma 4 12B it QAT (UD-Q4_K_XL)",
          aliases: ["gemma-4-12b-qat"],
          ttl: 1800,
          checkEndpoint: "/health",
          flags: { contextLength: 131072, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: 99 },
          recipe: {
            modelPath: "/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf",
            draftPath: "/models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            specType: "draft-mtp",
            moeCpuLayers: null,
            kvK: "f16",
            kvV: "f16",
          },
        },
      ],
    };
    const parsed = ServingResponseSchema.parse(response);
    expect(parsed.models[0]?.recipe).toEqual(response.models[0]?.recipe);
  });
});

describe("ServingTimingRowSchema", () => {
  test("round-trips a fully-populated row (modelKey/aliases, p50/p90 prefill+decode, arm, mtp)", () => {
    const row = {
      modelKey: "qwen36-a3b-bakeoff",
      aliases: ["qwen36-a3b"],
      p50PrefillTokS: 366.2,
      p90PrefillTokS: 390.1,
      p50DecodeTokS: 45.6,
      p90DecodeTokS: 47.6,
      samplingArm: "production",
      sampleCount: 3,
      mtp: true,
    };
    expect(ServingTimingRowSchema.parse(row)).toEqual(row);
  });

  test("nullable numeric fields and a null samplingArm (unlabelled arm) parse cleanly", () => {
    const row = {
      modelKey: "nemotron-3-nano-4B-UD-Q4_K_XL",
      aliases: ["nemotron-4b"],
      p50PrefillTokS: null,
      p90PrefillTokS: null,
      p50DecodeTokS: 12.3,
      p90DecodeTokS: 12.3,
      samplingArm: null,
      sampleCount: 3,
      mtp: false,
    };
    expect(ServingTimingRowSchema.parse(row)).toEqual(row);
  });
});

describe("CaptureEnvelopeSchema", () => {
  test("requires captureVersion to be exactly 1 and stability to be exactly \"v1\"", () => {
    const envelope = {
      captureVersion: 1, stability: "v1",
      generatedAt: "2026-08-23T00:00:00Z", recordCount: 0, records: [],
    };
    expect(() => CaptureEnvelopeSchema.parse(envelope)).not.toThrow();
    expect(() => CaptureEnvelopeSchema.parse({ ...envelope, stability: "experimental" })).toThrow();
    expect(() => CaptureEnvelopeSchema.parse({ ...envelope, captureVersion: 2 })).toThrow();
  });
});

describe("HealthResponseSchema", () => {
  test("requires status to be exactly \"ok\"", () => {
    expect(() => HealthResponseSchema.parse({ status: "ok", experimentCount: 0 })).not.toThrow();
    expect(() => HealthResponseSchema.parse({ status: "degraded", experimentCount: 0 })).toThrow();
  });
});

describe("QueueListResponseSchema", () => {
  test("round-trips a joined queue entry plus an error entry in the same response", () => {
    const response = {
      generatedAt: "2026-08-23T00:00:00Z",
      queues: [
        {
          name: "wp5-validation",
          queueState: "completed",
          deadline: "13:00",
          vramProfile: "desktop-shared",
          cellCounts: { pending: 0, running: 0, completed: 2, failed: 3, "skipped-propagated": 0, "aborted-hang": 0 },
          cells: [
            {
              alias: "gemma-qat-c32768-f16-prod",
              model: "gemma-4-12b-qat",
              c: 32768,
              ctk: "f16",
              ctv: "f16",
              fa: "on",
              samplingArm: "production",
              trials: 3,
              state: "completed",
              reason: null,
            },
          ],
        },
        { name: "malformed-truncated", error: "Unexpected end of JSON input" },
      ],
    };
    expect(() => QueueListResponseSchema.parse(response)).not.toThrow();
  });

  test("accepts an all-null cellCounts (no state file) and a null queueState", () => {
    const entry = {
      name: "no-state-queue",
      queueState: null,
      deadline: "02:00",
      vramProfile: "headless",
      cellCounts: { pending: null, running: null, completed: null, failed: null, "skipped-propagated": null, "aborted-hang": null },
      cells: [],
    };
    expect(() => QueueListResponseSchema.parse({ generatedAt: "2026-08-23T00:00:00Z", queues: [entry] })).not.toThrow();
  });

  test("rejects an unknown cell state", () => {
    const badResponse = {
      generatedAt: "2026-08-23T00:00:00Z",
      queues: [
        {
          name: "bad",
          queueState: null,
          deadline: null,
          vramProfile: null,
          cellCounts: { pending: 0, running: 0, completed: 0, failed: 0, "skipped-propagated": 0, "aborted-hang": 0 },
          cells: [
            {
              alias: "a",
              model: null,
              c: null,
              ctk: null,
              ctv: null,
              fa: null,
              samplingArm: null,
              trials: null,
              state: "not-a-real-state",
              reason: null,
            },
          ],
        },
      ],
    };
    expect(() => QueueListResponseSchema.parse(badResponse)).toThrow();
  });

  test("empty queues array (missing/empty QUEUES_DIR) round-trips", () => {
    expect(() => QueueListResponseSchema.parse({ generatedAt: "2026-08-23T00:00:00Z", queues: [] })).not.toThrow();
  });

  // cycle-1 M4 regression test: `control` MUST be declared on `QueueListResponseSchema`
  // itself (a closed zod object) or it is silently stripped by every parse, including on
  // the web client — this is exactly the failure mode the finding named.
  test("keeps a populated `control` section through a round-trip (cycle-1 M4)", () => {
    const response = {
      generatedAt: "2026-08-23T00:00:00Z",
      queues: [],
      control: {
        statusGeneratedAt: "2026-08-23T00:00:00Z",
        stale: false,
        armed: true,
        live: { requestId: "req-20260823T000000Z-0123456789abcdef", queueStem: "ladder-v1", phase: "running", startedAt: "2026-08-23T00:00:00Z" },
        lastRun: {
          requestId: "req-20260822T230000Z-fedcba9876543210",
          endedAt: "2026-08-22T23:59:00Z",
          runExit: 0,
          verifyExit: 0,
          disposition: "clean" as const,
        },
        anomalies: ["late-launch-after-terminal"],
        requests: [
          {
            requestId: "req-20260823T000000Z-0123456789abcdef",
            action: "start" as const,
            state: "running" as const,
            reason: null,
            requestedAt: "2026-08-23T00:00:00Z",
          },
        ],
      },
    };
    const parsed = QueueListResponseSchema.parse(response);
    expect(parsed.control).not.toBeNull();
    expect(parsed.control).toEqual(response.control);
  });

  test("accepts a null `control` (control plane unset/unreachable)", () => {
    expect(() =>
      QueueListResponseSchema.parse({ generatedAt: "2026-08-23T00:00:00Z", queues: [], control: null }),
    ).not.toThrow();
  });

  test("still round-trips with `control` entirely absent (pre-WP-4 responses stay parseable)", () => {
    const parsed = QueueListResponseSchema.parse({ generatedAt: "2026-08-23T00:00:00Z", queues: [] });
    expect(parsed.control).toBeUndefined();
  });

  test("rejects an unknown request state inside `control.requests`", () => {
    const badResponse = {
      generatedAt: "2026-08-23T00:00:00Z",
      queues: [],
      control: {
        statusGeneratedAt: "2026-08-23T00:00:00Z",
        stale: false,
        armed: false,
        live: null,
        lastRun: null,
        anomalies: [],
        requests: [
          { requestId: "req-20260823T000000Z-0123456789abcdef", action: "start", state: "not-a-real-state", reason: null, requestedAt: "2026-08-23T00:00:00Z" },
        ],
      },
    };
    expect(() => QueueListResponseSchema.parse(badResponse)).toThrow();
  });
});
