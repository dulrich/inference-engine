import { describe, expect, test } from "bun:test";
import {
  bytesToGiB,
  formatCount,
  formatGiB,
  formatMs,
  formatPct,
  formatShortHash,
  formatSignedGiB,
  formatTimestamp,
  formatTokS,
} from "./format.ts";

describe("bytesToGiB / formatGiB", () => {
  test("converts bytes to GiB", () => {
    expect(bytesToGiB(1024 ** 3)).toBe(1);
    expect(bytesToGiB(500_000_000)).toBeCloseTo(0.4657, 3);
  });

  test("null passes through as null / em-dash", () => {
    expect(bytesToGiB(null)).toBeNull();
    expect(formatGiB(null)).toBe("—");
  });

  test("formats to two decimal places with a unit suffix", () => {
    expect(formatGiB(1024 ** 3)).toBe("1.00 GiB");
  });
});

describe("formatSignedGiB", () => {
  test("prefixes a positive delta with +", () => {
    expect(formatSignedGiB(1024 ** 3)).toBe("+1.00 GiB");
  });

  test("keeps the sign on a negative delta", () => {
    expect(formatSignedGiB(-(1024 ** 3))).toBe("-1.00 GiB");
  });

  test("null is an em-dash", () => {
    expect(formatSignedGiB(null)).toBe("—");
  });
});

describe("formatMs", () => {
  test("sub-second stays milliseconds", () => {
    expect(formatMs(900.2)).toBe("900 ms");
  });

  test("one second and over switches to seconds", () => {
    expect(formatMs(6531.7)).toBe("6.53 s");
  });

  test("null is an em-dash", () => {
    expect(formatMs(null)).toBe("—");
  });
});

describe("formatTokS / formatPct / formatCount", () => {
  test("formatTokS", () => {
    expect(formatTokS(62.15)).toBe("62.1 tok/s");
    expect(formatTokS(null)).toBe("—");
  });

  test("formatPct", () => {
    expect(formatPct(0.25)).toBe("25.0%");
    expect(formatPct(0)).toBe("0.0%");
    expect(formatPct(null)).toBe("—");
  });

  test("formatCount", () => {
    expect(formatCount(3)).toBe("3");
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe("—");
  });
});

describe("formatTimestamp", () => {
  test("null is an em-dash", () => {
    expect(formatTimestamp(null)).toBe("—");
  });

  test("an unparsable string passes through unchanged", () => {
    expect(formatTimestamp("not-a-date")).toBe("not-a-date");
  });

  test("a valid ISO timestamp formats via toLocaleString (non-empty, no throw)", () => {
    const formatted = formatTimestamp("2026-08-23T18:56:20.520839+00:00");
    expect(formatted.length).toBeGreaterThan(0);
    expect(formatted).not.toBe("2026-08-23T18:56:20.520839+00:00");
  });
});

describe("formatShortHash", () => {
  test("null is an em-dash", () => {
    expect(formatShortHash(null)).toBe("—");
  });

  test("truncates a long hash to 10 chars plus ellipsis", () => {
    const hash = "abc123abc123abc123abc123abc123abc123abc123abc123abc123abc123ab";
    expect(formatShortHash(hash)).toBe("abc123abc1…");
  });

  test("a short string passes through unchanged", () => {
    expect(formatShortHash("b10534")).toBe("b10534");
  });
});
