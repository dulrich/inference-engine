import { describe, expect, test } from "bun:test";
import { guardedNavClick, parsePath, runDetailPath, tabToPath } from "./routes.ts";

describe("tabToPath / runDetailPath", () => {
  test("every tab maps to its own path -- Serving owns \"/\" (WP-2 default landing route)", () => {
    expect(tabToPath("runs")).toBe("/runs");
    expect(tabToPath("ladders")).toBe("/ladders");
    expect(tabToPath("longcontext")).toBe("/longcontext");
    expect(tabToPath("fit")).toBe("/fit");
    expect(tabToPath("serving")).toBe("/");
    expect(tabToPath("queue")).toBe("/queue");
    expect(tabToPath("router")).toBe("/router");
  });

  test("runDetailPath encodes the experiment id", () => {
    expect(runDetailPath("wp1-smoke-cell-ling-q4km")).toBe("/runs/wp1-smoke-cell-ling-q4km");
  });
});

describe("parsePath", () => {
  test("resolves each tab's bare path -- \"/\" resolves to Serving (WP-2 default landing route)", () => {
    expect(parsePath("/")).toEqual({ tab: "serving", matched: true });
    expect(parsePath("/runs")).toEqual({ tab: "runs", matched: true });
    expect(parsePath("/ladders")).toEqual({ tab: "ladders", matched: true });
    expect(parsePath("/longcontext")).toEqual({ tab: "longcontext", matched: true });
    expect(parsePath("/fit")).toEqual({ tab: "fit", matched: true });
    expect(parsePath("/queue")).toEqual({ tab: "queue", matched: true });
    expect(parsePath("/router")).toEqual({ tab: "router", matched: true });
  });

  test("the pre-WP-2 \"/serving\" path is no longer a tab path (Serving moved to \"/\") -- unmatched, normalizes to Serving anyway via the shared fallback", () => {
    expect(parsePath("/serving")).toEqual({ tab: "serving", matched: false });
  });

  test("resolves a run detail path", () => {
    expect(parsePath("/runs/fixture-v2-full")).toEqual({ tab: "runs", runId: "fixture-v2-full", matched: true });
  });

  test("decodes an encoded run id", () => {
    expect(parsePath("/runs/a%2Fb")).toEqual({ tab: "runs", runId: "a/b", matched: true });
  });

  test("an unrecognized path is unmatched, defaulting to serving (WP-2 default landing route)", () => {
    expect(parsePath("/nonsense")).toEqual({ tab: "serving", matched: false });
  });

  test("/runs/ with no id is unmatched, defaulting to serving", () => {
    expect(parsePath("/runs/")).toEqual({ tab: "serving", matched: false });
  });
});

describe("guardedNavClick", () => {
  function clickEvent(overrides: Partial<MouseEvent> = {}): MouseEvent {
    let prevented = false;
    return {
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      button: 0,
      preventDefault: () => {
        prevented = true;
      },
      get defaultPrevented() {
        return prevented;
      },
      ...overrides,
    } as unknown as MouseEvent;
  }

  test("a plain left click prevents default and runs the action", () => {
    let called = false;
    const event = clickEvent();
    guardedNavClick(event, () => {
      called = true;
    });
    expect(called).toBe(true);
    expect(event.defaultPrevented).toBe(true);
  });

  test("a modified click (ctrl) falls through -- action not called, default not prevented", () => {
    let called = false;
    const event = clickEvent({ ctrlKey: true });
    guardedNavClick(event, () => {
      called = true;
    });
    expect(called).toBe(false);
    expect(event.defaultPrevented).toBe(false);
  });

  test("a non-primary button click falls through", () => {
    let called = false;
    const event = clickEvent({ button: 1 });
    guardedNavClick(event, () => {
      called = true;
    });
    expect(called).toBe(false);
  });
});
