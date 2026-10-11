// Owned SPA build: Bun.build + svelte/compiler (replaces vite + vite-plugin-svelte).
//   bun build.ts          production build into dist/
//   bun build.ts --watch  rebuild on src/ change
//   bun build.ts --dev    watch + dev server (dist/ with SPA fallback, /api proxied)
import { watch } from "node:fs";
import { rm } from "node:fs/promises";
import { join } from "node:path";
import type { BunPlugin, Server, ServerWebSocket } from "bun";
import { compile, compileModule } from "svelte/compiler";

// ---- app-specific constants (the only block a copying app edits) ----
// Dev port PINNED to 5810 — the SERVICES.md-reserved pair for "Inference Engine
// (dashboard)" (cycle-1 material finding M3: 5810 was documented but never wired). API on
// 4810 (`apps/api/src/config.ts` default `PORT`). Bun.serve has no port fallback, so the
// former vite `strictPort: true` is inherent: a taken port fails the dev server.
export const DEV_PORT = 5810;
export const DEV_HOST: string | undefined = undefined; // e.g. "0.0.0.0" for LAN access
export const PROXY: Record<string, string> = {
  "/api": "http://127.0.0.1:4810",
  "/health": "http://127.0.0.1:4810",
};
export const NAMING = {
  entry: "[name].[ext]",
  chunk: "assets/[name]-[hash].[ext]",
  asset: "assets/[name]-[hash].[ext]",
};
// Only these build-time env vars reach client code, as `import.meta.env.<KEY>`.
export const ENV_KEYS = ["VITE_COMMAND_CENTER_URL"] as const;
// ----------------------------------------------------------------------

const here = import.meta.dir;
export const OUTDIR = join(here, "dist");

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
export function envDefine(env: Record<string, string | undefined> = process.env): Record<string, string> {
  const picked: Record<string, string> = {};
  for (const k of ENV_KEYS) if (env[k] !== undefined) picked[k] = env[k];
  return { "import.meta.env": JSON.stringify(picked) };
}

export async function build(opts: { dev?: boolean; outdir?: string; env?: Record<string, string | undefined> } = {}) {
  const dev = opts.dev ?? false;
  const outdir = opts.outdir ?? OUTDIR;
  await rm(outdir, { recursive: true, force: true });
  const r = await Bun.build({
    entrypoints: [join(here, "index.html")],
    outdir,
    splitting: true,
    minify: !dev,
    publicPath: "/",
    sourcemap: dev ? "linked" : "none",
    naming: NAMING,
    plugins: [sveltePlugin(dev)],
    define: { "process.env.NODE_ENV": JSON.stringify(dev ? "development" : "production"), ...envDefine(opts.env) },
    conditions: ["browser", "svelte", dev ? "development" : "production"],
  });
  if (!r.success) throw new AggregateError(r.logs, "build failed");
  return r;
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

/** Listener options for Bun.serve (asserted in tests; only `--dev` binds). */
export function devServeOptions(root = OUTDIR, proxy = PROXY) {
  const handler = createDevHandler({ root, proxy });
  return {
    port: DEV_PORT,
    hostname: DEV_HOST,
    fetch: (req: Request, server: Server<WsData>) => handler(req, server),
    websocket: createWsProxy(),
  };
}

async function main() {
  const argv = process.argv.slice(2);
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

if (import.meta.main) await main();
