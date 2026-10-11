// App smoke for the vendored webkit build; the kit's own tests run in fleet-tools/webkit/.
import { expect, test } from "bun:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DEV_PORT, PROXY } from "./build.config.ts";
import { kit } from "./build.ts";

const SENTINEL = "__WEBKIT_ENV_LEAK_SENTINEL_7f3a__";

async function builtAssets(env: Record<string, string | undefined>) {
  const outdir = await mkdtemp(join(tmpdir(), "web-build-"));
  try {
    await kit.build({ outdir, env });
    const files = await readdir(join(outdir, "assets"));
    const read = async (ext: string) =>
      (await Promise.all(files.filter((f) => f.endsWith(ext)).map((f) => Bun.file(join(outdir, "assets", f)).text()))).join("\n");
    return { files, js: await read(".js"), html: await Bun.file(join(outdir, "index.html")).text() };
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}

test("builds, links the entry CSS and inlines only the env whitelist", async () => {
  const set = await builtAssets({ VITE_COMMAND_CENTER_URL: "http://cc.example:4730/", OTHER_SECRET: SENTINEL });
  expect(set.files.some((f) => /^index-[a-z0-9]+\.js$/.test(f))).toBe(true);
  expect(set.html).toMatch(/<link rel="stylesheet"[^>]*href="\/assets\/index-[a-z0-9]+\.css"/);
  expect(set.js).toContain("http://cc.example:4730/");
  expect(set.js).not.toContain("OTHER_SECRET");
  expect(set.js).not.toContain(SENTINEL);

  const unset = await builtAssets({});
  expect(unset.js).not.toContain("cc.example");
  expect(unset.js).toContain(":4730/"); // AppHeader's location fallback survives
  expect(unset.js).not.toMatch(/import\.meta\.env/);
});

test("dev listener carries the app constants without binding", () => {
  expect(kit.devServeOptions().port).toBe(DEV_PORT);
  expect(DEV_PORT).toBe(5810);
  expect(PROXY).toEqual({ "/api": "http://127.0.0.1:4810", "/health": "http://127.0.0.1:4810" });
});
