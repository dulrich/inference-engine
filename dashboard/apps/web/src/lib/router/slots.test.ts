import { describe, expect, test } from "bun:test";
import type { RouterSlot, RouterSlotRecipe } from "@bench-dashboard/shared";
import { cacheRatioLabel, modelStatePill, processingPill, recipeFlags, sampleAgeLabel, unavailableMessage } from "./slots.ts";

const EMPTY_RECIPE: RouterSlotRecipe = { ctk: null, ctv: null, fa: null, reasoningBudget: null, mtp: null, moeCpuLayers: null };

function slot(overrides: Partial<RouterSlot> = {}): RouterSlot {
  return { id: 0, nCtx: 131072, isProcessing: false, promptTokens: null, promptTokensCache: null, cacheHitRatio: null, ...overrides };
}

describe("recipeFlags", () => {
  test("renders only the non-null flags, in recipe order, with mtp as on/off", () => {
    expect(
      recipeFlags({ ctk: "q8_0", ctv: "q8_0", fa: "on", reasoningBudget: 2048, mtp: true, moeCpuLayers: 12 }),
    ).toEqual([
      { label: "ctk", value: "q8_0" },
      { label: "ctv", value: "q8_0" },
      { label: "fa", value: "on" },
      { label: "reasoning budget", value: "2048" },
      { label: "mtp", value: "on" },
      { label: "moe cpu layers", value: "12" },
    ]);
    expect(recipeFlags({ ...EMPTY_RECIPE, mtp: false })).toEqual([{ label: "mtp", value: "off" }]);
  });

  test("an all-null recipe contributes no flags rather than guessed defaults", () => {
    expect(recipeFlags(EMPTY_RECIPE)).toEqual([]);
  });
});

describe("cacheRatioLabel", () => {
  test("a measured ratio is a percent; an idle slot's null stays the em-dash", () => {
    expect(cacheRatioLabel(slot({ cacheHitRatio: 0.62 }))).toBe("62 %");
    expect(cacheRatioLabel(slot())).toBe("—");
  });
});

describe("modelStatePill / processingPill", () => {
  test("known llama-swap states get their hue, an unknown state stays informational", () => {
    expect(modelStatePill("ready")).toEqual({ cls: "pill-ok", label: "ready" });
    expect(modelStatePill("starting")).toEqual({ cls: "pill-warn", label: "starting" });
    expect(modelStatePill("stopping")).toEqual({ cls: "pill-warn", label: "stopping" });
    expect(modelStatePill("wedged")).toEqual({ cls: "pill-info", label: "wedged" });
  });

  test("every pill carries a text label, including the null cases (CC-04 redundancy)", () => {
    expect(modelStatePill(null)).toEqual({ cls: "pill-muted", label: "unknown" });
    expect(processingPill(true)).toEqual({ cls: "pill-info", label: "processing" });
    expect(processingPill(false)).toEqual({ cls: "pill-muted", label: "idle" });
    expect(processingPill(null)).toEqual({ cls: "pill-muted", label: "unknown" });
  });
});

describe("unavailableMessage / sampleAgeLabel", () => {
  test("each unavailable reason renders in words", () => {
    expect(unavailableMessage("disabled")).toBe("not configured");
    expect(unavailableMessage("unreadable")).toBe("log directory unreadable");
    expect(unavailableMessage("empty")).toBe("no slots samples yet — enable the host poller unit");
  });

  test("the header age comes from the API-computed ageMs, not the browser clock", () => {
    expect(sampleAgeLabel(12_000)).toBe("sampled 12 s ago");
  });
});
