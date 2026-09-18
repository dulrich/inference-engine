// sinks.mjs — the two response transports, and the message envelope they share.
//
// A sink is the ONLY thing that writes to a client socket. Both expose the same five calls —
// `start`, `textDelta`, `toolUse`, `finish`, `error` (plus `abort`) — so the request path emits
// one outcome without knowing whether the client asked for `stream:true`. `BufferSink` answers
// with one Messages JSON body; `SseSink` streams real Anthropic SSE with a silence ping.
//
// The pre-header frame queue in `SseSink` is load-bearing and is documented at `#raw`: the
// app-server routinely puts the `turn/start` reply and the turn's first delta in one stdout
// chunk, so a sink can be written to before its headers exist.
import crypto from "node:crypto";
import { usageOut } from "./protocol.mjs";

// The two one-shot writers, for the replies that never open a message: a bare JSON body and
// an Anthropic-shaped error (with its `retry-after` when the upstream gave us one).
export function sendJson(res, status, obj, headers = {}) {
  res.writeHead(status, { "content-type": "application/json", ...headers });
  res.end(JSON.stringify(obj));
}

export function sendError(res, err) {
  const headers = {};
  if (err.extra?.retryAfter != null) headers["retry-after"] = String(err.extra.retryAfter);
  sendJson(res, err.status, err.body(), headers);
}

// The id the client selected, restored for the wire: the router strips its provider prefix
// (`codex-b/gpt-5.6-sol` reaches us as `gpt-5.6-sol`, router/AGENTS.md), and Claude Code
// persists the response envelope's `model` per turn — a bare name there means `--resume` cannot
// match the session's model to a picker row ("could not be restored", measured 2026-09-14). The
// persona IS the provider prefix, so re-prefixing is exact; an already-prefixed id passes through.
export function wireModelId(personaName, requested) {
  const id = String(requested ?? "");
  return id.includes("/") ? id : `${personaName}/${id}`;
}

export function messageSkeleton(model) {
  return {
    id: `msg_${crypto.randomBytes(12).toString("hex")}`,
    type: "message",
    role: "assistant",
    model,
    content: [],
    stop_reason: null,
    stop_sequence: null,
  };
}

