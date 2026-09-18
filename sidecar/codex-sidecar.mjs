#!/usr/bin/env node
// codex-sidecar.mjs — Anthropic Messages front end over `codex app-server` children.
//
// Claude Code (via the router) -> POST /<persona>/v1/messages -> a persistent
// `<persona> app-server` child driven over JSON-RPC on its stdio pipe.
//
// THIS FILE IS THE COMPOSITION ROOT, and nothing else: it reads the config, builds the
// process-wide `ctx`, constructs one Persona per configured name, binds the servers and owns
// shutdown. It is also the stable entry path — `install.sh` resolves it by name and the
// systemd unit is generated against it — so it does not move and does not grow features.
//
// The owners it composes, and the one thing each is responsible for:
//
//   config.mjs         the pinned inputs (preamble, thread config) and the env reading
//   errors.mjs         SidecarError and the upstream -> Anthropic error/rate-limit mapping
//   logging.mjs        the durable JSONL log and its redaction rule
//   protocol.mjs       the Anthropic wire boundary, as pure functions
//   sinks.mjs          the two response transports (buffer, SSE)
//   session.mjs        one thread's state and the outcome protocol over it
//   persona.mjs        the app-server child: stdio, JSON-RPC, thread bus, restart, boundary
//   thread-config.mjs  is the started thread the one we declared?
//   request.mjs        normalization, request identity, and THE replay|join|drive decision
//   turn.mjs           making the app-server do the work
//   messages.mjs       the request path as an explicit four-phase sequence
//   outbox-events.mjs  the tracker rows one completed turn produces
//   http.mjs           auth, routing, body reading, bind-aware health
//   outbox.mjs         the SQLite writer itself
//
// Invariants (plan: plans/codex-appserver-sidecar.md):
//   * the adapter never intermediates the codex credential — the child owns $CODEX_HOME;
//   * the security boundary is the runtime item-type filter, not thread config;
//   * one codex thread per (Claude Code `session_id`, agent scope) — a parent and its
//     subagents share a session id on the wire and must never share a thread;
//   * no dependency outside node:*.
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createOutboxWriter } from "./outbox.mjs";
import { VERSION, readConfig } from "./config.mjs";
import { Logger } from "./logging.mjs";
import { Persona } from "./persona.mjs";
import { makeHandler } from "./http.mjs";

// ------------------------------------------------------------------ boot ----

export async function startSidecar(env = process.env) {
  const config = readConfig(env);
  if (!config.key) throw new Error("SIDECAR_KEY is required");
  const logger = new Logger(config.logDir, config.logKeepDays);
  // One outbox writer per process; an unset `SIDECAR_OUTBOX_PATH` makes it a no-op that
  // opens nothing. `outboxSpawned` is the per-process set of subagent keys already announced.
  const outbox = createOutboxWriter({ path: config.outboxPath });
  const ctx = { config, logger, outbox, outboxSpawned: new Set(), personas: new Map() };
  for (const name of config.personas) ctx.personas.set(name, new Persona(name, ctx));

  const handler = makeHandler(ctx);
  const servers = [];
  const listen = (server, host, port) => new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => { server.off("error", reject); resolve(server.address().port); });
  });

  const primary = http.createServer(handler);
  const port = await listen(primary, "127.0.0.1", config.port);
  servers.push(primary);
  const bindOf = s => `${s.address().address}:${s.address().port}`;
  // Truth about which binds are up lives here and feeds /healthz (bind-aware health).
  const binds = { expected: [bindOf(primary)], bound: [bindOf(primary)] };
  ctx.binds = binds;

  // The extra bind (docker0 gateway) can legitimately fail at boot: a user unit cannot
  // order after the system docker.service, and until docker creates docker0 the address
  // does not exist (EADDRNOTAVAIL). The first attempt is inline so the happy path is
  // unchanged; a failure starts a bounded background retry while /healthz reports
  // degraded, so the shortfall is never silent. The primary loopback bind stays fail-fast.
  let bindRetryTimer = null;
  if (config.bindExtra) {
    const target = `${config.bindExtra}:${port}`;
    binds.expected.push(target);
    const tryExtra = async () => {
      const extra = http.createServer(handler);
      await listen(extra, config.bindExtra, port);
      servers.push(extra);
      binds.bound.push(bindOf(extra));
    };
    try {
      await tryExtra();
    } catch (first) {
      const startedAt = Date.now();
      let attempts = 1;
      let delay = Math.max(1, config.bindRetryMs);
      logger.write("bind-extra-retrying", {
        host: config.bindExtra, port, err: String(first?.message || first), budgetMs: config.bindRetryBudgetMs,
      });
      const schedule = () => {
        bindRetryTimer = setTimeout(async () => {
          bindRetryTimer = null;
          attempts += 1;
          try {
            await tryExtra();
            logger.write("bind-extra-recovered", { host: config.bindExtra, port, attempts, elapsedMs: Date.now() - startedAt });
          } catch (e) {
            if (Date.now() - startedAt + delay > config.bindRetryBudgetMs) {
              logger.write("bind-extra-failed", {
                host: config.bindExtra, port, attempts, elapsedMs: Date.now() - startedAt, err: String(e?.message || e),
              });
              return;
            }
            delay = Math.min(delay * 2, 5000);
            schedule();
          }
        }, delay);
        bindRetryTimer.unref?.();
      };
      schedule();
    }
  }

  logger.write("listen", {
    port, binds: binds.bound, expected: binds.expected,
    personas: [...ctx.personas.keys()], logDir: config.logDir, keepDays: config.logKeepDays,
  });

  const close = async () => {
    if (bindRetryTimer) { clearTimeout(bindRetryTimer); bindRetryTimer = null; }
    for (const p of ctx.personas.values()) p.stop();
    await Promise.all(servers.map(s => new Promise(resolve => {
      s.close(resolve);
      // server.close() on its own waits forever on idle keep-alive sockets, which
      // would hang `systemctl stop`: let in-flight requests land, then cut the rest.
      try { s.closeIdleConnections(); } catch { /* ignore */ }
      const t = setTimeout(() => { try { s.closeAllConnections(); } catch { /* ignore */ } }, 250);
      t.unref?.();
    })));
    logger.write("shutdown", {});
    logger.close();
    outbox.close();
  };
  return { ctx, config, logger, outbox, port, servers, close, personas: ctx.personas };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  startSidecar().then(h => {
    process.stdout.write(`codex-sidecar ${VERSION} listening on 127.0.0.1:${h.port} personas=${[...h.personas.keys()].join(",")}\n`);
    let stopping = false;
    const bye = () => {
      if (stopping) process.exit(0);
      stopping = true;
      setTimeout(() => process.exit(0), 5000);   // a hung shutdown still exits
      h.close().finally(() => process.exit(0));
    };
    process.on("SIGTERM", bye);
    process.on("SIGINT", bye);
  }).catch(e => {
    process.stderr.write(`codex-sidecar: ${e?.message || e}\n`);
    process.exit(1);
  });
}
