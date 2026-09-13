import { describe, expect, test } from "bun:test";
import { elapsedLabel, phasePill, splitLabel, tokenSplit, tokPerSecLabel } from "./inflight.ts";

describe("tokenSplit", () => {
  test("the two segments sum to 100 % and round without drift", () => {
    expect(tokenSplit(30, 70)).toEqual({ total: 100, reasoningPct: 30, responsePct: 70 });
    const split = tokenSplit(1, 2);
    expect(split.reasoningPct + split.responsePct).toBe(100);
    expect(split.reasoningPct).toBe(33);
  });

  test("before the first token the bar is empty, not half-filled", () => {
    expect(tokenSplit(0, 0)).toEqual({ total: 0, reasoningPct: 0, responsePct: 0 });
  });

  test("a response-only generation is all --viz-2 width", () => {
    expect(tokenSplit(0, 40)).toEqual({ total: 40, reasoningPct: 0, responsePct: 100 });
  });
});

describe("phasePill / elapsedLabel / tokPerSecLabel", () => {
  test("both phases carry their own label; neither uses a verdict hue", () => {
    expect(phasePill("decode")).toEqual({ cls: "pill-info", label: "decode" });
    expect(phasePill("prefill")).toEqual({ cls: "pill-muted", label: "prefill" });
  });

  test("elapsed and tok/s reuse the shared coarse formatters", () => {
    expect(elapsedLabel(4_300)).toBe("4.3 s");
    expect(tokPerSecLabel(18.44)).toBe("18.4 tok/s");
    expect(tokPerSecLabel(null)).toBe("—");
  });
});

describe("splitLabel", () => {
  test("the bar always has a text readout beside it", () => {
    expect(splitLabel(tokenSplit(12, 40), 12, 40)).toBe("12 reasoning / 40 response");
    expect(splitLabel(tokenSplit(0, 0), 0, 0)).toBe("no tokens yet");
  });
});
