// http.mjs — the HTTP surface: auth, routing, body reading, health.
//
// The route table is small and closed: `/healthz`, then `/<persona>/api/hello`,
// `/<persona>/v1/messages` and `/<persona>/v1/messages/count_tokens`. Everything else is an
// Anthropic-shaped 404. Two orderings here are deliberate:
//
//   * auth runs BEFORE body parsing — the shared key is the boundary for bridge peers, so an
//     unauthenticated peer never gets to stream 32 MiB at us;
//   * the query string is ignored (`?beta=true` from the SDK is not a different route).
//
// `/healthz` is bind-aware: a missing extra bind is a lost capability (the router container
// reaches this process only through the docker0 bind), so it degrades health exactly as a
// degraded persona does, and the expected/bound/missing lists are published so the verdict is
// checkable rather than asserted.
import { SidecarError, asSidecarError } from "./errors.mjs";
import { BODY_LIMIT, VERSION } from "./config.mjs";
import { redactHeaders } from "./logging.mjs";
import { safeEqual, presentedKey, countTokensEstimate } from "./protocol.mjs";
import { sendJson, sendError } from "./sinks.mjs";
import { handleMessages } from "./messages.mjs";

export function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let n = 0;
    req.on("data", d => {
      n += d.length;
      if (n > BODY_LIMIT) { reject(new SidecarError(413, "invalid_request_error", "request body exceeds 32 MiB")); req.destroy(); return; }
      chunks.push(d);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", e => reject(asSidecarError(e)));
  });
}

// A request's identity is a DIGEST of everything that decides what the model would do
// with it — model id, resolved effort, system text, the tool list, and the whole parsed
// message array (every content byte, `is_error` included). Shape is not identity: the same
// message count echoing the same `tool_use_id`s with a *different* `tool_result.content` is
// a genuinely different request, and joining it to an in-flight turn (or answering it from
// a replayed outcome) would silently drop it and return a stale answer — acceptance-audit
// defects B1/B2, 2026-09-13. Only a byte-for-byte re-send of the execution-affecting request
// earns a JOIN or a replay; anything else supersedes.
//
// `canon` gives JSON.stringify a stable key order so two structurally identical bodies that
// differ only in property order digest the same (no dependency; arrays keep their order,

export function healthBody(ctx) {
  const personas = {};
  let degraded = false;
  for (const p of ctx.personas.values()) {
    personas[p.name] = { alive: p.alive, degraded: p.degraded };
    if (p.degraded) degraded = true;
  }
  // A bind shortfall is a lost capability (the router container reaches us only through
  // the docker0 bind), so it degrades health exactly like a degraded persona would. The
  // lists are exposed so the verdict is checkable rather than asserted.
  const expected = ctx.binds?.expected ?? [];
  const bound = ctx.binds?.bound ?? [];
  const missing = expected.filter(b => !bound.includes(b));
  if (missing.length) degraded = true;
  return { status: degraded ? "degraded" : "ok", version: VERSION, personas, binds: { expected, bound, missing }, degraded };
}

export function makeHandler(ctx) {
  return async (req, res) => {
    const url = new URL(req.url || "/", "http://sidecar.invalid");
    const pathname = url.pathname;          // the query string (?beta=true) is ignored
    try {
      // auth runs before body parsing: the shared key is the boundary for bridge peers
      if (!safeEqual(presentedKey(req.headers), ctx.config.key)) {
        ctx.logger.write("auth-failed", { path: pathname, method: req.method, headers: redactHeaders(req.headers) });
        req.resume();
        sendError(res, new SidecarError(401, "authentication_error", "missing or invalid sidecar key"));
        return;
      }

      if (pathname === "/healthz") {
        req.resume();
        const body = healthBody(ctx);
        sendJson(res, body.degraded ? 503 : 200, body);
        return;
      }

      const seg = pathname.split("/").filter(Boolean);
      const persona = seg.length ? ctx.personas.get(seg[0]) : null;
      const rest = `/${seg.slice(1).join("/")}`;
      if (!persona) {
        req.resume();
        sendError(res, new SidecarError(404, "not_found_error", `unknown persona in path ${pathname}`));
        return;
      }

      if (rest === "/api/hello" && (req.method === "GET" || req.method === "HEAD")) {
        req.resume();
        ctx.logger.write("hello", { persona: persona.name, method: req.method });
        res.writeHead(200, { "content-type": "application/json" });
        res.end(req.method === "HEAD" ? undefined : "{}");
        return;
      }

      if (req.method !== "POST" || (rest !== "/v1/messages" && rest !== "/v1/messages/count_tokens")) {
        req.resume();
        sendError(res, new SidecarError(404, "not_found_error", `unsupported path ${pathname}`));
        return;
      }

      const raw = await readBody(req);
      let body;
      try { body = JSON.parse(raw); } catch { throw new SidecarError(400, "invalid_request_error", "request body is not valid JSON"); }

      if (rest === "/v1/messages/count_tokens") {
        const n = countTokensEstimate(body);
        ctx.logger.write("count_tokens", { persona: persona.name, input_tokens: n });
        sendJson(res, 200, { input_tokens: n });
        return;
      }

      await handleMessages(ctx, persona, body, req, res);
    } catch (e) {
      const err = asSidecarError(e);
      ctx.logger.write("http-error", { path: pathname, status: err.status, type: err.anthropicType, message: err.message });
      if (!res.headersSent) sendError(res, err);
      else if (!res.writableEnded) res.end();
    }
  };
}

