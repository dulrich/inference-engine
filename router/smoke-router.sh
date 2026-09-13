#!/usr/bin/env bash
# router/smoke-router.sh — probe an ai-gateway instance (the WP-1 transient build, or the
# resident :4813 service) by response body, never status code alone (repo quirk: "a healthy
# service has told you nothing" — llvmpipe-style, HTTP-200-but-wrong-answer failures are the
# family this guards against).
#
# Usage:
#   router/smoke-router.sh <base-url> [<gateway-key>] [--subscription] [--codex]
#
#   <base-url>      mandatory, e.g. http://127.0.0.1:4813 (resident) or a transient
#                   http://127.0.0.1:470x (WP-1 scratch-decade run).
#   <gateway-key>   the x-gateway-key auth value for the local checks below. Falls back to
#                   $GATEWAY_KEY if omitted. Required for checks 1, 2 and 4 to pass (check 3
#                   deliberately omits/misconfigures it).
#   --subscription  default OFF, and this script NEVER passes it to itself. Without it, the
#                   hosted-leg check is skipped and only PRINTS what it would run — no request
#                   ever reaches api.anthropic.com. Passing it runs one `claude -p` call through
#                   this gateway on the operator's saved claude.ai login, which bills the real
#                   subscription. This is WP-3's held-for-user step
#                   (docs/tool-calling-gateway/oauth-passthrough-probe.md) — WP-1 implements the
#                   code path but never exercises it.
#
# Checks (all by response body):
#   1. GET /v1/models lists both the claude-local/* ids (including the four qwen38 reasoning-
#      effort ids — still listed, no longer picker rows) and the hosted anthropic/* ids.
#   2. POST /v1/messages via claude-local/qwen38-27b-iq3s with thinking:{"type":"disabled"}
#      returns 2xx and NO thinking content block — the deterministic half of the router's
#      effort mapping (patch 0007). The low-vs-medium token-delta gate is prompt-dependent and
#      lives only in router/test/effort-mapping.test.mjs, never here.
#   3. A request with a missing gateway key, and one with a wrong gateway key, each return a
#      401 error body (asserted on the body's error message, not just the status).
#   4. One non-streamed chat completion via claude-local/qwen36-a3b returns non-empty content.
#   5. POST /v1/messages/count_tokens via claude-local/qwen36-a3b returns an integer
#      input_tokens field (local leg; patch 0004).
#   6. POST /v1/messages/count_tokens with an unresolvable model returns a 404-class JSON
#      error body (patch 0004).
#   7. (--subscription only) one claude -p call through the gateway returns a completion.
#   8. (--codex only, metered: one luna turn on the operator's Codex subscription) one
#      non-streamed codex-a/gpt-5.6-luna completion via /v1/messages returns
#      usage.output_tokens > 0 — proves the host sidecar leg (plans/codex-appserver-sidecar.md)
#      end to end through the router: extra_hosts, the shared CODEX_SIDECAR_KEY, and the
#      engine's baseurl-with-path derivation. (Not input_tokens: since 2026-09-13 the sidecar
#      emits Anthropic usage semantics, so a fully cached turn legitimately reports
#      input_tokens 0 with the prompt in cache_read_input_tokens.)
#   9. POST /v1/messages/count_tokens via codex-a/gpt-5.6-luna returns an integer
#      input_tokens (unmetered — the sidecar answers locally with a chars/4 estimate).
#
# Exits non-zero on any check failure. Requires: curl, jq.
set -u

SUBSCRIPTION=0
CODEX=0
BASE_URL=""
GATEWAY_KEY="${GATEWAY_KEY:-}"

for arg in "$@"; do
	case "$arg" in
	--subscription)
		SUBSCRIPTION=1
		;;
	--codex)
		CODEX=1
		;;
	*)
		if [ -z "$BASE_URL" ]; then
			BASE_URL="$arg"
		elif [ -z "$GATEWAY_KEY" ]; then
			GATEWAY_KEY="$arg"
		fi
		;;
	esac
done

if [ -z "$BASE_URL" ]; then
	echo "usage: $0 <base-url> [<gateway-key>] [--subscription] [--codex]" >&2
	exit 2
fi

BASE_URL="${BASE_URL%/}"

for bin in curl jq; do
	if ! command -v "$bin" >/dev/null 2>&1; then
		echo "[smoke] FATAL: '$bin' is required but not on PATH." >&2
		exit 2
	fi
done

fail=0
note() { echo "[smoke] $*"; }
pass() { echo "[smoke] PASS: $*"; }
bad() {
	echo "[smoke] FAIL: $*" >&2
	fail=1
}

