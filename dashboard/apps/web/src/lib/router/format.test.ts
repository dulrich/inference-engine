import { describe, expect, test } from "bun:test";
import {
  formatAge,
  formatAgeMs,
  formatDurationMs,
  formatRatioPct,
  formatTokPerSec,
  sessionLabel,
  truncateId,
  truncateLine,
} from "./format.ts";

describe("formatAgeMs / formatAge", () => {
  test("picks the coarse unit by magnitude and clamps a negative (clock-skew) age", () => {
    expect(formatAgeMs(12_000)).toBe("12 s ago");
    expect(formatAgeMs(3 * 60_000 + 4_000)).toBe("3 min ago");
    expect(formatAgeMs(2 * 3_600_000)).toBe("2 h ago");
    expect(formatAgeMs(50 * 3_600_000)).toBe("2 d ago");
    expect(formatAgeMs(-5_000)).toBe("0 s ago");
    expect(formatAgeMs(null)).toBe("—");
  });

  test("formatAge measures an ISO stamp against the injected clock", () => {
    const now = Date.parse("2026-09-13T12:00:30.000Z");
    expect(formatAge("2026-09-13T12:00:18.000Z", now)).toBe("12 s ago");
  });

  test("a null or unparseable stamp is the em-dash, never NaN", () => {
    expect(formatAge(null)).toBe("—");
    expect(formatAge("not-a-date", Date.parse("2026-09-13T12:00:00.000Z"))).toBe("—");
  });
});

describe("formatDurationMs / formatRatioPct / formatTokPerSec", () => {
  test("durations switch from whole ms to one-decimal seconds at 1000 ms", () => {
    expect(formatDurationMs(840)).toBe("840 ms");
    expect(formatDurationMs(1_420)).toBe("1.4 s");
    expect(formatDurationMs(null)).toBe("—");
  });

  test("a ratio renders as a whole percent; null stays the em-dash (idle slot, not 0 %)", () => {
    expect(formatRatioPct(0.6234)).toBe("62 %");
    expect(formatRatioPct(0)).toBe("0 %");
    expect(formatRatioPct(null)).toBe("—");
  });

  test("tok/s is one decimal, null is the em-dash (fewer than two progress samples)", () => {
    expect(formatTokPerSec(23.46)).toBe("23.5 tok/s");
    expect(formatTokPerSec(null)).toBe("—");
  });
});

describe("truncateId / sessionLabel / truncateLine", () => {
  test("ids truncate past the head length and pass through when already short", () => {
    expect(truncateId("0199aa11-bbbb-cccc")).toBe("0199aa11…");
    expect(truncateId("short")).toBe("short");
    expect(truncateId(null)).toBe("—");
  });

  test("a null sessionId is the word 'unattributed', a real one truncates", () => {
    expect(sessionLabel(null)).toBe("unattributed");
    expect(sessionLabel("abcdefghijkl")).toBe("abcdefgh…");
  });

  test("a multi-line sidecar message collapses to one truncated line", () => {
    expect(truncateLine("line one\n  line two")).toBe("line one line two");
    expect(truncateLine("x".repeat(200))).toBe(`${"x".repeat(120)}…`);
    expect(truncateLine("   ")).toBe("—");
    expect(truncateLine(null)).toBe("—");
  });
});
