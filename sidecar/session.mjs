// session.mjs — one codex thread's state, and the outcome protocol over it.
//
// A Session owns everything that persists across the requests of one (session_id, agent
// scope): the thread and turn ids, the parked tool calls and the ids already delivered, the
// codex token figures, the attached sink, and the request currently driving it together with
// that request's joiners and replayable last outcome.
//
// The outcome protocol is the contract with the request path: `waitOutcome` parks one awaiting
// request, `settle` queues an outcome, `drain` hands the next one over. Outcomes QUEUE in order
// (a tool-call batch settled while nothing is attached must not drop the next), except an
// error, which replaces whatever is queued. Every app-server frame for this thread lands in
// `onFrame` and becomes either text, a parked call, a usage reading or an outcome.
import { SidecarError, mapUpstreamError } from "./errors.mjs";
import { INBAND_ITEM_TYPES } from "./config.mjs";
import { ZERO_USAGE } from "./protocol.mjs";

export class Session {
  constructor(persona, key) {
    this.persona = persona;
    this.key = key;
    this.threadId = null;
    this.turnId = null;
    this.model = null;
    this.lastEffort = null;       // effort sent on this thread's previous turn/start (effort-changed)
    this.parked = new Map();      // callId -> JSON-RPC request id
    this.delivered = new Set();   // callIds already emitted to the client as tool_use blocks
    this.turnOpen = false;        // the codex turn is still running, blocked on a tool call
    this.batch = [];              // tool calls collected inside the batching window
    this.batchTimer = null;
    this.msgCount = 0;
    this.hist = [];               // undecorated per-message digests of the last accepted request
    this.deltaItems = new Set();  // item ids already streamed as deltas
    this.usageLast = null;        // previous call's tokenUsage.last (the lag)
    this.usageTotal = null;       // codex-side cumulative for this thread
    this.contextWindow = null;    // ThreadTokenUsage.modelContextWindow, when the app-server reports one
    this.outboxKey = null;        // {session_id, agent_id} for the tracker outbox; null = skip
    this.sink = null;
    this.owner = null;            // the request currently driving this session (a request token)
    this.ownerGone = false;       // the owner's socket closed but a joiner keeps the turn alive
    this.joiners = new Set();     // duplicate requests attached to the owner's turn (WP-5)
    this.turnText = [];           // text emitted to the owner's sink since this request attached
    this.lastOutcome = null;      // the last completed request's outcome, for straggler replay
    this.pendingOutcomes = [];    // settled outcomes not yet handed to a request, in order
    this.resolveOutcome = null;
    this.timeoutTimer = null;
    this.closed = false;
  }

  attach(sink) { this.sink = sink; }
  detach(sink) { if (!sink || this.sink === sink) this.sink = null; }

  // The joiners bound to one owner (its request token, by identity). `joiners` is
  // session-global, so every read filters: a request only ever writes to, drains, promotes
  // or settles the duplicates that joined *it* (audit defect B3, 2026-09-13).
  joinersOf(owner) { return owner ? [...this.joiners].filter(j => j.ownerToken === owner) : []; }

  // Every byte of assistant text goes through here: to the owner's sink, to every
  // duplicate joined to *the current owner*, and into `turnText` so a joiner attaching
  // mid-stream can be replayed the text it missed. A promoted joiner is both `sink` and a
  // member of `joiners`, so it is written exactly once.
  emitText(t) {
    if (!t) return;
    this.turnText.push(t);
    this.sink?.textDelta(t);
    for (const j of this.joinersOf(this.owner)) if (j.sink !== this.sink) j.sink.textDelta(t);
  }

  // A newer request on the same session takes over: the request parked in
  // waitOutcome is resolved with the given error (its timer is cleared by the
  // resolver) instead of being silently orphaned with its slot overwritten.
  preempt(error) { this.resolveOutcome?.({ kind: "error", error }); }

  // Outcome protocol: the HTTP handler awaits one outcome per request.
  waitOutcome(timeoutMs) {
    return new Promise(resolve => {
      this.resolveOutcome = o => {
        if (!this.resolveOutcome) return;
        this.resolveOutcome = null;
        if (this.timeoutTimer) { clearTimeout(this.timeoutTimer); this.timeoutTimer = null; }
        resolve(o);
      };
      this.timeoutTimer = setTimeout(() => {
        this.resolveOutcome?.({ kind: "error", error: new SidecarError(502, "api_error", "timed out waiting for the codex app-server") });
      }, timeoutMs);
      this.timeoutTimer.unref?.();
      this.drain();
    });
  }

  // Outcomes queue in order (a tool-call batch settled while no request is attached
  // must not drop the next one — each parked call has to be delivered exactly once);
  // an error replaces whatever is queued.
  settle(outcome) {
    if (outcome.kind === "error") this.pendingOutcomes = [outcome];
    else this.pendingOutcomes.push(outcome);
    this.drain();
  }

  drain() {
    if (!this.pendingOutcomes.length || !this.resolveOutcome) return;
    this.resolveOutcome(this.pendingOutcomes.shift());
  }