if [ -z "$GATEWAY_KEY" ]; then
	note "WARNING: no gateway key given (arg or \$GATEWAY_KEY) — checks 1, 2 and 4 will fail authentication."
fi

# ── 1. GET /v1/models lists both legs ───────────────────────────────────────────────────────
note "checking GET /v1/models ..."
models_json=$(curl -s --max-time 10 -H "x-gateway-key: $GATEWAY_KEY" "$BASE_URL/v1/models") || models_json=""
model_ids=$(echo "$models_json" | jq -r '.data[]?.id // empty' 2>/dev/null)
models_local_ok=1
# The four qwen38-27b-iq3s-{off,low,medium,high} ids are the local leg's server-enforced
# hard-cap surface (plans/local-leg-claude-code-serving-fit.md WP-2; retagged router-internal by
# plans/router-effort-mapping-claude-local.md WP-3 — still listed here, no longer picker rows):
# each is a llama-swap alias whose own cmd carries the reasoning flags, so an id missing here
# means llama/config.yaml and router/gateway.config.json have drifted apart and a caller naming
# that id 400s at model_resolution.
for id in \
	claude-local/qwen36-a3b \
	claude-local/qwen38-27b-iq3s \
	claude-local/qwen38-27b-iq3s-off \
	claude-local/qwen38-27b-iq3s-low \
	claude-local/qwen38-27b-iq3s-medium \
	claude-local/qwen38-27b-iq3s-high \
	claude-local/nemotron-4b; do
	if ! echo "$model_ids" | grep -qx "$id"; then
		models_local_ok=0
	fi
done
models_hosted_ok=1
for id in anthropic/claude-sonnet-5 anthropic/claude-opus-5 anthropic/claude-fable-5 anthropic/claude-fable-5-1 anthropic/claude-haiku-4-5 anthropic/claude-haiku-4-5-20251001; do
	if ! echo "$model_ids" | grep -qx "$id"; then
		models_hosted_ok=0
	fi
done
models_codex_ok=1
for id in codex-a/gpt-5.6-luna codex-a/gpt-5.6-sol codex-a/gpt-6-astra codex-b/gpt-5.6-luna codex-b/gpt-5.6-sol codex-b/gpt-6-astra; do
	if ! echo "$model_ids" | grep -qx "$id"; then
		models_codex_ok=0
	fi
done
if [ "$models_local_ok" -eq 1 ] && [ "$models_hosted_ok" -eq 1 ] && [ "$models_codex_ok" -eq 1 ]; then
	pass "/v1/models lists all claude-local/*, anthropic/* and codex-{david,b}/* ids"
else
	bad "/v1/models missing expected ids (local ok=$models_local_ok, hosted ok=$models_hosted_ok, codex ok=$models_codex_ok). Got: $(echo "$model_ids" | tr '\n' ',')"
fi

# ── 2. Effort mapping, deterministic half: thinking disabled ⇒ no thinking block ────────────
# Patch 0007 maps the harness's own effort control onto chat_template_kwargs on the upstream body
# (plans/router-effort-mapping-claude-local.md): thinking:{"type":"disabled"} becomes
# enable_thinking:false for claude-local/qwen38-27b-iq3s. That is the one level whose effect is
# deterministic on this template — the low-vs-medium token delta is prompt-dependent (measured)
# and is gated in router/test/effort-mapping.test.mjs, never here. First request after a model
# switch may include a llama-swap load, hence the long timeout.
note "checking POST /v1/messages (model=claude-local/qwen38-27b-iq3s, thinking disabled) ..."
effort_status=$(curl -s --max-time 300 -o /tmp/smoke-router-effort-body.$$ -w '%{http_code}' \
	-X POST "$BASE_URL/v1/messages" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-H 'anthropic-version: 2023-06-01' \
	-d '{"model":"claude-local/qwen38-27b-iq3s","max_tokens":128,"stream":false,"thinking":{"type":"disabled"},"messages":[{"role":"user","content":"Reply with the single word: pong"}]}') || effort_status=""
effort_json=$(cat /tmp/smoke-router-effort-body.$$ 2>/dev/null)
rm -f /tmp/smoke-router-effort-body.$$
effort_types=$(echo "$effort_json" | jq -r '[.content[]?.type] | join(",")' 2>/dev/null)
if [ "${effort_status#2}" != "$effort_status" ] && [ -n "$effort_types" ] && ! echo "$effort_types" | grep -q thinking; then
	pass "/v1/messages (claude-local/qwen38-27b-iq3s, thinking disabled) returned $effort_status with content blocks [$effort_types] — no thinking block"
