import { describe, expect, test } from "bun:test";
import { wilsonInterval } from "./wilson.ts";

describe("wilsonInterval", () => {
  test("0/30 one-sided 95% upper matches the README's own cited example (8.27%)", () => {
    const interval = wilsonInterval(0, 30, "one", 0.95);
    expect(interval.sided).toBe("one");
    expect(interval.lower).toBeNull();
    expect(interval.upper).toBeCloseTo(0.0827, 3);
  });

  test("two-sided returns both bounds, straddling the point estimate", () => {
    const interval = wilsonInterval(2, 4, "two", 0.95);
    expect(interval.lower).not.toBeNull();
    expect(interval.lower as number).toBeLessThan(0.5);
    expect(interval.upper).toBeGreaterThan(0.5);
  });

  test("throws for an untabulated confidence rather than guessing a z-value", () => {
    expect(() => wilsonInterval(1, 10, "two", 0.9)).toThrow();
  });

  test("throws for n <= 0", () => {
    expect(() => wilsonInterval(0, 0, "two")).toThrow();
  });
});