// Buffers the whole message and answers with one Messages JSON body (stream:false).
export class BufferSink {
  constructor(res, model) {
    this.res = res;
    this.msg = messageSkeleton(model);
    this.text = "";
    this.blocks = [];
    this.started = false;
    this.done = false;
  }
  start() { this.started = true; }
  abort() { this.done = true; }
  textDelta(t) { if (t) this.text += t; }
  toolUse(block) { this.#flushText(); this.blocks.push(block); }
  #flushText() { if (this.text) { this.blocks.push({ type: "text", text: this.text }); this.text = ""; } }
  finish(stopReason, usage) {
    if (this.done) return;
    this.done = true;
    this.#flushText();
    if (!this.blocks.length) this.blocks.push({ type: "text", text: "(no text)" });
    const body = { ...this.msg, content: this.blocks, stop_reason: stopReason, usage: usageOut(usage) };
    this.res.writeHead(200, { "content-type": "application/json" });
    this.res.end(JSON.stringify(body));
  }
  error(err) {
    if (this.done) return;
    this.done = true;
    const headers = { "content-type": "application/json" };
    if (err.extra?.retryAfter != null) headers["retry-after"] = String(err.extra.retryAfter);
    this.res.writeHead(err.status, headers);
    this.res.end(JSON.stringify(err.body()));
  }
}

// Real SSE: message_start as soon as the turn is running, deltas as they arrive,
// `event: ping` after every `pingMs` of silence (Claude Code's 300 s watchdog).
export class SseSink {
  constructor(res, model, pingMs) {
    this.res = res;
    this.msg = messageSkeleton(model);
    this.pingMs = pingMs;
    this.index = 0;
    this.textOpen = false;
    this.anyBlock = false;
    this.started = false;
    this.done = false;
    this.timer = null;
    this.preStart = [];
  }
  #raw(text) {
    if (this.done || this.res.writableEnded || this.res.destroyed) return false;
    // Nothing may reach the socket before `start()` has written the headers. The app-server
    // routinely puts the `turn/start` response and the turn's first `item/agentMessage/delta`
    // in one stdout chunk; the frame loop dispatches both synchronously, so the delta reaches
    // this sink before the awaiting handler's continuation calls `start()`. A raw write there
    // sends implicit headers, `start()`'s `writeHead` then throws ERR_HTTP_HEADERS_SENT, and
    // the turn dies as an `event: error` with no `message_start` and no armed ping timer.
    if (!this.started) { this.preStart.push(text); return true; }
    try { this.res.write(text); } catch { this.abort(); return false; }
    return true;
  }
  #write(type, obj) {
    if (this.#raw(`event: ${type}\ndata: ${JSON.stringify(obj)}\n\n`)) this.#arm();
  }
  #arm() {
    if (this.timer) clearTimeout(this.timer);
    if (this.done || !this.started) return;
    this.timer = setTimeout(() => {
      if (this.#raw(`event: ping\ndata: ${JSON.stringify({ type: "ping" })}\n\n`)) this.#arm();
    }, this.pingMs);
    this.timer.unref?.();
  }
  abort() {
    this.done = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
  }
  start(usage) {
    if (this.started) return;
    this.started = true;
    this.res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    this.#write("message_start", { type: "message_start", message: { ...this.msg, usage: usageOut(usage) } });
    const early = this.preStart.splice(0);   // frames that raced ahead of the headers, in order
    for (const text of early) if (!this.#raw(text)) return;
    if (early.length) this.#arm();
  }
  textDelta(t) {
    if (!t || this.done) return;
    if (!this.textOpen) {
      this.#write("content_block_start", { type: "content_block_start", index: this.index, content_block: { type: "text", text: "" } });
      this.textOpen = true;
      this.anyBlock = true;
    }
    this.#write("content_block_delta", { type: "content_block_delta", index: this.index, delta: { type: "text_delta", text: t } });
  }
  #closeText() {
    if (!this.textOpen) return;
    this.#write("content_block_stop", { type: "content_block_stop", index: this.index });
    this.textOpen = false;
    this.index += 1;
  }
  toolUse(block) {
    if (this.done) return;
    this.#closeText();
    this.#write("content_block_start", { type: "content_block_start", index: this.index, content_block: { type: "tool_use", id: block.id, name: block.name, input: {} } });
    this.#write("content_block_delta", { type: "content_block_delta", index: this.index, delta: { type: "input_json_delta", partial_json: JSON.stringify(block.input ?? {}) } });
    this.#write("content_block_stop", { type: "content_block_stop", index: this.index });
    this.index += 1;
    this.anyBlock = true;
  }
  finish(stopReason, usage) {
    if (this.done) return;
    if (!this.anyBlock) this.textDelta("(no text)");
    this.#closeText();
    this.#write("message_delta", { type: "message_delta", delta: { stop_reason: stopReason, stop_sequence: null }, usage: usageOut(usage) });
    this.#write("message_stop", { type: "message_stop" });
    this.done = true;
    if (this.timer) clearTimeout(this.timer);
    if (!this.res.writableEnded) this.res.end();
  }
  // Before message_start an error is an HTTP status; after it, an `event: error` frame.
  error(err) {
    if (this.done) return;
    this.done = true;
    if (this.timer) clearTimeout(this.timer);
    if (!this.started) {
      const headers = { "content-type": "application/json" };
      if (err.extra?.retryAfter != null) headers["retry-after"] = String(err.extra.retryAfter);
      this.res.writeHead(err.status, headers);
      this.res.end(JSON.stringify(err.body()));
      return;
    }
    if (!this.res.writableEnded && !this.res.destroyed) {
      try {
        this.res.write(`event: error\ndata: ${JSON.stringify(err.body())}\n\n`);
        this.res.end();
      } catch { /* the client is gone */ }
    }
  }
}
