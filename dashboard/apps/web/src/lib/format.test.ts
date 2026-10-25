import { describe, expect, test } from "bun:test";
import { formatCount } from "./format.ts";

describe("formatCount", () => {
  test("formats a count, null as an em-dash", () => {
    expect(formatCount(3)).toBe("3");
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe("—");
  });
});
