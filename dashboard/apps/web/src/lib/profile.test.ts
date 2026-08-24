import { describe, expect, test } from "bun:test";
import { vramProfileLabel, vramProfilePillClass } from "./profile.ts";

describe("vramProfileLabel", () => {
  test("passes a known profile through verbatim", () => {
    expect(vramProfileLabel("desktop-shared")).toBe("desktop-shared");
    expect(vramProfileLabel("headless")).toBe("headless");
  });

  test("null is unknown", () => {
    expect(vramProfileLabel(null)).toBe("unknown");
  });
});

describe("vramProfilePillClass", () => {
  test("known profiles get their own pill class", () => {
    expect(vramProfilePillClass("desktop-shared")).toBe("pill-warn");
    expect(vramProfilePillClass("headless")).toBe("pill-ok");
  });

  test("null is muted", () => {
    expect(vramProfilePillClass(null)).toBe("pill-muted");
  });

  test("an unrecognized non-null profile is shown, not hidden -- falls back to pill-info", () => {
    expect(vramProfilePillClass("future-profile")).toBe("pill-info");
  });
});
