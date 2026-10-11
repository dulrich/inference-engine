import { describe, expect, test } from "bun:test";
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  compileSvelteModule,
  createDevHandler,
  createWebkit,
  createWsProxy,
  linkedStylesheets,
  MINIFY_OPTIONS,
  type WebkitConfig,
} from "./build.ts";

// Collision-proof env-leak marker: no minifier can produce it as an identifier or string fragment.
const SENTINEL = "__WEBKIT_ENV_LEAK_SENTINEL_7f3a__";

const config: WebkitConfig = {
  here: join(import.meta.dir, "fixture"),
  DEV_PORT: 5799,
  DEV_HOST: undefined,
  PROXY: { "/api": "http://localhost:4799" },
  NAMING: { entry: "[name].[ext]", chunk: "assets/[name]-[hash].[ext]", asset: "assets/[name]-[hash].[ext]" },
  ENV_KEYS: ["WEBKIT_FIXTURE_URL"],
};
const kit = createWebkit(config);

async function built(env: Record<string, string | undefined>, opts: { dev?: boolean } = {}) {
  const outdir = await mkdtemp(join(tmpdir(), "webkit-build-"));
  try {
    const r = await kit.build({ outdir, env, dev: opts.dev });
    const files = await readdir(join(outdir, "assets"));
    const contents = new Map(await Promise.all(files.map(async (f) => [f, await Bun.file(join(outdir, "assets", f)).text()] as const)));
    const text = async (f: string) => contents.get(f) ?? "";
    const read = (ext: string) => files.filter((f) => f.endsWith(ext)).map((f) => contents.get(f)).join("\n");
    return { r, files, text, js: read(".js"), css: read(".css"), html: await Bun.file(join(outdir, "index.html")).text() };
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}

describe("build", () => {
  test("links the entry CSS and inlines only the env whitelist", async () => {
    const set = await built({ WEBKIT_FIXTURE_URL: "http://fixture.example:4799/", OTHER_SECRET: SENTINEL });
    expect(set.html).toMatch(/<link rel="stylesheet"[^>]*href="\/assets\/index-[a-z0-9]+\.css"/);
    const scoped = (await set.text(set.files.find((f) => /^index-.*\.css$/.test(f))!)).match(/\.entry-title\.(svelte-[a-z0-9]+)/)?.[1];
    expect(scoped).toBeDefined();
    expect(set.js).toContain(scoped!);
    expect(set.js).toContain("http://fixture.example:4799/");
    expect(set.js).not.toContain("OTHER_SECRET");
    expect(set.js).not.toContain(SENTINEL);
    expect(set.js).not.toMatch(/import\.meta\.env/);
  });

  test("lazy component chunk names its own cssBundle and injects it (metafile association)", async () => {
    const set = await built({});
    const outputs = set.r.metafile!.outputs as Record<string, { cssBundle?: string; entryPoint?: string }>;
    const lazyKey = Object.keys(outputs).find((k) => /^\.\/assets\/Lazy-[a-z0-9]+\.js$/.test(k))!;
    expect(lazyKey).toBeDefined();
    const lazyCss = outputs[lazyKey]!.cssBundle!;
    expect(lazyCss).toMatch(/^\.\/assets\/Lazy-[a-z0-9]+\.css$/);
    const href = lazyCss.replace(/^\./, "");
    expect(linkedStylesheets(set.html).has(href)).toBe(false); // Bun never links it
    const lazyJs = await set.text(lazyKey.replace("./assets/", ""));
    expect(lazyJs.startsWith("await")).toBe(true);
    expect(lazyJs).toContain(href);
    expect(await set.text(href.replace("/assets/", ""))).toMatch(/\.lazy-marker\.svelte-[a-z0-9]+\s*\{\s*color:/);
    // The linked entry chunk carries no injector.
    const entryJs = await set.text(set.files.find((f) => /^index-.*\.js$/.test(f))!);
    expect(entryJs).not.toContain(".css`");
    expect(entryJs).not.toContain('.css"');
  });

  test("fails the build when an emitted CSS file is neither linked nor injected", async () => {
    const orphan = createWebkit({ ...config, here: join(import.meta.dir, "fixture-orphan") });
    const outdir = await mkdtemp(join(tmpdir(), "webkit-orphan-"));
    try {
      await expect(orphan.build({ outdir })).rejects.toThrow(/neither linked by index\.html nor injected by a chunk: \/assets\/stray-/);
    } finally {
      await rm(outdir, { recursive: true, force: true });
    }
  });

  test("production JS is oxc-minified; dev JS is not", async () => {
    expect(MINIFY_OPTIONS.module).toBe(true);
    const prod = await built({});
    const dev = await built({}, { dev: true });
    expect(prod.js.length).toBeLessThan(dev.js.length / 2);
    expect(prod.js.split("\n").length).toBeLessThan(10);
    expect(dev.files.some((f) => f.endsWith(".js.map"))).toBe(true);
  });

  test("injected loader is idempotent and resolves once the stylesheet settles", async () => {
    const set = await built({});
    const lazyJs = await set.text(set.files.find((f) => /^Lazy-.*\.js$/.test(f))!);
    const injector = lazyJs.slice(0, lazyJs.indexOf(";import") + 1);
    const appended: { href: string; onload?: () => void }[] = [];
    let existing: unknown = null;
    const document = {
      querySelector: () => existing,
      createElement: () => ({}) as { href: string; onload?: () => void },
      head: { appendChild: (l: { href: string; onload?: () => void }) => appended.push(l) },
    };
    const run = new Function("document", `return (async () => { ${injector} })()`) as (d: unknown) => Promise<void>;
    let done = false;
    const p = run(document).then(() => (done = true));
    await Promise.resolve();
    expect(appended.length).toBe(1);
    expect(done).toBe(false); // waits for the stylesheet
    appended[0]!.onload!();
    await p;
    existing = {};
    await run(document);
    expect(appended.length).toBe(1); // second run is a no-op
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
    const dir = await mkdtemp(join(tmpdir(), "webkit-dev-"));
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

  test("listener options carry the app config without binding", () => {
    const opts = kit.devServeOptions();
    expect(opts.port).toBe(5799);
    expect(opts.hostname).toBeUndefined();
    expect(kit.OUTDIR).toBe(join(config.here, "dist"));
    expect(typeof opts.websocket.open).toBe("function");
  });
});
