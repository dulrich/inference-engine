import { describe, expect, test } from "bun:test";
import type { ServingRequest, ServingStatusFile, ServingVisibilityRequest } from "./index.ts";
import {
  CaptureEnvelopeSchema,
  CaptureRecordSchema,
  CurveSeriesSchema,
  ExperimentDetailSchema,
  ExperimentSummarySchema,
  HealthResponseSchema,
  LadderRowSchema,
  QueueListResponseSchema,
  RouterSidecarResponseSchema,
  RouterSlotsRecordSchema,
  RouterSlotsResponseSchema,
  RouterTelemetryResponseSchema,
  SERVING_STATUS_STALE_MS,
  ServingRequestSchema,
  ServingResponseSchema,
  ServingStatusResponseSchema,
  ServingTimingRowSchema,
  ServingVisibilityRequestSchema,
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
    // A pre-WP-1 capability payload -- no `sampling`, no `maxOutput`, no `sources`, and no
    // `serving.reasoningMode`/`reasoningBudget`. It still round-trips (that is the additive
    // wire invariant), with every WP-1 field widened in as `null` rather than rejected.
    const parsed = ServingResponseSchema.parse(response);
    expect(parsed.models[0]?.capability).toMatchObject(response.models[0]?.capability ?? {});
    expect(parsed.models[0]?.capability?.sampling).toBeNull();
    expect(parsed.models[0]?.capability?.maxOutput).toBeNull();
    expect(parsed.models[0]?.capability?.sources).toBeNull();
    expect(parsed.models[0]?.capability?.serving.reasoningMode).toBeNull();
  });

  // WP-1 (`plans/bench-dashboard-serving-control.md`): the advisory block and its provenance.
  // `sources` is a SIBLING of `sampling`, keyed by dotted field path -- never nested inside
  // `sampling`, whose six leaves stay plain scalars for the Open WebUI sync and obsidian-crucible.
  test("round-trips the WP-1 advisory block: sampling scalars, maxOutput, and the sources map", () => {
    const capability = {
      contextLength: 131072,
      inputModalities: ["text"],
      outputModalities: ["text"],
      toolCalling: true,
      reasoning: "budgeted",
      serving: {
        quant: "UD-IQ3_S",
        kvCacheType: "q4_0",
        flashAttention: true,
        mtp: false,
        moeCpuOffloadLayers: null,
        ubatch: null,
        reasoningMode: null,
        reasoningBudget: 8192,
      },
      sampling: {
        temperature: 0.6,
        topP: 0.95,
        topK: 20,
        minP: 0,
        presencePenalty: null,
        repetitionPenalty: null,
      },
      maxOutput: { reasoningBudget: 8192, answerTokens: 32768 },
      sources: {
        "sampling.temperature": { url: "https://huggingface.co/Qwen/Qwen3.8-27B", verified: "2026-09-13", reason: null },
        "sampling.presence_penalty": { url: "none" as const, reason: "the card is silent on this value", verified: null },
      },
    };
    const response = {
      configPath: "/fixtures/llama-config.yaml",
      models: [
        { modelKey: "Qwen3.8-27B-UD-IQ3_S", name: null, aliases: ["qwen38-27b-iq3s"], ttl: 1800, checkEndpoint: "/health",
          flags: { contextLength: 131072, kvCacheTypeK: "q4_0", kvCacheTypeV: "q4_0", flashAttention: "on", gpuLayers: 99 },
          capability },
      ],
    };
    expect(ServingResponseSchema.parse(response).models[0]?.capability).toEqual(capability);
  });

  // Cycle-2 E3: `verified` is a bare `YYYY-MM-DD` date in exactly one spelling -- neither a
  // boolean nor an ISO timestamp is a date this schema accepts.
  test("rejects a `verified` that is not a YYYY-MM-DD string", () => {
    const withVerified = (verified: unknown) => ({
      configPath: "/fixtures/llama-config.yaml",
      models: [
        { modelKey: "m", name: null, aliases: ["a"], ttl: null, checkEndpoint: null,
          flags: { contextLength: null, kvCacheTypeK: null, kvCacheTypeV: null, flashAttention: null, gpuLayers: null },
          capability: {
            contextLength: null, inputModalities: [], outputModalities: [], toolCalling: null, reasoning: null,
            serving: { quant: null, kvCacheType: null, flashAttention: null, mtp: null },
            sources: { "sampling.temperature": { url: "https://huggingface.co/Qwen/Qwen3.8-27B", verified } },
          } },
      ],
    });
    expect(() => ServingResponseSchema.parse(withVerified("2026-09-13"))).not.toThrow();
    expect(() => ServingResponseSchema.parse(withVerified(true))).toThrow();
    expect(() => ServingResponseSchema.parse(withVerified("2026-09-13T00:00:00Z"))).toThrow();
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

describe("CaptureRecordSchema", () => {
  // WP-3 (`plans/served-recipe-p50-prefill-and-decode.md`): `serving` is additive and
  // `.nullable().default(null)`-widened, the same posture `ServingAliasSchema.capability`/
  // `.recipe` use -- a legacy record predating this field (no `serving` key at all, exactly
  // what every pre-WP-3 export payload on disk/in a cache looks like) still parses, and
  // reads back `serving: null`, never a thrown error.
  test("a legacy record without `serving` parses to `serving: null`", () => {
    const legacy = {
      experimentId: "exp-1",
      config: { schema_version: 2 },
      prompts: [],
      completions: [],
      samples: [],
    };
    const parsed = CaptureRecordSchema.parse(legacy);
    expect(parsed.serving).toBeNull();
  });

  test("round-trips a resolved `serving` object verbatim", () => {
    const record = {
      experimentId: "exp-2",
      config: {},
      prompts: [],
      completions: [],
      samples: [],
      serving: { modelKey: "fixture-chat-q4", aliases: ["fixture-chat"], recipeMatch: true, mtp: false, moeCpuLayers: null, specType: null },
    };
    const parsed = CaptureRecordSchema.parse(record);
    expect(parsed.serving).toEqual(record.serving);
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
          restartFailed: null,
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

describe("RouterTelemetryResponseSchema", () => {
  const record = {
    kind: "request" as const,
    ts: "2026-09-13T04:00:00.000Z",
    reqId: "0d037a4a-4af9-416e-8916-ca9725618ee7",
    route: "/v1/messages",
    method: "POST",
    leg: "claude-local",
    modelResolved: "qwen36-a3b",
    sessionId: "1f0e9b2c-4a7d-4f31-9c88-abcdef012345",
    isSubagent: true,
    stream: true,
    status: 200,
    responseTokens: 0,
    reasoningTokens: 36,
    ttftMs: 430,
    wallMs: 1533,
    error: null,
    modelRequested: "claude-local/qwen36-a3b",
    providerType: "openai",
    sourceAdapterKey: "anthropic_messages",
    upstreamUrl: "http://inference-engine-llama:4806/v1/chat/completions",
    upstreamFirstByteMs: 430,
    attemptSequence: 1,
    stopReason: "length",
    upstreamTimings: { prompt_n: 38, predicted_n: 48, draft_n_accepted: 34 },
    usage: { input: 38, output: 48, cacheRead: 0, cacheWrite: 0 },
  };
  const valid = {
    available: true as const,
    generatedAt: "2026-09-13T04:00:10.000Z",
    windowBytes: 1048576,
    file: "router-2026-09-13.0.jsonl",
    recent: [record],
    inFlight: [
      {
        reqId: "374ccb44-2624-4b05-8df5-5e26db94101a",
        leg: "claude-local",
        modelResolved: "qwen36-a3b",
        sessionId: null,
        phase: "decode" as const,
        responseTokens: 60,
        reasoningTokens: 90,
        elapsedMs: 4000,
        lastSeenTs: "2026-09-13T04:00:09.000Z",
        tokPerSec: 50,
      },
    ],
    legs: [{ leg: "claude-local", requests: 1, errors: 0, p50TtftMs: 430 }],
  };

  test("round-trips a telemetry response with a request record, an in-flight stream and a leg rollup", () => {
    expect(RouterTelemetryResponseSchema.parse(valid)).toEqual(valid);
  });

  test("rejects a `recent` record missing reqId, and keeps unknown sink keys via passthrough", () => {
    const withoutReqId: Record<string, unknown> = { ...record };
    delete withoutReqId.reqId;
    expect(() => RouterTelemetryResponseSchema.parse({ ...valid, recent: [withoutReqId] })).toThrow();
    // The sink evolves under `router/`: a key this schema has never heard of survives.
    const parsed = RouterTelemetryResponseSchema.parse({
      ...valid,
      recent: [{ ...record, someFutureSinkField: 7 }],
    });
    if (!parsed.available) throw new Error("expected available");
    expect((parsed.recent[0] as Record<string, unknown>).someFutureSinkField).toBe(7);
  });
});

describe("RouterSlotsResponseSchema / RouterSlotsRecordSchema", () => {
  test("round-trips the response AND the on-disk poller record they share", () => {
    const response = {
      available: true as const,
      sampledAt: "2026-09-13T04:00:05.000Z",
      ageMs: 5000,
      stale: false,
      models: [
        {
          model: "Qwen3.8-27B-UD-IQ3_S",
          state: "ready",
          ttl: 1800,
          nCtx: 131072,
          recipe: { ctk: "q8_0", ctv: "q8_0", fa: "on", reasoningBudget: 8192, mtp: true, moeCpuLayers: 12 },
          slots: [
            { id: 0, nCtx: 131072, isProcessing: true, promptTokens: 2000, promptTokensCache: 1500, cacheHitRatio: 0.75 },
          ],
          error: null,
        },
      ],
    };
    expect(RouterSlotsResponseSchema.parse(response)).toEqual(response);

    const onDisk = {
      kind: "slots" as const,
      ts: "2026-09-13T04:00:05.000Z",
      sampledAt: "2026-09-13T04:00:05.000Z",
      error: null,
      models: [
        { model: "nemotron-4b", state: "starting", ttl: 1800, cmd: null, slots: null, error: null },
        { model: "bge-m3-f16", state: "ready", ttl: 1800, cmd: "/app/llama-server -c 8192", slots: [{ id: 0, n_ctx: 8192 }], error: null },
      ],
    };
    expect(RouterSlotsRecordSchema.parse(onDisk)).toEqual(onDisk);

    // …and the `available: false` arm of the same discriminated union: `reason` is
    // required and closed to the three documented values.
    expect(() => RouterSlotsResponseSchema.parse({ available: false })).toThrow();
    expect(() => RouterSlotsResponseSchema.parse({ available: false, reason: "who-knows" })).toThrow();
    expect(RouterSlotsResponseSchema.parse({ available: false, reason: "unreadable" })).toEqual({
      available: false,
      reason: "unreadable",
    });
  });
});

describe("RouterSidecarResponseSchema", () => {
  test("round-trips an open kind map, and rejects a persona missing lastSeenTs", () => {
    const valid = {
      available: true as const,
      generatedAt: "2026-09-13T04:00:10.000Z",
      file: "codex-sidecar-2026-09-13.jsonl",
      byKind: { http: 179, "http-error": 89, "a-kind-invented-tomorrow": 1 },
      personas: [
        {
          persona: "codex-b",
          threadId: "01a098ec-6ba9-7e41-87f6-c0e802f57acd",
          model: "gpt-5.6-sol",
          lastSeenTs: "2026-09-13T04:00:00.000Z",
          lastKind: "http",
        },
      ],
      recentErrors: [
        { ts: "2026-09-13T04:00:55.813Z", persona: "codex-b", kind: "http-error", status: 409, message: "superseded" },
      ],
    };
    expect(RouterSidecarResponseSchema.parse(valid)).toEqual(valid);
    const brokenPersona: Record<string, unknown> = { ...(valid.personas[0] as (typeof valid.personas)[number]) };
    delete brokenPersona.lastSeenTs;
    expect(() => RouterSidecarResponseSchema.parse({ ...valid, personas: [brokenPersona] })).toThrow();
  });
});

describe("ServingVisibilityRequestSchema / ServingRequestSchema (WP-4, serving control)", () => {
  test("the POST body is camelCase and closed to the two-consumer vocabulary", () => {
    const valid: ServingVisibilityRequest = { modelKey: "Qwen3.8-27B-UD-IQ3_S", consumers: ["cc", "openwebui"] };
    expect(ServingVisibilityRequestSchema.parse(valid)).toEqual(valid);
    // An empty array is the unpublish request, not an invalid one.
    expect(ServingVisibilityRequestSchema.parse({ modelKey: "k", consumers: [] }).consumers).toEqual([]);
    expect(() => ServingVisibilityRequestSchema.parse({ modelKey: "", consumers: [] })).toThrow();
    expect(() => ServingVisibilityRequestSchema.parse({ modelKey: "k", consumers: ["ollama"] })).toThrow();
    expect(() => ServingVisibilityRequestSchema.parse({ consumers: ["cc"] })).toThrow();
  });

  test("the on-disk file is snake_case, pins the schema literal and the srv- id grammar", () => {
    const valid: ServingRequest = {
      schema: "serving-request-v1" as const,
      request_id: "srv-20260913T115900Z-00000000000000a1",
      action: "set-visibility" as const,
      model_key: "Qwen3.8-27B-UD-IQ3_S",
      consumers: ["cc", "openwebui"],
      requested_at: "2026-09-13T11:59:00.480Z",
    };
    expect(ServingRequestSchema.parse(valid)).toEqual(valid);
    expect(() => ServingRequestSchema.parse({ ...valid, schema: "serving-request-v2" })).toThrow();
    // The bake-off plane's own `req-` id is NOT a serving request id: the two planes share
    // one drop-box directory and each ignores the other's basenames.
    expect(() => ServingRequestSchema.parse({ ...valid, request_id: "req-20260913T115900Z-00000000000000a1" })).toThrow();
    expect(() => ServingRequestSchema.parse({ ...valid, model_key: "" })).toThrow();
    expect(() => ServingRequestSchema.parse({ ...valid, consumers: ["cc", "ollama"] })).toThrow();
  });
});

describe("ServingStatusResponseSchema (WP-4, serving control)", () => {
  const statusFile: ServingStatusFile = {
    schema: "serving-status-v1" as const,
    generated_at: "2026-09-13T12:00:00.000Z",
    visibility: {
      "Qwen3.8-27B-UD-IQ3_S": { gateway_id: "qwen38-27b-iq3s", consumers: ["cc", "openwebui"] },
      "bge-m3-f16": { gateway_id: null, consumers: [] },
    },
    last_request: {
      request_id: "srv-20260913T115930Z-00000000000000a2",
      state: "applied" as const,
      reason: null,
      applied_at: "2026-09-13T11:59:31.004Z",
    },
    recent: [
      {
        request_id: "srv-20260913T115930Z-00000000000000a2",
        state: "applied" as const,
        reason: null,
        applied_at: "2026-09-13T11:59:31.004Z",
      },
      {
        request_id: "srv-20260913T115800Z-00000000000000b1",
        state: "rejected" as const,
        reason: "cc requires a gateway id",
        applied_at: "2026-09-13T11:58:01.508Z",
      },
    ],
  };

  test("round-trips the available arm, carrying the whole declared visibility map", () => {
    const valid = { available: true as const, ...statusFile };
    expect(ServingStatusResponseSchema.parse(valid)).toEqual(valid);
  });

  test("`reason` defaults to null so a writer that omits it on the happy path still parses", () => {
    const thin = {
      available: true as const,
      ...statusFile,
      recent: [{ request_id: "srv-20260913T115930Z-00000000000000a2", state: "applied" as const, applied_at: "x" }],
    };
    const parsed = ServingStatusResponseSchema.parse(thin);
    if (!parsed.available) throw new Error("expected available");
    expect(parsed.recent[0]?.reason).toBeNull();
  });

  test("the unavailable arm is closed to the four reasons, staleness pinned at 180 s", () => {
    expect(SERVING_STATUS_STALE_MS).toBe(180_000);
    for (const reason of ["disabled", "unreadable", "empty", "stale"] as const) {
      expect(ServingStatusResponseSchema.parse({ available: false, reason })).toEqual({ available: false, reason });
    }
    expect(() => ServingStatusResponseSchema.parse({ available: false })).toThrow();
    expect(() => ServingStatusResponseSchema.parse({ available: false, reason: "who-knows" })).toThrow();
  });

  test("rejects an available payload missing the visibility map or carrying a bad state word", () => {
    const withoutVisibility: Record<string, unknown> = { available: true, ...statusFile };
    delete withoutVisibility.visibility;
    expect(() => ServingStatusResponseSchema.parse(withoutVisibility)).toThrow();
    expect(() =>
      ServingStatusResponseSchema.parse({
        available: true,
        ...statusFile,
        recent: [{ request_id: "srv-1", state: "in-flight", reason: null, applied_at: "x" }],
      }),
    ).toThrow();
  });
});
