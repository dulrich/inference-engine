import { describe, expect, test } from "bun:test";
import { guardedNavClick, parsePath, TABS, tabToPath } from "./routes.ts";

describe("tabToPath", () => {
  test("every tab maps to its own path -- Serving owns \"/\"", () => {
    expect(tabToPath("serving")).toBe("/");
    expect(tabToPath("router")).toBe("/router");
  });
});

describe("parsePath", () => {
  test("resolves each tab's bare path", () => {
    expect(parsePath("/")).toEqual({ tab: "serving", matched: true });
    expect(parsePath("/router")).toEqual({ tab: "router", matched: true });
  });

  test("the research paths moved to the research dashboard -- unmatched, normalizing to Serving", () => {
    for (const p of ["/runs", "/runs/fixture-v2-full", "/ladders", "/longcontext", "/fit", "/queue"]) {
      expect(parsePath(p)).toEqual({ tab: "serving", matched: false });
    }
  });

  test("an unrecognized path is unmatched, defaulting to serving", () => {
    expect(parsePath("/nonsense")).toEqual({ tab: "serving", matched: false });
    expect(parsePath("/serving")).toEqual({ tab: "serving", matched: false });
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

describe("TABS", () => {
  test("two labelled tabs: Serving, Router", () => {
    expect(TABS.map((t) => t.label)).toEqual(["Serving", "Router"]);
    expect(TABS.map((t) => t.tab)).toEqual(["serving", "router"]);
  });
});
