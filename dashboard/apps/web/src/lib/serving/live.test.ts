import { describe, expect, test } from "bun:test";
import type { ServingLiveEvent, ServingLiveModel } from "@bench-dashboard/shared";
import { eventPill, liveUnavailableMessage, recentEvents, residencyDetail, residentPill, tierView } from "./live.ts";

function model(overrides: Partial<ServingLiveModel> = {}): ServingLiveModel {
  return { model: "m", state: null, ttl: null, preload: null, idle_unload_seconds: null, ...overrides };
}

describe("liveUnavailableMessage", () => {
  test("every reason renders in words", () => {
    expect(liveUnavailableMessage("disabled")).toBe("not configured (SERVING_FEED_DIR unset)");
    expect(liveUnavailableMessage("unreadable")).toBe("feed directory unreadable");
    expect(liveUnavailableMessage("empty")).toContain("status-collector unit installed");
    expect(liveUnavailableMessage("stale")).toContain("over a minute");
  });
});

describe("residentPill / residencyDetail", () => {
  test("gpu states get hues; an onnx model with no state reads as loaded", () => {
    expect(residentPill(model({ state: "ready" }))).toEqual({ cls: "pill-ok", label: "ready" });
    expect(residentPill(model({ state: "starting" }))).toEqual({ cls: "pill-warn", label: "starting" });
    expect(residentPill(model({ state: "weird" }))).toEqual({ cls: "pill-info", label: "weird" });
    expect(residentPill(model())).toEqual({ cls: "pill-ok", label: "loaded" });
  });

  test("detail names the unload policy per tier", () => {
    expect(residencyDetail(model({ ttl: 1800 }))).toBe("unloads after 1800 s idle (ttl)");
    expect(residencyDetail(model({ ttl: 0 }))).toBe("no ttl");
    expect(residencyDetail(model({ preload: true }))).toBe("preloaded, always resident");
    expect(residencyDetail(model({ preload: false, idle_unload_seconds: 900 }))).toBe("lazy, unloads after 900 s idle");
    expect(residencyDetail(model())).toBe("—");
  });
});

describe("tierView", () => {
  test("available, empty and unavailable tiers", () => {
    const ok = tierView({ available: true, sampled_at: "t", models: [model()] });
    expect(ok).toEqual({ available: true, sampledAt: "t", models: [model()], note: "" });
    expect(tierView({ available: true, sampled_at: "t", models: [] }).note).toBe("nothing resident");
    const off = tierView({ available: false, sampled_at: null, reason: "no snapshot in feed" });
    expect(off).toEqual({ available: false, sampledAt: null, models: [], note: "unavailable — no snapshot in feed" });
  });
});

describe("events", () => {
  const ev = (kind: "load" | "unload", n: number): ServingLiveEvent => ({ ts: `t${n}`, tier: "gpu", kind, model: `m${n}` });

  test("pills and the newest-first cap", () => {
    expect(eventPill(ev("load", 1))).toEqual({ cls: "pill-info", label: "load" });
    expect(eventPill(ev("unload", 1))).toEqual({ cls: "pill-muted", label: "unload" });
    const many = Array.from({ length: 15 }, (_, i) => ev("load", i));
    expect(recentEvents(many).map((e) => e.ts)).toEqual(many.slice(0, 10).map((e) => e.ts));
    expect(recentEvents(many, 2)).toHaveLength(2);
  });
});
