# @fleet-tools/webkit

The fleet's owned Svelte SPA toolchain: `build.ts` (Bun.build + `svelte/compiler`, the dev server with proxy/websocket bridging) and `svelte-template-lint.ts` (template rules oxlint can't see). It replaces vite, vite-plugin-svelte and eslint-plugin-svelte.

**Canonical home:** `fleet-tools/webkit/`. Consumers get a vendored, hash-manifested copy at `<app>/packages/webkit/`. **Never edit a vendored copy** — fix it here, run the gates, then re-sync.

## Install / verify contract

Mirrors `signalworks/install.sh`:

- `bash webkit/install.sh --dest <app>` — replace the payload items in `<app>/packages/webkit/` (stale payload files are removed, the bun-created `node_modules` is preserved) and write `.manifest` (version + sorted sha256 per file; `node_modules` is never hashed). A consumer directory that does not exist is skipped with a stderr warning, never created. Run `bun install` in the app after a fresh copy.
- `bash webkit/install.sh --all` — every path in `consumers.txt`.
- `bash webkit/install.sh --list` / `--verify [--dest <app>]` — report or fail on drift (local edits in the copy, or source changes not yet re-synced).

Payload: `package.json`, `README.md`, `build.ts`, `svelte-template-lint.ts` and their tests. `install.sh`, `consumers.txt`, `tsconfig.json` and the test fixtures stay here. Each consumer runs `"webkit:verify": "bash ../webkit/install.sh --verify --dest ."` (path relative to the consumer) as a gate.

The kit's `svelte` and `oxc-minify` are `devDependencies` of the workspace member: a consumer's `bun install` resolves them, and `bun install --production` (the Dockerfile `prod-deps` stage) drops them. Both Dockerfile manifest COPY sets need `COPY packages/webkit/package.json packages/webkit/`.

## Consumer config

An app keeps only its constants in `apps/web/build.config.ts`:

```ts
import type { WebkitConfig } from "../../packages/webkit/build.ts";
export const config: WebkitConfig = { here: import.meta.dir, DEV_PORT, DEV_HOST, PROXY, NAMING, ENV_KEYS };
```

and a thin `apps/web/build.ts` entry:

```ts
import { createWebkit } from "../../packages/webkit/build.ts";
import { config } from "./build.config.ts";
export const kit = createWebkit(config);
if (import.meta.main) await kit.main(); // bun build.ts [--watch|--dev]
```

`kit.build({ dev?, outdir?, env? })`, `kit.envDefine()`, `kit.devServeOptions()` and `kit.OUTDIR` are bound to the config; `sveltePlugin`, `compileSvelteModule`, `createDevHandler`, `createWsProxy` are exported directly. `ENV_KEYS` is the whitelist of build-time env vars inlined as `import.meta.env.<KEY>`.

Template lint: `bun packages/webkit/svelte-template-lint.ts [dir...]` (default `apps/web/src`).

## Chunk CSS

Bun links only the entry's CSS from `index.html`. A dynamically imported component's `<style>` lands in its own CSS file, named by the lazy chunk's metafile `outputs[<chunk>].cssBundle`. The kit prepends an idempotent loader to each such chunk: it skips an existing `<link>` for the href, otherwise appends one and top-level-awaits its load, so the lazy module resolves only once its styles apply. No `<svelte:options css="injected" />` is needed. The build **fails** if any emitted `.css` is neither linked by `index.html` nor injected by a chunk.

## Minification

Production builds run `Bun.build` with `minify: false`, inject chunk CSS, then minify every emitted `.js` with oxc-minify (`MINIFY_OPTIONS`: module, es2022 compress, toplevel mangle, whitespace removal) — about 18 % smaller gzipped than Bun's minifier.

## Gates (here)

`bun test && bunx tsc -p tsconfig.json`, plus `trivy fs --scanners vuln --include-dev-deps --skip-db-update --exit-code 1 --severity CRITICAL,HIGH .`
