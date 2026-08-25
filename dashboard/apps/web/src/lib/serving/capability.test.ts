import { describe, expect, test } from "bun:test";
import type { ServingCapability } from "@bench-dashboard/shared";
import {
  capabilityReasoningLabel,
  capabilityReasoningPillClass,
  capabilityToolsLabel,
  capabilityToolsPillClass,
  formatCapabilityServingSummary,
} from "./capability.ts";

function capability(overrides: Partial<ServingCapability> = {}): ServingCapability {
  return {
    contextLength: 131072,
    inputModalities: ["text"],
    outputModalities: ["text"],
    toolCalling: true,
    reasoning: "request-controllable",
    serving: {
      quant: "UD-Q4_K_XL",
      kvCacheType: "f16",
      flashAttention: false,
      mtp: false,
      moeCpuOffloadLayers: null,
      ubatch: null,
    },
    ...overrides,
  };
}

describe("capabilityToolsLabel / capabilityToolsPillClass", () => {
  test("true reads \"tools\", pill-ok", () => {
    expect(capabilityToolsLabel(true)).toBe("tools");
    expect(capabilityToolsPillClass(true)).toBe("pill-ok");
  });

  test("false reads \"no tools\", pill-muted", () => {
    expect(capabilityToolsLabel(false)).toBe("no tools");
    expect(capabilityToolsPillClass(false)).toBe("pill-muted");
  });

  test("null (unresolvable) renders the em-dash, pill-muted -- never mistaken for false", () => {
    expect(capabilityToolsLabel(null)).toBe("—");
    expect(capabilityToolsPillClass(null)).toBe("pill-muted");
  });
});

describe("capabilityReasoningLabel / capabilityReasoningPillClass", () => {
  test("each known level gets its own pill class, label is the raw value", () => {
    expect(capabilityReasoningLabel("none")).toBe("none");
    expect(capabilityReasoningPillClass("none")).toBe("pill-muted");
    expect(capabilityReasoningPillClass("request-controllable")).toBe("pill-info");
    expect(capabilityReasoningPillClass("always-on")).toBe("pill-warn");
  });

  test("an unrecognized-but-valid future level is shown, not hidden -- falls back to pill-muted", () => {
    expect(capabilityReasoningLabel("future-level")).toBe("future-level");
    expect(capabilityReasoningPillClass("future-level")).toBe("pill-muted");
  });

  test("null renders the em-dash, pill-muted", () => {
    expect(capabilityReasoningLabel(null)).toBe("—");
    expect(capabilityReasoningPillClass(null)).toBe("pill-muted");
  });
});

describe("formatCapabilityServingSummary", () => {
  test("a minimal serving block (no FA/MTP/optional keys) lists only what's true/present", () => {
    expect(formatCapabilityServingSummary(capability())).toBe("131072 ctx · UD-Q4_K_XL · f16 kv");
  });

  test("flash attention and MTP are only listed when true", () => {
    const summary = formatCapabilityServingSummary(
      capability({ serving: { quant: "UD-IQ3_S", kvCacheType: "q4_0", flashAttention: true, mtp: false, moeCpuOffloadLayers: null, ubatch: null } }),
    );
    expect(summary).toBe("131072 ctx · UD-IQ3_S · q4_0 kv · fa");
  });

  test("both optional serving keys append in order when present", () => {
    const summary = formatCapabilityServingSummary(
      capability({
        serving: { quant: "UD-Q4_K_XL", kvCacheType: "f16", flashAttention: false, mtp: true, moeCpuOffloadLayers: 32, ubatch: 2048 },
      }),
    );
    expect(summary).toBe("131072 ctx · UD-Q4_K_XL · f16 kv · mtp · ncmoe 32 · ub 2048");
  });

  test("an all-null capability renders the em-dash, never an empty string", () => {
    const summary = formatCapabilityServingSummary(
      capability({
        contextLength: null,
        serving: { quant: null, kvCacheType: null, flashAttention: null, mtp: null, moeCpuOffloadLayers: null, ubatch: null },
      }),
    );
    expect(summary).toBe("—");
  });
});
