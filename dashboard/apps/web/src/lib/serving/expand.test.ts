import { describe, expect, test } from "bun:test";
import { expandToggleLabel, flagValueClass, formatFlagValue, toggleExpandedRow } from "./expand.ts";

describe("toggleExpandedRow", () => {
  test("adds a key that isn't in the set", () => {
    const next = toggleExpandedRow(new Set(), "model-a");
    expect(next.has("model-a")).toBe(true);
  });

  test("removes a key that's already in the set", () => {
    const next = toggleExpandedRow(new Set(["model-a"]), "model-a");
    expect(next.has("model-a")).toBe(false);
  });

  test("leaves other keys untouched", () => {
    const next = toggleExpandedRow(new Set(["model-a", "model-b"]), "model-a");
    expect(next.has("model-b")).toBe(true);
    expect(next.has("model-a")).toBe(false);
  });

  test("never mutates the input set", () => {
    const original = new Set(["model-a"]);
    toggleExpandedRow(original, "model-a");
    expect(original.has("model-a")).toBe(true);
  });
});

describe("expandToggleLabel", () => {
  // CC-04: the toggle is always a text state word, never icon-only/color-only -- this pair
  // is the one place both label strings are pinned.
  test("collapsed reads \"show config\"", () => {
    expect(expandToggleLabel(false)).toBe("show config");
  });

  test("expanded reads \"hide config\"", () => {
    expect(expandToggleLabel(true)).toBe("hide config");
  });
});

describe("formatFlagValue", () => {
  test("a present value renders as-is", () => {
    expect(formatFlagValue("0.8")).toBe("0.8");
  });

  test("a negative-number value renders as-is (never mistaken for missing)", () => {
    expect(formatFlagValue("-1")).toBe("-1");
  });

  test("a boolean-style flag (null value) renders the null-safe em-dash", () => {
    expect(formatFlagValue(null)).toBe("—");
  });
});

describe("flagValueClass", () => {
  test("a present value gets no muted class", () => {
    expect(flagValueClass("q4_0")).toBe("");
  });

  test("a boolean-style flag (null value) gets the existing serving-table-muted class", () => {
    expect(flagValueClass(null)).toBe("serving-table-muted");
  });
});
