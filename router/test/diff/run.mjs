#!/usr/bin/env bun
// Differential contract harness runner (router/test/diff/README.md).
//   bun router/test/diff/run.mjs --target image:<tag>|bin:<path>
//        [--case <glob>] [--slow] [--config-source <path>] [--catalog-source <path>] [--keep] [--stub-image <tag>]
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { cases, OAUTH } from './cases.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRouter = path.resolve(here, '../..');
export const GW_KEY = 'test-gw-key';
const DEFAULT_STUB_IMAGE = 'node:24-slim';

function parseArgs(argv) {
  const a = { slow: false, keep: false, caseGlob: '*', configSource: path.join(repoRouter, 'gateway.config.json'), catalogSource: path.join(repoRouter, 'catalog', 'catalog.json') };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (k === '--target') a.target = argv[++i];
    else if (k === '--case') a.caseGlob = argv[++i];
    else if (k === '--config-source') a.configSource = path.resolve(argv[++i]);
    else if (k === '--catalog-source') a.catalogSource = path.resolve(argv[++i]);
    else if (k === '--slow') a.slow = true;
    else if (k === '--keep') a.keep = true;
    else if (k === '--stub-image') a.stubImage = argv[++i];
    else throw new Error(`unknown argument ${k}`);
  }
  if (!/^(image|bin):.+/.test(a.target || '')) throw new Error('--target image:<tag> | bin:<path> is required');
  // The stub needs `node`; the scratch gateway image has none, so default to a pinned public node image.
  if (!a.stubImage) a.stubImage = DEFAULT_STUB_IMAGE;
  return a;
}

// Config generation: rewrite every provider baseurl to <stub>/p/<name><original path suffix>,
// textually, so every other byte of the source config is kept. Fixture providers (needed only
// by cases the production config cannot express) are appended to the providers array.
export function generateConfig(sourceText, stubBase) {
  const parsed = JSON.parse(sourceText);
  const providers = parsed.providers || [];
  let i = 0;
  let out = sourceText.replace(/("baseurl"\s*:\s*)"([^"]*)"/g, (whole, lead, url) => {
    const p = providers[i++];
    if (!p || p.baseurl !== url) throw new Error(`baseurl #${i} does not align with providers[${i - 1}]`);
    const suffix = new URL(url).pathname.replace(/\/$/, '');
    return `${lead}${JSON.stringify(`${stubBase}/p/${p.name}${suffix}`)}`;
  });
  if (i !== providers.length) throw new Error(`rewrote ${i} baseurls for ${providers.length} providers`);
  const fixture = JSON.parse(fs.readFileSync(path.join(here, 'fixture-providers.json'), 'utf8'));
  // `models`/`consumers` are catalog fields (generateCatalog); the strict loader rejects them here.
  const extra = fixture.map(({ models: _m, consumers: _c, ...p }) => ({ ...p, baseurl: `${stubBase}/p/${p.name}${p.baseurl}` }));
  const close = out.lastIndexOf(']');
  out = `${out.slice(0, close).replace(/\s*$/, '')},\n${extra.map((p) => `    ${JSON.stringify(p)}`).join(',\n')}\n  ${out.slice(close)}`;
  JSON.parse(out);
  return out;
}

// Catalog generation: the source catalog plus one entry per fixture provider (its `models[]`,
// and its `consumers` per model), written as <tmp>/catalog/catalog.json.
export function generateCatalog(sourceText) {
  const catalog = JSON.parse(sourceText);
  const fixture = JSON.parse(fs.readFileSync(path.join(here, 'fixture-providers.json'), 'utf8'));
  for (const p of fixture) {
    catalog.providers[p.name] = {
      models: p.models.map((id) => {
        const consumers = Array.isArray(p.consumers) ? p.consumers : p.consumers?.[id];
        return consumers ? { id, consumers } : { id };
      }),
    };
  }
  return `${JSON.stringify(catalog, null, 2)}\n`;
}

function writeCatalog(run) {
  fs.mkdirSync(run.catalogDir, { recursive: true });
  fs.writeFileSync(path.join(run.catalogDir, 'catalog.json'), generateCatalog(fs.readFileSync(run.catalogSource, 'utf8')));
}

