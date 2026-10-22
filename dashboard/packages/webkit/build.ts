// @fleet-tools/webkit — owned SPA build: Bun.build + svelte/compiler (replaces vite + vite-plugin-svelte).
// Canonical home: fleet-tools/webkit/. Vendored into <app>/packages/webkit/ by install.sh; never edit a copy.
// An app keeps only its constants in apps/web/build.config.ts and a thin apps/web/build.ts:
//   import { config } from "./build.config.ts";
//   export const kit = createWebkit(config);
//   if (import.meta.main) await kit.main();
// CLI (via the thin entry):
//   bun build.ts          production build into dist/
//   bun build.ts --watch  rebuild on src/ change
//   bun build.ts --dev    watch + dev server (dist/ with SPA fallback, proxied prefixes)
import { watch } from "node:fs";
import { readdir, rm } from "node:fs/promises";
import { join, relative, sep } from "node:path";
import type { BuildOutput, BunPlugin, Server, ServerWebSocket } from "bun";
import { minifySync, type MinifyOptions } from "oxc-minify";
import { compile, compileModule } from "svelte/compiler";

export type WebkitConfig = {
  /** apps/web directory: holds index.html and src/, dist/ is written beside them. */
  here: string;
  DEV_PORT: number;
  DEV_HOST?: string;
  PROXY: Record<string, string>;
  NAMING: { entry: string; chunk: string; asset: string };
  /** Only these build-time env vars reach client code, as `import.meta.env.<KEY>`. */
  ENV_KEYS: readonly string[];
};

export type BuildOptions = { dev?: boolean; outdir?: string; env?: Record<string, string | undefined> };

const tsErase = new Bun.Transpiler({ loader: "ts", trimUnusedImports: false });

/** compileModule parses JS only, so `.svelte.ts` types are erased first. */
export function compileSvelteModule(src: string, filename: string, dev = false): string {
  const js = filename.endsWith(".ts") ? tsErase.transformSync(src) : src;
  return compileModule(js, { filename, generate: "client", dev }).js.code;
}

export function sveltePlugin(dev = false): BunPlugin {
  const cssByPath = new Map<string, string>();
  return {
    name: "svelte",
    setup(build) {
      // Extracted component CSS comes back as a virtual `<file>.svelte.css` import.
      build.onResolve({ filter: /\.svelte\.css$/ }, ({ path }) => ({ path, namespace: "svelte-css" }));
      build.onLoad({ filter: /.*/, namespace: "svelte-css" }, ({ path }) => ({
        contents: cssByPath.get(path) ?? "",
        loader: "css",
      }));
      build.onLoad({ filter: /\.svelte$/ }, async ({ path }) => {
        const source = await Bun.file(path).text();
        const { js, css, warnings } = compile(source, { filename: path, generate: "client", css: "external", dev });
        for (const w of warnings) console.warn(`${path}: ${w.message}`);
        if (!css?.code) return { contents: js.code, loader: "js" };
        cssByPath.set(`${path}.css`, css.code);
        return { contents: `import ${JSON.stringify(`${path}.css`)};\n${js.code}`, loader: "js" };
      });
      build.onLoad({ filter: /\.svelte\.(ts|js)$/ }, async ({ path }) => ({
        contents: compileSvelteModule(await Bun.file(path).text(), path, dev),
        loader: "js",
      }));
    },
  };
}

/** `import.meta.env` replacement: whitelisted keys only, undefined when unset. */
export function envDefine(keys: readonly string[], env: Record<string, string | undefined> = process.env): Record<string, string> {
  const picked: Record<string, string> = {};
  for (const k of keys) if (env[k] !== undefined) picked[k] = env[k];
  return { "import.meta.env": JSON.stringify(picked) };
}

// ---- chunk CSS: Bun links only the entry's CSS from index.html; lazy chunks load their own ----

/**
 * Prepended to a JS chunk whose `cssBundle` index.html doesn't link. Idempotent (skips an
 * existing <link> for the href) and top-level-awaits the stylesheet, so a dynamically imported
 * component's module resolves only once its CSS is applied (never rendered unstyled).
 */
export function cssInjector(href: string): string {
  return `await(function(h){var d=document;if(d.querySelector('link[rel="stylesheet"][href="'+h+'"]'))return;var l=d.createElement("link");l.rel="stylesheet";l.href=h;var p=new Promise(function(r){l.onload=l.onerror=r});d.head.appendChild(l);return p})(${JSON.stringify(href)});\n`;
}

