import test from "node:test";
import assert from "node:assert/strict";
import { withSidecar, req, msgBody, SIMPLE_TURN, sentTo } from "./helpers.mjs";

const scenario = { defaultTurn: SIMPLE_TURN };

test("the shared key is the boundary: 401 without it and with a wrong one", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const none = await req(h, "/healthz", { method: "GET", key: null });
  assert.equal(none.status, 401);
  assert.equal(none.json.error.type, "authentication_error");

  const wrong = await req(h, "/healthz", { method: "GET", key: "nope" });
  assert.equal(wrong.status, 401);

  // /api/hello is authenticated too — the key, not the bind, is the boundary
  const hello = await req(h, "/codex-a/api/hello", { method: "GET", key: null });
  assert.equal(hello.status, 401);

  // the key is never logged
  const dump = JSON.stringify(h.log());
  assert.ok(!dump.includes("test-key"), "the sidecar key must never reach the log");
  assert.ok(dump.includes("<redacted>"), "auth headers are redacted in the log");
});

test("both header forms are accepted and /healthz reports every persona", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const bearer = await req(h, "/healthz", { method: "GET" });
  assert.equal(bearer.status, 200);
  assert.deepEqual(Object.keys(bearer.json.personas).sort(), ["codex-b", "codex-a"]);
  assert.equal(bearer.json.degraded, false);

  const apiKey = await req(h, "/healthz", { method: "GET", keyHeader: "x-api-key" });
  assert.equal(apiKey.status, 200);
});

test("?beta=true is tolerated and both persona routes drive their own child", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const david = await req(h, "/codex-a/v1/messages?beta=true", { body: msgBody({}) });
  assert.equal(david.status, 200, david.text);
  assert.equal(david.json.content[0].text, "hello world");

  const b = await req(h, "/codex-b/v1/messages", { body: msgBody({ model: "gpt-5.6-sol" }) });
  assert.equal(b.status, 200, b.text);

  assert.equal(sentTo(h.trace("codex-a"), "thread/start").length, 1);
  assert.equal(sentTo(h.trace("codex-b"), "thread/start").length, 1);
});

test("unknown persona and unsupported paths are Anthropic-shaped 404s", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const persona = await req(h, "/codex-nobody/v1/messages", { body: msgBody({}) });
  assert.equal(persona.status, 404);
  assert.equal(persona.json.error.type, "not_found_error");

  const route = await req(h, "/codex-a/v1/complete", { body: {} });
  assert.equal(route.status, 404);
});

test("count_tokens estimates and api/hello answers 200", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const ct = await req(h, "/codex-a/v1/messages/count_tokens", { body: msgBody({ messages: [{ role: "user", content: "abcdefgh" }] }) });
  assert.equal(ct.status, 200);
  assert.ok(Number.isInteger(ct.json.input_tokens) && ct.json.input_tokens > 0);

  for (const method of ["GET", "HEAD"]) {
    const hello = await req(h, "/codex-a/api/hello", { method });
    assert.equal(hello.status, 200);
  }
  // no thread was opened by either probe
  assert.equal(sentTo(h.trace("codex-a"), "thread/start").length, 0);
});

test("unknown model suffixes are 400s and image blocks are refused", async t => {
  const h = await withSidecar({ scenario });
  t.after(() => h.close());

  const bad = await req(h, "/codex-a/v1/messages", { body: msgBody({ model: "gpt-5.6-luna-ultra" }) });
  assert.equal(bad.status, 400);
  assert.equal(bad.json.error.type, "invalid_request_error");

  const img = await req(h, "/codex-a/v1/messages", {
    body: msgBody({ messages: [{ role: "user", content: [{ type: "image", source: { type: "base64", data: "x" } }] }] }),
  });
  assert.equal(img.status, 400);
  assert.match(img.json.error.message, /images unsupported/);

  const badJson = await fetch(`http://127.0.0.1:${h.port}/codex-a/v1/messages`, {
    method: "POST", headers: { authorization: `Bearer ${h.key}` }, body: "{not json",
  });
  assert.equal(badJson.status, 400);
});