// The gateway's auth is always on (x-gateway-key, raw key).
const fakeEnv = (telemetryDir) => ({
  AUTH_STATIC_API_KEYS: GW_KEY,
  OPENROUTER_API_KEY: 'test-or-key',
  CODEX_SIDECAR_KEY: 'test-codex-key',
  GATEWAY_LOG_ENABLED: 'true',
  GATEWAY_LOG_LEVEL: 'info',
  GATEWAY_ACCESS_LOG: 'true',
  GATEWAY_METRICS_ENABLED: 'true',
  ROUTER_TELEMETRY_ENABLED: 'true',
  ROUTER_TELEMETRY_DIR: telemetryDir,
  ROUTER_TELEMETRY_MAX_BYTES: '16777216',
  ROUTER_TELEMETRY_KEEP_FILES: '14',
});

const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

async function waitFor(url, ms, label) {
  const deadline = Date.now() + ms;
  for (;;) {
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(2000) });
      await r.arrayBuffer();
      return;
    } catch {
      if (Date.now() > deadline) throw new Error(`${label} did not answer at ${url} within ${ms} ms`);
      await sleep(300);
    }
  }
}

async function startImage(tag, stubImage, run, cleanups) {
  const id = run.id;
  const netName = `rrdiff-net-${id}`;
  docker('network', 'create', '--internal', netName);
  cleanups.push(() => docker('network', 'rm', netName));
  const stubName = `rrdiff-stub-${id}`;
  docker('run', '-d', '--name', stubName, '--network', netName, '--entrypoint', 'node',
    '-v', `${here}:/diff:ro`, stubImage, '/diff/stub.mjs', '8080', '0.0.0.0');
  cleanups.push(() => docker('rm', '-f', stubName));
  const ip = (n) => docker('inspect', '-f', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', n);
  run.stubUrl = `http://${ip(stubName)}:8080`;
  await waitFor(`${run.stubUrl}/_log`, 20000, 'stub');
  fs.writeFileSync(run.configPath, generateConfig(fs.readFileSync(run.configSource, 'utf8'), `http://${stubName}:8080`));
  writeCatalog(run);
  const gwName = `rrdiff-gw-${id}`;
  const envArgs = Object.entries(fakeEnv('/var/log/inference-router')).flatMap(([k, v]) => ['-e', `${k}=${v}`]);
  docker('run', '-d', '--name', gwName, '--network', netName, '--user', `${process.getuid()}:${process.getgid()}`,
    '-v', `${run.configPath}:/app/gateway.config.json:ro`, '-v', `${run.catalogDir}:/app/catalog:ro`,
    '-v', `${run.telemetryDir}:/var/log/inference-router`,
    ...envArgs, tag);
  cleanups.push(() => {
    if (process.env.RRDIFF_GW_LOG) fs.writeFileSync(process.env.RRDIFF_GW_LOG, execFileSync('docker', ['logs', gwName], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }));
    docker('rm', '-f', gwName);
  });
  run.gwUrl = `http://${ip(gwName)}:4813`;
  await waitFor(`${run.gwUrl}/v1/models`, 60000, 'gateway');
}

async function startBin(binPath, run, cleanups) {
  const stub = spawn(process.execPath, [path.join(here, 'stub.mjs'), '0', '127.0.0.1'], { stdio: ['ignore', 'pipe', 'inherit'] });
  cleanups.push(() => stub.kill('SIGTERM'));
  const stubPort = await new Promise((resolve, reject) => {
    let buf = '';
    stub.stdout.on('data', (d) => {
      buf += d;
      const m = /"port":(\d+)/.exec(buf);
      if (m) resolve(Number(m[1]));
    });
    stub.once('exit', (c) => reject(new Error(`stub exited ${c}`)));
  });
  run.stubUrl = `http://127.0.0.1:${stubPort}`;
  fs.writeFileSync(run.configPath, generateConfig(fs.readFileSync(run.configSource, 'utf8'), run.stubUrl));
  writeCatalog(run);
  const gwPort = await freePort();
  const gw = spawn(path.resolve(binPath), [], {
    stdio: ['ignore', process.env.RRDIFF_GW_LOG ? fs.openSync(process.env.RRDIFF_GW_LOG, 'w') : 'ignore', 'inherit'],
    env: { PATH: process.env.PATH, HOST: '127.0.0.1', PORT: String(gwPort), GATEWAY_CONFIG_PATH: run.configPath, GATEWAY_CATALOG_PATH: path.join(run.catalogDir, 'catalog.json'), ...fakeEnv(run.telemetryDir) },
  });
  cleanups.push(() => gw.kill('SIGTERM'));
  run.gwUrl = `http://127.0.0.1:${gwPort}`;
  await waitFor(`${run.gwUrl}/v1/models`, 30000, 'gateway');
}

