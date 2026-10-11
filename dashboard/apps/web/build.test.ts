import { describe, expect, test } from "bun:test";
import { mkdtemp, readdir, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build, compileSvelteModule, createDevHandler, createWsProxy, devServeOptions, DEV_PORT, PROXY } from "./build.ts";

async function builtAssets(env: Record<string, string | undefined>) {
  const outdir = await mkdtemp(join(tmpdir(), "web-build-"));
  try {
    await build({ outdir, env });
    const files = await readdir(join(outdir, "assets"));
    const read = async (ext: string) =>
      (await Promise.all(files.filter((f) => f.endsWith(ext)).map((f) => Bun.file(join(outdir, "assets", f)).text()))).join("\n");
    return { files, js: await read(".js"), css: await read(".css"), html: await Bun.file(join(outdir, "index.html")).text() };
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}

describe("build", () => {
  test("extracts component CSS into a separate hashed file and inlines the env whitelist", async () => {
    const set = await builtAssets({ VITE_COMMAND_CENTER_URL: "http://cc.example:4730/", OTHER_SECRET: "other-secret-sentinel" });
    expect(set.files.some((f) => /^index-[a-z0-9]+\.css$/.test(f))).toBe(true);
    expect(set.files.some((f) => /^index-[a-z0-9]+\.js$/.test(f))).toBe(true);
    // Scoped component CSS lives in the .css file, the matching class hash in the JS.
    const scoped = set.css.match(/svelte-[a-z0-9]+/)?.[0];
    expect(scoped).toBeDefined();
    expect(set.js).toContain(scoped!);
    expect(set.html).toMatch(/<link rel="stylesheet"[^>]*href="\/assets\/index-[a-z0-9]+\.css"/);
    expect(set.js).toContain("http://cc.example:4730/");
    expect(set.js).not.toContain("OTHER_SECRET");
    expect(set.js).not.toContain("other-secret-sentinel");

    const unset = await builtAssets({});
    expect(unset.js).not.toContain("cc.example");
    expect(unset.js).toContain(":4730/"); // AppHeader's location fallback survives
    expect(unset.js).not.toMatch(/import\.meta\.env/);
  });

  test("erases TypeScript before compileModule in .svelte.ts", () => {
    const src = `import { untrack } from "svelte";
import type { Thing } from "./thing.ts";
export interface Resource<T> { data: T | null; readonly loading: boolean }
export function resource<T>(fetcher: () => Promise<T>): Resource<T> {
  let data = $state<T | null>(null);
  const n = (fetcher as unknown as { length: number }).length;
  return { get data() { return untrack(() => data) }, set data(v: T | null) { data = v }, get loading() { return n < 0 } };
}
export type Alias = Thing;`;
    const js = compileSvelteModule(src, "/virtual/resource.svelte.ts");
    expect(js).toContain('from "svelte"');
    expect(js).not.toMatch(/interface|import type|as unknown|<T>/);
    expect(js).not.toContain("$state");
  });
});

describe("dev server", () => {
  async function root() {
    const dir = await mkdtemp(join(tmpdir(), "web-dev-"));
    await mkdir(join(dir, "assets"));
    await writeFile(join(dir, "index.html"), "<html>spa</html>");
    await writeFile(join(dir, "assets", "a.js"), "console.log(1)");
    return dir;
  }

  test("serves dist files with SPA fallback and blocks traversal", async () => {
    const dir = await root();
    const h = createDevHandler({ root: dir, proxy: {}, fetchImpl: (() => Promise.reject(new Error("no upstream"))) as unknown as typeof fetch });
    expect(await (await h(new Request("http://dev/assets/a.js")))!.text()).toBe("console.log(1)");
    expect(await (await h(new Request("http://dev/some/route")))!.text()).toBe("<html>spa</html>");
    expect(await (await h(new Request("http://dev/../../etc/passwd")))!.text()).toBe("<html>spa</html>");
    await rm(dir, { recursive: true, force: true });
  });

  test("proxies /api HTTP to the injected upstream", async () => {
    const seen: { url: string; method: string; body: string }[] = [];
    const fake = (async (url: string, init: RequestInit) => {
      seen.push({ url, method: init.method!, body: init.body ? new TextDecoder().decode(init.body as ArrayBuffer) : "" });
      return new Response("upstream-ok", { status: 201 });
    }) as unknown as typeof fetch;
    const h = createDevHandler({ root: "/nonexistent", proxy: { "/api": "http://up.test:9" }, fetchImpl: fake });
    const res = (await h(new Request("http://dev/api/items?x=1", { method: "POST", body: "hi" })))!;
    expect(res.status).toBe(201);
    expect(seen).toEqual([{ url: "http://up.test:9/api/items?x=1", method: "POST", body: "hi" }]);
    // /apix is not under the /api prefix
    const spa = await h(new Request("http://dev/apix"), undefined);
    expect(spa!.status).toBe(200);
    expect(seen.length).toBe(1);
  });

  test("upgrades /api websockets and bridges messages to the injected upstream", async () => {
    let upgraded: unknown;
    const h = createDevHandler({ root: "/nonexistent", proxy: { "/api": "http://up.test:9" } });
    const res = await h(
      new Request("http://dev/api/ws", { headers: { upgrade: "websocket", "sec-websocket-protocol": "a, b" } }),
      { upgrade: (_req: Request, opts?: { data?: unknown }) => ((upgraded = opts?.data), true) } as never,
    );
    expect(res).toBeUndefined();
    expect(upgraded).toEqual({ upstream: "ws://up.test:9/api/ws", protocols: ["a", "b"], queue: [] });

    const upstream = new EventTarget() as EventTarget & { readyState: number; sent: unknown[]; send(m: unknown): void; close(): void; binaryType: string };
    Object.assign(upstream, { readyState: 0, sent: [], binaryType: "", send(m: unknown) { upstream.sent.push(m); }, close() {} });
    let connected: [string, string[]] | undefined;
    const proxy = createWsProxy((u, p) => ((connected = [u, p]), upstream as unknown as WebSocket));
    const toClient: unknown[] = [];
    let closed: [number, string] | undefined;
    const client = { data: upgraded, send: (m: unknown) => toClient.push(m), close: (c: number, r: string) => (closed = [c, r]) } as never;
    proxy.open(client);
    expect(connected).toEqual(["ws://up.test:9/api/ws", ["a", "b"]]);
    proxy.message(client, "early"); // queued until upstream opens
    expect(upstream.sent).toEqual([]);
    upstream.readyState = WebSocket.OPEN;
    upstream.dispatchEvent(new Event("open"));
    proxy.message(client, "late");
    expect(upstream.sent).toEqual(["early", "late"]);
    upstream.dispatchEvent(new MessageEvent("message", { data: "from-up" }));
    expect(toClient).toEqual(["from-up"]);
    upstream.dispatchEvent(new CloseEvent("close", { code: 4001, reason: "bye" }));
    expect(closed).toEqual([4001, "bye"]);
  });

  test("listener options carry the app constants without binding", () => {
    const opts = devServeOptions();
    expect(opts.port).toBe(DEV_PORT);
    expect(DEV_PORT).toBe(5810);
    expect(PROXY).toEqual({ "/api": "http://127.0.0.1:4810", "/health": "http://127.0.0.1:4810" });
    expect(opts.hostname).toBeUndefined();
    expect(typeof opts.websocket.open).toBe("function");
  });
});