/** Hrefs of every `<link rel="stylesheet">` in an HTML document. */
export function linkedStylesheets(html: string): Set<string> {
  const out = new Set<string>();
  for (const tag of html.match(/<link\b[^>]*>/gi) ?? []) {
    if (!/\brel=["']?stylesheet\b/i.test(tag)) continue;
    const href = tag.match(/\bhref=["']?([^"'\s>]+)/i)?.[1];
    if (href) out.add(href);
  }
  return out;
}

const toHref = (outdir: string, abs: string, publicPath: string) => publicPath + relative(outdir, abs).split(sep).join("/");

async function filesUnder(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { recursive: true, withFileTypes: true });
  return entries.filter((e) => e.isFile()).map((e) => join(e.parentPath, e.name));
}

/**
 * Inject a stylesheet loader into every JS chunk whose metafile `cssBundle` index.html doesn't
 * link, then fail if any emitted `.css` is neither linked nor injected. Returns the injected
 * chunk → css href map (relative output keys).
 */
export async function injectChunkCss(r: BuildOutput, outdir: string, publicPath = "/"): Promise<Map<string, string>> {
  const html = await Bun.file(join(outdir, "index.html")).text();
  const linked = linkedStylesheets(html);
  const injected = new Map<string, string>();
  const outputs = r.metafile?.outputs ?? {};
  for (const [key, meta] of Object.entries(outputs)) {
    const cssBundle = (meta as { cssBundle?: string }).cssBundle;
    if (!key.endsWith(".js") || !cssBundle) continue;
    const href = toHref(outdir, join(outdir, cssBundle), publicPath);
    if (linked.has(href)) continue;
    const file = join(outdir, key);
    await Bun.write(file, cssInjector(href) + (await Bun.file(file).text()));
    injected.set(key, href);
  }
  const covered = new Set([...linked, ...injected.values()]);
  const orphans = (await filesUnder(outdir))
    .filter((f) => f.endsWith(".css"))
    .map((f) => toHref(outdir, f, publicPath))
    .filter((h) => !covered.has(h));
  if (orphans.length) throw new Error(`build emitted CSS that is neither linked by index.html nor injected by a chunk: ${orphans.join(", ")}`);
  return injected;
}

// ---- production JS minification: oxc-minify post-pass (Bun's minifier is ~+22 % gz) ----
export const MINIFY_OPTIONS: MinifyOptions = {
  module: true,
  compress: { target: "es2022" },
  mangle: { toplevel: true },
  codegen: { removeWhitespace: true },
};

export async function minifyJs(outdir: string): Promise<void> {
  for (const file of (await filesUnder(outdir)).filter((f) => f.endsWith(".js"))) {
    const res = minifySync(file, await Bun.file(file).text(), MINIFY_OPTIONS);
    if (res.errors.length) throw new AggregateError(res.errors, `oxc-minify failed on ${file}`);
    await Bun.write(file, res.code);
  }
}

// ---- dev server: exported pieces take upstream origins as parameters ----
type WsData = { upstream: string; protocols: string[]; socket?: WebSocket; queue: (string | Buffer)[] };

function proxyTarget(url: URL, proxy: Record<string, string>): string | undefined {
  for (const [prefix, origin] of Object.entries(proxy)) {
    if (url.pathname === prefix || url.pathname.startsWith(`${prefix}/`)) return origin + url.pathname + url.search;
  }
  return undefined;
}

/** Request handler: proxy prefixes go upstream (HTTP + websocket upgrade), else dist with SPA fallback. */
export function createDevHandler(opts: { root: string; proxy: Record<string, string>; fetchImpl?: typeof fetch }) {
  const fetchImpl = opts.fetchImpl ?? fetch;
  return async (req: Request, server?: Pick<Server<WsData>, "upgrade">): Promise<Response | undefined> => {
    const url = new URL(req.url);
    const target = proxyTarget(url, opts.proxy);
    if (target) {
      if (req.headers.get("upgrade")?.toLowerCase() === "websocket" && server) {
        const protocols = (req.headers.get("sec-websocket-protocol") ?? "").split(",").map((s) => s.trim()).filter(Boolean);
        const upstream = target.replace(/^http/, "ws");
        if (server.upgrade(req, { data: { upstream, protocols, queue: [] } })) return undefined;
        return new Response("websocket upgrade failed", { status: 400 });
      }
      const headers = new Headers(req.headers);
      headers.delete("host");
      const body = req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer();
      return fetchImpl(target, { method: req.method, headers, body, redirect: "manual" });
    }
    const rel = decodeURIComponent(url.pathname).replace(/^\/+/, "");
    if (rel && !rel.split("/").includes("..")) {
      const file = Bun.file(join(opts.root, rel));
      if (await file.exists()) return new Response(file);
    }
    return new Response(Bun.file(join(opts.root, "index.html")), { headers: { "content-type": "text/html;charset=utf-8" } });
  };
}

/** Websocket handlers bridging a client socket to its upstream; `connect` is injectable for tests. */
export function createWsProxy(connect: (url: string, protocols: string[]) => WebSocket = (u, p) => new WebSocket(u, p)) {
  return {
    open(ws: ServerWebSocket<WsData>) {
      const up = connect(ws.data.upstream, ws.data.protocols);
      ws.data.socket = up;
      up.binaryType = "arraybuffer";
      up.addEventListener("open", () => {
        for (const m of ws.data.queue.splice(0)) up.send(m);
      });
      up.addEventListener("message", (e) => ws.send(typeof e.data === "string" ? e.data : new Uint8Array(e.data as ArrayBuffer)));
      up.addEventListener("close", (e) => ws.close(e.code === 1005 ? 1000 : e.code, e.reason));
      up.addEventListener("error", () => ws.close(1011, "upstream error"));
    },
    message(ws: ServerWebSocket<WsData>, msg: string | Buffer) {
      const up = ws.data.socket;
      if (up?.readyState === WebSocket.OPEN) up.send(msg);
      else ws.data.queue.push(msg);
    },
    close(ws: ServerWebSocket<WsData>) {
      ws.data.socket?.close();
    },
  };
}

/** Bind the app config: the per-app build, env whitelist, dev listener options and CLI. */
export function createWebkit(config: WebkitConfig) {
  const { here, DEV_PORT, DEV_HOST, PROXY, NAMING, ENV_KEYS } = config;
  const OUTDIR = join(here, "dist");
  const publicPath = "/";

  async function build(opts: BuildOptions = {}) {
    const dev = opts.dev ?? false;
    const outdir = opts.outdir ?? OUTDIR;
    await rm(outdir, { recursive: true, force: true });
    const r = await Bun.build({
      entrypoints: [join(here, "index.html")],
      outdir,
      splitting: true,
      // Bun minifies CSS (and JS whitespace/syntax); JS identifiers are left to the oxc-minify post-pass.
      minify: dev ? false : { whitespace: true, syntax: true, identifiers: false },
      metafile: true,
      publicPath,
      sourcemap: dev ? "linked" : "none",
      naming: NAMING,
      plugins: [sveltePlugin(dev)],
      define: { "process.env.NODE_ENV": JSON.stringify(dev ? "development" : "production"), ...envDefine(ENV_KEYS, opts.env) },
      conditions: ["browser", "svelte", dev ? "development" : "production"],
    });
    if (!r.success) throw new AggregateError(r.logs, "build failed");
    await injectChunkCss(r, outdir, publicPath);
    if (!dev) await minifyJs(outdir);
    return r;
  }

  /** Listener options for Bun.serve (asserted in tests; only `--dev` binds). */
  function devServeOptions(root = OUTDIR, proxy = PROXY) {
    const handler = createDevHandler({ root, proxy });
    return {
      port: DEV_PORT,
      hostname: DEV_HOST,
      fetch: (req: Request, server: Server<WsData>) => handler(req, server),
      websocket: createWsProxy(),
    };
  }

  async function main(argv = process.argv.slice(2)) {
    if (argv.includes("--help") || argv.includes("-h")) {
      console.log("usage: bun build.ts [--watch | --dev]\n\n  (none)   production build into dist/\n  --watch  rebuild on src/ change\n  --dev    watch + dev server (dist/ with SPA fallback, proxied prefixes)\n  -h, --help  print this usage and exit");
      return;
    }
    const dev = argv.includes("--dev");
    const watching = dev || argv.includes("--watch");
    const run = async () => {
      const t = performance.now();
      try {
        const r = await build({ dev });
        console.log(`built ${r.outputs.length} files in ${(performance.now() - t).toFixed(0)}ms`);
      } catch (e) {
        console.error(e instanceof AggregateError ? e.errors : e);
        if (!watching) process.exit(1);
      }
    };
    await run();
    if (!watching) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    watch(join(here, "src"), { recursive: true }, () => {
      clearTimeout(timer);
      timer = setTimeout(run, 30);
    });
    if (dev) {
      const server = Bun.serve(devServeOptions());
      console.log(`dev server http://localhost:${server.port} (proxy ${JSON.stringify(PROXY)})`);
    }
  }

  return {
    config,
    OUTDIR,
    build,
    envDefine: (env: Record<string, string | undefined> = process.env) => envDefine(ENV_KEYS, env),
    devServeOptions,
    main,
  };
}