function globToRe(g) {
  return new RegExp(`^${g.split('*').map((s) => s.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')}$`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const id = `${process.pid}-${Math.random().toString(36).slice(2, 7)}`;
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'rrdiff-'));
  const run = { id, configSource: args.configSource, configPath: path.join(tmp, 'gateway.config.json'), catalogSource: args.catalogSource, catalogDir: path.join(tmp, 'catalog'), telemetryDir: path.join(tmp, 'telemetry') };
  fs.mkdirSync(run.telemetryDir, { mode: 0o777 });
  const cleanups = [];
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    for (const fn of cleanups.reverse()) {
      try { fn(); } catch (e) { process.stderr.write(`cleanup: ${String(e.message || e).split('\n')[0]}\n`); }
    }
  };
  for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(sig, () => { cleanup(); process.exit(130); });
  // A closed stdout pipe (e.g. `| head`) must not kill the run before cleanup.
  process.stdout.on('error', () => {});
  process.on('uncaughtException', (e) => { process.stderr.write(`uncaught: ${e && e.stack || e}\n`); cleanup(); process.exit(1); });
  process.on('exit', cleanup);

  const re = globToRe(args.caseGlob);
  const selected = cases.filter((c) => re.test(c.id) && c.tags.some((t) => t === 'parity' || t === 'changed') && (args.slow || !c.tags.includes('slow')));
  let failed = 0;
  try {
    const [mode, ref] = [args.target.slice(0, args.target.indexOf(':')), args.target.slice(args.target.indexOf(':') + 1)];
    if (mode === 'image') await startImage(ref, args.stubImage, run, cleanups);
    else await startBin(ref, run, cleanups);
    const ctx = makeCtx(run);
    console.log(`TAP version 13\n1..${selected.length}`);
    let n = 0;
    for (const c of selected) {
      n++;
      const started = Date.now();
      try {
        await ctx.script(c.script || {});
        await c.run(ctx);
        console.log(`ok ${n} - ${c.id} [${c.tags.join(',')}] (${Date.now() - started} ms)`);
      } catch (err) {
        failed++;
        console.log(`not ok ${n} - ${c.id} [${c.tags.join(',')}] (${Date.now() - started} ms)`);
        console.log(`  # ${String(err && err.message || err).split('\n').slice(0, 6).join('\n  # ')}`);
      }
    }
  } catch (err) {
    failed++;
    console.log(`Bail out! harness error: ${err && err.stack || err}`);
  } finally {
    cleanup();
  }
  console.log(`# target=${args.target} cases=${selected.length} failed=${failed}`);
  console.log(`# config=${run.configPath} telemetryDir=${run.telemetryDir}`);
  process.exit(failed ? 1 : 0);
}

function makeCtx(run) {
  const ctx = {
    gwKey: GW_KEY,
    async script(s) {
      const r = await fetch(`${run.stubUrl}/_script`, { method: 'POST', body: JSON.stringify(s) });
      await r.text();
    },
    async upstream() {
      const r = await fetch(`${run.stubUrl}/_log`);
      return (await r.json()).map((e) => ({ ...e, body: Buffer.from(e.bodyB64, 'base64').toString('utf8') }));
    },
    // Client request; body objects are JSON-encoded. Returns {status, headers, text}.
    // Every call carries a fake OAuth bearer (as Claude Code does) unless bearer:false.
    async req(p, { method = 'POST', headers = {}, body, auth = 'bare', bearer = true, signal } = {}) {
      const h = { ...headers };
      if (auth === 'bare') h['x-gateway-key'] = GW_KEY;
      if (bearer && !('authorization' in h) && auth !== 'none') h.authorization = `Bearer ${OAUTH}`;
      if (body !== undefined && typeof body !== 'string') h['content-type'] ??= 'application/json';
      const r = await fetch(`${run.gwUrl}${p}`, { method, headers: h, body: body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body)), signal });
      const buf = Buffer.from(await r.arrayBuffer());
      return { status: r.status, headers: r.headers, text: buf.toString('utf8'), buf };
    },
    async stream(p, opts) {
      const h = { 'x-gateway-key': GW_KEY, authorization: `Bearer ${OAUTH}`, 'content-type': 'application/json', ...(opts.headers || {}) };
      return fetch(`${run.gwUrl}${p}`, { method: 'POST', headers: h, body: JSON.stringify(opts.body), signal: opts.signal });
    },
    sleep,
  };
  return ctx;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) main();
