// Stub upstream for the differential harness (router/test/diff/README.md). Node stdlib only.
// Every request under /p/<provider>/... is recorded and answered from the script the runner
// installed for that provider (POST /_script). Control routes: /_script, /_log, /_reset.
import http from 'node:http';

const port = Number(process.argv[2] || process.env.STUB_PORT || 0);
const host = process.argv[3] || process.env.STUB_HOST || '127.0.0.1';
let scripts = {}; // provider -> spec | spec[] (consumed in order; last one sticks)
let log = [];
const t0 = Date.now();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function nextSpec(provider) {
  const s = scripts[provider];
  if (Array.isArray(s)) return s.length > 1 ? s.shift() : s[0];
  return s;
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks);
}

async function serveScripted(req, res, rec) {
  const spec = nextSpec(rec.provider) || { status: 599, body: 'rrdiff stub: no script for provider' };
  let closed = false;
  res.on('close', () => {
    closed = true;
    if (!res.writableFinished) rec.clientClosedAtMs = Date.now() - t0;
  });
  if (spec.destroy) {
    req.socket.destroy();
    return;
  }
  if (spec.headDelayMs) await sleep(spec.headDelayMs);
  if (closed) return;
  const headers = { ...(spec.headers || {}) };
  if (spec.chunks) {
    headers['content-type'] ??= 'text/event-stream';
    res.writeHead(spec.status || 200, headers);
    res.flushHeaders();
    for (const c of spec.chunks) {
      if (c.delayMs) await sleep(c.delayMs);
      if (closed) return;
      res.write(c.data);
    }
    rec.streamCompleted = true;
    res.end();
    return;
  }
  const body = spec.json !== undefined ? JSON.stringify(spec.json) : (spec.body ?? '');
  if (spec.json !== undefined) headers['content-type'] ??= 'application/json';
  res.writeHead(spec.status || 200, headers);
  res.end(body);
}

const server = http.createServer(async (req, res) => {
  try {
    const body = await readBody(req);
    if (req.url === '/_script' && req.method === 'POST') {
      scripts = JSON.parse(body.toString('utf8'));
      log = [];
      res.end('{"ok":true}');
      return;
    }
    if (req.url === '/_log') {
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify(log));
      return;
    }
    if (req.url === '/_reset') {
      scripts = {};
      log = [];
      res.end('{"ok":true}');
      return;
    }
    const m = /^\/p\/([^/?]+)(.*)$/.exec(req.url || '');
    const rec = {
      provider: m ? m[1] : null,
      method: req.method,
      url: m ? m[2] : req.url,
      headers: req.headers,
      bodyB64: body.toString('base64'),
      bodyLength: body.length,
      receivedAtMs: Date.now() - t0,
    };
    log.push(rec);
    if (!m) {
      res.writeHead(404);
      res.end('rrdiff stub: unknown path');
      return;
    }
    await serveScripted(req, res, rec);
  } catch (err) {
    if (!res.headersSent) res.writeHead(598);
    res.end(String(err));
  }
});
server.requestTimeout = 0;
server.headersTimeout = 0;
server.keepAliveTimeout = 1000;
server.listen(port, host, () => {
  process.stdout.write(`rrdiff-stub listening ${JSON.stringify(server.address())}\n`);
});
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => process.exit(0));
