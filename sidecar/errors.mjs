// errors.mjs — the sidecar's error taxonomy and the upstream -> Anthropic mapping.
//
// One owner for "what does the client see when something fails": the SidecarError value
// itself, the CodexErrorInfo/TurnError translation, and the rate-limit reading that turns an
// account snapshot into a 429 with a retry-after. Nothing here touches HTTP, a session or a
// child process, so every consumer can depend on it without a cycle.

export class SidecarError extends Error {
  constructor(status, type, message, extra = {}) {
    super(message);
    this.status = status;
    this.anthropicType = type;
    this.extra = extra;
  }
  body() { return { type: "error", error: { type: this.anthropicType, message: this.message } }; }
}

export const asSidecarError = e => (e instanceof SidecarError
  ? e
  : new SidecarError(502, "api_error", String(e?.message || e || "sidecar failure")));

// CodexErrorInfo string variants (schema) -> Anthropic error shape.
const CODEX_ERROR_MAP = {
  unauthorized: [401, "authentication_error"],
  rateLimitExceeded: [429, "rate_limit_error"],
  usageLimitExceeded: [429, "rate_limit_error"],
  sessionBudgetExceeded: [429, "rate_limit_error"],
  serverOverloaded: [502, "api_error"],
};

// Map an app-server JSON-RPC error / TurnError onto an Anthropic error shape.
export function mapUpstreamError(err, ctxLabel = "app-server") {
  const obj = (err && typeof err === "object") ? err : {};
  const text = typeof err === "string" ? err : JSON.stringify(err ?? {});
  const msg = obj.message ?? obj.error?.message ?? text;
  const info = obj.codexErrorInfo ?? obj.error?.codexErrorInfo ?? obj.data?.codexErrorInfo ?? null;
  const infoKey = typeof info === "string" ? info : (info && typeof info === "object" ? Object.keys(info)[0] : null);
  if (infoKey && Object.hasOwn(CODEX_ERROR_MAP, infoKey)) {
    const [status, type] = CODEX_ERROR_MAP[infoKey];
    return new SidecarError(status, type, `${ctxLabel}: ${msg}`);
  }
  // object CodexErrorInfo variants forward the upstream HTTP status verbatim
  const httpStatus = infoKey && info && typeof info === "object" ? info[infoKey]?.httpStatusCode : null;
  if (httpStatus === 401 || httpStatus === 403) return new SidecarError(401, "authentication_error", `${ctxLabel}: ${msg}`);
  if (httpStatus === 429) return new SidecarError(429, "rate_limit_error", `${ctxLabel}: ${msg}`);
  if (/rate.?limit|usage limit|quota|too many requests/i.test(text)) {
    return new SidecarError(429, "rate_limit_error", `${ctxLabel}: ${msg}`);
  }
  if (/unauthori[sz]ed|authentication|not logged in|login required|credential|token refresh|invalid.*api.?key/i.test(text)) {
    return new SidecarError(401, "authentication_error", `${ctxLabel}: ${msg}`);
  }
  return new SidecarError(502, "api_error", `${ctxLabel}: ${msg}`);
}

// RateLimitWindow.resetsAt is an absolute int64 (schema); tolerate s or ms epochs.
export function retryAfterSeconds(resetsAt, now = Date.now()) {
  if (typeof resetsAt !== "number" || !Number.isFinite(resetsAt)) return null;
  const ms = resetsAt > 1e12 ? resetsAt : resetsAt * 1000;
  const secs = Math.ceil((ms - now) / 1000);
  return secs > 0 ? secs : 1;
}

// A RateLimitSnapshot is "exhausted" when a window is at/over 100 % or a reached-type is set.
export function rateLimitExhaustion(snapshot) {
  if (!snapshot || typeof snapshot !== "object") return null;
  const windows = [snapshot.primary, snapshot.secondary].filter(w => w && typeof w === "object");
  const full = windows.filter(w => typeof w.usedPercent === "number" && w.usedPercent >= 100);
  if (!full.length && !snapshot.rateLimitReachedType) return null;
  const resets = (full.length ? full : windows).map(w => retryAfterSeconds(w.resetsAt)).filter(n => n != null);
  return {
    reason: snapshot.rateLimitReachedType || "rate_limit_reached",
    retryAfter: resets.length ? Math.min(...resets) : null,
  };
}