else
	bad "/v1/messages (claude-local/qwen38-27b-iq3s, thinking disabled) expected 2xx with no thinking block (status=$effort_status, blocks=[$effort_types]). Response: $(echo "$effort_json" | head -c 300)"
fi

# ── 3. Missing / wrong gateway key both 401 by body ─────────────────────────────────────────
note "checking GET /v1/models with a missing gateway key ..."
missing_json=$(curl -s --max-time 10 "$BASE_URL/v1/models") || missing_json=""
missing_msg=$(echo "$missing_json" | jq -r '.error.message // empty' 2>/dev/null)
if [ -n "$missing_msg" ]; then
	pass "missing key rejected with body error: $missing_msg"
else
	bad "missing key did not produce a body error message. Response: $missing_json"
fi

note "checking GET /v1/models with a wrong gateway key ..."
wrong_json=$(curl -s --max-time 10 -H "x-gateway-key: wp1-smoke-wrong-key" "$BASE_URL/v1/models") || wrong_json=""
wrong_msg=$(echo "$wrong_json" | jq -r '.error.message // empty' 2>/dev/null)
if [ -n "$wrong_msg" ]; then
	pass "wrong key rejected with body error: $wrong_msg"
else
	bad "wrong key did not produce a body error message. Response: $wrong_json"
fi