  #armBatch() {
    if (this.batchTimer) clearTimeout(this.batchTimer);
    this.batchTimer = setTimeout(() => {
      this.batchTimer = null;
      if (!this.batch.length) return;
      const batch = this.batch;
      this.batch = [];
      this.settle({ kind: "tool_use", batch });
    }, this.persona.ctx.config.batchMs);
    this.batchTimer.unref?.();
  }

  // Every frame the app-server sends for this thread lands here.
  onFrame(ev) {
    if (this.closed) return;
    if (ev.type === "child-exit") { this.settle({ kind: "error", error: ev.error }); return; }
    if (ev.type === "toolcall") {
      const p = ev.rpc.params || {};
      this.turnId = p.turnId || this.turnId;
      this.parked.set(p.callId, ev.rpc.id);
      this.batch.push(ev.rpc);
      this.#armBatch();
      return;
    }
    const m = ev.frame;
    const p = m.params || {};
    switch (m.method) {
      case "turn/started":
        this.turnId = p.turn?.id || this.turnId;
        break;
      case "item/started": {
        const type = p.item?.type;
        this.turnId = p.turnId || this.turnId;
        if (type && !INBAND_ITEM_TYPES.has(type)) { this.persona.tripBoundary(type, this); return; }
        break;
      }
      case "item/agentMessage/delta":
        if (typeof p.delta === "string" && p.delta) {
          if (p.itemId) this.deltaItems.add(p.itemId);
          this.emitText(p.delta);
        }
        break;
      case "item/completed": {
        const item = p.item;
        if (item?.type === "agentMessage" && typeof item.text === "string" && item.text && !this.deltaItems.has(item.id)) {
          this.emitText(item.text);          // buffered fallback when no deltas arrived
        }
        break;
      }
      case "thread/tokenUsage/updated":
        if (p.tokenUsage) {
          this.usageLast = p.tokenUsage.last ?? this.usageLast;
          this.usageTotal = p.tokenUsage.total ?? this.usageTotal;
          this.contextWindow = p.tokenUsage.modelContextWindow ?? this.contextWindow;
        }
        break;
      case "hook/started":
      case "mcpServer/startupStatus/updated":
        this.persona.log("config-unverified", {
          persona: this.persona.name, threadId: this.threadId, stage: "runtime", method: m.method,
        });
        this.settle({ kind: "error", error: new SidecarError(502, "api_error", `thread config not honoured: unexpected ${m.method}`) });
        break;
      case "error":
        this.settle({ kind: "error", error: mapUpstreamError(p.error ?? p, "app-server error") });
        break;
      case "turn/completed": {
        const turn = p.turn || {};
        this.turnId = turn.id || this.turnId;
        // the schema puts tokenUsage on thread/tokenUsage/updated only; tolerate it here too
        if (p.tokenUsage) {
          this.usageLast = p.tokenUsage.last ?? this.usageLast;
          this.usageTotal = p.tokenUsage.total ?? this.usageTotal;
          this.contextWindow = p.tokenUsage.modelContextWindow ?? this.contextWindow;
        }
        if (turn.status === "failed" || turn.error) {
          this.settle({ kind: "error", error: mapUpstreamError(turn.error ?? { message: `turn ${turn.status}` }, "turn failed") });
          return;
        }
        if (this.batchTimer) return;   // a tool-call batch is still settling
        this.settle({ kind: "end_turn" });
        break;
      }
      default:
        break;
    }
  }

  // The OCCUPANCY of this turn, in RAW CODEX UNITS: the latest `tokenUsage.last` codex
  // reported, never a cumulative figure and never a delta of one. Claude Code sums the three
  // input columns of a message's `usage` as "how full is my context", so anything cumulative
  // on the wire inflates it without bound — measured 2026-09-14: ten code-mode `wait` pollings
  // between two client messages re-billed a ~107k cached prompt each time and the old
  // `sent`-ledger delta reported 1,076,389 for a codex context that never left 109k.
  // Cumulative spend leaves this process by two other doors only: the outbox `kind: "usage"`
  // row (`usageTotal`, the tracker's figure) and the `http` row's `usageTotalCodex`.
  reconcile() {
    const target = this.usageLast || ZERO_USAGE;
    const out = {};
    for (const k of Object.keys(ZERO_USAGE)) out[k] = Math.max(0, target[k] ?? 0);
    return out;
  }

  // The lagged preview carried by message_start (previous call's tokenUsage.last) — the same
  // occupancy quantity as reconcile(), one call behind. It is a preview only: Anthropic's
  // message_delta finalises the message's usage.
  lagUsage() { return this.usageLast || ZERO_USAGE; }

  answerParked(callId, text, success) {
    const id = this.parked.get(callId);
    if (id === undefined) return false;
    this.parked.delete(callId);
    this.persona.send({ jsonrpc: "2.0", id, result: { contentItems: [{ type: "inputText", text: text || "(empty)" }], success: !!success } });
    return true;
  }

  // End-of-request cleanup (Defect E1). Only a call the client has already been
  // shown can be "unanswered": a call parked after the last response was written has
  // never crossed the request boundary, so it must survive to be delivered on the
  // next one instead of being answered with a refusal the model then has to absorb.
  abandonDelivered(reason) {
    for (const callId of [...this.parked.keys()]) {
      if (this.delivered.has(callId)) this.answerParked(callId, reason, false);
    }
  }

  abandonParked(reason) {
    for (const callId of [...this.parked.keys()]) this.answerParked(callId, reason, false);
    this.batch = [];
    if (this.batchTimer) { clearTimeout(this.batchTimer); this.batchTimer = null; }
  }

  close(reason) {
    if (this.closed) return;
    this.closed = true;
    this.abandonParked(reason || "aborted");
    // A request parked in waitOutcome must not outlive its session (it would hang until the
    // turn timeout): settle it with a retryable 409 through the resolver, which also clears
    // the timer, so any later settle stays a no-op.
    this.resolveOutcome?.({ kind: "error", error: new SidecarError(409, "api_error", `session closed: ${reason || "aborted"}`) });
    if (this.timeoutTimer) { clearTimeout(this.timeoutTimer); this.timeoutTimer = null; }
    if (this.threadId) this.persona.bus.delete(this.threadId);
  }
}
