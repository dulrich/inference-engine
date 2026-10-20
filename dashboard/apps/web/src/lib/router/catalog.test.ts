import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ServingCatalogProjection, ServingStatusResponse } from "@bench-dashboard/shared";
import { ServingStatusFileSchema } from "@bench-dashboard/shared";
import {
  addVerdict,
  addableProviders,
  catalogBlock,
  chipVerdict,
  entryKey,
  isRouterInternal,
  latestCatalogOutcome,
  nextCatalogConsumers,
  outcomeFor,
  outcomePillClass,
  removeVerdict,
  uiConsumers,
} from "./catalog.ts";

// The panel's decision table, exercised against the committed contract-v2 status fixture --
// the same bytes the dispatcher's tests regenerate and the Rust reader parses.
const FIXTURE = join(import.meta.dir, "../../../../../fixtures/serving/contract-v2/control/host/serving-status.json");
const file = ServingStatusFileSchema.parse(JSON.parse(readFileSync(FIXTURE, "utf8")));
const status: ServingStatusResponse = { ...file, available: true };

function block(): ServingCatalogProjection {
  const value = catalogBlock(status);
  if (value === null) throw new Error("fixture carries no catalog block");
  return value;
}

function provider(name: string) {
  const found = block().providers.find((p) => p.name === name);
  if (found === undefined) throw new Error(`no provider ${name}`);
  return found;
}

function model(providerName: string, id: string) {
  const found = provider(providerName).models.find((m) => m.id === id);
  if (found === undefined) throw new Error(`no model ${providerName}/${id}`);
  return found;
}

describe("catalogBlock", () => {
  test("reads the fixture's projection and nothing from an unavailable status", () => {
    expect(block().revision).toMatch(/^[0-9a-f]{16}$/);
    expect(catalogBlock(null)).toBeNull();
    expect(catalogBlock({ available: false, reason: "stale" })).toBeNull();
    const { catalog: _omitted, ...v1 } = file;
    expect(catalogBlock({ ...v1, available: true })).toBeNull();
  });
});

describe("consumers", () => {
  test("an absent list is every consumer; router is never a UI consumer", () => {
    expect(uiConsumers(null)).toEqual(["cc", "openwebui"]);
    expect(uiConsumers(["router"])).toEqual([]);
    expect(uiConsumers(["openwebui", "cc"])).toEqual(["cc", "openwebui"]);
  });

  test("toggling yields the full canonical array", () => {
    expect(nextCatalogConsumers(["router"], "cc", true)).toEqual(["cc"]);
    expect(nextCatalogConsumers(null, "cc", false)).toEqual(["openwebui"]);
    expect(nextCatalogConsumers(["openwebui"], "cc", true)).toEqual(["cc", "openwebui"]);
  });

  test("the router tag is detected for the muted marker", () => {
    expect(isRouterInternal(model("codex-a", "gpt-6-sol-low").consumers)).toBe(true);
    expect(isRouterInternal(null)).toBe(false);
  });

  test("cc needs a routed id", () => {
    expect(chipVerdict("cc", false)).toEqual({ ok: false, reason: "not a gateway model" });
    expect(chipVerdict("openwebui", false).ok).toBe(true);
    expect(chipVerdict("cc", true).ok).toBe(true);
  });
});

describe("removeVerdict", () => {
  test("protected ids and a provider's last id are not removable", () => {
    expect(removeVerdict(provider("codex-a"), model("codex-a", "gpt-6-sol")).ok).toBe(false);
    expect(removeVerdict(provider("claude-local"), model("claude-local", "nemotron-4b")).reason).toContain(
      "clear its consumers",
    );
    expect(removeVerdict(provider("openrouter-decide"), model("openrouter-decide", "typesafe/jev-1.13")).reason).toContain(
      "last id",
    );
    expect(removeVerdict(provider("claude-local"), model("claude-local", "qwen36-a3b")).ok).toBe(true);
  });
});

describe("addVerdict", () => {
  test("codex providers are not offered", () => {
    expect(addableProviders(block()).some((p) => p.kind === "codex")).toBe(false);
  });

  test("mirrors the dispatcher's refusals", () => {
    const b = block();
    expect(addVerdict(null, "x/y", b).reason).toBe("pick a provider");
    expect(addVerdict(provider("openrouter"), "", b).reason).toBe("enter an id");
    expect(addVerdict(provider("openrouter"), " x/y", b).ok).toBe(false);
    expect(addVerdict(provider("anthropic"), "@preset/a", b).reason).toContain("OpenRouter legs");
    expect(addVerdict(provider("openrouter"), "@preset/a", b).ok).toBe(true);
    expect(addVerdict(provider("openrouter"), "qwen/qwen3.8-flash", b).reason).toBe("already in the catalog");
    expect(addVerdict(provider("claude-local"), "invented", b).reason).toBe("not an unrouted llama-swap alias");
    expect(addVerdict(provider("codex-a"), "x", b).ok).toBe(false);
    expect(addVerdict(provider("openrouter"), "vendor/new-model", b)).toEqual({ ok: true, reason: null });
  });
});

describe("outcomes", () => {
  test("the newest v2 record carries the applied revision", () => {
    const latest = latestCatalogOutcome(status);
    expect(latest?.state).toBe("applied");
    expect(latest?.revision).toBe(block().revision);
    expect(latestCatalogOutcome(null)).toBeNull();
  });

  test("a known id resolves; an unknown one is still pending", () => {
    const first = status.available ? status.recent[0] : null;
    expect(outcomeFor(status, first?.request_id ?? "")?.state).toBe("applied");
    expect(outcomeFor(status, "srv-unknown")).toBeNull();
  });

  test("pill classes", () => {
    expect(outcomePillClass("applied")).toBe("pill-ok");
    expect(outcomePillClass("rejected")).toBe("pill-warn");
    expect(outcomePillClass("failed")).toBe("pill-error");
    expect(entryKey("openrouter", "a/b")).toBe("openrouter/a/b");
  });
});