# ── 4. Local chat completion via claude-local/qwen36-a3b ────────────────────────────────────
note "checking POST /v1/chat/completions (model=claude-local/qwen36-a3b) ..."
chat_json=$(curl -s --max-time 120 -X POST "$BASE_URL/v1/chat/completions" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d '{"model":"claude-local/qwen36-a3b","max_tokens":256,"stream":false,"messages":[{"role":"user","content":"Reply with the single word: pong"}]}') || chat_json=""
chat_content=$(echo "$chat_json" | jq -r '
	(.choices[0].message.content // empty)
	| if type == "string" and length > 0 then . else empty end
' 2>/dev/null)
if [ -n "$chat_content" ]; then
	pass "/v1/chat/completions (claude-local/qwen36-a3b) returned non-empty content"
else
	bad "/v1/chat/completions (claude-local/qwen36-a3b) did not return non-empty content. Response: $chat_json"
fi

# ── 5. Local-leg count_tokens (claude-local/qwen36-a3b) ─────────────────────────────────────
note "checking POST /v1/messages/count_tokens (model=claude-local/qwen36-a3b) ..."
count_tokens_json=$(curl -s --max-time 30 -X POST "$BASE_URL/v1/messages/count_tokens" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d '{"model":"claude-local/qwen36-a3b","messages":[{"role":"user","content":"Reply with the single word: pong"}]}') || count_tokens_json=""
count_tokens_value=$(echo "$count_tokens_json" | jq -r '
	.input_tokens
	| if type == "number" and (. == (. | floor)) then . else empty end
' 2>/dev/null)
if [ -n "$count_tokens_value" ]; then
	pass "/v1/messages/count_tokens (claude-local/qwen36-a3b) returned integer input_tokens=$count_tokens_value"
else
	bad "/v1/messages/count_tokens (claude-local/qwen36-a3b) did not return an integer input_tokens. Response: $count_tokens_json"
fi

# ── 6. count_tokens with an unresolvable model returns a 404-class JSON error body ──────────
note "checking POST /v1/messages/count_tokens with an unresolvable model ..."
wrong_model_status=$(curl -s --max-time 15 -o /tmp/smoke-router-wrong-model-body.$$ -w '%{http_code}' \
	-X POST "$BASE_URL/v1/messages/count_tokens" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d '{"model":"wp2-smoke-nonexistent-model","messages":[{"role":"user","content":"hi"}]}') || wrong_model_status=""
wrong_model_json=$(cat /tmp/smoke-router-wrong-model-body.$$ 2>/dev/null)
rm -f /tmp/smoke-router-wrong-model-body.$$
wrong_model_msg=$(echo "$wrong_model_json" | jq -r '.error.message // empty' 2>/dev/null)
if [ "$wrong_model_status" = "404" ] && [ -n "$wrong_model_msg" ]; then
	pass "/v1/messages/count_tokens with an unresolvable model returned 404 with a body error: $wrong_model_msg"
else
	bad "/v1/messages/count_tokens with an unresolvable model did not return a 404 body error (status=$wrong_model_status). Response: $wrong_model_json"
fi

# ── 7. Hosted-leg subscription probe (held; never run unattended) ──────────────────────────
if [ "$SUBSCRIPTION" -eq 1 ]; then
	note "--subscription passed: running one claude -p call through $BASE_URL on the saved claude.ai login."
	note "This bills the operator's real Claude subscription."
	if ! command -v claude >/dev/null 2>&1; then
		bad "--subscription: 'claude' CLI not found on PATH"
	else
		sub_out=$(ANTHROPIC_BASE_URL="$BASE_URL" ANTHROPIC_CUSTOM_HEADERS="x-gateway-key: $GATEWAY_KEY" claude -p "Reply with the single word: pong" 2>&1)
		sub_rc=$?
		if [ "$sub_rc" -eq 0 ] && [ -n "$sub_out" ]; then
			pass "--subscription: claude -p returned a non-empty completion through the hosted leg"
		else
			bad "--subscription: claude -p failed (rc=$sub_rc). Output: $sub_out"
		fi
	fi
else
	note "--subscription not passed: SKIPPING the hosted-leg claude -p probe (never run unattended — it bills"
	note "the operator's live subscription). What it would run:"
	note "  ANTHROPIC_BASE_URL=$BASE_URL ANTHROPIC_CUSTOM_HEADERS='x-gateway-key: <key>' claude -p 'Reply with the single word: pong'"
fi

# ── 8. Codex sidecar leg, metered (--codex only) ────────────────────────────────────────────
if [ "$CODEX" -eq 1 ]; then
	note "--codex passed: running one non-streamed codex-a/gpt-5.6-luna turn through $BASE_URL (bills the operator's Codex subscription)."
	# The engine's anthropic-type leg needs *some* client bearer to build passthrough headers
	# (openrouter-spike finding 3); the provider's apiKeyEnv replaces it before the sidecar.
	codex_json=$(curl -s --max-time 180 -X POST "$BASE_URL/v1/messages" \
		-H 'Content-Type: application/json' \
		-H "x-gateway-key: $GATEWAY_KEY" \
		-H 'authorization: Bearer smoke-dummy-client-bearer' \
		-H 'anthropic-version: 2023-06-01' \
		-d '{"model":"codex-a/gpt-5.6-luna","max_tokens":32,"stream":false,"metadata":{"user_id":"{\"session_id\":\"smoke-router-codex\"}"},"messages":[{"role":"user","content":"Reply with the single word: pong"}]}') || codex_json=""
	# Anthropic usage semantics (2026-09-13): input_tokens is the *uncached* prompt share, so a
	# fully cached turn legitimately reports 0 — output_tokens is the liveness signal here.
	codex_out=$(echo "$codex_json" | jq -r '.usage.output_tokens | if type == "number" and . > 0 then . else empty end' 2>/dev/null)
	if [ -n "$codex_out" ]; then
		pass "/v1/messages (codex-a/gpt-5.6-luna) returned usage.output_tokens=$codex_out"
	else
		bad "/v1/messages (codex-a/gpt-5.6-luna) did not return usage.output_tokens > 0. Response: $codex_json"
	fi
else
	note "--codex not passed: SKIPPING the metered codex-a/gpt-5.6-luna probe (one Codex-subscription turn)."
fi

# ── 9. Codex sidecar count_tokens (unmetered) ───────────────────────────────────────────────
note "checking POST /v1/messages/count_tokens (model=codex-a/gpt-5.6-luna) ..."
codex_ct_json=$(curl -s --max-time 30 -X POST "$BASE_URL/v1/messages/count_tokens" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-H 'authorization: Bearer smoke-dummy-client-bearer' \
	-d '{"model":"codex-a/gpt-5.6-luna","messages":[{"role":"user","content":"Reply with the single word: pong"}]}') || codex_ct_json=""
codex_ct_value=$(echo "$codex_ct_json" | jq -r '
	.input_tokens
	| if type == "number" and (. == (. | floor)) then . else empty end
' 2>/dev/null)
if [ -n "$codex_ct_value" ]; then
	pass "/v1/messages/count_tokens (codex-a/gpt-5.6-luna) returned integer input_tokens=$codex_ct_value"
else
	bad "/v1/messages/count_tokens (codex-a/gpt-5.6-luna) did not return an integer input_tokens. Response: $codex_ct_json"
fi

echo
if [ "$fail" -eq 0 ]; then
	echo "[smoke] all checks passed"
	exit 0
else
	echo "[smoke] one or more checks FAILED" >&2
	exit 1
fi
